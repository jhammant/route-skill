"""Usage caps: reading them off a model's output, and remembering them until they reset."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from route import caps, eligibility
from route.caps import Cap, detect_cap, parse_reset
from route.cli import main
from route.eligibility import gate, gate_keys
from route.pools import Model, Pool
from route.ultra_select import candidates, pick

#: 18 Nov 2026 18:39 UTC, when the planner in the incident printed the message below.
NOW = datetime(2026, 11, 18, 18, 39, tzinfo=timezone.utc).timestamp()
#: 23 Nov 2026 15:00 Europe/London, the reset it named.
RESET = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc).timestamp()
FABLE_LIMIT = ("You've hit your Fable limit · resets Nov 23 at 3pm (Europe/London). Switch to another model, or "
               "manage usage credits at https://claude.ai/settings/usage?from=cc_cli_limit_message, to continue.\n")
CODING = ("coding:implement", "coding:review")


@pytest.fixture
def arms():
    """The second Claude account's Fable and Opus arms, and Codex, as pools.toml declares them."""
    return {
        "claude-work-fable": Pool(name="claude-work-fable", shapes=CODING, model=Model(name="claude-fable-5-2"),
                                  quota_provider="claude-work", quota_scope="Fable", dispatch="x {task}"),
        "claude-work": Pool(name="claude-work", shapes=CODING, model=Model(name="claude-opus-5-5"),
                            quota_provider="claude-work", dispatch="x {task}"),
        "codex": Pool(name="codex", shapes=CODING, model=Model(name="gpt-6-astra"), dispatch="x {task}"),
    }


@pytest.fixture
def clock(monkeypatch):
    """Hold the date at the incident; move it by assigning ``clock[0]``."""
    now = [NOW]
    monkeypatch.setattr(caps, "_clock", lambda: now[0])
    return now


# -- detection -------------------------------------------------------------------

def test_the_exact_message_from_the_failed_run_is_a_fable_cap_with_its_reset():
    # Act
    cap = detect_cap(FABLE_LIMIT, 1, now=NOW)
    # Assert
    assert cap is not None
    assert (cap.model, cap.window, cap.resets) == ("Fable", "", "Nov 23 15:00")
    assert cap.resets_at == RESET
    assert cap.message.startswith("You've hit your Fable limit")


@pytest.mark.parametrize("text, model, window, resets", [
    ("You’ve hit your session limit · resets 11:30pm (Europe/London)\n", "", "session", "Nov 18 23:30"),
    ("You've hit your weekly limit · resets Nov 23 at 3pm (Europe/London)\n", "", "weekly", "Nov 23 15:00"),
    ("You've reached your usage limit.\n", "", "usage", ""),
    ("Usage limit reached\n", "", "usage", ""),
    ("Claude AI usage limit reached|1795446000\n", "", "usage", None),
    ("5-hour limit reached ∙ resets 9pm (Europe/London)\n", "", "5-hour", "Nov 18 21:00"),
    ("Weekly limit reached · resets Mon 9am (Europe/London)\n", "", "weekly", "Nov 23 09:00"),
    ("Opus weekly limit reached ∙ resets Nov 20, 9am (Europe/London)\n", "Opus", "weekly", "Nov 20 09:00"),
    ("You're out of extra usage · resets Nov 23 at 3pm (Europe/London)\n", "", "usage", "Nov 23 15:00"),
    ("Limit reached · resets 3pm (Europe/London)\n", "", "", "Nov 19 15:00"),
    ('{"type":"result","is_error":true,"result":"You\'ve hit your Fable limit · resets Nov 23 at 3pm '
     '(Europe/London)."}\n', "Fable", "", "Nov 23 15:00"),
], ids=["session", "weekly", "reached-usage", "usage-reached", "old-epoch", "five-hour", "weekly-weekday",
        "opus-weekly", "extra-usage", "bare-limit", "json-result"])
