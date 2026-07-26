"""Swarms — fan-out when the pool has headroom.

A swarm is a property of the DISPATCH, not a separate arm. There is no
``kimi-swarm`` arm: that would split the bandit's evidence about Kimi's
*quality* across two arms that share one underlying model, and neither would
learn properly. The bandit picks ``kimi``; this concurrency planner then
decides *how many*:

    n = min(units, pool.parallel_limit, headroom_cap)

- ``units`` is the decomposability gate, and the one that actually matters:
  a swarm only helps when the task genuinely splits into independent units,
  each with its own acceptance check. units == 1 means no swarm, regardless
  of headroom.
- ``headroom_cap`` scales to quota, because a swarm multiplies burn by n:
  abundant -> pool limit, comfortable -> 8, constrained -> 1 (no swarm),
  critical -> the arm is already ineligible, so 0.

Statistical honesty — do not inflate the posterior. A 12-unit swarm produces
12 results, but they are HIGHLY CORRELATED: one task, one model, one moment.
Recording them as 12 independent Bernoulli trials would massively overstate
confidence and let a single lucky swarm dominate a context cell. So a swarm
records ONE observation against the arm, weighted by the fraction of units
accepted (alpha += fraction, beta += 1-fraction: alpha+beta advances by
exactly 1). Per-unit detail lives in stats only.

Safety: Kimi has no filesystem sandbox, so every swarm member gets its own
directory and its own branch (``kimi/<slug>-<i>``), and results are verified
individually — a swarm that writes into one shared tree will corrupt it.
"""

from __future__ import annotations

from .router import REWARD_EVENT, Router
from .stats import Decision, Stats

#: quotamax headroom -> fan-out cap. "abundant" resolves to the pool's own
#: parallel_limit inside plan_swarm.
HEADROOM_CAPS: dict[str, int | None] = {
    "abundant": None,  # up to pool.parallel_limit
    "comfortable": 8,
    "constrained": 1,  # no swarm
    "critical": 0,     # arm already ineligible — never dispatch
}


def plan_swarm(units: int, parallel_limit: int, headroom: str) -> int:
    """How many instances to dispatch: min(units, pool limit, headroom cap).

    Returns 0 when the headroom is critical (the arm should not have been
    eligible at all) and 1 when there is nothing to fan out.
    """
    if units <= 1 or parallel_limit <= 1:
        return 1 if units >= 1 else 0
    cap = HEADROOM_CAPS.get(headroom, 1)  # unknown headroom: no swarm
    if cap == 0:
        return 0
    effective_cap = parallel_limit if cap is None else cap
    return max(1, min(units, parallel_limit, effective_cap))


def record_swarm_outcome(
    router: Router,
    shape: str,
    tier: str,
    arm: str,
    units_total: int,
    units_accepted: int,
    *,
    stats: Stats | None = None,
) -> float:
    """Record the swarm as ONE weighted trial, not n trials.

    success = fraction of units accepted, credited as a single weighted
    observation: alpha += fraction, beta += 1 - fraction. An n-unit swarm
    advances alpha + beta by exactly 1, never by n. Returns the fraction.
    """
    if units_total <= 0:
        raise ValueError("a swarm needs at least one unit")
    if not 0 <= units_accepted <= units_total:
        raise ValueError("units_accepted must be within [0, units_total]")
    fraction = units_accepted / units_total
    router.record_impression(shape, tier, arm, by=1)
    if fraction:
        router.reward(shape, tier, arm, REWARD_EVENT, by=fraction)
    if stats is not None:
        # Per-unit detail lives in stats only — never in the posterior.
        stats.record_decision(Decision(
            cell=f"route:{shape}:{tier}", shape=shape, tier=tier,
            eligible=[arm], chosen=arm, why="swarm",
            units=[True] * units_accepted + [False] * (units_total - units_accepted),
        ))
    return fraction
