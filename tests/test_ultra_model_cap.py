"""``route ultra`` when a model answers with a usage cap instead of doing the work.

On 18 Nov 2026 the planner arm (claude-work-fable) printed "You've hit your
Fable limit" and exited; ultra read that as "no plan block", asked the same arm
again and gave up. These runs use shell stand-ins for every model: an arm is
capped by dropping ``cap-<script>`` (every role) or ``cap-<script>-<role>`` in
the fake directory, and then prints that file and exits 1, as the CLIs do.
"""

from __future__ import annotations

import json
import os
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from route import caps, cli, swarm_dispatch, ultra_select
from route.eligibility import QuotaSnapshot
from route.pools import load_pools, pools_for_host
from route.ultra import run_ultra, ultra_status
from tests import test_ultra as build
from tests import test_ultra_research as research
from tests import test_ultra_team as team
from tests.test_ultra import repo  # noqa: F401  (a fixture)

NOW = datetime(2026, 11, 18, 18, 39, tzinfo=timezone.utc).timestamp()
RESET = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc).timestamp()
FABLE_LIMIT = ("You've hit your Fable limit · resets Nov 23 at 3pm (Europe/London). Switch to another model, or "
               "manage usage credits at https://claude.ai/settings/usage?from=cc_cli_limit_message, to continue.\n")
WEEKLY_LIMIT = "You've hit your weekly limit · resets Nov 24 at 9am (Europe/London).\n"
OPUS_LIMIT = "You've hit your Opus limit · resets Nov 25 at 9am (Europe/London).\n"
CODEX_LIMIT = "ERROR: You've hit your usage limit. Upgrade to Pro or try again at Nov 26th, 2026 9:00 AM (UTC).\n"
FABLE_HIT = "claude-work-fable hit its Fable limit (resets Nov 23 15:00)"
QUOTA = {"claude": "critical", "claude-work": "ok", "codex": "ok", "kimi": "critical", "deepseek": "critical"}

#: Spliced into each harness's stand-in right after it logs the call: print the cap and exit, as a capped CLI does.
CAP_SNIPPET = r'''
for capfile in "$FAKE_DIR/cap-$(basename "$0")" "$FAKE_DIR/cap-$(basename "$0")-$role"; do
  if [ -f "$capfile" ]; then cat "$capfile"; exit 1; fi
done
'''


def cappable(script_text: str) -> str:
    """A harness's fake arm that can also be told to answer with a usage cap."""
    lines = script_text.splitlines(keepends=True)
    [logged] = [index for index, line in enumerate(lines) if 'calls.log' in line and line.startswith("echo")]
    return "".join(lines[:logged + 1]) + CAP_SNIPPET + "".join(lines[logged + 1:])


