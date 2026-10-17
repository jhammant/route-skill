"""Atomic live status for route swarms (route-swarm-status/1)."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .storage import atomic_write, state_dir

SCHEMA = "route-swarm-status/1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SwarmStatus:
    """Serialize worker and hub updates without exposing channel credentials."""

    def __init__(self, directory: Path, *, name: str, host: str, repo: str,
                 route_pid: int, units: list[dict], held: list[dict]):
        self.path = directory / "status.json"
        self._lock = threading.Lock()
        self._status = {
            "schema": SCHEMA, "swarm_id": directory.name, "name": name,
            "state": "running", "exit_code": None, "host": host, "repo": repo,
            "route_pid": route_pid, "started_at": utc_now(), "updated_at": None,
            # The launching Claude Code session's socket (never its token), so a
            # watcher can name the owner after route itself has exited.
            "owner_socket": os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET") or None,
            "finished_at": None, "hub": None, "group_id": None,
            "channel": {"messages": 0, "log": str(directory / "channel.jsonl")},
            "held": [{"id": row["id"], "reason": row["reason"]} for row in held],
            "units": [{**{key: row[key] for key in (
                "id", "arm", "why", "shape", "tier", "branch", "worktree", "log")},
                "state": "queued", "pid": None, "address": None,
                "started_at": None, "finished_at": None, "agent_rc": None,
                "wall_clock_s": None} for row in units],
        }
        with self._lock:
            self._write()

    def _write(self) -> None:
        self._status["updated_at"] = utc_now()
        # atomic_write uses mkstemp (0600) and replaces the destination.
        atomic_write(self.path, json.dumps(self._status, indent=2) + "\n")

    def unit(self, identity: str, **fields) -> None:
        with self._lock:
            for row in self._status["units"]:
                if row["id"] == identity:
                    row.update(fields)
                    self._write()
                    return
            raise KeyError(f"unknown swarm unit: {identity}")

    def connect(self, hub: str, group_id: str | None) -> None:
        with self._lock:
            self._status.update(hub=hub, group_id=group_id)
            self._write()

    def channel(self, messages: int) -> None:
        with self._lock:
            self._status["channel"]["messages"] = messages
            self._write()

    def finish(self, state: str, exit_code: int) -> None:
        with self._lock:
            self._status.update(state=state, exit_code=exit_code, finished_at=utc_now())
            self._write()


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for size, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{suffix}"
    return f"{seconds}s"


def list_swarms(*, include_all: bool = False, json_output: bool = False) -> int:
    """List readable snapshots by start time; JSON preserves the on-disk schema."""
    now = datetime.now(timezone.utc)
    rows = []
    for path in (state_dir() / "swarms").glob("*/status.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(row, dict) or row.get("schema") != SCHEMA:
                continue
            started = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
            if started.tzinfo is None:
                continue
            if not include_all and now - started > timedelta(hours=24):
                continue
            if (not isinstance(row["swarm_id"], str)
                    or row["state"] not in ("running", "done", "failed", "interrupted")
                    or not isinstance(row["units"], list)
                    or not all(isinstance(unit, dict) and all(isinstance(unit[key], str)
                               for key in ("id", "arm", "state")) for unit in row["units"])):
                continue
            rows.append((started, row))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    rows.sort(key=lambda item: (item[0], item[1]["swarm_id"]), reverse=True)
    if json_output:
        print(json.dumps([row for _, row in rows], indent=2))
        return 0
    print("swarm_id\tstate\tage\tunits")
    for started, row in rows:
        state = row["state"]
        if state == "running" and not _pid_alive(row.get("route_pid")):
            state = "stopped?"
        units = " · ".join(
            f"{unit['id']} {unit['arm']} " + {"accepted": "✓", "failed": "✗"}.get(unit["state"], unit["state"])
            for unit in row["units"])
        print(f"{row['swarm_id']}\t{state}\t{_age((now - started).total_seconds())}\t{units}")
    return 0
