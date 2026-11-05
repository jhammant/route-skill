"""Ultra keeps quality and review independence deterministic across quota changes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from route.pools import Model, Pool, load_pools, pool_family, pools_for_host
from route.ultra_select import (
    ULTRA_MIN_RANK,
    candidates,
    dispatch_template,
    model_rank,
    pick,
    review_independence,
)


@pytest.fixture
def ultra_pools():
    example = Path(__file__).resolve().parent.parent / "examples" / "claude-work-pools.toml"
    return load_pools(example)


@pytest.mark.parametrize("arm,family", [
    ("claude", "anthropic"),
    ("codex", "openai"),
    ("kimi", "moonshot"),
    ("deepseek", "deepseek"),
    ("local-batch", "local"),
    ("local-agent", "local"),
    ("free", "free"),
])
def test_every_shipped_pool_has_a_family(pools, arm, family):
    pool = pools[arm]

    result = pool_family(pool)

    assert pool.family == ""
    assert result == family


@pytest.mark.parametrize("arm", ["claude-work", "claude-work-fable", "claude-work-sonnet"])
def test_shipped_account_arms_share_the_anthropic_family(ultra_pools, arm):
    pool = ultra_pools[arm]

    result = pool_family(pool)

    assert result == "anthropic"


@pytest.mark.parametrize("name,model,cost,family", [
    ("custom", "claude-opus-5-5", "included", "anthropic"),
    ("claude-custom", "unknown", "included", "anthropic"),
    ("custom", "GPT-6-ASTRA", "included", "openai"),
    ("custom", "codex-mini", "included", "openai"),
    ("custom", "o3", "included", "openai"),
    ("custom", "o4-mini", "included", "openai"),
    ("codex", "unknown", "included", "openai"),
    ("custom", "kimi-k3.1", "included", "moonshot"),
    ("kimi-custom", "unknown", "included", "moonshot"),
    ("custom", "deepseek-v4", "included", "deepseek"),
    ("deepseek-custom", "unknown", "included", "deepseek"),
    ("custom", "qwen", "local", "local"),
    ("custom", "gpt-6-astra", "local", "openai"),
    ("custom", "other-model", "included", "other"),
])
def test_family_inference_for_custom_arms(name, model, cost, family):
    pool = Pool(name=name, shapes=(), model=Model(name=model), cost=cost)

    result = pool_family(pool)

    assert result == family


def test_explicit_family_from_pools_toml_wins_over_inference(tmp_path):
    config = tmp_path / "pools.toml"
    config.write_text('[pools.claude-custom]\nfamily = "custom-lineage"\n'
                      'model = { name = "gpt-6-astra" }\n', encoding="utf-8")

    pool = load_pools(config)["claude-custom"]

    assert pool.family == "custom-lineage"
    assert pool_family(pool) == "custom-lineage"


@pytest.mark.parametrize("name,model,rank", [
    ("custom", "claude-fable-5-1", 3),
    ("codex", "FABLE", 3),
    ("custom", "claude-opus-5-5", 2),
    ("claude-work", "unknown", 2),
    ("claude", "unknown", 2),
    ("codex", "gpt-6-astra", 2),
    ("custom", "claude-sonnet-5-5", 1),
    ("custom", "kimi-k3.1", 1),
    ("kimi", "unknown", 1),
    ("custom", "deepseek-v4", 0),
    ("custom", "qwen", 0),
])
def test_rank_depends_on_model_and_named_best_model_pools(name, model, rank):
    pool = Pool(name=name, shapes=(), model=Model(name=model))

    result = model_rank(pool)

    assert ULTRA_MIN_RANK == 2
    assert result == rank


def test_candidates_put_fable_above_opus_and_codex_and_exclude_sonnet(ultra_pools):
    quota = {}

    result = candidates(ultra_pools, quota, allow_lower=True)

    assert result == ["claude-work-fable", "claude-work", "codex", "claude"]


@pytest.mark.parametrize("quota,expected", [
    ({"codex": "critical"}, ["claude-work-fable", "claude-work", "claude"]),
    ({"claude-work": "critical"}, ["codex", "claude"]),
    ({"claude-work:Fable": "critical"}, ["claude-work", "codex", "claude"]),
    ({"claude-work": "low"}, ["claude-work-fable", "codex", "claude", "claude-work"]),
])
def test_candidates_gate_and_sort_by_account_and_model_quota(ultra_pools, quota, expected):
    pools = ultra_pools

    result = candidates(pools, quota)

    assert result == expected


def test_low_model_window_sorts_after_ok_at_the_same_rank(ultra_pools):
    pools = dict(ultra_pools)
    pools["second-fable"] = replace(pools["claude-work-fable"], name="second-fable",
                                    quota_provider="second-account", endpoint="")
    quota = {"claude-work:Fable": "low"}

    result = candidates(pools, quota)

    assert result[:3] == ["second-fable", "claude-work-fable", "claude-work"]


@pytest.mark.parametrize("host,expected", [
    ("claude", ["claude-work", "codex", "claude"]),
    ("codex", ["claude", "claude-work", "codex"]),
])
def test_host_sorts_last_among_equals(ultra_pools, host, expected):
    pools = {arm: ultra_pools[arm] for arm in ("claude", "codex", "claude-work")}

    result = candidates(pools, {}, host=host)

    assert result == expected


def test_lower_models_require_opt_in_and_exhausted_best_models(ultra_pools):
    quota = {"claude": "critical", "codex": "critical", "claude-work:Fable": "critical"}
    pools = {arm: pool for arm, pool in ultra_pools.items() if arm != "claude-work"}

    default = candidates(pools, quota)
    fallback = candidates(pools, quota, allow_lower=True)

    assert default == []
    assert fallback == ["claude-work-sonnet", "kimi", "deepseek", "free", "local-agent", "local-batch"]


def test_family_and_arm_exclusions_precede_lower_model_fallback(ultra_pools):
    families, arms = {"anthropic"}, {"codex"}

    result = candidates(ultra_pools, {}, allow_lower=True,
                        exclude_families=families, exclude_arms=arms)

    assert result == ["kimi", "deepseek", "free", "local-agent", "local-batch"]
    assert families == {"anthropic"}
    assert arms == {"codex"}


def test_quota_keys_follow_configured_provider_and_allow_untracked_arms(pools):
    pools["codex"] = replace(pools["codex"], quota_provider="custom-account")
    pools["claude"] = replace(pools["claude"], quota_provider="")

    result = candidates(pools, {"custom-account": "critical", "claude": "critical"})

    assert result == ["claude"]


@pytest.mark.parametrize("role", ["planner", "integrator"])
def test_planner_and_integrator_take_the_best_candidate(ultra_pools, role):
    quota = {"claude-work:Fable": "low"}

    result = pick(role, ultra_pools, quota)

    assert result == "claude-work-fable"


@pytest.mark.parametrize("tier", ["hard", "open-ended"])
def test_hard_workers_prefer_fable(ultra_pools, tier):
    quota = {"claude-work:Fable": "low"}

    result = pick("worker", ultra_pools, quota, tier=tier)

    assert result == "claude-work-fable"


@pytest.mark.parametrize("tier", ["trivial", "simple", "moderate", "hard", "open-ended"])
def test_workers_fall_back_to_the_best_rank_two_arm(ultra_pools, tier):
    quota = {"claude-work:Fable": "critical", "claude-work": "low"}

    result = pick("worker", ultra_pools, quota, tier=tier)

    assert result == "codex"


def test_ordinary_workers_prefer_rank_two_when_fable_is_available(ultra_pools):
    quota = {}

    result = pick("worker", ultra_pools, quota)

    assert result == "claude-work"


@pytest.mark.parametrize("author,expected", [
    ("claude-work", "codex"),
    ("codex", "claude-work-fable"),
])
def test_reviewer_prefers_a_different_family(ultra_pools, author, expected):
    quota = {}

    result = pick("reviewer", ultra_pools, quota, author_arm=author)

    assert result == expected
    assert review_independence(author, result, ultra_pools) == "cross-family"


def test_reviewer_falls_back_to_another_arm_of_the_same_family(ultra_pools):
    quota = {"codex": "critical"}

    result = pick("reviewer", ultra_pools, quota, author_arm="claude-work")

    assert result == "claude-work-fable"
    assert review_independence("claude-work", result, ultra_pools) == "same-family"


def test_reviewer_never_selects_the_author(pools):
    only_author = {"codex": pools["codex"]}

    result = pick("reviewer", only_author, {}, author_arm="codex")

    assert result is None
    assert review_independence("codex", "codex", pools) == "self"


def test_review_independence_honours_explicit_families(ultra_pools):
    ultra_pools["codex"] = replace(ultra_pools["codex"], family="anthropic")

    result = review_independence("claude-work", "codex", ultra_pools)

    assert result == "same-family"


def test_fixer_keeps_a_usable_author_even_with_a_stronger_model_available(ultra_pools):
    quota = {"codex": "low"}

    result = pick("fixer", ultra_pools, quota, author_arm="codex")

    assert result == "codex"


def test_fixer_escalates_within_family_when_author_is_gated(ultra_pools):
    pools = dict(ultra_pools)
    pools["claude-backup"] = Pool(name="claude-backup", shapes=(),
                                 model=Model(name="claude-opus-5-5"))
    quota = {"claude-work": "critical", "claude": "critical"}

    result = pick("fixer", pools, quota, author_arm="claude-work")

    assert result == "claude-backup"


def test_fixer_uses_another_family_when_its_family_is_exhausted(ultra_pools):
    quota = {"claude-work": "critical", "claude": "critical"}

    result = pick("fixer", ultra_pools, quota, author_arm="claude-work")

    assert result == "codex"


@pytest.mark.parametrize("role", ["planner", "worker", "reviewer", "fixer", "integrator"])
def test_no_usable_arm_returns_none(pools, role):
    only_author = {"codex": pools["codex"]}

    result = pick(role, only_author, {"codex": "critical"}, author_arm="codex")

    assert result is None


def test_invalid_role_fails_fast(pools):
    role = "unknown"

    with pytest.raises(ValueError, match="unknown ultra role"):
        pick(role, pools, {})


@pytest.mark.parametrize("host,expected", [
    ("claude", "claude --print --permission-mode auto {task}"),
    ("codex", "codex exec --sandbox workspace-write {task}"),
])
def test_empty_host_template_becomes_a_delegated_command(pools, host, expected):
    hosted = pools_for_host(pools, host)

    result = dispatch_template(host, hosted, host=host)

    assert hosted[host].dispatch == ""
    assert result == expected


def test_configured_dispatch_templates_are_preserved(ultra_pools):
    arm = "claude-work-fable"

    result = dispatch_template(arm, ultra_pools)

    assert result == ultra_pools[arm].dispatch


def test_configured_host_template_is_preserved(pools):
    template = "custom-claude --model pinned {task}"
    pools["claude"] = replace(pools["claude"], dispatch=template)

    result = dispatch_template("claude", pools)

    assert result == template
