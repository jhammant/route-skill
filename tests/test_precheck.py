"""A pool's ``precheck``: a command that decides, per directory, whether the arm may take a task."""
from __future__ import annotations

import sys
from dataclasses import replace

import pytest

from route import eligibility
from route.eligibility import eligible_for, precheck_passes
from route.pools import load_pools

SHAPE = "coding:implement"
PY = sys.executable


def test_precheck_passes_on_exit_zero():
    assert precheck_passes(f"{PY} -c 'raise SystemExit(0)'") is True


@pytest.mark.parametrize("command", [
    f"{PY} -c 'raise SystemExit(3)'",      # the check said no
    "no-such-tool-anywhere --check",       # the tool is not installed
    "",                                    # nothing to run
    "unbalanced 'quote",                   # malformed
])
def test_precheck_fails_closed(command):
    assert precheck_passes(command) is False


def test_precheck_times_out_closed():
    assert precheck_passes(f"{PY} -c 'import time; time.sleep(5)'", timeout=0.3) is False


def test_precheck_runs_in_the_task_directory(tmp_path):
    (tmp_path / "marker").write_text("here")
    command = f"{PY} -c \"import os,sys; sys.exit(0 if os.path.exists('marker') else 1)\""

    here = precheck_passes(command, cwd=str(tmp_path))
    elsewhere = precheck_passes(command, cwd=str(tmp_path.parent))

    assert here is True and elsewhere is False


def test_precheck_is_not_run_through_a_shell(tmp_path):
    target = tmp_path / "made-by-shell"

    precheck_passes(f"{PY} -c 'pass' ; touch {target}")

    assert not target.exists()


def test_arm_with_a_failing_precheck_is_not_eligible(pools, monkeypatch):
    pools = dict(pools, cluster=replace(pools["codex"], name="cluster", precheck="cluster-check ."))
    monkeypatch.setattr(eligibility, "precheck_passes", lambda command, cwd=None, timeout=20.0: False)

    arms = eligible_for(SHAPE, pools)

    assert "cluster" not in arms and "codex" in arms


def test_arm_with_a_passing_precheck_is_eligible(pools, monkeypatch):
    pools = dict(pools, cluster=replace(pools["codex"], name="cluster", precheck="cluster-check ."))
    seen = []
    monkeypatch.setattr(eligibility, "precheck_passes", lambda command, cwd=None, timeout=20.0: seen.append(command) or True)

    arms = eligible_for(SHAPE, pools)

    assert "cluster" in arms and seen == ["cluster-check ."]


def test_arms_without_a_precheck_never_run_one(pools, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no arm here declares a precheck")
    monkeypatch.setattr(eligibility, "precheck_passes", boom)

    arms = eligible_for(SHAPE, pools)

    assert arms


def test_pools_toml_reads_precheck(tmp_path):
    cfg = tmp_path / "pools.toml"
    cfg.write_text('[pools.cluster]\nshapes = ["coding:implement"]\nremote = true\n'
                   'dispatch = "run-there {task}"\nprecheck = "cluster-check . --quiet"\n')

    pools = load_pools(cfg)

    assert pools["cluster"].precheck == "cluster-check . --quiet" and pools["codex"].precheck == ""
