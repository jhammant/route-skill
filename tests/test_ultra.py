"""``route ultra`` end to end, with shell scripts standing in for every model.

The fake arm reads its role off the prompt it is given: a planner prompt gets
the manifest from a file, a review prompt gets findings from a file, and worker
and fixer prompts carry ``ultra-fake: write PATH CONTENT`` instructions. Nothing
here reaches a network or a real model; quota is monkeypatched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from route import cli, swarm_dispatch, ultra
from route.storage import state_dir
from route.swarm_dispatch import swarm_dir
from route.ultra import run_ultra, ultra_cancel, ultra_dir, ultra_status

FAKE_ARM = r'''#!/bin/bash
# A model stand-in: the last argument is the prompt; the role is read off it.
FAKE_DIR="__FAKE_DIR__"
prompt="${@: -1}"
test "$ROUTE_WORKER" = "1" || { echo "ROUTE_WORKER not set" >&2; exit 97; }
role=worker
case "$prompt" in
  *ULTRA-MANIFEST-BEGIN*) role=planner ;;
  *ULTRA-FINDINGS-BEGIN*) role=reviewer ;;
  *"Fix pass "*) role=fixer ;;
esac
unit=$(printf '%s\n' "$prompt" | sed -n -e 's/^Unit id: //p' -e 's/^## Your unit: //p' | head -1)
echo "$(basename "$0") $role ${unit:-plan} $PWD" >> "$FAKE_DIR/calls.log"
if [ "$role" = planner ]; then
  n=$(cat "$FAKE_DIR/plans.count" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$FAKE_DIR/plans.count"
  plan="$FAKE_DIR/plan-$n.json"; [ -f "$plan" ] || plan="$FAKE_DIR/plan.json"
  echo "Thinking about the repo..."; echo "ULTRA-MANIFEST-BEGIN"; cat "$plan"; echo "ULTRA-MANIFEST-END"; exit 0
fi
if [ "$role" = reviewer ]; then
  findings="$FAKE_DIR/findings-$unit.json"
  echo "ULTRA-FINDINGS-BEGIN"; if [ -f "$findings" ]; then cat "$findings"; else echo "[]"; fi; echo "ULTRA-FINDINGS-END"; exit 0
fi
verb=write; [ "$role" = fixer ] && verb=fix
printf '%s\n' "$prompt" | grep -o "ultra-fake: $verb [^ ]* [^ ]*" | sort -u | while read -r _ _ path content; do
  mkdir -p "$(dirname "$path")"; printf '%s\n' "$content" > "$path"
done
exit 0
'''

CODING = '["coding:implement", "coding:refactor", "coding:test", "coding:debug", "coding:review"]'
QUOTA = {"claude": "critical", "claude-work": "ok", "codex": "ok"}


@pytest.fixture
def repo(tmp_path):
    """A repo whose checks are committed test scripts; units add the files they test."""
    directory = tmp_path / "repo"
    (directory / "tests").mkdir(parents=True)
    for args in (("init", "-b", "main"), ("config", "user.name", "Ultra Test"),
                 ("config", "user.email", "ultra@example.test"), ("config", "commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True)
    (directory / "README.md").write_text("An isolated test repo.\n")
    (directory / "shared.txt").write_text("original\n")
    scripts = {
        "check_alpha.sh": 'test -s alpha.txt',
        "check_beta.sh": 'test -s beta.txt',
        "check_goal.sh": 'test -s alpha.txt && test -s beta.txt',
        "repro_alpha.sh": 'grep -q fixed alpha.txt',
        "passes.sh": 'true',
        "fails_goal.sh": 'false',
    }
    for name, body in scripts.items():
        (directory / "tests" / name).write_text(f"#!/bin/sh\n{body}\n")
    subprocess.run(["git", "-C", str(directory), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(directory), "commit", "-q", "-m", "initial"], check=True, capture_output=True)
    return directory


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Two fake arms in pools.toml (one per family), patched quota, no a-mail, no real models."""
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    for name in ("fake-anthropic", "fake-openai"):
        script = fake_dir / name
        script.write_text(FAKE_ARM.replace("__FAKE_DIR__", str(fake_dir)))
        script.chmod(0o700)
    config = Path(os.environ["ROUTE_CONFIG_DIR"])
    config.mkdir(parents=True, exist_ok=True)
    (config / "pools.toml").write_text(textwrap.dedent(f"""
        [pools.claude-work]
        shapes = {CODING}
        remote = true
        dispatch = "{fake_dir}/fake-anthropic --print {{task}}"
        model = {{ name = "claude-opus-5-5" }}
        parallel_limit = 4
        quota_provider = "claude-work"

        [pools.codex]
        shapes = {CODING}
        remote = true
        needs_repo = true
        dispatch = "{fake_dir}/fake-openai exec {{task}}"
        model = {{ name = "gpt-6-astra" }}
        parallel_limit = 4
    """))
    monkeypatch.setattr(cli, "fetch_quota", lambda *a, **k: dict(QUOTA))
    monkeypatch.setattr(cli, "fetch_usage", lambda *a, **k: {"codex": {"5h": 10.0, "weekly": 20.0}})
    monkeypatch.setattr(swarm_dispatch, "amail_binary", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("ultra must never prompt"))
    return fake_dir


