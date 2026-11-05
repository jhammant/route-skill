"""A pinned weaker model must not pass the ultra gate because of the arm name or a substring."""
import pytest

from route.pools import Model, Pool
from route.ultra_select import ULTRA_MIN_RANK, model_rank


@pytest.mark.parametrize("name,model,cost", [
    ("claude-work", "claude-sonnet-5-5", "included"),
    ("claude", "claude-haiku-4-5", "included"),
    ("local-agent", "octopus-v2", "local"),
])
def test_pinned_weaker_model_is_below_the_ultra_gate(name, model, cost):
    pool = Pool(name=name, shapes=(), model=Model(name=model), cost=cost)
    assert model_rank(pool) < ULTRA_MIN_RANK
