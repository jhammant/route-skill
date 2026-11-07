"""Concurrent review/fix jobs preserve verdicts, integration order and dispatch caps."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from route import ultra
from route.pools import Model, Pool
from route.ultra_plan import Plan, Unit
from route.ultra_review import Finding


def execute(monkeypatch, tmp_path, unit_ids, *, max_parallel, rejected=(), swarm_order=None):
    """Exercise _execute and real integration ordering without models, git writes or quota calls."""
    plan = Plan("Parallel review", "", "pytest", [Unit(uid, "task", "pytest") for uid in unit_ids])
    rows = {uid: dict(id=uid, arm="author", accepted=uid not in rejected,
                     worktree=str(tmp_path / uid), branch=f"unit-{uid}", log="unused",
                     decision_id=f"decision-{uid}", agent_rc=1 if uid in rejected else 0)
            for uid in (swarm_order or unit_ids)}
    pools = {"author": Pool(name="author", shapes=(), model=Model(name="claude-opus"))}
    quota = {"author": "ok"}
    run = ultra.Run("parallel-review", "goal", tmp_path, "HEAD", "claude")
    captured = {"merge_order": []}
    integrate = ultra._integrate

    def record_integration(*args):
        captured["args"] = args
        return integrate(*args)

    def merge(_run, unit, *args):
        captured["merge_order"].append(unit["id"])
        return None

    monkeypatch.setattr(ultra.cli, "load_pools", lambda: pools)
    monkeypatch.setattr(ultra, "pools_for_host", lambda *a: pools)
    monkeypatch.setattr(ultra.cli, "fetch_quota", lambda: quota)
    monkeypatch.setattr(ultra.cli, "fetch_usage", lambda: {})
    monkeypatch.setattr(ultra, "pick", lambda *a, **k: "author")
    monkeypatch.setattr(ultra, "_plan", lambda *a: (plan, []))
    monkeypatch.setattr(ultra, "run_swarm_manifest", lambda *a, **k: {"units": list(rows.values())})
    monkeypatch.setattr(ultra, "_record_scope", lambda *a: [])
    monkeypatch.setattr(ultra, "_git", lambda *a: SimpleNamespace(returncode=0))
    monkeypatch.setattr(ultra, "_on_branch", lambda *a: None)
    monkeypatch.setattr(ultra, "_merge_unit", merge)
    monkeypatch.setattr(ultra, "run_checked", lambda *a, **k: SimpleNamespace(rc=0, output=""))
    monkeypatch.setattr(ultra, "_integrate", record_integration)
    monkeypatch.setattr(ultra, "_settle", lambda *a: None)
    monkeypatch.setattr(ultra, "_report", lambda *a: a[-2])

    rc = ultra._execute(run, "goal", tmp_path, "HEAD", "base-sha", "brief", None,
                        10, max_parallel, False, False, False, "claude")
    # Every input to integration is unchanged apart from the concurrently computed sound set.
    assert captured["args"] == (run, plan, rows, captured["args"][3], tmp_path, "base-sha",
                                pools, quota, "claude", False, "goal")
    return rc, run, captured


@pytest.mark.parametrize("max_parallel", [1, 2, 3])
def test_review_and_fix_jobs_overlap_up_to_max_parallel(monkeypatch, tmp_path, max_parallel):
    # Arrange: count jobs from review entry through fix exit, including the transition between them.
    lock = threading.Lock()
    first_wave = threading.Barrier(max_parallel)
    active = peak = started = 0
    events = []
    finding = Finding("bug", "high", "detail", "pytest")

    def review(run, unit, *args):
        nonlocal active, peak, started
        with lock:
            active += 1
            started += 1
            ordinal = started
            peak = max(peak, active)
            events.append((unit["id"], "review"))
        if ordinal <= max_parallel:
            first_wave.wait(timeout=5)
        time.sleep(0.01)
        return [finding]

    def fix(run, unit, *args):
        nonlocal active
        with lock:
            events.append((unit["id"], "fix"))
        time.sleep(0.01)
        with lock:
            active -= 1
        return True

    monkeypatch.setattr(ultra, "_review_unit", review)
    monkeypatch.setattr(ultra, "_fix_unit", fix)
    unit_ids = [f"unit-{i}" for i in range(6)]
    # Act
    rc, _, captured = execute(monkeypatch, tmp_path, unit_ids, max_parallel=max_parallel)
    # Assert: max_parallel=1 is still serial; larger values reach the cap without exceeding it.
    assert peak == max_parallel
    assert active == 0
    assert rc == 0
    assert captured["args"][3] == set(unit_ids)
    assert captured["merge_order"] == unit_ids
    for uid in unit_ids:
        assert [phase for unit_id, phase in events if unit_id == uid] == ["review", "fix"]


def test_fix_starts_before_slower_review_finishes(monkeypatch, tmp_path):
    # Arrange: the slow review cannot finish until the fast unit's fix has started.
    slow_started = threading.Event()
    fix_started = threading.Event()
    times = {}

    def review(run, unit, *args):
        if unit["id"] == "slow":
            slow_started.set()
            assert fix_started.wait(timeout=5), "fix waited for every review to finish"
            time.sleep(0.01)
            times["slow_end"] = time.monotonic()
            return []
        assert slow_started.wait(timeout=5)
        time.sleep(0.01)
        return [Finding("bug", "high", "detail", "pytest")]

    def fix(run, unit, *args):
        assert unit["id"] == "fast"
        times["fix_start"] = time.monotonic()
        fix_started.set()
        time.sleep(0.01)
        return True

    monkeypatch.setattr(ultra, "_review_unit", review)
    monkeypatch.setattr(ultra, "_fix_unit", fix)
    # Act
    rc, _, captured = execute(monkeypatch, tmp_path, ["slow", "fast"], max_parallel=2)
    # Assert: integration still follows the plan even though the fast unit fixed first.
    assert times["fix_start"] < times["slow_end"]
    assert rc == 0
    assert captured["merge_order"] == ["slow", "fast"]


def test_sound_matches_serial_verdicts_and_merges_in_plan_order(monkeypatch, tmp_path):
    # Arrange: failed review, clean review, successful/failed fixes, successful/failed build repairs.
    finding = Finding("bug", "high", "detail", "pytest")
    verdicts = {"review-failed": None, "clean": [], "fixed": [finding], "unfixed": [finding]}
    unit_ids = [*verdicts, "build-fixed", "build-failed"]
    calls = {"review": {}, "fix": {}}
    lock = threading.Lock()

    def review(run, unit, *args):
        with lock:
            calls["review"][unit["id"]] = True
        time.sleep(0.01)
        return verdicts[unit["id"]]

    def fix(run, unit, row, confirmed, *args):
        with lock:
            calls["fix"][unit["id"]] = confirmed
        time.sleep(0.01)
        return unit["id"] in {"fixed", "build-fixed"}

    monkeypatch.setattr(ultra, "_review_unit", review)
    monkeypatch.setattr(ultra, "_fix_unit", fix)
    # Act: deliberately return swarm rows in the opposite order to the plan.
    rc, _, captured = execute(monkeypatch, tmp_path, unit_ids, max_parallel=3,
                              rejected={"build-fixed", "build-failed"}, swarm_order=list(reversed(unit_ids)))
    # Assert: the old serial semantics clear precisely these three units.
    assert captured["args"][3] == {"clean", "fixed", "build-fixed"}
    assert calls["review"] == dict.fromkeys(verdicts, True)
    assert calls["fix"] == {"fixed": [finding], "unfixed": [finding], "build-fixed": [], "build-failed": []}
    assert captured["merge_order"] == ["clean", "fixed", "build-fixed"]
    assert rc == 1


def test_failed_build_can_fix_while_accepted_unit_is_reviewed(monkeypatch, tmp_path):
    # Arrange: an unaccepted unit must not wait behind the review phase or get reviewed itself.
    review_started = threading.Event()
    fix_started = threading.Event()

    def review(run, unit, *args):
        assert unit["id"] == "accepted"
        review_started.set()
        assert fix_started.wait(timeout=5)
        return []

    def fix(run, unit, row, confirmed, *args):
        assert unit["id"] == "rejected" and confirmed == []
        assert review_started.wait(timeout=5)
        fix_started.set()
        return True

    monkeypatch.setattr(ultra, "_review_unit", review)
    monkeypatch.setattr(ultra, "_fix_unit", fix)
    # Act
    rc, _, captured = execute(monkeypatch, tmp_path, ["accepted", "rejected"], max_parallel=2,
                              rejected={"rejected"})
    # Assert
    assert rc == 0
    assert captured["args"][3] == {"accepted", "rejected"}


def test_concurrent_unit_updates_persist_every_unit(monkeypatch, tmp_path):
    # Arrange: widen the write window to catch overlapping saves and stale snapshots.
    run = ultra.Run("progress-threads", "goal", tmp_path, "HEAD", "claude")
    atomic_write = ultra.atomic_write
    lock = threading.Lock()
    start = threading.Barrier(8)
    active = peak = 0

    def slow_write(path, text):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.002)
            atomic_write(path, text)
        finally:
            with lock:
                active -= 1

    def update_units(worker):
        start.wait(timeout=5)
        for index in range(8):
            run.unit(f"{worker}-{index}", state="fixed", fix_passes=index)
            # Watchers must also see valid JSON while other writers are active.
            assert json.loads((run.dir / "progress.json").read_text())["schema"] == ultra.SCHEMA

    monkeypatch.setattr(ultra, "atomic_write", slow_write)
    # Act
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(update_units, range(8)))
    progress = json.loads((run.dir / "progress.json").read_text())
    # Assert
    assert peak == 1
    assert progress == run.progress
    assert set(progress["units"]) == {f"{worker}-{index}" for worker in range(8) for index in range(8)}
    assert all(row["state"] == "fixed" for row in progress["units"].values())


@pytest.mark.parametrize("parallel_limit", [1, 2])
def test_dispatch_caps_each_arm_across_review_and_fix_roles(monkeypatch, tmp_path, parallel_limit):
    # Arrange: both roles use the same arm; a different arm can still run alongside it.
    run = ultra.Run("dispatch-slots", "goal", tmp_path, "HEAD", "claude")
    (run.dir / "logs").mkdir(parents=True)
    pools = {arm: Pool(name=arm, shapes=(), dispatch="fake {task}", parallel_limit=limit)
             for arm, limit in (("shared", parallel_limit), ("other", 1))}
    lock = threading.Lock()
    active, peak = Counter(), Counter()
    shared_started, other_started = threading.Event(), threading.Event()
    shared_wave = threading.Barrier(parallel_limit)
    shared_calls = 0

    def dispatch(template, prompt, *, target, log, on_start, **kwargs):
        nonlocal shared_calls
        with lock:
            active[target] += 1
            peak[target] = max(peak[target], active[target])
            if target == "shared":
                shared_calls += 1
                ordinal = shared_calls
        try:
            on_start(int(prompt) + 1000)
            if target == "shared":
                shared_started.set()
                assert other_started.wait(timeout=5), "one arm blocked another arm's dispatch"
                if ordinal <= parallel_limit:
                    shared_wave.wait(timeout=5)
            else:
                other_started.set()
                assert shared_started.wait(timeout=5)
            time.sleep(0.01)
            Path(log).write_text("done")
            return 0
        finally:
            with lock:
                active[target] -= 1

    monkeypatch.setattr(ultra.cli, "_dispatch", dispatch)
    # Act
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(ultra._dispatch, run, arm, pools, "claude", str(index), tmp_path,
                                   f"{'review' if index % 2 else 'fix'}-{index}.log", 10)
                   for index, arm in enumerate(["shared"] * 6 + ["other"] * 2)]
        results = [future.result(timeout=5) for future in futures]
    # Assert: each arm reaches its own cap; dispatch tracking is empty after completion.
    assert peak == {"shared": parallel_limit, "other": 1}
    assert not any(active.values())
    assert results == [(0, "done")] * 8
    assert run.progress["pids"] == []
    assert json.loads((run.dir / "progress.json").read_text())["pids"] == []


def test_dispatch_releases_arm_slot_and_pid_after_exception(monkeypatch, tmp_path):
    # Arrange
    run = ultra.Run("dispatch-error", "goal", tmp_path, "HEAD", "claude")
    pools = {"author": Pool(name="author", shapes=(), dispatch="fake {task}", parallel_limit=1)}

    def dispatch(*args, on_start, **kwargs):
        on_start(12345)
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(ultra.cli, "_dispatch", dispatch)
    # Act
    with pytest.raises(RuntimeError, match="dispatch failed"):
        ultra._dispatch(run, "author", pools, "claude", "prompt", tmp_path, "review.log", 10)
    # Assert: a failed dispatch cannot strand a queued job or a tracked PID.
    slot = run.arm_slot("author", pools["author"])
    assert slot.acquire(blocking=False)
    slot.release()
    assert run.progress["pids"] == []
