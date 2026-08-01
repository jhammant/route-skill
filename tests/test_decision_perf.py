"""SPEC "Stats": what a dispatch cost must reach the decision record.

`Decision` declared `wall_clock_s` and `tokens` from the first commit and
nothing ever assigned them, so every row carried nulls in the two columns a
consumer would use to compare arms on latency or spend. `cmd_task` was already
timing the dispatch — it handed the number to the hook payload and dropped it.
"""

from __future__ import annotations

import json

import pytest

from route import cli
from route.stats import Decision, Stats


def _decision(ts: float, chosen: str = "codex") -> Decision:
    return Decision(
        cell="coding:implement:moderate",
        shape="coding:implement",
        tier="moderate",
        eligible=[chosen],
        chosen=chosen,
        why="prior",
        ts=ts,
    )


def _rows(stats: Stats) -> list[dict]:
    return [
        json.loads(line)
        for line in stats.decisions_path.read_text().splitlines()
        if line.strip()
    ]


def test_wall_clock_lands_on_the_decision_it_belongs_to():
    stats = Stats()
    stats.record_decision(_decision(100.0))
    stats.record_decision(_decision(200.0))
    stats.record_perf(decision_ts=100.0, wall_clock_s=12.5)
    rows = _rows(stats)
    assert rows[0]["wall_clock_s"] == 12.5
    assert rows[1]["wall_clock_s"] is None


def test_tokens_and_wall_clock_are_set_independently():
    """route can time a dispatch; only the arm knows what it spent."""
    stats = Stats()
    stats.record_decision(_decision(100.0))
    stats.record_perf(decision_ts=100.0, wall_clock_s=3.0)
    stats.record_perf(decision_ts=100.0, tokens=4096)
    row = _rows(stats)[0]
    assert (row["wall_clock_s"], row["tokens"]) == (3.0, 4096)


def test_a_later_measurement_corrects_an_earlier_one():
    """A caller that knows the arm's real accounting outranks the wall clock.

    Last write wins per field, so a backfilling reconciler does not have to
    care whether it ran before or after the inline measurement.
    """
    stats = Stats()
    stats.record_decision(_decision(100.0))
    stats.record_perf(decision_ts=100.0, wall_clock_s=3.0)
    stats.record_perf(decision_ts=100.0, wall_clock_s=9.0)
    assert _rows(stats)[0]["wall_clock_s"] == 9.0


def test_recording_nothing_writes_nothing():
    stats = Stats()
    stats.record_decision(_decision(100.0))
    before = stats.decisions_path.read_text()
    stats.record_perf(decision_ts=100.0)
    assert stats.decisions_path.read_text() == before


def test_an_unmatched_timestamp_warns_and_leaves_the_log_alone(capsys):
    """A rotated log must not take recording down — same rule as outcomes."""
    stats = Stats()
    stats.record_decision(_decision(100.0))
    stats.record_perf(decision_ts=999.0, wall_clock_s=1.0)
    assert _rows(stats)[0]["wall_clock_s"] is None
    assert "no decision at ts=999.0" in capsys.readouterr().err


def test_outcome_events_still_report_themselves_by_name(capsys):
    """The shared row-locator must not blur what a warning is about."""
    stats = Stats()
    stats.record_decision(_decision(100.0))
    stats.record_outcome_events(["accepted"], decision_ts=999.0)
    assert "outcome events not attached" in capsys.readouterr().err


# --- through the CLI -----------------------------------------------------


def test_a_dispatch_records_its_own_duration(monkeypatch):
    monkeypatch.setattr(cli, "_dispatch", lambda template, task: 0)
    assert cli.main_auto(["--pool", "local-agent", "write the docs"]) == 0
    row = _rows(Stats())[-1]
    assert row["wall_clock_s"] is not None and row["wall_clock_s"] >= 0.0


def test_the_stay_path_records_no_duration():
    """Nothing was dispatched, so 0.0 would be a fabricated latency."""
    assert cli.main_auto(["--pool", "claude", "write the docs"]) == 0
    assert _rows(Stats())[-1]["wall_clock_s"] is None


def test_an_interrupted_dispatch_still_records_what_it_burned(monkeypatch):
    """An arm that hangs and dies is exactly the observation worth keeping."""

    def _interrupted(template, task):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_dispatch", _interrupted)
    with pytest.raises(KeyboardInterrupt):
        cli.main_auto(["--pool", "local-agent", "write the docs"])
    assert _rows(Stats())[-1]["wall_clock_s"] is not None


def test_outcome_backfills_tokens_against_a_decision(monkeypatch):
    monkeypatch.setattr(cli, "_dispatch", lambda template, task: 0)
    cli.main_auto(["--pool", "local-agent", "write the docs"])
    ts = _rows(Stats())[-1]["ts"]
    assert (
        cli.main(
            [
                "outcome",
                "--shape",
                "coding:implement",
                "--tier",
                "moderate",
                "--arm",
                "local-agent",
                "--outcome",
                "accepted",
                "--decision-ts",
                repr(ts),
                "--tokens",
                "4096",
                "--wall-clock",
                "42.5",
            ]
        )
        == 0
    )
    row = _rows(Stats())[-1]
    assert (row["tokens"], row["wall_clock_s"]) == (4096, 42.5)
    assert "accepted" in row["outcome_events"]


def test_outcome_without_the_flags_leaves_perf_untouched(monkeypatch):
    monkeypatch.setattr(cli, "_dispatch", lambda template, task: 0)
    cli.main_auto(["--pool", "local-agent", "write the docs"])
    measured = _rows(Stats())[-1]["wall_clock_s"]
    cli.main(
        [
            "outcome",
            "--shape",
            "coding:implement",
            "--tier",
            "moderate",
            "--arm",
            "local-agent",
            "--outcome",
            "accepted",
        ]
    )
    row = _rows(Stats())[-1]
    assert row["wall_clock_s"] == measured
    assert row["tokens"] is None
