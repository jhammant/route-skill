"""Cross-family review of a unit, and the fix briefs that follow it.

A reviewer is a model from another family reading the unit's diff. Its word is
not evidence: a finding counts only once its repro command has been run in the
unit's own worktree and failed there, which is why every finding must carry a
command that fails while the bug exists. Everything else is advisory and goes
in the summary, never to a fixer.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .ultra_safety import ALLOWED, run_checked

FINDINGS_BEGIN = "ULTRA-FINDINGS-BEGIN"
FINDINGS_END = "ULTRA-FINDINGS-END"
SEVERITIES = ("high", "medium", "low")

#: Tiers whose units always get a review; below them the diff size decides.
REVIEW_TIERS = frozenset({"moderate", "hard", "open-ended"})
REVIEW_LINES = 50

#: Exit codes that say nothing about the code under test: a timeout, a refused
#: command, and a command that could not be found.
_NOT_EVIDENCE = frozenset({124, 126, 127})

_FENCE = re.compile(r"^```[\w-]*\s*$", re.M)


@dataclass
class Finding:
    title: str
    severity: str
    detail: str
    repro: str | None

    def to_dict(self) -> dict:
        return {"title": self.title, "severity": self.severity, "detail": self.detail, "repro": self.repro}


def unit_diff(worktree: str | Path, base: str, *, max_chars: int = 60000) -> str:
    """What the unit changed since the base, cut from the end to fit a review prompt."""
    try:
        proc = subprocess.run(["git", "-C", str(worktree), "diff", f"{base}...HEAD"],
                              capture_output=True, text=True, errors="replace")
    except OSError as exc:
        return f"[diff unavailable: {exc}]"
    if proc.returncode:
        return f"[diff unavailable: {proc.stderr.strip()}]"
    diff = proc.stdout
    if len(diff) > max_chars:
        omitted = len(diff) - max_chars
        diff = diff[:max_chars] + f"\n[diff truncated: {omitted} more characters not shown]\n"
    return diff


def _changed_lines(diff: str) -> int:
    return sum(1 for line in diff.splitlines()
               if (line.startswith("+") and not line.startswith("+++"))
               or (line.startswith("-") and not line.startswith("---")))


def needs_review(unit: dict, diff: str) -> bool:
    """Review anything non-trivial by tier, or by size when the tier says trivial."""
    return unit.get("tier") in REVIEW_TIERS or _changed_lines(diff) > REVIEW_LINES


def review_prompt(unit: dict, diff: str) -> str:
    """An adversarial brief: real defects with repros, or nothing."""
    allowed = "\n".join(f"  - {' '.join(prefix)}" for prefix in ALLOWED)
    return f"""You are an independent reviewer for one unit of an automated build ("route ultra").
You are in a read-only checkout of the unit's branch. Another model wrote this change; your job is
to find what is wrong with it, not to praise it or restyle it.

Unit id: {unit['id']}
The unit's brief:
{unit.get('task', '').strip()}

Its acceptance check (already passing): {unit.get('check', '')}

# What counts

Real defects only, in this order: correctness (wrong results, missed cases, broken interfaces, tests
that do not test the claim), then security (injection, path escapes, leaked secrets, unsafe
defaults). Not style, naming, or anything the brief did not ask for. Read the surrounding code when
the diff alone cannot settle a question.

For EVERY finding give a "repro": one command, run from the repository root, that FAILS (exits
non-zero) while the bug exists and passes once it is fixed. It will be run in the unit's own
worktree, so it may only use files that exist on this branch or a NEW test file you add under
tests/ or test/ (new test files you create are carried over; edits to existing files are not).
The repro follows the check rules: it may only start with one of these prefixes, parts may be
joined with " && " and nothing else, paths relative and inside the repository:
{allowed}
A finding you cannot give a runnable repro for gets "repro": null and is reported as advisory only.

# Output

Print ONLY this, as the last thing (an empty list when the change is sound):

