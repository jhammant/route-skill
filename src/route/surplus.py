"""Signed quota adjustments, applied only after eligibility and vetoes."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .storage import config_dir

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

WEEK_MINUTES = 7 * 24 * 60
DAY_MINUTES = 24 * 60
MIN_RESET_MINUTES = 2 * 24 * 60
DEFAULT_K = 0.15


def _finite_number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _projected_fractions(percent_used, resets_in_minutes) -> tuple[float, float]:
    """(surplus, overuse), with symmetric first-day damping; no I/O."""
    if not all(_finite_number(v) for v in (percent_used, resets_in_minutes)):
        return 0.0, 0.0
    if not 0 <= percent_used <= 100 or not 0 < resets_in_minutes <= WEEK_MINUTES:
        return 0.0, 0.0
    elapsed_minutes = WEEK_MINUTES - resets_in_minutes
    if elapsed_minutes <= 0:
        return 0.0, 0.0
    elapsed_fraction = elapsed_minutes / WEEK_MINUTES
    used_fraction = percent_used / 100
    projected_end = used_fraction / elapsed_fraction
    surplus = min(1.0, max(0.0, 1 - projected_end))
    overuse = min(1.0, max(0.0, projected_end - 1))
    if elapsed_minutes < DAY_MINUTES:
        surplus = max(max(0.0, elapsed_fraction - used_fraction), 0.5 * surplus)
        overuse = max(max(0.0, used_fraction - elapsed_fraction), 0.5 * overuse)
    # Spare quota is encouraged only with >=2 days left. Pressure remains
    # useful until reset; it does not remove an arm or replace quota gating.
    return (surplus if resets_in_minutes >= MIN_RESET_MINUTES else 0.0), overuse


def surplus_bonus(percent_used: float, resets_in_minutes: float, k: float = DEFAULT_K) -> float:
    """Legacy positive-only helper; new callers should use quota_adjustment."""
    if not _finite_number(k):
        return 0.0
    surplus, _ = _projected_fractions(percent_used, resets_in_minutes)
    return min(DEFAULT_K, max(0.0, k)) * surplus


def quota_adjustment(percent_used: float, resets_in_minutes: float, k: float = DEFAULT_K,
                     *, headroom: str | None = None) -> float:
    """Bounded signed preference: projected surplus, or pressure when constrained.

    Constrained headroom imposes at least half of k in pressure even when the
    weekly window is missing or under-used (a session limit may be binding).
    Pressure takes precedence over surplus, rather than cancelling it out.
    """
    if not _finite_number(k):
        return 0.0
    coefficient = min(DEFAULT_K, max(0.0, k))
    surplus, overuse = _projected_fractions(percent_used, resets_in_minutes)
    if headroom == "constrained":
        overuse = max(overuse, 0.5)
    return -coefficient * overuse if overuse else coefficient * surplus


def load_surplus_k() -> float:
    path = config_dir() / "config.toml"
    if not path.is_file():
        return DEFAULT_K
    raw = tomllib.loads(path.read_text(encoding="utf-8")).get("surplus", {}).get("k", DEFAULT_K)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ValueError("config.toml: surplus.k must be a finite number")
    return min(DEFAULT_K, max(0.0, float(raw)))


def parse_weekly(agent, providers, *, now: datetime | None = None) -> dict[str, dict]:
    """Keep weekly windows from the same payloads used for headroom gates.

    Agent view uses weekly.percentUsed/resetsInMinutes; provider limits use
    percent and either resetsInMinutes or an absolute resetsAt timestamp.
    """
    now = now or datetime.now(timezone.utc)
    windows = {}

    def add(provider, window):
        if not isinstance(window, dict):
            return
        percent = window.get("percentUsed", window.get("percent"))
        minutes = window.get("resetsInMinutes")
        if minutes is None and window.get("resetsAt"):
            try:
                reset = datetime.fromisoformat(window["resetsAt"].replace("Z", "+00:00"))
                minutes = (reset - now).total_seconds() / 60
            except (ValueError, TypeError, AttributeError):
                return
        windows[provider] = {"percentUsed": percent, "resetsInMinutes": minutes}

    if isinstance(agent, dict) and agent.get("ok"):
        add("claude", agent.get("weekly"))
    if isinstance(providers, list):
        for provider in providers:
            if not isinstance(provider, dict) or not provider.get("ok") or not provider.get("id"):
                continue
            if isinstance(provider.get("weekly"), dict):
                add(provider["id"], provider["weekly"])
            for window in provider.get("limits", []):
                if not isinstance(window, dict):
                    continue
                label = str(window.get("label", "")).lower()
                if "week" in label or label in ("7d", "7 days", "7-day"):
                    add(provider["id"], window)
    return windows
