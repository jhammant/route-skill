"""The 'free' arm: proxy-gated eligibility, endpoint dispatch, private veto.

The free-llm proxy is a LOCAL process fronting REMOTE third-party providers,
several of which train on submitted prompts — so private data must never
route to it, and it is eligible only while the proxy actually answers.
"""

from __future__ import annotations

import pytest

from route import eligibility
from route.eligibility import eligible_for, gate, probe_health
from route.pools import Pool, load_pools
from route.shapes import SHAPES

BATCH = [s for s in SHAPES if s.startswith("batch:")]

# Every quota-backed pool exhausted: nothing may rescue the task into 'free'.
CRITICAL_QUOTA = {"claude": "critical", "codex": "critical", "kimi": "critical"}


@pytest.fixture
def proxy_up(monkeypatch):
    monkeypatch.setattr(eligibility, "probe_health", lambda url, timeout=1.0: True)


@pytest.fixture
def proxy_down(monkeypatch):
    monkeypatch.setattr(eligibility, "probe_health", lambda url, timeout=1.0: False)


# -- registration ------------------------------------------------------------


def test_free_arm_is_its_own_cost_class(pools):
    free = pools["free"]
    assert free.cost == "free"
    assert free.cost not in ("included", "paid", "local")
    assert free.remote  # the proxy is local; the providers are not
    assert free.strength < pools["local-batch"].strength  # weakest arm
    # The proxy self-limits to provider rate limits; do not stack on top.
    assert free.parallel_limit <= 4


def test_free_arm_dispatches_through_the_local_proxy(pools):
    free = pools["free"]
    assert free.endpoint == "http://127.0.0.1:8080/v1"
    assert free.dispatch == "local-llm batch {task} --endpoint http://127.0.0.1:8080/v1"
    assert free.probe == "local-llm ask {task} --endpoint http://127.0.0.1:8080/v1"
    assert free.health_url == "http://127.0.0.1:8080/healthz"


# -- eligibility -------------------------------------------------------------


def test_free_arm_eligible_for_batch_shapes_when_proxy_up(pools, proxy_up):
    for shape in BATCH:
        assert "free" in eligible_for(shape, pools), shape


def test_free_arm_never_takes_refactor_or_debug(pools, proxy_up):
    for shape in ("coding:refactor", "coding:debug", "orchestration"):
        assert "free" not in eligible_for(shape, pools), shape


def test_unreachable_proxy_makes_free_ineligible_without_raising(pools, proxy_down):
    eligible = eligible_for("batch:classify", pools)  # must not raise
    assert "free" not in eligible
    assert "local-batch" in eligible  # everyone else unaffected


def test_probe_health_is_fail_safe():
    # Port 1 on loopback refuses connections; probe must return False, not raise.
    assert probe_health("http://127.0.0.1:1/healthz", timeout=0.2) is False
    assert probe_health("not a url", timeout=0.2) is False


# -- the private-data veto ----------------------------------------------------


def test_private_data_never_routes_to_free(pools, proxy_up):
    """Proxy up, every other arm exhausted — free still cannot take it."""
    result = gate(
        "Summarise all of these customer data records",
        "batch:summarize",
        pools,
        quota=CRITICAL_QUOTA,
        private=True,
    )
    assert result.veto is not None and result.veto.name == "private-data"
    assert result.eligible == ["claude"]
    assert "free" not in result.eligible


def test_private_data_veto_short_circuits_regardless_of_quota(pools, proxy_up):
    """A veto is not a score: quota and complexity are never consulted."""
    result = gate("rotate the api keys in our .env", "batch:extract", pools,
                  quota=CRITICAL_QUOTA)
    assert result.veto is not None and result.veto.name == "private-data"
    assert "free" not in result.eligible


# -- endpoint awareness for local arms ---------------------------------------


def test_endpoint_pool_appends_endpoint_to_dispatch_and_probe():
    pool = Pool(
        name="local-batch-lmstudio",
        shapes=("batch:classify",),
        cost="local",
        dispatch="local-llm batch {task}",
        probe="local-llm ask {task}",
        endpoint="http://127.0.0.1:1234/v1",
    )
    assert pool.dispatch == "local-llm batch {task} --endpoint http://127.0.0.1:1234/v1"
    assert pool.probe == "local-llm ask {task} --endpoint http://127.0.0.1:1234/v1"


def test_plain_local_batch_default_is_unchanged(pools):
    local = pools["local-batch"]
    assert local.endpoint == ""
    assert local.dispatch == "local-llm batch {task}"
    assert local.probe == "local-llm ask {task}"


def test_same_tool_registered_as_two_endpoint_arms(tmp_path, proxy_down):
    cfg = tmp_path / "pools.toml"
    cfg.write_text(
        """
[pools.local-batch-lmstudio]
shapes = ["batch:classify", "batch:summarize", "batch:extract", "batch:embed"]
cost = "local"
strength = 1
dispatch = "local-llm batch {task}"
probe = "local-llm ask {task}"
endpoint = "http://127.0.0.1:1234/v1"

[pools.local-batch-ollama]
shapes = ["batch:classify", "batch:summarize", "batch:extract", "batch:embed"]
cost = "local"
strength = 1
dispatch = "local-llm batch {task}"
probe = "local-llm ask {task}"
endpoint = "http://127.0.0.1:11434/v1"
""",
        encoding="utf-8",
    )
    pools = load_pools(cfg)
    lmstudio = pools["local-batch-lmstudio"]
    ollama = pools["local-batch-ollama"]
    assert lmstudio.dispatch.endswith("--endpoint http://127.0.0.1:1234/v1")
    assert ollama.dispatch.endswith("--endpoint http://127.0.0.1:11434/v1")
    # Two distinct arms over the same tool — the bandit learns per backend.
    assert lmstudio.name != ollama.name
    assert "local-batch-lmstudio" in eligible_for("batch:classify", pools)
    assert "local-batch-ollama" in eligible_for("batch:classify", pools)
