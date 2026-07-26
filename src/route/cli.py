"""route / auto / stats / federate / benchmark — the command line.

``route <task>`` decides and explains, then waits for confirmation.
``auto <task>`` decides and dispatches immediately. Dispatch hands to the
existing skills (``codex``, ``kimi``, ``local-llm``) or returns ``stay``
for Claude — it never reimplements what those already do, and it never
triggers paid provisioning.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .benchmark import (
    BENCHMARK_DISCOUNT,
    SUITES,
    enable_seed,
    local_llm_throughput_path,
    run_benchmark,
    seed_priors,
)
from .complexity import estimate_complexity
from .eligibility import fetch_quota, gate
from .federate import (
    FederationState,
    aggregate,
    assert_payload_clean,
    export,
    export_throughput,
    merge_exports,
    pull,
    push,
    status,
)
from .pools import load_pools
from .router import Router, cell_name
from .shapes import classify_shape
from .stats import Decision, Stats, report_arm, report_cells, report_throughput
from .storage import JsonStorage


def _build_router() -> Router:
    fed = FederationState()
    storage = JsonStorage()
    # Community priors (0.4x) and, if `benchmark --seed` was used, benchmark
    # priors (0.3x) both seed the live cells; own observations grow past them.
    community: dict[str, dict[str, tuple[float, float]]] = fed.community_priors()
    for context, arms in seed_priors(storage).items():
        cell = community.setdefault(context, {})
        for arm, (a, b) in arms.items():
            have_a, have_b = cell.get(arm, (0.0, 0.0))
            cell[arm] = (have_a + a, have_b + b)
    return Router(storage=storage, community=community)


def decide(args: argparse.Namespace, *, auto: bool) -> int:
    text = " ".join(args.task).strip()
    if not text:
        print("route: empty task", file=sys.stderr)
        return 2

    pools = load_pools()
    shape = args.shape or classify_shape(text, use_llm=not args.no_llm)
    tier = args.tier or estimate_complexity(text)
    quota = fetch_quota() if not args.no_quota else {}

    result = gate(
        text,
        shape,
        pools,
        quota=quota,
        pool_pin=args.pool,
        private=args.private,
        no_acceptance=args.no_acceptance,
        needs_context=args.needs_context,
        cross_repo=args.cross_repo,
    )

    router = _build_router()
    if result.veto is not None:
        chosen = result.veto.target
        why = f"veto:{result.veto.name}"
    else:
        chosen = router.select(shape, tier, result.eligible)
        observed = router.observed_counts(shape, tier)
        why = "posterior" if any(sum(v) for v in observed.values()) else "prior"

    plan = {
        "shape": shape,
        "tier": tier,
        "cell": cell_name(shape, tier),
        "eligible": result.eligible,
        "removed_by_quota": result.removed_by_quota,
        "chosen": chosen,
        "why": why,
    }
    if result.veto is not None:
        plan["veto"] = {"name": result.veto.name, "reason": result.veto.reason}
    print(json.dumps(plan, indent=2, sort_keys=True))

    Stats().record_decision(Decision(
        cell=plan["cell"], shape=shape, tier=tier,
        eligible=result.eligible, chosen=chosen, why=why,
    ))

    if chosen == "claude":
        print("dispatch: stay (this task belongs here)")
        return 0

    pool = pools[chosen]
    if not auto:
        rendered = pool.dispatch.replace("{task}", shlex.quote(text))
        try:
            answer = input(f"dispatch to {chosen} via `{rendered}`? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("not dispatched")
            return 0

    return _dispatch(pool.dispatch, text)


def _dispatch(template: str, task: str) -> int:
    if not template:
        print("dispatch: no command configured for this pool", file=sys.stderr)
        return 1
    argv = [task if tok == "{task}" else tok for tok in shlex.split(template)]
    if "{task}" not in shlex.split(template):
        argv.append(task)
    started = time.monotonic()
    try:
        proc = subprocess.run(argv)
    except OSError as exc:
        print(f"dispatch failed: {exc}", file=sys.stderr)
        return 1
    print(f"dispatch: exit {proc.returncode} in {time.monotonic() - started:.1f}s")
    return proc.returncode


def cmd_stats(args: argparse.Namespace) -> int:
    router = _build_router()
    if args.throughput:
        rows = report_throughput(Stats())
    elif args.arm:
        rows = report_arm(router, args.arm)
    else:
        rows = report_cells(router)
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


def cmd_outcome(args: argparse.Namespace) -> int:
    from .outcomes import OUTCOME_EVENTS, record_outcome

    router = _build_router()
    record_outcome(router, args.shape, args.tier, args.arm, args.outcome)
    Stats().record_outcome_events(list(OUTCOME_EVENTS[args.outcome]))
    print(json.dumps({"recorded": args.outcome, "arm": args.arm,
                      "cell": cell_name(args.shape, args.tier)}))
    return 0


def cmd_federate(args: argparse.Namespace) -> int:
    router = _build_router()
    pools = load_pools()
    fed = FederationState()
    if args.federate_cmd == "export":
        payload = export(router, pools, fed)
        if args.throughput:
            payload["throughput"] = export_throughput(Stats())["throughput"]
            assert_payload_clean(payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        print("# nothing was shared — this is exactly what `push` would send",
              file=sys.stderr)
    elif args.federate_cmd == "push":
        payload = export(router, pools, fed)
        if args.throughput:
            payload["throughput"] = export_throughput(Stats())["throughput"]
        try:
            path = push(payload, fed, opt_in=args.yes)
        except PermissionError as exc:
            print(f"route federate push: {exc}", file=sys.stderr)
            return 1
        print(f"wrote submission: {path}")
        print("submit it to the community repo; CI rebuilds the aggregate nightly.")
    elif args.federate_cmd == "pull":
        result = pull(args.source, fed, trust=args.trust)
        print(json.dumps(result, sort_keys=True))
    elif args.federate_cmd == "merge":
        payloads = []
        for name in args.files:
            payloads.append(json.loads(Path(name).read_text(encoding="utf-8")))
        if not payloads:
            print("route federate merge: give at least one export file", file=sys.stderr)
            return 2
        # Beta posteriors compose by ADDITION: alpha=sigma, beta=sigma.
        print(json.dumps(merge_exports(*payloads), indent=2, sort_keys=True))
    elif args.federate_cmd == "aggregate":
        if not args.files:
            print("route federate aggregate: give a directory of exports", file=sys.stderr)
            return 2
        payload = aggregate(args.files[0])
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            print(f"wrote community aggregate: {out_path}")
        else:
            print(json.dumps(payload, indent=2, sort_keys=True))
    elif args.federate_cmd == "status":
        print(json.dumps(status(router, pools, fed), indent=2, sort_keys=True))
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    pools = load_pools()
    if args.arm:
        arms = [args.arm]
    elif args.compare:
        arms = [a.strip() for a in args.compare.split(",") if a.strip()]
    else:
        arms = list(pools)
    unknown = [a for a in arms if a not in pools]
    if unknown:
        print(f"route benchmark: unknown arm(s) {unknown}", file=sys.stderr)
        return 2
    suites = [args.suite] if args.suite else list(SUITES)
    results = run_benchmark(
        arms,
        suites,
        pools,
        storage=JsonStorage(),
        stats=Stats(),
        local_llm_path=local_llm_throughput_path(),
    )
    seeded = False
    if args.seed:
        # Merge benchmark evidence into live priors at 0.3x — below the 0.4x
        # community weight. Synthetic evidence never outranks real work.
        enable_seed()
        seeded = True
    print(json.dumps({
        "results": [asdict(r) for r in results],
        "namespace": "bench:{shape}:{tier}",
        "seeded": seeded,
        "seed_discount": BENCHMARK_DISCOUNT if seeded else None,
    }, indent=2, sort_keys=True))
    return 0


def _parser(prog: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command")

    t = sub.add_parser("task", help="route a task (default)")
    _add_task_flags(t)

    s = sub.add_parser("stats")
    s.add_argument("--arm")
    s.add_argument("--throughput", action="store_true")
    s.set_defaults(func=cmd_stats)

    o = sub.add_parser("outcome", help="record what happened")
    o.add_argument("--shape", required=True)
    o.add_argument("--tier", required=True)
    o.add_argument("--arm", required=True)
    o.add_argument("--outcome", required=True, choices=["accepted", "verified", "completed", "failed"])
    o.set_defaults(func=cmd_outcome)

    f = sub.add_parser("federate")
    f.add_argument("federate_cmd", choices=["export", "push", "pull", "status", "merge", "aggregate"])
    f.add_argument("files", nargs="*",
                   help="merge: export files to sum; aggregate: directory of exports")
    f.add_argument("--out", help="aggregate: write the community aggregate here")
    f.add_argument("--yes", action="store_true", help="opt in to sharing (push only)")
    f.add_argument("--from", "--source", dest="source", default="",
                   help="community prior file or URL (pull)")
    f.add_argument("--trust", choices=["community", "team"], default="community")
    f.add_argument("--throughput", action="store_true", help="include the throughput dataset")
    f.set_defaults(func=cmd_federate)

    b = sub.add_parser("benchmark", help="manufacture evidence instead of waiting for it")
    b.add_argument("--arm", help="benchmark one arm (e.g. a model just downloaded)")
    b.add_argument("--suite", choices=list(SUITES), help="run one suite")
    b.add_argument("--seed", action="store_true",
                   help="merge results into live priors at 0.3x (below the 0.4x community weight)")
    b.add_argument("--compare", help="comma-separated arms, head-to-head on the same tasks")
    b.set_defaults(func=cmd_benchmark)
    return p


def _add_task_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("task", nargs="*")
    p.add_argument("--pool", help="pin a pool explicitly — beats inference")
    p.add_argument("--shape", help="override shape classification")
    p.add_argument("--tier", help="override complexity tier")
    p.add_argument("--private", action="store_true", help="veto: data must not leave the machine")
    p.add_argument("--no-acceptance", action="store_true", help="veto: no clear acceptance check")
    p.add_argument("--needs-context", action="store_true", help="veto: needs this conversation")
    p.add_argument("--cross-repo", action="store_true", help="veto: cross-repo orchestration")
    p.add_argument("--no-llm", action="store_true", help="skip the local-LLM shape tiebreak")
    p.add_argument("--no-quota", action="store_true", help="skip quotamax")


_SUBCOMMANDS = {"task", "stats", "outcome", "federate", "benchmark"}


def _run(argv: list[str] | None, *, auto: bool) -> int:
    argv = list(argv or [])
    # Bare task text is the default command; route it to the task parser.
    if argv and argv[0] not in _SUBCOMMANDS:
        argv = ["task", *argv]
    elif not argv:
        argv = ["task"]
    parser = _parser("auto" if auto else "route")
    args = parser.parse_args(argv)
    if getattr(args, "func", None):
        return args.func(args)
    return decide(args, auto=auto)


def main(argv: list[str] | None = None) -> int:
    return _run(argv if argv is not None else sys.argv[1:], auto=False)


def main_auto(argv: list[str] | None = None) -> int:
    return _run(argv if argv is not None else sys.argv[1:], auto=True)


if __name__ == "__main__":
    raise SystemExit(main())