def unit(identity, *, touches, write=None, fix=None, tier="moderate", check=None):
    task = f"Create the {identity} component."
    if write:
        task += f" ultra-fake: write {write}"
    if fix:
        task += f" (if fixing) ultra-fake: fix {fix}"
    return {"id": identity, "task": task, "check": check or f"bash tests/check_{identity}.sh",
            "touches": touches, "tier": tier}


def plan(units, *, goal_check="bash tests/check_goal.sh", name="Two files"):
    return {"name": name, "shared": "Keep files small.", "goal_check": goal_check, "units": units}


def two_units():
    return [unit("alpha", touches=["alpha.txt"], write="alpha.txt alpha"),
            unit("beta", touches=["beta.txt"], write="beta.txt beta")]


def write_plan(fake_dir, payload, attempt=None):
    (fake_dir / ("plan.json" if attempt is None else f"plan-{attempt}.json")).write_text(json.dumps(payload))


def calls(fake_dir):
    return [line.split(" ", 3) for line in (fake_dir / "calls.log").read_text().splitlines()]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def latest_run():
    [path] = list((state_dir() / "ultra").glob("*/summary.json"))
    return json.loads(path.read_text()), path.parent


def settle_recorder(monkeypatch):
    recorded = []
    original = cli.main

    def main(argv=None):
        if argv and argv[0] == "settle":
            recorded.append(list(argv))
        return original(argv)

    monkeypatch.setattr(ultra.cli, "main", main)
    return recorded


# -- the whole run -------------------------------------------------------------

