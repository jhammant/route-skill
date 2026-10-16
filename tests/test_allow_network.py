"""--allow-network: a codex run may use the network for that one run only.

Codex's workspace-write sandbox has the network off, local sockets included, so a
delegated job whose tests open a port fails there (loom's wake protocols, 17 Oct
2026). Deterministic: subprocess.run is stubbed, nothing is dispatched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from route.cli import CODEX_NETWORK, main_auto, with_network

ARGS = ["--no-llm", "--no-quota", "--shape", "coding:test", "--tier", "hard"]


def _stub_dispatch(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("route.cli.subprocess.run", fake_run)
    monkeypatch.setattr("route.cli.fetch_quota", lambda *a, **k: {})
    return calls


def _plan(out: str) -> dict:
    start = out.index("{")
    return json.loads(out[start:out.index("\n}\n", start) + 2])


def test_with_network_adds_the_override_once_and_only_to_codex():
    template = "codex exec --sandbox workspace-write {task}"
    once = with_network(template)
    assert once == f"codex exec {CODEX_NETWORK} --sandbox workspace-write {{task}}"
    assert with_network(once) == once
    assert with_network("kimi -p {task}") == "kimi -p {task}"


def test_codex_run_gets_network_only_when_asked(monkeypatch, capsys):
    # Arrange
    calls = _stub_dispatch(monkeypatch)

    # Act
    assert main_auto([*ARGS, "--pool", "codex", "write tests for the parser"]) == 0
    assert main_auto([*ARGS, "--pool", "codex", "--allow-network", "write socket tests for the waker"]) == 0
    out = capsys.readouterr().out

    # Assert: the first run is sandboxed as configured; the second carries the override
    plain, networked = calls
    assert "sandbox_workspace_write.network_access=true" not in plain
    assert networked[:4] == ["codex", "exec", "-c", "sandbox_workspace_write.network_access=true"]
    assert "--sandbox" in networked and "workspace-write" in networked
    assert "network allowed for this codex run" in out


def test_plan_records_the_network_grant(monkeypatch, capsys):
    _stub_dispatch(monkeypatch)
    assert main_auto([*ARGS, "--pool", "codex", "--allow-network", "--plan-only", "socket tests"]) == 0
    assert _plan(capsys.readouterr().out)["allow_network"] is True


def test_sensitive_task_refuses_the_flag(monkeypatch, capsys):
    calls = _stub_dispatch(monkeypatch)
    assert main_auto([*ARGS, "--sensitive", "--allow-network", "anything"]) == 2
    assert calls == []
    assert "cannot be combined with a sensitive task" in capsys.readouterr().err


def test_other_pools_ignore_the_flag_and_say_so(monkeypatch, capsys):
    calls = _stub_dispatch(monkeypatch)
    assert main_auto([*ARGS, "--pool", "kimi", "--allow-network", "write tests for the parser"]) == 0
    assert calls and "sandbox_workspace_write.network_access=true" not in calls[0]
    assert "only applies to codex" in capsys.readouterr().out
