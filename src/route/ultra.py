"""``route ultra``: plan a goal, build it as a swarm on the best models with quota,
review each unit from another model family, fix what the repros confirm,
integrate on the run's own branch and report.

Everything the run does to the repository happens in worktrees under the state
dir and on branches named for the run. The user's checked-out branch, the base
branch and every remote are never written: merging ``ultra/<run-id>`` is the one
step left to a person, and summary.md spells out the command. Nothing here ever
asks for approval.
"""

from __future__ import annotations

import contextlib
import io
from collections import Counter
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import cli
from .digest import digest_preamble, write_digest
from .pools import Pool, pool_family, pools_for_host
from .storage import atomic_write, state_dir
from .swarm_dispatch import is_artefact, run_swarm_manifest, signal_tree, stage_argv, swarm_dir
from .swarm_status import utc_now
from .ultra_plan import Plan, PlanError, lint_plan, merge_overlaps, parse_plan, planner_prompt, to_manifest
from .ultra_review import (Finding, confirm, fix_prompt, merge_prompt, needs_review, parse_review, resolved,
                           review_prompt, unit_diff)
from .ultra_safety import run_checked, screen_digest
from .ultra_select import candidates, dispatch_template, pick, review_independence, pinned, set_pin

RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
SCHEMA = "route-ultra-progress/1"
MAX_FIX_PASSES = 2
#: Seconds. Module constants so a test (or an operator) can shorten them.
PLANNER_TIMEOUT = 1800
REVIEW_TIMEOUT = 1800
FIX_TIMEOUT = 3600
CHECK_TIMEOUT = 600
#: How long a cancel waits for SIGTERM to work before it sends SIGKILL.
CANCEL_GRACE = 5.0
FINISHED = frozenset({"done", "failed", "planned", "cancelled", "interrupted"})


def ultra_dir(run_id: str) -> Path:
    """Where one run's plan, manifest, progress, logs, worktrees and summary live."""
    return state_dir() / "ultra" / run_id


def new_run_id(goal: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:24].rstrip("-") or "ultra"
    return f"{slug}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, errors="replace")


class Run:
    """One run's directory and progress.json, written before every step acts.

    A watcher (``route ultra status``) and a canceller only ever see this file,
    so it carries every PID the run started and each unit's current state.
    """

    def __init__(self, run_id: str, goal: str, repo: Path, base: str, host: str):
        self.id = run_id
        self.dir = ultra_dir(run_id)
        self.progress: dict = {
            "schema": SCHEMA, "run_id": run_id, "goal": goal, "repo": str(repo), "base": base,
            "base_sha": None, "host": host, "pid": os.getpid(), "state": "starting", "step": "",
            "started_at": utc_now(), "updated_at": None, "finished_at": None, "exit_code": None,
            "context": {}, "planner": None, "plan_problems": [], "pids": [], "units": {}, "held": [],
            "integration": {}, "notes": [],
        }

    def save(self) -> None:
        self.progress["updated_at"] = utc_now()
        atomic_write(self.dir / "progress.json", json.dumps(self.progress, indent=2) + "\n")

    def step(self, state: str, step: str = "") -> None:
        self.progress.update(state=state, step=step)
        self.save()

    def note(self, text: str) -> None:
        self.progress["notes"].append(text)
        print(f"ultra: {text}", flush=True)
        self.save()

    def unit(self, unit_id: str, **fields) -> dict:
        row = self.progress["units"].setdefault(unit_id, {"id": unit_id, "state": "planned", "fix_passes": 0})
        row.update(fields)
        self.save()
        return row

    def track(self, pid: int) -> None:
        self.progress["pids"].append(pid)
        self.save()

    def untrack(self, pid: int) -> None:
        self.progress["pids"] = [p for p in self.progress["pids"] if p != pid]
        self.save()

    def finish(self, state: str, exit_code: int) -> None:
        self.progress.update(state=state, exit_code=exit_code, finished_at=utc_now(), pids=[])
        self.save()


def _resolve_base(repo: Path, base: str) -> str | None:
    if base.startswith("-"):
        return None
    probe = _git(repo, "rev-parse", "--is-inside-work-tree")
    if probe.returncode or probe.stdout.strip() != "true":
        return None
    sha = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
    return sha.stdout.strip() if sha.returncode == 0 else None


def _dispatch(run: Run, arm: str, pools: dict[str, Pool], host: str, prompt: str, cwd: Path,
              log_name: str, timeout: float) -> tuple[int, str]:
    """Run one model on a prompt in a directory, logging to the run dir; (exit code, output)."""
    log = run.dir / "logs" / log_name
    template = dispatch_template(arm, pools, host=host)
    pid_box: list[int] = []

    def started(pid: int) -> None:
        pid_box.append(pid)
        run.track(pid)

    try:
        rc = cli._dispatch(template, prompt, target=arm, cwd=cwd, log=log, timeout=timeout, on_start=started)
    finally:
        for pid in pid_box:
            run.untrack(pid)
    try:
        return rc, log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rc, ""


@contextlib.contextmanager
def _throwaway(repo: Path, path: Path, rev: str):
    """A detached worktree that is removed afterwards, whatever happened inside."""
    added = _git(repo, "worktree", "add", "--detach", str(path), rev)
    if added.returncode:
        raise RuntimeError(f"could not create worktree at {path}: {added.stderr.strip()}")
    try:
        yield path
    finally:
        _git(repo, "worktree", "remove", "--force", str(path))


class CommitError(RuntimeError):
    """A result that could not be kept: wrong branch, or git refused the commit."""


def _on_branch(worktree: Path, branch: str, base_sha: str | None = None) -> str | None:
    """Why the worktree is not on ``branch`` (descending from the base), or None.

    A linked worktree shares the repository's refs, so a model told to work in
    it can still check out main or the base branch; every commit and merge the
    orchestrator makes is preceded by this check so nothing lands anywhere but
    on a branch the run owns.
    """
    head = _git(worktree, "symbolic-ref", "-q", "HEAD")
    current = head.stdout.strip() if head.returncode == 0 else ""
    if current != f"refs/heads/{branch}":
        return f"worktree is on {current or 'a detached HEAD'}, not refs/heads/{branch}"
    if base_sha and _git(worktree, "merge-base", "--is-ancestor", base_sha, "HEAD").returncode:
        return f"{branch} no longer descends from the base {base_sha[:12]}"
    return None


