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

import re
from dataclasses import dataclass

from .router import REWARD_EVENT, Router
from .stats import Decision, Stats

#: quotamax headroom -> fan-out cap. "abundant" resolves to the pool's own
#: parallel_limit inside plan_swarm.
HEADROOM_CAPS: dict[str, int | None] = {
    "abundant": None,  # up to pool.parallel_limit
    "comfortable": 8,
    "constrained": 1,  # no swarm
    "critical": 0,     # arm already ineligible — never dispatch
    # Provider pools (codex, kimi) come from parse_quota's own scale:
    # ok < 80% used <= low < 95% <= critical.
    "ok": 8,
    "low": 1,
}


@dataclass(frozen=True)
class SwarmHint:
    """Decomposability only; the task planner adds the chosen arm's quota cap."""

    suggested: bool
    units: int
    cues: list[str]


def detect_swarm(text: str, shape: str) -> SwarmHint:
    """Conservative, local hints; never create or dispatch a swarm implicitly."""
    if not shape.startswith("coding:"):
        return SwarmHint(False, 1, [])
    cues = []
    counts = [1]
    lines = re.findall(r"^\s*(?:[-*+] |\d+[.)]\s+)\S.*", text, re.MULTILINE)
    if len(lines) >= 2:
        cues.append("listed units")
        counts.append(len(lines))
    paths = set(re.findall(
        r"(?<![\w/])(?:(?:[\w.-]+/)+[\w.-]+|[\w-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|c|h|cpp|"
        r"cs|vue|svelte|sql|ya?ml|json|toml|md|html|css|sh|swift|kt|php|lua))\b",
        text, re.IGNORECASE))
    modules = set(re.findall(r"\b([\w.]+)\s+module\b", text, re.IGNORECASE))
    modules -= {"the", "a", "this", "each"}
    if max(len(paths), len(modules)) >= 2:
        cues.append("distinct files or modules")
        counts.append(max(len(paths), len(modules)))
    explicit = re.findall(r"\b(\d+)\s+(?:files|modules|endpoints|services|pages|tests)\b", text, re.IGNORECASE)
    if explicit and max(map(int, explicit)) >= 2:
        cues.append("explicit unit count")
        counts.append(max(map(int, explicit)))
    cue = re.search(
        r"\b(?:for each of|each of|all of these|these\s+\d+\s+(?:files|modules|endpoints|services|pages|tests)|"
        r"in parallel|independently|modules)\b\s*:?\s*([^\n;]+)", text, re.IGNORECASE)
    if cue:
        items = [part.strip() for part in re.split(r",|\band\b", cue[1]) if part.strip()]
        if len(items) >= 2:
            cues.append("enumeration")
            counts.append(len(items))
    sequential = bool(re.search(r"\b(?:then|after that|once\b[^.\n]*\bis done)\b", text, re.IGNORECASE))
    single = bool(re.search(r"\b(?:this function|the bug)\b", text, re.IGNORECASE))
    if sequential:
        cues.append("sequential dependency")
    if single:
        cues.append("single target")
    units = max(counts)
    return SwarmHint(units >= 2 and not sequential and not single, units, cues)


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
