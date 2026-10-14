"""Idle quota adds bounded preference only inside the eligible candidate set."""

import json
from datetime import datetime, timezone

import pytest

from route.cli import main
from route.eligibility import QuotaSnapshot, fetch_quota
from route.router import Router
from route.storage import config_dir
from route.surplus import load_surplus_k, parse_weekly, surplus_bonus
from tests.conftest import MeanRNG


@pytest.mark.parametrize("used,days,k,expected", [
    (7, 6.2, 0.15, 0.0290625),  # 38.75% projected unused, halved in the first day
    (7, 6, 0.15, 0.0765),       # exactly one day: full projected surplus
    (3.5, 6.5, 0.15, 0.03825),
    (0, 6.9, 0.15, 0.075),
    (20, 6.2, 0.15, 0.0),
    (20, 3.5, 0.15, 0.09),
    (0, 3.5, 0.15, 0.15),
    (50, 3.5, 0.15, 0.0),
    (70, 3.5, 0.15, 0.0),
    (0, 2, 0.15, 0.15),
    (0, 1.99, 0.15, 0.0),
    (0, 7, 0.15, 0.0),
    (0, 8, 0.15, 0.0),
    (0, 0, 0.15, 0.0),
    (0, -1, 0.15, 0.0),
    (0, 3.5, 99, 0.15),
    (0, 3.5, 0.05, 0.05),
    (0, 3.5, 0, 0.0),
    (0, 3.5, -1, 0.0),
])
def test_projected_unused_weekly_bonus(used, days, k, expected):
    # Arrange / Act
    bonus = surplus_bonus(used, days * 24 * 60, k)

    # Assert
    assert bonus == pytest.approx(expected)
    assert 0 <= bonus <= 0.15


@pytest.mark.parametrize("days,expected", [(6.000001, 0.0375), (6.0, 0.075), (5.999999, 0.075)])
def test_first_day_damping_ends_at_exactly_one_elapsed_day(days, expected):
    # Arrange: a steady pace projected to consume half the weekly quota.
    used = 50 * (7 - days) / 7

    # Act
    bonus = surplus_bonus(used, days * 24 * 60)

    # Assert
    assert bonus == pytest.approx(expected)


@pytest.mark.parametrize("used,minutes,k", [
    (None, 3000, 0.15), (0, None, 0.15), ("7", 3000, 0.15),
    (float("nan"), 3000, 0.15), (0, float("inf"), 0.15),
    (0, 3000, float("nan")), (-1, 3000, 0.15), (101, 3000, 0.15),
    (False, 3000, 0.15),
])
def test_bad_quota_data_earns_no_bonus(used, minutes, k):
    # Arrange / Act / Assert
    assert surplus_bonus(used, minutes, k) == 0


@pytest.mark.parametrize("configured,expected", [(None, 0.15), (0.05, 0.05), (0, 0), (1, 0.15), (-1, 0)])
def test_bonus_coefficient_config(configured, expected):
    # Arrange
    if configured is not None:
        config_dir().mkdir(parents=True)
        (config_dir() / "config.toml").write_text(f"[surplus]\nk = {configured}\n")

    # Act / Assert
    assert load_surplus_k() == expected


def test_weekly_windows_are_parsed_from_both_existing_quota_views():
    # Arrange
    agent = {"ok": True, "weekly": {"percentUsed": 20, "resetsInMinutes": 4320}}
    providers = [
        {"id": "codex", "ok": True, "limits": [
            {"label": "session", "percent": 99},
            {"label": "weekly", "percent": 7, "resetsInMinutes": 8640}]},
        {"id": "kimi", "ok": True, "limits": [
            {"label": "7 days", "percent": 10, "resetsAt": "2026-10-20T00:00:00Z"}]},
        {"id": "broken", "ok": False, "weekly": {"percentUsed": 0}},
    ]

    # Act
    windows = parse_weekly(agent, providers, now=datetime(2026, 10, 14, tzinfo=timezone.utc))

    # Assert
    assert windows == {"claude": {"percentUsed": 20, "resetsInMinutes": 4320},
                       "codex": {"percentUsed": 7, "resetsInMinutes": 8640},
                       "kimi": {"percentUsed": 10, "resetsInMinutes": 8640}}
    assert parse_weekly(None, None) == {}


