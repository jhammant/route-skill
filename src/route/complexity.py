"""Complexity tier: taskgauge bridge + built-in lexical fallback.

Tiers come from taskgauge's ``estimateComplexity`` (``trivial | simple |
moderate | hard | open-ended``), shelled out via ``node``. taskgauge is
OPTIONAL — the lexical fallback below keeps the published tool standalone.
"""

from __future__ import annotations

import re
import subprocess

from .shapes import _is_feature_build

TIERS: tuple[str, ...] = ("trivial", "simple", "moderate", "hard", "open-ended")

# Count distinct component kinds, not repeated mentions or plural variants.
_COMPONENT_RE = re.compile(r"\b(route|endpoint|migration|webhook|worker|test|flag)s?\b", re.I)

#: Long-horizon cues: the task is many steps over a long wall-clock, not one
#: thing. This is a separate axis from difficulty — "fix every failing test
#: in the suite" may be individually easy — but for routing it behaves like
#: ``hard``: it wants an arm that stays coherent across hours (Codex on
#: GPT-6 Astra keeps searchable notes across context windows; a local
#: model does not).
_LONG_HORIZON_RE = re.compile(
    r"\b(long[- ]running|long[- ]horizon|overnight|multi[- ]hour|for hours|"
    r"until (all |every |the )?(tests?|checks?|suite|ci|build) (pass|passes|is green|are green)|"
    r"keep (going|iterating|working) until|"
    r"(whole|entire|full|every file in the) (codebase|repo|repository|project|monorepo)|"
    r"end[- ]to[- ]end (migration|rewrite|port)|"
    r"(all|every) (\d+ )?(call ?sites?|usages?|occurrences?|modules?|endpoints?|packages?))\b",
    re.IGNORECASE,
)

_HARD_RE = re.compile(
    r"\b(distributed|migrations?|migrate|architecture|concurrency|concurrent|race|"
    r"security|auth(entication|orization)?|performance|optimi[sz]e|redesign|"
    r"from scratch|backwards?[- ]compat|database schema|deadlock)\b",
    re.IGNORECASE,
)
_TRIVIAL_RE = re.compile(
    r"\b(typo|one[- ]liner|bump|tweak|rename (a |the )?(variable|flag)|"
    r"comment|wording|whitespace)\b",
    re.IGNORECASE,
)
_OPEN_ENDED_RE = re.compile(
    r"\b(open[- ]ended|figure out|explore|not sure|spike|investigate whether|"
    r"design (a|the|an) (system|architecture)|greenfield|proof of concept)\b",
    re.IGNORECASE,
)


def is_valid_tier(tier: str) -> bool:
    return tier in TIERS


def is_long_horizon(text: str) -> bool:
    """True when the task reads as many steps over a long wall-clock."""
    return bool(_LONG_HORIZON_RE.search(text or ""))


def _estimate_taskgauge(text: str, timeout: float = 5.0) -> str | None:
    """Shell out to taskgauge via node. Any failure returns None."""
    script = (
        "const { estimateComplexity } = require('taskgauge');"
        "const r = estimateComplexity(process.argv[1]);"
        "console.log(typeof r === 'string' ? r : (r && r.tier) || '');"
    )
    try:
        proc = subprocess.run(
            ["node", "-e", script, text],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    tier = proc.stdout.strip().lower()
    return tier if tier in TIERS else None


def _estimate_lexical(text: str) -> str:
    """Deterministic fallback scorer — no dependencies, no network."""
    words = text.split()
    if _OPEN_ENDED_RE.search(text):
        return "open-ended"
    score = 0
    if _HARD_RE.search(text):
        score += 2
    if len(words) > 25:
        score += 1
    if re.search(r"\b(files|modules|services|packages|repos)\b", text, re.I):
        score += 1
    if _TRIVIAL_RE.search(text):
        score -= 1
    if score <= 0:
        return "trivial"
    if score == 1:
        return "simple"
    if score == 2:
        return "moderate"
    return "hard"


def estimate_complexity(text: str, *, use_taskgauge: bool = True) -> str:
    """Best-available tier for ``text``; always a member of ``TIERS``.

    Feature builds floor at ``moderate`` (the enum's medium tier), or ``hard``
    when at least three distinct component kinds are named. Long-horizon
    tasks also floor at ``hard``. Floors apply to either estimator without
    downgrading a stronger estimate, including ``open-ended``.
    """
    tier = None
    if use_taskgauge:
        tier = _estimate_taskgauge(text)
    if tier is None:
        tier = _estimate_lexical(text)
    floor = "trivial"
    if _is_feature_build(text):
        components = {match.group(1).lower() for match in _COMPONENT_RE.finditer(text)}
        floor = "hard" if len(components) >= 3 else "moderate"
    if is_long_horizon(text):
        floor = "hard"
    return max((tier, floor), key=TIERS.index)
