"""The quotamax probe is cached so a slow rescan can't strip quota awareness."""

from __future__ import annotations

import json

import pytest

from route import eligibility
from route.cli import main
from route.router import Router
from tests.conftest import MeanRNG


def test_successful_quota_probe_is_cached(monkeypatch):
    # Arrange
    calls = []

    def fake_run_json(cmd, timeout):
        calls.append(cmd)
        return {"headroom": "constrained"} if cmd[1] == "agent" else {}

    monkeypatch.setattr(eligibility, "_run_json", fake_run_json)

    # Act
    eligibility.fetch_quota()
    eligibility.fetch_quota()

    # Assert: one probe pair, then served from the cache.
    assert len(calls) == 2


def test_failed_probe_is_not_cached(monkeypatch):
    # Arrange
    calls = []

    def fake_run_json(cmd, timeout):
        calls.append(cmd)
        return None

    monkeypatch.setattr(eligibility, "_run_json", fake_run_json)

    # Act
    eligibility.fetch_quota()
    eligibility.fetch_quota()

    # Assert: a timeout is retried next time rather than cached as "no data".
    assert len(calls) == 4


def test_cached_codex_host_plan_preserves_pressure_and_refreshes_quota_gates(monkeypatch, capsys):
    # Arrange: a fresh snapshot favours idle Codex; at cache expiry it is critical.
    clock = [1000.0]
    calls = []

    def fake_run_json(cmd, timeout):
        calls.append((cmd, timeout))
        if cmd[1] == "agent":
            return {"ok": True, "headroom": "constrained",
                    "weekly": {"percentUsed": 28, "resetsInMinutes": 6.3 * 1440},
                    "backstop": {"available": True, "low": False}}
        return [{"id": "codex", "ok": True, "limits": [
            {"label": "weekly", "percent": 7 if clock[0] < 1300 else 96,
             "resetsInMinutes": 6.2 * 1440}]}]

    monkeypatch.setattr(eligibility.time, "time", lambda: clock[0])
    monkeypatch.setattr(eligibility, "_run_json", fake_run_json)
    monkeypatch.setattr("route.cli._build_router", lambda: Router(rng=MeanRNG()))
    monkeypatch.setattr("route.complexity._estimate_taskgauge", lambda text: "trivial")
    args = ["--host", "codex", "--plan-only", "--no-llm",
            "build the trial SMS notices feature with a signed redirect route"]

    # Act: reuse the payloads just before five minutes, then refresh at expiry.
    plans = []
    for now in (1000.0, 1299.0, 1300.0):
        clock[0] = now
        assert main(args) == 0
        plans.append(json.JSONDecoder().raw_decode(capsys.readouterr().out)[0])
        assert len(calls) == (2 if now < 1300 else 4)

    # Assert: cached weekly data drives signed adjustments without reviving paid arms.
    assert all(timeout == 15.0 for _, timeout in calls)
    for plan in plans:
        assert plan["host"] == "codex"
        assert plan["shape"] == "coding:implement" and plan["tier"] == "moderate"
        assert plan["shape_confidence"] == "high"
        assert plan["removed_by_cost"] == ["deepseek"]
        assert plan["quota_adjustment"] == plan["surplus_bonus"]
        assert plan["quota_adjustment"]["claude"] == pytest.approx(-0.075)
    for plan in plans[:2]:
        assert plan["chosen"] == "codex" and plan["why"] == "prior+surplus:codex"
        assert plan["quota_adjustment"]["codex"] == pytest.approx(0.0290625)
    assert plans[2]["removed_by_quota"] == ["codex"]
    assert "codex" not in plans[2]["quota_adjustment"]
    assert plans[2]["chosen"] == "claude" and plans[2]["why"] == "prior-pressure:claude"
