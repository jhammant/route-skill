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
from dataclasses import dataclass

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


# A feature build: a build verb plus at least two delivery cues. Summarise or
# classify words inside such a spec ("digest", "summarising") describe the
# feature, not the task, so this beats the batch rules.
_BUILD_VERB_RE = re.compile(r"\b(implement|build|scaffold|develop)\b", re.I)
_FEATURE_RE = re.compile(r"\b(feature|endpoint|signed redirect route)\b", re.I)
_DELIVERY_CUE_RE = re.compile(
    r"\b(migrations?|tests?|branch|typecheck|endpoints?|schema|commit|pull request)\b", re.I)


def _is_feature_build(text: str) -> bool:
    cues = {m.group(0).lower().rstrip("s") for m in _DELIVERY_CUE_RE.finditer(text)}
    return bool(_BUILD_VERB_RE.search(text)) and (len(cues) >= 2 or bool(_FEATURE_RE.search(text)))


def _classify_deterministic(text: str) -> str | None:
    """First-match heuristic pass. Returns None when nothing matches."""
    if _is_feature_build(text):
        return "coding:implement"
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


@dataclass(frozen=True)
class ShapeClassification:
    shape: str
    confidence: str
    source: str


_STRONG_CUE = {
    "coding:debug": re.compile(r"\b(debug|fix|why)\b", re.I),
    "coding:test": re.compile(r"\b(write|add|create|run|test)\b", re.I),
    "batch:summarize": re.compile(r"\b(summari[sz]\w*|condense\w*|tldr)\b", re.I),
    "batch:embed": re.compile(r"\b(embed|vectori[sz]\w*)\b", re.I),
    "batch:classify": re.compile(r"\b(classif\w*|categori[sz]\w*)\b", re.I),
}


def classify_with_confidence(text: str, *, use_llm: bool = False) -> ShapeClassification:
    """Closed-enum classification with heuristic confidence, not a probability.

    Defaults and ambiguous noun cues are low confidence. When requested, the
    existing local LLM may resolve either; failed/off-enum replies keep the
    deterministic result and its low confidence.
    """
    text = (text or "").strip()
    shape = _classify_deterministic(text) if text else None
    strong_cue = _STRONG_CUE.get(shape)
    low = shape is None or (strong_cue is not None and not strong_cue.search(text))
    if low and use_llm:
        llm_shape = _classify_llm(text)
        if llm_shape in SHAPES:
            return ShapeClassification(llm_shape, "high", "llm")
    return ShapeClassification(shape or FALLBACK_SHAPE, "low" if low else "high",
                               "deterministic" if shape else "fallback")


def classify_shape(text: str, *, use_llm: bool = False) -> str:
    """Backward-compatible shape-only interface; always a member of SHAPES."""
    return classify_with_confidence(text, use_llm=use_llm).shape


def is_valid_shape(shape: str) -> bool:
    return shape in SHAPES
