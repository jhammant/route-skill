"""Optional shape and tier classification by Jev (TypeSafe AI's typed-decision model).

The built-in rules are regexes: "build a report generator that summarises each customer's usage" reads as
``batch:summarize`` and most tasks score ``trivial``. Jev answers two typed questions about the task in one call of
a few hundred tokens: which of the closed ``SHAPES`` it is, and where it sits on the ``TIERS`` scale.

OFF BY DEFAULT, because it sends the task text to an outside service (api.typesafe.ai). Turn it on with
``ROUTE_JEV=1`` or, in ``~/.config/route/config.toml``::

    [classify]
    jev = true

It needs a ``jev`` command on PATH (``ROUTE_JEV_CMD`` names another) that accepts
``jev ask --questions FILE --file - --json`` and prints the API's JSON reply.

Rules this module keeps:
- The caller never asks for a sensitive or private task (see ``cli.plan_task``): that text stays on the machine.
- The shape vocabulary stays closed. An answer outside ``SHAPES`` is discarded.
- Jev's shape is used only when it gives its pick at least ``SHAPE_MIN_PROBABILITY``; otherwise the rules stand.
- A ``batch:*`` shape still needs the rules' own volume cue, so one odd answer can't send a coding task to a
  batch pool.
- Any failure (no command, no key, timeout, bad JSON) returns None and the built-in rules decide.

The questions and thresholds live here, in one place, so a person can review them.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10
    import tomli as tomllib

from .complexity import TIERS
from .shapes import BATCH_SHAPES, SHAPES, ShapeClassification, _VOLUME_RE
from .storage import config_dir

#: Jev's pick must carry at least this probability to replace the rules' shape. On 44 hand-labelled tasks
#: (18 Nov 2026) Jev alone was right on 44 and the rules on 30; at 0.5 the combination kept all 44.
SHAPE_MIN_PROBABILITY = 0.5

#: Longest task text sent. A brief's opening carries the ask; the rest is detail.
MAX_CHARS = 6000

TAG = "route-classify"

SHAPE_QUESTION = ("What kind of work is this task asking for? Judge the work to be done, not words that describe "
                  "the thing being built.")

SHAPE_OPTIONS: dict[str, str] = {
    "coding:implement": "Write new code: a feature, tool, script, endpoint or module, even one whose job is to "
                        "summarise, classify or extract",
    "coding:refactor": "Restructure, rename, split, port or simplify existing code without changing what it does",
    "coding:test": "Write or extend automated tests for existing code",
    "coding:debug": "Find and fix a bug, failure, crash, wrong output or slowness in existing code",
    "coding:review": "Read existing code or a diff and report problems, without changing it",
    "batch:classify": "Put each of a large set of existing items into categories or labels, with no code to write",
    "batch:summarize": "Write a summary of each of a large set of existing documents, with no code to write",
    "batch:extract": "Pull named fields out of each of a large set of existing documents, with no code to write",
    "batch:embed": "Compute embeddings or vectors for a set of documents",
    "research": "Find out, compare, investigate or explain something and report back, with no code to write",
    "writing": "Write prose for people: a post, README, documentation, release notes or an announcement",
    "orchestration": "Coordinate work across several repositories or services, or sequence deployments",
}

TIER_QUESTION = "How hard is this task for a capable software engineer or analyst?"

#: One description per entry of ``TIERS``, lowest first.
TIER_LEVELS: tuple[str, ...] = (
    "Trivial: a one-line or mechanical change, such as a typo, a version bump or a wording tweak",
    "Simple: one small, well-specified piece of work in one place, done in well under an hour",
    "Moderate: several connected parts or some investigation, a few hours of work",
    "Hard: many components, a migration, concurrency, security, performance, or work spanning a whole codebase",
    "Open-ended: the goal or approach is not settled; it needs exploration or design before building",
)

assert tuple(SHAPE_OPTIONS) == SHAPES, "SHAPE_OPTIONS must describe every shape, in order"
assert len(TIER_LEVELS) == len(TIERS), "TIER_LEVELS must describe every tier"

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


@dataclass(frozen=True)
class JevReading:
    """What Jev said. Either part is None when its answer was missing or unusable."""

    shape: str | None = None
    shape_probability: float = 0.0
    tier: str | None = None


def enabled(env: dict | None = None) -> bool:
    """``ROUTE_JEV`` when set, else ``[classify] jev`` in config.toml, else off."""
    env = os.environ if env is None else env
    flag = (env.get("ROUTE_JEV") or "").strip().lower()
    if flag in _TRUE:
        return True
    if flag in _FALSE:
        return False
    try:
        raw = tomllib.loads((config_dir() / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return raw.get("classify", {}).get("jev") is True


def _commands(env: dict | None = None) -> list[str]:
    env = os.environ if env is None else env
    named = (env.get("ROUTE_JEV_CMD") or "").strip()
    if named:
        return [named]
    # A hook or scheduled job may run without ~/.local/bin on PATH.
    return ["jev", str(Path.home() / ".local" / "bin" / "jev")]


def questions() -> dict:
    return {
        "shape": {"type": "choice", "instructions": SHAPE_QUESTION, "criteria": dict(SHAPE_OPTIONS)},
        "tier": {"type": "score", "instructions": TIER_QUESTION, "criteria": list(TIER_LEVELS)},
    }


def parse(reply: dict) -> JevReading:
    """A reading from the API's JSON reply. Never raises; anything odd becomes None."""
    answers = reply.get("answers") if isinstance(reply, dict) else None
    if not isinstance(answers, dict):
        return JevReading()
    shape, probability, tier = None, 0.0, None
    picked = answers.get("shape")
    if isinstance(picked, dict) and picked.get("choice") in SHAPES:
        probabilities = picked.get("probabilities")
        value = probabilities.get(picked["choice"]) if isinstance(probabilities, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            shape, probability = picked["choice"], float(value)
    scored = answers.get("tier")
    if isinstance(scored, dict):
        value = scored.get("score")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            tier = TIERS[max(0, min(len(TIERS) - 1, round(value)))]
    return JevReading(shape, probability, tier)


def classify(text: str, *, timeout: float = 8.0, run=subprocess.run, env: dict | None = None) -> JevReading | None:
    """Ask Jev about ``text``. None on any failure, so the caller keeps the built-in rules."""
    text = (text or "").strip()[:MAX_CHARS]
    if not text:
        return None
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(questions(), handle)
            path = handle.name
        for command in _commands(env):
            try:
                proc = run([command, "ask", "--questions", path, "--file", "-", "--json", "--tag", TAG],
                           input=text, capture_output=True, text=True, timeout=timeout)
            except FileNotFoundError:
                continue
            except (OSError, subprocess.TimeoutExpired):
                return None
            if proc.returncode != 0:
                return None
            try:
                reading = parse(json.loads(proc.stdout))
            except ValueError:
                return None
            return reading if (reading.shape or reading.tier) else None
        return None
    except OSError:
        return None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def refine_shape(current: ShapeClassification, reading: JevReading | None, text: str) -> ShapeClassification:
    """Jev's shape when it is sure enough and passes the batch guard; otherwise ``current`` unchanged."""
    if reading is None or reading.shape is None or reading.shape_probability < SHAPE_MIN_PROBABILITY:
        return current
    if reading.shape in BATCH_SHAPES and not _VOLUME_RE.search(text or ""):
        return current
    return ShapeClassification(reading.shape, "high", "jev")
