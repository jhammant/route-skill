"""route ship — from routed task to reviewable PR (SPEC-ship section 2).

A routed coding task ends as edits on a branch, and then stops. ``ship``
closes the gap: show what changed, push the branch, open a PR whose body
carries the routing provenance (shape, tier, arm, why it was chosen,
verification result) — the record of *why this model wrote this code*, which
is otherwise lost when the session ends.

Hard safety rules, all enforced before anything leaves the machine:

- never push or open a PR without explicit per-invocation confirmation
- never merge — ``ship`` opens PRs; a human merges them
- never force-push, and never push the default branch
- refuse a tree dirty outside the task's branch rather than sweeping
  unrelated changes in
- if ``gh`` is absent, push and print the compare URL instead of failing
- targeting a repo without push access forks first

Everything runs through injected ``reader``/``runner`` callables so tests can
fake git and gh — no test ever pushes, calls gh, or touches a network.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Callable

#: Branches that are never a push target, whatever the remote says.
DEFAULT_BRANCHES: tuple[str, ...] = ("main", "master")

#: A runner takes argv, returns a RunResult. reader = read-only git/gh
#: probes (safe without confirmation); runner = anything that pushes,
#: forks, or opens a PR (only ever invoked AFTER explicit confirmation).
Runner = Callable[[list[str]], "RunResult"]


class ShipAbort(Exception):
    """A safety rule stopped the ship. The message is shown to the user."""


@dataclass
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class ShipResult:
    pushed: bool
    pr_opened: bool
    forked: bool
    commands: list[list[str]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def build_pr_body(
    *,
    shape: str,
    tier: str,
    arm: str,
    why: str,
    verification: str,
    task: str = "",
) -> str:
    """The PR body: routing provenance as a table, then the task description."""
    rows = [
        "Routed by /route.",
        "",
        "| | |",
        "|---|---|",
        f"| shape | {shape} |",
        f"| complexity | {tier} |",
        f"| arm | {arm} |",
        f"| why | {why} |",
        f"| verification | {verification} |",
    ]
    if task:
        rows += ["", task]
    return "\n".join(rows) + "\n"


def parse_repo_slug(remote_url: str) -> str | None:
    """'git@github.com:owner/repo.git' or 'https://github.com/owner/repo' -> 'owner/repo'."""
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?\s*$", remote_url.strip())
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def compare_url(repo: str, base: str, branch: str) -> str:
    return f"https://github.com/{repo}/compare/{base}...{branch}"


def _read(reader: Runner, argv: list[str]) -> RunResult:
    """A read-only probe; a missing binary or failure yields empty output."""
    try:
        return reader(list(argv))
    except OSError as exc:
        return RunResult(127, "", str(exc))


def ship(
    *,
    reader: Runner,
    runner: Runner,
    confirm: Callable[[str], bool],
    display: Callable[[str], None] | None = None,
    repo: str | None = None,
    base: str | None = None,
    draft: bool = False,
    open_pr: bool = True,
    title: str = "",
    body: str = "",
    show_diff: bool = False,
) -> ShipResult:
    """Show, confirm, push, open a PR. Raises ShipAbort on any safety stop.

    ``confirm`` is REQUIRED and is consulted exactly once, after the user has
    seen the diff and before ``runner`` is ever invoked: without an explicit
    yes on this invocation, nothing is pushed.
    """
    display = display or (lambda _msg: None)

    branch = _read(reader, ["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if not branch or branch == "HEAD":
        raise ShipAbort("not on a branch (detached HEAD) — nothing to ship")

    origin_url = _read(reader, ["git", "remote", "get-url", "origin"]).stdout.strip()
    origin_slug = parse_repo_slug(origin_url)
    target_repo = repo or origin_slug

    if base is None:
        ref = _read(reader, ["git", "symbolic-ref", "refs/remotes/origin/HEAD"]).stdout.strip()
        base = ref.rsplit("/", 1)[-1] if ref else "main"

    if branch in DEFAULT_BRANCHES or branch == base:
        raise ShipAbort(
            f"refusing to push '{branch}' — ship never pushes the default "
            "branch; do the work on a task branch first"
        )

    dirty = _read(reader, ["git", "status", "--porcelain"]).stdout.strip()
    if dirty:
        raise ShipAbort(
            "working tree has uncommitted changes outside the task's branch — "
            "commit or stash them first, ship will not sweep them in:\n" + dirty
        )

    if target_repo is None:
        raise ShipAbort(
            "could not determine owner/name from the origin remote; pass --repo"
        )

    # 1. Show what changed first. Nothing is pushed before the user sees it,
    #    and the diff is never redacted.
    stat = _read(reader, ["git", "diff", "--stat", f"{base}...HEAD"]).stdout.rstrip()
    display(f"branch '{branch}' -> {target_repo} (base {base})\n{stat}")
    if show_diff:
        display(_read(reader, ["git", "diff", f"{base}...HEAD"]).stdout)

    fork = bool(repo) and repo != origin_slug
    gh_available = _read(reader, ["gh", "--version"]).returncode == 0

    # 2. Explicit, per-invocation confirmation. The prompt names the target
    #    repo because opening a PR — especially on someone else's project —
    #    is a public act.
    where = f"a fork of {target_repo}" if fork else target_repo
    prompt = (
        f"Push branch '{branch}' to {where} and open a "
        f"{'draft ' if draft else ''}PR targeting {target_repo} (base {base})?"
    )
    if not confirm(prompt):
        return ShipResult(False, False, False, [], ["not shipped — nothing was pushed"])

    # 3. Push the branch (never --force, never the default branch), forking
    #    first when the target repo is not ours to push to.
    commands: list[list[str]] = []
    push_remote = "origin"
    if fork:
        commands.append(["gh", "repo", "fork", target_repo, "--remote", "--remote-name", "fork"])
        push_remote = "fork"
    commands.append(["git", "push", "-u", push_remote, branch])

    # 4. Open the PR via gh — or degrade to the compare URL when gh is absent.
    pr_opened = False
    if open_pr and gh_available:
        pr = [
            "gh", "pr", "create",
            "--repo", target_repo,
            "--base", base,
            "--title", title or branch,
            "--body", body,
        ]
        if fork:
            pr += ["--head", branch]
        if draft:
            pr.append("--draft")
        commands.append(pr)
        pr_opened = True

    messages: list[str] = []
    for argv in commands:
        messages.append("+ " + " ".join(shlex.quote(a) for a in argv))
        result = runner(list(argv))
        if result.returncode != 0:
            raise ShipAbort(
                f"command failed: {' '.join(argv)}\n{result.stderr.strip()}"
            )

    if pr_opened:
        messages.append(
            f"PR opened on {target_repo} (base {base}) — a human merges it, ship never does"
        )
    else:
        note = " (gh not found)" if open_pr and not gh_available else ""
        messages.append(f"pushed; open a PR here: {compare_url(target_repo, base, branch)}{note}")
    return ShipResult(True, pr_opened, fork, commands, messages)
