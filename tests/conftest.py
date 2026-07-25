"""Shared fixtures. Deterministic: no network, no real dispatch, no money.

Every test runs with ROUTE_STATE_DIR / ROUTE_CONFIG_DIR pointed at a tmp
dir, so nothing here ever touches the developer's real counts or config.
"""

from __future__ import annotations

import pytest

from route.pools import load_pools
from route.router import Router, load_priors
from route.storage import JsonStorage


class MeanRNG:
    """Deterministic Thompson sampling: sample = Beta mean.

    banditry only calls ``betavariate`` under algorithm="thompson"; returning
    the mean makes selection deterministic — the arm with the best posterior
    mean always wins, ties broken by pool order.
    """

    def betavariate(self, alpha: float, beta: float) -> float:
        return alpha / (alpha + beta)

    def random(self) -> float:  # epsilon-greedy paths only; unused
        return 1.0

    def choice(self, seq):
        return seq[0]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROUTE_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path


@pytest.fixture
def pools():
    return load_pools()  # defaults — config dir is empty under isolated_env


@pytest.fixture
def priors():
    return load_priors()


@pytest.fixture
def storage(tmp_path):
    return JsonStorage(tmp_path / "bandits.json")


@pytest.fixture
def router(storage, priors):
    return Router(storage=storage, priors=priors, rng=MeanRNG())
