"""Federation: export / push / pull / status.

The unit of sharing is the Beta posterior — Beta is conjugate to Bernoulli,
so federation is summation: merging N users is alpha=sigma, beta=sigma. What
ships is COUNTS, NOT CONTENT: schema, context, arm, model, alpha, beta.
Nothing else. ``export`` prints exactly what WOULD be shared and shares
nothing; it is the documented first step so people can see precisely what
would leave their machine before anything does.

Never leaves: prompts, diffs, file paths, repo names, task titles, item
content. ``assert_payload_clean`` makes that an assert-level guarantee.

Anti-gaming (a public registry creates a vendor incentive to inflate
numbers): per-installation contribution is capped, k-anonymity suppresses
cells with fewer than K contributors, model identity is pinned to
model+quant+version, and the aggregate is published openly for audit.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

from .complexity import TIERS
from .pools import Pool
from .router import Router, parse_cell
from .shapes import SHAPES
from .stats import Stats
from .storage import state_dir

SCHEMA = 1

#: Trust weights as prior-strength multipliers (same as claude-history-cloud).
COMMUNITY_DISCOUNT = 0.4
TEAM_DISCOUNT = 0.7

#: A cell needs at least this many contributors before it may publish.
K_ANONYMITY = 3

#: One machine cannot dominate a cell.
CONTRIBUTION_CAP = 200

_ALLOWED_RECORD_KEYS = {"schema", "context", "arm", "model", "alpha", "beta"}
_CONTEXT_RE = re.compile(
    r"^(?:" + "|".join(re.escape(s) for s in SHAPES) + r"):(?:"
    + "|".join(re.escape(t) for t in TIERS) + r")$"
)
_ARM_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:@-]{0,127}$")
_THROUGHPUT_KEYS = {
    "schema", "model", "quant", "context_length", "hardware",
    "tok_s", "items_s", "load_s", "peak_gb",
}


def assert_payload_clean(payload: dict) -> dict:
    """Assert-level guarantee: the payload is counts only.

    Every routing record carries exactly {schema, context, arm, model, alpha,
    beta}; context must be a closed-enum cell, arm and model must be simple
    identifiers (no whitespace, no path separators — so no task text, file
    paths, or repo names can appear, structurally).
    """
    for rec in payload.get("records", []):
        assert set(rec.keys()) == _ALLOWED_RECORD_KEYS, f"unexpected keys: {sorted(rec)}"
        assert isinstance(rec["schema"], int) and rec["schema"] == SCHEMA
        assert _CONTEXT_RE.match(rec["context"]), f"bad context: {rec['context']!r}"
        assert _ARM_RE.match(rec["arm"]), f"bad arm: {rec['arm']!r}"
        assert _MODEL_RE.match(rec["model"]), f"bad model: {rec['model']!r}"
        assert isinstance(rec["alpha"], (int, float)) and rec["alpha"] >= 0
        assert isinstance(rec["beta"], (int, float)) and rec["beta"] >= 0
    for rec in payload.get("throughput", []):
        assert set(rec.keys()) == _THROUGHPUT_KEYS, f"unexpected keys: {sorted(rec)}"
        for key, value in rec.items():
            if isinstance(value, str):
                assert "/" not in value and "\\" not in value, f"path-like {key}: {value!r}"
    return payload


# -- federation state --------------------------------------------------------


class FederationState:
    """Pulled (already discounted) counts + contributor bookkeeping."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else state_dir() / "federation.json"
        self.data = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"cells": {}, "last_pull": None, "last_push": None}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")

    def contributors(self, context: str) -> int:
        return int(self.data["cells"].get(context, {}).get("contributors", 0))

    def merge_pulled(self, records: list[dict], contributor_counts: dict[str, int], discount: float) -> None:
        cells = self.data["cells"]
        for rec in records:
            cell = cells.setdefault(rec["context"], {"contributors": 0, "arms": {}})
            arm = cell["arms"].setdefault(
                rec["arm"], {"model": rec["model"], "alpha": 0.0, "beta": 0.0}
            )
            arm["alpha"] += rec["alpha"] * discount
            arm["beta"] += rec["beta"] * discount
        for context, n in contributor_counts.items():
            if context in cells:
                cells[context]["contributors"] += int(n)

    def community_priors(self) -> dict[str, dict[str, tuple[float, float]]]:
        """{cell: {arm: (alpha, beta)}} for seeding the Router's priors."""
        return {
            context: {arm: (a["alpha"], a["beta"]) for arm, a in cell["arms"].items()}
            for context, cell in self.data["cells"].items()
        }


# -- export -------------------------------------------------------------------


