"""contrib/beads: the reconciler that turns bead lifecycle into rewards.

The integration is not part of the package — it shells out to `bd`, which is a
separate Go binary — so it is loaded here by path. What these tests pin is the
decision table (which bead states become which outcome) and the two ordering
properties that make the reward trustworthy: claim before record, and one
writer only.
"""

from __future__ import annotations

import importlib.util
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

CONTRIB = Path(__file__).resolve().parent.parent / "contrib" / "beads"


def _load_reconciler():
    spec = importlib.util.spec_from_file_location(
        "route_reconcile", CONTRIB / "route_reconcile.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves annotations via
    # sys.modules[cls.__module__], which is None for an unregistered module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rc = _load_reconciler()

NOW = 1_800_000_000.0
DAY = 86_400.0


def bead(**overrides) -> dict:
    """A bead the `decision` and `complete` events both annotated."""
    base = {
        "id": "proj-1",
        "status": "open",
        "updated_at": "2027-01-15T12:00:00Z",
        "metadata": {
            "shape": "refactor",
            "tier": "hard",
            "arm": "codex",
            "exit_code": 0,
            "decision_ts": NOW - 60,
        },
    }
    metadata = overrides.pop("metadata", None)
    if metadata is not None:
        base["metadata"] = {**base["metadata"], **metadata}
    base.update(overrides)
    return base


def classify(b: dict, *, stale_days: int = 14):
    return rc.classify(b, now=NOW, stale_days=stale_days)


# --- decision table ----------------------------------------------------


def test_closed_after_clean_dispatch_is_the_accepted_signal():
    v = classify(bead(status="closed"))
    assert (v.action, v.outcome) == ("record", "accepted")


def test_closed_after_failed_dispatch_records_failure():
    v = classify(bead(status="closed", metadata={"exit_code": 2}))
    assert (v.action, v.outcome) == ("record", "failed")


def test_reopened_after_closing_is_a_failure():
    v = classify(bead(closed_at="2027-01-14T12:00:00Z"))
    assert (v.action, v.outcome) == ("record", "failed")


def test_nonzero_exit_on_open_work_is_a_failure():
    v = classify(bead(metadata={"exit_code": 1}))
    assert (v.action, v.outcome) == ("record", "failed")


def test_user_abort_is_not_charged_to_the_arm():
    assert classify(bead(metadata={"exit_code": 130})).action == "skip"
    assert (
        classify(
            bead(metadata={"exit_code": 1, "exception": "KeyboardInterrupt"})
        ).action
        == "skip"
    )


def test_abort_outranks_closure():
    """A closed bead whose dispatch was Ctrl-C'd is still not evidence."""
    v = classify(bead(status="closed", metadata={"exit_code": 130}))
    assert v.action == "skip"


@pytest.mark.parametrize("status", ["deferred", "blocked"])
def test_scheduling_states_are_left_alone(status):
    assert classify(bead(status=status)).action == "leave"


def test_in_flight_dispatch_is_left_alone():
    no_completion = bead()
    no_completion["metadata"].pop("exit_code")
    assert classify(no_completion).action == "leave"


def test_clean_but_unclosed_work_ages_into_a_failure():
    old = bead(metadata={"decision_ts": NOW - 20 * DAY})
    assert classify(old).outcome == "failed"
    assert classify(old, stale_days=30).action == "leave"


def test_dispatch_that_never_completed_is_released_not_punished():
    """Rule 8's ageing is unreachable without exit_code; rule 1 must escape."""
    abandoned = bead(metadata={"decision_ts": NOW - 20 * DAY})
    abandoned["metadata"].pop("exit_code")
    v = classify(abandoned)
    assert (v.action, v.outcome) == ("skip", None)


def test_bool_exit_code_is_not_an_int():
    """`True` is an int in Python; it is not an exit code."""
    weird = bead(metadata={"exit_code": True})
    assert classify(weird).action == "leave"


# --- timestamps --------------------------------------------------------


def test_decision_ts_is_preferred_over_updated_at():
    b = bead(metadata={"decision_ts": NOW - 20 * DAY})
    assert rc.dispatch_ts(b) == NOW - 20 * DAY


def test_updated_at_is_the_fallback_for_an_unpatched_route():
    b = bead(updated_at="2027-01-15T12:00:00Z")
    b["metadata"].pop("decision_ts")
    assert rc.dispatch_ts(b) == pytest.approx(1_800_000_000.0, abs=DAY * 400)


def test_a_bead_with_no_usable_timestamp_never_ages():
    b = bead(updated_at="not a timestamp")
    b["metadata"].pop("decision_ts")
    assert rc.dispatch_ts(b) is None
    assert rc.is_stale(b, now=NOW, stale_days=0) is False


def test_incomplete_routing_metadata_is_reported_not_guessed():
    b = bead(metadata={"arm": ""})
    assert rc.missing_fields(b) == ["arm"]


# --- ordering: claim before record -------------------------------------


class FakeRuns:
    """Records argv in call order and returns canned results."""

    def __init__(self, fail: str | None = None):
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv, cwd, timeout):
        self.calls.append(argv)
        failed = self.fail is not None and self.fail in " ".join(argv)
        return subprocess.CompletedProcess(argv, 1 if failed else 0, "", "boom")

    def verbs(self) -> list[str]:
        return [" ".join(a[:3]) for a in self.calls]