def _commit_if_changed(worktree: Path, message: str, *, branch: str | None = None,
                       base_sha: str | None = None) -> bool:
    """Commit whatever changed; True when a commit was made, False when the tree was clean.

    Raises CommitError when the worktree is not on ``branch`` or git refused:
    a fix that exists only in a working tree is not a result, and the caller
    must not report one.
    """
    if branch is not None:
        reason = _on_branch(worktree, branch, base_sha)
        if reason:
            raise CommitError(reason)
    status = _git(worktree, "status", "--porcelain")
    if status.returncode:
        raise CommitError(f"git status failed: {status.stderr.strip()}")
    if not status.stdout.strip():
        return False
    added = _git(worktree, *stage_argv())
    if added.returncode:
        raise CommitError(f"git add failed: {added.stderr.strip()}")
    commit = _git(worktree, "commit", "-q", "-m", message)
    if commit.returncode:
        raise CommitError(f"git commit failed: {(commit.stderr or commit.stdout).strip()}")
    return True


# -- scope ---------------------------------------------------------------------

def _changed_paths(worktree: Path, base_sha: str) -> list[str]:
    """Repo-relative paths the unit's branch changed since the base (committed work only)."""
    diff = _git(worktree, "diff", "--name-only", base_sha, "HEAD")
    if diff.returncode:
        return []
    return [line.strip() for line in diff.stdout.splitlines() if line.strip()]


def out_of_scope(changed: list[str], touches: list[str], allowed: list[str] = ()) -> list[str]:
    """Changed paths the plan did not give the unit: a touch that is a directory covers everything
    under it, and paths ultra itself added (reviewer regression tests) are never counted."""
    prefixes = tuple(f"{touch.strip('/')}/" for touch in touches if touch.strip("/"))
    exact = {touch.strip("/") for touch in touches}
    return [path for path in changed
            if path not in exact and not path.startswith(prefixes) and path not in set(allowed)]


def _record_scope(run: Run, unit: dict, row: dict, base_sha: str) -> list[str]:
    """Record what a unit changed outside its planned touches; informative, never a failure."""
    review = run.progress["units"].get(unit["id"], {}).get("review") or {}
    strayed = out_of_scope(_changed_paths(Path(row["worktree"]), base_sha), unit.get("touches", []),
                           list(review.get("carried_tests", [])))
    run.unit(unit["id"], out_of_scope=strayed)
    return strayed


# -- context -------------------------------------------------------------------

def _context(run: Run, context: str, session: str | None) -> str:
    """The note every prompt carries about the conversation; brief carries nothing.

    A digest that looks like it holds credentials or private data is deleted and
    the run degrades to brief: what the workers never see cannot leak.
    """
    record = {"requested": context, "level": "brief", "note": ""}
    run.progress["context"] = record
    if context == "brief":
        run.save()
        return ""
    if not session:
        record["note"] = "no session id (set CLAUDE_CODE_SESSION_ID or pass --session)"
        run.save()
        return ""
    try:
        path = write_digest(session)
    except OSError as exc:
        record["note"] = f"digest unavailable: {exc}"
        run.save()
        return ""
    reason = screen_digest(path.read_text(encoding="utf-8", errors="replace"))
    if reason:
        path.unlink(missing_ok=True)
        record["note"] = f"digest refused ({reason}); workers get the goal only"
        run.note(f"context digest -> brief: {reason}")
        return ""
    record.update(level="digest", note=f"digest at {path}", path=str(path))
    run.save()
    return digest_preamble(path)


# -- plan ----------------------------------------------------------------------

def _plan_once(run: Run, planner: str, pools: dict[str, Pool], host: str, prompt: str,
               repo: Path, base_sha: str, max_units: int, attempt: int) -> tuple[Plan | None, list[str]]:
    with _throwaway(repo, run.dir / f"plan-{attempt}", base_sha) as worktree:
        rc, text = _dispatch(run, planner, pools, host, prompt, worktree, f"plan-{attempt}.log", PLANNER_TIMEOUT)
    if rc == 124:
        return None, ["planner timed out"]
    try:
        plan = merge_overlaps(parse_plan(text))
    except PlanError as exc:
        return None, [str(exc)]
    problems = lint_plan(plan, max_units=max_units)
    return (plan, []) if not problems else (None, problems)


def _plan(run: Run, planner: str, pools: dict[str, Pool], host: str, goal: str, repo: Path, base: str,
          base_sha: str, max_units: int, context_note: str) -> tuple[Plan | None, list[str]]:
    """Plan, and on problems re-plan once with them appended; the planner sees its own mistakes."""
    prompt = planner_prompt(goal, repo=repo, base=base, max_units=max_units, context_note=context_note)
    run.step("planning", f"planner {planner} (attempt 1)")
    plan, problems = _plan_once(run, planner, pools, host, prompt, repo, base_sha, max_units, 1)
    if plan is not None:
        return plan, []
    run.progress["plan_problems"] = list(problems)
    run.note("re-planning once: " + "; ".join(problems))
    run.step("planning", f"planner {planner} (attempt 2)")
    retry = prompt + ("\n\n# Problems with your previous plan (fix all of them)\n\n"
                      + "\n".join(f"- {problem}" for problem in problems) + "\n")
    plan, problems = _plan_once(run, planner, pools, host, retry, repo, base_sha, max_units, 2)
    if plan is None:
        run.progress["plan_problems"].extend(problems)
    return plan, problems


