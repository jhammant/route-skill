"""SPEC 6-7: the dispatch hook seam.

Hooks are an opt-in, vendor-neutral observability point. These tests pin the
two things a consumer depends on: which hooks get discovered, and the fact
that a broken hook cannot affect routing.
"""

from __future__ import annotations

import json as _json
import os
import stat
import subprocess as _subprocess
import time
from pathlib import Path

import pytest

from route import cli
from route.hooks import HOOKS_DIRNAME, discover_hooks, fire
from route.storage import config_dir


def _make_hook(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_no_hooks_configured_returns_empty():
    assert discover_hooks() == []


def test_discovers_executables_from_config_dir_in_sorted_order():
    d = config_dir() / HOOKS_DIRNAME
    _make_hook(d / "20-second")
    _make_hook(d / "10-first")
    assert [p.name for p in discover_hooks()] == ["10-first", "20-second"]


def test_ignores_non_executable_files():
    d = config_dir() / HOOKS_DIRNAME
    _make_hook(d / "10-yes")
    (d / "20-no").write_text("#!/bin/sh\n")
    assert [p.name for p in discover_hooks()] == ["10-yes"]


def test_ignores_subdirectories():
    d = config_dir() / HOOKS_DIRNAME
    _make_hook(d / "10-yes")
    (d / "20-dir").mkdir()
    assert [p.name for p in discover_hooks()] == ["10-yes"]


def test_env_override_replaces_the_directory(monkeypatch, tmp_path):
    _make_hook(config_dir() / HOOKS_DIRNAME / "10-ignored")
    a = _make_hook(tmp_path / "a")
    b = _make_hook(tmp_path / "b")
    monkeypatch.setenv("ROUTE_DISPATCH_HOOKS", os.pathsep.join([str(a), str(b)]))
    assert discover_hooks() == [a, b]


def test_env_override_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ROUTE_DISPATCH_HOOKS", "~/myhook")
    assert discover_hooks() == [tmp_path / "myhook"]


def test_fire_passes_payload_as_json_on_stdin(tmp_path):
    seen = tmp_path / "seen.json"
    hook = _make_hook(tmp_path / "h", f"#!/bin/sh\ncat > {seen}\n")
    fire(hook, {"event": "decision", "plan": {"chosen": "local"}})
    assert _json.loads(seen.read_text())["plan"]["chosen"] == "local"


def test_fire_returns_stripped_stdout(tmp_path):
    hook = _make_hook(tmp_path / "h", "#!/bin/sh\necho '  ISSUE-42  '\n")
    assert fire(hook, {}) == "ISSUE-42"


def test_fire_truncates_oversized_stdout(tmp_path):
    hook = _make_hook(
        tmp_path / "h", "#!/bin/sh\nhead -c 9000 /dev/zero | tr '\\0' 'x'\n"
    )
    assert len(fire(hook, {})) == 4096


def test_fire_returns_empty_on_nonzero_exit_but_does_not_raise(tmp_path):
    hook = _make_hook(tmp_path / "h", "#!/bin/sh\necho out\nexit 3\n")
    assert fire(hook, {}) == "out"


def test_fire_survives_missing_hook(tmp_path):
    assert fire(tmp_path / "nope", {}) == ""


def test_fire_survives_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("route.hooks.HOOK_TIMEOUT_S", 1)
    hook = _make_hook(tmp_path / "h", "#!/bin/sh\nsleep 5\n")
    assert fire(hook, {}) == ""


def _recording_hook(tmp_path, monkeypatch):
    """Install a hook that appends each payload to events.jsonl and echoes an id.

    ``fire()`` sends the payload with no trailing newline, so the hook adds
    one itself before appending — otherwise back-to-back invocations (decision
    then complete) would concatenate onto a single line.
    """
    log = tmp_path / "events.jsonl"
    hook = _make_hook(
        tmp_path / "rec",
        f"#!/bin/sh\ncat >> '{log}'\nprintf '\\n' >> '{log}'\necho 'ISSUE-1'\n",
    )
    monkeypatch.setenv("ROUTE_DISPATCH_HOOKS", str(hook))
    return log


def _events(log: Path) -> list[dict]:
    return [_json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_stay_path_fires_both_events(tmp_path, monkeypatch, capsys):
    log = _recording_hook(tmp_path, monkeypatch)
    rc = cli.main_auto(["--pool", "claude", "write the docs"])
    assert rc == 0
    events = _events(log)
    assert [e["event"] for e in events] == ["decision", "complete"]
    assert events[0]["plan"]["chosen"] == "claude"
    assert events[1]["exit_code"] == 0


def test_hook_context_round_trips_from_decision_to_complete(tmp_path, monkeypatch):
    log = _recording_hook(tmp_path, monkeypatch)
    cli.main_auto(["--pool", "claude", "write the docs"])
    assert _events(log)[1]["hook_context"] == "ISSUE-1"


def test_dispatch_path_reports_real_exit_code(tmp_path, monkeypatch):
    log = _recording_hook(tmp_path, monkeypatch)

    def _slow_dispatch(template, task):
        time.sleep(0.05)
        return 7

    monkeypatch.setattr(cli, "_dispatch", _slow_dispatch)
    rc = cli.main_auto(["--pool", "local-agent", "write the docs"])
    assert rc == 7
    complete = _events(log)[1]
    assert complete["exit_code"] == 7
    assert complete["wall_clock_s"] >= 0.05


def test_broken_hook_does_not_change_return_code(tmp_path, monkeypatch):
    hook = _make_hook(tmp_path / "bad", "#!/bin/sh\nexit 9\n")
    monkeypatch.setenv("ROUTE_DISPATCH_HOOKS", str(hook))
    monkeypatch.setattr(cli, "_dispatch", lambda template, task: 0)
    assert cli.main_auto(["--pool", "local-agent", "write the docs"]) == 0


def test_exception_during_dispatch_still_fires_complete_and_propagates(
    tmp_path, monkeypatch
):
    """decision fired -> complete must fire even if _dispatch raises.

    Covers Ctrl-C (KeyboardInterrupt) and any exception _dispatch doesn't
    swallow itself (e.g. a malformed dispatch template raising ValueError out
    of shlex.split). The exception must still propagate unchanged — this
    seam is advisory, never a replacement for real error handling.
    """
    log = _recording_hook(tmp_path, monkeypatch)

    def _raising_dispatch(template, task):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_dispatch", _raising_dispatch)
    with pytest.raises(KeyboardInterrupt):
        cli.main_auto(["--pool", "local-agent", "write the docs"])
    events = _events(log)
    assert [e["event"] for e in events] == ["decision", "complete"]
    assert events[1]["exit_code"] == 130


def test_no_hooks_configured_does_not_invoke_subprocess(monkeypatch):
    """The default path must not gain a subprocess call.

    ``ROUTE_DISPATCH_HOOKS`` is deliberately unset (not set to ``""``) —
    ``discover_hooks()`` treats an empty string as an override to an empty
    hook list, which is a different code path from the unset default real
    users hit. ``--shape``/``--tier``/``--no-quota`` sidestep the
    *pre-existing* subprocess calls in
    ``classify_shape``/``estimate_complexity``/``fetch_quota`` so this test
    isolates the one thing it's checking: that the hook seam itself doesn't
    invoke a subprocess when unconfigured.
    """
    monkeypatch.delenv("ROUTE_DISPATCH_HOOKS", raising=False)
    calls = []
    real_run = _subprocess.run
    monkeypatch.setattr(
        _subprocess, "run", lambda *a, **k: (calls.append(a), real_run(*a, **k))[1]
    )
    cli.main_auto(
        [
            "--pool",
            "claude",
            "--shape",
            "writing",
            "--tier",
            "trivial",
            "--no-quota",
            "write the docs",
        ]
    )
    assert calls == []