def cap(fake_dir: Path, script: str, text: str, role: str | None = None) -> None:
    (fake_dir / (f"cap-{script}" + (f"-{role}" if role else ""))).write_text(text)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """The day of the incident, so "Nov 23" is five days away whenever the suite runs."""
    now = [NOW]
    monkeypatch.setattr(caps, "_clock", lambda: now[0])
    return now


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Fable and Opus on the second Claude account, and Codex: the arms of the incident."""
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    for name in ("fake-fable", "fake-opus", "fake-openai"):
        script = fake_dir / name
        script.write_text(cappable(build.FAKE_ARM).replace("__FAKE_DIR__", str(fake_dir)))
        script.chmod(0o700)
    config = Path(os.environ["ROUTE_CONFIG_DIR"])
    config.mkdir(parents=True, exist_ok=True)
    (config / "pools.toml").write_text(textwrap.dedent(f"""
        [pools.claude-work-fable]
        shapes = {build.CODING}
        remote = true
        dispatch = "{fake_dir}/fake-fable --print {{task}}"
        model = {{ name = "claude-fable-5-2" }}
        parallel_limit = 1
        quota_provider = "claude-work"
        quota_scope = "Fable"

        [pools.claude-work]
        shapes = {build.CODING}
        remote = true
        dispatch = "{fake_dir}/fake-opus --print {{task}}"
        model = {{ name = "claude-opus-5-5" }}
        parallel_limit = 4
        quota_provider = "claude-work"

        [pools.codex]
        shapes = {build.CODING}
        remote = true
        needs_repo = true
        dispatch = "{fake_dir}/fake-openai exec {{task}}"
        model = {{ name = "gpt-6-astra" }}
        parallel_limit = 4
    """))
    monkeypatch.setattr(cli, "fetch_quota", lambda *a, **k: dict(QUOTA))
    monkeypatch.setattr(cli, "fetch_usage", lambda *a, **k: {})
    monkeypatch.setattr(swarm_dispatch, "amail_binary", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("ultra must never prompt"))
    yield fake_dir
    ultra_select.set_pin(())


def by_script(fake_dir: Path, script: str) -> list[tuple[str, str]]:
    """(role, unit) for every time one stand-in was started, capped or not."""
    return [(role, unit) for name, role, unit, _ in build.calls(fake_dir) if name == script]


# -- planner -------------------------------------------------------------------

def test_a_capped_planner_is_flagged_and_planning_moves_to_the_next_arm(repo, fake, capsys):  # noqa: F811
    # Arrange: the Fable arm answers every prompt with the message from the incident.
    build.write_plan(fake, build.plan(build.two_units()))
    cap(fake, "fake-fable", FABLE_LIMIT)
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude")
    # Assert: the run finished, on Opus, having asked Fable exactly once.
    captured = capsys.readouterr()
    summary, run_dir = build.latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    assert by_script(fake, "fake-fable") == [("planner", "plan")]
    assert by_script(fake, "fake-opus")[0] == ("planner", "plan")
    assert summary["planner"] == "claude-work"
    assert {unit["arm"] for unit in summary["units"]} == {"claude-work"}
    # One stderr line at the moment it happened.
    flag = f"ultra: {FABLE_HIT}; planning on claude-work (Opus) instead"
    assert captured.err.count(flag) == 1 and "re-planning once" not in captured.out
    # progress.json, summary.json and summary.md carry the quota event.
    progress = json.loads((run_dir / "progress.json").read_text())
    [event] = progress["quota_events"]
    assert (event["role"], event["arm"], event["model"], event["fallback"], event["fallback_model"]) == (
        "planner", "claude-work-fable", "Fable", "claude-work", "Opus")
    assert event["resets"] == "Nov 23 15:00" and event["resets_at"] == RESET
    assert event["keys"] == ["claude-work:Fable", "arm:claude-work-fable"] and event["also_out"] == []
    assert summary["quota_events"] == progress["quota_events"] and progress["plan_problems"] == []
    text = (run_dir / "summary.md").read_text()
    assert f"Quota events:\n- {FABLE_HIT}; planning on claude-work (Opus) instead\n" in text
    # The capped attempt's log is kept beside the plan the next arm wrote.
    assert (run_dir / "logs" / "plan-1.capped-claude-work-fable.log").read_text() == FABLE_LIMIT
    assert "ULTRA-MANIFEST-BEGIN" in (run_dir / "logs" / "plan-1.log").read_text()
    # `route ultra status` says it too.
    assert ultra_status(summary["run_id"]) == 0
    assert f"quota: {FABLE_HIT}; planning on claude-work (Opus) instead" in capsys.readouterr().out


def test_the_cap_is_remembered_so_the_next_run_skips_the_window_until_it_resets(repo, fake, frozen_clock,  # noqa: F811
                                                                              monkeypatch, capsys):
    # Arrange: one run meets the cap; later runs read quota the way fetch_quota does, caps laid over it.
    def snapshot(*args, **kwargs):
        quota = QuotaSnapshot(dict(QUOTA), {})
        quota.caps = caps.apply_caps(quota)
        return quota

    monkeypatch.setattr(cli, "fetch_quota", snapshot)
    build.write_plan(fake, build.plan(build.two_units()))
    cap(fake, "fake-fable", FABLE_LIMIT)
    assert run_ultra("Create two files", repo=str(repo), host="claude", plan_only=True) == 0
    first_id = json.loads(caps.caps_path().read_text())["caps"][0]["source"]
    capsys.readouterr()
    pools = pools_for_host(load_pools(), "claude")
    # Assert: the record names the window and its reset, and routing reads it as spent.
    [record] = caps.active_caps()
    assert (record["arm"], record["keys"], record["until"]) == (
        "claude-work-fable", ["claude-work:Fable", "arm:claude-work-fable"], RESET)
    assert first_id.startswith("ultra:create-two-files-") and first_id.endswith(":planner")
    assert ultra_select.pick("planner", pools, snapshot()) == "claude-work"

    # Act: a second run, an hour later.
    frozen_clock[0] = NOW + 3600
    assert run_ultra("Create two more files", repo=str(repo), host="claude", plan_only=True) == 0
    # Assert: Fable was not asked again, and the run says why it planned on its second choice.
    assert by_script(fake, "fake-fable") == [("planner", "plan")]
    skipped = f"ultra: skipped claude-work-fable: already at its Fable limit until Nov 23 15:00 (seen by {first_id})"
    assert skipped in capsys.readouterr().err
    second = max((path for path in caps.caps_path().parent.glob("ultra/*/progress.json")),
                 key=lambda path: path.stat().st_mtime)
    progress = json.loads(second.read_text())
    assert progress["quota_events"] == [] and progress["planner"] == "claude-work"
    assert [row["arms"] for row in progress["quota_standing"]] == [["claude-work-fable"]]

    # Act: the reset passes.
    frozen_clock[0] = RESET + 60
    # Assert: the window is offered again.
    assert caps.active_caps() == []
    assert ultra_select.pick("planner", pools, snapshot()) == "claude-work-fable"


def test_every_planner_capped_exits_2_naming_the_caps_the_resets_and_a_rerun(repo, fake, capsys):  # noqa: F811
    # Arrange: Fable, then the whole second account, then Codex.
    build.write_plan(fake, build.plan(build.two_units()))
    cap(fake, "fake-fable", FABLE_LIMIT)
    cap(fake, "fake-opus", WEEKLY_LIMIT)
    cap(fake, "fake-openai", CODEX_LIMIT)
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude")
    # Assert: exit 2, each arm asked once, in rank order, and never again.
    captured = capsys.readouterr()
    summary, run_dir = build.latest_run()
    assert rc == 2 and summary["exit_code"] == 2 and summary["units"] == []
    assert [name for name, *_ in build.calls(fake)] == ["fake-fable", "fake-opus", "fake-openai"]
    message = (f"could not plan: no model left for planning: {FABLE_HIT}; "
               "claude-work hit its weekly limit (resets Nov 24 09:00); "
               "codex hit its usage limit (resets Nov 26 09:00). "
               "No other model has quota: rerun after the reset (Nov 23 15:00).")
    assert f"route ultra: {message}" in captured.err
    assert "no ULTRA-MANIFEST-BEGIN" not in captured.out + captured.err
    assert message in summary["notes"] and message in (run_dir / "summary.md").read_text()
    assert [(event["arm"], event["fallback"]) for event in summary["quota_events"]] == [
        ("claude-work-fable", "claude-work"), ("claude-work", "codex"), ("codex", None)]
    assert "ultra: codex hit its usage limit (resets Nov 26 09:00); no other arm left for planning" in captured.err
    # The account-wide cap also took the Fable arm's account away.
    assert summary["quota_events"][1]["also_out"] == ["claude-work-fable"]


def test_a_pinned_run_that_is_capped_out_suggests_the_pool_that_still_has_quota(repo, fake, capsys):  # noqa: F811
    # Arrange
    build.write_plan(fake, build.plan(build.two_units()))
    cap(fake, "fake-fable", FABLE_LIMIT)
    # Act: --pool claude-work-fable, as a run that wants Fable only.
    rc = run_ultra("Create two files", repo=str(repo), host="claude", pin=("claude-work-fable",))
    # Assert: the pin holds (nothing else is asked) and the message names the way out.
    assert rc == 2
    assert [name for name, *_ in build.calls(fake)] == ["fake-fable"]
    assert (f"route ultra: could not plan: no model left for planning: {FABLE_HIT}. "
            "Rerun with --pool claude-work, or wait for the reset (Nov 23 15:00).") in capsys.readouterr().err


def test_lower_models_are_suggested_when_only_they_have_quota(repo, fake, monkeypatch, capsys):  # noqa: F811
    # Arrange: Kimi (a rank below ultra's floor) has quota; everything above it is capped.
    monkeypatch.setattr(cli, "fetch_quota", lambda *a, **k: {**QUOTA, "kimi": "ok"})
    build.write_plan(fake, build.plan(build.two_units()))
    for script, text in (("fake-fable", FABLE_LIMIT), ("fake-opus", WEEKLY_LIMIT), ("fake-openai", CODEX_LIMIT)):
        cap(fake, script, text)
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude")
    # Assert
    assert rc == 2
    assert "Rerun with --allow-lower-models (kimi has quota), or wait for the reset" in capsys.readouterr().err


# -- worker --------------------------------------------------------------------

def test_a_capped_worker_is_rebuilt_on_the_next_arm_and_not_graded(repo, fake, monkeypatch, capsys):  # noqa: F811
    # Arrange: Fable plans, then hits its cap when the hard units reach it as a worker.
    units = [build.unit("alpha", touches=["alpha.txt"], write="alpha.txt alpha", tier="hard"),
             build.unit("beta", touches=["beta.txt"], write="beta.txt beta", tier="hard")]
    build.write_plan(fake, build.plan(units))
    cap(fake, "fake-fable", FABLE_LIMIT, role="worker")
    settles = build.settle_recorder(monkeypatch)
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude")
    # Assert: both units were built by Opus, reviewed by another family, merged, and the goal check passed.
    captured = capsys.readouterr()
    summary, run_dir = build.latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    assert summary["planner"] == "claude-work-fable"
    rows = {unit["id"]: unit for unit in summary["units"]}
    for unit_id in ("alpha", "beta"):
        assert (rows[unit_id]["arm"], rows[unit_id]["state"], rows[unit_id]["rebuilt_after_cap"]) == (
            "claude-work", "merged", "claude-work-fable")
        assert rows[unit_id]["reviewer"] == "codex" and rows[unit_id]["fix_passes"] == 0
        assert captured.err.count(f"ultra: {FABLE_HIT}; building {unit_id} on claude-work (Opus) instead") == 1
        assert "Acceptance check: bash tests/check_" in (run_dir / "logs" / f"build-{unit_id}.log").read_text()
    # Fable was asked to build once; the unit queued behind that one was not sent to ask again.
    assert [role for role, _ in by_script(fake, "fake-fable")] == ["planner", "worker"]
    assert sorted(call for call in by_script(fake, "fake-opus") if call[0] == "worker") == [
        ("worker", "alpha"), ("worker", "beta")]
    assert build.git(repo, "show", f"ultra/{summary['run_id']}:alpha.txt").strip() == "alpha"
    # The swarm recorded the cap rather than a failed check, and nothing grades Fable for work it never did.
    swarm = json.loads((swarm_dispatch.swarm_dir(summary["run_id"]) / "summary.json").read_text())
    assert [row["capped"]["model"] for row in swarm["units"]] == ["Fable", "Fable"] and swarm["outcomes"] == []
    notes = sorted(Path(row["log"]).read_text().strip().splitlines()[-1] for row in swarm["units"])
    assert notes == ["Not checked: the arm reported a usage cap, so nothing was built.",
                     "Not started: the arm is at a usage cap, so nothing was built."]
    assert settles == [] and {row["settled"] for row in rows.values()} == {False}
    assert captured.err.count(f"{FABLE_HIT}; not checked") == 2
    assert [event["role"] for event in summary["quota_events"]] == ["worker", "worker"]


def test_a_capped_worker_with_no_arm_left_fails_its_unit_with_the_reason(repo, fake, capsys):  # noqa: F811
    # Arrange: a run pinned to Fable, which plans and then runs out.
    build.write_plan(fake, build.plan(build.two_units()))
    cap(fake, "fake-fable", FABLE_LIMIT, role="worker")
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude", pin=("claude-work-fable",))
    # Assert
    summary, _ = build.latest_run()
    assert rc == 1
    for unit in summary["units"]:
        assert unit["state"] == "failed" and unit["fix_passes"] == 0
        assert unit["reason"].startswith(f"not built: no model left for building {unit['id']}: {FABLE_HIT}. "
                                         "Rerun with --pool claude-work")
    assert [role for role, _ in by_script(fake, "fake-opus")] == []
    assert "no other arm left for building alpha" in capsys.readouterr().err


# -- reviewer and fixer ----------------------------------------------------------

def test_a_capped_reviewer_hands_the_review_to_the_next_arm(repo, fake, capsys):  # noqa: F811
    # Arrange: Opus builds; Codex, the cross-family reviewer, is at its cap.
    build.write_plan(fake, build.plan(build.two_units()))
    cap(fake, "fake-openai", CODEX_LIMIT, role="reviewer")
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude", pin=("claude-work",))
    # Assert: the review still happened, on the only other arm, and is reported as same-family.
    captured = capsys.readouterr()
    summary, run_dir = build.latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    for unit in summary["units"]:
        assert (unit["arm"], unit["reviewer"], unit["independence"]) == ("claude-work", "claude-work-fable",
                                                                         "same-family")
    assert captured.err.count("codex hit its usage limit (resets Nov 26 09:00); reviewing ") == 2
    assert "on claude-work-fable (Fable) instead" in captured.err
    assert sorted(by_script(fake, "fake-fable")) == [("reviewer", "alpha"), ("reviewer", "beta")]
    assert len(by_script(fake, "fake-openai")) == 2


def test_a_capped_fixer_passes_the_same_fix_pass_to_a_sibling(repo, fake, capsys):  # noqa: F811
    # Arrange: Opus builds alpha, Codex confirms a bug, and Opus is capped when asked to fix it.
    build.write_plan(fake, build.plan(build.two_units()))
    (fake / "findings-alpha.json").write_text(json.dumps([
        {"title": "alpha is not fixed", "severity": "high", "detail": "ultra-fake: fix alpha.txt fixed",
         "repro": "bash tests/repro_alpha.sh"}]))
    cap(fake, "fake-opus", OPUS_LIMIT, role="fixer")
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude", pin=("claude-work", "claude-work-fable"))
    # Assert: one fix pass, done by the Fable arm of the same family; the Opus arm is out by name.
    summary, run_dir = build.latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    alpha = next(unit for unit in summary["units"] if unit["id"] == "alpha")
    assert (alpha["state"], alpha["fix_passes"]) == ("merged", 1)
    progress = json.loads((run_dir / "progress.json").read_text())
    assert progress["units"]["alpha"]["fixers"] == ["claude-work-fable"]
    [event] = summary["quota_events"]
    assert (event["role"], event["arm"], event["keys"]) == ("fixer", "claude-work", ["claude-work:Opus",
                                                                                   "arm:claude-work"])
    assert ("ultra: claude-work hit its Opus limit (resets Nov 25 09:00); fixing alpha on claude-work-fable "
            "(Fable) instead") in capsys.readouterr().err
    assert build.git(repo, "show", f"ultra/{summary['run_id']}:alpha.txt").strip() == "fixed"


# -- research and team roles -----------------------------------------------------

@pytest.fixture
def research_fake(tmp_path, monkeypatch):
    """The research harness's two arms (claude-work and codex), able to answer with a cap."""
    fake_dir = tmp_path / "fake"
    (fake_dir / "answers").mkdir(parents=True)
    (fake_dir / "analyses").mkdir()
    (fake_dir / "verdicts").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setattr(cli, "fetch_quota", lambda *a, **k: dict(QUOTA))
    monkeypatch.setattr(cli, "fetch_usage", lambda *a, **k: {})
    monkeypatch.setattr(swarm_dispatch, "amail_binary", lambda: None)

    def install(script_text: str) -> Path:
        for name in ("claude-work", "codex"):
            script = bin_dir / name
            script.write_text(cappable(script_text).replace("__FAKE_DIR__", str(fake_dir)))
            script.chmod(0o700)
        team.write_pools(tmp_path, bin_dir)
        return fake_dir

    return install