def _print_plan(plan: Plan, arms: dict[str, str], held: list[dict], planner: str, json_out: bool) -> None:
    if json_out:
        print(json.dumps({"plan": plan.to_dict(), "arms": arms, "held": held, "planner": planner}, indent=2))
        return
    print(f"plan: {plan.name} ({len(plan.units)} units; goal check: {plan.goal_check})")
    print("unit\ttier\tarm\tcheck\ttouches")
    for unit in plan.units:
        print(f"{unit.id}\t{unit.tier or '-'}\t{arms.get(unit.id, 'held')}\t{unit.check}\t{', '.join(unit.touches)}")
    for row in held:
        print(f"held: {row['id']}: {row['reason']}")


# -- review and fix ------------------------------------------------------------

def _read_regular_file(path: Path) -> bytes | None:
    """The bytes of a regular file reached without following a symlink, else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _write_new_file(path: Path, data: bytes) -> bool:
    """Create ``path`` with ``data``; False when anything is already there (link or file)."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except OSError:
        return False
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return True


def _carry_review_tests(review_wt: Path, unit_wt: Path, unit_id: str, *, branch: str | None = None) -> list[str]:
    """New test files the reviewer wrote travel to the unit's branch, so repros can run there.

    Only untracked regular files under test directories qualify, reached and
    written without following symlinks and only inside the two worktrees: a
    reviewer cannot edit the unit's code or its existing tests through this
    door, and a link it plants (to a credential file, say) is not copied.
    """
    status = _git(review_wt, "status", "--porcelain", "--untracked-files=all")
    review_root, unit_root = review_wt.resolve(), unit_wt.resolve()
    carried = []
    for line in status.stdout.splitlines():
        if not line.startswith("?? "):
            continue
        rel = line[3:].strip().strip('"')
        if not rel.startswith(("tests/", "test/")) or ".." in Path(rel).parts:
            continue
        source, target = review_wt / rel, unit_wt / rel
        if source.is_symlink() or not source.is_file() or source.resolve() != review_root / rel:
            continue
        if target.is_symlink() or target.exists() or target.resolve() != unit_root / rel:
            continue
        data = _read_regular_file(source)
        if data is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if _write_new_file(target, data):
            carried.append(rel)
    if carried:
        try:
            _commit_if_changed(unit_wt, f"ultra({unit_id}): review tests", branch=branch)
        except CommitError:
            # Tests that are not on the branch would not reach the merge; leave the tree as it was.
            _git(unit_wt, "reset", "-q", "--", *carried)
            for rel in carried:
                (unit_wt / rel).unlink(missing_ok=True)
            return []
    return carried


def _another_reviewer(tried: list[str], author: str, pools: dict[str, Pool], quota: dict, host: str,
                      allow_lower: bool) -> str | None:
    """A second reviewer after the first failed to deliver: cross-family if there is one."""
    others = candidates(pools, quota, host=host, allow_lower=allow_lower, exclude_arms=set(tried) | {author})
    if not others:
        return None
    family = pool_family(pools[author]) if author in pools else ""
    foreign = [arm for arm in others if pool_family(pools[arm]) != family]
    return (foreign or others)[0]


def _review_unit(run: Run, unit: dict, row: dict, pools: dict[str, Pool], quota: dict, host: str,
                 allow_lower: bool, repo: Path, base_sha: str) -> list[Finding] | None:
    """Review one accepted unit; the findings its repros confirmed, or None when no reviewer delivered.

    None is a failed review, recorded as such on the unit: a crashed, timed-out
    or incoherent reviewer has not cleared anything. One other reviewer gets a
    try first, when there is one.
    """
    unit_id, worktree = unit["id"], Path(row["worktree"])
    diff = unit_diff(worktree, base_sha)
    if not needs_review(unit, diff):
        run.unit(unit_id, review={"skipped": "trivial change"})
        return []
    reviewer = pick("reviewer", pools, quota, host=host, author_arm=row["arm"], allow_lower=allow_lower)
    if reviewer is None:
        run.unit(unit_id, review={"skipped": "no reviewer with quota"})
        return []
    tried: list[str] = []
    failures: list[str] = []
    while reviewer is not None:
        tried.append(reviewer)
        independence = review_independence(row["arm"], reviewer, pools)
        run.step("reviewing", f"{unit_id} by {reviewer} ({independence})")
        try:
            with _throwaway(repo, run.dir / "review" / unit_id, row["branch"]) as review_wt:
                rc, text = _dispatch(run, reviewer, pools, host, review_prompt(unit, diff), review_wt,
                                     f"review-{unit_id}-{len(tried)}.log", REVIEW_TIMEOUT)
                carried = _carry_review_tests(review_wt, worktree, unit_id, branch=row.get("branch"))
            findings = parse_review(rc, text)
        except (RuntimeError, ValueError) as exc:
            failures.append(f"{reviewer}: {exc}")
            run.note(f"review of {unit_id} by {reviewer} failed: {exc}")
            reviewer = (_another_reviewer(tried, row["arm"], pools, quota, host, allow_lower)
                        if len(tried) < 2 else None)
            continue
        confirmed, advisory = confirm(findings, worktree, timeout=CHECK_TIMEOUT)
        run.unit(unit_id, review={
            "reviewer": reviewer, "independence": independence, "agent_rc": rc, "findings": len(findings),
            "confirmed": [finding.to_dict() for finding in confirmed],
            "advisory": [finding.to_dict() for finding in advisory], "carried_tests": carried,
            "attempts": list(tried), "note": "; ".join(failures)})
        return confirmed
    reason = "review failed: " + "; ".join(failures)
    run.unit(unit_id, state="review failed", reason=reason, review={"attempts": tried, "failed": failures})
    return None


def _pick_fixer(pass_no: int, previous: list[str], author: str, pools: dict[str, Pool], quota: dict,
                host: str, allow_lower: bool) -> str | None:
    """The author fixes first; the second pass goes to a different model.

    An author that could not fix its own work gets a sibling from its family
    (same strengths, fresh eyes); a stranger that failed gives way to the best arm left.
    """
    if pass_no == 1 or not previous:
        return pick("fixer", pools, quota, host=host, author_arm=author, allow_lower=allow_lower)
    first = previous[-1]
    others = pinned(candidates(pools, quota, host=host, allow_lower=allow_lower, exclude_arms={first}), "fixer")
    if not others:
        return first
    if first == author:
        family = pool_family(pools[author])
        same = [arm for arm in others if pool_family(pools[arm]) == family]
        return same[0] if same else others[0]
    return others[0]


