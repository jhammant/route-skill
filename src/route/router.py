"""The banditry wrapper — one bandit per context cell.

Cells are named ``route:{shape}:{tier}`` so ``coding:refactor:hard`` learns
independently of ``batch:classify:trivial``. Arms are the pools. Each arm's
Beta prior is seeded from the static rule table (``data/priors.toml``):
favoured pools start at Beta(8, 2), the rest at Beta(2, 8). With
``algorithm="thompson"`` the cold start behaves exactly like the rules and
evidence takes over automatically, per cell — there is no "switch to
adaptive" flag by design.

banditry applies a single global ``alpha``/``beta`` smoothing on top of raw
counts, so per-arm priors are realised as seeded pseudo-counts in storage:
with banditry's defaults (alpha=beta=1), seeding 7 rewards in 8 impressions
yields an effective Beta(8, 2); 1 in 8 yields Beta(2, 8). Seeds are recorded
under a ``:seed`` key so federation can subtract them and export only real
observations.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Iterable

from banditry import Bandit

from .storage import JsonStorage, config_dir

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

REWARD_EVENT = "accepted"
IMPRESSION_EVENT = "impression"
SEED_EVENT = "seed"

#: Reward events we track, hardest-to-fake first (SPEC sections 6-7).
REWARD_EVENTS: tuple[str, ...] = ("accepted", "verified", "completed")


class Priors:
    """The static rule table: favoured pools per shape, prior strengths."""

    def __init__(self, data: dict) -> None:
        self.schema = int(data.get("schema", 1))
        self.favoured = tuple(data.get("favoured", (8, 2)))
        self.unfavoured = tuple(data.get("unfavoured", (2, 8)))
        self.rules: dict[str, tuple[str, ...]] = {
            str(shape): tuple(arms) for shape, arms in (data.get("rules") or {}).items()
        }

    def favoured_arms(self, shape: str) -> tuple[str, ...]:
        return self.rules.get(shape, ())


def load_priors(path: str | Path | None = None) -> Priors:
    """Shipped rule table, optionally replaced by ~/.config/route/priors.toml."""
    if path is None:
        override = config_dir() / "priors.toml"
        path = override if override.is_file() else None
    if path is not None:
        return Priors(tomllib.loads(Path(path).read_text(encoding="utf-8")))
    text = resources.files("route").joinpath("data/priors.toml").read_text(encoding="utf-8")
    return Priors(tomllib.loads(text))


def cell_name(shape: str, tier: str) -> str:
    return f"route:{shape}:{tier}"


def parse_cell(key: str) -> tuple[str, str] | None:
    """'route:coding:refactor:hard[:event]' -> ('coding:refactor', 'hard')."""
    parts = key.split(":")
    if len(parts) < 4 or parts[0] != "route":
        return None
    return f"{parts[1]}:{parts[2]}", parts[3]


class Router:
    """One Thompson-sampling bandit per context cell, priors from the rules."""

    def __init__(
        self,
        storage: JsonStorage | None = None,
        priors: Priors | None = None,
        *,
        community: dict[str, dict[str, tuple[float, float]]] | None = None,
        rng=None,
    ) -> None:
        self.storage = storage if storage is not None else JsonStorage()
        self.priors = priors if priors is not None else load_priors()
        # Discounted community/team counts, used as extra prior strength:
        # {cell: {arm: (alpha, beta)}}. Local observations grow past it.
        self.community = community or {}
        self._rng = rng

    # -- bandit construction ----------------------------------------------

    def _bandit(self, cell: str, arms: Iterable[str]) -> Bandit:
        arms = list(arms)
        return Bandit(
            cell,
            {a: a for a in arms},
            storage=self.storage,
            algorithm="thompson",
            reward_event=REWARD_EVENT,
            control="claude" if "claude" in arms else arms[0],
            key_prefix=cell,
            rng=self._rng,
        )

    def _seed_prior_counts(self, shape: str, cell: str, arm: str) -> tuple[float, float]:
        """(impressions, rewards) to seed so the effective prior is right.

        banditry adds alpha=beta=1 on top of raw counts, so to land on the
        rule table's Beta(a, b) we seed a-1 rewards in a-1 + b-1 impressions.
        Community and benchmark-seed counts add prior strength on top of the
        rule seed; they may be fractional (already discounted), so the seed
        counts stay fractional too.
        """
        a, b = (
            self.priors.favoured
            if arm in self.priors.favoured_arms(shape)
            else self.priors.unfavoured
        )
        # Community counts are keyed by federation context ("{shape}:{tier}"),
        # without the "route:" storage prefix.
        context = cell.split(":", 1)[1] if cell.startswith("route:") else cell
        extra_a, extra_b = self.community.get(context, {}).get(arm, (0.0, 0.0))
        rewards = a - 1 + extra_a
        impressions = a - 1 + b - 1 + extra_a + extra_b
        return impressions, rewards

    def _ensure_seeded(self, shape: str, cell: str, arms: Iterable[str]) -> None:
        seeds = self.storage.counts(f"{cell}:{SEED_EVENT}")
        impressions = self.storage.counts(f"{cell}:{IMPRESSION_EVENT}")
        for arm in arms:
            if arm in seeds or arm in impressions:
                continue
            imps, rewards = self._seed_prior_counts(shape, cell, arm)
            self.storage.incr(f"{cell}:{IMPRESSION_EVENT}", arm, imps)
            if rewards:
                self.storage.incr(f"{cell}:{REWARD_EVENT}", arm, rewards)
            self.storage.incr(f"{cell}:{SEED_EVENT}", arm, imps)
            self.storage.incr(f"{cell}:{SEED_EVENT}", f"{arm}:rewards", rewards)

    # -- selection and rewards ---------------------------------------------

    def select(
        self,
        shape: str,
        tier: str,
        eligible: Iterable[str],
        *,
        record_impression: bool = True,
    ) -> str:
        eligible = list(eligible)
        cell = cell_name(shape, tier)
        self._ensure_seeded(shape, cell, eligible)
        bandit = self._bandit(cell, eligible)
        return bandit.select(eligible=eligible, record_impression=record_impression)

    def reward(self, shape: str, tier: str, arm: str, event: str = REWARD_EVENT, by: float = 1) -> None:
        cell = cell_name(shape, tier)
        self._bandit(cell, [arm]).reward(arm, event, by)

    def record_impression(self, shape: str, tier: str, arm: str, by: float = 1) -> None:
        """An impression with no reward — the negative outcome of escalation."""
        self.storage.incr(f"{cell_name(shape, tier)}:{IMPRESSION_EVENT}", arm, by)

    # -- introspection ------------------------------------------------------

    def posterior(self, shape: str, tier: str) -> dict[str, tuple[float, float]]:
        """Effective Beta(alpha, beta) per arm, priors included."""
        cell = cell_name(shape, tier)
        impressions = self.storage.counts(f"{cell}:{IMPRESSION_EVENT}")
        rewards = self.storage.counts(f"{cell}:{REWARD_EVENT}")
        out = {}
        for arm in impressions.keys() | rewards.keys():
            won = rewards.get(arm, 0)
            lost = max(impressions.get(arm, 0) - won, 0)
            out[arm] = (won + 1.0, lost + 1.0)
        return out

    def observed_counts(self, shape: str, tier: str) -> dict[str, tuple[float, float]]:
        """Real observations only — seeded prior pseudo-counts subtracted.

        Returns {arm: (alpha, beta)} of user-observed accepted/not-accepted.
        Values may be fractional: a swarm's single weighted observation counts
        as exactly one trial. This is the only data federation may ever see.
        """
        cell = cell_name(shape, tier)
        seeds = self.storage.counts(f"{cell}:{SEED_EVENT}")
        impressions = self.storage.counts(f"{cell}:{IMPRESSION_EVENT}")
        rewards = self.storage.counts(f"{cell}:{REWARD_EVENT}")
        out: dict[str, tuple[float, float]] = {}
        for arm in impressions.keys() | rewards.keys():
            seed_imps = seeds.get(arm, 0)
            seed_rewards = seeds.get(f"{arm}:rewards", 0)
            alpha = max(rewards.get(arm, 0) - seed_rewards, 0)
            beta = max(impressions.get(arm, 0) - seed_imps - alpha, 0)
            if alpha or beta:
                out[arm] = (alpha, beta)
        return out
