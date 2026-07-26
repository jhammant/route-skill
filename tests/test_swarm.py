"""SPEC 'Swarms': dispatch-time fan-out, statistically honest recording."""

from __future__ import annotations

import pytest

from route.swarm import plan_swarm, record_swarm_outcome


def test_plan_swarm_is_min_of_units_limit_and_headroom_cap():
    assert plan_swarm(12, 30, "abundant") == 12    # units bind
    assert plan_swarm(40, 30, "abundant") == 30    # pool limit binds
    assert plan_swarm(12, 30, "comfortable") == 8  # headroom cap binds
    assert plan_swarm(5, 2, "abundant") == 2       # small pool limit binds


def test_plan_swarm_no_swarm_when_constrained_or_single_unit():
    assert plan_swarm(12, 30, "constrained") == 1  # no swarm
    assert plan_swarm(1, 30, "abundant") == 1      # one unit never fans out
    assert plan_swarm(12, 30, "critical") == 0     # arm already ineligible
    assert plan_swarm(12, 30, "unknown-word") == 1  # conservative default


def test_pool_registry_exposes_server_authoritative_parallel_limit(pools):
    assert pools["kimi"].parallel_limit == 30  # /usages parallel.limit, ADVANCED
    assert pools["codex"].parallel_limit < pools["kimi"].parallel_limit
    assert "kimi-swarm" not in pools  # a swarm is NOT a separate arm


def test_n_unit_swarm_advances_alpha_beta_by_one_not_n(router):
    """CRITICAL: 12 correlated results are ONE weighted observation."""
    shape, tier = "coding:test", "moderate"
    router.select(shape, tier, ["claude", "kimi"], record_impression=False)
    assert router.observed_counts(shape, tier) == {}

    fraction = record_swarm_outcome(router, shape, tier, "kimi",
                                    units_total=12, units_accepted=9)
    assert fraction == pytest.approx(0.75)

    alpha, beta = router.observed_counts(shape, tier)["kimi"]
    assert alpha == pytest.approx(0.75)       # fraction of units accepted
    assert alpha + beta == pytest.approx(1.0)  # ONE trial — not 12


def test_swarm_per_unit_detail_goes_to_stats_only(router, tmp_path):
    from route.stats import Stats

    stats = Stats(directory=tmp_path)
    record_swarm_outcome(router, "coding:test", "moderate", "kimi",
                         units_total=4, units_accepted=3, stats=stats)
    (row,) = stats.decisions()
    assert row["units"] == [True, True, True, False]
    alpha, beta = router.observed_counts("coding:test", "moderate")["kimi"]
    assert alpha + beta == pytest.approx(1.0)


def test_swarm_rejects_nonsense_counts(router):
    with pytest.raises(ValueError):
        record_swarm_outcome(router, "coding:test", "moderate", "kimi", 0, 0)
    with pytest.raises(ValueError):
        record_swarm_outcome(router, "coding:test", "moderate", "kimi", 4, 5)