def _check_output(log: str) -> str:
    """The acceptance-check tail the swarm appended to the unit's log."""
    try:
        text = Path(log).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    marker = text.rfind("\nAcceptance check:")
    return text[marker + 1:] if marker >= 0 else text[-6000:]


def _fix_unit(run: Run, unit: dict, row: dict, confirmed: list[Finding], pools: dict[str, Pool],
              quota: dict, host: str, allow_lower: bool, base_sha: str = "") -> bool:
    """Up to MAX_FIX_PASSES rounds of fix, re-check and re-confirm; True when the unit is sound.

    Every finding that was ever confirmed is re-run after every pass, not only
    the ones the fixer was told about: a second fix can undo the first, and a
    repro that times out or is refused has not passed.
    """
    unit_id, worktree, author = unit["id"], Path(row["worktree"]), row["arm"]
    check_output = None if row["accepted"] else _check_output(row["log"])
    outstanding = list(confirmed)
    fixers: list[str] = []
    reason = ""
    for pass_no in range(1, MAX_FIX_PASSES + 1):
        fixer = _pick_fixer(pass_no, fixers, author, pools, quota, host, allow_lower)
        if fixer is None:
            reason = "no fixer with quota"
            break
        fixers.append(fixer)
        run.unit(unit_id, state="fixing", fix_passes=pass_no, fixers=list(fixers))
        run.step("fixing", f"{unit_id} pass {pass_no} by {fixer}")
        prompt = fix_prompt(unit, check_output=check_output, confirmed=outstanding, pass_no=pass_no)
        _dispatch(run, fixer, pools, host, prompt, worktree, f"fix-{unit_id}-{pass_no}.log", FIX_TIMEOUT)
        result = run_checked(unit["check"], worktree, timeout=CHECK_TIMEOUT)
        check_output = None if result.rc == 0 else result.output
        _, outstanding = resolved(confirmed, worktree, timeout=CHECK_TIMEOUT)
        try:
            _commit_if_changed(worktree, f"ultra({unit_id}): fix pass {pass_no}", branch=row.get("branch"))
        except CommitError as exc:
            reason = f"fix pass {pass_no} could not be kept: {exc}"
            break
        if base_sha:
            _record_scope(run, unit, row, base_sha)
        if result.rc == 0 and not outstanding:
            run.unit(unit_id, state="fixed", reason=None, outstanding=[])
            return True
        reason = (f"check exit {result.rc}" if result.rc else "") + (
            ("; " if result.rc else "") + f"{len(outstanding)} confirmed finding(s) remain" if outstanding else "")
    run.unit(unit_id, state="failed", reason=reason or "unresolved after fix passes",
             outstanding=[finding.to_dict() for finding in outstanding])
    return False


# -- integrate and settle ------------------------------------------------------

def _integrate(run: Run, plan: Plan, rows: dict[str, dict], sound: set[str], repo: Path, base_sha: str,
               pools: dict[str, Pool], quota: dict, host: str, allow_lower: bool, goal: str) -> dict:
    """Merge every sound unit onto ultra/<run-id> in plan order, then run the goal check there.

    Before every merge and commit the worktree must still be on that branch,
    descending from the base; if anything moved it (a model that checked out
    main, say) integration stops rather than touch whatever is checked out.
    """
    branch, path = f"ultra/{run.id}", run.dir / "integration"
    result = {"branch": branch, "worktree": str(path), "merged": [], "goal_check": plan.goal_check,
              "goal_check_passed": False, "goal_check_rc": None, "goal_check_output": "", "fix": None}
    run.progress["integration"] = result
    run.step("integrating", f"creating {branch}")
    added = _git(repo, "worktree", "add", "-b", branch, str(path), base_sha)
    if added.returncode:
        result["error"] = f"could not create {branch}: {added.stderr.strip()}"
        run.save()
        return result
    units = {unit.id: unit.to_dict() for unit in plan.units}
    for unit in plan.units:
        if unit.id not in sound:
            continue
        reason = _on_branch(path, branch, base_sha)
        if reason:
            result["error"] = f"integration stopped: {reason}"
            run.unit(unit.id, state="unmerged", reason=reason)
            continue
        problem = _merge_unit(run, units[unit.id], rows[unit.id]["branch"], path, branch, base_sha,
                              pools, quota, host, allow_lower, goal)
        if problem:
            run.unit(unit.id, state="conflict", reason=problem[:240])
            continue
        result["merged"].append(unit.id)
        run.unit(unit.id, state="merged")
    result["unmerged"] = [unit.id for unit in plan.units if unit.id not in result["merged"]]
    if result.get("error"):
        run.save()
        return result
    run.step("integrating", "goal check")
    check = run_checked(plan.goal_check, path, timeout=CHECK_TIMEOUT)
    if check.rc != 0:
        integrator = pick("integrator", pools, quota, host=host, allow_lower=allow_lower)
        if integrator is not None:
            run.step("integrating", f"goal check failed (exit {check.rc}); fix pass by {integrator}")
            unit = {"id": "integration", "task": f"{goal}\n\n(The units are merged on branch {branch}; "
                    "make the whole goal pass its check.)", "check": plan.goal_check}
            rc, _ = _dispatch(run, integrator, pools, host,
                              fix_prompt(unit, check_output=check.output, confirmed=[], pass_no=1),
                              path, "integration-fix.log", FIX_TIMEOUT)
            result["fix"] = {"arm": integrator, "agent_rc": rc}
            try:
                _commit_if_changed(path, f"ultra({run.id}): integration fix", branch=branch, base_sha=base_sha)
            except CommitError as exc:
                # The goal check must speak for the branch, so an unkept fix is not re-checked.
                result["error"] = f"integration fix not kept: {exc}"
            else:
                check = run_checked(plan.goal_check, path, timeout=CHECK_TIMEOUT)
    result.update(goal_check_passed=check.rc == 0, goal_check_rc=check.rc, goal_check_output=check.output[-4000:])
    run.save()
    return result


