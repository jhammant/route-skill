"""Complexity tier: taskgauge bridge + built-in lexical fallback.

Tiers come from taskgauge's ``estimateComplexity`` (``trivial | simple |
moderate | hard | open-ended``), shelled out via ``node``. taskgauge is
OPTIONAL — the lexical fallback below keeps the published tool standalone.
"""

from __future__ import annotations

import re
import subprocess

TIERS: tuple[str, ...] = ("trivial", "simple", "moderate", "hard", "open-ended")

_HARD_RE = re.compile(
    r"\b(distributed|migration|migrate|architecture|concurrency|concurrent|race|"
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
    """Best-available tier for ``text``; always a member of ``TIERS``."""
    if use_taskgauge:
        tier = _estimate_taskgauge(text)
        if tier is not None:
            return tier
    return _estimate_lexical(text)
