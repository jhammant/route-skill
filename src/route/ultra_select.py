"""Deterministic selection keeps ultra on the strongest available model families."""

from __future__ import annotations

from .eligibility import quota_keys
from .pools import Pool, pool_family

ULTRA_MIN_RANK = 2


def model_rank(pool: Pool) -> int:
    """Rank pinned models, independent of the learned routing priors."""
    name, model = pool.name.lower(), pool.model.name.lower()
    if "fable" in model:
        return 3
    if "opus" in model or name in {"claude-work", "claude", "codex"}:
        return 2
    if "sonnet" in model or "kimi" in model or name == "kimi":
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
    """Turn the host's stay-here marker into a command for a delegated ultra unit."""
    template = pools[arm].dispatch
    if template or arm != host:
        return template
    if host == "claude":
        return "claude --print --permission-mode auto {task}"
    if host == "codex":
        return "codex exec --sandbox workspace-write {task}"
    raise ValueError(f"unsupported host: {host}")