def _conflicted(path: Path) -> list[str]:
    """Paths the in-progress merge left unresolved."""
    listing = _git(path, "diff", "--name-only", "--diff-filter=U")
    return [line.strip() for line in listing.stdout.splitlines() if line.strip()]


_MARKER = re.compile(rb"^(<{7}|={7}|>{7})( |$)", re.M)


def _has_markers(path: Path, rels: list[str]) -> list[str]:
    """Files among ``rels`` that still carry conflict markers (a deleted file is resolved)."""
    hit = []
    for rel in rels:
        data = _read_regular_file(path / rel)
        if data is not None and _MARKER.search(data):
            hit.append(rel)
    return hit


def _restore(path: Path, branch: str, before: str) -> None:
    """Put the integration worktree back where it was before a merge was attempted.

    The reset only happens while the worktree is still on the run's branch: a
    model that checked out main in it must not have main reset underneath it,
    and ``_integrate`` stops at the next branch check anyway.
    """
    _git(path, "merge", "--abort")
    if _git(path, "rev-parse", "HEAD").stdout.strip() != before and _on_branch(path, branch) is None:
        _git(path, "reset", "-q", "--hard", before)


def _merge_unit(run: Run, unit: dict, unit_branch: str, path: Path, branch: str, base_sha: str,
                pools: dict[str, Pool], quota: dict, host: str, allow_lower: bool, goal: str) -> str | None:
    """Merge one unit branch into the integration worktree; the reason when it could not be.

    Artefact-only conflicts (a worker that committed its ``__pycache__``) are
    settled by dropping those paths: no model wrote them. A real conflict gets
    the integrator ONE pass in the live merge; anything short of a clean result
    with the unit's branch as an ancestor is undone, so the branch never carries
    half a merge.
    """
    unit_id = unit["id"]
    before = _git(path, "rev-parse", "HEAD").stdout.strip()
    merge = _git(path, "merge", "--no-ff", "-m", f"ultra({run.id}): merge {unit_id}", unit_branch)
    if merge.returncode == 0:
        return None
    conflicted = _conflicted(path)
    if not conflicted:
        _restore(path, branch, before)
        detail = (merge.stdout + merge.stderr).strip().splitlines()
        return "merge failed: " + (detail[-1] if detail else "")
    artefacts = [rel for rel in conflicted if is_artefact(rel)]
    if artefacts:
        # Neither side wrote these by hand; the merge goes on without them.
        _git(path, "rm", "-q", "--cached", "--", *artefacts)
        for rel in artefacts:
            (path / rel).unlink(missing_ok=True)
        run.note(f"{unit_id}: {len(artefacts)} artefact conflict(s) dropped: " + ", ".join(artefacts[:5]))
    remaining = [rel for rel in conflicted if rel not in artefacts]
    resolver = None
    if remaining:
        resolver = pick("integrator", pools, quota, host=host, allow_lower=allow_lower)
        if resolver is None:
            _restore(path, branch, before)
            return "merge conflict: " + ", ".join(remaining)[:160] + " (no integrator with quota)"
        run.unit(unit_id, state="resolving", resolver=resolver)
        run.step("integrating", f"{unit_id} conflicts on {len(remaining)} path(s); one pass by {resolver}")
        _dispatch(run, resolver, pools, host,
                  merge_prompt(unit, branch=branch, conflicted=remaining, goal=goal),
                  path, f"merge-{unit_id}.log", FIX_TIMEOUT)
        markers = _has_markers(path, remaining)
        if markers:
            _restore(path, branch, before)
            return f"merge conflict: {resolver} left conflict markers in " + ", ".join(markers)[:160]
    reason = _on_branch(path, branch, base_sha)
    if reason is None:
        if _git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
            # Still mid-merge: stage what was resolved (artefacts excluded) and commit it.
            _git(path, *stage_argv())
            if _git(path, "ls-files", "-u").stdout.strip():
                reason = "paths left unresolved: " + ", ".join(_conflicted(path))[:160]
            else:
                who = f" ({resolver} resolved {', '.join(remaining)[:120]})" if resolver else ""
                commit = _git(path, "commit", "-q", "-m", f"ultra({run.id}): merge {unit_id}{who}")
                if commit.returncode:
                    reason = f"git commit failed: {(commit.stderr or commit.stdout).strip()[:160]}"
        if reason is None and _git(path, "merge-base", "--is-ancestor", unit_branch, "HEAD").returncode:
            reason = f"{unit_branch} is not in the result"
    if reason is not None:
        _restore(path, branch, before)
        return f"merge conflict: {reason}"
    return None


def _settle(run: Run, rows: dict[str, dict]) -> None:
    """Grade each unit's decision for the router: merged with N fix passes, or abandoned.

    The swarm recorded the units sharing an arm and cell as one trial, so each
    unit's grade carries 1/n of that trial's weight; otherwise abandoning two
    units would take back two rewards where one was given.
    """
    stats = cli.Stats()
    cells: dict[str, tuple] = {}
    for unit_id, row in rows.items():
        try:
            decision = stats.decision(row["decision_id"])
        except ValueError:
            decision = {}
        cells[unit_id] = (decision.get("shape", row.get("shape")), decision.get("tier", row.get("tier")),
                          decision.get("chosen", row.get("arm")))
    shares = {cell: 1 / count for cell, count in Counter(cells.values()).items()}
    for unit_id, row in rows.items():
        state = run.progress["units"][unit_id]
        merged = state["state"] == "merged"
        argv = ["settle", "--decision", row["decision_id"], "--fix-passes", str(state.get("fix_passes", 0)),
                "--merged" if merged else "--abandoned", "--within-swarm", run.id,
                "--weight", repr(shares[cells[unit_id]])]
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                rc = cli.main(argv)
            except Exception as exc:  # settlement must never undo a finished build
                rc, state["settle_error"] = 1, str(exc)
        state["settled"] = rc == 0
    run.save()


