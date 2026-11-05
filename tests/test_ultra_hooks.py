"""The hooks ``route ultra`` drives swarms through: a preset swarm ID, the
programmatic runner, the worker marker in every delegated environment, and a
claude-work session gating the host arm on the account it really spends."""

from __future__ import annotations

import json
import os
import subprocess
import types
from pathlib import Path

import pytest

from route import cli, eligibility, swarm_dispatch
from route.eligibility import fetch_quota, gate, quota_provider
from route.pools import load_pools
from route.storage import state_dir
from route.swarm_dispatch import load_manifest, run_swarm_manifest, swarm_dir
from tests.test_swarm_dispatch import manifest_file, offline, repo, summary, unit  # noqa: F401

PRESET = "ultra-20261105152507"


def fake_agent(tmp_path, name="codex"):
    """A delegate on a private PATH that records the ROUTE_* it was given and leaves a result."""
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir(exist_ok=True)
    script = binary_dir / name
    script.write_text("#!/bin/sh\nenv | grep -E '^ROUTE_(HOST|WORKER)=' | sort > route-env.txt\n"
                      "echo done > result.txt\n")
    script.chmod(0o700)
    return binary_dir


def route_env(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


# -- ROUTE_WORKER in every delegated process -----------------------------------

def test_dispatch_marks_the_child_as_a_route_worker(tmp_path, monkeypatch):
    # Arrange: a real shell script that prints its environment into a file.
    binary_dir = fake_agent(tmp_path)
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}{os.environ['PATH']}")
    cwd = tmp_path / "work"
    cwd.mkdir()
    # Act
    rc = cli._dispatch("codex exec {task}", "do the thing", target="codex", cwd=cwd,
                       log=tmp_path / "dispatch.log")
    # Assert
    assert rc == 0
    assert route_env(cwd / "route-env.txt") == {"ROUTE_HOST": "codex", "ROUTE_WORKER": "1"}


@pytest.mark.parametrize("with_worker", [False, True])
def test_swarm_dispatch_env_marks_the_worker(tmp_path, with_worker):
    # Arrange: a kimi assignment (no MCP wiring), with or without an a-mail identity.
    ctx = types.SimpleNamespace(template="kimi run {task}", pools=load_pools())
    assignment = swarm_dispatch.Assignment(unit("one"), "task", {"chosen": "kimi"}, ctx)
    worker = types.SimpleNamespace(environment=lambda: {"AMAIL_CREDENTIALS": "/c", "AMAIL_SERVER": "s"}) \
        if with_worker else None
    # Act
    argv, env = swarm_dispatch.dispatch_command(assignment, worker, tmp_path, allow_network=False)
    # Assert
    assert argv == ["kimi", "run", "{task}"]
    assert env["ROUTE_WORKER"] == "1"
    assert ("AMAIL_SERVER" in env) == with_worker


# -- preset swarm_id and swarm_dir --------------------------------------------

def test_swarm_dir_lives_under_the_state_dir():
    assert swarm_dir(PRESET) == (state_dir() / "swarms" / PRESET).resolve()


@pytest.mark.parametrize("preset", ["", None])
def test_blank_preset_swarm_id_gets_a_generated_one(repo, tmp_path, preset):  # noqa: F811
    manifest = load_manifest(manifest_file(repo, tmp_path, [unit("one")], swarm_id=preset))
    assert swarm_dispatch._swarm_id(manifest).startswith("parser-rewrite-20")


@pytest.mark.parametrize("preset", ["Ultra-1", "ultra/1", "-ultra", "a" * 82, 7])
def test_invalid_preset_swarm_id_is_refused(repo, tmp_path, preset):  # noqa: F811
    with pytest.raises(ValueError, match="swarm_id"):
        load_manifest(manifest_file(repo, tmp_path, [unit("one")], swarm_id=preset))


def test_cli_swarm_honours_a_preset_swarm_id(repo, tmp_path, offline):  # noqa: F811
    # Arrange
    path = manifest_file(repo, tmp_path, [unit("one", pool="codex")], swarm_id=PRESET)
    # Act
    rc = cli.main_auto(["swarm", str(path), "--host", "codex", "--no-amail", "--no-quota"])
    # Assert: the run, its branch and its directory all carry the preset ID.
    assert rc == 0
    result, directory = summary()
    assert result["swarm_id"] == PRESET and directory == swarm_dir(PRESET)
    assert result["units"][0]["branch"] == f"swarm/{PRESET}/one"


def test_load_manifest_copies_a_dict_and_leaves_the_caller_untouched(repo):  # noqa: F811
    source = dict(name="Parser rewrite", repo=str(repo), units=[unit("one")])
    manifest = load_manifest(source)
    assert manifest["slug"] == "parser-rewrite" and "slug" not in source


# -- run_swarm_manifest -------------------------------------------------------

