"""Paid arms cannot enter the draw while non-metered capacity survives."""

import random
from dataclasses import replace

import pytest

from route.eligibility import gate
from route.router import Router


def test_subscription_arms_are_included_not_metered(pools):
    # Arrange / Act / Assert
    assert {pools[arm].cost for arm in ("claude", "codex", "kimi")} == {"included"}
    assert pools["deepseek"].cost == "paid"


@pytest.mark.parametrize("healthy", ["claude", "codex", "both"])
def test_real_thompson_rng_never_chooses_backstop_in_1000_draws(pools, healthy):
    # Arrange: no local arm is needed to protect the subscription survivor.
    registry = {name: pools[name] for name in ("claude", "codex", "deepseek")}
    quota = {name: "ok" if name == healthy or healthy == "both" else "critical"
             for name in ("claude", "codex")}
    quota["deepseek"] = "ok"
    result = gate("build an endpoint", "coding:implement", registry, quota=quota)
    router = Router(rng=random.Random(20261014))  # genuine Beta draws, reproducible seed

    # Act: even a bonus and perfect neighbours cannot resurrect a removed arm.
    choices = [router.select("coding:implement", "moderate", result.eligible,
                             record_impression=False,
                             quota_adjustment={"deepseek": 0.15, "claude": -0.15, "codex": -0.15},
                             neighbours=[{"arm": "deepseek", "score": 1.0, "similarity": 1.0}] * 5)
               for _ in range(1000)]

    # Assert
    assert result.removed_by_cost == ["deepseek"]
    assert set(choices) <= {"claude", "codex"}
    assert "deepseek" not in choices
    assert router.observed_counts("coding:implement", "moderate") == {}


@pytest.mark.parametrize("cost", ["included", "free", "local"])
def test_rule_applies_to_configured_paid_arms_and_all_nonpaid_classes(pools, cost):
    # Arrange
    registry = {"claude": replace(pools["claude"], cost=cost),
                "metered-custom": replace(pools["deepseek"], name="metered-custom")}

    # Act
    result = gate("build an endpoint", "coding:implement", registry)

    # Assert
    assert result.eligible == ["claude"]
    assert result.removed_by_cost == ["metered-custom"]


def test_backstop_becomes_eligible_only_after_nonpaid_arms_are_gated_out(pools, monkeypatch):
    # Arrange
    monkeypatch.setattr("route.eligibility.subcommand_available", lambda spec: False)
    quota = {"claude": "critical", "codex": "critical", "kimi": "critical", "deepseek": "ok"}

    # Act
    result = gate("build an endpoint", "coding:implement", pools, quota=quota)

    # Assert
    assert result.eligible == ["deepseek"]
    assert result.removed_by_cost == []


@pytest.mark.parametrize("proxy_up,expected", [(True, ["free"]), (False, ["deepseek"])])
def test_unhealthy_free_pool_does_not_block_paid_fallback(pools, monkeypatch, proxy_up, expected):
    # Arrange
    registry = {name: pools[name] for name in ("claude", "deepseek", "free")}
    monkeypatch.setattr("route.eligibility.probe_health", lambda url: proxy_up)

    # Act
    result = gate("write parser tests", "coding:test", registry, quota={"claude": "critical"})

    # Assert
    assert result.eligible == expected


def test_local_pool_for_another_shape_does_not_block_paid_arm(pools):
    # Arrange
    registry = {name: pools[name] for name in ("claude", "deepseek", "local-batch")}

    # Act
    result = gate("build an endpoint", "coding:implement", registry, quota={"claude": "critical"})

    # Assert
    assert result.eligible == ["deepseek"]


def test_explicit_paid_pin_remains_authoritative(pools):
    # Arrange / Act
    result = gate("build an endpoint", "coding:implement", pools, pool_pin="deepseek")

    # Assert: the veto leaves only the pinned arm before cost filtering.
    assert result.eligible == ["deepseek"] and result.veto.name == "user-pin"