# -- summary -------------------------------------------------------------------

def _summary(run: Run, goal: str, plan: Plan | None, arms: dict[str, str], integration: dict,
             usage_before: dict, usage_after: dict, exit_code: int) -> dict:
    units = []
    for unit_id, state in run.progress["units"].items():
        review = state.get("review") or {}
        units.append({
            "id": unit_id, "arm": state.get("arm"), "tier": state.get("tier"), "state": state["state"],
            "reviewer": review.get("reviewer"), "independence": review.get("independence"),
            "confirmed_findings": len(review.get("confirmed", [])),
            "advisory_findings": len(review.get("advisory", [])),
            "fix_passes": state.get("fix_passes", 0), "reason": state.get("reason"),
            "branch": state.get("branch"), "settled": state.get("settled"),
            "out_of_scope": list(state.get("out_of_scope") or []),
        })
    quota = {}
    for provider in sorted(set(usage_before) | set(usage_after)):
        before, after = usage_before.get(provider, {}), usage_after.get(provider, {})
        quota[provider] = {label: {"before": before.get(label), "after": after.get(label)}
                           for label in sorted(set(before) | set(after))}
    return {
        "run_id": run.id, "goal": goal, "repo": run.progress["repo"], "base": run.progress["base"],
        "base_sha": run.progress["base_sha"], "state": run.progress["state"], "exit_code": exit_code,
        "planner": run.progress.get("planner"), "context": run.progress.get("context"),
        "plan": plan.to_dict() if plan else None, "plan_problems": run.progress.get("plan_problems", []),
        "units": units, "held": run.progress.get("held", []), "integration": integration, "quota": quota,
        "merge_command": f"git merge --no-ff {integration['branch']}" if integration.get("branch") else None,
        "run_dir": str(run.dir), "notes": run.progress.get("notes", []),
    }


def goal_check_verdict(integration: dict, planned: int) -> str:
    """What the goal check proved, counting units: a pass on a partial merge is not a pass on the goal."""
    merged, unmerged = integration.get("merged") or [], integration.get("unmerged") or []
    if integration.get("goal_check_rc") is None:
        return "not run"
    verdict = ("passed" if integration.get("goal_check_passed")
               else f"FAILED (exit {integration.get('goal_check_rc')})")
    verdict += f" on {len(merged)} of {planned} units"
    if unmerged:
        verdict += f" ({len(unmerged)} not merged: {', '.join(unmerged)})"
    return verdict


def _summary_md(summary: dict) -> str:
    integration = summary["integration"] or {}
    planned = len((summary.get("plan") or {}).get("units") or summary["units"])
    lines = [f"# ultra {summary['run_id']}", "", f"Goal: {summary['goal']}", "",
             f"Repo: {summary['repo']} at {summary['base']} ({(summary['base_sha'] or '')[:12]})"]
    if summary.get("planner"):
        lines.append(f"Planner: {summary['planner']}")
    context = summary.get("context") or {}
    if context.get("requested") and context["requested"] != "brief":
        lines.append(f"Context: {context['requested']} -> {context['level']}: {context['note']}")
    if integration.get("branch"):
        passed = integration.get("goal_check_passed")
        unmerged = integration.get("unmerged") or []
        lines += ["", f"Integration branch: {integration['branch']}", f"Worktree: {integration['worktree']}",
                  f"Goal check `{integration['goal_check']}`: " + goal_check_verdict(integration, planned)
                  + (f"; integration fix pass by {integration['fix']['arm']}" if integration.get("fix") else "")]
        if integration.get("error"):
            lines.append(f"Integration error: {integration['error']}")
        lines.append("Result: " + (
            f"complete: all {planned} units merged and the goal check passed" if passed and not unmerged else
            f"INCOMPLETE: {len(unmerged)} of {planned} units not merged; the goal check only covers the rest"
            if unmerged else "the goal check failed on the integration branch"))
    if summary["plan_problems"]:
        lines += ["", "Plan problems: " + "; ".join(summary["plan_problems"])]
    if summary["units"]:
        lines += ["", "| unit | arm | review | confirmed | fix passes | out of scope | state |",
                  "|---|---|---|---|---|---|---|"]
        for unit in summary["units"]:
            review = (f"{unit['reviewer']} ({unit['independence']})" if unit.get("reviewer") else "-")
            state = unit["state"] + (f": {unit['reason']}" if unit.get("reason") else "")
            strayed = unit.get("out_of_scope") or []
            scope = ", ".join(strayed[:4]) + (f" (+{len(strayed) - 4} more)" if len(strayed) > 4 else "") or "-"
            lines.append(f"| {unit['id']} | {unit['arm'] or '-'} | {review} | {unit['confirmed_findings']} "
                         f"| {unit['fix_passes']} | {scope} | {state} |")
    for row in summary["held"]:
        lines.append(f"| {row['id']} | held | - | - | - | - | {row['reason']} |")
    if summary["quota"]:
        lines += ["", "Quota (percent used, before -> after):"]
        for provider, labels in summary["quota"].items():
            moved = ", ".join(f"{label} {v['before']} -> {v['after']}" for label, v in labels.items())
            lines.append(f"- {provider}: {moved}")
    if summary.get("merge_command"):
        lines += ["", "To take the result onto your branch (ultra never does this itself):", "",
                  "```bash", summary["merge_command"], "```"]
    if summary["notes"]:
        lines += ["", "Notes:"] + [f"- {note}" for note in summary["notes"][:8]]
    lines += ["", f"Run directory: {summary['run_dir']}", f"Exit: {summary['exit_code']}"]
    return "\n".join(lines) + "\n"


