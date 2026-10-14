"""Feature-delivery floors apply before selecting a shape:tier cell."""

import json

import pytest

from route.cli import main
from route.complexity import estimate_complexity
from route.router import Router
from route.shapes import classify_shape
from tests.conftest import MeanRNG

SMS_TASK = "build the trial SMS notices feature with a signed redirect route"


@pytest.mark.parametrize("task,expected", [
    (SMS_TASK, "moderate"),
    ("build the SMS feature", "moderate"),
    ("build the SMS feature with a route and endpoint", "moderate"),
    ("build the SMS feature with a route, endpoint and worker", "hard"),
    ("build the SMS feature with a migration, webhook and tests", "hard"),
    ("build the SMS feature with routes, endpoints and flags", "hard"),
    ("BUILD the SMS feature with ROUTES, WORKERS and TESTS", "hard"),
    ("build the SMS feature with a route, routes and ROUTE", "moderate"),
    ("build the SMS feature with route, routes, worker and workers", "moderate"),
    ("build the SMS feature until all tests pass", "hard"),
    ("explore whether to build the SMS feature with a route, worker and tests", "open-ended"),
    ("fix the typo in the parser", "trivial"),
    ("review the route, endpoint and worker", "trivial"),
])
def test_feature_and_component_floors_with_lexical_estimator(task, expected):
    # Arrange / Act
    tier = estimate_complexity(task, use_taskgauge=False)

    # Assert
    assert tier == expected


@pytest.mark.parametrize("estimate,expected", [
    ("trivial", "moderate"), ("simple", "moderate"), ("moderate", "moderate"),
    ("hard", "hard"), ("open-ended", "open-ended"),
])
def test_feature_floor_applies_to_taskgauge_without_downgrading(monkeypatch, estimate, expected):
    # Arrange
    monkeypatch.setattr("route.complexity._estimate_taskgauge", lambda text: estimate)

    # Act
    tier = estimate_complexity(SMS_TASK)

    # Assert
    assert tier == expected


def test_component_floor_also_applies_to_taskgauge(monkeypatch):
    # Arrange
    monkeypatch.setattr("route.complexity._estimate_taskgauge", lambda text: "trivial")

    # Act
    tier = estimate_complexity("build the SMS feature with a route, worker and tests")

    # Assert
    assert tier == "hard"


def test_cli_explicit_tier_still_overrides_feature_floor(capsys):
    # Arrange / Act
    assert main(["--host", "claude", "--no-quota", "--no-llm", "--plan-only",
                 "--tier", "simple", SMS_TASK]) == 0
    plan = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]

    # Assert
    assert plan["shape"] == "coding:implement"
    assert plan["tier"] == "simple"


def test_exact_sms_plan_with_mocked_quota_and_fresh_state(monkeypatch, capsys):
    # Arrange: use the real quota parsing/gating path and deterministic Beta
    # means. Claude reset timing is unspecified, so it cannot earn surplus.
    agent = {"ok": True, "headroom": "comfortable",
             "session": {"percentUsed": 62}, "weekly": {"percentUsed": 28},
             "backstop": {"available": False}}
    providers = [{"id": "codex", "ok": True, "limits": [
        {"label": "weekly", "percent": 7, "resetsInMinutes": 6.2 * 24 * 60}]}]
    monkeypatch.setattr("route.eligibility._run_json",
                        lambda command, timeout: agent if command[1] == "agent" else providers)
    monkeypatch.setattr("route.complexity._estimate_taskgauge", lambda text: "trivial")
    monkeypatch.setattr("route.cli._build_router", lambda: Router(rng=MeanRNG()))

    # Act
    assert main(["--host", "claude", "--no-llm", "--plan-only", SMS_TASK]) == 0
    plan = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]

    # Assert
    assert classify_shape(SMS_TASK) == plan["shape"] == "coding:implement"
    assert plan["tier"] == "moderate"
    assert plan["cell"] == "route:coding:implement:moderate"
    assert plan["chosen"] == "codex"
    assert plan["surplus_bonus"]["codex"] == pytest.approx(0.0290625)
    assert plan["surplus_bonus"]["claude"] == 0
    assert plan["why"] == "prior+surplus:codex"
    assert plan["quota"] == "live" and plan["neighbours"] == []
    assert plan["decision_id"]