{FINDINGS_BEGIN}
[{{"title": "...", "severity": "high|medium|low", "detail": "what is wrong and where", "repro": "..." }}]
{FINDINGS_END}

# The diff

{diff}
"""


def _finding_from(raw: object) -> Finding | None:
    """One well-formed finding, or None: a malformed entry is dropped, not fatal."""
    if not isinstance(raw, dict) or not isinstance(raw.get("title"), str) or not raw["title"].strip():
        return None
    severity = str(raw.get("severity", "medium")).lower()
    repro = raw.get("repro")
    if not isinstance(repro, str) or not repro.strip():
        repro = None
    detail = raw.get("detail", "")
    return Finding(title=raw["title"].strip(), severity=severity if severity in SEVERITIES else "medium",
                   detail=detail if isinstance(detail, str) else json.dumps(detail), repro=repro)


def parse_findings(text: str) -> list[Finding]:
    """The last marked findings list; no block means the reviewer found nothing to say."""
    start = (text or "").rfind(FINDINGS_BEGIN)
    if start < 0:
        return []
    start += len(FINDINGS_BEGIN)
    stop = text.find(FINDINGS_END, start)
    if stop < 0:
        raise ValueError(f"{FINDINGS_BEGIN} has no matching {FINDINGS_END}")
    block = _FENCE.sub("", text[start:stop]).strip()
    try:
        raw = json.loads(block) if block else []
    except ValueError as exc:
        raise ValueError(f"findings are not valid JSON: {exc}") from None
    if not isinstance(raw, list):
        raise ValueError("findings must be a JSON list")
    return [finding for finding in map(_finding_from, raw) if finding is not None]


def confirm(findings: list[Finding], worktree: str | Path, *, timeout: float = 600) -> tuple[list[Finding], list[Finding]]:
    """Split findings into (confirmed, advisory) by running each repro in the unit's worktree.

    Only a repro that is allowed to run and then fails on its own merits confirms
    a bug; a timeout, a refused command or a missing program proves nothing.
    """
    confirmed, advisory = [], []
    for finding in findings:
        if finding.repro is None:
            advisory.append(finding)
            continue
        result = run_checked(finding.repro, Path(worktree), timeout=timeout)
        if result.rc != 0 and result.rc not in _NOT_EVIDENCE and not result.timed_out:
            confirmed.append(finding)
        else:
            advisory.append(finding)
    return confirmed, advisory


def fix_prompt(unit: dict, *, check_output: str | None, confirmed: list[Finding], pass_no: int) -> str:
    """The brief for a fixer: what failed, how it was shown, and what done looks like."""
    parts = [f"""You are fixing one unit of an automated build ("route ultra") in its own worktree.
Fix pass {pass_no}: make the unit correct and keep the rest of its work intact.

Unit id: {unit['id']}
The unit's original brief:
{unit.get('task', '').strip()}

Acceptance check (run from the repository root): {unit.get('check', '')}"""]
    if check_output is not None:
        parts.append("# The acceptance check FAILS\n\nIts output (tail):\n\n```text\n"
                     f"{check_output.strip()[-6000:]}\n```")
    if confirmed:
        lines = []
        for index, finding in enumerate(confirmed, 1):
            lines.append(f"{index}. [{finding.severity}] {finding.title}\n   {finding.detail.strip()}\n"
                         f"   Repro (fails while the bug exists): {finding.repro}")
        parts.append("# Confirmed review findings\n\nEach repro below was run here and failed, so these "
                     "are real:\n\n" + "\n".join(lines))
    parts.append("""# What to do

Find the cause and fix it properly; do not weaken, skip or delete the check or the repros to make
them pass. Then run the acceptance check and every repro yourself and make sure they all pass.
Leave your changes in the working tree (committing is fine too); route records them when the check
passes. Do not touch files outside this worktree and do not push.""")
    return "\n\n".join(parts) + "\n"