def _report(run: Run, goal: str, plan: Plan | None, arms: dict[str, str], integration: dict,
            usage_before: dict, exit_code: int, json_out: bool) -> int:
    state = "done" if exit_code == 0 else "failed"
    run.finish(state, exit_code)
    usage_after = cli.fetch_usage()
    summary = _summary(run, goal, plan, arms, integration, usage_before, usage_after, exit_code)
    atomic_write(run.dir / "summary.json", json.dumps(summary, indent=2) + "\n")
    text = _summary_md(summary)
    atomic_write(run.dir / "summary.md", text)
    print(json.dumps(summary, indent=2) if json_out else text, flush=True)
    return exit_code


# -- the run -------------------------------------------------------------------

def run_ultra(goal: str, *, repo: str | Path = ".", base: str = "HEAD", context: str = "brief",
              session: str | None = None, max_units: int = 10, max_parallel: int = 6,
              plan_only: bool = False, allow_lower: bool = False, json_out: bool = False,
              host: str = "claude", pin: tuple[str, ...] | list[str] = ()) -> int:
    """One whole run; exit 0 when the goal check passed on the integration branch with
    every unit merged, 1 when anything failed, 2 when it could not plan or nothing was usable."""
    goal = goal.strip()
    if not goal:
        print("route ultra: empty goal", file=sys.stderr)
        return 2
    if context not in ("brief", "digest"):
        print(f"route ultra: --context must be brief or digest, not {context}", file=sys.stderr)
        return 2
    repo_path = Path(repo).expanduser().resolve()
    base_sha = _resolve_base(repo_path, base)
    if base_sha is None:
        print(f"route ultra: {repo_path} is not a git work tree with commit {base!r}", file=sys.stderr)
        return 2
    if pin:
        known = pools_for_host(cli.load_pools(), host)
        unknown = sorted(set(pin) - set(known))
        if unknown:
            print(f"route ultra: --pool names no such pool: {', '.join(unknown)} (pools: {', '.join(sorted(known))})",
                  file=sys.stderr)
            return 2
    set_pin(pin)
    run = Run(new_run_id(goal), goal, repo_path, base, host)
    run.progress["pin"] = sorted(set(pin))
    try:
        run.dir.mkdir(parents=True, mode=0o700)
        (run.dir / "logs").mkdir(mode=0o700)
    except FileExistsError:
        print(f"route ultra: run {run.id} already exists at {run.dir}", file=sys.stderr)
        return 2
    run.progress["base_sha"] = base_sha
    run.save()
    print(f"ultra: run {run.id} at {run.dir}", flush=True)
    try:
        return _execute(run, goal, repo_path, base, base_sha, context, session, max_units, max_parallel,
                        plan_only, allow_lower, json_out, host)
    except KeyboardInterrupt:
        run.finish("interrupted", 130)
        print("ultra: interrupted", file=sys.stderr)
        return 130


def _give_up(run: Run, reason: str, json_out: bool, usage_before: dict, plan: Plan | None = None) -> int:
    """Exit 2: nothing was built, and the summary says why."""
    run.note(reason)
    return _report(run, run.progress["goal"], plan, {}, {}, usage_before, 2, json_out)


