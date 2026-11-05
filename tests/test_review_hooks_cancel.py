"""Regression: cancelling a swarm must not return while a worker is still running."""

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from route import swarm_dispatch as s


def test_interrupted_swarm_leaves_no_running_worker():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    pool = SimpleNamespace(parallel_limit=1, quota_provider=None)
    ctx = SimpleNamespace(task_on_stdin=False, pools={"claude": pool}, decision=object())
    a = s.Assignment({"id": "unit", "task": "test", "check": "true"}, "test",
        {"chosen": "claude", "why": "test", "shape": "code", "tier": "small", "decision_id": "review"}, ctx)
    manifest = {"name": "review", "swarm_id": "review", "units": [a.unit]}

    def run_unit(*args):
        started.set()
        release.wait(2)
        finished.set()
        return {"id": "unit", "accepted": True}

    def interrupt(futures):
        assert started.wait(1), "worker did not start"
        raise KeyboardInterrupt

    try:
        with patch.object(s, "state_dir", return_value=Path("/unused-route-review")), \
             patch.object(Path, "mkdir"), patch.object(s, "SwarmStatus"), \
             patch.object(s, "Stats"), patch.object(s, "TaskMemory"), \
             patch.object(s, "atomic_write"), \
             patch.object(s, "_git", return_value=SimpleNamespace(returncode=0, stdout="true\n", stderr="")), \
             patch.object(s, "assign_units", return_value=([a], [])), \
             patch.object(s, "render_brief", return_value="brief"), \
             patch.object(s, "dispatch_command", return_value=(["not-launched"], {})), \
             patch.object(s, "plan_swarm", return_value=1), \
             patch.object(s, "_run_unit", side_effect=run_unit), \
             patch.object(s, "as_completed", side_effect=interrupt):
            with pytest.raises(KeyboardInterrupt):
                s.run_swarm_manifest(manifest, no_quota=True, no_amail=True)
            assert finished.is_set(), "API returned control while a worker was still running"
    finally:
        release.set()
        if started.is_set():
            assert finished.wait(3)
