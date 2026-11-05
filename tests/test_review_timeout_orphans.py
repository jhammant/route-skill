"""A timed-out check must not leave its children running against the repo."""
import time

from route.ultra_safety import run_checked


def test_timeout_kills_the_whole_process_tree(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "slow.sh").write_text("sh -c 'sleep 3; touch late'\necho done\n", encoding="utf-8")
    result = run_checked("bash tests/slow.sh", tmp_path, timeout=1)
    time.sleep(4)
    assert result.timed_out
    assert not (tmp_path / "late").exists()