def _execute(run: Run, goal: str, repo: Path, base: str, base_sha: str, context: str, session: str | None,
             max_units: int, max_parallel: int, plan_only: bool, allow_lower: bool, json_out: bool,
             host: str) -> int:
    pools = pools_for_host(cli.load_pools(), host)
    quota = cli.fetch_quota()
    usage_before = cli.fetch_usage()
    run.progress["headroom"] = dict(quota)
    context_note = _context(run, context, session)

    planner = pick("planner", pools, quota, host=host, allow_lower=allow_lower)
    if planner is None:
        return _give_up(run, "no usable model has quota to plan (try --allow-lower-models)", json_out, usage_before)
    run.progress["planner"] = planner
    plan, problems = _plan(run, planner, pools, host, goal, repo, base, base_sha, max_units, context_note)
    if plan is None:
        return _give_up(run, "could not plan: " + "; ".join(problems), json_out, usage_before)

    run.step("assigning", "choosing an arm per unit")
    arms: dict[str, str] = {}
    for unit in plan.units:
        arm = pick("worker", pools, quota, tier=unit.tier or "moderate", host=host, allow_lower=allow_lower)
        if arm is None:
            run.progress["held"].append({"id": unit.id, "reason": "no usable model has quota"})
            run.unit(unit.id, state="held", tier=unit.tier, reason="no usable model has quota")
        else:
            arms[unit.id] = arm
            run.unit(unit.id, arm=arm, tier=unit.tier, state="planned")
    atomic_write(run.dir / "plan.json", json.dumps(
        {"plan": plan.to_dict(), "arms": arms, "held": run.progress["held"], "planner": planner}, indent=2) + "\n")
    if plan_only:
        _print_plan(plan, arms, run.progress["held"], planner, json_out)
        run.finish("planned", 0)
        return 0
    if not arms:
        return _give_up(run, "no usable model has quota for any unit", json_out, usage_before, plan)

    manifest = to_manifest(plan, repo=repo, base=base_sha, swarm_id=run.id, arms=arms)
    if context_note:
        manifest["shared"] = f"{context_note}\n\n{manifest['shared']}".strip()
    atomic_write(run.dir / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    run.step("running", f"swarm {run.id}: {len(arms)} unit(s), up to {max_parallel} in parallel")
    try:
        swarm = run_swarm_manifest(manifest, host=host, max_parallel=max_parallel, check_timeout=CHECK_TIMEOUT)
    except RuntimeError as exc:
        return _give_up(run, f"swarm did not run: {exc}", json_out, usage_before, plan)
    run.progress["swarm_dir"] = str(swarm_dir(run.id))
    rows = {row["id"]: row for row in swarm["units"]}
    for row in swarm.get("held", []):
        run.progress["held"].append(row)
        run.unit(row["id"], state="held", reason=row["reason"])
    for unit_id, row in rows.items():
        run.unit(unit_id, state="accepted" if row["accepted"] else "failed", decision_id=row["decision_id"],
                 branch=row["branch"], worktree=row["worktree"], log=row["log"], agent_rc=row["agent_rc"],
                 reason=None if row["accepted"] else f"check failed (agent exit {row['agent_rc']})")

    units = {unit.id: unit.to_dict() for unit in plan.units}
    for unit_id, row in rows.items():
        _record_scope(run, units[unit_id], row, base_sha)
    reviews: dict[str, list[Finding] | None] = {}
    run.step("reviewing", "")
    for unit_id, row in rows.items():
        if row["accepted"]:
            reviews[unit_id] = _review_unit(run, units[unit_id], row, pools, quota, host, allow_lower, repo, base_sha)

    sound: set[str] = set()
    for unit_id, row in rows.items():
        if not row["accepted"]:
            if _fix_unit(run, units[unit_id], row, [], pools, quota, host, allow_lower, base_sha):
                sound.add(unit_id)
            continue
        confirmed = reviews[unit_id]
        if confirmed is None:
            continue  # review failed: nothing is known to fix, and nothing was cleared
        if not confirmed or _fix_unit(run, units[unit_id], row, confirmed, pools, quota, host, allow_lower,
                                      base_sha):
            sound.add(unit_id)

    integration = _integrate(run, plan, rows, sound, repo, base_sha, pools, quota, host, allow_lower, goal)
    run.step("settling", "")
    _settle(run, rows)
    all_merged = set(integration["merged"]) == {unit.id for unit in plan.units}
    exit_code = 0 if integration["goal_check_passed"] and all_merged else 1
    return _report(run, goal, plan, arms, integration, usage_before, exit_code, json_out)


# -- status and cancel ---------------------------------------------------------

def _load_progress(run_id: str | None) -> tuple[dict | None, str]:
    """The run's progress.json (latest run when no ID), or a reason there is none."""
    if run_id is not None:
        if not RUN_ID.fullmatch(run_id):
            return None, "invalid run ID"
        paths = [ultra_dir(run_id) / "progress.json"]
    else:
        paths = sorted((state_dir() / "ultra").glob("*/progress.json"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
    for path in paths:
        try:
            progress = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(progress, dict) and progress.get("schema") == SCHEMA:
            return progress, ""
    return None, (f"no ultra run {run_id}" if run_id else "no ultra runs yet")


def _zombie(pid: int) -> bool:
    """A process that has exited but not been reaped still answers signals; ps tells it apart."""
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                               timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return state.strip().startswith("Z")


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return not _zombie(pid)


def _stop_processes(pids: list) -> tuple[list[int], list[int]]:
    """SIGTERM every live process (with its group when it leads one), wait, then SIGKILL the rest.

    Returns (stopped, survivors). A delegate's shells and test runners share its
    group, so signalling the group is what makes a cancel reach them.
    """
    targets = []
    for pid in pids:
        if pid in targets or pid == os.getpid() or not _alive(pid):
            continue
        if signal_tree(pid, signal.SIGTERM):
            targets.append(pid)
    deadline = time.monotonic() + CANCEL_GRACE
    while any(_alive(pid) for pid in targets) and time.monotonic() < deadline:
        time.sleep(0.1)
    for pid in targets:
        # The sweep reaches group members that outlived a leader which already answered SIGTERM.
        signal_tree(pid, signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    while any(_alive(pid) for pid in targets) and time.monotonic() < deadline:
        time.sleep(0.05)
    return targets, [pid for pid in targets if _alive(pid)]


def ultra_status(run_id: str | None = None) -> int:
    progress, reason = _load_progress(run_id)
    if progress is None:
        print(f"route ultra status: {reason}", file=sys.stderr)
        return 1
    state = progress["state"]
    if state not in FINISHED and not _alive(progress.get("pid")):
        state = f"{state} (stopped?)"
    print(f"run {progress['run_id']}: {state}" + (f" — {progress['step']}" if progress.get("step") else ""))
    print(f"goal: {progress['goal']}")
    print(f"started {progress['started_at']}, updated {progress['updated_at']}")
    if progress["units"]:
        print("unit\tarm\tstate\tfix passes\treason")
        for row in progress["units"].values():
            print(f"{row['id']}\t{row.get('arm') or '-'}\t{row['state']}\t{row.get('fix_passes', 0)}"
                  f"\t{row.get('reason') or ''}")
    integration = progress.get("integration") or {}
    if integration.get("branch"):
        planned = len([row for row in progress["units"].values() if row["state"] != "held"])
        verdict = goal_check_verdict(integration, planned)
        print(f"integration: {integration['branch']}: goal check "
              + ("pending" if verdict == "not run" else verdict))
    return 0


def ultra_cancel(run_id: str) -> int:
    """Stop a run: its orchestrator first, then every model process it recorded, with their groups.

    The progress file is re-read before it is marked cancelled: a run that
    finished while its processes were being checked keeps its own verdict.
    """
    progress, reason = _load_progress(run_id)
    if progress is None:
        print(f"route ultra cancel: {reason}", file=sys.stderr)
        return 1
    if progress["state"] in FINISHED:
        print(f"run {run_id} already finished: {progress['state']}")
        return 0
    pids = [progress.get("pid")] + list(progress.get("pids", []))
    try:
        status = json.loads((swarm_dir(run_id) / "status.json").read_text(encoding="utf-8"))
        pids += [row.get("pid") for row in status.get("units", [])]
    except (OSError, ValueError, AttributeError):
        pass
    stopped, survivors = _stop_processes(pids)
    latest, _ = _load_progress(run_id)
    if latest is not None and latest["state"] in FINISHED:
        print(f"run {run_id} finished meanwhile: {latest['state']}; stopped {len(stopped)} process(es)")
        return 0
    progress = latest or progress
    progress.update(state="cancelled", exit_code=130, finished_at=utc_now(), updated_at=utc_now(), pids=[])
    atomic_write(ultra_dir(run_id) / "progress.json", json.dumps(progress, indent=2) + "\n")
    print(f"run {run_id} cancelled; stopped {len(stopped)} process(es)"
          + (f"; {len(survivors)} did not confirm exit: {survivors}" if survivors else ""))
    return 0
