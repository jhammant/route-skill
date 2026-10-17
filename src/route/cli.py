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
from contextlib import ExitStack
from copy import copy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import __version__
from .benchmark import (
    BENCHMARK_DISCOUNT,
    SUITES,
    enable_seed,
    local_llm_throughput_path,
    run_benchmark,
    seed_priors,
)
from .complexity import TIERS, estimate_complexity, is_long_horizon
from .eligibility import GateResult, SensitiveRoutingError, fetch_quota, gate
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
from .pools import Pool, load_pools, pools_for_host
from .memory import TaskMemory
from .outcomes import OUTCOME_EVENTS, record_failure, record_score, score_from_fix_passes, validate_score
from .router import Router, cell_name
from .eligibility import QUOTA_PROVIDER, fetch_usage, probably_private, quotamax_available
from .sensitive import detect_sensitive
from .shapes import SHAPES, ShapeClassification, classify_with_confidence
from .ship import RunResult, ShipAbort, build_pr_body, ship
from .stats import report_quota, Decision, Stats, report_arm, report_cells, report_throughput
from .storage import JsonStorage
from .surplus import load_surplus_k, quota_adjustment
from .swarm import detect_swarm, plan_swarm


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


@dataclass
class PlanContext:
    pools: dict[str, Pool]
    result: GateResult
    decision: Decision
    template: str
    task_on_stdin: bool
    router: Router


class PlanningError(ValueError):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def plan_task(text: str, opts: argparse.Namespace, *, quota=None, exclude=(),
              record: bool = True) -> tuple[dict, PlanContext]:
    """Plan without dispatching; record=False leaves routing state untouched."""
    args = opts
    if not text.strip():
        raise PlanningError("empty task", 2)
    host = args.host
    pools = pools_for_host(load_pools(), host)
    classification = (ShapeClassification(args.shape, "high", "explicit") if args.shape else
                      classify_with_confidence(text, use_llm=args.llm_shape))
    shape = classification.shape
    tier = args.tier or estimate_complexity(text)
    long_horizon = is_long_horizon(text)
    if quota is None:
        quota = ({} if args.no_quota else
                 fetch_quota() if record else fetch_quota(cache_result=False))
    # Quota gating is fail-open by contract, but a silent no-op is how a
    # 97%-used pool keeps getting picked. Say so in the plan.
    if args.no_quota:
        quota_status = "skipped"
    elif quota:
        quota_status = "live"
    elif not quotamax_available():
        quota_status = "unavailable: quotamax not on PATH — no arm removed"
    else:
        quota_status = "unavailable: quotamax returned no data — no arm removed"

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
    if getattr(args, "allow_network", False) and sensitive:
        raise PlanningError("--allow-network cannot be combined with a sensitive task", 2)

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
        raise PlanningError(str(exc)) from exc

    survivors = [arm for arm in result.eligible if arm not in exclude]
    if survivors:
        result.eligible = survivors
    router = _build_router()
    if not record:
        router = copy(router)
        router.storage = router.storage.snapshot()
    memory = TaskMemory()
    adjustments = {arm: 0.0 for arm in result.eligible}
    neighbours = []
    if result.veto is not None:
        chosen = result.veto.target
        why = f"veto:{result.veto.name}"
    else:
        # Sensitive work PREFERS local arms over claude: free, private, and
        # the data physically never leaves the machine.
        prefer = [a for a in result.eligible if not pools[a].remote] if sensitive else None
        # A plan is not execution evidence: dispatch or settlement owns the trial.
        observed = router.observed_counts(shape, tier)
        candidates = prefer or result.eligible
        weekly = getattr(quota, "weekly", {})
        k = load_surplus_k()
        for arm in candidates:
            provider = QUOTA_PROVIDER.get(arm)
            window = weekly.get(provider, {})
            adjustments[arm] = quota_adjustment(
                window.get("percentUsed"), window.get("resetsInMinutes"), k,
                headroom=quota.get(provider))
        if record or memory.salt_path.is_file():
            neighbours = memory.neighbours(text, shape, candidates)
        chosen = router.select(shape, tier, result.eligible, prefer=prefer,
                               record_impression=False, neighbours=neighbours, quota_adjustment=adjustments)
        why = "posterior" if any(sum(v) for v in observed.values()) else "prior"
        if long_horizon and not args.tier:
            why += "+long-horizon"
        if prefer and chosen in prefer:
            why += "+sensitive:local-preferred"
        if auto_sensitive:
            why += "(auto-detected)"
        if adjustments[chosen] > 0:
            why += f"+surplus:{chosen}"
        elif adjustments[chosen] < 0:
            why += f"-pressure:{chosen}"

    decision = Decision(cell=cell_name(shape, tier), shape=shape, tier=tier,
                        eligible=result.eligible, chosen=chosen, why=why)
    if record:
        Stats().record_decision(decision)
        memory.remember(decision.decision_id, text, shape, tier, chosen)

    plan = {
        "decision_id": decision.decision_id,
        "host": host,
        "shape": shape,
        "tier": tier,
        "horizon": "long" if long_horizon else "short",
        "cell": cell_name(shape, tier),
        "data_class": result.data_class,
        "eligible": result.eligible,
        "removed_by_quota": result.removed_by_quota,
        "removed_by_cost": result.removed_by_cost,
        "quota": quota_status,
        "chosen": chosen,
        "why": why,
        "shape_confidence": classification.confidence,
        "shape_source": classification.source,
        "quota_adjustment": adjustments,
        "surplus_bonus": adjustments,  # deprecated signed alias for one release
        "neighbours": neighbours,
    }
    if classification.confidence == "low":
        plan["suggest"] = "pass --shape <shape> --tier <tier>"
    if result.removed_by_sensitivity:
        plan["removed_by_sensitivity"] = result.removed_by_sensitivity
    if result.veto is not None:
        plan["veto"] = {"name": result.veto.name, "reason": result.veto.reason}
    if getattr(args, "allow_network", False):
        plan["allow_network"] = True  # on the record: this run may reach the network
    hint = detect_swarm(text, shape)
    cap = plan_swarm(hint.units, pools[chosen].parallel_limit,
                     quota.get(QUOTA_PROVIDER.get(chosen)) or "comfortable")
    plan["swarm"] = {**asdict(hint), "cap": cap,
                     "suggested": hint.suggested and cap >= 2}
    if hint.suggested and cap < 2:
        headroom = quota.get(QUOTA_PROVIDER.get(chosen)) or "comfortable"
        plan["swarm"]["reason"] = f"quota: {chosen} headroom {headroom}"
    override = getattr(args, "swarm", None)
    if override is not None:
        plan["swarm"].update(suggested=override,
                             reason="user:--swarm" if override else "user:--no-swarm")
    template, task_on_stdin = pools[chosen].dispatch_for(shape)
    return plan, PlanContext(pools, result, decision, template, task_on_stdin, router)


