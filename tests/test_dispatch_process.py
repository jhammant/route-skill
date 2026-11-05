"""The opt-in PID callback uses real, harmless child processes offline."""

import json
import os
import subprocess
import sys
import time

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


def test_tracked_delegate_leads_its_own_session_and_reads_no_terminal(tmp_path):
    # Arrange: the child reports its session and what stdin holds.
    script = ("import json, os, sys\n"
              "print(json.dumps({'leader': os.getsid(0) == os.getpid(), 'stdin': sys.stdin.read()}))\n")

    # Act
    rc = _dispatch([sys.executable, "-c", script], "task", log=tmp_path / "agent.log", timeout=10,
                   on_start=lambda pid: None)

    # Assert
    assert rc == 0
    assert json.loads((tmp_path / "agent.log").read_text()) == {"leader": True, "stdin": ""}


def test_dispatch_timeout_kills_the_delegates_descendants(tmp_path):
    # Arrange: the delegate spawns a grandchild and writes its pid before sleeping.
    pid_file = tmp_path / "grandchild.pid"
    script = ("import subprocess, sys, time\n"
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
              "time.sleep(60)\n")

    # Act
    rc = _dispatch([sys.executable, "-c", script], "task", timeout=2, log=tmp_path / "agent.log",
                   on_start=lambda pid: None)

    # Assert: the grandchild went with the group.
    assert rc == 124
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _gone(grandchild):
        time.sleep(0.05)
    assert _gone(grandchild)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return not state.strip() or state.strip().startswith("Z")


def test_dispatch_never_hands_the_parent_stdin_to_a_delegate(tmp_path):
    # Arrange: a delegate that blocks reading stdin, the way `codex exec` reads
    # "additional input" from any open stdin.
    import os
    import subprocess
    import sys
    from route import cli
    script = tmp_path / "reads_stdin.py"
    script.write_text("import sys\nsys.stdin.read()\nprint('stdin closed')\n")
    read_end, write_end = os.pipe()  # a parent stdin that never reaches EOF
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        # Act
        rc = cli._dispatch([sys.executable, str(script)], "task", timeout=10)
    finally:
        os.dup2(saved, 0)
        for fd in (read_end, write_end, saved):
            os.close(fd)
    # Assert: it finished instead of timing out (124) on the inherited pipe
    assert rc == 0
