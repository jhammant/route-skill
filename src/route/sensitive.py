"""Explicit-only detection of sensitive tasks (SPEC-ship section 1).

Sensitivity is NEVER inferred from content heuristics: a false negative
silently sends private data to a third party, and no regex is worth that
risk. Three explicit signals only:

1. ``--sensitive`` on the command line.
2. A ``sensitive: true`` field on a task passed programmatically.
3. A path allowlist in ``~/.config/route/sensitive.toml`` — e.g.
   ``paths = ["~/dev/clients/*", "~/Documents/finance/*"]``. A task naming a
   file under one of those paths is sensitive.

The old content-matching regex in ``eligibility.detect_veto`` keeps firing
as an ADDITIONAL safety net (it conservatively pins the task to ``claude``),
but it must never be the only signal a user relies on. When unsure, the user
marks it.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from .storage import config_dir

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

_GLOB_CHARS = "*?["
_TOKEN_STRIP = "\"'`.,;:()[]{}<>"


def load_sensitive_patterns(path: str | Path | None = None) -> list[str]:
    """The path allowlist from ``sensitive.toml`` (``paths = [...]``).

    Patterns are ``expanduser``-ed. Missing or unreadable config means no
    patterns — never an error.
    """
    path = Path(path) if path is not None else config_dir() / "sensitive.toml"
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [os.path.expanduser(str(p)) for p in data.get("paths") or []]


def names_sensitive_path(text: str, patterns: list[str]) -> str | None:
    """The allowlist pattern a path named in ``text`` falls under, or None.

    A glob pattern (``~/dev/clients/*``) matches a token directly; a plain
    path matches itself and anything beneath it.
    """
    for token in text.split():
        token = token.strip(_TOKEN_STRIP)
        if not token:
            continue
        for pattern in patterns:
            if any(c in pattern for c in _GLOB_CHARS):
                if fnmatch.fnmatch(token, pattern):
                    return pattern
            else:
                root = pattern.rstrip(os.sep)
                if token == root or token.startswith(root + os.sep):
                    return pattern
    return None


def detect_sensitive(
    text: str,
    *,
    flag: bool = False,
    task_field: bool = False,
    patterns: list[str] | None = None,
) -> bool:
    """True only on an explicit signal: flag, task field, or path allowlist."""
    if flag or task_field:
        return True
    if patterns is None:
        patterns = load_sensitive_patterns()
    return names_sensitive_path(text, patterns) is not None