def decide(args: argparse.Namespace, *, auto: bool) -> int:
    text = " ".join(args.task).strip()
    try:
        plan, ctx = plan_task(text, args)
    except PlanningError as exc:
        print(f"route: {exc}", file=sys.stderr)
        return exc.exit_code
    pools, router, decision = ctx.pools, ctx.router, ctx.decision
    chosen, host, shape, tier = (plan[k] for k in ("chosen", "host", "shape", "tier"))
    print(json.dumps(plan, indent=2, sort_keys=True), flush=True)
    if plan["swarm"]["suggested"]:
        hint = plan["swarm"]
        print(f"swarm: this splits into ~{hint['units']} independent units (cap {hint['cap']}). "
              "Write a manifest and run: auto swarm <manifest.json>")

    if args.plan_only:
        print("dispatch: plan only (nothing launched)")
        return 0

    if chosen == host:
        print("dispatch: stay (this task belongs here)")
        return 0

    template, task_on_stdin = ctx.template, ctx.task_on_stdin
    if getattr(args, "allow_network", False):
        if chosen == "codex":
            template = with_network(template)
            print("dispatch: network allowed for this codex run (--allow-network)")
        else:
            print(f"dispatch: --allow-network only applies to codex; {chosen} runs as configured")
    if not auto:
        rendered = (f"{template}  # task on stdin" if task_on_stdin
                    else template.replace("{task}", shlex.quote(text)))
        try:
            answer = input(f"dispatch to {chosen} via `{rendered}`? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("not dispatched")
            return 0

    router.observe(shape, tier, chosen, [a for a, p in pools.items() if p.accepts(shape)])
    Stats().annotate(decision.decision_id, impression_recorded=True)

    usage_before = fetch_usage() if not args.no_quota else {}
    started = time.monotonic()
    rc = _dispatch(template, text, target=chosen, stdin=task_on_stdin)
    wall_clock_s = round(time.monotonic() - started, 1)
    quota = _quota_cost(chosen, usage_before, fetch_usage() if usage_before else {})
    Stats().annotate(decision.decision_id, wall_clock_s=wall_clock_s, quota=quota)
    # Other route calls may have learned while this subprocess was running.
    # Reopen counts so its completion cannot overwrite their newer evidence.
    _auto_outcome(_build_router(), shape, tier, chosen, rc, decision_id=decision.decision_id)
    if quota:
        moved = ", ".join(
            f"{label} {quota['before'][label]:g}% -> {quota['after'][label]:g}% ({delta:+g})"
            for label, delta in quota["delta"].items()
        )
        print(f"quota: {quota['provider']} {moved}")
    elif not args.no_quota:
        print(f"quota: not tracked for {chosen}" + ("" if chosen in QUOTA_PROVIDER else " (no quota pool)"))
    return rc


def _quota_cost(arm: str, before: dict, after: dict) -> dict | None:
    """Before/after usage for the arm's quota pool; None when not measurable."""
    provider = QUOTA_PROVIDER.get(arm)
    if not provider or provider not in before or provider not in after:
        return None
    b, a = before[provider], after[provider]
    delta = {label: round(a[label] - b[label], 2) for label in b if label in a}
    if not delta:
        return None
    return {"provider": provider, "before": b, "after": a, "delta": delta}


def _auto_outcome(router: Router, shape: str, tier: str, arm: str, rc: int, *,
                  decision_id: str | None = None) -> None:
    """Close the loop from the exit code; the caller only adds what it knows.

    ``completed`` (ran without erroring) is exactly what an exit code says,
    so record it here rather than hoping the caller remembers. ``accepted``
    (diff kept) and ``verified`` (tests passed) are still the caller's to
    report via ``route outcome`` — that call skips anything already on the
    decision, so nothing double-counts.
    """
    decision = Stats().decision(decision_id) if decision_id else {}
    already = set(decision.get("outcome_events", []))
    if rc == 0:
        if "completed" not in already:
            router.reward(shape, tier, arm, "completed")
        Stats().record_outcome_events(["completed"], decision_id=decision_id)
        print(
            "outcome: completed (recorded from exit code). When the diff is kept run\n"
            f"  route outcome --shape {shape} --tier {tier} --arm {arm} --outcome accepted"
        )
    else:
        # The impression is already counted with no reward — that IS the
        # negative outcome. Mark the decision so stats can show it.
        Stats().record_outcome_events(["failed"], decision_id=decision_id)
        if decision_id and decision.get("final_score") is None and "accepted" not in already:
            TaskMemory().settle(decision_id, 0.0)
        print(
            f"outcome: failed (exit {rc}, recorded). Retry once on the next-stronger arm:\n"
            f"  route outcome --shape {shape} --tier {tier} --arm {arm} --outcome failed"
        )
    if decision_id:
        print(f"settle: route settle --decision {decision_id} --fix-passes N [--merged|--abandoned]")


# Codex's workspace-write sandbox has no network, so a job that opens even a local test
# socket fails there. This turns it on for one run, never in ~/.codex/config.toml.
CODEX_NETWORK = "-c sandbox_workspace_write.network_access=true"


def with_network(template: str) -> str:
    """The codex dispatch template with network access for this run only."""
    if "codex exec" not in template or CODEX_NETWORK in template:
        return template
    return template.replace("codex exec", f"codex exec {CODEX_NETWORK}", 1)


def _dispatch(template: str | list[str], task: str, *, target: str | None = None,
              stdin: bool = False, cwd: str | Path | None = None,
              env: dict[str, str] | None = None, log: str | Path | None = None,
              timeout: float | None = None, on_start: Callable[[int], None] | None = None) -> int:
    if not template:
        print("dispatch: no command configured for this pool", file=sys.stderr)
        return 1
    tokens = shlex.split(template) if isinstance(template, str) else list(template)
    if stdin:
        # The task travels on stdin, never argv (deepseek batch: `ask -`).
        argv = tokens
    else:
        argv = [task if tok == "{task}" else tok for tok in tokens]
        if "{task}" not in tokens:
            argv.append(task)
    started = time.monotonic()
    try:
        # A delegated agent must see itself as host if it invokes route again.
        child_env = dict(os.environ)
        child_env.update(env or {})
        if target in ("claude", "codex"):
            child_env["ROUTE_HOST"] = target
        kwargs = {"input": task, "text": True} if stdin else {}
        if cwd is not None:
            kwargs["cwd"] = cwd
        if timeout is not None:
            kwargs["timeout"] = timeout
        with ExitStack() as stack:
            if log is not None:
                kwargs["stdout"] = stack.enter_context(Path(log).open("w", encoding="utf-8"))
                kwargs["stderr"] = subprocess.STDOUT
            if on_start is None:
                proc = subprocess.run(argv, env=child_env, **kwargs)
            else:
                input_text = kwargs.pop("input", None)
                wait_timeout = kwargs.pop("timeout", None)
                if stdin:
                    kwargs["stdin"] = subprocess.PIPE
                with subprocess.Popen(argv, env=child_env, **kwargs) as proc:
                    try:
                        on_start(proc.pid)
                        proc.communicate(input=input_text, timeout=wait_timeout)
                    except BaseException:
                        proc.kill()
                        proc.communicate()
                        raise
    except subprocess.TimeoutExpired:
        print(f"dispatch: timed out after {timeout}s", file=sys.stderr)
        return 124
    except OSError as exc:
        print(f"dispatch failed: {exc}", file=sys.stderr)
        return 1
    if log is None:
        print(f"dispatch: exit {proc.returncode} in {time.monotonic() - started:.1f}s")
    return proc.returncode


def cmd_stats(args: argparse.Namespace) -> int:
    router = _build_router()
    if args.throughput:
        rows = report_throughput(Stats())
    elif args.quota:
        rows = report_quota(Stats())
    elif args.arm:
        rows = report_arm(router, args.arm)
    else:
        rows = report_cells(router)
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


def cmd_outcome(args: argparse.Namespace) -> int:
    router = _build_router()
    stats = Stats()
    decision = _outcome_decision(args, stats)
    decision_id = decision.get("decision_id") if decision else None
    if decision and decision.get("swarm"):
        raise ValueError(f"decision {decision_id} was settled as part of swarm {decision['swarm']}")
    shape, tier, arm = args.shape, args.tier, args.arm
    already = set(decision.get("outcome_events", [])) if decision else set()

    if args.score is not None or args.fix_passes is not None or args.abandoned:
        score = (args.score if args.score is not None else
                 score_from_fix_passes(args.fix_passes or 0, abandoned=args.abandoned))
        previous = decision.get("final_score") if decision else None
        if previous is None:
            previous = 1.0 if "accepted" in already else 0.0
        record_score(router, shape, tier, arm, score, previous_score=previous,
                     impression_recorded=_has_trial(decision))
        events = ["graded"]
        if args.abandoned:
            events.append("failed")
        if getattr(args, "merged", False):
            # Merge is acceptance metadata; its reward is already in score.
            for event in OUTCOME_EVENTS["accepted"]:
                if event not in already and event != "accepted":
                    router.reward(shape, tier, arm, event)
            events.extend(OUTCOME_EVENTS["accepted"])
        if decision:
            stats.annotate(decision_id, events=events, final_score=score, impression_recorded=True,
                           fix_passes=args.fix_passes, abandoned=args.abandoned,
                           merged=getattr(args, "merged", False) or decision.get("merged", False))
            if decision_id:
                TaskMemory().settle(decision_id, score)
        print(json.dumps({"recorded": "graded", "decision_id": decision_id,
                          "arm": arm, "cell": cell_name(shape, tier), "score": score}))
        return 0

    events = list(OUTCOME_EVENTS[args.outcome])
    new_events = [e for e in events if e not in already]
    if decision and not _has_trial(decision):
        router.observe(shape, tier, arm, [arm])
    for event in new_events:
        # Explicit graded feedback remains authoritative even if a caller
        # later sends a legacy accepted event for bookkeeping.
        if event != "accepted" or not decision or decision.get("final_score") is None:
            router.reward(shape, tier, arm, event)
    if args.outcome == "failed" and decision is None:
        record_failure(router, shape, tier, arm)
    if decision:
        stats.annotate(decision_id, events=events or ["failed"], impression_recorded=True)
        if decision_id and decision.get("final_score") is None:
            if "accepted" in already or args.outcome == "accepted":
                TaskMemory().settle(decision_id, 1.0)
            elif args.outcome == "failed":
                TaskMemory().settle(decision_id, 0.0)
    print(json.dumps({"recorded": args.outcome, "arm": arm,
                      "cell": cell_name(shape, tier), "decision_id": decision_id,
                      "events": new_events, "already": sorted(already - {"failed"})}))
    return 0


def _has_trial(decision: dict | None) -> bool:
    if decision is None:
        return False
    if "impression_recorded" in decision:
        return decision["impression_recorded"]
    # Pre-ID logs counted every bandit selection, plus dispatched user pins.
    return not decision.get("why", "").startswith("veto:") or bool(decision.get("outcome_events"))


def _outcome_decision(args: argparse.Namespace, stats: Stats) -> dict | None:
    if args.decision:
        decision = stats.decision(args.decision)
        for name, field in (("shape", "shape"), ("tier", "tier"), ("arm", "chosen")):
            value = getattr(args, name)
            if value is not None and value != decision[field]:
                raise ValueError(f"--{name} does not match decision {args.decision}")
            setattr(args, name, decision[field])
        return decision
    if not all((args.shape, args.tier, args.arm)):
        raise ValueError("outcome needs --decision or all of --shape, --tier and --arm")
    if args.arm not in load_pools():
        raise ValueError(f"unknown arm: {args.arm}")
    decision = next((row for row in reversed(stats.decisions())
                     if row.get("cell") == cell_name(args.shape, args.tier) and row.get("chosen") == args.arm), None)
    return stats.identify(decision) if decision is not None else None


def cmd_reseed(args: argparse.Namespace) -> int:
    """Move every live cell's seed to the current rule table; keep observations."""
    router = _build_router()
    pools = load_pools()
    arms_for_shape = {
        shape: [name for name, pool in pools.items() if pool.accepts(shape)]
        for shape in SHAPES
    }
    changes = router.reseed(arms_for_shape, apply=not args.dry_run)
    print(json.dumps({
        "applied": not args.dry_run,
        "changed": len(changes),
        "changes": changes,
    }, indent=2))
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

    swarm = sub.add_parser("swarm", help="dispatch independently checked units across model pools")
    swarm.add_argument("manifest", help="JSON manifest path, or - for stdin")
    swarm.add_argument("--host", choices=["claude", "codex"],
                       default=os.environ.get("ROUTE_HOST", "claude"))
    swarm.add_argument("--plan-only", action="store_true")
    swarm.add_argument("--pool", help="pin units that do not name their own pool")
    swarm.add_argument("--no-amail", action="store_true")
    swarm.add_argument("--ttl", type=_ttl_arg, default=120, metavar="MIN")
    swarm.add_argument("--max-parallel", type=_positive_int, default=4, metavar="N")
    swarm.add_argument("--unit-timeout", type=_positive_int, default=3600, metavar="S")
    swarm.add_argument("--check-timeout", type=_positive_int, default=600, metavar="S")
    swarm.add_argument("--allow-network", action="store_true", help="codex only; refused with sensitive units")
    swarm.add_argument("--sensitive", action="store_true")
    swarm.add_argument("--no-quota", action="store_true")
    swarm_log = sub.add_parser("swarm-log", help="read a swarm's hub channel log")
    swarm_log.add_argument("swarm_id")
    swarms = sub.add_parser("swarms", help="list recent swarm status")
    swarms.add_argument("--json", action="store_true", help="print raw status records")
    swarms.add_argument("--all", action="store_true", help="include swarms older than 24 hours")

    s = sub.add_parser("stats")
    s.add_argument("--arm")
    s.add_argument("--throughput", action="store_true")
    s.add_argument("--quota", action="store_true", help="per arm: quota cost per dispatch and per accepted result")
    s.set_defaults(func=cmd_stats)

    o = sub.add_parser("outcome", help="record what happened")
    o.add_argument("--decision", help="stable decision id from the plan")
    o.add_argument("--shape", choices=SHAPES)
    o.add_argument("--tier", choices=TIERS)
    o.add_argument("--arm")
    feedback = o.add_mutually_exclusive_group()
    feedback.add_argument("--outcome", choices=list(OUTCOME_EVENTS))
    feedback.add_argument("--score", type=_score_arg, help="execution-verified quality in [0, 1]")
    feedback.add_argument("--fix-passes", type=_fix_passes_arg)
    o.add_argument("--abandoned", "--rewritten", action="store_true", help="abandoned or rewritten: score 0")
    o.set_defaults(func=cmd_outcome)

    settle = sub.add_parser("settle", help="settle a decision with execution-grounded feedback")
    settle.add_argument("--decision", required=True)
    settle.add_argument("--fix-passes", type=_fix_passes_arg)
    state = settle.add_mutually_exclusive_group()
    state.add_argument("--merged", action="store_true", help="record that the diff was accepted")
    state.add_argument("--abandoned", "--rewritten", action="store_true", help="abandoned or rewritten: score 0")
    settle.set_defaults(func=cmd_outcome, shape=None, tier=None, arm=None, score=None, outcome=None)

    r = sub.add_parser("reseed", help="move live cells' priors to the current rule table, keeping observations")
    r.add_argument("--dry-run", action="store_true", help="show what would change without writing")
    r.set_defaults(func=cmd_reseed)

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
    p.add_argument("--shape", choices=SHAPES, help="override shape classification")
    p.add_argument("--tier", choices=TIERS, help="override complexity tier")
    p.add_argument("--private", action="store_true", help="veto: data must not leave the machine")
    p.add_argument("--sensitive", action="store_true",
                   help="dataClass sensitive: veto remote pools (codex/kimi/free/deepseek), prefer local")
    p.add_argument("--allow-network", action="store_true",
                   help="codex only, this run only: let the sandbox use the network, including local "
                        "test sockets (workspace-write has it off). Refused with --sensitive")
    p.add_argument("--no-acceptance", action="store_true", help="veto: no clear acceptance check")
    p.add_argument("--needs-context", action="store_true", help="veto: needs this conversation")
    p.add_argument("--cross-repo", action="store_true", help="veto: cross-repo orchestration")
    llm = p.add_mutually_exclusive_group()
    llm.add_argument("--llm-shape", action="store_true", help="resolve low-confidence shapes with the local LLM")
    llm.add_argument("--no-llm", action="store_true", help="skip the local-LLM shape tiebreak (default)")
    p.add_argument("--no-quota", action="store_true", help="skip quotamax")
    swarm = p.add_mutually_exclusive_group()
    swarm.add_argument("--swarm", dest="swarm", action="store_true", default=None,
                       help="suggest a swarm, even when quota limits concurrency")
    swarm.add_argument("--no-swarm", dest="swarm", action="store_false",
                       help="suppress swarm suggestions")


def _score_arg(text: str) -> float:
    try:
        return validate_score(float(text))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _fix_passes_arg(text: str) -> int:
    try:
        value = int(text)
        score_from_fix_passes(value)
        return value
    except ValueError as exc:
        raise argparse.ArgumentTypeError("fix passes must be a non-negative integer") from exc


def _positive_int(text: str) -> int:
    try:
        value = int(text)
        if value > 0:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a positive integer")


def _ttl_arg(text: str) -> int:
    value = _positive_int(text)
    if value > 10080:
        raise argparse.ArgumentTypeError("ttl must be between 1 and 10080 minutes")
    return value


_SUBCOMMANDS = {"task", "stats", "outcome", "settle", "reseed", "federate", "benchmark", "ship",
                "swarm", "swarm-log", "swarms"}


def _run(argv: list[str] | None, *, auto: bool) -> int:
    argv = list(argv or [])
    # Bare task text is the default command; route it to the task parser.
    if argv and argv[0] not in _SUBCOMMANDS:
        argv = ["task", *argv]
    elif not argv:
        argv = ["task"]
    parser = _parser("auto" if auto else "route")
    args = parser.parse_args(argv)
    if args.command in ("outcome", "settle"):
        if args.outcome is None and args.score is None and args.fix_passes is None and not args.abandoned:
            parser.error("give --outcome, --score, --fix-passes or --abandoned")
        if args.abandoned and (args.outcome is not None or args.score is not None):
            parser.error("--abandoned cannot be combined with --outcome or --score")
    try:
        if args.command == "swarm":
            from .swarm_dispatch import run_swarm
            return run_swarm(args, auto=auto)
        if args.command == "swarm-log":
            from .swarm_dispatch import swarm_log
            return swarm_log(args.swarm_id)
        if args.command == "swarms":
            from .swarm_status import list_swarms
            return list_swarms(include_all=args.all, json_output=args.json)
        if getattr(args, "func", None):
            return args.func(args)
        return decide(args, auto=auto)
    except ValueError as exc:
        print(f"route: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    return _run(argv if argv is not None else sys.argv[1:], auto=False)


def main_auto(argv: list[str] | None = None) -> int:
    return _run(argv if argv is not None else sys.argv[1:], auto=True)


if __name__ == "__main__":
    raise SystemExit(main())
