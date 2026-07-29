"""SPEC-ship section 2: route ship — fakes only.

No test here runs `git push`, invokes `gh`, or touches a network. git/gh are
injected callables; the mutating `runner` fake records what it was asked to
do, and where the spec says nothing should be sent we assert it was never
called.
"""

from __future__ import annotations

import pytest

from route.ship import RunResult, ShipAbort, build_pr_body, compare_url, parse_repo_slug, ship

BRANCH = "task/rotate-creds"
ORIGIN = "git@github.com:me/proj.git"


def make_reader(*, branch=BRANCH, dirty="", origin=ORIGIN, gh=True, default="main"):
    """Read-only git/gh probes. Returns canned output; records nothing it
    doesn't need to — mutations are the `runner` fake's job."""

    def reader(argv):
        assert argv[0] in ("git", "gh")
        if argv[0] == "gh":
            return RunResult(0 if gh else 1, "", "")
        sub = argv[1:3]
        if sub == ["rev-parse", "--abbrev-ref"]:
            return RunResult(0, branch + "\n", "")
        if sub == ["status", "--porcelain"]:
            return RunResult(0, dirty, "")
        if sub == ["remote", "get-url"]:
            return RunResult(0, origin + "\n", "")
        if argv[1:2] == ["symbolic-ref"]:
            return RunResult(0, f"refs/remotes/origin/{default}\n", "")
        if argv[1:2] == ["diff"]:
            return RunResult(0, " src/route/x.py | 4 ++--\n", "")
        return RunResult(0, "", "")

    return reader


class RecordingRunner:
    """The mutating runner: push, fork, pr create. Records every call."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return RunResult(0, "", "")


def yes(_prompt: str) -> bool:
    return True


PROVENANCE = dict(
    shape="coding:implement",
    tier="moderate",
    arm="kimi",
    why="claude weekly at 88%; task self-contained with a clear acceptance check",
    verification="62 tests pass",
    task="rotate the API credentials",
)


def run_ship(reader, runner, confirm=yes, **kwargs):
    kwargs.setdefault("body", build_pr_body(**PROVENANCE))
    return ship(reader=reader, runner=runner, confirm=confirm, **kwargs)


# -- the spec's seven tests ----------------------------------------------------


def test_no_confirmation_pushes_nothing():
    """Ship without confirmation: the mutating runner was NEVER called."""
    runner = RecordingRunner()
    result = run_ship(make_reader(), runner, confirm=lambda _p: False)
    assert runner.calls == []
    assert result.pushed is False and result.pr_opened is False
    assert "nothing was pushed" in result.messages[-1]


def test_dirty_tree_outside_the_branch_aborts():
    runner = RecordingRunner()
    with pytest.raises(ShipAbort, match="uncommitted changes outside the task's branch"):
        run_ship(make_reader(dirty=" M unrelated.py\n"), runner)
    assert runner.calls == []  # aborted before anything was sent


def test_pr_body_contains_provenance():
    body = build_pr_body(**PROVENANCE)
    assert "coding:implement" in body          # shape
    assert "moderate" in body                  # tier / complexity
    assert "kimi" in body                      # arm
    assert "claude weekly at 88%" in body      # rationale
    assert "62 tests pass" in body             # verification
    assert "rotate the API credentials" in body
    assert "Routed by /route." in body


def test_force_is_never_in_any_constructed_command():
    """Plain flow and fork flow: no constructed git/gh command uses --force."""
    cases = [
        (make_reader(), None),                      # plain flow
        (make_reader(), "upstream/awesome-list"),   # fork flow
    ]
    for reader, repo in cases:
        runner = RecordingRunner()
        run_ship(reader, runner, repo=repo)
        for argv in runner.calls:
            assert not any(a.startswith("--force") for a in argv)


def test_default_branch_is_never_a_push_target():
    for branch in ("main", "master"):
        runner = RecordingRunner()
        with pytest.raises(ShipAbort, match="never pushes the default branch"):
            run_ship(make_reader(branch=branch), runner)
        assert runner.calls == []
    # A task branch that happens to equal the requested base is refused too.
    runner = RecordingRunner()
    with pytest.raises(ShipAbort, match="never pushes the default branch"):
        run_ship(make_reader(branch="release"), runner, base="release")
    assert runner.calls == []


def test_missing_gh_degrades_to_compare_url():
    runner = RecordingRunner()
    result = run_ship(make_reader(gh=False), runner)
    assert result.pushed and not result.pr_opened
    assert ["git", "push", "-u", "origin", BRANCH] in runner.calls
    assert not any(argv[:3] == ["gh", "pr", "create"] for argv in runner.calls)
    url = compare_url("me/proj", "main", BRANCH)
    assert any(f"pushed; open a PR here: {url}" in m for m in result.messages)


def test_repo_without_push_access_forks_first():
    runner = RecordingRunner()
    prompts: list[str] = []
    result = run_ship(
        make_reader(), runner,
        confirm=lambda p: prompts.append(p) or True,
        repo="upstream/awesome-list", base="main",
    )
    assert result.forked and result.pr_opened
    # Fork BEFORE push; push goes to the fork; the PR targets the upstream.
    assert runner.calls[0][:3] == ["gh", "repo", "fork"]
    assert "upstream/awesome-list" in runner.calls[0]
    push = next(a for a in runner.calls if a[:2] == ["git", "push"])
    assert push == ["git", "push", "-u", "fork", BRANCH]
    pr = next(a for a in runner.calls if a[:3] == ["gh", "pr", "create"])
    assert pr[pr.index("--repo") + 1] == "upstream/awesome-list"
    # The confirmation prompt names the repo the PR will target.
    assert prompts and "upstream/awesome-list" in prompts[0]


# -- supporting behaviour -------------------------------------------------------


def test_parse_repo_slug():
    assert parse_repo_slug("git@github.com:me/proj.git") == "me/proj"
    assert parse_repo_slug("https://github.com/me/proj") == "me/proj"
    assert parse_repo_slug("https://github.com/me/proj.git\n") == "me/proj"
    assert parse_repo_slug("") is None


def test_same_repo_target_does_not_fork():
    runner = RecordingRunner()
    result = run_ship(make_reader(), runner, repo="me/proj")
    assert not result.forked
    assert ["git", "push", "-u", "origin", BRANCH] in runner.calls
    assert not any(a[:3] == ["gh", "repo", "fork"] for a in runner.calls)


def test_draft_flag_reaches_the_pr_command():
    runner = RecordingRunner()
    run_ship(make_reader(), runner, draft=True)
    pr = next(a for a in runner.calls if a[:3] == ["gh", "pr", "create"])
    assert "--draft" in pr


def test_ship_never_merges():
    runner = RecordingRunner()
    run_ship(make_reader(), runner)
    assert not any("merge" in argv for argv in runner.calls)


def test_display_runs_before_confirmation():
    """The user sees what changed before being asked to confirm anything."""
    events: list[str] = []
    run_ship(
        make_reader(),
        RecordingRunner(),
        confirm=lambda _p: events.append("confirm") or False,
        display=lambda _msg: events.append("display"),
    )
    assert events == ["display", "confirm"]