@pytest.fixture
def one_pending(monkeypatch):
    monkeypatch.setattr(rc, "resolve_auto", lambda: "/fake/auto")
    monkeypatch.setattr(rc, "fetch_pending", lambda cwd: [bead(status="closed")])


def _reconcile(**kwargs):
    return rc.reconcile(Path("."), now=NOW, stale_days=14, dry_run=False, **kwargs)


def test_bead_is_claimed_before_the_outcome_is_recorded(monkeypatch, one_pending):
    runs = FakeRuns()
    monkeypatch.setattr(rc, "_run", runs)
    results = _reconcile()
    assert results[0]["outcome"] == "accepted"
    assert runs.verbs() == [
        "bd label add",
        "/fake/auto outcome --shape",
        "bd label remove",
    ]


def test_decision_ts_is_passed_through_to_auto_outcome(monkeypatch, one_pending):
    runs = FakeRuns()
    monkeypatch.setattr(rc, "_run", runs)
    _reconcile()
    outcome = next(a for a in runs.calls if a[0] == "/fake/auto")
    assert "--decision-ts" in outcome
    assert float(outcome[outcome.index("--decision-ts") + 1]) == NOW - 60


def test_a_failed_claim_records_nothing(monkeypatch, one_pending):
    runs = FakeRuns(fail="label add")
    monkeypatch.setattr(rc, "_run", runs)
    results = _reconcile()
    assert results[0]["action"] == "error"
    assert not any(a[0] == "/fake/auto" for a in runs.calls)


def test_a_claimed_bead_that_fails_to_record_is_not_left_pending(
    monkeypatch, one_pending
):
    """Signal lost is recoverable. Double-counted is not, so it stays claimed."""
    runs = FakeRuns(fail="outcome")
    monkeypatch.setattr(rc, "_run", runs)
    results = _reconcile()
    assert results[0]["action"] == "error"
    assert "bd label remove" not in runs.verbs()


def test_dry_run_touches_nothing(monkeypatch, one_pending):
    runs = FakeRuns()
    monkeypatch.setattr(rc, "_run", runs)
    results = rc.reconcile(Path("."), now=NOW, stale_days=14, dry_run=True)
    assert results[0]["outcome"] == "accepted"
    assert runs.calls == []


def test_one_bad_bead_does_not_blank_the_run(monkeypatch):
    monkeypatch.setattr(rc, "resolve_auto", lambda: "/fake/auto")
    monkeypatch.setattr(
        rc,
        "fetch_pending",
        lambda cwd: [bead(id="proj-1", metadata={"shape": None}), bead(id="proj-2")],
    )
    monkeypatch.setattr(rc, "_run", FakeRuns())
    results = _reconcile()
    assert [r["id"] for r in results] == ["proj-1", "proj-2"]
    assert results[0]["action"] == "skipped-incomplete"


def test_missing_bd_exits_zero(monkeypatch):
    monkeypatch.setattr(rc.shutil, "which", lambda name: None)
    assert rc.main([]) == 0


def test_missing_auto_records_nothing(monkeypatch):
    monkeypatch.setattr(rc, "resolve_auto", lambda: None)
    monkeypatch.setattr(rc, "fetch_pending", lambda cwd: [bead(status="closed")])
    assert _reconcile() == []


# --- the hook is inert without bd --------------------------------------


HOOK = CONTRIB / "route-bead-hook.sh"


@pytest.mark.skipif(shutil.which("jq") is None, reason="hook needs jq")
def test_hook_is_inert_and_silent_on_stdout_without_bd(tmp_path, monkeypatch):
    """No bd on PATH must mean no hook_context and no non-zero exit."""
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()
    for tool in ("bash", "sh", "env", "cat", "jq", "git", "timeout"):
        found = shutil.which(tool)
        if found:
            (empty_bin / tool).symlink_to(found)
    HOOK.chmod(HOOK.stat().st_mode | stat.S_IXUSR)

    proc = subprocess.run(
        [str(HOOK)],
        input='{"event": "decision", "plan": {"chosen": "codex"}}',
        capture_output=True,
        text=True,
        env={"PATH": str(empty_bin), "HOME": str(tmp_path), "ROUTE_BEAD": "proj-1"},
        cwd=str(tmp_path),
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "bd not installed" in proc.stderr
