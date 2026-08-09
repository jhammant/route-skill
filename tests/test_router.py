"""SPEC test 4: seeded priors behave like the rules; evidence flips them."""

from __future__ import annotations

from route.router import IMPRESSION_EVENT, RESOLVED_EVENT, REWARD_EVENT, cell_name


def test_cold_start_matches_the_static_rule_table(router):
    """Zero observations: selection is one of the rule-favoured arms."""
    shape, tier = "coding:refactor", "hard"
    eligible = ["claude", "codex", "kimi"]
    chosen = router.select(shape, tier, eligible)
    assert chosen in router.priors.favoured_arms(shape)  # ["claude", "codex"]
    assert chosen != "kimi"


def test_seeded_priors_are_beta_8_2_and_beta_2_8(router, storage):
    shape, tier = "coding:test", "moderate"
    router.select(shape, tier, ["claude", "codex", "kimi"], record_impression=False)
    posterior = router.posterior(shape, tier)
    resolved = storage.counts(f"{cell_name(shape, tier)}:{RESOLVED_EVENT}")
    favoured = set(router.priors.favoured_arms(shape))  # codex, kimi
    for arm, (alpha, beta) in posterior.items():
        assert resolved[arm] == 8
        if arm in favoured:
            assert (alpha, beta) == (8.0, 2.0), arm
        else:
            assert (alpha, beta) == (2.0, 8.0), arm


def test_zero_observations_after_seeding(router):
    """Seed pseudo-counts are priors, not evidence — observed is empty."""
    router.select("coding:debug", "trivial", ["claude", "codex"], record_impression=False)
    assert router.observed_counts("coding:debug", "trivial") == {}


def test_dispatch_without_outcome_does_not_move_posterior(router, storage, monkeypatch):
    shape, tier, arm = "coding:test", "moderate", "codex"
    samples = []

    def sample_mean(alpha, beta):
        samples.append((alpha, beta))
        return alpha / (alpha + beta)

    monkeypatch.setattr(router._rng, "betavariate", sample_mean)
    router.select(shape, tier, [arm], record_impression=False)
    cell = cell_name(shape, tier)
    before = router.posterior(shape, tier)[arm]
    impressions_before = storage.counts(f"{cell}:{IMPRESSION_EVENT}")[arm]
    resolved_before = storage.counts(f"{cell}:{RESOLVED_EVENT}")[arm]
    samples.clear()

    assert router.select(shape, tier, [arm]) == arm
    assert router.select(shape, tier, [arm], record_impression=False) == arm

    assert samples == [before, before]
    assert storage.counts(f"{cell}:{IMPRESSION_EVENT}")[arm] == impressions_before + 1
    assert storage.counts(f"{cell}:{RESOLVED_EVENT}")[arm] == resolved_before
    assert router.posterior(shape, tier)[arm] == before
    assert router.observed_counts(shape, tier) == {}


def test_enough_successes_flip_selection_to_the_unfavoured_arm(router):
    shape, tier = "coding:refactor", "hard"
    eligible = ["claude", "codex", "kimi"]
    before = router.select(shape, tier, eligible, record_impression=False)
    assert before in ("claude", "codex")

    for _ in range(20):
        router.reward(shape, tier, "kimi", REWARD_EVENT)

    after = router.select(shape, tier, eligible, record_impression=False)
    assert after == "kimi"
    # And the flip shows up as real observations, not prior tweaking.
    alpha, beta = router.observed_counts(shape, tier)["kimi"]
    assert alpha == 20


def test_priors_seeded_only_once(router, storage):
    shape, tier = "batch:classify", "simple"
    eligible = ["claude", "local-batch"]
    router.select(shape, tier, eligible, record_impression=False)
    counts_after_first = storage.counts(f"{cell_name(shape, tier)}:impression")
    router.select(shape, tier, eligible, record_impression=False)
    counts_after_second = storage.counts(f"{cell_name(shape, tier)}:impression")
    assert counts_after_first == counts_after_second


def test_cells_learn_independently(router):
    """route:coding:refactor:hard is not route:batch:classify:trivial."""
    router.select("coding:refactor", "hard", ["claude", "codex"], record_impression=False)
    assert router.posterior("batch:classify", "trivial") == {}
