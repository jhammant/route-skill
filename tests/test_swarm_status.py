"""Read-only swarm discovery and concurrent status persistence."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from route import cli, swarm_status
from route.storage import atomic_write, state_dir
from route.swarm_status import SwarmStatus


def create_status(identity="test", units=("alpha",)):
    directory = state_dir() / "swarms" / identity
    directory.mkdir(parents=True, mode=0o700)
    return SwarmStatus(directory, name=identity, host="codex", repo="/test/repo",
        route_pid=os.getpid(), held=[], units=[{
            "id": unit, "arm": "codex", "why": "posterior", "shape": "coding:implement",
            "tier": "simple", "branch": f"swarm/{identity}/{unit}",
            "worktree": str(directory / "wt" / unit), "log": str(directory / "logs" / f"{unit}.log"),
        } for unit in units])


def snapshot(identity, age, *, state="done", pid=None):
    status = create_status(identity, units=("alpha", "beta"))
    row = json.loads(status.path.read_text())
    row["started_at"] = (datetime.now(timezone.utc) - age).isoformat().replace("+00:00", "Z")
    row["state"], row["route_pid"] = state, pid if pid is not None else os.getpid()
    row["units"][0]["state"] = "accepted"
    row["units"][1].update(arm="kimi", state="running")
    atomic_write(status.path, json.dumps(row))
    return row


def test_concurrent_worker_and_channel_updates_are_preserved():
    # Arrange
    identities = [f"unit-{i}" for i in range(8)]
    status = create_status(units=identities)

    def update_unit(identity):
        status.unit(identity, state="running", pid=os.getpid(), started_at=swarm_status.utc_now())
        status.unit(identity, state="checking", pid=None)
        status.unit(identity, state="accepted", agent_rc=0, wall_clock_s=1,
                    finished_at=swarm_status.utc_now())

    def update_channel():
        for messages in range(1, 21):
            status.channel(messages)

    # Act
    with ThreadPoolExecutor(max_workers=9) as executor:
        futures = [executor.submit(update_unit, identity) for identity in identities]
        futures.append(executor.submit(update_channel))
        for future in futures:
            future.result()
    status.finish("done", 0)

    # Assert
    row = json.loads(status.path.read_text())
    assert all(unit["state"] == "accepted" and unit["pid"] is None for unit in row["units"])
    assert row["channel"]["messages"] == 20
    assert (row["state"], row["exit_code"]) == ("done", 0)
    assert status.path.stat().st_mode & 0o777 == 0o600
    assert not list(status.path.parent.glob("*.tmp"))


@pytest.mark.parametrize("command", [cli.main, cli.main_auto])
def test_swarms_lists_newest_first_and_all_includes_old(command, capsys):
    # Arrange: names intentionally sort differently from start times.
    newest = snapshot("a-newest", timedelta(minutes=2))
    oldest = snapshot("m-oldest", timedelta(hours=25))
    recent = snapshot("z-recent", timedelta(hours=2))

    # Act / Assert
    assert command(["swarms", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [newest, recent]
    assert command(["swarms", "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [newest, recent, oldest]
    assert command(["swarms"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "swarm_id\tstate\tage\tunits"
    assert lines[1] == "a-newest\tdone\t2m\talpha codex ✓ · beta kimi running"
    assert lines[2] == "z-recent\tdone\t2h\talpha codex ✓ · beta kimi running"
    assert len(lines) == 3
    assert command(["swarms", "--all"]) == 0
    assert "m-oldest\tdone\t1d\t" in capsys.readouterr().out


@pytest.mark.parametrize("error,expected", [(ProcessLookupError, "stopped?"),
                                           (PermissionError, "running"), (None, "running")])
def test_swarms_checks_running_coordinator_pid(monkeypatch, capsys, error, expected):
    # Arrange
    row = snapshot("active", timedelta(seconds=10), state="running", pid=123456)
    calls = []

    def kill(pid, signal):
        calls.append((pid, signal))
        if error:
            raise error()

    monkeypatch.setattr(swarm_status.os, "kill", kill)

    # Act / Assert
    assert cli.main(["swarms"]) == 0
    assert f"active\t{expected}\t10s\t" in capsys.readouterr().out
    assert calls == [(123456, 0)]
    assert cli.main(["swarms", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [row]
    assert json.loads((state_dir() / "swarms/active/status.json").read_text()) == row


@pytest.mark.parametrize("pid", [None, 0, -1, True, "123", 10 ** 100])
def test_invalid_coordinator_pids_are_not_alive(pid):
    assert not swarm_status._pid_alive(pid)


def test_swarms_skips_unreadable_or_unknown_status_and_ignores_unknown_fields(capsys):
    # Arrange
    good = snapshot("good", timedelta(minutes=1))
    good["future_field"] = {"unknown": True}
    atomic_write(state_dir() / "swarms/good/status.json", json.dumps(good))
    for name, text in {
        "partial": '{"schema":', "list": "[]", "empty": "{}", "invalid-utf8": "\udcff",
        "future": json.dumps(dict(good, schema="route-swarm-status/2")),
        "bad-time": json.dumps(dict(good, started_at="yesterday")),
        "no-timezone": json.dumps(dict(good, started_at="2026-10-17T12:00:00")),
        "missing-field": json.dumps({key: value for key, value in good.items() if key != "units"}),
        "bad-units": json.dumps(dict(good, units=[{}])),
    }.items():
        path = state_dir() / "swarms" / name / "status.json"
        path.parent.mkdir()
        path.write_bytes(text.encode("utf-8", errors="surrogateescape"))

    # Act / Assert
    assert cli.main(["swarms", "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [good]


def test_swarms_without_state_is_read_only(capsys):
    # Act / Assert
    assert cli.main_auto(["swarms", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not state_dir().exists()


def test_status_names_the_launching_session_socket_but_never_its_token(monkeypatch):
    # Arrange
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/tmp/cc-socks/4242.sock")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "secret-token-value")
    # Act
    status = create_status("owned")
    # Assert
    text = status.path.read_text()
    assert json.loads(text)["owner_socket"] == "/tmp/cc-socks/4242.sock"
    assert "secret-token-value" not in text


def test_status_owner_socket_is_null_outside_claude_code(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)
    assert json.loads(create_status("unowned").path.read_text())["owner_socket"] is None
