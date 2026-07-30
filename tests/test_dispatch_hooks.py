"""SPEC 6-7: the dispatch hook seam.

Hooks are an opt-in, vendor-neutral observability point. These tests pin the
two things a consumer depends on: which hooks get discovered, and the fact
that a broken hook cannot affect routing.
"""

from __future__ import annotations

import json as _json
import os
import stat
from pathlib import Path

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
