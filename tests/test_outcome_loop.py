"""The outcome loop closes itself: dispatch exit codes become evidence.

Before this, 12 of 185 real decisions ever received an outcome. Deterministic:
subprocess.run is stubbed, nothing is dispatched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from route.cli import main_auto, main
from route.router import Router, cell_name
from route.stats import Stats
from route.storage import JsonStorage
from route.storage import state_dir


def _stub_dispatch(monkeypatch, rc: int):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=rc)

    monkeypatch.setattr("route.cli.subprocess.run", fake_run)
    monkeypatch.setattr("route.cli.fetch_quota", lambda *a, **k: {})
    return calls


def _counts(cell: str, event: str) -> dict:
    return JsonStorage(state_dir() / "bandits.json").counts(f"{cell}:{event}")


def _last_decision() -> dict:
    return Stats().decisions()[-1]


ARGS = ["--no-llm", "--no-quota", "--shape", "coding:test", "--tier", "hard", "--pool", "codex"]


def test_successful_dispatch_records_completed(monkeypatch, capsys):
    calls = _stub_dispatch(monkeypatch, 0)
    assert main_auto([*ARGS, "write tests for the parser"]) == 0
    assert calls and calls[0][:2] == ["codex", "exec"]
    cell = cell_name("coding:test", "hard")
    assert _counts(cell, "completed") == {"codex": 1}
    assert "accepted" not in _counts(cell, "accepted") or _counts(cell, "accepted")["codex"] == 7  # seed only
    assert _last_decision()["outcome_events"] == ["completed"]
    assert "route outcome --shape coding:test --tier hard --arm codex --outcome accepted" in capsys.readouterr().out


def test_failed_dispatch_records_failed_and_no_reward(monkeypatch, capsys):
    _stub_dispatch(monkeypatch, 3)
    assert main_auto([*ARGS, "write tests for the parser"]) == 3
    cell = cell_name("coding:test", "hard")
    assert _counts(cell, "completed") == {}
    assert _last_decision()["outcome_events"] == ["failed"]
    assert "outcome: failed (exit 3" in capsys.readouterr().out


def test_pinned_dispatch_is_a_seeded_trial(monkeypatch):
    """A --pool pin never ran select(); the trial must still be evidence."""
    _stub_dispatch(monkeypatch, 0)
    main_auto([*ARGS, "write tests for the parser"])
    router = Router(storage=JsonStorage(state_dir() / "bandits.json"))
    post = router.posterior("coding:test", "hard")
    assert post["codex"] == (8.0, 3.0)   # prior Beta(8,2) + one trial, not yet accepted
    assert post["claude"] == (5.0, 5.0)  # the rest of the cell was seeded too
    assert router.observed_counts("coding:test", "hard") == {"codex": (0, 1.0)}


def test_accepted_after_auto_completed_does_not_double_count(monkeypatch, capsys):
    _stub_dispatch(monkeypatch, 0)
    main_auto([*ARGS, "write tests for the parser"])
    capsys.readouterr()
    assert main(["outcome", "--shape", "coding:test", "--tier", "hard",
                 "--arm", "codex", "--outcome", "accepted"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["already"] == ["completed"]
    assert report["events"] == ["verified", "accepted"]
    cell = cell_name("coding:test", "hard")
    assert _counts(cell, "completed") == {"codex": 1}
    assert _counts(cell, "verified") == {"codex": 1}
    router = Router(storage=JsonStorage(state_dir() / "bandits.json"))
    assert router.observed_counts("coding:test", "hard") == {"codex": (1.0, 0)}
    assert sorted(_last_decision()["outcome_events"]) == ["accepted", "completed", "verified"]


def test_manual_outcome_without_a_dispatch_still_records(capsys):
    """route outcome on its own (a task run by hand) behaves as before."""
    assert main(["outcome", "--shape", "coding:debug", "--tier", "simple",
                 "--arm", "kimi", "--outcome", "verified"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["events"] == ["completed", "verified"] and report["already"] == []
    cell = cell_name("coding:debug", "simple")
    assert _counts(cell, "completed") == {"kimi": 1}


def test_manual_failed_counts_the_trial(capsys):
    main(["outcome", "--shape", "coding:debug", "--tier", "simple",
          "--arm", "kimi", "--outcome", "failed"])
    assert _counts(cell_name("coding:debug", "simple"), "impression") == {"kimi": 1}
