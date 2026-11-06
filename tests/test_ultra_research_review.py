"""Regression repros for research orchestration; all model processes are fakes."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from route import cli, ultra, ultra_research
from route.storage import state_dir
from tests.test_ultra_research import (
    QUESTION,
    QUOTA,
    calls,
    fake as fake,
    latest_run,
    part,
    standard_answers,
    three_parts,
    write_allowlist,
    write_claude_json,
    write_plan,
)


@pytest.mark.parametrize("words", [("status",), ("status", "known-run"), ("cancel", "known-run")])
def test_research_rejects_status_and_cancel_positionals(monkeypatch, words):
    # Arrange: record the command handlers without cancelling any real run.
    dispatched = []
    monkeypatch.setattr(ultra, "ultra_status", lambda run_id: dispatched.append(("status", run_id)) or 0)
    monkeypatch.setattr(ultra, "ultra_cancel", lambda run_id: dispatched.append(("cancel", run_id)) or 0)

    # Act.
    rc = cli.main(["ultra", *words, "--research", "q"])

    # Assert: all positional words conflict with --research, including commands.
    assert rc == 2, f"Conflicting argv dispatched {dispatched} instead of rejecting it"
    assert dispatched == []


# Model Codex's documented preflight gate, without starting a real model or
# requiring Codex to be installed on the CI machine.
CODEX_GIT_GATE = r'''
skip_git_check=false
for arg in "$@"; do
  [ "$arg" = --skip-git-repo-check ] && skip_git_check=true
done
if [ "$skip_git_check" != true ] && ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Not inside a trusted directory and --skip-git-repo-check was not specified." >&2
  exit 1
fi
'''


@pytest.mark.parametrize("stage", ["planner", "retry"])
def test_codex_can_run_in_research_scratch_directories(fake, monkeypatch, stage):
    # Arrange: preserve the real Codex Git preflight in the otherwise fake arm.
    write_plan(fake, [part("alpha", "What is alpha?")])
    standard_answers(fake)
    codex = fake.parent / "bin" / "codex"
    codex.write_text(codex.read_text().replace(
        'if [ "$role" = planner ]; then', CODEX_GIT_GATE + '\nif [ "$role" = planner ]; then', 1))
    if stage == "planner":
        monkeypatch.setattr(cli, "fetch_quota", lambda: {**QUOTA, "claude-work": "critical"})
    else:
        (fake / "answers" / "alpha.bad-claude-work").touch()

    # Act: the default research run has no --repo.
    rc = ultra_research.run_research(QUESTION)

    # Assert: Codex is usable both as the sole planner and as a part's retry.
    summary, run_dir = latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    assert next(row for row in summary["parts"] if row["id"] == "alpha")["arm"] == "codex"
    assert any(row[0] == "codex" and row[1] == "part" for row in calls(fake))


def test_nonzero_synthesis_exit_uses_deterministic_fallback(fake):
    # Arrange: the synthesizer emits a complete block, but its process fails.
    write_plan(fake, three_parts())
    standard_answers(fake)
    script = fake.parent / "bin" / "claude-work"
    script.write_text(script.read_text().replace('echo "ULTRA-REPORT-END"; exit 0',
                                                'echo "ULTRA-REPORT-END"; exit 19'))

    # Act.
    rc = ultra_research.run_research(QUESTION)

    # Assert: failed synthesis must leave the successful parts as the report.
    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0
    assert summary["synthesizer"] is None, "A report from an unsuccessful dispatch was accepted"
    assert "## alpha: What is alpha?" in text and "## beta: What is beta?" in text


@pytest.mark.parametrize("field", ["sources", "confidence", "gaps"])
def test_incomplete_answer_envelope_is_retried_and_fails(fake, field):
    # Arrange: both arms omit one of the required answer fields.
    write_plan(fake, [part("alpha", "What is alpha?")])
    standard_answers(fake)
    answer_path = fake / "answers" / "alpha.json"
    payload = json.loads(answer_path.read_text())
    del payload[field]
    answer_path.write_text(json.dumps(payload))

    # Act.
    rc = ultra_research.run_research(QUESTION)

    # Assert: a partial envelope is invalid, not an answered research part.
    summary, _ = latest_run()
    assert rc == 1, f"The run silently supplied the missing {field!r} field"
    alpha = next(row for row in summary["parts"] if row["id"] == "alpha")
    assert alpha["state"] == "failed" and alpha["attempts"] == ["claude-work", "codex"]


@pytest.fixture
def running_research(fake, tmp_path, monkeypatch):
    """An isolated orchestrator with a sleeping fake part and a dummy MCP token."""
    write_plan(fake, [part("alpha", "What is alpha?")])
    standard_answers(fake)
    write_allowlist(["read-only-server"])
    write_claude_json(tmp_path, monkeypatch, {
        "read-only-server": {"command": "unused-fake-server", "env": {"TOKEN": "dummy-test-value"}},
    })
    script = fake.parent / "bin" / "claude-work"
    script.write_text(script.read_text().replace(
        'if [ "$role" = synth ]; then',
        'if [ "$role" = part ]; then touch "$FAKE_DIR/part-started"; sleep 30; fi\n'
        'if [ "$role" = synth ]; then', 1))
    code = (
        "from route import cli, ultra_research\n"
        f"cli.fetch_quota = lambda: {QUOTA!r}\n"
        "cli.fetch_usage = lambda: {}\n"
        "raise SystemExit(ultra_research.run_research('test interruption'))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    delegates = []
    try:
        deadline = time.monotonic() + 10
        run_dir = None
        while time.monotonic() < deadline:
            paths = list((state_dir() / "ultra").glob("*/progress.json"))
            if paths:
                run_dir = paths[0].parent
                progress = json.loads(paths[0].read_text())
                delegates = progress["pids"]
                if delegates and (fake / "part-started").exists():
                    break
            if proc.poll() is not None:
                pytest.fail(f"Orchestrator exited during setup: {proc.communicate()[0]}")
            time.sleep(0.025)
        else:
            pytest.fail("Fake research worker did not start")
        assert run_dir is not None and (run_dir / "research-mcp.json").is_file()
        yield proc, run_dir
    finally:
        # Every group here was started by this fixture; no real agent is involved.
        for pid in [proc.pid, *delegates]:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
        proc.communicate(timeout=5)


def test_interrupt_stops_inflight_research_promptly(running_research):
    # Arrange: wait until the executor is blocked on its running worker.
    proc, run_dir = running_research

    # Act: Ctrl-C is delivered to the orchestrator, not its detached workers.
    proc.send_signal(signal.SIGINT)
    try:
        output, _ = proc.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        pytest.fail("SIGINT waits for the running executor job instead of cancelling it")

    # Assert: interruption has the documented status and cleans credentials.
    assert proc.returncode == 130, output
    assert json.loads((run_dir / "progress.json").read_text())["state"] == "interrupted"
    assert not (run_dir / "research-mcp.json").exists()


def test_cancel_removes_research_mcp_credentials(running_research):
    # Arrange: a live research run has copied its allowlisted MCP credentials.
    proc, run_dir = running_research

    # Act: use the public cancellation path rather than an artificial exception.
    rc = ultra.ultra_cancel(run_dir.name)
    proc.communicate(timeout=5)

    # Assert: cancellation ends the run and removes the transient credential file.
    assert rc == 0
    assert json.loads((run_dir / "progress.json").read_text())["state"] in {"cancelled", "interrupted"}
    assert not (run_dir / "research-mcp.json").exists(), "Cancellation left the copied MCP credentials on disk"
