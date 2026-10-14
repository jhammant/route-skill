"""Bounded, local-only task similarity memory. Never a federation input."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import unicodedata
from pathlib import Path

from .storage import atomic_write, state_dir

MAX_ROWS = 2000
STOP_WORDS = frozenset("a an and are as at be by for from in is it of on or the this to with".split())


def normalise_tokens(text: str) -> set[str]:
    words = re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())
    return set(words) - STOP_WORDS


def jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


class TaskMemory:
    """One row per decision; pending rows cannot act as neighbours.

    Hash every task, including open tasks, so comparisons work across privacy
    modes without ever saving raw tokens. The per-install HMAC salt is local.
    """

    def __init__(self, directory: str | Path | None = None, *, max_rows: int = MAX_ROWS):
        if max_rows < 1:
            raise ValueError("memory must retain at least one row")
        base = Path(directory) if directory is not None else state_dir()
        self.path = base / "task-memory.json"
        self.salt_path = base / "task-memory.salt"
        self.max_rows = max_rows

    def _salt(self) -> bytes:
        self.salt_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self.salt_path.read_bytes()
        except FileNotFoundError:
            pass
        # Publish a fully written salt without replacing another process's
        # salt. Readers must never hash with an empty, half-created file.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".memory-salt-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(secrets.token_bytes(32))
            try:
                os.link(tmp, self.salt_path)
            except FileExistsError:
                pass
        finally:
            Path(tmp).unlink(missing_ok=True)
        return self.salt_path.read_bytes()

    def tokens(self, text: str) -> list[str]:
        salt = self._salt()
        return sorted(hmac.new(salt, token.encode("utf-8"), hashlib.sha256).hexdigest()
                      for token in normalise_tokens(text))

    def rows(self) -> list[dict]:
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return rows if isinstance(rows, list) else []

    def remember(self, decision_id: str, text: str, shape: str, tier: str, arm: str) -> None:
        rows = self.rows()
        if any(row["decision_id"] == decision_id for row in rows):
            return
        rows.append({"decision_id": decision_id, "tokens": self.tokens(text),
                     "shape": shape, "tier": tier, "arm": arm, "score": None})
        self._save(rows)

    def settle(self, decision_id: str, score: float) -> None:
        rows = self.rows()
        for row in rows:
            if row["decision_id"] == decision_id:
                row["score"] = score
                self._save(rows)
                return

    def _save(self, rows: list[dict]) -> None:
        atomic_write(self.path, json.dumps(rows[-self.max_rows:], sort_keys=True))

    def neighbours(self, text: str, shape: str, eligible: list[str], *,
                   k: int = 5, minimum: float = 0.25) -> list[dict]:
        tokens = set(self.tokens(text))
        found = []
        for row in self.rows():
            if row["shape"] != shape or row["arm"] not in eligible or row["score"] is None:
                continue
            similarity = jaccard(tokens, set(row["tokens"]))
            if similarity >= minimum:
                found.append({"arm": row["arm"], "score": row["score"], "similarity": similarity})
        return sorted(found, key=lambda row: row["similarity"], reverse=True)[:k]
