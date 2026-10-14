"""Signed quota pressure is a preference, never an availability gate."""

import json

import pytest

from route.cli import main
from route.eligibility import QuotaSnapshot
from route.router import Router
from route.surplus import quota_adjustment
from tests.conftest import MeanRNG


@pytest.mark.parametrize("used,remaining,headroom,k,expected", [
    (28, 6.3, "constrained", 0.15, -0.075),
    (28, 6.3, "ok", 0.15, -0.075),
    (7, 6.2, "ok", 0.15, 0.0290625),
    (60, 3.5, "ok", 0.15, -0.03),
    (60, 3.5, "constrained", 0.15, -0.075),
    (100, 3.5, "ok", 0.15, -0.15),
    (99, 6.93, "ok", 0.15, -0.147),  # first-day pace delta exceeds half projection
    (0, 3.5, "constrained", 0.15, -0.075),  # session pressure beats spare weekly quota
    (28, 6.3, "constrained", 0.05, -0.025),
    (28, 6.3, "constrained", 10, -0.075),
    (28, 6.3, "constrained", 0, 0),
    (28, 6.3, "constrained", -1, 0),
    (90, 1, "ok", 0.15, -0.0075),  # pressure still applies near reset
    (0, 1, "ok", 0.15, 0),       # surplus still needs >=2 days remaining
    (0, 7, "ok", 0.15, 0),
    (0, 7, "constrained", 0.15, -0.075),
    (90, 0, "ok", 0.15, 0),
])
def test_signed_adjustment(used, remaining, headroom, k, expected):
    # Arrange / Act
    adjustment = quota_adjustment(used, remaining * 24 * 60, k, headroom=headroom)

    # Assert
    assert adjustment == pytest.approx(expected)
    assert -0.15 <= adjustment <= 0.15


@pytest.mark.parametrize("remaining,expected", [(6.000001, -0.0375), (6, -0.075), (5.999999, -0.075)])
def test_pressure_damping_stops_at_one_elapsed_day(remaining, expected):
    # Arrange: projected end usage 150% regardless of elapsed time.
    used = 150 * (7 - remaining) / 7

    # Act / Assert
    assert quota_adjustment(used, remaining * 24 * 60) == pytest.approx(expected)


@pytest.mark.parametrize("used,minutes", [(None, None), (float("nan"), 9000),
                                        (28, float("inf")), (-1, 9000), (28, -1)])
def test_bad_weekly_data_only_allows_explicit_headroom_pressure(used, minutes):
    # Arrange / Act / Assert
    assert quota_adjustment(used, minutes) == 0
    assert quota_adjustment(used, minutes, headroom="constrained") == -0.075


@pytest.mark.parametrize("k", [float("nan"), float("inf"), True])
def test_invalid_coefficient_cannot_produce_pressure(k):
    # Arrange / Act / Assert
    assert quota_adjustment(28, 9000, k, headroom="constrained") == 0


def test_pressure_changes_draw_without_mutating_posterior_or_excluding_arm(router):
    # Arrange
    shape, tier, eligible = "coding:implement", "moderate", ["claude", "codex"]
    router.select(shape, tier, eligible, record_impression=False)
    before = router.posterior(shape, tier)

    # Act
    chosen = router.select(shape, tier, eligible, record_impression=False,
                           quota_adjustment={"claude": -0.075})
    only = router.select(shape, tier, ["claude"], record_impression=False,
                         quota_adjustment={"claude": -0.15})

    # Assert
    assert chosen == "codex" and only == "claude"
    assert router.posterior(shape, tier) == before


def test_router_clamps_negative_adjustments_and_supports_deprecated_keyword(router):
    # Arrange: Claude's prior mean exceeds Kimi's by 0.6; a bounded penalty
    # cannot reverse this. An unbounded -100 would incorrectly pick Kimi.
    kwargs = dict(shape="coding:implement", tier="moderate", eligible=["claude", "kimi"],
                  record_impression=False)

    # Act / Assert
    assert router.select(**kwargs, quota_adjustment={"claude": -100}) == "claude"
    assert router.select(**kwargs, surplus_bonus={"claude": -100}) == "claude"


def test_cli_explains_pressure_and_keeps_signed_deprecated_alias(monkeypatch, capsys):
    # Arrange: constrained Claude is the only included survivor.
    quota = QuotaSnapshot({"claude": "constrained", "codex": "critical", "kimi": "critical", "deepseek": "ok"},
                          {"claude": {"percentUsed": 28, "resetsInMinutes": 6.3 * 24 * 60}})
    monkeypatch.setattr("route.cli.fetch_quota", lambda: quota)
    monkeypatch.setattr("route.cli._build_router", lambda: Router(rng=MeanRNG()))
    monkeypatch.setattr("route.eligibility.subcommand_available", lambda spec: False)

    # Act
    assert main(["--host", "claude", "--plan-only", "--no-llm", "--tier", "moderate",
                 "build the SMS notices feature with a route"]) == 0
    plan = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]

    # Assert
    assert plan["eligible"] == ["claude"] and plan["chosen"] == "claude"
    assert plan["quota_adjustment"] == plan["surplus_bonus"] == {"claude": -0.075}
    assert plan["why"] == "prior-pressure:claude"
    assert plan["removed_by_cost"] == ["deepseek"]


def test_exact_sms_plan_balances_pressure_and_surplus(monkeypatch, capsys):
    # Arrange: exercise the normal quotamax parsing and gating paths.
    agent = {"ok": True, "headroom": "constrained",
             "weekly": {"percentUsed": 28, "resetsInMinutes": 6.3 * 24 * 60},
             "backstop": {"available": True, "low": False}}
    providers = [{"id": "codex", "ok": True, "limits": [
        {"label": "weekly", "percent": 7, "resetsInMinutes": 6.2 * 24 * 60}]}]
    monkeypatch.setattr("route.eligibility._run_json",
                        lambda command, timeout: agent if command[1] == "agent" else providers)
    monkeypatch.setattr("route.complexity._estimate_taskgauge", lambda text: "trivial")
    monkeypatch.setattr("route.cli._build_router", lambda: Router(rng=MeanRNG()))

    # Act
    assert main(["--host", "claude", "--plan-only", "--no-llm",
                 "build the trial SMS notices feature with a signed redirect route"]) == 0
    plan = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]

    # Assert
    assert plan["shape"] == "coding:implement" and plan["tier"] == "moderate"
    assert plan["chosen"] == "codex" and plan["why"] == "prior+surplus:codex"
    assert plan["quota_adjustment"] == plan["surplus_bonus"]
    assert plan["quota_adjustment"]["claude"] == pytest.approx(-0.075)
    assert plan["quota_adjustment"]["codex"] == pytest.approx(0.0290625)
    assert plan["removed_by_cost"] == ["deepseek"]
    assert "deepseek" not in plan["eligible"] and "deepseek" not in plan["quota_adjustment"]
