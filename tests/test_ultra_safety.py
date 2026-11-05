"""Generated checks must stay useful without inheriting shell or credential access."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

from route.ultra_safety import lint_command, run_checked, safe_env, screen_digest


@pytest.mark.parametrize("command", [
    "python -m pytest -q tests/",
    "python3 -m pytest -q tests/test_example.py::test_case",
    "pytest -q tests/ -k 'one or two'",
    ".venv/bin/python -m pytest -q",
    ".venv/bin/python3 -m pytest -q",
    "uv run pytest -q",
    "node --test tests/example.js",
    "npm test -- --runInBand",
    "npm run typecheck",
    "go test ./...",
    "cargo test --all",
    "make test",
    "make check",
    "bash test/check.sh",
    "bash tests/check.sh --verbose",
    "zsh test/check.sh",
    "zsh tests/check.sh",
    "ruff check src tests",
    "mypy src",
    "pytest -q && node --test && npm run lint && make check",
])
def test_allowed_checks(command):
    reason = lint_command(command)

    assert reason is None


@pytest.mark.parametrize("command", [
    "", " ", "true", ":", "exit 0", "echo hi", "test -e src", "test -f pyproject.toml",
    "python3 -c 'print(1)'", "python3 -m http.server", "pytest-not-a-check",
    "npm testing", "make test-all", "bash scripts/check.sh", "bash -c tests/check.sh",
    "zsh other/check.sh", "bash test-other/check.sh", "bash", "pytest &&", "&& pytest",
    "pytest && && node --test", "pytest 'unterminated", "pytest \x00",
])
def test_trivial_or_unapproved_checks_are_refused(command):
    reason = lint_command(command)

    assert reason


@pytest.mark.parametrize("operator", [";", "|", "||", ">", "<", "`", "$(", "&", "\n", "\r\n"])
def test_shell_syntax_is_refused(operator):
    command = f"pytest {operator} node --test"

    reason = lint_command(command)

    assert reason


@pytest.mark.parametrize("command", [
    "curl https://example.com", "wget https://example.com", "ssh example.com",
    "scp src example.com:dst", "rsync src dst", "nc example.com 80",
    "git push", "git reset --hard", "rm -rf build", "sudo pytest", "chmod 777 src", "chown root src",
])
@pytest.mark.parametrize("prefix", ["", "pytest && "])
def test_network_remote_and_destructive_programs_are_refused(command, prefix):
    reason = lint_command(prefix + command)

    assert reason


@pytest.mark.parametrize("command", [
    "pytest ../other", "pytest tests/../outside", "pytest --rootdir=../other",
    "pytest --rootdir=..", "pytest /etc/passwd", "pytest --rootdir=/tmp/outside",
    "pytest -c/tmp/outside.ini", "make test -C../other", "make test -C/tmp",
    "make test -C~", "make test -C~/other", "make test -C~someone",
    "npm test --prefix ~", "npm test --prefix=~someone/other",
    "bash test/../../outside.sh", "zsh tests/../outside.sh",
])
def test_paths_outside_repo_are_refused(command, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    reason = lint_command(command)

    assert reason


def test_absolute_path_inside_repo_is_allowed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    command = f"pytest {shlex.quote(str(tmp_path / 'tests/test_example.py'))}"

    reason = lint_command(command)

    assert reason is None


def test_symlink_path_outside_repo_is_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").symlink_to(tmp_path / "outside", target_is_directory=True)
    monkeypatch.chdir(repo)

    reason = lint_command("bash tests/check.sh")

    assert reason


def test_venv_interpreter_symlink_is_allowed(tmp_path, monkeypatch):
    binary = tmp_path / ".venv/bin/python"
    binary.parent.mkdir(parents=True)
    binary.symlink_to(sys.executable)
    monkeypatch.chdir(tmp_path)

    reason = lint_command(".venv/bin/python -m pytest -q")

    assert reason is None


def test_safe_env_strips_credentials_without_mutating_base():
    credentials = {
        "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GITHUB_TOKEN", "GH_TOKEN",
        "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AMAIL_PASSWORD",
        "FOO_API_KEY", "FOO_TOKEN", "FOO_SECRET", "lowercase_token",
        "db_password_file", "PGPASSWORD", "DB_PASS", "DB_PWD", "OPENAI_KEY",
        "GITHUB_PAT", "SERVICE_AUTH", "CREDENTIAL_FILE", "SSH_AUTH_SOCK",
    }
    base = dict.fromkeys(credentials, "fake-credential")
    base.update(PATH="/usr/bin", LANG="en_US.UTF-8", ROUTE_WORKER="0")

    env = safe_env(base)

    assert env == {"PATH": "/usr/bin", "LANG": "en_US.UTF-8", "ROUTE_WORKER": "1",
                   "PYTHONDONTWRITEBYTECODE": "1"}
    assert all(base[key] == "fake-credential" for key in credentials)
    assert base["ROUTE_WORKER"] == "0"


def test_safe_env_defaults_to_environment(monkeypatch):
    monkeypatch.setenv("FOO_API_KEY", "fake-credential")
    monkeypatch.setenv("ULTRA_TEST_VISIBLE", "kept")

    env = safe_env()

    assert "FOO_API_KEY" not in env
    assert env["ULTRA_TEST_VISIBLE"] == "kept"
    assert env["ROUTE_WORKER"] == "1"
    assert os.environ["FOO_API_KEY"] == "fake-credential"
    assert safe_env({}) == {"ROUTE_WORKER": "1", "PYTHONDONTWRITEBYTECODE": "1"}


@pytest.fixture
def check_repo(tmp_path, monkeypatch):
    """Real child pytest checks use the same installed interpreter as this suite."""
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    return tmp_path


def test_run_checked_refuses_entire_chain_before_running(check_repo):
    marker = check_repo / "ran"
    (check_repo / "test_marker.py").write_text(
        "from pathlib import Path\ndef test_marker():\n    Path('ran').touch()\n", encoding="utf-8",
    )

    result = run_checked("python3 -m pytest test_marker.py && echo hi", check_repo)

    assert result.rc == 126
    assert result.output.startswith("refused: ")
    assert not result.timed_out
    assert not marker.exists()


def test_run_checked_stops_at_first_failure(check_repo):
    (check_repo / "test_fail.py").write_text("def test_fail():\n    assert False\n", encoding="utf-8")
    (check_repo / "test_marker.py").write_text(
        "from pathlib import Path\ndef test_marker():\n    Path('ran').touch()\n", encoding="utf-8",
    )

    result = run_checked(
        "python3 -m pytest -q test_fail.py && python3 -m pytest -q test_marker.py", check_repo,
    )

    assert result.rc == 1
    assert "1 failed" in result.output
    assert not result.timed_out
    assert not (check_repo / "ran").exists()


def test_run_checked_combines_output_and_strips_child_credentials(check_repo, monkeypatch):
    monkeypatch.setenv("FOO_API_KEY", "fake-credential")
    (check_repo / "test_env.py").write_text(
        "import os\nimport sys\n"
        "def test_env():\n"
        "    assert 'FOO_API_KEY' not in os.environ\n"
        "    assert os.environ['ROUTE_WORKER'] == '1'\n"
        "    print('stdout-marker', flush=True)\n"
        "    print('stderr-marker', file=sys.stderr, flush=True)\n", encoding="utf-8",
    )

    result = run_checked(
        "python3 -m pytest -q -s test_env.py && python3 -m pytest -q test_env.py", check_repo,
    )

    assert result.rc == 0
    assert not result.timed_out
    assert "stdout-marker\nstderr-marker" in result.output
    assert result.output.count("1 passed") == 2


def test_run_checked_times_out_sleeping_pytest(check_repo):
    (check_repo / "test_sleep.py").write_text(
        "import time\ndef test_sleep():\n    print('sleep-started', flush=True)\n    time.sleep(30)\n",
        encoding="utf-8",
    )

    result = run_checked("python3 -m pytest -q -s test_sleep.py", check_repo, timeout=2)

    assert result.rc == 124
    assert result.timed_out
    assert "sleep-started" in result.output
    assert result.output.count("sleep-started") == 1


def test_run_checked_truncates_output_keeping_tail(check_repo):
    (check_repo / "test_output.py").write_text(
        "def test_output():\n    print('discard-this-head' + 'x' * 25000 + 'keep-this-tail')\n",
        encoding="utf-8",
    )

    result = run_checked("python3 -m pytest -q -s test_output.py", check_repo)

    assert result.rc == 0
    assert len(result.output) == 20_000
    assert "discard-this-head" not in result.output
    assert "keep-this-tail" in result.output
    assert "1 passed" in result.output


def test_run_checked_resolves_paths_against_supplied_cwd(check_repo):
    test_file = check_repo / "test_absolute.py"
    test_file.write_text("def test_absolute():\n    assert True\n", encoding="utf-8")

    result = run_checked(f"python3 -m pytest -q {shlex.quote(str(test_file))}", check_repo)

    assert result.rc == 0
    assert "1 passed" in result.output


def test_run_checked_timeout_is_shared_across_chain(tmp_path, monkeypatch):
    ticks = iter([0, 1, 4])
    calls = []
    timeouts = []
    monkeypatch.setattr("route.ultra_safety.time.monotonic", lambda: next(ticks))

    class Process:
        returncode = 0

        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, *, timeout):
            timeouts.append(timeout)
            return "ok\n", None

    monkeypatch.setattr("route.ultra_safety.subprocess.Popen", Process)

    result = run_checked("pytest && node --test", tmp_path, timeout=5)

    assert result.rc == 0
    assert result.output == "ok\nok\n"
    assert timeouts == [4, 1]
    assert all(kwargs["shell"] is False for _, kwargs in calls)
    assert all(kwargs["start_new_session"] is True for _, kwargs in calls)
    assert [argv for argv, _ in calls] == [["pytest"], ["node", "--test"]]


def test_run_checked_missing_program_is_a_failed_check(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    result = run_checked("pytest", tmp_path)

    assert result.rc == 127
    assert "could not run check" in result.output
    assert not result.timed_out


@pytest.mark.parametrize("text", [
    "AWS key " + "AKIA" + "ABCDEFGHIJKLMNOP",
    "password: hunter2hunter2",
    "password: 'hunter2hunter2hunter2'",
    "API_KEY=abc123def456ghi789",
    "summarise the customer data export",
    "'db_password' : 'hunter2hunter2'",
    '{"api_key" : "abc123def456ghi789"}',
])
def test_screen_digest_flags_credentials_or_private_data(text):
    reason = screen_digest(text)

    assert reason
    assert text not in reason


@pytest.mark.parametrize("text", [
    "", "Implemented parser tests and checked the README.",
    "Use a server secret for signed links; no credentials in code.",
    "Document password policy and API key rotation.",
    '{"password_policy": "Rotate credentials every 12 months."}',
])
def test_screen_digest_passes_ordinary_text(text):
    reason = screen_digest(text)

    assert reason is None


def test_screen_digest_respects_sensitive_path_config(isolated_env):
    config = isolated_env / "config"
    config.mkdir()
    (config / "sensitive.toml").write_text('paths = ["/private-client/*"]\n', encoding="utf-8")

    reason = screen_digest("Updated /private-client/example/report.txt")

    assert reason
    assert "configured sensitive path" in reason


def test_npm_run_is_limited_to_check_scripts():
    # Arrange
    allowed = ["npm run test", "npm run test:unit", "npm run lint", "npm run typecheck"]
    refused = ["npm run postinstall", "npm run deploy", "npm run", "npm run test; rm"]
    # Act / Assert
    assert all(lint_command(cmd) is None for cmd in allowed)
    assert all(lint_command(cmd) is not None for cmd in refused)


def test_pytest_plugins_can_only_be_disabled():
    # Arrange / Act / Assert
    assert lint_command("python3 -m pytest -q -p no:cacheprovider") is None
    assert lint_command("pytest -p evil_plugin") is not None
    assert lint_command("pytest -pevil_plugin") is not None


@pytest.mark.parametrize("assignment", ["X=plain", "X:=plain", "X+=plain", "X?=plain"])
def test_make_assignments_are_refused_even_without_expansion(assignment):
    command = f"make test {assignment}"

    reason = lint_command(command)

    assert reason is not None


@pytest.mark.parametrize("runner", ["pytest", "python3 -m pytest", "uv run pytest"])
@pytest.mark.parametrize("options", [
    "-oaddopts=-pevil_plugin", "--override-ini addopts=-pevil_plugin",
    "--basetemp src", "--rootdir=src", "--rootdir src",
])
def test_pytest_configuration_and_directory_overrides_are_refused(runner, options):
    command = f"{runner} {options}"

    reason = lint_command(command)

    assert reason is not None
