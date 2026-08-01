#!/usr/bin/env python3
"""Reconcile `route:pending` beads into route-skill bandit rewards.

route-skill records an *impression* when it selects an arm and a *reward*
only when something calls `auto outcome`. Nothing did, so every arm
accumulated impressions with no rewards and the posterior decayed toward the
prior. This is the missing caller.

Input is beads that a real dispatch annotated (see `route-bead-hook.sh` beside this file):
they carry the routing decision in metadata and the `route:pending` label. The
hook never creates a bead, so closure here is a human's judgement that the
work is done -- which is what makes it worth learning from.

Fail open, always. A missing `bd`, a missing `auto`, or an unreachable beads
server exits 0 with a line on stderr: this runs at session start and must
never be the reason a session fails to begin.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PENDING = "route:pending"
RECORDED = "route:recorded"
SKIPPED = "route:skipped"

#: Metadata the `decision` event writes; without all three there is nothing
#: to attribute a reward to.
REQUIRED_FIELDS = ("shape", "tier", "arm")

DEFAULT_STALE_DAYS = 14


def warn(message: str) -> None:
    print(f"route-reconcile: {message}", file=sys.stderr)


@dataclass(frozen=True)
class Verdict:
    """What to do with one bead.

    action "record" carries an `outcome` for `auto outcome`; "skip" relabels
    with no reward at all; "leave" touches nothing.
    """

    action: str
    outcome: str | None
    reason: str


def _parse_rfc3339(value: object) -> float | None:
    """bd emits `2026-07-30T15:38:53Z`. Anything else is not a timestamp.

    `%z` has accepted a literal `Z` as UTC since Python 3.7, so this parses
    tz-aware in one step -- no separate `.replace(tzinfo=...)` needed.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    return parsed.timestamp()