def test_full_run_builds_reviews_fixes_integrates_and_settles(repo, fake, monkeypatch, capsys):
    # Arrange: two units; a cross-family review of alpha finds a real bug that the author fixes in one pass.
    write_plan(fake, plan(two_units()))
    (fake / "findings-alpha.json").write_text(json.dumps([
        {"title": "alpha is not fixed", "severity": "high", "detail": "ultra-fake: fix alpha.txt fixed",
         "repro": "bash tests/repro_alpha.sh"},
        {"title": "style nit", "severity": "low", "detail": "no repro", "repro": None},
        {"title": "already fine", "severity": "medium", "detail": "passes", "repro": "bash tests/passes.sh"},
    ]))
    settles = settle_recorder(monkeypatch)
    head_before = git(repo, "rev-parse", "HEAD").strip()
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude")
    # Assert: exit, summary, branch, base untouched, roles and families.
    summary, run_dir = latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    run_id = summary["run_id"]
    assert run_id.startswith("create-two-files-20") and run_dir == ultra_dir(run_id)
    assert summary["integration"]["goal_check_passed"] and summary["integration"]["merged"] == ["alpha", "beta"]
    assert summary["merge_command"] == f"git merge --no-ff ultra/{run_id}"
    assert git(repo, "rev-parse", "HEAD").strip() == head_before
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert git(repo, "status", "--porcelain") == ""
    branch_files = git(repo, "ls-tree", "--name-only", f"ultra/{run_id}").split()
    assert {"alpha.txt", "beta.txt"} <= set(branch_files)
    assert git(repo, "show", f"ultra/{run_id}:alpha.txt") == "fixed\n"
    by_id = {row["id"]: row for row in summary["units"]}
    assert by_id["alpha"] == {**by_id["alpha"], "arm": "claude-work", "reviewer": "codex",
                              "independence": "cross-family", "confirmed_findings": 1,
                              "advisory_findings": 2, "fix_passes": 1, "state": "merged", "settled": True}
    assert by_id["beta"]["state"] == "merged" and by_id["beta"]["fix_passes"] == 0
    roles = [(binary, role, who) for binary, role, who, _ in calls(fake)]
    assert ("fake-anthropic", "planner", "plan") in roles
    assert ("fake-openai", "reviewer", "alpha") in roles and ("fake-anthropic", "fixer", "alpha") in roles
    assert [c for c in calls(fake) if c[1] == "planner"][0][3] == str(run_dir / "plan-1")
    assert not (run_dir / "plan-1").exists() and not (run_dir / "review" / "alpha").exists()
    # Settled exactly once per unit, graded, within this swarm.
    assert sorted(a[a.index("--decision") + 1] for a in settles) == sorted(
        row["decision_id"] for row in json.loads((swarm_dir(run_id) / "summary.json").read_text())["units"])
    assert len(settles) == 2 and all("--merged" in a and "--within-swarm" in a for a in settles)
    text = (run_dir / "summary.md").read_text()
    assert text.count("\n") <= 60 and f"ultra/{run_id}" in text and "codex (cross-family)" in text
    assert "codex: 5h 10.0 -> 10.0" in text
    assert capsys.readouterr().out.rstrip().endswith("Exit: 0")
    for name in ("plan.json", "manifest.json", "progress.json", "summary.json"):
        assert (run_dir / name).is_file()
    assert json.loads((run_dir / "progress.json").read_text())["state"] == "done"


def test_fix_loop_stops_after_two_passes_and_escalates_the_fixer(repo, fake):
    # Arrange: a confirmed finding nobody can fix (no fix instruction anywhere).
    write_plan(fake, plan(two_units()))
    (fake / "findings-alpha.json").write_text(json.dumps([
        {"title": "alpha never fixed", "severity": "high", "detail": "unfixable", "repro": "bash tests/repro_alpha.sh"}]))
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude")
    # Assert: two passes, author then the other family, then the unit fails and is not merged.
    summary, run_dir = latest_run()
    assert rc == 1
    alpha = next(row for row in summary["units"] if row["id"] == "alpha")
    assert alpha["state"] == "failed" and alpha["fix_passes"] == 2 and "confirmed finding" in alpha["reason"]
    fixers = [binary for binary, role, who, _ in calls(fake) if role == "fixer" and who == "alpha"]
    assert fixers == ["fake-anthropic", "fake-openai"]
    assert (run_dir / "logs" / "fix-alpha-2.log").is_file() and not (run_dir / "logs" / "fix-alpha-3.log").exists()
    assert summary["integration"]["merged"] == ["beta"]
    progress = json.loads((run_dir / "progress.json").read_text())
    assert progress["units"]["alpha"]["fixers"] == ["claude-work", "codex"]


