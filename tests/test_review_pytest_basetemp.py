"""pytest rm -rf's --basetemp before first use, so it must not name repo content."""
import os
import sys
from pathlib import Path

from route.ultra_safety import lint_command, run_checked


def test_basetemp_inside_repo_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert lint_command("python3 -m pytest --basetemp=src") is not None


def test_basetemp_check_does_not_delete_source(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "keep.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "test_t.py").write_text("def test_t(tmp_path):\n    assert tmp_path.exists()\n", encoding="utf-8")
    run_checked("python3 -m pytest -q -p no:cacheprovider --basetemp=src test_t.py", tmp_path)
    assert (tmp_path / "src" / "keep.py").exists()
