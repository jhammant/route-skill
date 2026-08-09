"""SPEC test 5: escalation — one hop only; the arm left behind goes negative."""

from __future__ import annotations

from route.outcomes import escalate, record_outcome
from route.router import IMPRESSION_EVENT, RESOLVED_EVENT, REWARD_EVENT, cell_name


def test_escalate_picks_next_stronger_eligible_arm(router, pools):
    nxt = escalate(router, "coding:implement", "moderate", "codex",
                   ["claude", "codex", "kimi"], pools)
    assert nxt == "claude"  # strength 3 -> 4


def test_escalate_skips_ineligible_stronger_arms(router, pools):
    nxt = escalate(router, "coding:implement", "moderate", "local-agent",
                   ["local-agent", "kimi"], pools)
    assert nxt == "kimi"  # codex and claude are stronger but not eligible


def test_escalation_is_capped_at_one_hop(router, pools):
    first = escalate(router, "coding:implement", "moderate", "kimi",
                     ["claude", "codex", "kimi"], pools)
    assert first == "codex"
    second = escalate(router, "coding:implement", "moderate", "codex",
                      ["claude", "codex", "kimi"], pools, already_escalated=True)
    assert second is None


def test_no_stronger_arm_means_no_escalation(router, pools):
    assert escalate(router, "coding:implement", "moderate", "claude",
                    ["claude", "codex"], pools) is None


def test_escalated_from_arm_receives_a_negative_outcome(router, pools, storage):
    shape, tier = "coding:implement", "moderate"
    router.select(shape, tier, ["claude", "codex"], record_impression=False)
    cell = cell_name(shape, tier)
    impressions_before = storage.counts(f"{cell}:{IMPRESSION_EVENT}")["codex"]
    resolved_before = storage.counts(f"{cell}:{RESOLVED_EVENT}")["codex"]
    accepted_before = storage.counts(f"{cell}:{REWARD_EVENT}")["codex"]
    posterior_before = router.posterior(shape, tier)["codex"]

    escalate(router, shape, tier, "codex", ["claude", "codex"], pools)

    assert storage.counts(f"{cell}:{IMPRESSION_EVENT}")["codex"] == impressions_before
    assert storage.counts(f"{cell}:{RESOLVED_EVENT}")["codex"] == resolved_before + 1
    assert storage.counts(f"{cell}:{REWARD_EVENT}")["codex"] == accepted_before
    alpha, beta = posterior_before
    assert router.posterior(shape, tier)["codex"] == (alpha, beta + 1)


def test_recorded_outcomes_move_the_posterior(router):
    shape, tier, arm = "coding:debug", "simple", "claude"
    router.select(shape, tier, [arm], record_impression=False)
    alpha, beta = router.posterior(shape, tier)[arm]

    record_outcome(router, shape, tier, arm, "accepted")
    assert router.posterior(shape, tier)[arm] == (alpha + 1, beta)

    record_outcome(router, shape, tier, arm, "failed")
    assert router.posterior(shape, tier)[arm] == (alpha + 1, beta + 1)


def test_record_outcome_records_cumulative_events(router, storage):
    shape, tier = "coding:debug", "simple"
    record_outcome(router, shape, tier, "claude", "accepted")
    cell = cell_name(shape, tier)
    assert storage.counts(f"{cell}:accepted")["claude"] == 1
    assert storage.counts(f"{cell}:verified")["claude"] == 1
    assert storage.counts(f"{cell}:completed")["claude"] == 1

    record_outcome(router, shape, tier, "claude", "verified")
    assert storage.counts(f"{cell}:accepted")["claude"] == 1  # unchanged
    assert storage.counts(f"{cell}:verified")["claude"] == 2

    record_outcome(router, shape, tier, "claude", "failed")
    assert storage.counts(f"{cell}:completed")["claude"] == 2  # unchanged