def dispatch_ts(bead: dict) -> float | None:
    """When the dispatch happened, for ageing.

    `decision_ts` is exact. `updated_at` is the fallback for a bead annotated
    by an unpatched route; it moves whenever anything touches the bead, which
    makes it conservative -- it can only ever delay ageing, never hasten it.
    """
    metadata = bead.get("metadata") or {}
    ts = metadata.get("decision_ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    return _parse_rfc3339(bead.get("updated_at"))


def is_stale(bead: dict, *, now: float, stale_days: int) -> bool:
    """Has this dispatch been sitting long enough to stop calling it in flight?

    Shared by both ageing rules so they cannot drift apart. A bead with no
    usable timestamp is never stale: there is no clock to age it against, and
    guessing would be worse than leaving it. bd always writes `updated_at`, so
    that case is a malformed bead rather than a routine one.
    """
    started = dispatch_ts(bead)
    return started is not None and now - started > stale_days * 86_400


def missing_fields(bead: dict) -> list[str]:
    metadata = bead.get("metadata") or {}
    return [field for field in REQUIRED_FIELDS if not metadata.get(field)]


def classify(bead: dict, *, now: float, stale_days: int) -> Verdict:
    """Apply the decision table. First match wins; order is load-bearing.

    The abort and not-yet-finished rules sit above every status rule on
    purpose: neither a Ctrl-C nor an in-flight dispatch is a verdict on the
    arm, whatever the bead's status happens to be.
    """
    metadata = bead.get("metadata") or {}
    status = bead.get("status", "")
    exit_code = metadata.get("exit_code")

    # 1. `complete` never fired: the dispatch has not finished (or crashed
    #    hard enough that the hook never ran). Past --stale-days the second
    #    reading is the only credible one -- no dispatch runs for two weeks --
    #    so the bead is abandoned and gets released. `skip`, not `failed`:
    #    the arm never reported anything, so there is nothing to punish.
    #    Without this escape the pending set grows without bound, because
    #    rule 8's ageing is unreachable while `exit_code` is missing.
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        if is_stale(bead, now=now, stale_days=stale_days):
            return Verdict(
                "skip", None, f"no completion event in {stale_days} days — abandoned"
            )
        return Verdict("leave", None, "dispatch has not finished")

    # 2. The user aborted. Not the arm's fault; record nothing either way.
    if exit_code == 130 or metadata.get("exception") == "KeyboardInterrupt":
        return Verdict("skip", None, "user aborted the dispatch")

    # 3. Deferred or blocked work is a scheduling fact, not a verdict on the
    #    arm -- a bead stuck behind an unrelated dependency is no different
    #    from one someone chose to look at later.
    if status in ("deferred", "blocked"):
        return Verdict("leave", None, f"bead is {status}")

    # 4/5. Closed. A clean dispatch that ended in closure is the real signal.
    if status == "closed":
        if exit_code == 0:
            return Verdict("record", "accepted", "closed after a clean dispatch")
        return Verdict(
            "record", "failed", f"closed, but the dispatch exited {exit_code}"
        )

    # 6. Closed once, open now: someone reopened it. The output did not hold.
    if bead.get("closed_at"):
        return Verdict("record", "failed", "reopened after closing")

    # 7. The dispatch errored and the work is still open.
    if exit_code != 0:
        return Verdict("record", "failed", f"dispatch exited {exit_code}")

    # 8. Clean dispatch, nobody closed it, and it has been long enough that
    #    "in flight" is no longer a credible reading.
    if is_stale(bead, now=now, stale_days=stale_days):
        return Verdict("record", "failed", f"open for more than {stale_days} days")

    # 9. Still in flight.
    return Verdict("leave", None, "still in flight")


BD_TIMEOUT_S = 30
AUTO_TIMEOUT_S = 30


def _run(argv: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def resolve_auto() -> str | None:
    """route's CLI. Not on PATH by default -- it lives in its own venv."""
    override = os.environ.get("ROUTE_AUTO")
    if override:
        return override if Path(override).is_file() else None
    found = shutil.which("auto")
    if found:
        return found
    fallback = Path.home() / ".route-env" / "bin" / "auto"
    return str(fallback) if fallback.is_file() else None


def fetch_pending(cwd: Path) -> list[dict]:
    """Beads awaiting a verdict.

    `--all` is not optional: closed beads are the population of interest and
    bd hides them by default. `--exclude-label route:recorded` is what makes a
    bead claimed by a crashed run inert instead of double-counted. `--limit 0`
    is equally not optional: `bd list` defaults to 50 results, and rule 1
    ("leave") beads sort ahead of everything else forever -- past 50 of them,
    newly-closed beads fall off the end of the page and become permanently
    invisible to this reconciler, silently, with exit 0.
    """
    proc = _run(
        [
            "bd",
            "list",
            "--label",
            PENDING,
            "--exclude-label",
            RECORDED,
            "--all",
            "--limit",
            "0",
            "--json",
        ],
        cwd,
        BD_TIMEOUT_S,
    )
    if proc.returncode != 0:
        warn(f"bd list failed: {(proc.stderr.strip().splitlines() or [''])[0]}")
        return []
    try:
        beads = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        warn("bd list returned output that is not JSON")
        return []
    return beads if isinstance(beads, list) else []


def label_add(bead_id: str, label: str, cwd: Path) -> bool:
    return (
        _run(["bd", "label", "add", bead_id, label], cwd, BD_TIMEOUT_S).returncode == 0
    )


def label_remove(bead_id: str, label: str, cwd: Path) -> bool:
    return (
        _run(["bd", "label", "remove", bead_id, label], cwd, BD_TIMEOUT_S).returncode
        == 0
    )


def record_outcome(auto: str, bead: dict, outcome: str, cwd: Path) -> bool:
    """Hand the verdict to route. Counts and enum values only -- never task text."""
    metadata = bead.get("metadata") or {}
    argv = [
        auto,
        "outcome",
        "--shape",
        str(metadata["shape"]),
        "--tier",
        str(metadata["tier"]),
        "--arm",
        str(metadata["arm"]),
        "--outcome",
        outcome,
    ]
    ts = metadata.get("decision_ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        argv += ["--decision-ts", repr(float(ts))]
    proc = _run(argv, cwd, AUTO_TIMEOUT_S)
    if proc.returncode != 0:
        warn(f"auto outcome failed: {(proc.stderr.strip().splitlines() or [''])[0]}")
        return False
    return True


def reconcile(cwd: Path, *, now: float, stale_days: int, dry_run: bool) -> list[dict]:
    auto = resolve_auto()
    if auto is None and not dry_run:
        warn("route's `auto` CLI not found (set ROUTE_AUTO) — nothing reconciled")
        return []
    # A dry run never calls `auto` (see the `v.action == "leave" or dry_run:
    # continue` guard below), so it has no dependency on route being
    # installed at all -- and a machine without route installed is exactly
    # where previewing what reconciliation *would* do is most useful.

    results: list[dict] = []
    for bead in fetch_pending(cwd):
        bead_id = str(bead.get("id", ""))
        # `entry` is the result dict already appended for this bead, if any --
        # tracked so the except clause below can mark it "error" in place
        # instead of appending a second, duplicate entry for the same bead.
        entry: dict | None = None
        try:
            absent = missing_fields(bead)
            if absent:
                reason = f"metadata missing {', '.join(absent)}"
                warn(f"{bead_id}: {reason} — left pending")
                # Seen and rejected, not silently dropped: still counted so
                # `--json` reflects the population actually fetched.
                results.append(
                    {
                        "id": bead_id,
                        "action": "skipped-incomplete",
                        "outcome": None,
                        "reason": reason,
                    }
                )
                continue

            v = classify(bead, now=now, stale_days=stale_days)
            entry = {
                "id": bead_id,
                "action": v.action,
                "outcome": v.outcome,
                "reason": v.reason,
            }
            results.append(entry)
            if v.action == "leave" or dry_run:
                continue

            # Claim first. A crash between here and the record loses one
            # sample; the other order double-counts, which is the failure
            # nobody can see.
            claim = RECORDED if v.action == "record" else SKIPPED
            if not label_add(bead_id, claim, cwd):
                warn(f"{bead_id}: could not claim ({claim}) — left pending")
                entry["action"] = "error"
                continue

            if v.action == "record":
                assert v.outcome is not None  # guaranteed by classify()
                # guaranteed by the `dry_run` continue above: this branch is
                # only reachable when dry_run is False, and the entry guard
                # already rejected `auto is None and not dry_run`.
                assert auto is not None
                if not record_outcome(auto, bead, v.outcome, cwd):
                    warn(
                        f"{bead_id}: claimed but not recorded — signal lost, "
                        "not double-counted"
                    )
                    entry["action"] = "error"
                    continue

            if not label_remove(bead_id, PENDING, cwd):
                warn(f"{bead_id}: recorded, but `route:pending` could not be removed")
        except Exception as exc:  # noqa: BLE001 -- one bad bead must not blank the run
            warn(f"{bead_id}: unexpected failure, left as-is: {exc!r}")
            if entry is not None:
                entry["action"] = "error"
            else:
                results.append(
                    {
                        "id": bead_id,
                        "action": "error",
                        "outcome": None,
                        "reason": repr(exc),
                    }
                )

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="route-reconcile",
        description="Turn bead lifecycle into route-skill bandit rewards.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="beads workspace to reconcile (default: cwd)",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=DEFAULT_STALE_DAYS,
        help="a clean dispatch nobody closed in this many days "
        f"counts as failed (default: {DEFAULT_STALE_DAYS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the verdicts, change nothing"
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the verdicts as JSON"
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="say nothing when there was nothing to reconcile "
        "(for the session-start hook, whose stdout becomes model context)",
    )
    args = parser.parse_args(argv)

    if shutil.which("bd") is None:
        warn("bd not installed — nothing reconciled")
        return 0

    try:
        results = reconcile(
            args.repo,
            now=time.time(),
            stale_days=args.stale_days,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 -- fail open: this runs at session start
        warn(f"unexpected failure, nothing further reconciled: {exc!r}")
        return 0

    acted = [r for r in results if r["action"] != "leave"]

    # `--quiet` is for the session-start hook: this runs at the top of every
    # session in every repo, and its stdout is injected into the model's
    # context. A "reconciled 0 of 0 pending" line every time is pure noise,
    # so with `--quiet` an empty run says nothing at all. Anything actually
    # acted on -- including a bead deliberately left alone -- still reports,
    # because that is a fact about the session's repo worth surfacing.
    if args.quiet and not acted:
        return 0

    if args.as_json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for r in acted:
            label = r["outcome"] or r["action"]
            print(f"{r['id']}: {label} ({r['reason']})")
        # The numerator is beads actually acted on -- "leave" was already
        # excluded above; "skipped-incomplete" and "error" are beads that
        # were seen and explicitly left untouched (incomplete metadata) or
        # left in an indeterminate state (a claim/record/release failure),
        # not beads whose fate was recorded.
        untouched = ("skipped-incomplete", "error")
        counted = [r for r in acted if r["action"] not in untouched]
        prefix = "would reconcile" if args.dry_run else "reconciled"
        print(f"{prefix} {len(counted)} of {len(results)} pending")
    return 0


if __name__ == "__main__":
    sys.exit(main())