def test_research_moves_a_capped_planner_and_a_capped_part_to_the_other_arm(research_fake, capsys):
    # Arrange: the second Claude account is out for the week, from the first prompt on.
    fake_dir = research_fake(research.FAKE_ARM)
    research.write_plan(fake_dir, [research.part("alpha", "What is alpha?"), research.part("beta", "What is beta?")])
    research.standard_answers(fake_dir)
    cap(fake_dir, "claude-work", WEEKLY_LIMIT)
    # Act
    rc = research.run_research(research.QUESTION, host="claude")
    # Assert: planned, researched and written up on Codex; the account was asked once.
    captured = capsys.readouterr()
    summary, run_dir = research.latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    assert [row[0] for row in research.calls(fake_dir)].count("claude-work") == 1
    assert summary["planner"] == "codex" and summary["synthesizer"] == "codex"
    assert {row["arm"] for row in summary["parts"]} == {"codex"}
    line = "claude-work hit its weekly limit (resets Nov 24 09:00); planning on codex (gpt-6-astra) instead"
    assert f"ultra: {line}" in captured.err
    assert f"- Quota events:\n  - {line}\n" in (run_dir / "summary.md").read_text()
    assert [event["role"] for event in summary["quota_events"]] == ["planner"]


def test_a_capped_research_part_is_not_one_of_its_two_tries(research_fake, capsys):
    # Arrange: Opus plans, then is capped when the parts reach it; Codex's first answer for alpha is unusable.
    fake_dir = research_fake(research.FAKE_ARM)
    research.write_plan(fake_dir, [research.part("alpha", "What is alpha?")])
    research.standard_answers(fake_dir)
    cap(fake_dir, "claude-work", OPUS_LIMIT, role="part")
    # Act
    rc = research.run_research(research.QUESTION, host="claude")
    # Assert: the part moved to Codex and was answered there.
    summary, run_dir = research.latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    [alpha] = summary["parts"]
    assert (alpha["state"], alpha["arm"], alpha["attempts"]) == ("answered", "codex", ["claude-work", "codex"])
    assert ("ultra: claude-work hit its Opus limit (resets Nov 25 09:00); researching alpha on codex "
            "(gpt-6-astra) instead") in capsys.readouterr().err
    assert [event["role"] for event in summary["quota_events"]] == ["researcher"]


