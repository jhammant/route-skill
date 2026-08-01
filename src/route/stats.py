"""Local stats: record every decision from the first run, report it.

Per decision: context cell, eligible arms, chosen arm, why (veto / prior /
posterior), outcome events, wall-clock, tokens, escalations. NEVER task
text — the log is counts and enum values only, so it can feed federation
without leaking content.

Local runs also record throughput: (model, quant, context_length, hardware)
-> tok/s, items/s, load seconds, peak GB.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .router import REWARD_EVENT, Router, parse_cell
from .storage import state_dir


@dataclass
class Decision:
    cell: str
    shape: str
    tier: str
    eligible: list[str]
    chosen: str
    why: str  # "veto:<name>" | "prior" | "posterior"
    ts: float = field(default_factory=time.time)
    outcome_events: list[str] = field(default_factory=list)
    wall_clock_s: float | None = None
    tokens: int | None = None
    escalations: int = 0
    #: Per-unit accept/reject detail for a swarm dispatch. Counts only —
    #: the posterior sees one weighted observation, never this list.
    units: list[bool] | None = None


@dataclass
class Throughput:
    model: str
    quant: str
    context_length: int
    hardware: str
    tok_s: float
    items_s: float = 0.0
    load_s: float = 0.0
    peak_gb: float = 0.0
    ts: float = field(default_factory=time.time)


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class Stats:
    def __init__(self, directory: str | Path | None = None) -> None:
        base = Path(directory) if directory is not None else state_dir()
        self.decisions_path = base / "decisions.jsonl"
        self.throughput_path = base / "throughput.jsonl"

    def record_decision(self, decision: Decision) -> None:
        _append_jsonl(self.decisions_path, asdict(decision))

    def _amend(
        self,
        mutate: Callable[[dict], None],
        decision_ts: float | None,
        what: str,
    ) -> None:
        """Apply ``mutate`` to one logged decision and rewrite the log.

        With ``decision_ts``, the row whose ``ts`` matches (within float
        round-tripping tolerance) — so a consumer reconciling long after the
        dispatch annotates the decision it actually observed. Without it, the
        most recent decision is targeted, which is what an inline caller
        means.

        An unmatched ``decision_ts`` warns (naming ``what`` was dropped) and
        writes nothing: a rotated or hand-edited log must never take down
        recording, because the bandit reward is the part that matters.

        Everything that amends a decision after the fact goes through here.
        Locating the row is the subtle part — the tolerance, the last-match
        rule, the fail-soft on a rotated log — and a second copy of it would
        be a second set of those decisions to keep in step.
        """
        rows = _read_jsonl(self.decisions_path)
        if not rows:
            return
        if decision_ts is None:
            index = len(rows) - 1
        else:
            matches = [
                i
                for i, r in enumerate(rows)
                if abs(float(r.get("ts", 0.0)) - decision_ts) < 1e-6
            ]
            if not matches:
                print(
                    f"route: no decision at ts={decision_ts!r}; {what} not attached",
                    file=sys.stderr,
                )
                return
            index = matches[-1]
        mutate(rows[index])
        self.decisions_path.write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
        )

    def record_outcome_events(
        self, events: list[str], decision_ts: float | None = None
    ) -> None:
        """Attach outcome events to a decision."""

        def _merge(row: dict) -> None:
            row["outcome_events"] = sorted(
                set(row.get("outcome_events", [])) | set(events)
            )

        self._amend(_merge, decision_ts, "outcome events")

    def record_perf(
        self,
        decision_ts: float | None = None,
        wall_clock_s: float | None = None,
        tokens: int | None = None,
    ) -> None:
        """Attach measured cost to a decision.

        `Decision` has declared `wall_clock_s` and `tokens` from the start and
        nothing ever assigned them, so every row carried nulls where the
        latency and cost data was supposed to be. The measurement existed —
        `cmd_task` times the dispatch — it was simply thrown away once the
        hook payload had been built.

        Both are optional and set independently. route times its own dispatch
        and can fill `wall_clock_s` inline; it never sees a token count,
        because it shells out to arms that report one (or do not), so `tokens`
        arrives later through `auto outcome --tokens`.

        Last write wins per field. A backfilled measurement is a correction —
        a wrapper that knows the arm's real accounting is a better source than
        route's wall clock, and it must not have to care which ran first.
        """
        if wall_clock_s is None and tokens is None:
            return

        def _set(row: dict) -> None:
            if wall_clock_s is not None:
                row["wall_clock_s"] = float(wall_clock_s)
            if tokens is not None:
                row["tokens"] = int(tokens)

        self._amend(_set, decision_ts, "perf")

    def record_throughput(self, sample: Throughput) -> None:
        _append_jsonl(self.throughput_path, asdict(sample))

    def decisions(self) -> list[dict]:
        return _read_jsonl(self.decisions_path)

    def throughput(self) -> list[dict]:
        return _read_jsonl(self.throughput_path)


def _prior_of(router: Router, shape: str, arm: str) -> tuple[float, float]:
    a, b = (
        router.priors.favoured
        if arm in router.priors.favoured_arms(shape)
        else router.priors.unfavoured
    )
    return float(a), float(b)


def _bench_obs(router: Router) -> dict[tuple[str, str, str], float]:
    """{(shape, tier, arm): benchmark observations} from the bench namespace."""
    from .benchmark import parse_bench_key  # local import: stats <-> benchmark

    totals: dict[tuple[str, str, str], float] = {}
    for key in router.storage.keys("bench:"):
        parsed = parse_bench_key(key)
        if parsed is None:
            continue
        shape, tier, event = parsed
        if event != "impression":
            continue
        for arm, count in router.storage.counts(key).items():
            slot = (shape, tier, arm)
            totals[slot] = totals.get(slot, 0.0) + count
    return totals


def report_cells(router: Router) -> list[dict]:
    """Per-cell, per-arm: observations, success rate, prior vs posterior.

    Showing prior and posterior side by side is the transparency that makes
    an adaptive router trustworthy — you can see exactly when evidence took
    over from the rules. Real vs benchmark observations are reported in
    SEPARATE columns, so it is always visible how much of a cell's
    confidence is manufactured.
    """
    cells: dict[tuple[str, str], list[str]] = {}
    for key in router.storage.keys("route:"):
        parsed = parse_cell(key)
        if parsed is None:
            continue
        shape, tier = parsed
        cells.setdefault((shape, tier), [])
    bench = _bench_obs(router)
    rows = []
    for shape, tier in sorted(cells):
        posterior = router.posterior(shape, tier)
        observed = router.observed_counts(shape, tier)
        for arm in sorted(posterior):
            pa, pb = posterior[arm]
            oa, ob = observed.get(arm, (0, 0))
            prior_a, prior_b = _prior_of(router, shape, arm)
            rows.append({
                "cell": f"{shape}:{tier}",
                "arm": arm,
                "obs": oa + ob,
                "real_obs": oa + ob,
                "bench_obs": bench.get((shape, tier, arm), 0),
                "success_rate": round(oa / (oa + ob), 4) if (oa + ob) else None,
                "prior": f"Beta({prior_a:g},{prior_b:g})",
                "posterior": f"Beta({pa:g},{pb:g})",
            })
    return rows


def report_arm(router: Router, arm: str) -> list[dict]:
    """How one pool is doing, by shape."""
    return [r for r in report_cells(router) if r["arm"] == arm]


def report_throughput(stats: Stats) -> list[dict]:
    """Local model performance table."""
    keys = ("model", "quant", "context_length", "hardware", "tok_s", "items_s", "load_s", "peak_gb")
    return [{k: row.get(k) for k in keys} for row in stats.throughput()]
