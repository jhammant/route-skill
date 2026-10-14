"""Real CLI subprocesses with isolated state and a harmless fake dispatch CLI."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from route.storage import config_dir, state_dir


def cli(*args, auto=False):
    if auto:
        command = [sys.executable, "-c", "from route.cli import main_auto; raise SystemExit(main_auto())"]
    else:
        command = [sys.executable, "-m", "route.cli"]
    return subprocess.run([*command, *args], capture_output=True, text=True, timeout=15)


def json_output(proc):
    assert proc.returncode == 0, proc.stderr + proc.stdout
    start = proc.stdout.index("{")
    return json.JSONDecoder().raw_decode(proc.stdout[start:])[0]


def test_dispatch_settle_reroute_and_export_across_processes(tmp_path):
    # Arrange: no provider can run or read real configuration.
    helper = tmp_path / "fake-agent.py"
    helper.write_text("import sys\nassert sys.argv[1] == 'build parser endpoint'\nprint('fake execution complete')\n")
    command = f'"{sys.executable}" "{helper}" {{task}}'
    config_dir().mkdir(parents=True)
    (config_dir() / "pools.toml").write_text(
        '[pools.codex]\nshapes = ["coding:implement"]\n'
        f'dispatch = {json.dumps(command)}\nremote = true\n'
        '[pools.codex.model]\nname = "fake-agent"\n'
    )
    common = ["--host", "claude", "--no-quota", "--no-llm",
              "--shape", "coding:implement", "--tier", "moderate"]

    # Act: execute, resolve an older decision after another plan, then route again.
    dispatched = json_output(cli(*common, "--pool", "codex", "build parser endpoint", auto=True))
    pending = json_output(cli(*common, "--pool", "claude", "--plan-only", "build parser endpoint"))
    settled = json_output(cli("settle", "--decision", dispatched["decision_id"], "--fix-passes", "1", "--merged"))
    repeated = json_output(cli("settle", "--decision", dispatched["decision_id"], "--fix-passes", "1", "--merged"))
    # Disable optional health/subcommand probes in the next process via config.
    pool_path = config_dir() / "pools.toml"
    pool_path.write_text(pool_path.read_text() + '\n[pools.local-agent]\nshapes = ["coding:implement"]\n')
    rerouted = json_output(cli(*common, "--plan-only", "build parser endpoint"))
    stats = cli("stats")
    exported = json_output(cli("federate", "export"))

    # Assert: stable ID, exactly one weighted trial, no pending neighbour, no memory export.
    assert settled == repeated and settled["score"] == 0.7
    assert dispatched["decision_id"] != pending["decision_id"]
    assert rerouted["neighbours"] == [{"arm": "codex", "score": 0.7, "similarity": 1.0}]
    assert stats.returncode == 0
    observed = [r for r in json.loads(stats.stdout) if r["arm"] == "codex"]
    assert len(observed) == 1 and observed[0]["obs"] == 1
    assert observed[0]["success_rate"] == 0.7
    assert "parser" not in json.dumps(exported) and "neighbours" not in json.dumps(exported)
    decisions = [json.loads(line) for line in (state_dir() / "decisions.jsonl").read_text().splitlines()]
    assert decisions[0]["decision_id"] == dispatched["decision_id"]
    assert decisions[0]["final_score"] == 0.7 and decisions[1]["final_score"] is None
    assert Path(os.environ["ROUTE_STATE_DIR"]).is_relative_to(tmp_path)


@pytest.mark.parametrize("feedback", [["--score", "1.2"], ["--fix-passes", "-2"], ["--score", "nan"]])
def test_invalid_feedback_process_exits_two_without_creating_state(feedback):
    # Arrange / Act
    proc = cli("outcome", "--shape", "coding:test", "--tier", "hard", "--arm", "codex", *feedback)

    # Assert
    assert proc.returncode == 2
    assert not (state_dir() / "bandits.json").exists()