def _collect_observed(router: Router) -> dict[str, dict[str, tuple[int, int]]]:
    """{context: {arm: (alpha, beta)}} of real observations, priors subtracted."""
    cells: set[tuple[str, str]] = set()
    for key in router.storage.keys("route:"):
        parsed = parse_cell(key)
        if parsed is not None:
            cells.add(parsed)
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for shape, tier in sorted(cells):
        observed = router.observed_counts(shape, tier)
        if observed:
            out[f"{shape}:{tier}"] = observed
    return out


def export(
    router: Router,
    pools: dict[str, Pool],
    fed: FederationState | None = None,
    *,
    k: int = K_ANONYMITY,
    cap: int = CONTRIBUTION_CAP,
) -> dict:
    """What WOULD be shared — counts only, and sharing nothing by doing this.

    Cells with fewer than ``k`` contributors are held back (listed under
    ``suppressed`` with the reason). Per-installation contribution is capped
    at ``cap`` total counts per record.
    """
    fed = fed or FederationState()
    observed = _collect_observed(router)
    records: list[dict] = []
    suppressed: list[dict] = []
    contributor_counts: dict[str, int] = {}

    for context, arms in observed.items():
        contributors = fed.contributors(context) + 1  # +1: this installation
        contributor_counts[context] = contributors
        if contributors < k:
            suppressed.append({
                "context": context,
                "reason": f"k-anonymity: {contributors} contributor(s) < {k}",
            })
            continue
        for arm, (alpha, beta) in sorted(arms.items()):
            total = alpha + beta
            if total > cap:
                scale = cap / total
                alpha, beta = round(alpha * scale), round(beta * scale)
            pool = pools.get(arm)
            records.append({
                "schema": SCHEMA,
                "context": context,
                "arm": arm,
                "model": pool.model.ident() if pool else "unknown",
                "alpha": alpha,
                "beta": beta,
            })

    payload = {
        "schema": SCHEMA,
        "kind": "routing-quality",
        "records": records,
        "suppressed": suppressed,
        # Internal bookkeeping for merging/audit; stripped by push.
        "meta": {"contributors": contributor_counts},
    }
    return assert_payload_clean(payload)


def export_throughput(stats: Stats) -> dict:
    """The throughput dataset — zero task content, deliberately separate."""
    records = [
        {
            "schema": SCHEMA,
            "model": str(row.get("model", "unknown")),
            "quant": str(row.get("quant", "")),
            "context_length": int(row.get("context_length", 0)),
            "hardware": str(row.get("hardware", "unknown")),
            "tok_s": float(row.get("tok_s", 0.0)),
            "items_s": float(row.get("items_s", 0.0)),
            "load_s": float(row.get("load_s", 0.0)),
            "peak_gb": float(row.get("peak_gb", 0.0)),
        }
        for row in stats.throughput()
    ]
    return assert_payload_clean({"schema": SCHEMA, "kind": "throughput", "throughput": records})


# -- merge --------------------------------------------------------------------


def merge_exports(*payloads: dict) -> dict:
    """Federation is summation: alpha=sigma, beta=sigma, per (context, arm, model).

    Beta posteriors compose by ADDITION — merging N users is just summing
    counts, no gradients, no weights, no consensus protocol.
    """
    merged: dict[tuple[str, str, str], dict] = {}
    contributors: dict[str, int] = {}
    for payload in payloads:
        for rec in payload.get("records", []):
            key = (rec["context"], rec["arm"], rec["model"])
            slot = merged.setdefault(key, {
                "schema": SCHEMA, "context": rec["context"],
                "arm": rec["arm"], "model": rec["model"], "alpha": 0, "beta": 0,
            })
            slot["alpha"] += rec["alpha"]
            slot["beta"] += rec["beta"]
        for context, n in (payload.get("meta", {}).get("contributors") or {}).items():
            contributors[context] = contributors.get(context, 0) + int(n)
    out = {
        "schema": SCHEMA,
        "kind": "routing-quality",
        "records": sorted(merged.values(), key=lambda r: (r["context"], r["arm"])),
        "meta": {"contributors": contributors},
    }
    return assert_payload_clean(out)


# -- aggregate ------------------------------------------------------------------


