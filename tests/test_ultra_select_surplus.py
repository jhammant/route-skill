"""Ultra spends the account whose week would otherwise expire unused, never a weaker model."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from route.eligibility import QuotaSnapshot, quota_keys
from route.pools import load_pools
from route.storage import config_dir
from route.surplus import DEFAULT_K, quota_adjustment
from route.ultra_select import candidates, pick, surplus_adjustment

DAYS_LEFT = 3 * 24 * 60
SURPLUS = {"percentUsed": 10, "resetsInMinutes": DAYS_LEFT}
PRESSURE = {"percentUsed": 90, "resetsInMinutes": DAYS_LEFT}
HEADROOM = {"claude-work": "ok", "codex": "ok", "claude": "critical"}


@pytest.fixture
def ultra_pools():
    example = Path(__file__).resolve().parent.parent / "examples" / "claude-work-pools.toml"
    return load_pools(example)


def snapshot(weekly: dict[str, dict], headroom: dict[str, str] = HEADROOM) -> QuotaSnapshot:
    return QuotaSnapshot(dict(headroom), weekly)


@pytest.mark.parametrize("weekly,host,expected", [
    ({"codex": SURPLUS}, "claude", ["claude-work-fable", "codex", "claude-work"]),
    ({"claude-work": SURPLUS}, "claude-work", ["claude-work-fable", "claude-work", "codex"]),
    ({"claude-work": {"percentUsed": 40, "resetsInMinutes": DAYS_LEFT}, "codex": SURPLUS},
     "claude", ["claude-work-fable", "codex", "claude-work"]),
    ({"claude-work": PRESSURE}, "claude", ["claude-work-fable", "codex", "claude-work"]),
    ({"codex": PRESSURE}, "codex", ["claude-work-fable", "claude-work", "codex"]),
])
def test_equal_ranks_spend_the_surplus_account_first_and_the_pressured_one_last(
        ultra_pools, weekly, host, expected):
    """Opus and Codex are both rank two, so the weekly pace decides which week gets used."""
    quota = snapshot(weekly)

    result = candidates(ultra_pools, quota, host=host)

    assert result == expected


def test_rank_dominates_surplus(ultra_pools):
    """A spare week must never buy a weaker model ahead of Fable, even a pressured Fable."""
    quota = snapshot({"claude-work:Fable": {"percentUsed": 95, "resetsInMinutes": DAYS_LEFT},
                      "codex": {"percentUsed": 0, "resetsInMinutes": DAYS_LEFT}})

    result = candidates(ultra_pools, quota)

    assert result == ["claude-work-fable", "codex", "claude-work"]
    assert surplus_adjustment("claude-work-fable", ultra_pools, quota) < 0
    assert surplus_adjustment("codex", ultra_pools, quota) == DEFAULT_K


def test_low_headroom_sorts_after_ok_whatever_the_surplus(ultra_pools):
    """Low headroom is a live gate; projected surplus is only a tie-break below it."""
    quota = snapshot({"claude-work": {"percentUsed": 0, "resetsInMinutes": DAYS_LEFT},
                      "codex": PRESSURE},
                     {"claude-work": "low", "codex": "ok", "claude": "critical"})

    result = candidates(ultra_pools, quota)

    assert result == ["claude-work-fable", "codex", "claude-work"]


@pytest.mark.parametrize("headroom", [
    {},
    HEADROOM,
    {"claude-work": "low"},
    {"codex": "constrained", "claude-work:Fable": "low"},
])
def test_no_weekly_windows_keep_the_old_order(ultra_pools, headroom):
    """Callers without quotamax weekly data (plain dicts, failed probes) see no reordering."""
    plain = dict(headroom)
    empty = QuotaSnapshot(dict(headroom), {})

    old = candidates(ultra_pools, plain, allow_lower=True)
    result = candidates(ultra_pools, empty, allow_lower=True)

    assert result == old
    assert all(surplus_adjustment(arm, ultra_pools, quota) == 0.0
               for arm in ultra_pools for quota in (plain, empty))


def test_plain_dict_quota_never_reads_config(ultra_pools):
    """Without windows there is nothing to pace, so a bad surplus.k cannot break selection."""
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "config.toml").write_text('[surplus]\nk = "bad"\n', encoding="utf-8")

    result = candidates(ultra_pools, dict(HEADROOM))

    assert result == ["claude-work-fable", "claude-work", "codex"]
    with pytest.raises(ValueError, match="surplus.k"):
        candidates(ultra_pools, snapshot({"codex": SURPLUS}))


def test_zero_k_in_config_turns_the_tie_break_off(ultra_pools):
    """Ultra honours the same [surplus] k as the router, so k = 0 restores the old order."""
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "config.toml").write_text("[surplus]\nk = 0\n", encoding="utf-8")
    quota = snapshot({"codex": SURPLUS, "claude-work": PRESSURE})

    result = candidates(ultra_pools, quota)

    assert result == ["claude-work-fable", "claude-work", "codex"]


def test_model_scoped_arm_is_paced_by_the_tighter_window(ultra_pools):
    """A nearly spent Fable week must not borrow the account's spare week."""
    quota = QuotaSnapshot({"claude-work": "ok"}, {
        "claude-work": {"percentUsed": 10, "resetsInMinutes": DAYS_LEFT},
        "claude-work:Fable": {"percentUsed": 95, "resetsInMinutes": DAYS_LEFT},
    })

    fable = surplus_adjustment("claude-work-fable", ultra_pools, quota)
    opus = surplus_adjustment("claude-work", ultra_pools, quota)

    assert fable == quota_adjustment(95, DAYS_LEFT, DEFAULT_K, headroom="ok")
    assert fable < 0
    assert opus == quota_adjustment(10, DAYS_LEFT, DEFAULT_K, headroom="ok")
    assert opus > 0


def test_reviewer_keeps_family_independence_over_surplus(ultra_pools):
    """Independent review outranks spending spare quota on the author's own family."""
    quota = snapshot({"claude-work:Fable": {"percentUsed": 5, "resetsInMinutes": DAYS_LEFT},
                      "codex": PRESSURE})
    adjustments = {arm: surplus_adjustment(arm, ultra_pools, quota)
                   for arm in candidates(ultra_pools, quota)}

    result = pick("reviewer", ultra_pools, quota, author_arm="claude-work")

    assert result == "codex"
    assert max(adjustments, key=adjustments.get) == "claude-work-fable"


@pytest.mark.parametrize("arm", ["local-agent", "codex"])
def test_untracked_arm_has_no_adjustment(ultra_pools, arm):
    """An arm with no quota pool has no week to waste or protect."""
    ultra_pools["codex"] = replace(ultra_pools["codex"], quota_provider="")
    quota = QuotaSnapshot({"codex": "constrained", "claude-work": "ok"},
                          {"codex": PRESSURE, "claude-work": SURPLUS})

    result = surplus_adjustment(arm, ultra_pools, quota)

    assert quota_keys(arm, ultra_pools) == []
    assert result == 0.0
