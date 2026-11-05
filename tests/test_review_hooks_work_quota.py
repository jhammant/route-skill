"""Regression: a live work-account probe must gate the host arm on the account it reports."""

from pathlib import Path
from unittest.mock import patch

from route import eligibility as e


def test_live_work_account_critical_quota_is_enforced():
    agent = {"ok": True, "headroom": "critical", "backstop": {"available": False}}
    with patch.dict(e.os.environ, {"CLAUDE_WORK_ACCOUNT": "1"}), \
         patch("route.storage.state_dir", return_value=Path("/unused-route-review")), \
         patch.object(Path, "read_text", side_effect=FileNotFoundError), \
         patch.object(e, "_run_json", side_effect=lambda cmd, timeout: agent if cmd[1] == "agent" else None):
        quota = e.fetch_quota(cache_result=False)
        assert e.apply_quota(["claude"], quota, quota_pool={"claude": e.quota_keys("claude")}) == [], \
            "Known exhausted work account is still eligible"
