"""Outcome events must land on the decision they belong to.

`record_outcome_events` historically annotated `rows[-1]` — fine when the
outcome is recorded seconds after the dispatch, wrong for any consumer that
reconciles later. `decision_ts` is the correlation key that fixes it.
"""

from __future__ import annotations

import json

import pytest

from route.stats import Decision, Stats


def _decision(cell: str, chosen: str, ts: float) -> Decision:
    shape, tier = cell.split(":", 1)
    return Decision(
        cell=cell, shape=shape, tier=tier,
        eligible=[chosen], chosen=chosen, why="prior", ts=ts,
    )


def _rows(stats: Stats) -> list[dict]:
    return [json.loads(line) for line in
            stats.decisions_path.read_text().splitlines() if line.strip()]


def test_without_decision_ts_annotates_the_last_row():
    stats = Stats()
    stats.record_decision(_decision("coding:trivial", "claude", 100.0))
    stats.record_decision(_decision("coding:moderate", "codex", 200.0))

    stats.record_outcome_events(["completed"])

    rows = _rows(stats)
    assert rows[0]["outcome_events"] == []
    assert rows[1]["outcome_events"] == ["completed"]


def test_decision_ts_annotates_the_matching_row_not_the_last():
    stats = Stats()
    stats.record_decision(_decision("coding:trivial", "claude", 100.0))
    stats.record_decision(_decision("coding:moderate", "codex", 200.0))

    stats.record_outcome_events(["completed", "verified"], decision_ts=100.0)

    rows = _rows(stats)
    assert rows[0]["outcome_events"] == ["completed", "verified"]
    assert rows[1]["outcome_events"] == []


def test_decision_ts_tolerates_float_round_tripping():
    stats = Stats()
    stats.record_decision(_decision("coding:trivial", "claude", 1785405334.9370985))

    stats.record_outcome_events(["completed"], decision_ts=1785405334.937098)

    assert _rows(stats)[0]["outcome_events"] == ["completed"]


def test_unmatched_decision_ts_writes_nothing_and_warns(capsys):
    stats = Stats()
    stats.record_decision(_decision("coding:trivial", "claude", 100.0))

    stats.record_outcome_events(["completed"], decision_ts=999.0)

    assert _rows(stats)[0]["outcome_events"] == []
    assert "999" in capsys.readouterr().err


def test_empty_log_is_a_no_op():
    Stats().record_outcome_events(["completed"], decision_ts=100.0)  # must not raise