def _trimmed_mean(values: list[float], trim: float) -> float:
    """Drop the top/bottom ``trim`` fraction, average the rest."""
    ordered = sorted(values)
    k = int(len(ordered) * trim)
    core = ordered[k:len(ordered) - k] or ordered
    return sum(core) / len(core)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def aggregate(
    directory: str | Path,
    *,
    k: int = K_ANONYMITY,
    cap: int = CONTRIBUTION_CAP,
    trim: float = 0.25,
) -> dict:
    """Build the publishable community aggregate from many contributor exports.

    Each ``*.json`` file in ``directory`` is ONE installation's export (the
    V1 infrastructure is a static file in a public repo, rebuilt nightly by
    CI — no server). Anti-gaming, applied per (context, arm, model) cell:

    - per-installation cap: each contributor's counts are scaled down to
      ``cap`` total before anything is combined
    - k-anonymity: a cell with fewer than ``k`` distinct contributors is
      suppressed, listed under ``suppressed`` with the reason
    - trimmed-mean robust aggregation: the published rate is the trimmed
      mean of per-contributor success rates, scaled to the MEDIAN
      contributor's total count — so no single contributor can dominate a
      cell with either an outlier rate or a mountain of counts
    """
    paths = sorted(Path(directory).glob("*.json"))
    # context -> (arm, model) -> [per-contributor (alpha, beta)]
    cells: dict[str, dict[tuple[str, str], list[tuple[float, float]]]] = {}
    contributors: dict[str, set[int]] = {}
    for idx, path in enumerate(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for rec in payload.get("records", []):
            alpha, beta = float(rec["alpha"]), float(rec["beta"])
            total = alpha + beta
            if total > cap:  # one installation cannot dominate a cell
                scale = cap / total
                alpha, beta = alpha * scale, beta * scale
            context = rec["context"]
            key = (rec["arm"], rec["model"])
            cells.setdefault(context, {}).setdefault(key, []).append((alpha, beta))
            contributors.setdefault(context, set()).add(idx)

    records: list[dict] = []
    suppressed: list[dict] = []
    contributor_counts: dict[str, int] = {}
    for context in sorted(cells):
        n_contributors = len(contributors[context])
        contributor_counts[context] = n_contributors
        if n_contributors < k:
            suppressed.append({
                "context": context,
                "reason": f"k-anonymity: {n_contributors} contributor(s) < {k}",
            })
            continue
        for (arm, model), observations in sorted(cells[context].items()):
            pairs = [(a, b) for a, b in observations if a + b > 0]
            if not pairs:
                continue
            rate = _trimmed_mean([a / (a + b) for a, b in pairs], trim)
            total = _median([a + b for a, b in pairs])
            records.append({
                "schema": SCHEMA,
                "context": context,
                "arm": arm,
                "model": model,
                "alpha": round(rate * total, 3),
                "beta": round((1 - rate) * total, 3),
            })
    payload = {
        "schema": SCHEMA,
        "kind": "routing-quality",
        "records": records,
        "suppressed": suppressed,
        "meta": {"contributors": contributor_counts},
    }
    return assert_payload_clean(payload)


# -- pull / push / status -------------------------------------------------------


def pull(source: str, fed: FederationState | None = None, *, trust: str = "community") -> dict:
    """Refresh community priors from a static JSON file (path or URL).

    Community counts are discounted 0.4x, team counts 0.7x before merging —
    the aggregate becomes your PRIOR; your own observations update on top and
    eventually dominate, because your counts grow and the downloaded prior
    does not.
    """
    discount = {"community": COMMUNITY_DISCOUNT, "team": TEAM_DISCOUNT}[trust]
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=15) as resp:  # noqa: S310 - user-configured URL
            payload = json.loads(resp.read().decode("utf-8"))
    else:
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
    fed = fed or FederationState()
    records = payload.get("records", [])
    contributors = payload.get("meta", {}).get("contributors") or {}
    fed.merge_pulled(records, contributors, discount)
    fed.data["last_pull"] = time.time()
    fed.save()
    return {"merged_records": len(records), "discount": discount}


def push(payload: dict, fed: FederationState | None = None, *, opt_in: bool = False,
         outbox: str | Path | None = None) -> Path:
    """Opt-in contribute. V1 has no server: writes a submission file for the
    community repo (rebuilt nightly by CI). Off by default — refuses unless
    ``opt_in`` is explicit. The internal meta block is stripped: what leaves
    is exactly the counts ``export`` showed you.
    """
    if not opt_in:
        raise PermissionError("federation is opt-in: pass --yes (or set federate.opt_in) to push")
    assert_payload_clean(payload)
    fed = fed or FederationState()
    shared = {
        "schema": payload["schema"],
        "kind": payload.get("kind", "routing-quality"),
        "records": payload.get("records", []),
    }
    if "throughput" in payload:
        shared["throughput"] = payload["throughput"]
    path = Path(outbox) if outbox is not None else (
        state_dir() / f"federation-submission-{int(time.time())}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(shared, indent=2, sort_keys=True), encoding="utf-8")
    fed.data["last_push"] = time.time()
    fed.save()
    return path


def status(router: Router, pools: dict[str, Pool], fed: FederationState | None = None) -> dict:
    """What's shared, when, and what's held back."""
    fed = fed or FederationState()
    payload = export(router, pools, fed)
    return {
        "publishable_cells": sorted({r["context"] for r in payload["records"]}),
        "held_back": payload["suppressed"],
        "contributors": payload["meta"]["contributors"],
        "community_prior_cells": sorted(fed.data["cells"].keys()),
        "last_pull": fed.data.get("last_pull"),
        "last_push": fed.data.get("last_push"),
    }
