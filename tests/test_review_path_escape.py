"""Tools expand ~ and attached -C.. themselves, so the repo boundary must catch both."""
import pytest

from route.ultra_safety import lint_command, run_checked


@pytest.mark.parametrize("command", [
    "make test -C..", "make check -C ~", "make test -C ~/elsewhere", "npm test --prefix=~/other",
])
def test_parent_and_home_directories_are_refused(command, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert lint_command(command) is not None


def test_make_tilde_does_not_run_a_makefile_outside_the_repo(tmp_path, monkeypatch):
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    (home / "Makefile").write_text(f"test:\n\ttouch {home / 'ran'}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    run_checked("make test -C ~", repo)
    assert not (home / "ran").exists()
