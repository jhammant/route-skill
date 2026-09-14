"""Quota cost per decision: usage snapshots either side of a dispatch."""

from __future__ import annotations

import json
from types import SimpleNamespace

from route.cli import main, main_auto
from route.eligibility import parse_usage
from route.stats import Stats, report_quota

AGENT = {"ok": True, "session": {"percentUsed": 8}, "weekly": {"percentUsed": 14},
         "headroom": "comfortable"}
PROVIDERS = [
    {"id": "codex", "ok": True, "limits": [{"label": "weekly", "percent": 2}]},
    {"id": "kimi", "ok": False},
]


def test_parse_usage_keeps_raw_percentages():
    assert parse_usage(AGENT, PROVIDERS) == {
        "claude": {"session": 8.0, "weekly": 14.0},
        "codex": {"weekly": 2.0},
    }
    assert parse_usage(None, None) == {}


def _stub(monkeypatch, usages, rc=0):
    seq = iter(usages)
    monkeypatch.setattr("route.cli.fetch_usage", lambda *a, **k: next(seq))
    monkeypatch.setattr("route.cli.fetch_quota", lambda *a, **k: {"codex": "ok"})
    monkeypatch.setattr("route.cli.subprocess.run", lambda argv, **kw: SimpleNamespace(returncode=rc))


ARGS = ["--no-llm", "--shape", "coding:test", "--tier", "hard", "--pool", "codex"]


def test_dispatch_records_quota_delta_and_wall_clock(monkeypatch, capsys):
    _stub(monkeypatch, [{"codex": {"weekly": 2.0}}, {"codex": {"weekly": 3.0}}])
    assert main_auto([*ARGS, "write tests"]) == 0
    last = Stats().decisions()[-1]
    assert last["quota"] == {"provider": "codex", "before": {"weekly": 2.0},
                             "after": {"weekly": 3.0}, "delta": {"weekly": 1.0}}
    assert isinstance(last["wall_clock_s"], float)
    assert last["outcome_events"] == ["completed"]
    assert "quota: codex weekly 2% -> 3% (+1)" in capsys.readouterr().out


def test_local_arm_has_no_quota_record(monkeypatch, capsys):
    _stub(monkeypatch, [{"codex": {"weekly": 2.0}}, {"codex": {"weekly": 2.0}}])
    main_auto(["--no-llm", "--shape", "batch:classify", "--tier", "simple",
               "--pool", "local-batch", "classify these"])
    assert Stats().decisions()[-1]["quota"] is None
    assert "quota: not tracked for local-batch (no quota pool)" in capsys.readouterr().out


def test_no_quota_flag_skips_snapshots(monkeypatch):
    calls = []
    monkeypatch.setattr("route.cli.fetch_usage", lambda *a, **k: calls.append(1) or {})
    monkeypatch.setattr("route.cli.subprocess.run", lambda argv, **kw: SimpleNamespace(returncode=0))
    main_auto([*ARGS, "--no-quota", "write tests"])
    assert calls == []


def test_stats_quota_report(monkeypatch, capsys):
    _stub(monkeypatch, [{"codex": {"weekly": 2.0}}, {"codex": {"weekly": 3.0}}])
    main_auto([*ARGS, "write tests"])
    main(["outcome", "--shape", "coding:test", "--tier", "hard", "--arm", "codex", "--outcome", "accepted"])
    _stub(monkeypatch, [{"codex": {"weekly": 3.0}}, {"codex": {"weekly": 6.0}}], rc=1)
    main_auto([*ARGS, "write more tests"])
    main(["--no-llm", "--no-quota", "--plan-only", "--pool", "codex", "plan only"])  # not a dispatch
    capsys.readouterr()
    assert main(["stats", "--quota"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == report_quota(Stats())
    [codex] = rows
    assert codex["dispatches"] == 2 and codex["completed"] == 1
    assert codex["accepted"] == 1 and codex["failed"] == 1
    assert codex["accept_rate"] == 0.5
    assert codex["quota_pct_total"] == {"weekly": 4.0}
    assert codex["quota_pct_per_dispatch"] == {"weekly": 2.0}
    assert codex["quota_pct_per_accept"] == {"weekly": 4.0}