def test_claude_code_limit_messages_are_caps(text, model, window, resets):
    # Act
    cap = detect_cap(text, 1, now=NOW)
    # Assert
    assert cap is not None, text
    assert (cap.model, cap.window) == (model, window)
    if resets is not None:
        assert cap.resets == resets
    else:
        assert cap.resets_at == RESET


@pytest.mark.parametrize("text, window, seconds", [
    ("thinking\n" * 400 + "ERROR: You've hit your usage limit. Upgrade to Pro (https://openai.com/chatgpt/pricing) "
     "or try again in 3 days 4 hours 5 minutes.\n", "usage", 3 * 86400 + 4 * 3600 + 5 * 60),
    ("[2026-11-18T18:39:12] ERROR: You've hit your usage limit. Try again in 2h 30m.\n", "usage", 9000),
    ("stream error: exceeded retry limit, last status: 429 Too Many Requests\n", "rate", None),
    ('error: {"type": "usage_limit_reached", "message": "The usage limit has been reached"}\n', "usage", None),
    ("Rate limit reached for gpt-6-astra. Please try again in 20s.\n", "rate", 20),
], ids=["usage-after-transcript", "usage-timestamped", "retry-429", "usage-limit-reached-type", "rate-limit"])
def test_codex_limit_messages_are_caps(text, window, seconds):
    # Act
    cap = detect_cap(text, 1, now=NOW)
    # Assert
    assert cap is not None, text
    assert cap.window == window and cap.model == ""
    assert cap.resets_at == (None if seconds is None else NOW + seconds)


def test_a_cap_printed_with_exit_zero_still_counts_when_it_is_all_the_cli_said():
    assert detect_cap(FABLE_LIMIT, 0, now=NOW) is not None
    assert detect_cap(FABLE_LIMIT, None, now=NOW) is not None


@pytest.mark.parametrize("text, rc", [
    ("", 1),
    ("ULTRA-MANIFEST-BEGIN\n{}\nULTRA-MANIFEST-END\n", 0),
    # A worker describing a change to this very feature, in a short final message.
    ("Done.\n- Usage limit reached handling is in caps.py\n- weekly limit reached is covered by a test\n", 0),
    # Quoted in prose rather than printed by the CLI.
    ("The detector reads lines such as: You've hit your Fable limit\n", 1),
    # A model that exited 0 after a long answer was not stopped by a cap, whatever it quotes last.
    ("review notes\n" * 400 + FABLE_LIMIT, 0),
    # The message is long gone by the end of a transcript that then failed for another reason.
    (FABLE_LIMIT + "more work\n" * 40 + "error: tests failed\n", 1),
], ids=["empty", "a-plan", "worker-describing-the-feature", "quoted-in-prose", "long-answer-exit-0",
        "not-at-the-end"])
def test_ordinary_output_is_not_a_cap(text, rc):
    assert detect_cap(text, rc, now=NOW) is None


@pytest.mark.parametrize("text, expected", [
    ("resets Nov 23 at 3pm (Europe/London).", datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)),
    ("resets 3:05 PM (America/New_York)", datetime(2026, 11, 18, 20, 5, tzinfo=timezone.utc)),
    ("resets Jan 2 at 9am (Europe/London)", datetime(2027, 1, 2, 9, 0, tzinfo=timezone.utc)),
    ("try again at Nov 23rd, 2026 3:00 PM (UTC).", datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)),
    ("resets 2026-11-23T15:00:00Z", datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)),
    ("try again in 1 week", datetime(2026, 11, 25, 18, 39, tzinfo=timezone.utc)),
], ids=["month-day-zone", "clock-other-zone", "next-year", "codex-dated", "iso", "relative"])
def test_resets_are_read_in_the_zone_the_message_names(text, expected):
    stamp, _ = parse_reset(text, now=NOW)
    assert stamp == expected.timestamp()


def test_an_unreadable_reset_keeps_the_cli_s_own_words():
    assert parse_reset("resets when the moon is full. Switch model.", now=NOW) == (None, "when the moon is full")
    assert parse_reset("nothing about a reset here", now=NOW) == (None, "")