def test_failed_check_gets_fixed_from_the_task_brief(repo, fake):
    # Arrange: beta's worker writes nothing, so its check fails; its brief says what a fix writes.
    units = [unit("alpha", touches=["alpha.txt"], write="alpha.txt alpha"),
             unit("beta", touches=["beta.txt"], fix="beta.txt beta")]
    write_plan(fake, plan(units))
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude")
    # Assert
    summary, _ = latest_run()
    beta = next(row for row in summary["units"] if row["id"] == "beta")
    assert rc == 0 and beta["state"] == "merged" and beta["fix_passes"] == 1


def test_conflicting_units_are_marked_conflict(repo, fake):
    # Arrange: both units claim disjoint touches but edit shared.txt differently.
    units = [unit("alpha", touches=["alpha.txt"], write="shared.txt alpha", check="bash tests/passes.sh"),
             unit("beta", touches=["beta.txt"], write="shared.txt beta", check="bash tests/passes.sh")]
    write_plan(fake, plan(units, goal_check="bash tests/passes.sh"))
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude")
    # Assert: the first merges, the second conflicts and is abandoned; exit 1 despite a passing goal check.
    summary, _ = latest_run()
    by_id = {row["id"]: row for row in summary["units"]}
    assert by_id["alpha"]["state"] == "merged" and by_id["beta"]["state"] == "conflict"
    assert by_id["beta"]["reason"].startswith("merge conflict")
    assert summary["integration"]["goal_check_passed"] and rc == 1
    integration = Path(summary["integration"]["worktree"])
    assert git(integration, "status", "--porcelain") == ""
    assert (integration / "shared.txt").read_text() == "alpha\n"


def test_failing_goal_check_gets_one_integration_fix_pass_then_exit_1(repo, fake):
    # Arrange
    write_plan(fake, plan(two_units(), goal_check="bash tests/fails_goal.sh"))
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude")
    # Assert
    summary, run_dir = latest_run()
    assert rc == 1 and not summary["integration"]["goal_check_passed"]
    assert summary["integration"]["fix"]["arm"] == "claude-work"
    assert (run_dir / "logs" / "integration-fix.log").is_file()
    assert [who for _, role, who, _ in calls(fake) if role == "fixer"] == ["integration"]
    assert "FAILED (exit 1)" in (run_dir / "summary.md").read_text()
    assert all(row["state"] == "merged" for row in summary["units"])


def test_plan_only_writes_nothing_to_the_repo(repo, fake, capsys):
    # Arrange
    write_plan(fake, plan(two_units()))
    branches_before = git(repo, "branch", "--list")
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude", plan_only=True)
    # Assert: table printed, plan.json written, nothing dispatched, repo and branches untouched.
    out = capsys.readouterr().out
    assert rc == 0 and "alpha\tmoderate\tclaude-work\tbash tests/check_alpha.sh\talpha.txt" in out
    [run_dir] = list((state_dir() / "ultra").glob("*"))
    assert json.loads((run_dir / "plan.json").read_text())["arms"] == {"alpha": "claude-work", "beta": "claude-work"}
    assert not (run_dir / "manifest.json").exists() and not (state_dir() / "swarms").exists()
    assert [role for _, role, _, _ in calls(fake)] == ["planner"]
    assert git(repo, "branch", "--list") == branches_before and git(repo, "status", "--porcelain") == ""
    assert git(repo, "worktree", "list").count("\n") == 1
    assert json.loads((run_dir / "progress.json").read_text())["state"] == "planned"


def test_plan_only_json_prints_the_plan(repo, fake, capsys):
    write_plan(fake, plan(two_units()))
    rc = run_ultra("Create two files", repo=str(repo), host="claude", plan_only=True, json_out=True)
    payload = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert rc == 0 and [u["id"] for u in payload["plan"]["units"]] == ["alpha", "beta"]
    assert payload["planner"] == "claude-work"


