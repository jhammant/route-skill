"""The opt-in PID callback uses real, harmless child processes offline."""

import json
import os
import sys

import pytest

from route.cli import _dispatch


@pytest.mark.parametrize("stdin", [False, True])
def test_dispatch_reports_pid_and_preserves_input_environment_and_log(tmp_path, stdin):
    # Arrange
    task = "literal $(touch should-not-exist); `shell`\nsecond line"
    log = tmp_path / "agent.log"
    script = (
        "import json, os, sys\n"
        "print(json.dumps({'pid': os.getpid(), 'task': sys.stdin.read() if len(sys.argv) == 1 else sys.argv[1], "
        "'host': os.environ['ROUTE_HOST'], 'extra': os.environ['TEST_EXTRA'], 'cwd': os.getcwd()}))\n"
        "sys.exit(7)\n"
    )
    pids = []

    # Act
    rc = _dispatch([sys.executable, "-c", script], task, stdin=stdin, target="codex",
                   cwd=tmp_path, env={"TEST_EXTRA": "present"}, log=log, timeout=10,
                   on_start=pids.append)

    # Assert
    assert rc == 7
    assert len(pids) == 1 and pids[0] > 0
    assert json.loads(log.read_text()) == {"pid": pids[0], "task": task, "host": "codex",
                                          "extra": "present", "cwd": str(tmp_path.resolve())}
    assert not (tmp_path / "should-not-exist").exists()
    with pytest.raises(ChildProcessError):
        os.waitpid(pids[0], os.WNOHANG)


def test_dispatch_timeout_reports_pid_kills_and_reaps(tmp_path, capsys):
    # Arrange
    pids = []

    # Act
    rc = _dispatch([sys.executable, "-c", "import time; time.sleep(60)"], "task",
                   timeout=0.1, log=tmp_path / "agent.log", on_start=pids.append)

    # Assert
    assert rc == 124
    assert len(pids) == 1 and pids[0] > 0
    assert "timed out after 0.1s" in capsys.readouterr().err
    with pytest.raises(ChildProcessError):
        os.waitpid(pids[0], os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


def test_dispatch_callback_failure_also_kills_and_reaps(tmp_path):
    # Arrange
    pids = []

    def on_start(pid):
        pids.append(pid)
        raise RuntimeError("cannot record status")

    # Act / Assert
    with pytest.raises(RuntimeError, match="cannot record status"):
        _dispatch([sys.executable, "-c", "import time; time.sleep(60)"], "task",
                  log=tmp_path / "agent.log", on_start=on_start)
    assert len(pids) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(pids[0], os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
