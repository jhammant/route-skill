"""The 'deepseek' arm: the paid backstop.

DeepSeek is pay-as-you-go real money, so the rule-table prior never lets it
beat a healthy included pool — but when quota gating has removed claude,
codex and kimi, work must still have somewhere to go: deepseek is chosen
ahead of failing. Deterministic: MeanRNG picks the best posterior mean;
balance probes are scripted fakes — no network, no money.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from route import eligibility
from route.cli import _dispatch, main
from route.eligibility import deepseek_headroom, eligible_for, gate, parse_quota
from route.pools import load_pools
from route.shapes import SHAPES

BATCH = [s for s in SHAPES if s.startswith("batch:")]
CODING = [s for s in SHAPES if s.startswith("coding:")]

#: Every subscription pool exhausted; the backstop's own balance still OK.
ALL_CRITICAL = {"claude": "critical", "codex": "critical", "kimi": "critical", "deepseek": "ok"}


# -- registration ------------------------------------------------------------


def test_deepseek_is_a_paid_remote_arm_between_kimi_and_codex(pools):
    ds = pools["deepseek"]
    assert ds.cost == "paid"
    assert ds.remote
    assert pools["kimi"].strength < ds.strength < pools["codex"].strength
    assert ds.model.ident() == "deepseek@v4-pro+flash"


def test_deepseek_takes_coding_and_batch_shapes_only(pools):
    for shape in CODING + BATCH:
        assert "deepseek" in eligible_for(shape, pools), shape
    for shape in ("research", "writing", "orchestration"):
        assert "deepseek" not in eligible_for(shape, pools), shape


def test_deepseek_dispatch_splits_by_shape_family(pools):
    ds = pools["deepseek"]
    template, on_stdin = ds.dispatch_for("coding:refactor")
    assert template.endswith("/scripts/deepseek code --dir . --model deepseek-v4-pro {task}")
    assert not on_stdin
    template, on_stdin = ds.dispatch_for("batch:classify")
    assert template.endswith("/scripts/deepseek ask - --model deepseek-flash")
    assert on_stdin and "{task}" not in template


def test_batch_dispatch_sends_the_task_on_stdin(monkeypatch, pools):
    calls = []
    monkeypatch.setattr(
        "route.cli.subprocess.run",
        lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(returncode=0),
    )
    template, on_stdin = pools["deepseek"].dispatch_for("batch:summarize")
    assert _dispatch(template, "summarise these 400 tickets", stdin=on_stdin) == 0
    argv, kw = calls[0]
    assert argv[1:] == ["ask", "-", "--model", "deepseek-flash"]
    assert kw["input"] == "summarise these 400 tickets"  # stdin, never argv
    assert not any("summarise" in tok for tok in argv)


# -- sensitive and private vetoes ---------------------------------------------


def test_sensitive_vetoes_deepseek_like_any_remote_third_party(pools):
    result = gate("rewrite the billing module", "coding:implement", pools, sensitive=True)
    assert "deepseek" not in result.eligible
    assert "deepseek" in result.removed_by_sensitivity


def test_private_data_never_routes_to_deepseek(pools):
    result = gate("implement a handler", "coding:implement", pools, private=True)
    assert result.veto is not None and result.veto.name == "private-data"
    assert result.eligible == ["claude"]


# -- quota gating --------------------------------------------------------------


def test_backstop_field_gates_eligibility():
    healthy = {"backstop": {"provider": "deepseek", "available": True, "balanceUsd": 12.50, "low": False}}
    assert deepseek_headroom(healthy) == "ok"
    low = {"backstop": {"provider": "deepseek", "available": True, "balanceUsd": 0.40, "low": True}}
    assert deepseek_headroom(low) == "critical"
    gone = {"backstop": {"provider": "deepseek", "available": False, "balanceUsd": 0.0, "low": True}}
    assert deepseek_headroom(gone) == "critical"


def test_balance_fallback_when_quotamax_has_no_backstop_field():
    ok = lambda *a, **k: SimpleNamespace(returncode=0)  # noqa: E731
    assert deepseek_headroom({"codex": "ok"}, runner=ok) == "ok"
    below_floor = lambda *a, **k: SimpleNamespace(returncode=3)  # noqa: E731
    assert deepseek_headroom({}, runner=below_floor) == "critical"
    errored = lambda *a, **k: SimpleNamespace(returncode=1)  # noqa: E731
    assert deepseek_headroom(None, runner=errored) == "critical"


def test_balance_fallback_fail_closed_when_the_skill_is_missing():
    def missing(*a, **k):
        raise OSError("no such file: deepseek")

    assert deepseek_headroom({}, runner=missing) == "critical"


def test_critical_backstop_removes_deepseek(pools):
    result = gate("implement a handler", "coding:implement", pools, quota={"deepseek": "critical"})
    assert "deepseek" not in result.eligible
    assert "deepseek" in result.removed_by_quota


def test_no_quota_data_leaves_deepseek_eligible(pools):
    """--no-quota passes an empty dict: gating is skipped entirely."""
    result = gate("implement a handler", "coding:implement", pools, quota={})
    assert "deepseek" in result.eligible


def test_parse_quota_ignores_the_backstop_block():
    assert parse_quota({"codex": "ok", "backstop": {"available": True, "low": False}}) == {"codex": "ok"}


def test_fetch_quota_folds_the_backstop_into_headroom(monkeypatch):
    agent = {"pools": {"codex": "ok"},
             "backstop": {"provider": "deepseek", "available": True, "balanceUsd": 9.0, "low": False}}
    monkeypatch.setattr(
        eligibility, "_run_json",
        lambda cmd, timeout: agent if cmd[1] == "agent" else [],
    )
    assert eligibility.fetch_quota() == {"codex": "ok", "deepseek": "ok"}


def test_fetch_quota_uses_the_balance_fallback_without_a_backstop_field(monkeypatch):
    monkeypatch.setattr(
        eligibility, "_run_json",
        lambda cmd, timeout: {"pools": {"codex": "low"}} if cmd[1] == "agent" else [],
    )
    monkeypatch.setattr(eligibility, "deepseek_headroom", lambda payload, **k: "critical")
    assert eligibility.fetch_quota() == {"codex": "low", "deepseek": "critical"}


# -- the backstop rule ----------------------------------------------------------


def test_all_healthy_means_deepseek_is_eligible_but_never_chosen(router, pools):
    result = gate("refactor the parser into smaller functions", "coding:refactor", pools,
                  quota={"claude": "ok", "codex": "ok", "kimi": "ok", "deepseek": "ok"})
    assert "deepseek" in result.eligible
    chosen = router.select("coding:refactor", "moderate", result.eligible)
    assert chosen in ("claude", "codex")  # the favoured, already-paid-for pools
    assert chosen != "deepseek"


def test_backstop_chosen_when_quota_removes_every_other_remote_pool(router, pools):
    """claude critical, codex removed, kimi removed -> deepseek, not failure."""
    result = gate("refactor the parser into smaller functions", "coding:refactor", pools,
                  quota=ALL_CRITICAL)
    assert sorted(result.removed_by_quota) == ["claude", "codex", "kimi"]
    assert result.eligible == ["deepseek", "local-agent"]  # registry order
    assert router.select("coding:refactor", "moderate", result.eligible) == "deepseek"


def test_backstop_also_covers_long_horizon_cells(router, pools):
    """hard/open-ended cells: codex leads, claude falls back, deepseek last."""
    result = gate("refactor the parser into smaller functions", "coding:refactor", pools,
                  quota=ALL_CRITICAL)
    assert router.select("coding:refactor", "hard", result.eligible) == "deepseek"
    # With codex out but claude healthy, the included fallback still wins.
    result = gate("refactor the parser", "coding:refactor", pools,
                  quota={"codex": "critical", "deepseek": "ok"})
    assert router.select("coding:refactor", "hard", result.eligible) == "claude"


def test_cli_plan_lands_on_the_backstop(monkeypatch, capsys):
    from route.router import Router
    from tests.conftest import MeanRNG

    monkeypatch.setattr("route.cli._build_router", lambda: Router(rng=MeanRNG()))
    monkeypatch.setattr("route.cli.fetch_quota", lambda *a, **k: dict(ALL_CRITICAL))
    rc = main(["--no-llm", "--plan-only", "--shape", "coding:refactor", "--tier", "moderate",
               "refactor the parser into smaller functions"])
    assert rc == 0
    out = capsys.readouterr().out
    plan = json.loads(out[: out.rindex("}") + 1])
    assert plan["chosen"] == "deepseek"
    assert sorted(plan["removed_by_quota"]) == ["claude", "codex", "kimi"]
