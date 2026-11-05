"""Reviews count only when their repros fail for real; everything else is advisory."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import pytest

from route import ultra_review
from route.ultra_review import (
    FINDINGS_BEGIN,
    FINDINGS_END,
    Finding,
    confirm,
    fix_prompt,
    needs_review,
    parse_findings,
    review_prompt,
    unit_diff,
)
from route.ultra_safety import RunResult


@pytest.fixture
def worktree(tmp_path):
    """A repo with one commit on top of its base: the unit's change."""
    directory = tmp_path / "wt"
    (directory / "tests").mkdir(parents=True)
    for args in (("init", "-b", "main"), ("config", "user.name", "Review Test"),
                 ("config", "user.email", "review@example.test"), ("config", "commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True)
    (directory / "tests" / "fails.sh").write_text("#!/bin/sh\ntest -f fixed.txt\n")
    (directory / "tests" / "passes.sh").write_text("#!/bin/sh\ntrue\n")
    (directory / "tests" / "test_fixed.py").write_text(
        "from pathlib import Path\n\ndef test_fixed():\n    assert Path('fixed.txt').is_file()\n")
    subprocess.run(["git", "-C", str(directory), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(directory), "commit", "-q", "-m", "base"], check=True, capture_output=True)
    base = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    (directory / "feature.py").write_text("".join(f"line {i}\n" for i in range(60)))
    subprocess.run(["git", "-C", str(directory), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(directory), "commit", "-q", "-m", "unit"], check=True, capture_output=True)
    return directory, base


def marked(payload):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return f"Review notes...\n{FINDINGS_BEGIN}\n{body}\n{FINDINGS_END}\n"


def finding(title="bug", repro="bash tests/fails.sh", severity="high"):
    return Finding(title=title, severity=severity, detail=f"{title} detail", repro=repro)


# -- unit_diff and needs_review -------------------------------------------------

def test_unit_diff_shows_the_change_since_base_and_truncates_with_a_note(worktree):
    directory, base = worktree
    diff = unit_diff(directory, base)
    assert "+++ b/feature.py" in diff and "+line 59" in diff
    short = unit_diff(directory, base, max_chars=100)
    assert len(short) < len(diff) and "[diff truncated:" in short and short.startswith(diff[:100])


def test_unit_diff_reports_git_failure_instead_of_raising(tmp_path):
    assert unit_diff(tmp_path, "HEAD").startswith("[diff unavailable:")


@pytest.mark.parametrize("tier,lines,expected", [
    ("moderate", 1, True), ("hard", 1, True), ("open-ended", 1, True),
    ("trivial", 50, False), ("simple", 50, False), (None, 50, False),
    ("trivial", 51, True), (None, 51, True),
])
def test_needs_review_by_tier_or_size(tier, lines, expected):
    diff = "--- a/x\n+++ b/x\n" + "".join(f"+{i}\n" for i in range(lines))
    unit = {"id": "u", "tier": tier} if tier else {"id": "u"}
    assert needs_review(unit, diff) is expected


# -- prompts -------------------------------------------------------------------

def test_review_prompt_is_adversarial_and_demands_repros():
    prompt = review_prompt({"id": "alpha", "task": "Build alpha", "check": "pytest -q"}, "DIFF-BODY")
    assert "Unit id: alpha" in prompt and "Build alpha" in prompt and "DIFF-BODY" in prompt
    assert "FAILS" in prompt and "correctness" in prompt and "security" in prompt
    assert prompt.count(FINDINGS_BEGIN) == 1 and prompt.index(FINDINGS_END) < prompt.index("DIFF-BODY")


def test_fix_prompt_carries_the_check_output_and_confirmed_findings():
    prompt = fix_prompt({"id": "alpha", "task": "Build alpha", "check": "pytest -q"},
                        check_output="E assert 1 == 2", confirmed=[finding("off by one")], pass_no=2)
    assert "Fix pass 2" in prompt and "Unit id: alpha" in prompt
    assert "E assert 1 == 2" in prompt and "[high] off by one" in prompt and "bash tests/fails.sh" in prompt
    assert "do not weaken" in prompt and "do not push" in prompt


def test_fix_prompt_without_check_failure_mentions_only_findings():
    prompt = fix_prompt({"id": "alpha", "task": "t", "check": "pytest -q"}, check_output=None,
                        confirmed=[finding()], pass_no=1)
    assert "acceptance check FAILS" not in prompt and "Confirmed review findings" in prompt


# -- parse_findings ------------------------------------------------------------

def test_missing_block_means_no_findings():
    assert parse_findings("Looks fine to me.") == [] and parse_findings("") == []


def test_parse_findings_normalises_entries_and_drops_malformed_ones():
    text = marked([
        {"title": "A", "severity": "HIGH", "detail": "d", "repro": "pytest -q tests/a.py"},
        {"title": "B", "severity": "unknown", "detail": {"k": 1}, "repro": ""},
        {"title": "C"},
        {"no": "title"},
        "string",
        {"title": " ", "repro": "x"},
    ])
    found = parse_findings(text)
    assert [f.title for f in found] == ["A", "B", "C"]
    assert found[0] == Finding("A", "high", "d", "pytest -q tests/a.py")
    assert found[1] == Finding("B", "medium", '{"k": 1}', None)
    assert found[2] == Finding("C", "medium", "", None)


def test_parse_findings_takes_the_last_block_and_strips_fences():
    text = marked([{"title": "old"}]) + marked("```json\n" + json.dumps([{"title": "new"}]) + "\n```")
    assert [f.title for f in parse_findings(text)] == ["new"]
    assert parse_findings(marked("")) == []


@pytest.mark.parametrize("body,reason", [
    (f"{FINDINGS_BEGIN}\n[]", "no matching"),
    (marked("{broken"), "not valid JSON"),
    (marked({"title": "x"}), "must be a JSON list"),
])
def test_parse_findings_rejects_unusable_blocks(body, reason):
    with pytest.raises(ValueError, match=reason):
        parse_findings(body)


# -- confirm -------------------------------------------------------------------

def test_confirm_splits_confirmed_from_advisory(worktree):
    directory, _ = worktree
    findings = [
        finding("real", "bash tests/fails.sh"),
        finding("not shown", "bash tests/passes.sh"),
        finding("no repro", None),
        finding("refused", "rm -rf tests"),
        finding("escapes", "bash tests/../../x.sh"),
    ]
    confirmed, advisory = confirm(findings, directory, timeout=30)
    assert [f.title for f in confirmed] == ["real"]
    assert [f.title for f in advisory] == ["not shown", "no repro", "refused", "escapes"]


def test_confirm_with_a_pytest_repro_flips_once_the_bug_is_fixed(worktree, tmp_path, monkeypatch):
    # Arrange: python3 on PATH is the interpreter running this suite, so pytest is importable.
    directory, _ = worktree
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    (binary_dir / "python3").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}{os.environ['PATH']}")
    repro = finding("pytest shows it", "python3 -m pytest -q tests/test_fixed.py")
    # Act
    before = confirm([repro], directory, timeout=120)
    (directory / "fixed.txt").write_text("fixed\n")
    after = confirm([repro], directory, timeout=120)
    # Assert
    assert before == ([repro], []) and after == ([], [repro])


@pytest.mark.parametrize("result", [
    RunResult(rc=124, output="", timed_out=True),
    RunResult(rc=126, output="refused: x", timed_out=False),
    RunResult(rc=127, output="could not run check", timed_out=False),
])
def test_confirm_treats_timeouts_refusals_and_missing_programs_as_not_evidence(worktree, monkeypatch, result):
    directory, _ = worktree
    monkeypatch.setattr(ultra_review, "run_checked", lambda cmd, cwd, *, timeout: result)
    confirmed, advisory = confirm([finding("unproven")], directory, timeout=1)
    assert confirmed == [] and [f.title for f in advisory] == ["unproven"]


# -- parse_review and resolved ------------------------------------------------

def test_parse_review_needs_a_clean_exit_and_a_marked_block():
    # Arrange
    text = marked([{"title": "bug", "severity": "high", "detail": "d", "repro": "bash tests/fails.sh"}])
    # Act
    found = ultra_review.parse_review(0, text)
    # Assert: a verdict is a block from a reviewer that finished; anything else is no verdict.
    assert [f.title for f in found] == ["bug"]
    with pytest.raises(ValueError, match="exited 1"):
        ultra_review.parse_review(1, text)
    with pytest.raises(ValueError, match="no ULTRA-FINDINGS-BEGIN"):
        ultra_review.parse_review(0, "Looks fine to me.")
    with pytest.raises(ValueError, match="not valid JSON"):
        ultra_review.parse_review(0, marked("{not json"))


def test_resolved_counts_only_a_passing_repro_as_gone(worktree, monkeypatch):
    # Arrange: one repro per outcome, by patching the runner with a table.
    directory, _ = worktree
    results = {
        "bash tests/passes.sh": RunResult(rc=0, output="", timed_out=False),
        "bash tests/fails.sh": RunResult(rc=1, output="", timed_out=False),
        "bash tests/slow.sh": RunResult(rc=124, output="", timed_out=True),
        "rm -rf tests": RunResult(rc=126, output="refused: x", timed_out=False),
    }
    monkeypatch.setattr(ultra_review, "run_checked", lambda cmd, cwd, *, timeout: results[cmd])
    findings = [finding(cmd, cmd) for cmd in results] + [finding("no repro", None)]
    # Act
    passing, outstanding = ultra_review.resolved(findings, directory, timeout=1)
    # Assert: a timeout or a refusal has not shown the bug gone.
    assert [f.title for f in passing] == ["bash tests/passes.sh"]
    assert [f.title for f in outstanding] == ["bash tests/fails.sh", "bash tests/slow.sh", "rm -rf tests", "no repro"]
