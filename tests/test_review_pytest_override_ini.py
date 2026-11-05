"""-o addopts=... re-adds -p NAME after the -p narrowing has looked at argv."""
import pytest

from route.ultra_safety import lint_command


@pytest.mark.parametrize("command", [
    "pytest -o addopts=-pevil_plugin",
    "pytest --override-ini=addopts=-pevil_plugin",
    "python3 -m pytest -o 'addopts=-p evil_plugin'",
])
def test_override_ini_cannot_load_a_plugin(command, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert lint_command(command) is not None