# -- what a cap exhausts -----------------------------------------------------------

def test_a_model_cap_spends_the_model_window_and_an_unnamed_cap_spends_the_account(arms):
    fable, weekly = Cap(model="Fable"), Cap(window="weekly")
    # A Fable cap on the Fable arm is that account's Fable window: the Opus arm keeps going.
    assert caps.cap_keys("claude-work-fable", arms, fable) == ["claude-work:Fable", "arm:claude-work-fable"]
    assert caps.exhausted_arms(caps.cap_keys("claude-work-fable", arms, fable), arms) == ["claude-work-fable"]
    # A weekly cap is the account's: every arm that spends it is out.
    keys = caps.cap_keys("claude-work-fable", arms, weekly)
    assert keys == ["claude-work", "arm:claude-work-fable"]
    assert caps.exhausted_arms(keys, arms) == ["claude-work-fable", "claude-work"]
    # An arm with no declared window is still removed by name.
    assert caps.cap_keys("claude-work", arms, Cap(model="Opus")) == ["claude-work:Opus", "arm:claude-work"]
    assert caps.cap_keys("codex", arms, Cap(window="usage")) == ["codex", "arm:codex"]


def test_marked_caps_remove_the_arm_from_ultra_selection_and_the_next_rank_takes_over(arms):
    # Arrange
    quota = {"claude-work": "ok", "codex": "ok"}
    assert pick("planner", arms, quota) == "claude-work-fable"
    # Act
    caps.mark(quota, caps.cap_keys("claude-work-fable", arms, Cap(model="Fable")))
    # Assert
    assert candidates(arms, quota) == ["claude-work", "codex"]
    assert pick("planner", arms, quota) == "claude-work"
    assert gate_keys("claude-work-fable", arms) == ["claude-work", "claude-work:Fable", "arm:claude-work-fable"]


# -- the record and its expiry -----------------------------------------------------

def test_a_recorded_cap_gates_later_quota_snapshots_until_it_resets(arms, clock, monkeypatch):
    # Arrange: quotamax reports the account as fine and knows no Fable window, as on the day.
    monkeypatch.setattr(eligibility, "_run_json", lambda cmd, timeout: (
        {"ok": True, "headroom": "ok"} if cmd[1] == "agent" else
        [{"id": "claude-work", "ok": True, "limits": [{"label": "weekly", "percent": 60}]}]))
    # Act
    record = caps.record_cap("claude-work-fable", arms, detect_cap(FABLE_LIMIT, 1), source="ultra:run-1:planner")
    quota = eligibility.fetch_quota()
    # Assert: the record, on disk, and laid over the snapshot.
    assert record["keys"] == ["claude-work:Fable", "arm:claude-work-fable"] and record["until"] == RESET
    stored = json.loads(caps.caps_path().read_text())["caps"]
    assert [(row["arm"], row["model"], row["resets"], row["source"]) for row in stored] == [
        ("claude-work-fable", "Fable", "Nov 23 15:00", "ultra:run-1:planner")]
    assert quota["claude-work"] == "ok"
    assert quota["claude-work:Fable"] == "critical" and quota["arm:claude-work-fable"] == "critical"
    assert [row["arm"] for row in quota.caps] == ["claude-work-fable"]
    assert candidates(arms, quota) == ["claude-work", "codex"]
    # /route and /auto take the same gate: the Fable arm is removed by quota, the Opus arm is not.
    result = gate("add a retry to the client", "coding:implement", {**arms, "claude": Pool("claude", CODING)},
                  quota=quota)
    assert result.removed_by_quota == ["claude-work-fable"] and "claude-work" in result.eligible

    # Act: the reset passes.
    clock[0] = RESET + 1
    later = eligibility.fetch_quota(cache_result=False)
    # Assert: the window is back.
    assert "claude-work:Fable" not in later and "arm:claude-work-fable" not in later and later.caps == []
    assert candidates(arms, later)[0] == "claude-work-fable"


