"""Closed-vocabulary shape classification.

PRIVACY-CRITICAL. The shape becomes the federation context key
(``route:{shape}:{tier}``), so the vocabulary is a fixed enum — a free-text
key would leak task content. Never emit a shape outside ``SHAPES``; anything
unrecognised maps to ``orchestration``, which never routes away. Adding a
shape is a schema version bump, not a config tweak.
"""

from __future__ import annotations

import re
import subprocess

SCHEMA_VERSION = 1

SHAPES: tuple[str, ...] = (
    "coding:implement",
    "coding:refactor",
    "coding:test",
    "coding:debug",
    "coding:review",
    "batch:classify",
    "batch:summarize",
    "batch:extract",
    "batch:embed",
    "research",
    "writing",
    "orchestration",
)

#: Shapes that operate on many items at once — never agentic remote pools.
BATCH_SHAPES: tuple[str, ...] = tuple(s for s in SHAPES if s.startswith("batch:"))

#: The shape nothing routes away from; also the unknown-task fallback.
FALLBACK_SHAPE = "orchestration"

# Volume cues: the task is about many items, not one thing.
_VOLUME_RE = re.compile(
    r"\b\d{2,}\b|\b\d{1,3}(?:,\d{3})+\b|"
    r"\b(every|each|all|these|those|bulk|corpus|hundreds|thousands)\b",
    re.IGNORECASE,
)

# Ordered (shape, pattern) rules; first match wins. Deterministic heuristics
# over verb + object + volume cues, per SPEC step 1.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("batch:embed", re.compile(r"\b(embed|embeds|embedding|embeddings|vectori[sz]\w*)\b", re.I)),
    ("batch:classify", re.compile(
        r"\b(classif\w*|categori[sz]\w*|labell?\w*|tag\w*|sort\w*)\b", re.I)),
    ("batch:summarize", re.compile(
        r"\b(summari[sz]\w*|tldr|digest\w*|condense\w*)\b", re.I)),
    ("batch:extract", re.compile(
        r"\b(extract\w*|scrape\w*|harvest\w*|pull out)\b", re.I)),
    ("orchestration", re.compile(
        r"\b(orchestrat|coordinat|cross[- ]repo|multi[- ]repo|"
        r"across (repos|repositories|directories|projects|services)|"
        r"deploy\b.*\bthen\b)\b", re.I)),
    ("coding:review", re.compile(
        r"\b(review|audit|look over)\b[\s\S]*\b(code|diff|pr|pull request|patch|"
        r"branch|commit|changes|implementation)\b|\bcode review\b", re.I)),
    ("coding:debug", re.compile(
        r"\b(debug|fix|bug|broken|failing|flaky|crash|error|exception|"
        r"stack ?trace|regression|traceback|why (is|does|did|won't))\b", re.I)),
    ("coding:test", re.compile(
        r"\b(test|tests|testing|coverage|pytest|unittest|test suite|spec)\b", re.I)),
    ("coding:refactor", re.compile(
        r"\b(refactor|rename|restructure|simplify|clean ?up|dedup|"
        r"extract (a )?(function|method|class)|split (up|into)|reorgani[sz]e)\b", re.I)),
    ("coding:implement", re.compile(
        r"\b(implement|scaffold|develop)\b|"
        r"\b(add|build|create|write|make)\b[\s\S]*\b(function|feature|endpoint|"
        r"cli|class|module|script|api|command|tool|flag|option|handler|parser|"
        r"endpoint|component|integration)\b", re.I)),
    ("research", re.compile(
        r"\b(research|investigate|compare|find out|look up|survey|explain|"
        r"what is|what are|how does|how do|pros and cons|evaluate|which (is|should))\b", re.I)),
    ("writing", re.compile(
        r"\b(write|draft|rewrite|proofread|document)\b[\s\S]*\b(blog|post|article|"
        r"readme|docs?|documentation|essay|copy|announcement|changelog|release notes|"
        r"guide|tutorial)\b", re.I)),
)

_LLM_PROMPT = (
    "Classify the task below into exactly one of these labels. "
    "Reply with the label only, nothing else.\n"
    + "\n".join(SHAPES)
    + "\n\nTask:\n"
)


def _classify_deterministic(text: str) -> str | None:
    """First-match heuristic pass. Returns None when nothing matches."""
    for shape, pattern in _RULES:
        if not pattern.search(text):
            continue
        # batch:{classify,summarize,extract} also need a volume cue — without
        # one, "extract the version string" is a single-item coding task.
        if shape in ("batch:classify", "batch:summarize", "batch:extract"):
            if not _VOLUME_RE.search(text):
                continue
        return shape
    return None


def _classify_llm(text: str, timeout: float = 15.0) -> str | None:
    """Cheap local-LLM tiebreak via `local-llm ask --class reflex`.

    Fully fail-safe: any error, timeout, or off-enum answer returns None.
    """
    try:
        proc = subprocess.run(
            ["local-llm", "ask", "--class", "reflex", _LLM_PROMPT + text],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    answer = proc.stdout.strip().lower()
    return answer if answer in SHAPES else None


def classify_shape(text: str, *, use_llm: bool = False) -> str:
    """Map task text onto the closed shape enum.

    Deterministic heuristics first; an optional cheap local-LLM call breaks
    ties; anything still unknown maps to ``orchestration`` (which never
    routes away). The return value is ALWAYS a member of ``SHAPES``.
    """
    text = (text or "").strip()
    shape = _classify_deterministic(text) if text else None
    if shape is None and use_llm:
        shape = _classify_llm(text)
    if shape not in SHAPES:
        shape = FALLBACK_SHAPE
    return shape


def is_valid_shape(shape: str) -> bool:
    return shape in SHAPES
