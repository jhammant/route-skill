"""Deterministic selection keeps ultra on the strongest available model families."""

from __future__ import annotations

import re

from .eligibility import quota_keys
from .pools import Pool, pool_family

ULTRA_MIN_RANK = 2


def model_rank(pool: Pool) -> int:
    """Pinned tier words outrank arm defaults; substrings are not model tiers."""
    name, model = pool.name.lower(), pool.model.name.lower()
    words = set(re.findall(r"[a-z0-9]+", model))
    if "fable" in words:
        return 3
    if "opus" in words:
        return 2
    if words & {"sonnet", "kimi"}:
        return 1
    if "haiku" in words:
        return 0
    # Codex's pinned OpenAI model retains its existing rank-two tier.
    if name == "codex" and (words & {"gpt", "codex"}
                             or any(re.fullmatch(r"o\d+", word) for word in words)):
        return 2
    if model in {"", "unknown", "claude"}:
        if name in {"claude-work", "claude", "codex"}:
            return 2
        if name == "kimi":
            return 1
    return 0


def candidates(
    pools: dict[str, Pool],
    quota: dict[str, str],
    *,
    host: str = "claude",
    allow_lower: bool = False,
    exclude_families: set[str] = frozenset(),
    exclude_arms: set[str] = frozenset(),
) -> list[str]:
    """Apply account and model-window gates before allowing weaker fallback arms."""
    ranked = []
    for arm, pool in pools.items():
        if arm in exclude_arms or pool_family(pool) in exclude_families:
            continue
        headroom = [quota.get(key, "ok") for key in quota_keys(arm, pools)]
        if "critical" in headroom:
            continue
        rank = model_rank(pool)
        ranked.append((-rank, "low" in headroom, arm == host, arm))
    best = [item for item in ranked if -item[0] >= ULTRA_MIN_RANK]
    usable = best if best else (ranked if allow_lower else [])
    return [arm for _, _, _, arm in sorted(usable)]


def pick(
    role: str,
    pools: dict[str, Pool],
    quota: dict[str, str],
    *,
    tier: str = "moderate",
    host: str = "claude",
    author_arm: str | None = None,
    allow_lower: bool = False,
) -> str | None:
    """Spend the strongest models on hard work and keep reviews independent."""
    if role not in {"planner", "worker", "reviewer", "fixer", "integrator"}:
        raise ValueError(f"unknown ultra role: {role}")
    excluded = {author_arm} if role == "reviewer" and author_arm is not None else set()
    available = candidates(pools, quota, host=host, allow_lower=allow_lower,
                           exclude_arms=excluded)
    if not available:
        return None
    if role == "worker":
        preferred_rank = 3 if tier in {"hard", "open-ended"} else ULTRA_MIN_RANK
        return next((arm for arm in available if model_rank(pools[arm]) == preferred_rank),
                    available[0])
    if author_arm is not None and role in {"reviewer", "fixer"}:
        family = pool_family(pools[author_arm])
        if role == "reviewer":
            return next((arm for arm in available if pool_family(pools[arm]) != family),
                        available[0])
        if author_arm in available:
            return author_arm
        return next((arm for arm in available if pool_family(pools[arm]) == family),
                    available[0])
    return available[0]


def review_independence(author_arm: str, reviewer_arm: str, pools: dict[str, Pool]) -> str:
    """Expose same-family fallback so callers cannot claim independent review."""
    if author_arm == reviewer_arm:
        return "self"
    if pool_family(pools[author_arm]) == pool_family(pools[reviewer_arm]):
        return "same-family"
    return "cross-family"


def dispatch_template(arm: str, pools: dict[str, Pool], *, host: str = "claude") -> str:
    """Resolve stay-here markers even when pools have not been adapted to the host."""
    template = pools[arm].dispatch
    if template:
        return template
    if arm == "claude":
        return "claude --print --permission-mode auto {task}"
    if arm == "codex":
        return "codex exec --sandbox workspace-write {task}"
    raise ValueError(f"no dispatch template for arm: {arm}")