def test_fetch_quota_preserves_weekly_windows_without_extra_probes(monkeypatch):
    # Arrange
    calls = []

    def payload(cmd, timeout):
        calls.append(cmd)
        if cmd[1] == "agent":
            return {"ok": True, "headroom": "low", "weekly": {"percentUsed": 80, "resetsInMinutes": 4000},
                    "backstop": {"available": False}}
        return [{"id": "codex", "ok": True, "limits": [
            {"label": "weekly", "percent": 7, "resetsInMinutes": 8640}]}]

    monkeypatch.setattr("route.eligibility._run_json", payload)

    # Act
    quota = fetch_quota()

    # Assert
    assert len(calls) == 2
    assert quota == {"claude": "low", "codex": "ok", "deepseek": "critical"}
    assert quota.weekly["codex"] == {"percentUsed": 7, "resetsInMinutes": 8640}


def snapshot(**headroom):
    return QuotaSnapshot(headroom, {arm: {"percentUsed": 0, "resetsInMinutes": 3.5 * 24 * 60}
                                    for arm in ("claude", "codex", "kimi")})


def plan_with_quota(monkeypatch, capsys, quota, flags=()):
    monkeypatch.setattr("route.cli._build_router", lambda: Router(rng=MeanRNG()))
    monkeypatch.setattr("route.cli.fetch_quota", lambda: quota)
    assert main(["--host", "claude", "--no-llm", "--plan-only", "--shape", "coding:implement",
                 "--tier", "moderate", *flags, "build parser endpoint"]) == 0
    return json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]


def test_surplus_breaks_a_quality_tie_and_explains_the_choice(monkeypatch, capsys):
    # Arrange
    quota = QuotaSnapshot({"codex": "ok"}, {"codex": {"percentUsed": 7, "resetsInMinutes": 8640}})

    # Act
    plan = plan_with_quota(monkeypatch, capsys, quota)

    # Assert
    assert plan["chosen"] == "codex"
    assert plan["why"] == "prior+surplus:codex"
    assert plan["surplus_bonus"]["codex"] == pytest.approx(0.0765)
    assert Router().observed_counts("coding:implement", "moderate") == {}


@pytest.mark.parametrize("flags", [["--needs-context"], ["--cross-repo"], ["--private"],
                                   ["--no-acceptance"], ["--pool", "kimi"]])
def test_surplus_never_overrides_a_veto(monkeypatch, capsys, flags):
    # Arrange / Act
    plan = plan_with_quota(monkeypatch, capsys, snapshot(), flags)

    # Assert
    assert plan["chosen"] == ("kimi" if "--pool" in flags else "claude")
    assert plan["why"].startswith("veto:")
    assert not any(plan["surplus_bonus"].values())


def test_quota_removed_arm_cannot_receive_surplus_bonus(monkeypatch, capsys):
    # Arrange / Act
    plan = plan_with_quota(monkeypatch, capsys, snapshot(codex="critical"))

    # Assert
    assert "codex" in plan["removed_by_quota"]
    assert "codex" not in plan["surplus_bonus"]
    assert plan["chosen"] != "codex"


def test_sensitive_local_preference_survives_remote_surplus(monkeypatch, capsys):
    # Arrange / Act
    plan = plan_with_quota(monkeypatch, capsys, snapshot(), ["--sensitive"])

    # Assert
    assert plan["chosen"] == "local-agent"
    assert "codex" not in plan["surplus_bonus"]
    assert not any(plan["surplus_bonus"].values())


def test_no_quota_disables_bonus_and_probe(monkeypatch, capsys):
    # Arrange
    monkeypatch.setattr("route.cli.fetch_quota", lambda: pytest.fail("quota disabled"))

    # Act
    assert main(["--host", "claude", "--no-llm", "--no-quota", "--plan-only",
                 "--shape", "coding:implement", "--tier", "moderate", "build parser endpoint"]) == 0
    plan = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]

    # Assert
    assert not any(plan["surplus_bonus"].values())


def test_router_bonus_applies_to_draw_not_posterior_and_cannot_widen_eligibility(router):
    # Arrange
    shape, tier = "coding:implement", "moderate"
    eligible = ["claude", "codex"]
    router.select(shape, tier, eligible, record_impression=False)
    before = router.posterior(shape, tier)

    # Act
    choice = router.select(shape, tier, eligible, record_impression=False,
                           surplus_bonus={"codex": 0.1, "kimi": 1000})

    # Assert
    assert choice == "codex"
    assert router.posterior(shape, tier) == before
