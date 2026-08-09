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
with banditry's defaults (alpha=beta=1), seeding 7 rewards in 8 resolved
trials yields an effective Beta(8, 2); 1 in 8 yields Beta(2, 8). Seeds are
recorded under a ``:seed`` key so federation can subtract them and export
only real observations.

Three counters, and the distinction between them is load-bearing:

``impression``
    A dispatch happened. Written at selection time. A diagnostic only;
    subtract the seed pseudo-count to learn how often an arm was chosen.
    Nothing infers from it.
``resolved``
    A dispatch whose outcome came back and was attributed. Written only by
    ``record_resolution``, i.e. only from ``outcomes.record_outcome`` /
    ``record_failure`` / a swarm tally. This is the trial count: it is what
    the Thompson sampler and ``observed_counts`` divide by.
``accepted`` (``REWARD_EVENT``)
    A resolved dispatch that succeeded. The numerator.

So ``beta = resolved - accepted``, never ``impressions - accepted``. A
dispatch nobody reconciled is *unknown*, and unknown must move neither
alpha nor beta — deriving beta from impressions instead scores every
unreconciled dispatch as a failure and degrades the posterior in proportion
to how much the router is used, which is the opposite of learning.
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
#: Dispatches whose outcome was actually attributed. The trial count.
RESOLVED_EVENT = "resolved"

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
            # banditry's trial counter is whatever `impression_event` names,
            # and it samples Beta(rewards+1, trials-rewards+1) off it. Point
            # it at `resolved` so an unreconciled dispatch is not a trial at
            # all. `Router.select` therefore books the dispatch counter
            # itself and never lets banditry write this one.
            impression_event=RESOLVED_EVENT,
            control="claude" if "claude" in arms else arms[0],
            key_prefix=cell,
            rng=self._rng,
        )

    def _seed_prior_counts(self, shape: str, cell: str, arm: str) -> tuple[float, float]:
        """(trials, rewards) to seed so the effective prior is right.

        banditry adds alpha=beta=1 on top of raw counts, so to land on the
        rule table's Beta(a, b) we seed a-1 rewards in a-1 + b-1 trials.
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

    def _backfill_resolved(self, cell: str, arm: str) -> None:
        """Materialise `arm`'s prior in the trial counter if it predates it.

        A cell written before `resolved` existed carries the prior in `seed`
        and `impression` only. EVERY write path must run this before adding a
        real trial: the sampler reads the raw key (not `_resolved_counts`), so
        without it the prior vanishes, and `observed_counts` would subtract a
        seed of 8 from a fresh count of 1 and swallow the observation whole.
        incr by 0 still creates the row, so this is once per arm.
        """
        if arm in self.storage.counts(f"{cell}:{RESOLVED_EVENT}"):
            return
        seeded = self.storage.counts(f"{cell}:{SEED_EVENT}").get(arm, 0)
        self.storage.incr(f"{cell}:{RESOLVED_EVENT}", arm, seeded)

    def _ensure_seeded(self, shape: str, cell: str, arms: Iterable[str]) -> None:
        seeds = self.storage.counts(f"{cell}:{SEED_EVENT}")
        impressions = self.storage.counts(f"{cell}:{IMPRESSION_EVENT}")
        for arm in arms:
            if arm in seeds or arm in impressions:
                self._backfill_resolved(cell, arm)
                continue
            imps, rewards = self._seed_prior_counts(shape, cell, arm)
            self.storage.incr(f"{cell}:{IMPRESSION_EVENT}", arm, imps)
            if rewards:
                self.storage.incr(f"{cell}:{REWARD_EVENT}", arm, rewards)
            self.storage.incr(f"{cell}:{SEED_EVENT}", arm, imps)
            self.storage.incr(f"{cell}:{SEED_EVENT}", f"{arm}:rewards", rewards)
            # The prior is pseudo-EVIDENCE: it is resolved by construction.
            self.storage.incr(f"{cell}:{RESOLVED_EVENT}", arm, imps)

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
        # banditry's own impression counter is `resolved` here, so it must not
        # write on selection — a dispatch is not yet a trial. The dispatch
        # counter is booked separately, and `record_impression=False` from a
        # caller still means "do not count this dispatch".
        arm = bandit.select(eligible=eligible, record_impression=False)
        if record_impression:
            self.record_impression(shape, tier, arm)
        return arm

    def reward(self, shape: str, tier: str, arm: str, event: str = REWARD_EVENT, by: float = 1) -> None:
        cell = cell_name(shape, tier)
        self._bandit(cell, [arm]).reward(arm, event, by)

    def record_impression(self, shape: str, tier: str, arm: str, by: float = 1) -> None:
        """A dispatch happened. Diagnostic only — infers nothing.

        Deliberately NOT a negative outcome. It used to be one by omission:
        with beta derived as impressions-minus-rewards, booking an impression
        and never rewarding it was indistinguishable from an observed failure.
        Call `record_resolution` for a real one.
        """
        self.storage.incr(f"{cell_name(shape, tier)}:{IMPRESSION_EVENT}", arm, by)

    def record_resolution(self, shape: str, tier: str, arm: str, by: float = 1) -> None:
        """An outcome came back and was attributed to `arm` — one trial.

        Paired with `reward` (success) or called alone (failure). Nothing else
        may write this counter: it is the sole thing standing between "we do
        not know" and "it failed".

        Reachable without a preceding `select` — `route outcome` on a cell
        this process never routed — so it backfills rather than assuming
        `_ensure_seeded` has run.
        """
        cell = cell_name(shape, tier)
        self._backfill_resolved(cell, arm)
        self.storage.incr(f"{cell}:{RESOLVED_EVENT}", arm, by)

    # -- introspection ------------------------------------------------------

    def _resolved_counts(self, cell: str) -> dict[str, float]:
        """Trial counts, with the read-side fallback for pre-`resolved` state.

        A cell seeded before this counter existed has no row for it. Readers
        must not write, so such an arm falls back to its seed pseudo-count:
        the prior is realised, and its unreconciled impressions read as zero
        real observations rather than as failures — which is the whole point.
        `_ensure_seeded` materialises the row on the cell's next selection.
        """
        resolved = self.storage.counts(f"{cell}:{RESOLVED_EVENT}")
        for arm, seeded in self.storage.counts(f"{cell}:{SEED_EVENT}").items():
            if arm.endswith(":rewards"):
                continue
            resolved.setdefault(arm, seeded)
        return resolved

    def posterior(self, shape: str, tier: str) -> dict[str, tuple[float, float]]:
        """Effective Beta(alpha, beta) per arm, priors included."""
        cell = cell_name(shape, tier)
        # Impressions enumerate the arms this cell knows about; `resolved`
        # supplies the trial count. Enumerating off `resolved` instead would
        # drop rows for an arm dispatched but never reconciled, and "chosen 12
        # times, learned nothing" is exactly what has to stay visible.
        impressions = self.storage.counts(f"{cell}:{IMPRESSION_EVENT}")
        resolved = self._resolved_counts(cell)
        rewards = self.storage.counts(f"{cell}:{REWARD_EVENT}")
        out = {}
        for arm in impressions.keys() | resolved.keys() | rewards.keys():
            won = rewards.get(arm, 0)
            lost = max(resolved.get(arm, 0) - won, 0)
            out[arm] = (won + 1.0, lost + 1.0)
        return out

    def observed_counts(self, shape: str, tier: str) -> dict[str, tuple[float, float]]:
        """Real observations only — seeded prior pseudo-counts subtracted.

        Returns {arm: (alpha, beta)} of user-observed accepted/not-accepted.
        Values may be fractional: a swarm's single weighted observation counts
        as exactly one trial. This is the only data federation may ever see.

        alpha + beta is the number of dispatches whose outcome was actually
        attributed, NOT the number of dispatches. An arm chosen ten times and
        reconciled twice reports two observations, not ten — the other eight
        are unknown, and unknown is not evidence in either direction.
        """
        cell = cell_name(shape, tier)
        seeds = self.storage.counts(f"{cell}:{SEED_EVENT}")
        resolved = self._resolved_counts(cell)
        rewards = self.storage.counts(f"{cell}:{REWARD_EVENT}")
        out: dict[str, tuple[float, float]] = {}
        for arm in resolved.keys() | rewards.keys():
            seed_imps = seeds.get(arm, 0)
            seed_rewards = seeds.get(f"{arm}:rewards", 0)
            alpha = max(rewards.get(arm, 0) - seed_rewards, 0)
            beta = max(resolved.get(arm, 0) - seed_imps - alpha, 0)
            if alpha or beta:
                out[arm] = (alpha, beta)
        return out
