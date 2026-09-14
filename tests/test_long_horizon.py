"""GPT-6 Astra: the codex arm tracks what codex actually runs, and
long-horizon coding cells favour it.

Deterministic: MeanRNG makes Thompson pick the best posterior mean.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from route.cli import main
from route.complexity import estimate_complexity, is_long_horizon
from route.pools import CODEX_DEFAULT_MODEL, codex_model, load_pools
from route.router import load_priors


# -- model identity ----------------------------------------------------------

def test_codex_model_defaults_to_astra_without_a_config(tmp_path):
    assert codex_model(tmp_path / "missing.toml").name == CODEX_DEFAULT_MODEL
    assert CODEX_DEFAULT_MODEL == "gpt-6-astra"


def test_codex_model_reads_codex_home_config(tmp_path, monkeypatch):
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True)
    (home / "config.toml").write_text('model = "gpt-5.6-sol"\nmodel_reasoning_effort = "max"\n')
    assert load_pools()["codex"].model.ident() == "gpt-5.6-sol"


def test_codex_model_ignores_a_broken_config(tmp_path):
    bad = tmp_path / "config.toml"
    bad.write_text("model = [not toml")
    assert codex_model(bad).name == CODEX_DEFAULT_MODEL


def test_default_registry_pins_astra(pools):
    assert pools["codex"].model.ident() == "gpt-6-astra"


# -- long-horizon cue --------------------------------------------------------

def test_long_horizon_cues():
    assert is_long_horizon("keep going until the whole test suite passes")
    assert is_long_horizon("port every call site of the old client, all 140 usages")
    assert is_long_horizon("port every call site of the old client to the new API")
    assert is_long_horizon("run this overnight across the entire codebase")
    assert not is_long_horizon("fix the typo in the README")
    assert not is_long_horizon("add a --verbose flag to the CLI")


def test_long_horizon_floors_tier_at_hard():
    # Lexically this is one short sentence -> trivial without the cue.
    assert estimate_complexity("fix the typo", use_taskgauge=False) == "trivial"
    assert estimate_complexity(
        "keep going until all tests pass", use_taskgauge=False) == "hard"


def test_long_horizon_leaves_open_ended_alone():
    text = "explore whether we can keep going until the build is green"
    assert estimate_complexity(text, use_taskgauge=False) == "open-ended"


# -- tier-scoped priors ------------------------------------------------------

def test_tier_scoped_rule_beats_shape_rule(priors):
    assert priors.favoured_arms("coding:implement") == ("claude", "codex")
    assert priors.favoured_arms("coding:implement", "moderate") == ("claude", "codex")
    assert priors.favoured_arms("coding:implement", "hard") == ("codex", "claude")
    assert priors.favoured_arms("coding:implement", "open-ended") == ("codex", "claude")
    assert priors.favoured_arms("coding:test") == ("codex", "kimi")
    assert priors.favoured_arms("coding:test", "hard") == ("codex", "claude")


def test_explicit_priors_lead_and_fallback(priors):
    assert priors.prior("coding:implement", "hard", "codex") == (8.0, 2.0)
    assert priors.prior("coding:implement", "hard", "claude") == (5.0, 5.0)
    assert priors.prior("coding:implement", "hard", "kimi") == (2.0, 8.0)
    # List rules keep their all-or-nothing semantics.
    assert priors.prior("coding:implement", "moderate", "claude") == (8.0, 2.0)
    assert priors.prior("coding:implement", "moderate", "kimi") == (2.0, 8.0)


def test_hard_coding_cells_seed_codex_favoured_only(router):
    router.select("coding:implement", "hard", ["claude", "codex", "kimi"], record_impression=False)
    post = router.posterior("coding:implement", "hard")
    assert post["codex"] == (8.0, 2.0)
    assert post["claude"] == (5.0, 5.0)
    assert post["kimi"] == (2.0, 8.0)


def test_claude_is_the_fallback_when_codex_is_out_of_quota(router):
    """Codex removed by quota: hard coding lands on claude, not a coin flip."""
    assert router.select("coding:implement", "hard", ["claude", "kimi", "local-agent"]) == "claude"


def test_hard_coding_picks_codex_and_moderate_still_picks_claude(router):
    eligible = ["claude", "codex", "kimi"]
    assert router.select("coding:implement", "hard", eligible) == "codex"
    assert router.select("coding:refactor", "open-ended", eligible) == "codex"
    # Unchanged shape rule: claude and codex tie at (8, 2); pool order wins.
    assert router.select("coding:implement", "moderate", eligible) == "claude"


def test_batch_shapes_do_not_gain_codex(router):
    """The tier override never widens eligibility: shape gates still apply."""
    assert router.select("batch:classify", "hard", ["claude", "local-batch"]) == "local-batch"


def test_user_priors_override_can_drop_the_tier_rule(tmp_path, monkeypatch):
    cfg = Path(os.environ["ROUTE_CONFIG_DIR"])
    cfg.mkdir(parents=True)
    (cfg / "priors.toml").write_text(
        'schema = 1\nfavoured = [8, 2]\nunfavoured = [2, 8]\n[rules]\n'
        '"coding:implement" = ["claude"]\n'
    )
    assert load_priors().favoured_arms("coding:implement", "hard") == ("claude",)


# -- end to end through the CLI ----------------------------------------------

def test_cli_plan_marks_long_horizon_and_routes_to_codex(monkeypatch, capsys):
    monkeypatch.setattr("route.cli.fetch_quota", lambda *a, **k: {})
    monkeypatch.setattr("route.cli._build_router", _mean_router)
    rc = main([
        "--no-llm", "--no-quota", "--plan-only",
        "keep going until every test in the whole repo passes, then refactor the parser modules",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    plan = json.loads(out[: out.rindex("}") + 1])
    assert plan["horizon"] == "long"
    assert plan["tier"] == "hard"
    assert plan["shape"].startswith("coding:")
    assert plan["chosen"] == "codex"
    assert plan["why"] == "prior+long-horizon"


def test_cli_explicit_tier_beats_the_long_horizon_floor(monkeypatch, capsys):
    monkeypatch.setattr("route.cli._build_router", _mean_router)
    rc = main([
        "--no-llm", "--no-quota", "--plan-only", "--tier", "simple",
        "implement the endpoint and keep going until all tests pass",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    plan = json.loads(out[: out.rindex("}") + 1])
    assert plan["tier"] == "simple"
    assert plan["horizon"] == "long"
    assert plan["why"] == "prior"


def _mean_router():
    from route.router import Router
    from tests.conftest import MeanRNG
    return Router(rng=MeanRNG())


def test_cli_plan_reports_quota_status(monkeypatch, capsys):
    monkeypatch.setattr("route.cli._build_router", _mean_router)
    monkeypatch.setattr("route.cli.fetch_quota", lambda *a, **k: {})
    monkeypatch.setattr("route.cli.quotamax_available", lambda *a, **k: False)
    main(["--no-llm", "--plan-only", "add a --verbose flag"])
    out = capsys.readouterr().out
    plan = json.loads(out[: out.rindex("}") + 1])
    assert plan["quota"].startswith("unavailable: quotamax not on PATH")

    monkeypatch.setattr("route.cli.fetch_quota", lambda *a, **k: {"codex": "critical"})
    main(["--no-llm", "--plan-only", "--shape", "coding:implement", "--tier", "hard",
          "implement the endpoint"])
    out = capsys.readouterr().out
    plan = json.loads(out[: out.rindex("}") + 1])
    assert plan["quota"] == "live"
    assert plan["removed_by_quota"] == ["codex"]
    assert plan["chosen"] != "codex"


# -- reseed ------------------------------------------------------------------

def _priors_file(text: str) -> Path:
    cfg = Path(os.environ["ROUTE_CONFIG_DIR"])
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / "priors.toml"
    path.write_text("schema = 1\nfavoured = [8, 2]\nunfavoured = [2, 8]\n[rules]\n" + text)
    return path


def test_reseed_moves_seeds_and_keeps_observations(storage):
    from route.router import REWARD_EVENT, Router, load_priors
    from tests.conftest import MeanRNG

    _priors_file('"coding:implement" = ["claude"]\n')
    old = Router(storage=storage, priors=load_priors(), rng=MeanRNG())
    eligible = ["claude", "codex", "kimi"]
    old.select("coding:implement", "hard", eligible, record_impression=False)
    # rewards do not imply impressions in banditry: 4 tries, 3 accepted.
    for _ in range(4):
        old.record_impression("coding:implement", "hard", "codex")
    for _ in range(3):
        old.reward("coding:implement", "hard", "codex", REWARD_EVENT)
    before = old.observed_counts("coding:implement", "hard")
    assert before == {"codex": (3.0, 1.0)}
    assert old.posterior("coding:implement", "hard")["claude"] == (8.0, 2.0)

    _priors_file('"coding:implement:hard" = { codex = [8, 2], claude = [5, 5] }\n')
    new = Router(storage=storage, priors=load_priors(), rng=MeanRNG())
    dry = new.reseed({"coding:implement": eligible}, apply=False)
    assert {c["arm"] for c in dry} == {"claude", "codex"}
    assert new.posterior("coding:implement", "hard")["claude"] == (8.0, 2.0)  # untouched

    changes = new.reseed({"coding:implement": eligible})
    assert {c["arm"]: (c["from"], c["to"]) for c in changes} == {
        "claude": ("Beta(8,2)", "Beta(5,5)"),
        "codex": ("Beta(2,8)", "Beta(8,2)"),  # the seed moved; 3/4 observed kept below
    }
    post = new.posterior("coding:implement", "hard")
    assert post["claude"] == (5.0, 5.0)
    assert post["codex"] == (11.0, 3.0)
    assert post["kimi"] == (2.0, 8.0)
    assert new.observed_counts("coding:implement", "hard") == before
    assert new.reseed({"coding:implement": eligible}) == []  # idempotent


def test_reseed_seeds_arms_missing_from_a_cell(router):
    router.select("coding:test", "hard", ["kimi"], record_impression=False)
    assert "codex" not in router.posterior("coding:test", "hard")
    changes = router.reseed({"coding:test": ["claude", "codex", "kimi"]})
    assert {c["arm"] for c in changes} == {"claude", "codex"}
    assert router.posterior("coding:test", "hard")["codex"] == (8.0, 2.0)
    assert router.select("coding:test", "hard", ["claude", "codex", "kimi"]) == "codex"


def test_cli_reseed_dry_run_writes_nothing(monkeypatch, capsys):
    monkeypatch.setattr("route.cli._build_router", _mean_router)
    main(["--no-llm", "--no-quota", "--plan-only", "--shape", "coding:implement",
          "--tier", "moderate", "implement the endpoint"])
    capsys.readouterr()
    _priors_file('"coding:implement" = ["kimi"]\n')
    assert main(["reseed", "--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is False and report["changed"] >= 2
    assert main(["reseed", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == report["changed"]
    assert main(["reseed"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is True
    assert main(["reseed"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 0
