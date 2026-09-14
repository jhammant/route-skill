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
        # A rule is either a list of favoured arms (all Beta(favoured)) or a
        # table of explicit per-arm priors: {codex = [8, 2], claude = [5, 5]}
        # — for cells where one arm should lead and another stay the clear
        # fallback rather than dropping to Beta(unfavoured).
        self.rules: dict[str, tuple[str, ...]] = {}
        self.explicit: dict[str, dict[str, tuple[float, float]]] = {}
        for key, rule in (data.get("rules") or {}).items():
            key = str(key)
            if isinstance(rule, dict):
                self.explicit[key] = {
                    str(arm): (float(ab[0]), float(ab[1])) for arm, ab in rule.items()
                }
                self.rules[key] = tuple(self.explicit[key])
            else:
                self.rules[key] = tuple(rule)

    def _key(self, shape: str, tier: str | None) -> str | None:
        """The rule key that governs a cell: ``shape:tier`` beats ``shape``."""
        if tier is not None and f"{shape}:{tier}" in self.rules:
            return f"{shape}:{tier}"
        return shape if shape in self.rules else None

    def favoured_arms(self, shape: str, tier: str | None = None) -> tuple[str, ...]:
        """Favoured arms for a cell: a ``shape:tier`` rule beats the shape rule."""
        key = self._key(shape, tier)
        return self.rules[key] if key else ()

    def prior(self, shape: str, tier: str | None, arm: str) -> tuple[float, float]:
        """Beta(a, b) an arm starts from in a cell."""
        key = self._key(shape, tier)
        if key and arm in self.explicit.get(key, {}):
            return self.explicit[key][arm]
        return tuple(self.favoured) if arm in self.favoured_arms(shape, tier) else tuple(self.unfavoured)


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
        parsed = parse_cell(cell)
        tier = parsed[1] if parsed else None
        a, b = self.priors.prior(shape, tier, arm)
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

    def reseed(
        self, arms_for_shape: dict[str, Iterable[str]], *, apply: bool = True
    ) -> list[dict]:
        """Re-derive every live cell's seed pseudo-counts from the current rules.

        Seeding happens once per cell, so a cell touched under an older rule
        table keeps that table's priors forever — a new rule never reaches
        the cells you actually use. This moves each arm's seed to what the
        current table says, as a delta on the impression/reward counts, so
        real observations are untouched (``observed_counts`` is identical
        before and after). Arms the shape now accepts but that were never
        seeded in the cell are seeded fresh. Returns one record per change.
        """
        changes: list[dict] = []
        for key in self.storage.keys("route:"):
            if not key.endswith(f":{SEED_EVENT}"):
                continue
            cell = key[: -len(SEED_EVENT) - 1]
            parsed = parse_cell(cell)
            if parsed is None:
                continue
            shape, tier = parsed
            seeds = self.storage.counts(key)
            arms = {a for a in seeds if not a.endswith(":rewards")}
            arms |= set(arms_for_shape.get(shape, ()))
            for arm in sorted(arms):
                old_imps = seeds.get(arm, 0.0)
                old_rewards = seeds.get(f"{arm}:rewards", 0.0)
                new_imps, new_rewards = self._seed_prior_counts(shape, cell, arm)
                if (old_imps, old_rewards) == (new_imps, new_rewards):
                    continue
                changes.append({
                    "cell": f"{shape}:{tier}", "arm": arm,
                    "from": f"Beta({old_rewards + 1:g},{old_imps - old_rewards + 1:g})",
                    "to": f"Beta({new_rewards + 1:g},{new_imps - new_rewards + 1:g})",
                })
                if not apply:
                    continue
                self.storage.incr(f"{cell}:{IMPRESSION_EVENT}", arm, new_imps - old_imps)
                self.storage.incr(f"{cell}:{REWARD_EVENT}", arm, new_rewards - old_rewards)
                self.storage.incr(key, arm, new_imps - old_imps)
                self.storage.incr(key, f"{arm}:rewards", new_rewards - old_rewards)
        return changes

    # -- selection and rewards ---------------------------------------------

    def select(
        self,
        shape: str,
        tier: str,
        eligible: Iterable[str],
        *,
        record_impression: bool = True,
        prefer: Iterable[str] | None = None,
    ) -> str:
        eligible = list(eligible)
        if prefer:
            # A preference bonus realised as a restriction: when any
            # preferred arm is eligible, the bandit chooses among those only.
            # Used to land sensitive work on local arms rather than
            # defaulting to claude (SPEC-ship section 1); claude stays
            # eligible and is the fallback when no local arm survives.
            preferred = [a for a in eligible if a in set(prefer)]
            if preferred:
                eligible = preferred
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

    def observe(self, shape: str, tier: str, arm: str, arms: Iterable[str]) -> None:
        """Record a trial the bandit did not choose (a user ``--pool`` pin).

        A pinned dispatch is still evidence about that arm in that cell, but
        ``select`` never ran, so nothing was seeded or counted. Seed the cell
        for every arm that could take the shape (so the pinned arm's prior is
        in place, not just a bare count) and record one impression; the
        outcome recorded afterwards then lands on a properly seeded arm.
        """
        cell = cell_name(shape, tier)
        self._ensure_seeded(shape, cell, set(arms) | {arm})
        self.record_impression(shape, tier, arm)

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