def test_run_swarm_manifest_runs_real_units_and_returns_the_summary(repo, tmp_path, monkeypatch, capsys):  # noqa: F811
    # Arrange: two pinned units whose fake delegate leaves result.txt; only one check looks for it.
    binary_dir = fake_agent(tmp_path)
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("the API must never prompt"))
    manifest = dict(name="Parser rewrite", repo=str(repo), swarm_id=PRESET, units=[
        unit("passes", pool="codex"), {**unit("fails", pool="codex"), "check": "test -f missing.txt"}])
    # Act
    result = run_swarm_manifest(manifest, host="codex", no_amail=True, no_quota=True)
    # Assert: the dict is what summary.json holds, and the API prints no JSON of its own.
    assert result == json.loads((swarm_dir(PRESET) / "summary.json").read_text())
    assert result["swarm_id"] == PRESET and result["held"] == []
    assert {row["id"]: row["accepted"] for row in result["units"]} == {"passes": True, "fails": False}
    assert all(row["arm"] == "codex" and row["committed"] for row in result["units"])
    assert {row["id"]: row["branch"] for row in result["units"]} == {
        "passes": f"swarm/{PRESET}/passes", "fails": f"swarm/{PRESET}/fails"}
    assert [(row["arm"], row["total"], row["accepted"]) for row in result["outcomes"]] == [("codex", 2, 1)]
    for row in result["units"]:
        assert route_env(Path(row["worktree"]) / "route-env.txt") == {"ROUTE_HOST": "codex", "ROUTE_WORKER": "1"}
    status = json.loads((swarm_dir(PRESET) / "status.json").read_text())
    assert (status["state"], status["exit_code"]) == ("failed", 1)
    assert not capsys.readouterr().out.lstrip().startswith("{")


def test_run_swarm_manifest_raises_with_the_reason(repo, tmp_path, offline, monkeypatch):  # noqa: F811
    # Arrange: an invalid manifest, then a run whose worktree setup fails.
    with pytest.raises(RuntimeError, match="unit id must match"):
        run_swarm_manifest(dict(name="x", repo=str(repo), units=[unit("Bad")]), no_quota=True)
    original = swarm_dispatch._git

    def git(repo_path, *args):
        if args[:2] == ("worktree", "add"):
            return subprocess.CompletedProcess(args, 128, "", "fatal: worktree refused")
        return original(repo_path, *args)

    monkeypatch.setattr(swarm_dispatch, "_git", git)
    # Act / Assert
    with pytest.raises(RuntimeError, match="produced no summary"):
        run_swarm_manifest(dict(name="x", repo=str(repo), swarm_id=PRESET, units=[unit("one", pool="codex")]),
                           host="codex", no_amail=True, no_quota=True)
    status = json.loads((swarm_dir(PRESET) / "status.json").read_text())
    assert (status["state"], status["exit_code"]) == ("failed", 1)


def test_rerunning_a_preset_swarm_id_is_refused(repo, tmp_path, offline):  # noqa: F811
    manifest = dict(name="x", repo=str(repo), swarm_id=PRESET, units=[unit("one", pool="codex")])
    run_swarm_manifest(manifest, host="codex", no_amail=True, no_quota=True)
    with pytest.raises(RuntimeError):
        run_swarm_manifest(manifest, host="codex", no_amail=True, no_quota=True)


# -- CLAUDE_WORK_ACCOUNT=1 ----------------------------------------------------

def quota_probe(monkeypatch, calls):
    def fake_run_json(cmd, timeout):
        calls.append(cmd)
        return {"ok": True, "headroom": "comfortable"} if cmd[1] == "agent" else []
    monkeypatch.setattr(eligibility, "_run_json", fake_run_json)
    monkeypatch.setattr(eligibility, "deepseek_headroom", lambda *a, **k: "critical")


def test_claude_work_session_reads_but_never_writes_the_quota_cache(monkeypatch):
    # Arrange
    calls = []
    quota_probe(monkeypatch, calls)
    cache = state_dir() / "quota-cache.json"
    monkeypatch.setenv("CLAUDE_WORK_ACCOUNT", "1")
    # Act: probe twice as a worker; nothing is cached, so both probe.
    fetch_quota()
    fetch_quota()
    assert not cache.exists() and len(calls) == 4
    # Act: a cache the host wrote is still honoured by the worker.
    monkeypatch.delenv("CLAUDE_WORK_ACCOUNT")
    fetch_quota()
    assert cache.exists() and len(calls) == 6
    monkeypatch.setenv("CLAUDE_WORK_ACCOUNT", "1")
    fetch_quota()
    # Assert
    assert len(calls) == 6


def test_claude_work_session_gates_the_host_arm_on_claude_work(monkeypatch, pools):
    # Arrange
    quota = {"claude": "ok", "claude-work": "critical", "codex": "ok"}
    assert quota_provider("claude", pools) == "claude"
    monkeypatch.setenv("CLAUDE_WORK_ACCOUNT", "1")
    # Act
    result = gate("build an endpoint", "coding:implement", pools, quota=quota)
    # Assert: the host arm spends the second account, so its spent week removes it.
    assert quota_provider("claude", pools) == "claude-work"
    assert quota_provider("codex", pools) == "codex"
    assert "claude" in result.removed_by_quota and "codex" in result.eligible
