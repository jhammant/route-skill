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
import time
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
    #: Quota cost of the dispatch, from quotamax snapshots either side of it:
    #: {"provider", "before", "after", "delta"} with {limit_label: percent}.
    #: Percentages are coarse and shared with whatever else ran meanwhile,
    #: so a single delta is noisy; the per-arm mean in `stats --quota` is not.
    quota: dict | None = None


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

    def record_outcome_events(self, events: list[str]) -> None:
        """Attach outcome events to the most recent decision."""
        rows = _read_jsonl(self.decisions_path)
        if not rows:
            return
        rows[-1]["outcome_events"] = sorted(set(rows[-1].get("outcome_events", [])) | set(events))
        self.decisions_path.write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
        )

    def annotate_last(self, **fields) -> None:
        """Set fields (wall clock, quota cost) on the most recent decision."""
        rows = _read_jsonl(self.decisions_path)
        if not rows:
            return
        rows[-1].update(fields)
        self.decisions_path.write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
        )

    def record_throughput(self, sample: Throughput) -> None:
        _append_jsonl(self.throughput_path, asdict(sample))

    def decisions(self) -> list[dict]:
        return _read_jsonl(self.decisions_path)

    def throughput(self) -> list[dict]:
        return _read_jsonl(self.throughput_path)


def _prior_of(router: Router, shape: str, tier: str, arm: str) -> tuple[float, float]:
    a, b = router.priors.prior(shape, tier, arm)
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
            prior_a, prior_b = _prior_of(router, shape, tier, arm)
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


def report_quota(stats: Stats) -> list[dict]:
    """Per arm: what dispatches cost in quota, and what came back.

    Only decisions that actually dispatched (an outcome event or a quota
    record) count; a plan-only run or a `stay` is not a dispatch.
    """
    per_arm: dict[str, dict] = {}
    for row in stats.decisions():
        events = set(row.get("outcome_events") or [])
        quota = row.get("quota")
        if not events and not quota:
            continue
        arm = row.get("chosen", "?")
        agg = per_arm.setdefault(arm, {
            "arm": arm, "dispatches": 0, "completed": 0, "accepted": 0, "failed": 0,
            "quota_costed": 0, "quota_delta_total": {}, "wall_clock_s_total": 0.0, "wall_clock_n": 0,
        })
        agg["dispatches"] += 1
        for event in ("completed", "accepted", "failed"):
            agg[event] += event in events
        if quota and isinstance(quota.get("delta"), dict):
            agg["quota_costed"] += 1
            for label, pct in quota["delta"].items():
                agg["quota_delta_total"][label] = agg["quota_delta_total"].get(label, 0.0) + float(pct)
        if isinstance(row.get("wall_clock_s"), (int, float)):
            agg["wall_clock_s_total"] += float(row["wall_clock_s"])
            agg["wall_clock_n"] += 1
    rows = []
    for agg in sorted(per_arm.values(), key=lambda a: a["arm"]):
        n = agg["quota_costed"]
        rows.append({
            "arm": agg["arm"],
            "dispatches": agg["dispatches"],
            "completed": agg["completed"],
            "accepted": agg["accepted"],
            "failed": agg["failed"],
            "accept_rate": round(agg["accepted"] / agg["dispatches"], 2) if agg["dispatches"] else None,
            "quota_costed": n,
            "quota_pct_total": {k: round(v, 1) for k, v in agg["quota_delta_total"].items()},
            "quota_pct_per_dispatch": {k: round(v / n, 2) for k, v in agg["quota_delta_total"].items()} if n else {},
            "quota_pct_per_accept": {
                k: round(v / agg["accepted"], 2) for k, v in agg["quota_delta_total"].items()
            } if agg["accepted"] and n else {},
            "wall_clock_s_mean": round(agg["wall_clock_s_total"] / agg["wall_clock_n"], 1) if agg["wall_clock_n"] else None,
        })
    return rows
