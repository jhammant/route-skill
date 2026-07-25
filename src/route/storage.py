"""JSON-file storage backend for banditry, plus state/config path helpers.

banditry duck-types its storage: anything with ``.incr(key, arm, by=1)`` and
``.counts(key)`` works. This backend keeps the whole router local and
dependency-free — no Redis for a single-user CLI. Writes are atomic
(tmp file + rename) so a crash mid-write can't corrupt the counts.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

APP_NAME = "route"


def state_dir() -> Path:
    """Where counts, decision logs and federation state live."""
    override = os.environ.get("ROUTE_STATE_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / APP_NAME


def config_dir() -> Path:
    """Where pools.toml / priors.toml / config.toml overrides live."""
    override = os.environ.get("ROUTE_CONFIG_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / APP_NAME


class JsonStorage:
    """File-backed counter storage implementing banditry's Storage protocol.

    One JSON file holds every bucket: ``{key: {arm: count}}``. The file is
    loaded lazily and rewritten atomically on every mutation.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else state_dir() / "bandits.json"
        self._data: dict[str, dict[str, int]] | None = None

    def _load(self) -> dict[str, dict[str, int]]:
        if self._data is None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            self._data = {
                str(k): {str(arm): int(c) for arm, c in v.items()}
                for k, v in raw.items()
                if isinstance(v, dict)
            }
        return self._data

    def _save(self) -> None:
        assert self._data is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".bandits-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- banditry Storage protocol ----------------------------------------

    def incr(self, key: str, arm: str, by: int = 1) -> None:
        data = self._load()
        bucket = data.setdefault(key, {})
        bucket[arm] = bucket.get(arm, 0) + by
        self._save()

    def counts(self, key: str) -> dict[str, int]:
        return dict(self._load().get(key, {}))

    # -- introspection for stats/federation --------------------------------

    def keys(self, prefix: str = "") -> list[str]:
        """All bucket keys, optionally filtered by prefix."""
        return sorted(k for k in self._load() if k.startswith(prefix))
