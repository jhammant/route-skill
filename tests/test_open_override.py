"""--open: the user declares a task's data public, overriding the content safety net.

The keyword regex is a one-way safety net, so a brief that says "no customer data"
trips it and blocks --allow-network (Pocketpedia's reference-data spike, 1 Nov 2026).
--open is the explicit way out. It overrides only that keyword guess, never an explicit
signal (--sensitive, --private, a sensitive.toml path), and the plan records it.
Deterministic: subprocess.run is stubbed, nothing is dispatched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from route.cli import main_auto
from route.storage import config_dir

ARGS = ["--no-llm", "--no-quota", "--shape", "coding:implement", "--tier", "hard"]
FALSE_POSITIVE = "build a reference-data adapter from open datasets; no customer data involved"


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


def test_safety_net_still_blocks_network_without_open(monkeypatch, capsys):
    # Arrange
    calls = _stub_dispatch(monkeypatch)

    # Act
    code = main_auto([*ARGS, "--pool", "codex", "--allow-network", FALSE_POSITIVE])

    # Assert: unchanged behaviour, the keyword guess wins without an explicit override
    assert code == 2
    assert calls == []
    assert "cannot be combined with a sensitive task" in capsys.readouterr().err


def test_open_overrides_the_safety_net_and_records_it(monkeypatch, capsys):
    # Arrange
    calls = _stub_dispatch(monkeypatch)

    # Act
    code = main_auto([*ARGS, "--pool", "codex", "--allow-network", "--open", FALSE_POSITIVE])
    plan = _plan(capsys.readouterr().out)

    # Assert
    assert code == 0
    assert calls and calls[0][:4] == ["codex", "exec", "-c", "sandbox_workspace_write.network_access=true"]
    assert plan["data_class"] == "open"
    assert plan["data_class_override"] == {"flag": "--open", "content_safety_net_matched": True}


def test_open_clears_the_private_data_veto_when_not_pinned(monkeypatch, capsys):
    _stub_dispatch(monkeypatch)
    assert main_auto([*ARGS, "--plan-only", "--open", FALSE_POSITIVE]) == 0
    plan = _plan(capsys.readouterr().out)
    assert (plan.get("veto") or {}).get("name") != "private-data"


def test_open_on_a_clean_task_is_recorded_without_a_match(monkeypatch, capsys):
    _stub_dispatch(monkeypatch)
    assert main_auto([*ARGS, "--pool", "codex", "--plan-only", "--open", "write tests for the parser"]) == 0
    override = _plan(capsys.readouterr().out)["data_class_override"]
    assert override == {"flag": "--open", "content_safety_net_matched": False}


def test_open_contradicting_an_explicit_flag_fails_loudly(monkeypatch, capsys):
    calls = _stub_dispatch(monkeypatch)
    assert main_auto([*ARGS, "--open", "--sensitive", "anything"]) == 2
    assert main_auto([*ARGS, "--open", "--private", "anything"]) == 2
    assert calls == []
    assert "contradicts --sensitive/--private" in capsys.readouterr().err


def test_open_never_overrides_a_sensitive_toml_path(monkeypatch, capsys, tmp_path):
    # Arrange: the user's own config marks a path sensitive
    secret = tmp_path / "clients"
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "sensitive.toml").write_text(f'paths = ["{secret}"]\n')
    calls = _stub_dispatch(monkeypatch)

    # Act
    code = main_auto([*ARGS, "--pool", "codex", "--open", f"refactor {secret}/acme/report.py"])

    # Assert
    assert code == 2
    assert calls == []
    assert "cannot override a path listed in sensitive.toml" in capsys.readouterr().err