def test_a_cap_with_no_readable_reset_is_remembered_for_an_hour(arms, clock):
    # Act
    record = caps.record_cap("codex", arms, detect_cap("Usage limit reached\n", 1))
    # Assert
    assert record["until"] == NOW + caps.DEFAULT_SECONDS
    assert [row["arm"] for row in caps.active_caps()] == ["codex"]
    clock[0] = NOW + caps.DEFAULT_SECONDS + 1
    assert caps.active_caps() == []


def test_a_newer_cap_for_the_same_arm_replaces_the_older_record(arms, clock):
    caps.record_cap("claude-work-fable", arms, Cap(model="Fable", resets_at=NOW + 100))
    caps.record_cap("claude-work-fable", arms, Cap(window="weekly", resets_at=NOW + 900))
    caps.record_cap("codex", arms, Cap(window="usage", resets_at=NOW + 50))

    rows = {row["arm"]: row for row in caps.active_caps()}

    assert rows["claude-work-fable"]["keys"] == ["claude-work", "arm:claude-work-fable"]
    assert rows["claude-work-fable"]["until"] == NOW + 900 and set(rows) == {"claude-work-fable", "codex"}


def test_the_host_arm_s_own_key_never_crosses_between_accounts(arms, clock, monkeypatch):
    # Arrange: inside a claude-work session the arm called claude spends the second account.
    pools = {**arms, "claude": Pool("claude", CODING, model=Model(name="claude"))}
    monkeypatch.setenv("CLAUDE_WORK_ACCOUNT", "1")
    # Act
    record = caps.record_cap("claude", pools, Cap(model="Fable", resets_at=RESET))
    # Assert: the run that saw it drops the arm; what is kept names only the account's window.
    assert record["keys"] == ["claude-work:Fable", "arm:claude"]
    assert json.loads(caps.caps_path().read_text())["caps"][0]["keys"] == ["claude-work:Fable"]
    monkeypatch.delenv("CLAUDE_WORK_ACCOUNT")
    quota: dict = {}
    caps.apply_caps(quota)
    assert quota == {"claude-work:Fable": "critical"}


# -- claude-swarm's record -----------------------------------------------------------

def swarm_state(account: str, cooling: dict) -> None:
    path = Path(os.environ["CLAUDE_SWARM_STATE"]) / "quota" / f"{account}.cool.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cooling))


def test_a_limit_claude_swarm_recorded_is_honoured_before_any_arm_is_chosen(arms, clock):
    # Arrange: what `claude-swarm accounts` showed as "limit hit: fable" on the day.
    swarm_state("work", {"fable": RESET, "sonnet": NOW - 5})
    quota = {"claude-work": "ok", "codex": "ok"}
    # Act
    rows = caps.apply_caps(quota)
    # Assert: only the live limit, as the Fable window of the account claude-swarm calls "work".
    assert [(row["keys"], row["source"], row["resets_at"]) for row in rows] == [
        (["claude-work:Fable"], "claude-swarm:work", RESET)]
    assert quota["claude-work:Fable"] == "critical"
    assert pick("planner", arms, quota) == "claude-work"


def test_a_whole_account_limit_from_claude_swarm_removes_every_arm_on_it(arms, clock):
    swarm_state("work", {"*": RESET})
    quota: dict = {}

    caps.apply_caps(quota)

    assert quota == {"claude-work": "critical"}
    assert candidates(arms, quota) == ["codex"]


