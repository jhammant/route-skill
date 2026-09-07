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
import os
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
from .eligibility import SensitiveRoutingError, fetch_quota, gate
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
from .pools import load_pools, pools_for_host
from .router import Router, cell_name
from .eligibility import probably_private
from .sensitive import detect_sensitive
from .shapes import classify_shape
from .ship import RunResult, ShipAbort, build_pr_body, ship
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

    host = args.host
    pools = pools_for_host(load_pools(), host)
    shape = args.shape or classify_shape(text, use_llm=not args.no_llm)
    tier = args.tier or estimate_complexity(text)
    quota = fetch_quota() if not args.no_quota else {}

    # dataClass: 'open' by default, 'sensitive' only on an explicit signal
    # (--sensitive, a sensitive:true task field, or the sensitive.toml path
    # allowlist). Never inferred from content heuristics.
    sensitive = detect_sensitive(text, flag=args.sensitive)
    # The content regex is a one-way safety net: it may only ever move a task
    # toward MORE caution, never less. If it thinks this might be private, the
    # safest destination is the machine the data cannot leave — not Claude,
    # which is also a remote service. Treating a regex hit as `sensitive`
    # unifies both paths on local-preferred instead of leaving the
    # auto-detected case (the one users actually hit) with the worse outcome.
    #
    # It is never the reverse: the regex NOT firing proves nothing, which is why
    # detect_sensitive stays explicit-only.
    auto_sensitive = False
    if not sensitive and probably_private(text):
        sensitive = True
        auto_sensitive = True

    try:
        result = gate(
            text,
            shape,
            pools,
            quota=quota,
            pool_pin=args.pool,
            private=args.private,
            sensitive=sensitive,
            no_acceptance=args.no_acceptance,
            needs_context=args.needs_context,
            cross_repo=args.cross_repo,
            host=host,
        )
    except (SensitiveRoutingError, ValueError) as exc:
        # Fail loudly — never silently fall back to a remote arm.
        print(f"route: {exc}", file=sys.stderr)
        return 1

    router = _build_router()
    if result.veto is not None:
        chosen = result.veto.target
        why = f"veto:{result.veto.name}"
    else:
        # Sensitive work PREFERS local arms over claude: free, private, and
        # the data physically never leaves the machine.
        prefer = [a for a in result.eligible if not pools[a].remote] if sensitive else None
        chosen = router.select(shape, tier, result.eligible, prefer=prefer)
        observed = router.observed_counts(shape, tier)
        why = "posterior" if any(sum(v) for v in observed.values()) else "prior"
        if prefer and chosen in prefer:
            why += "+sensitive:local-preferred"
        if auto_sensitive:
            why += "(auto-detected)"

    plan = {
        "host": host,
        "shape": shape,
        "tier": tier,
        "cell": cell_name(shape, tier),
        "data_class": result.data_class,
        "eligible": result.eligible,
        "removed_by_quota": result.removed_by_quota,
        "chosen": chosen,
        "why": why,
    }
    if result.removed_by_sensitivity:
        plan["removed_by_sensitivity"] = result.removed_by_sensitivity
    if result.veto is not None:
        plan["veto"] = {"name": result.veto.name, "reason": result.veto.reason}
    print(json.dumps(plan, indent=2, sort_keys=True))

    Stats().record_decision(Decision(
        cell=plan["cell"], shape=shape, tier=tier,
        eligible=result.eligible, chosen=chosen, why=why,
    ))

    if args.plan_only:
        print("dispatch: plan only (nothing launched)")
        return 0

    if chosen == host:
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

    return _dispatch(pool.dispatch, text, target=chosen)


def _dispatch(template: str, task: str, *, target: str | None = None) -> int:
    if not template:
        print("dispatch: no command configured for this pool", file=sys.stderr)
        return 1
    argv = [task if tok == "{task}" else tok for tok in shlex.split(template)]
    if "{task}" not in shlex.split(template):
        argv.append(task)
    started = time.monotonic()
    try:
        # A delegated agent must see itself as host if it invokes route again.
        env = dict(os.environ)
        if target in ("claude", "codex"):
            env["ROUTE_HOST"] = target
        proc = subprocess.run(argv, env=env)
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


def _run_cmd(argv: list[str]) -> RunResult:
    """Real runner for ship: one subprocess, captured, never a shell."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
    except OSError as exc:
        return RunResult(127, "", str(exc))
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


def _confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def cmd_ship(args: argparse.Namespace) -> int:
    task_desc = " ".join(args.task_desc).strip()
    body = build_pr_body(
        shape=args.shape or "(unrouted)",
        tier=args.tier or "?",
        arm=args.arm or "?",
        why=args.why or "?",
        verification=args.verification or "?",
        task=task_desc,
    )
    try:
        result = ship(
            reader=_run_cmd,
            runner=_run_cmd,
            confirm=_confirm,
            display=print,
            repo=args.repo,
            base=args.base,
            draft=args.draft,
            open_pr=args.pr,
            title=args.title or task_desc,
            body=body,
            show_diff=args.diff,
        )
    except ShipAbort as exc:
        print(f"route ship: {exc}", file=sys.stderr)
        return 1
    for message in result.messages:
        print(message)
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

    sh = sub.add_parser("ship", help="push the task branch and open a PR with routing provenance")
    sh.add_argument("--pr", action="store_true",
                    help="open a PR via gh (default: push and print the compare URL)")
    sh.add_argument("--repo", help="target owner/name — forks first when you lack push access")
    sh.add_argument("--base", help="PR base branch (default: the repo's default branch)")
    sh.add_argument("--draft", action="store_true", help="open the PR as a draft")
    sh.add_argument("--diff", action="store_true", help="show the full diff, not just --stat")
    sh.add_argument("--title", default="", help="PR title (default: the task description)")
    sh.add_argument("--shape", default="", help="provenance: routed shape")
    sh.add_argument("--tier", default="", help="provenance: complexity tier")
    sh.add_argument("--arm", default="", help="provenance: arm that did the work")
    sh.add_argument("--why", default="", help="provenance: why that arm was chosen")
    sh.add_argument("--verification", default="", help="provenance: e.g. '62 tests pass'")
    sh.add_argument("task_desc", nargs="*", help="the task description, carried into the PR body")
    sh.set_defaults(func=cmd_ship)
    return p


def _add_task_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("task", nargs="*")
    p.add_argument("--host", choices=["claude", "codex"],
                   default=os.environ.get("ROUTE_HOST", "claude"),
                   help="current agent; context-dependent work stays here (default: ROUTE_HOST or claude)")
    p.add_argument("--plan-only", action="store_true", help="show routing without prompting or dispatching")
    p.add_argument("--pool", help="pin a pool explicitly — beats inference")
    p.add_argument("--shape", help="override shape classification")
    p.add_argument("--tier", help="override complexity tier")
    p.add_argument("--private", action="store_true", help="veto: data must not leave the machine")
    p.add_argument("--sensitive", action="store_true",
                   help="dataClass sensitive: veto remote pools (codex/kimi/free), prefer local")
    p.add_argument("--no-acceptance", action="store_true", help="veto: no clear acceptance check")
    p.add_argument("--needs-context", action="store_true", help="veto: needs this conversation")
    p.add_argument("--cross-repo", action="store_true", help="veto: cross-repo orchestration")
    p.add_argument("--no-llm", action="store_true", help="skip the local-LLM shape tiebreak")
    p.add_argument("--no-quota", action="store_true", help="skip quotamax")


_SUBCOMMANDS = {"task", "stats", "outcome", "federate", "benchmark", "ship"}


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
