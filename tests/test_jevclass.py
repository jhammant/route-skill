"""The optional Jev classifier: off by default, never for sensitive work, never able to break a plan.

No test reaches the network: `classify` is given a stand-in `run`, and plan tests patch `jevclass.classify`.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from route import cli, jevclass
from route.complexity import TIERS, estimate_complexity
from route.jevclass import JevReading
from route.shapes import SHAPES, ShapeClassification, classify_with_confidence

REPORT_TASK = "Build a report generator that summarises each customer's usage for the month and emails it."


def reply(shape="coding:implement", probability=0.99, score=2.1):
    return {"answers": {"shape": {"type": "choice", "choice": shape, "probabilities": {shape: probability}},
                        "tier": {"type": "score", "score": score}}}


def fake_run(payload, seen=None, returncode=0):
    def run(cmd, **kw):
        if seen is not None:
            seen.append({"cmd": cmd, "input": kw.get("input"),
                         "questions": json.loads(open(cmd[cmd.index("--questions") + 1]).read())})
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr="")
    return run


# ---- on and off ----

def test_off_by_default(isolated_env):
    assert jevclass.enabled() is False


def test_the_environment_turns_it_on_and_beats_the_config(isolated_env, monkeypatch):
    config = isolated_env / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text("[classify]\njev = true\n")
    assert jevclass.enabled() is True
    monkeypatch.setenv("ROUTE_JEV", "0")
    assert jevclass.enabled() is False
    monkeypatch.setenv("ROUTE_JEV", "1")
    (config / "config.toml").write_text("")
    assert jevclass.enabled() is True


def test_a_config_value_that_is_not_true_stays_off(isolated_env):
    config = isolated_env / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text('[classify]\njev = "yes"\n')
    assert jevclass.enabled() is False


# ---- the questions ----

def test_every_shape_and_tier_is_described():
    q = jevclass.questions()
    assert tuple(q["shape"]["criteria"]) == SHAPES
    assert len(q["tier"]["criteria"]) == len(TIERS)


# ---- one call ----

def test_classify_sends_the_task_on_stdin_and_reads_shape_and_tier():
    seen = []
    reading = jevclass.classify(REPORT_TASK, run=fake_run(reply(), seen), env={})
    assert reading == JevReading("coding:implement", 0.99, "moderate")
    assert seen[0]["input"] == REPORT_TASK
    assert seen[0]["cmd"][1:2] == ["ask"] and "--json" in seen[0]["cmd"]
    assert set(seen[0]["questions"]) == {"shape", "tier"}


def test_classify_truncates_a_long_brief():
    seen = []
    jevclass.classify("x" * 50_000, run=fake_run(reply(), seen), env={})
    assert len(seen[0]["input"]) == jevclass.MAX_CHARS


@pytest.mark.parametrize("run", [
    fake_run(reply(), returncode=2),                                                    # the tool refused
    lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="not json", stderr=""),      # rubbish back
    lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 8)),         # too slow
    lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError(cmd[0])),                 # no jev command at all
    fake_run({"answers": {}}),                                                          # nothing usable
])
def test_any_failure_is_none_never_an_error(run):
    assert jevclass.classify(REPORT_TASK, run=run, env={}) is None


def test_falls_back_to_the_link_in_local_bin_when_jev_is_not_on_path():
    tried = []

    def run(cmd, **kw):
        tried.append(cmd[0])
        if cmd[0] == "jev":
            raise FileNotFoundError("jev")
        return SimpleNamespace(returncode=0, stdout=json.dumps(reply()), stderr="")

    assert jevclass.classify(REPORT_TASK, run=run, env={}) is not None
    assert tried[0] == "jev" and tried[1].endswith("/.local/bin/jev")


def test_an_answer_outside_the_closed_vocabulary_is_dropped():
    reading = jevclass.parse(reply(shape="coding:rewrite-everything"))
    assert reading.shape is None and reading.tier == "moderate"


def test_the_tier_is_the_rounded_score_kept_inside_the_scale():
    assert jevclass.parse(reply(score=0.4)).tier == "trivial"
    assert jevclass.parse(reply(score=2.6)).tier == "hard"
    assert jevclass.parse(reply(score=9)).tier == "open-ended"
    assert jevclass.parse(reply(score="high")).tier is None


# ---- combining with the rules ----

RULES = ShapeClassification("batch:summarize", "high", "deterministic")


def test_a_sure_answer_replaces_the_rules():
    got = jevclass.refine_shape(RULES, JevReading("coding:implement", 0.99, None), REPORT_TASK)
    assert got == ShapeClassification("coding:implement", "high", "jev")


def test_an_unsure_answer_leaves_the_rules_alone():
    assert jevclass.refine_shape(RULES, JevReading("coding:implement", 0.49, None), REPORT_TASK) is RULES
    assert jevclass.refine_shape(RULES, None, REPORT_TASK) is RULES


def test_a_batch_answer_needs_the_rules_own_volume_cue():
    rules = ShapeClassification("coding:implement", "high", "deterministic")
    one_thing = "Summarise the design doc."
    assert jevclass.refine_shape(rules, JevReading("batch:summarize", 0.99, None), one_thing) is rules
    many = "Summarise every one of these 250 transcripts."
    assert jevclass.refine_shape(rules, JevReading("batch:summarize", 0.99, None), many).shape == "batch:summarize"


def test_floors_still_apply_to_a_jev_tier():
    feature = ("Implement a weekly digest feature: add a migration, an endpoint and a webhook, with tests. "
               "Work on a branch.")
    assert estimate_complexity(feature, use_taskgauge=False, estimate="simple") == "hard"
    assert estimate_complexity("Fix the typo.", use_taskgauge=False, estimate="moderate") == "moderate"
    assert estimate_complexity("Fix the typo.", use_taskgauge=False, estimate="nonsense") == "trivial"


# ---- in the planner ----

import argparse


def _options(**over):
    base = dict(host="claude", pool=None, sensitive=False, shape=None, tier=None, no_quota=True,
                allow_network=False, private=False, no_acceptance=False, needs_context=False,
                cross_repo=False, llm_shape=False, swarm=None)
    return argparse.Namespace(**{**base, **over})


@pytest.fixture
def jev_on(monkeypatch):
    """Turned on, with a stand-in for the call; returns the list of task texts that were sent."""
    sent = []
    monkeypatch.setenv("ROUTE_JEV", "1")
    monkeypatch.setattr(jevclass, "classify",
                        lambda task, **kw: (sent.append(task), JevReading("coding:implement", 0.99, "moderate"))[1])
    monkeypatch.setattr("route.complexity._estimate_taskgauge", lambda text, timeout=5.0: None)
    return sent


def _plan(text, **over):
    plan, _ = cli.plan_task(text, _options(**over), quota={}, record=False)
    return plan


def test_the_rules_alone_misread_the_report_task():
    assert classify_with_confidence(REPORT_TASK).shape == "batch:summarize"


def test_turned_on_the_plan_uses_jevs_shape_and_tier(jev_on):
    plan = _plan(REPORT_TASK)
    assert (plan["shape"], plan["shape_source"]) == ("coding:implement", "jev")
    assert (plan["tier"], plan["tier_source"]) == ("moderate", "jev")
    assert jev_on == [REPORT_TASK]


def test_turned_off_nothing_is_sent_and_the_rules_decide(jev_on, monkeypatch):
    monkeypatch.setenv("ROUTE_JEV", "0")
    plan = _plan(REPORT_TASK)
    assert plan["shape_source"] == "deterministic" and plan["tier_source"] == "estimate"
    assert jev_on == []


def test_no_jev_flag_skips_it_for_one_task(jev_on):
    assert _plan(REPORT_TASK, no_jev=True)["shape_source"] == "deterministic"
    assert jev_on == []


def test_a_sensitive_task_is_never_sent(jev_on):
    _plan(REPORT_TASK, sensitive=True)
    assert jev_on == []


def test_a_private_task_is_never_sent(jev_on):
    _plan(REPORT_TASK, private=True)
    assert jev_on == []


def test_a_task_the_safety_net_thinks_is_private_is_never_sent(jev_on, monkeypatch):
    monkeypatch.setattr(cli, "probably_private", lambda text: True)
    _plan(REPORT_TASK)
    assert jev_on == []


def test_an_explicit_shape_and_tier_are_never_second_guessed(jev_on):
    plan = _plan(REPORT_TASK, shape="research", tier="hard")
    assert (plan["shape"], plan["shape_source"], plan["tier"], plan["tier_source"]) == (
        "research", "explicit", "hard", "explicit")
    assert jev_on == [], "nothing left to ask"


def test_an_explicit_shape_still_lets_jev_fill_in_the_tier(jev_on):
    plan = _plan(REPORT_TASK, shape="research")
    assert (plan["shape"], plan["tier"], plan["tier_source"]) == ("research", "moderate", "jev")


def test_a_failed_call_leaves_the_plan_as_the_rules_made_it(jev_on, monkeypatch):
    monkeypatch.setattr(jevclass, "classify", lambda task, **kw: None)
    plan = _plan(REPORT_TASK)
    assert plan["shape_source"] == "deterministic" and plan["tier_source"] == "estimate"