def test_claude_swarm_accounts_map_by_login_and_unreadable_state_is_ignored(arms, clock):
    # Arrange: the claude-work login under another name, plus an account route has no pool for.
    config = Path(os.environ["CLAUDE_SWARM_CONFIG"])
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"accounts": [{"name": "second", "keychain": "claude-work-oauth"},
                                               {"name": "third", "keychain": "something-else"}]}))
    swarm_state("second", {"fable": RESET, "opus": "soon", "haiku": True})
    swarm_state("third", {"fable": RESET})
    swarm_state("work", {"fable": RESET})
    # Act
    rows = caps.swarm_caps()
    # Assert
    assert [(row["source"], row["keys"]) for row in rows] == [("claude-swarm:second", ["claude-work:Fable"])]
    (Path(os.environ["CLAUDE_SWARM_STATE"]) / "quota" / "second.cool.json").write_text("not json")
    assert caps.swarm_caps() == []


# -- route caps ----------------------------------------------------------------------

def test_route_caps_lists_and_clears_the_records(arms, clock, capsys):
    # Arrange
    caps.record_cap("claude-work-fable", arms, detect_cap(FABLE_LIMIT, 1), source="ultra:run-1:planner")
    swarm_state("work", {"*": RESET})
    # Act
    assert main(["caps"]) == 0
    listing = capsys.readouterr().out
    assert main(["caps", "--clear", "claude-work-fable"]) == 0
    cleared = capsys.readouterr().out
    assert main(["caps", "--json"]) == 0
    left = json.loads(capsys.readouterr().out)
    # Assert: both sources are listed; clearing forgets route's own record and leaves claude-swarm's.
    assert "claude-work:Fable, arm:claude-work-fable\tFable\tNov 23 15:00\tultra:run-1:planner" in listing
    assert f"claude-work\tusage\t{caps.format_reset(RESET)}\tclaude-swarm:work" in listing
    assert cleared.strip() == "cleared 1 usage cap record(s): claude-work-fable"
    assert [row["source"] for row in left] == ["claude-swarm:work"]


def test_route_caps_says_so_when_there_is_nothing(capsys):
    assert main(["caps"]) == 0
    assert capsys.readouterr().out.strip() == "no usage caps on record"


# -- /route and /auto ------------------------------------------------------------------

def test_a_routed_plan_skips_a_capped_arm_and_says_which_cap_removed_it(clock, monkeypatch, capsys):
    # Arrange: quotamax sees Codex as nearly idle, but a Codex run has just said it is out of usage.
    from route.pools import load_pools
    from route.router import Router
    from tests.conftest import MeanRNG

    monkeypatch.setattr(eligibility, "_run_json", lambda cmd, timeout: (
        {"ok": True, "headroom": "constrained", "backstop": {"available": True, "low": False}}
        if cmd[1] == "agent" else [{"id": "codex", "ok": True, "limits": [{"label": "weekly", "percent": 7}]}]))
    monkeypatch.setattr("route.cli._build_router", lambda: Router(rng=MeanRNG()))
    monkeypatch.setattr("route.complexity._estimate_taskgauge", lambda text: "trivial")
    args = ["--plan-only", "--no-llm", "build the trial SMS notices feature with a signed redirect route"]
    assert main(args) == 0
    before = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    # Act
    caps.record_cap("codex", load_pools(), detect_cap(
        "ERROR: You've hit your usage limit. Try again in 3 days.\n", 1), source="swarm:s1:alpha")
    assert main(args) == 0
    after = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    # Assert
    assert "codex" in before["eligible"] and "usage_caps" not in before
    assert after["removed_by_quota"] == ["codex"] and "codex" not in after["eligible"]
    assert after["usage_caps"] == [{"removes": ["codex", "arm:codex"], "seen_by": "swarm:s1:alpha",
                                    "resets": caps.format_reset(NOW + 3 * 86400)}]


@pytest.mark.parametrize("content", ["not json", "[]", '{"caps": 7}', '{"caps": [3, {"until": "soon"}, {"keys": 1}]}'])
def test_a_damaged_record_file_never_breaks_routing(content, clock):
    # Arrange
    caps.caps_path().parent.mkdir(parents=True, exist_ok=True)
    caps.caps_path().write_text(content)
    quota = {"codex": "ok"}
    # Act
    rows = caps.apply_caps(quota)
    # Assert
    assert rows == [] and quota == {"codex": "ok"}
