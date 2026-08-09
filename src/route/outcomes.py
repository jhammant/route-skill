"""Outcome -> reward events, and the escalation ladder (SPEC steps 6-7).

Events, ranked by the signal that is hardest to fake:

    accepted  — diff kept, no escalation needed — the REAL signal (drives selection)
    verified  — tests passed
    completed — ran without erroring

Escalation is capped at one hop: on failed verification, retry once on the
next-stronger eligible arm. Escalating away from an arm records a negative
outcome for it (a resolved trial with no reward), so unreliable routes decay
without anyone tuning weights.

Every function here resolves exactly one trial for the arm it names — that is
what makes an outcome an observation. A dispatch that never reaches this
module is left unresolved on purpose: it is unknown, not failed.
"""

from __future__ import annotations

from .pools import Pool
from .router import Router

#: outcome -> reward events to record, cumulative up the ladder.
OUTCOME_EVENTS: dict[str, tuple[str, ...]] = {
    "completed": ("completed",),
    "verified": ("completed", "verified"),
    "accepted": ("completed", "verified", "accepted"),
    "failed": (),
}


def record_outcome(router: Router, shape: str, tier: str, arm: str, outcome: str) -> None:
    """Record an outcome as its reward events. Unknown outcomes raise.

    Resolves one trial whatever the outcome — including ``failed``, whose
    event tuple is empty. That used to make ``route outcome --outcome failed``
    a no-op on the posterior, its whole effect coming from the impression
    already booked at selection; a reported failure now moves beta itself, and
    is the only thing that does.
    """
    events = OUTCOME_EVENTS[outcome]  # raises before anything is written
    for event in events:
        router.reward(shape, tier, arm, event)
    router.record_resolution(shape, tier, arm)


def record_failure(router: Router, shape: str, tier: str, arm: str) -> None:
    """A negative outcome: a resolved trial with no reward."""
    router.record_resolution(shape, tier, arm)


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