def test_bad_plan_is_replanned_once_with_the_problems(repo, fake, capsys):
    # Arrange: the first plan has a trivial check and a bad id; the second is sound.
    bad = plan([{"id": "Alpha", "task": "x", "check": "echo ok", "touches": ["alpha.txt"]}])
    write_plan(fake, bad, attempt=1)
    write_plan(fake, plan(two_units()), attempt=2)
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude", plan_only=True)
    # Assert
    [run_dir] = list((state_dir() / "ultra").glob("*"))
    assert rc == 0 and [role for _, role, _, _ in calls(fake)] == ["planner", "planner"]
    second_prompt = (run_dir / "logs" / "plan-2.log").read_text()
    assert "Problems with your previous plan" not in second_prompt  # the log holds output, not the prompt
    progress = json.loads((run_dir / "progress.json").read_text())
    assert any("not an allowed check" in p for p in progress["plan_problems"])
    assert any("id must match" in p for p in progress["plan_problems"])
    assert "re-planning once" in capsys.readouterr().out


def test_two_bad_plans_exit_2_without_touching_the_repo(repo, fake):
    write_plan(fake, plan([{"id": "alpha", "task": "x", "check": "curl http://x", "touches": ["/etc"]}]))
    rc = run_ultra("Create two files", repo=str(repo), host="claude")
    summary, _ = latest_run()
    assert rc == 2 and summary["state"] == "failed" and summary["units"] == []
    assert any("could not plan" in note for note in summary["notes"])
    assert git(repo, "worktree", "list").count("\n") == 1 and not (state_dir() / "swarms").exists()


def test_nothing_usable_exits_2(repo, fake, monkeypatch):
    monkeypatch.setattr(cli, "fetch_quota", lambda *a, **k: {"claude": "critical", "claude-work": "critical",
                                                            "codex": "critical"})
    rc = run_ultra("Create two files", repo=str(repo), host="claude")
    summary, _ = latest_run()
    assert rc == 2 and "no usable model" in summary["notes"][0] and not (fake / "calls.log").exists()


@pytest.mark.parametrize("args,reason", [
    (dict(repo="nowhere"), "not a git work tree"),
    (dict(base="no-such-rev"), "not a git work tree"),
    (dict(context="fork"), "brief or digest"),
])
def test_bad_inputs_exit_2_before_any_run_dir(repo, fake, tmp_path, capsys, args, reason):
    repo_arg = str(repo) if "repo" not in args else str(tmp_path / args.pop("repo"))
    rc = run_ultra("Create two files", repo=repo_arg, host="claude", **args)
    assert rc == 2 and reason in capsys.readouterr().err
    assert not (state_dir() / "ultra").exists()


# -- context ------------------------------------------------------------------

def transcript(home: Path, session: str, text: str) -> None:
    project = home / "projects" / "p"
    project.mkdir(parents=True)
    (project / f"{session}.jsonl").write_text(json.dumps(
        {"type": "user", "message": {"role": "user", "content": text}}) + "\n")


def test_digest_that_looks_private_falls_back_to_brief(repo, fake, tmp_path, monkeypatch):
    # Arrange: the transcript carries a credential.
    session = "abcdef12-3456-7890-abcd-ef1234567890"
    home = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    transcript(home, session, "Use api_key=sk-live-1234567890abcdefXYZ for the deploy")
    write_plan(fake, plan(two_units()))
    # Act
    rc = run_ultra("Create two files", repo=str(repo), host="claude", context="digest", session=session,
                   plan_only=True)
    # Assert: brief, reason recorded, the digest file deleted, workers never told about it.
    [run_dir] = list((state_dir() / "ultra").glob("*"))
    context = json.loads((run_dir / "progress.json").read_text())["context"]
    assert rc == 0 and context["requested"] == "digest" and context["level"] == "brief"
    assert "refused" in context["note"]
    assert list((state_dir() / "digests").glob("*.md")) == []


def test_clean_digest_reaches_the_workers(repo, fake, tmp_path, monkeypatch):
    session = "abcdef12-3456-7890-abcd-ef1234567891"
    home = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    transcript(home, session, "We decided alpha goes first and beta second.")
    write_plan(fake, plan(two_units()))
    rc = run_ultra("Create two files", repo=str(repo), host="claude", context="digest", session=session)
    summary, run_dir = latest_run()
    assert rc == 0 and summary["context"]["level"] == "digest"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["shared"].startswith("Before you start, read the handoff digest at")
    [digest] = list((state_dir() / "digests").glob("*.md"))
    assert str(digest) in manifest["shared"]


