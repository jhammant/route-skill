"""make expands ${shell ...} in a command-line := assignment, so it runs any command."""
from route.ultra_safety import lint_command, run_checked


def test_make_assignment_with_shell_function_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert lint_command("make test 'X:=${shell touch pwned}'") is not None


def test_make_assignment_does_not_execute(tmp_path):
    run_checked("make test 'X:=${shell touch pwned}'", tmp_path)
    assert not (tmp_path / "pwned").exists()
