"""Outcome -> reward events, and the escalation ladder (SPEC steps 6-7).

Events, ranked by the signal that is hardest to fake:

    accepted  — diff kept, no escalation needed — the REAL signal (drives selection)
    verified  — tests passed
    completed — ran without erroring

Escalation is capped at one hop: on failed verification, retry once on the
next-stronger eligible arm. Escalating away from an arm records a negative
outcome for it (an impression with no reward), so unreliable routes decay
without anyone tuning weights.
"""

from __future__ import annotations

import math

from .pools import Pool
from .router import Router

#: outcome -> reward events to record, cumulative up the ladder.
OUTCOME_EVENTS: dict[str, tuple[str, ...]] = {
    "completed": ("completed",),
    "verified": ("completed", "verified"),
    "accepted": ("completed", "verified", "accepted"),
    "failed": (),
}


def validate_score(score: float) -> float:
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("score must be a finite number between 0 and 1")
    return float(score)


def score_from_fix_passes(fix_passes: int = 0, *, abandoned: bool = False) -> float:
    """One rescue pass costs 0.3; abandonment or a rewrite earns zero."""
    if isinstance(fix_passes, bool) or not isinstance(fix_passes, int) or fix_passes < 0:
        raise ValueError("fix passes must be a non-negative integer")
    return 0.0 if abandoned else max(0.0, round(1.0 - 0.3 * fix_passes, 10))


def record_score(router: Router, shape: str, tier: str, arm: str, score: float, *,
                 previous_score: float = 0.0, impression_recorded: bool = False) -> None:
    """Credit one fractional trial, or replace an existing trial's reward.

    A dispatch already owns its impression. Moving its reward by the delta
    changes alpha by that delta and beta by its negative, never its mass.
    """
    score = validate_score(score)
    previous_score = validate_score(previous_score)
    if not impression_recorded:
        router.observe(shape, tier, arm, [arm])
    delta = score - previous_score
    if delta:
        # Write directly: settlement must report storage errors, not silently
        # swallow them through banditry.reward's fail-open policy.
        from .router import REWARD_EVENT, cell_name
        router.storage.incr(f"{cell_name(shape, tier)}:{REWARD_EVENT}", arm, delta)


def record_outcome(router: Router, shape: str, tier: str, arm: str, outcome: str) -> None:
    """Record an outcome as its reward events. Unknown outcomes raise."""
    events = OUTCOME_EVENTS[outcome]
    for event in events:
        router.reward(shape, tier, arm, event)


def record_failure(router: Router, shape: str, tier: str, arm: str) -> None:
    """A negative outcome: an impression with no reward."""
    router.record_impression(shape, tier, arm)


def escalate(
    router: Router,
    shape: str,
    tier: str,
    failed_arm: str,
    eligible: list[str],
    pools: dict[str, Pool],
    *,
    already_escalated: bool = False,
) -> str | None:
    """Retry once on the next-stronger eligible arm; None when capped.

    Records a negative outcome for the arm being escalated away from.
    """
    if already_escalated or failed_arm not in pools:
        return None
    record_failure(router, shape, tier, failed_arm)
    failed_strength = pools[failed_arm].strength
    candidates = [
        arm for arm in eligible
        if arm in pools and pools[arm].strength > failed_strength
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda a: pools[a].strength)
    return candidates[0]
