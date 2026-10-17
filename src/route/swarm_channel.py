"""Per-swarm a-mail identities. Tokens never leave this adapter."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SERVER = "https://amail.hammantlabs.com"


class ChannelError(RuntimeError):
    """A sanitized error: subprocess output can contain redemption tokens."""


def amail_binary() -> str | None:
    binary = shutil.which("amail")
    fallback = Path.home() / ".local/bin/amail"
    return binary or (str(fallback) if fallback.is_file() else None)


def _run(binary: str, args: list[str], *, env=None, timeout=30, json_output=True):
    try:
        proc = subprocess.run([binary, *args], env=dict(os.environ, **(env or {})),
                              capture_output=True, text=True, timeout=timeout, umask=0o077)
    except (OSError, subprocess.TimeoutExpired):
        raise ChannelError(f"a-mail {args[0]} could not complete") from None
    if proc.returncode:
        raise ChannelError(f"a-mail {args[0]} failed (exit {proc.returncode})")
    if not json_output:
        return proc.stdout
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        raise ChannelError(f"a-mail {args[0]} returned invalid JSON") from None


@dataclass(frozen=True)
class Worker:
    address: str
    credentials_path: Path

    def environment(self) -> dict[str, str]:
        try:
            credentials = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            server = credentials.get("server") or DEFAULT_SERVER
        except (OSError, ValueError, AttributeError):
            server = DEFAULT_SERVER
        return {"AMAIL_CREDENTIALS": str(self.credentials_path),
                "AMAIL_SERVER": os.environ.get("AMAIL_SERVER", server),
                "AMAIL_PROFILE": self.address}


class Channel:
    def __init__(self, binary: str, workers: dict[str, Worker], hub: Worker,
                 group_id: str | None = None):
        self.binary = binary
        self.workers = workers
        self.hub = hub
        self.group_id = group_id
        self._closed = False

    def inbox(self, wait: int) -> list[dict]:
        result = _run(self.binary, ["inbox", "--wait", str(wait), "--json"],
                      env=self.hub.environment(), timeout=wait + 10)
        if not isinstance(result, list):
            raise ChannelError("a-mail inbox returned a non-list response")
        return result

    def close(self) -> None:
        """Direct sub-agents expire by TTL; groups also override this to end."""


class DirectChannel(Channel):
    pass


class GroupChannel(Channel):
    def close(self) -> None:
        if self.group_id is None or self._closed:
            return
        self._closed = True
        try:
            _run(self.binary, ["swarm", "end", "--group", self.group_id], json_output=False)
        except ChannelError as exc:
            print(f"channel: {exc}", file=sys.stderr)


def _group_workers(payload, names: list[str], directory: Path) -> dict[str, Worker]:
    workers = {}
    for row in payload["workers"]:
        name, address = row["name"], row["address"]
        if name not in names or name in workers or not isinstance(address, str) or not address:
            raise ValueError("invalid worker")
        path = Path(row["credentials"])
        if not path.is_absolute():
            path = directory / path
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("credentials outside swarm directory")
        if not path.is_file():
            raise ValueError("missing credentials")
        path.chmod(0o600)
        workers[name] = Worker(address, path)
    if set(workers) != set(names):
        raise ValueError("missing workers")
    return workers


def open_channel(name_slug: str, worker_names: list[str], ttl: int, creds_dir: Path) -> Channel:
    """Create identities as the anchor; redeem before starting any delegate."""
    binary = amail_binary()
    if binary is None:
        raise ChannelError("amail binary missing")
    creds_dir = Path(creds_dir).resolve()
    creds_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    creds_dir.chmod(0o700)
    names = list(dict.fromkeys([*worker_names, "hub"]))
    help_text = _run(binary, ["--help"], json_output=False)
    if "amail swarm create" in help_text:
        payload = _run(binary, ["swarm", "create", "--name", name_slug,
                                "--workers", ",".join(names), "--ttl", str(ttl),
                                "--dir", str(creds_dir), "--json"])
        group_id = payload.get("group_id") if isinstance(payload, dict) else None
        if not isinstance(group_id, str) or not group_id:
            raise ChannelError("a-mail swarm create returned no group ID")
        channel = GroupChannel(binary, {}, Worker("", creds_dir / "hub.json"), group_id)
        try:
            workers = _group_workers(payload, names, creds_dir)
            channel.hub = workers.pop("hub")
            channel.workers = workers
            return channel
        except BaseException as exc:
            channel.close()
            if isinstance(exc, (KeyError, ValueError, TypeError, OSError)):
                raise ChannelError("a-mail swarm create returned invalid workers") from None
            raise

    workers = {}
    for name in names:
        payload = _run(binary, ["spawn", "--name", f"swarm-{name_slug}-{name}", "--ttl", str(ttl)])
        try:
            address, token = payload["address"], payload["subagent_token"]
            if not all(isinstance(value, str) and value for value in (address, token)):
                raise ValueError("missing identity")
            path = creds_dir / f"{name}.json"
            if path.exists():
                raise ValueError("credentials already exist")
            _run(binary, ["redeem", "--token", token],
                 env={"AMAIL_CREDENTIALS": str(path)}, json_output=False)
            path.chmod(0o600)
            workers[name] = Worker(address, path)
        except (KeyError, ValueError, TypeError, OSError):
            raise ChannelError(f"a-mail could not provision worker {name}") from None
    hub = workers.pop("hub")
    return DirectChannel(binary, workers, hub)


def mcp_runtime() -> tuple[str, str]:
    node = shutil.which("node")
    if node is None:
        raise ChannelError("node is required for the a-mail MCP bridge")
    home = Path(os.environ.get("AMAIL_HOME", str(Path.home() / ".local/share/amail")))
    return node, str(home / "amail-mcp.mjs")


class HubPoller:
    """Read only the hub inbox, never the user's anchor inbox."""

    def __init__(self, channel: Channel, path: Path):
        self.channel = channel
        self.path = path
        self.count = 0
        self._stop = threading.Event()
        self._warned = False
        self._names = {worker.address: name for name, worker in channel.workers.items()}
        self._thread = threading.Thread(target=self._loop, name="swarm-hub", daemon=True)

    def start(self) -> None:
        self.path.touch(mode=0o600, exist_ok=True)
        self._thread.start()

    def _poll(self, wait: int) -> None:
        try:
            for item in self.channel.inbox(wait):
                if not isinstance(item, dict) or item.get("kind") != "message":
                    continue
                message = item["message"]
                address = message["from"]
                text = message["parts"][0]["text"]
                row = {"at": message.get("created_at"), "from": address,
                       "from_unit": self._names.get(address), "text": text}
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row) + "\n")
                self.count += 1
                one_line = " ".join(text.split())[:200]
                print(f"[channel] {row['from_unit'] or address} → hub: {one_line}", flush=True)
        except (ChannelError, OSError, KeyError, IndexError, TypeError, AttributeError):
            if not self._warned:
                print("channel: hub poll failed; continuing the swarm", file=sys.stderr)
                self._warned = True

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._poll(15)
            # Back off even when a broken CLI returns immediately.
            self._stop.wait(0.1)
        self._poll(3)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()
