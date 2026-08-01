"""SPEC 3-4: headroom hooks, for the pools quotamax does not track.

An arm behind a vendor with no quota API — or one whose harness is not
installed on this machine at all — has no way to report that it cannot take
work, so the bandit keeps selecting it. A hook reports `critical` and the
existing removal in `apply_quota` does the rest; nothing new is needed in the
gate itself.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from route.eligibility import (
    HEADROOM_HOOKS_DIRNAME,
    apply_quota,
    discover_headroom_hooks,
    fetch_hook_pools,
    fetch_quota,
)
from route.storage import config_dir


def _hook(path: Path, pools: dict[str, str] | None = None, *, body: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(pools or {})
    path.write_text(body or f"#!/bin/sh\ncat <<'EOF'\n{payload}\nEOF\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _in_config_dir(name: str, pools: dict[str, str]) -> Path:
    return _hook(config_dir() / HEADROOM_HOOKS_DIRNAME / name, pools)


# --- discovery ----------------------------------------------------------


def test_no_directory_means_no_hooks():
    assert discover_headroom_hooks() == []


def test_discovers_executables_in_sorted_order():
    _in_config_dir("50-second", {})
    _in_config_dir("10-first", {})
    assert [Path(h).name for h in discover_headroom_hooks()] == [
        "10-first",
        "50-second",
    ]


def test_ignores_non_executable_files():
    directory = config_dir() / HEADROOM_HOOKS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "notes.md").write_text("not a hook")
    assert discover_headroom_hooks() == []


def test_env_override_replaces_the_directory(monkeypatch, tmp_path):
    _in_config_dir("10-ignored", {})
    pinned = _hook(tmp_path / "pinned", {})
    monkeypatch.setenv("ROUTE_HEADROOM_HOOKS", str(pinned))
    assert discover_headroom_hooks() == [str(pinned)]


def test_an_empty_override_disables_hooks(monkeypatch):
    """Explicitly off, distinct from "none configured"."""
    _in_config_dir("10-present", {"agy": "critical"})
    monkeypatch.setenv("ROUTE_HEADROOM_HOOKS", "")
    assert discover_headroom_hooks() == []


# --- collection ---------------------------------------------------------


def test_a_hook_reports_headroom_for_its_pool():
    _in_config_dir("10-presence", {"agy": "critical"})
    assert fetch_hook_pools() == {"agy": "critical"}


def test_later_hooks_refine_earlier_ones():
    """What the name ordering is for: presence first, then a real number."""
    _in_config_dir("10-presence", {"agy": "ok", "pi": "ok"})
    _in_config_dir("50-vendor", {"agy": "low"})
    assert fetch_hook_pools() == {"agy": "low", "pi": "ok"}


def test_a_broken_hook_contributes_nothing_and_spares_the_others():
    """Fail-open per hook: "no opinion", never "unavailable"."""
    _hook(
        config_dir() / HEADROOM_HOOKS_DIRNAME / "10-broken",
        body="#!/bin/sh\necho 'not json'\nexit 1\n",
    )
    _in_config_dir("50-fine", {"pi": "critical"})
    assert fetch_hook_pools() == {"pi": "critical"}


def test_garbage_on_stdout_is_not_a_pool():
    _hook(
        config_dir() / HEADROOM_HOOKS_DIRNAME / "10-garbage",
        body="#!/bin/sh\necho '<html>nope</html>'\n",
    )
    assert fetch_hook_pools() == {}


def test_a_hook_that_hangs_is_abandoned():
    _hook(
        config_dir() / HEADROOM_HOOKS_DIRNAME / "10-slow",
        body="#!/bin/sh\nsleep 5\n",
    )
    assert fetch_hook_pools(timeout=0.2) == {}


# --- through the gate ---------------------------------------------------


def test_hooks_are_consulted_when_quotamax_is_absent(monkeypatch):
    """An arm's availability must not hinge on an unrelated tool existing."""
    _in_config_dir("10-presence", {"agy": "critical"})
    assert fetch_quota(cmd=("definitely-not-installed",)) == {"agy": "critical"}


def test_a_hook_pool_beats_quotamax_on_the_same_name(tmp_path):
    quotamax = _hook(tmp_path / "fake-quotamax", {"kimi": "ok"})
    _in_config_dir("50-kimi", {"kimi": "critical"})
    assert fetch_quota(cmd=(str(quotamax),))["kimi"] == "critical"


def test_a_critical_hook_removes_the_arm():
    """The whole point: no new gate logic, just a pool reporting critical."""
    _in_config_dir("10-presence", {"agy": "critical"})
    quota = fetch_quota(cmd=("definitely-not-installed",))
    assert apply_quota(["agy", "claude"], quota, quota_pool={"agy": "agy"}) == [
        "claude"
    ]


def test_no_hooks_leaves_quota_exactly_as_quotamax_reported(tmp_path):
    quotamax = _hook(tmp_path / "fake-quotamax", {"kimi": "low"})
    assert fetch_quota(cmd=(str(quotamax),)) == {"kimi": "low"}