def test_a_capped_team_verifier_hands_its_vote_to_another_arm(research_fake, capsys):
    # Arrange: the standard site check; Codex is capped whenever it is asked to verify a claim.
    fake_dir = research_fake(team.FAKE_ARM)
    team.write_layouts()
    team.standard_analyses(fake_dir)
    cap(fake_dir, "codex", CODEX_LIMIT, role="verifier")
    # Act
    rc = team.run_team(team.QUESTION, layout="site-check", urls=(team.URL,), host="claude")
    # Assert: every claim still got a verdict; the ones Codex could not give came from Opus, and say so.
    summary, run_dir = team.latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0, text
    verdicts = {row["id"]: row for row in json.loads((run_dir / "verdicts.json").read_text())}
    assert verdicts["C1"]["verdicts"][0]["arm"] == "claude-work" and verdicts["C1"]["independence"] == "same-family"
    assert verdicts["C3"]["verdicts"][0]["arm"] == "claude-work" and verdicts["C3"]["independence"] == "cross-family"
    assert summary["verdict_counts"]["not cross-examined"] == 0
    assert {event["role"] for event in summary["quota_events"]} == {"verifier"}
    assert "- Quota events:\n  - codex hit its usage limit (resets Nov 26 09:00); verifying C" in text
    assert "on claude-work (Opus) instead" in capsys.readouterr().err