# -- status and cancel --------------------------------------------------------

def test_status_shows_the_latest_run_and_a_named_one(repo, fake, capsys):
    write_plan(fake, plan(two_units()))
    run_ultra("Create two files", repo=str(repo), host="claude", plan_only=True)
    [run_dir] = list((state_dir() / "ultra").glob("*"))
    capsys.readouterr()
    assert ultra_status() == 0
    out = capsys.readouterr().out
    assert out.startswith(f"run {run_dir.name}: planned") and "alpha\tclaude-work\tplanned\t0" in out
    assert ultra_status(run_dir.name) == 0
    assert ultra_status("no-such-run") == 1 and ultra_status("Bad/ID") == 1


def test_status_without_runs_says_so(capsys):
    assert ultra_status() == 1
    assert "no ultra runs yet" in capsys.readouterr().err


def test_cancel_stops_recorded_processes_and_marks_the_run(tmp_path, capsys):
    # Arrange: a progress file for a "running" run whose only process is a sleeping child.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    run_id = "sleepy-20261105120000"
    directory = ultra_dir(run_id)
    directory.mkdir(parents=True)
    progress = {"schema": ultra.SCHEMA, "run_id": run_id, "goal": "g", "state": "running", "step": "fixing",
                "pid": os.getpid(), "pids": [child.pid], "units": {}, "started_at": "x", "updated_at": "x"}
    (directory / "progress.json").write_text(json.dumps(progress))
    try:
        # Act
        rc = ultra_cancel(run_id)
        # Assert: the child is gone, the run is cancelled, the caller itself was not signalled.
        assert rc == 0 and child.wait(timeout=10) != 0
        after = json.loads((directory / "progress.json").read_text())
        assert after["state"] == "cancelled" and after["pids"] == [] and after["exit_code"] == 130
        assert "stopped 1 process" in capsys.readouterr().out
        assert ultra_cancel(run_id) == 0 and "already finished" in capsys.readouterr().out
        assert ultra_cancel("missing-run") == 1
    finally:
        if child.poll() is None:
            child.kill()


# -- CLI ----------------------------------------------------------------------

def test_cli_ultra_subcommand_and_console_script(repo, fake, capsys):
    write_plan(fake, plan(two_units()))
    assert cli.main(["ultra", "Create two files", "--repo", str(repo), "--plan-only", "--max-units", "3"]) == 0
    assert cli.main_ultra(["status"]) == 0
    assert "planned" in capsys.readouterr().out
    assert cli.main_ultra(["cancel"]) == 2 and "usage: route ultra cancel RUN_ID" in capsys.readouterr().err
    assert cli.main_ultra([]) == 2 and "needs a goal" in capsys.readouterr().err
    assert cli.main_ultra(["status", "a", "b"]) == 2


def test_settle_outside_the_swarm_is_still_refused(repo, fake, monkeypatch):
    # Arrange: a finished run whose units the swarm settled.
    write_plan(fake, plan(two_units()))
    assert run_ultra("Create two files", repo=str(repo), host="claude") == 0
    summary, _ = latest_run()
    [decision_id] = {row["decision_id"] for row in json.loads(
        (swarm_dir(summary["run_id"]) / "summary.json").read_text())["units"] if row["id"] == "alpha"}
    # Act / Assert: a user cannot grade it again by hand; the orchestrator (naming its swarm) could.
    assert cli.main(["settle", "--decision", decision_id, "--fix-passes", "0", "--merged"]) == 2
    assert cli.main(["settle", "--decision", decision_id, "--fix-passes", "0", "--merged",
                     "--within-swarm", summary["run_id"]]) == 0
