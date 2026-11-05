"""Router/Bedrock-style model ids must keep their lineage for review independence."""
import pytest

from route.pools import Model, Pool, pool_family
from route.ultra_select import pick, review_independence


@pytest.mark.parametrize("model,family", [
    ("anthropic/claude-opus-5-5", "anthropic"),
    ("us.anthropic.claude-opus-5-5", "anthropic"),
    ("openai/gpt-6-astra", "openai"),
    ("moonshotai/kimi-k3.1", "moonshot"),
])
def test_vendor_prefixed_model_ids_keep_their_family(model, family):
    assert pool_family(Pool(name="custom", shapes=(), model=Model(name=model))) == family


def test_claude_behind_a_router_is_not_a_cross_family_reviewer(pools):
    pools["openrouter-opus"] = Pool(name="openrouter-opus", shapes=(),
                                    model=Model(name="anthropic/claude-opus-5-5"), quota_provider="")
    reviewer = pick("reviewer", pools, {"codex": "critical"}, author_arm="claude")
    assert review_independence("claude", reviewer, pools) != "cross-family"
