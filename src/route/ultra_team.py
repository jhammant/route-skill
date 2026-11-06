"""``route ultra --team``: a team of analysts, cross-examined, synthesized into one report.

A research run splits a question into parts; a team run looks at one target
(a URL, a repository, a decision) from several angles at once. Each analyst
is a role, a lens and a brief; it runs read-only on its own arm, and every
third analyst goes to another model family so the team does not share one
model's blind spots. Findings are merged into claims, the strongest claims
are handed to verifiers told to REFUTE them (another family where one has
quota), a critic names the lenses still missing, and a synthesizer writes
the report in the layout's shape. The run appends what was contested,
refuted or never examined, what only this session can do, every citation
resolved, and a footer.

Layouts come from the packaged ``data/teams.toml`` and the user's
``teams.toml`` in the config directory; without one a planner designs the
team. ``--fix`` hands the code blockers to a build run (``ultra.run_ultra``)
on a branch of its own; nothing here merges, pushes, deploys or prompts.
"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path

from . import cli, ultra, ultra_safety
from .eligibility import quota_keys
from .pools import Pool, pool_family, pools_for_host
from .storage import atomic_write, config_dir, state_dir
from .surplus import WEEK_MINUTES
from .ultra_plan import _marked_block
from .ultra_research import (
    _LOCK as LOCK,
    MCP_FILE,
    READ_ONLY_RULES,
    REPORT_BEGIN,
    REPORT_END,
    dispatch,
    git_porcelain,
    quota_table,
    research_mcp_config,
    sources_section,
    stop_parts,
    supported,
    trap_sigterm,
    worker_arm,
)
from .ultra_select import candidates, pick

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

TEAM_BEGIN = "ULTRA-TEAM-BEGIN"
TEAM_END = "ULTRA-TEAM-END"
ANALYSIS_BEGIN = "ULTRA-ANALYSIS-BEGIN"
ANALYSIS_END = "ULTRA-ANALYSIS-END"
VERDICT_BEGIN = "ULTRA-VERDICT-BEGIN"
VERDICT_END = "ULTRA-VERDICT-END"
CRITIC_BEGIN = "ULTRA-CRITIC-BEGIN"
CRITIC_END = "ULTRA-CRITIC-END"
FIX_BEGIN = "ULTRA-FIX-BEGIN"
FIX_END = "ULTRA-FIX-END"

#: Seconds. Module constants so a test (or an operator) can shorten them.
ANALYST_TIMEOUT = 1800
VERIFIER_TIMEOUT = 900
CRITIC_TIMEOUT = 900
SYNTHESIS_TIMEOUT = 1800
PLANNER_TIMEOUT = 900

ID = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
TIERS = ("moderate", "hard")
WHERE = ("remote", "session")
TARGETS = ("url", "repo", "any")
SEVERITIES = ("blocker", "high", "medium", "low", "info")
CONFIDENCE = ("high", "medium", "low")
VERDICTS = ("upheld", "refuted", "uncertain")
NOT_EXAMINED = "not cross-examined"
MIN_ANALYSTS, MAX_ANALYSTS = 3, 8
#: Analysts a critic may add per round; more would be a second team, not a gap.
CRITIC_CAP = 3
#: Word-set overlap at which two findings are the same claim.
JACCARD = 0.6
#: name -> (team_size, verifiers, rounds, max_claims).
INTENSITIES = {"full": (8, 3, 2, 20), "standard": (5, 1, 1, 12), "lean": (3, 1, 0, 8)}


class TeamPlanError(ValueError):
    """The planner's (or critic's) output cannot be used; the message says what to fix."""


class TeamAnalysisError(ValueError):
    """An analyst's output has no usable analysis block."""


class TeamReportError(ValueError):
    """The synthesizer's output has no usable report block."""


# -- data ------------------------------------------------------------------------

@dataclass
class Analyst:
    id: str
    role: str
    lens: str
    brief: str
    tier: str = "moderate"
    where: str = "remote"

    def to_dict(self) -> dict:
        return {"id": self.id, "role": self.role, "lens": self.lens, "brief": self.brief, "tier": self.tier,
                "where": self.where}


@dataclass
class TeamLayout:
    name: str
    title: str
    description: str
    targets: str
    fix: bool
    report: str
    analysts: list[Analyst]
    source: str = "builtin"

    def to_dict(self) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description, "targets": self.targets,
                "fix": self.fix, "report": self.report, "analysts": [a.to_dict() for a in self.analysts],
                "source": self.source}


@dataclass
class Intensity:
    name: str
    team_size: int
    verifiers: int
    rounds: int
    max_claims: int
    why: str


@dataclass
class Finding:
    id: str
    claim: str
    evidence: str
    sources: list[dict] = field(default_factory=list)
    confidence: str = "low"
    severity: str = "info"
    fix: str | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "claim": self.claim, "evidence": self.evidence, "sources": list(self.sources),
                "confidence": self.confidence, "severity": self.severity, "fix": self.fix}


@dataclass
class Analysis:
    summary: str
    findings: list[Finding] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"summary": self.summary, "findings": [f.to_dict() for f in self.findings], "gaps": list(self.gaps)}

    @property
    def sources(self) -> list[dict]:
        """Every source any finding cites, in order, one entry per id."""
        seen: dict[str, dict] = {}
        for finding in self.findings:
            for src in finding.sources:
                seen.setdefault(src.get("id"), src)
        return list(seen.values())


@dataclass
class Claim:
    id: str
    text: str
    severity: str
    supporters: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    fix: str | None = None
    confidence: str = "low"
    author: str = ""
    verdict: str | None = None
    verdicts: list[dict] = field(default_factory=list)
    examined: bool = False
    independence: str | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "severity": self.severity, "supporters": list(self.supporters),
                "sources": list(self.sources), "evidence": list(self.evidence), "fix": self.fix,
                "confidence": self.confidence, "author": self.author, "verdict": self.verdict,
                "verdicts": list(self.verdicts), "examined": self.examined, "independence": self.independence}


# -- layouts ---------------------------------------------------------------------

def _text(raw: dict, key: str, default: str = "") -> str:
    """A string field, or '' when it is the wrong type so the lint can name it."""
    value = raw.get(key, default)
    return value if isinstance(value, str) else ""


def analyst_from(raw: object) -> Analyst:
    """An analyst from a TOML table or planner JSON; wrong types become '' for the lint to report."""
    if not isinstance(raw, dict):
        return Analyst(id="", role="", lens="", brief="")
    return Analyst(id=_text(raw, "id"), role=_text(raw, "role"), lens=_text(raw, "lens"), brief=_text(raw, "brief"),
                   tier=_text(raw, "tier", "moderate"), where=_text(raw, "where", "remote"))


def _layout_from(name: str, raw: dict, source: str) -> TeamLayout:
    analysts = raw.get("analysts")
    rows = [analyst_from(item) for item in analysts] if isinstance(analysts, list) else []
    return TeamLayout(name=name, title=_text(raw, "title"), description=_text(raw, "description"),
                      targets=_text(raw, "targets", "any"), fix=raw.get("fix", False), report=_text(raw, "report"),
                      analysts=rows, source=source)


def _layouts_from(text: str, source: str) -> dict[str, TeamLayout]:
    try:
        raw = tomllib.loads(text)
    except (ValueError, TypeError):
        return {}
    table = raw.get("layouts") if isinstance(raw, dict) else None
    if not isinstance(table, dict):
        return {}
    return {name: _layout_from(name, body, source) for name, body in table.items() if isinstance(body, dict)}


def _read(path: Path | None) -> str | None:
    if path is None:
        try:
            return resources.files("route").joinpath("data/teams.toml").read_text(encoding="utf-8")
        except (OSError, TypeError, ValueError):
            return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def load_layouts(builtin: Path | None = None, user: Path | None = None) -> dict[str, TeamLayout]:
    """Built-in layouts first, in file order, then the user's; a user layout replaces a built-in by name.

    A missing or unreadable file contributes nothing and never raises: a
    broken ``teams.toml`` must not stop ``route ultra teams`` from listing
    the rest.
    """
    layouts: dict[str, TeamLayout] = {}
    builtin_text = _read(builtin)
    if builtin_text is not None:
        layouts.update(_layouts_from(builtin_text, "builtin"))
    user_text = _read(user if user is not None else config_dir() / "teams.toml")
    if user_text is not None:
        layouts.update(_layouts_from(user_text, "user"))
    return layouts


def lint_analysts(analysts: list[Analyst], *, taken: set[str] = frozenset()) -> list[str]:
    """Every rule an analyst can break; ``taken`` are ids already on the team (for the critic)."""
    problems: list[str] = []
    seen = set(taken)
    for analyst in analysts:
        label = f"analyst {analyst.id!r}"
        if not ID.fullmatch(analyst.id):
            problems.append(f"{label}: id must match [a-z0-9][a-z0-9-]{{0,40}}")
        if analyst.id in seen:
            problems.append(f"{label}: duplicate id")
        seen.add(analyst.id)
        for key in ("role", "lens", "brief"):
            if not getattr(analyst, key).strip():
                problems.append(f"{label}: {key} must be non-empty")
        if analyst.tier not in TIERS:
            problems.append(f"{label}: tier must be one of {', '.join(TIERS)}, not {analyst.tier!r}")
        if analyst.where not in WHERE:
            problems.append(f"{label}: where must be one of {', '.join(WHERE)}, not {analyst.where!r}")
    return problems


def lint_layout(layout: TeamLayout) -> list[str]:
    """Every reason a layout cannot run, so a user (or the planner) can fix them all at once."""
    problems: list[str] = []
    for key in ("title", "description", "report"):
        value = getattr(layout, key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{key} must be non-empty")
    if layout.targets not in TARGETS:
        problems.append(f"targets must be one of {', '.join(TARGETS)}, not {layout.targets!r}")
    if not isinstance(layout.fix, bool):
        problems.append(f"fix must be a bool, not {layout.fix!r}")
    count = len(layout.analysts)
    if count < MIN_ANALYSTS:
        problems.append(f"{count} analyst(s); at least {MIN_ANALYSTS} are needed")
    elif count > MAX_ANALYSTS:
        problems.append(f"{count} analysts; at most {MAX_ANALYSTS} are allowed")
    return problems + lint_analysts(layout.analysts)


def list_teams(json_out: bool = False) -> int:
    """``route ultra teams``: every layout, built-in and user, one line each."""
    layouts = load_layouts()
    if json_out:
        print(json.dumps([layout.to_dict() for layout in layouts.values()], indent=2))
        return 0
    print("name\ttitle\tanalysts\tdescription")
    for layout in layouts.values():
        print(f"{layout.name}\t{layout.title}\t{len(layout.analysts)}\t{layout.description}")
    return 0


# -- intensity -------------------------------------------------------------------

def _intensity(name: str, why: str) -> Intensity:
    team_size, verifiers, rounds, max_claims = INTENSITIES[name]
    return Intensity(name, team_size, verifiers, rounds, max_claims, why)


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value \
        and value not in (float("inf"), float("-inf"))


def team_intensity(window: dict | None, headroom: str | None, override: str | None) -> Intensity:
    """How big a team the week can afford: full, standard or lean, and why.

    Spare quota is what is left of the week's budget beyond the share already
    elapsed: at 10 percent used after six of seven days almost everything is
    spare, so the last two days are not zeroed the way the router's surplus
    bonus is. An override always wins; constrained headroom always leans.
    """
    if override in ("full", "lean"):
        return _intensity(override, f"{override}: requested (--{'thorough' if override == 'full' else 'lean'})")
    if override not in (None, "", "standard"):
        raise ValueError(f"unknown intensity: {override!r}")
    if headroom in ("low", "constrained", "critical"):
        return _intensity("lean", f"lean: headroom is {headroom}")
    window = window or {}
    used, resets = window.get("percentUsed"), window.get("resetsInMinutes")
    if not (_finite(used) and _finite(resets)):
        return _intensity("standard", "standard: no weekly window to pace against")
    elapsed = WEEK_MINUTES - resets
    if elapsed <= 0:
        return _intensity("standard", "standard: the weekly window has just reset")
    spare = max(0.0, min(1.0, 1 - (used / 100) / (elapsed / WEEK_MINUTES)))
    percent = (f"{spare * 100:.0f}% of the week's quota is spare ({used:.0f}% used, "
               f"{elapsed / WEEK_MINUTES:.0%} elapsed)")
    if spare >= 0.5:
        return _intensity("full", f"full: {percent}")
    if spare >= 0.15:
        return _intensity("standard", f"standard: {percent}")
    return _intensity("lean", f"lean: {percent}")


# -- arms ------------------------------------------------------------------------

def _usable(pools: dict[str, Pool], quota: dict, *, host: str, allow_lower: bool,
            exclude_families: set[str] = frozenset()) -> list[str]:
    return [arm for arm in candidates(pools, quota, host=host, allow_lower=allow_lower,
                                      exclude_families=exclude_families) if supported(arm, pools, host)]


def spread_arms(analysts: list[Analyst], pools: dict[str, Pool], quota: dict, *, host: str,
                allow_lower: bool) -> dict[str, str | None]:
    """An arm per remote analyst: the best moderate arm, hard analysts on the rank-3 arm,
    every third remote analyst on another model family when one has quota. None means held."""
    main = worker_arm(pools, quota, tier="moderate", host=host, allow_lower=allow_lower)
    family = pool_family(pools[main]) if main is not None else None
    arms: dict[str, str | None] = {}
    remote = [analyst for analyst in analysts if analyst.where != "session"]
    for index, analyst in enumerate(remote):
        if analyst.tier == "hard":
            arms[analyst.id] = worker_arm(pools, quota, tier="hard", host=host, allow_lower=allow_lower)
        elif (index + 1) % 3 == 0 and family is not None:
            others = _usable(pools, quota, host=host, allow_lower=allow_lower, exclude_families={family})
            arms[analyst.id] = others[0] if others else main
        else:
            arms[analyst.id] = main
    return arms


# -- claims ----------------------------------------------------------------------

def _words(text: str) -> frozenset[str]:
    return frozenset(word for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) >= 3)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _rank(value: str, order: tuple[str, ...]) -> int:
    return order.index(value) if value in order else len(order)


def merge_findings(analyses: dict[str, Analysis]) -> list[Claim]:
    """Near-duplicate findings across analysts become one claim with every supporter and source.

    Greedy in analyst order: a finding joins the first claim whose word set
    overlaps it at least ``JACCARD``, else starts a new one. The merged claim
    keeps the highest severity and confidence, every source (tagged with its
    analyst so ``[analyst:S1]`` still resolves) and the first fix offered.
    """
    claims: list[Claim] = []
    words: list[frozenset[str]] = []
    for analyst_id, analysis in analyses.items():
        for finding in analysis.findings:
            tagged = [{"analyst": analyst_id, "id": src.get("id"), "ref": src.get("ref"), "note": src.get("note", "")}
                      for src in finding.sources]
            mine = _words(finding.claim)
            for claim, known in zip(claims, words):
                if _jaccard(mine, known) >= JACCARD:
                    if analyst_id not in claim.supporters:
                        claim.supporters.append(analyst_id)
                    claim.sources.extend(src for src in tagged if src not in claim.sources)
                    if finding.evidence.strip():
                        claim.evidence.append(f"{analyst_id}: {finding.evidence.strip()}")
                    if _rank(finding.severity, SEVERITIES) < _rank(claim.severity, SEVERITIES):
                        claim.severity = finding.severity
                    if _rank(finding.confidence, CONFIDENCE) < _rank(claim.confidence, CONFIDENCE):
                        claim.confidence = finding.confidence
                    claim.fix = claim.fix or finding.fix
                    break
            else:
                claims.append(Claim(id=f"C{len(claims) + 1}", text=finding.claim.strip(), severity=finding.severity,
                                    supporters=[analyst_id], sources=tagged,
                                    evidence=[f"{analyst_id}: {finding.evidence.strip()}"] if finding.evidence.strip()
                                    else [], fix=finding.fix, confidence=finding.confidence, author=analyst_id))
                words.append(mine)
    return claims


def decide_verdict(verdicts: list[str]) -> str:
    """A strict majority of upheld or refuted decides; anything else is contested."""
    counted = [verdict if verdict in VERDICTS else "uncertain" for verdict in verdicts]
    total = len(counted)
    if total and counted.count("upheld") * 2 > total:
        return "upheld"
    if total and counted.count("refuted") * 2 > total:
        return "refuted"
    return "contested"


def _examination_order(claims: list[Claim]) -> list[Claim]:
    return sorted(claims, key=lambda claim: (_rank(claim.severity, SEVERITIES), -len(claim.supporters),
                                             int(claim.id[1:]) if claim.id[1:].isdigit() else 0))


# -- prompts ---------------------------------------------------------------------

ANALYST_SCHEMA = ('{"id": "lowercase-id", "role": "who this analyst is", "lens": "what it looks at", '
                  '"brief": "what to check and how", "tier": "moderate|hard", "where": "remote|session"}')


def _mcp_note(mcp_names: list[str]) -> str:
    return (f"the MCP servers {', '.join(mcp_names)}" if mcp_names else "no MCP servers")


def targets_text(urls: tuple[str, ...], repo: Path | None) -> str:
    lines = [f"- URL: {url}" for url in urls]
    if repo is not None:
        lines.append(f"- Repository (read access): {repo}")
    return "\n".join(lines) if lines else "(no URL or repository given: work from the question, the web and your tools)"


def team_prompt(question: str, *, team_size: int, targets: str, context_note: str = "",
                mcp_names: list[str] = ()) -> str:
    """The planner designs the team: it sees the question and the targets, not this conversation."""
    note = f"\n{context_note.strip()}\n" if context_note.strip() else ""
    return f"""You are the planner for an automated analysis team ("route ultra --team").
{READ_ONLY_RULES}
{note}
# The question

{question.strip()}

# Targets

{targets}

# Your job

Design a team of {MIN_ANALYSTS} to {team_size} analysts who each look at the question from ONE angle, in
priority order (the most important first: a smaller budget keeps the first ones). Each analyst runs
alone in the background with web search and fetch, a shell, {_mcp_note(list(mcp_names))}, and read
access to the targets. An analyst that needs claude.ai connectors (Gmail, Calendar, Drive), Claude in
Chrome, a GUI walkthrough, or this Mac CANNOT run in the background: give it "where": "session" so the
user's own session does it afterwards. Mark "tier": "hard" only for the analyst that needs the
strongest model.

Each analyst: {ANALYST_SCHEMA}
- "id": lowercase letters, digits and hyphens, at most 41 characters, unique.
- "role", "lens", "brief": non-empty; the brief is complete on its own.

Also give "title": a short name for the review, and "report": one or two sentences on how the final
report should be shaped (a verdict first? a scorecard? ranked issues?).

# Output

Think as you need, then print ONLY this, as the last thing, with nothing after it:

{TEAM_BEGIN}
{{"title": "...", "report": "...", "analysts": [{ANALYST_SCHEMA}]}}
{TEAM_END}
"""


def analyst_prompt(question: str, analyst: Analyst, *, targets: str, context_note: str = "",
                   mcp_names: list[str] = ()) -> str:
    """One analyst's brief; ``Analyst id:`` is on its own line so logs and retries match it."""
    note = f"\n{context_note.strip()}\n" if context_note.strip() else ""
    return f"""You are one analyst in an automated analysis team ("route ultra --team").
Analyst id: {analyst.id}
{READ_ONLY_RULES}
{note}
# The question (other analysts cover other angles)

{question.strip()}

# Your role

{analyst.role.strip()}

# Your lens

{analyst.lens.strip()}

# Your brief

{analyst.brief.strip()}

# Targets

{targets}

# Rules

- Read only: use web search and fetch, your shell for reading and computing, {_mcp_note(list(mcp_names))},
  and the files in your working directory. Write no files: your findings go in your output.
- Every finding is one checkable claim with its evidence and a source id from your "sources" list.
- "severity": blocker, high, medium, low or info. "confidence": high, medium or low. "fix": the
  concrete fix, or null when there is nothing to fix.
- Say what you could not check under "gaps" instead of guessing.
- Finish with ONE JSON object between the markers, as the last thing you print, nothing after it:

{ANALYSIS_BEGIN}
{{"summary": "two to five sentences",
 "findings": [{{"id": "F1", "claim": "one checkable statement", "evidence": "what you saw, citing [S1]",
               "sources": [{{"id": "S1", "ref": "url | path:line | record id", "note": "what it shows"}}],
               "confidence": "high|medium|low", "severity": "blocker|high|medium|low|info",
               "fix": "the fix, or null"}}],
 "gaps": ["what you could not check"]}}
{ANALYSIS_END}
"""


def verifier_prompt(question: str, claim: Claim, *, targets: str, mcp_names: list[str] = ()) -> str:
    """A verifier is told to refute the claim; agreement must survive a real attempt to break it."""
    sources = "\n".join(f"- [{src['analyst']}:{src['id']}] {src.get('ref')}: {src.get('note', '')}"
                        for src in claim.sources) or "(none given)"
    evidence = "\n".join(f"- {line}" for line in claim.evidence) or "(none given)"
    return f"""You are a verifier in an automated analysis team ("route ultra --team").
Claim id: {claim.id}
{READ_ONLY_RULES}

# The question under review

{question.strip()}

# The claim (severity {claim.severity}, made by {', '.join(claim.supporters)})

{claim.text}

Evidence offered:
{evidence}

Sources offered:
{sources}

# Targets

{targets}

# Your job

Try to REFUTE this claim. Check its sources and evidence yourself (web search and fetch, your shell,
{_mcp_note(list(mcp_names))}, the targets). Then say "upheld" only when the evidence stands up to your
attempt, "refuted" when you found it wrong or unsupported, "uncertain" when you could not check.
Finish with ONE JSON object between the markers, as the last thing you print, nothing after it:

{VERDICT_BEGIN}
{{"verdict": "upheld|refuted|uncertain", "reason": "what you checked and what you found",
 "sources": [{{"id": "V1", "ref": "url | path:line", "note": "what it shows"}}]}}
{VERDICT_END}
"""


def critic_prompt(question: str, analysts: list[Analyst], claims: list[Claim], *, cap: int) -> str:
    lenses = "\n".join(f"- {a.id} ({a.role.strip()}): {a.lens.strip()}" for a in analysts) or "(none)"
    found = "\n".join(f"- [{c.severity}] {c.text} ({c.verdict or 'unexamined'}; {', '.join(c.supporters)})"
                      for c in claims) or "(nothing survived)"
    return f"""You are the critic of an automated analysis team ("route ultra --team").
{READ_ONLY_RULES} Do not investigate: judge the coverage from what is below.

# The question

{question.strip()}

# The lenses already on the team

{lenses}

# What the team found so far

{found}

# Your job

Name the angles the team MISSED that would change the answer: at most {cap} new analysts, each with a
new unique id, a role, a lens and a complete brief ({ANALYST_SCHEMA}). An empty list means the
coverage is good enough. Print ONLY this, as the last thing, nothing after it:

{CRITIC_BEGIN}
{{"missing": []}}
{CRITIC_END}
"""


def _claim_block(claim: Claim) -> str:
    sources = "\n".join(f"  - [{src['analyst']}:{src['id']}] {src.get('ref')}: {src.get('note', '')}"
                        for src in claim.sources) or "  (none)"
    evidence = "\n".join(f"  - {line}" for line in claim.evidence) or "  (none)"
    verdicts = "\n".join(f"  - {row['arm']} ({row['independence']}): {row['verdict']}: {row['reason']}"
                         for row in claim.verdicts) or "  (not cross-examined)"
    fix = f"\nFix offered: {claim.fix}" if claim.fix else ""
    return (f"### {claim.id} [{claim.severity}, {claim.verdict or NOT_EXAMINED}] {claim.text}\n\n"
            f"Supporters: {', '.join(claim.supporters)}; confidence {claim.confidence}{fix}\n\n"
            f"Evidence:\n{evidence}\n\nSources:\n{sources}\n\nVerifiers:\n{verdicts}")


def synthesis_prompt(question: str, *, title: str, report_shape: str, surviving: list[Claim], refuted: list[Claim],
                     unexamined: list[Claim], session: list[Analyst], fix_block: bool, repo: Path | None,
                     notes: list[str] = ()) -> str:
    """Everything the team found, for one report in the layout's shape, cited as [analyst-id:S1]."""
    session_lines = "\n".join(f"- {a.id}: {a.role.strip()}: {a.brief.strip()}" for a in session) or "(none)"
    fix = ""
    if fix_block:
        fix = f"""
# Fix build

After the report, ALSO print one JSON object for an automated build run on the repository at {repo}:
"goal" describes the code changes that would resolve the code blockers and high-severity code findings
above (be specific: files, behaviour, tests), and "goal_check" is ONE command that proves it, one of
the project's test or lint runners (for example `pytest -q`, `npm test`, `go test ./...`), no shell
operators, no paths outside the repository. Skip findings that are not code changes. Print it as the
last thing, after the report block:

{FIX_BEGIN}
{{"goal": "...", "goal_check": "pytest -q"}}
{FIX_END}
"""
    extra = "\n".join(f"- {note}" for note in notes)
    ending = ", then the fix block" if fix_block else ", as the last thing, nothing after it"
    return f"""You are the synthesizer for an automated analysis team ("route ultra --team").
{READ_ONLY_RULES} Do not investigate further: write from the material below only.

# The question

{question.strip()}

# The report shape ({title})

{report_shape.strip()}

# Claims that stood (upheld or contested)

{chr(10).join(_claim_block(c) for c in surviving) if surviving else '(none)'}

# Claims that were refuted (mention only to say they did not stand)

{chr(10).join(_claim_block(c) for c in refuted) if refuted else '(none)'}

# Claims not cross-examined (lower priority; cite them as unverified)

{chr(10).join(_claim_block(c) for c in unexamined) if unexamined else '(none)'}

# Left for the user's own session (do not answer these; say they are pending)

{session_lines}

# Notes from the run

{extra or '(none)'}

# Output

Write the report in markdown in the shape above: the verdict or answer first, then the material as
the shape asks, contested points shown with both sides. Cite every claim as [analyst-id:S1] using
exactly the source ids listed above; never invent one. Print the report between the markers{ending}:

{REPORT_BEGIN}
...markdown...
{REPORT_END}
{fix}"""


# -- parsing ---------------------------------------------------------------------

def _json_block(text: str, begin: str, end: str, error: type[ValueError], what: str) -> dict:
    block = _marked_block(text or "", begin, end, error)
    if block is None:
        raise error(f"no {begin} block in the output")
    try:
        raw = json.loads(block)
    except ValueError as exc:
        raise error(f"{what} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise error(f"{what} must be a JSON object")
    return raw


def parse_team(text: str) -> tuple[str, str, list[Analyst]]:
    """(title, report shape, analysts) from the planner's last marked block; content rules are the lint's."""
    raw = _json_block(text, TEAM_BEGIN, TEAM_END, TeamPlanError, "team")
    analysts = raw.get("analysts")
    if not isinstance(analysts, list):
        raise TeamPlanError("team 'analysts' must be a list")
    return _text(raw, "title"), _text(raw, "report"), [analyst_from(item) for item in analysts]


def _sources(raw: object, where: str, error: type[ValueError]) -> list[dict]:
    if not isinstance(raw, list):
        raise error(f"{where}: 'sources' must be a list")
    for index, source in enumerate(raw):
        if not isinstance(source, dict) or not isinstance(source.get("id"), str) \
                or not isinstance(source.get("ref"), str):
            raise error(f"{where}: source {index} must be an object with string 'id' and 'ref'")
        if "note" in source and not isinstance(source["note"], str):
            raise error(f"{where}: source {source['id']!r}: 'note' must be a string")
    return [dict(source) for source in raw]


def parse_analysis(text: str) -> Analysis:
    """The last marked JSON object in an analyst's output, every field checked."""
    raw = _json_block(text, ANALYSIS_BEGIN, ANALYSIS_END, TeamAnalysisError, "analysis")
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise TeamAnalysisError("'summary' must be a non-empty string")
    findings = raw.get("findings")
    if not isinstance(findings, list):
        raise TeamAnalysisError("'findings' must be a list")
    rows: list[Finding] = []
    for index, item in enumerate(findings):
        where = f"finding {index}"
        if not isinstance(item, dict):
            raise TeamAnalysisError(f"{where}: must be an object")
        claim = item.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            raise TeamAnalysisError(f"{where}: 'claim' must be a non-empty string")
        evidence = item.get("evidence", "")
        if not isinstance(evidence, str):
            raise TeamAnalysisError(f"{where}: 'evidence' must be a string")
        if item.get("confidence") not in CONFIDENCE:
            raise TeamAnalysisError(f"{where}: 'confidence' must be one of {', '.join(CONFIDENCE)}")
        if item.get("severity") not in SEVERITIES:
            raise TeamAnalysisError(f"{where}: 'severity' must be one of {', '.join(SEVERITIES)}")
        fix = item.get("fix")
        if fix is not None and not isinstance(fix, str):
            raise TeamAnalysisError(f"{where}: 'fix' must be a string or null")
        identity = item.get("id", f"F{index + 1}")
        if not isinstance(identity, str):
            raise TeamAnalysisError(f"{where}: 'id' must be a string")
        rows.append(Finding(id=identity, claim=claim, evidence=evidence,
                            sources=_sources(item.get("sources", []), where, TeamAnalysisError),
                            confidence=item["confidence"], severity=item["severity"], fix=fix or None))
    gaps = raw.get("gaps", [])
    if not isinstance(gaps, list) or not all(isinstance(gap, str) for gap in gaps):
        raise TeamAnalysisError("'gaps' must be a list of strings")
    return Analysis(summary=summary, findings=rows, gaps=list(gaps))


def parse_verdict(text: str) -> dict:
    """A verifier's verdict; anything unusable counts as 'uncertain' with the reason recorded."""
    try:
        raw = _json_block(text, VERDICT_BEGIN, VERDICT_END, TeamReportError, "verdict")
    except TeamReportError as exc:
        return {"verdict": "uncertain", "reason": f"unparseable verdict: {exc}", "sources": []}
    verdict = raw.get("verdict")
    reason = raw.get("reason")
    sources = raw.get("sources", [])
    return {"verdict": verdict if verdict in VERDICTS else "uncertain",
            "reason": reason.strip() if isinstance(reason, str) and reason.strip()
            else (f"no reason given (verdict {verdict!r})"),
            "sources": [dict(src) for src in sources if isinstance(src, dict)] if isinstance(sources, list) else []}


def parse_critic(text: str) -> list[Analyst] | None:
    """The critic's missing analysts, or None when the output is unusable (which ends the loop)."""
    try:
        raw = _json_block(text, CRITIC_BEGIN, CRITIC_END, TeamPlanError, "critic")
    except TeamPlanError:
        return None
    missing = raw.get("missing")
    if not isinstance(missing, list):
        return None
    return [analyst_from(item) for item in missing]


def parse_fix(text: str) -> tuple[dict | None, str | None]:
    """(the fix block, None) or (None, why it cannot be used)."""
    try:
        raw = _json_block(text, FIX_BEGIN, FIX_END, TeamReportError, "fix")
    except TeamReportError as exc:
        return None, str(exc)
    goal, check = raw.get("goal"), raw.get("goal_check")
    if not isinstance(goal, str) or not goal.strip():
        return None, "fix block has no 'goal'"
    if not isinstance(check, str) or not check.strip():
        return None, "fix block has no 'goal_check'"
    return {"goal": goal.strip(), "goal_check": check.strip()}, None


# -- analysts --------------------------------------------------------------------

def _run_analyst(run: ultra.Run, analyst: Analyst, arm: str, pools: dict[str, Pool], quota: dict, host: str,
                 allow_lower: bool, prompt: str, cwd: Path, mcp_config: Path | None,
                 cancel: threading.Event | None = None) -> dict:
    """One analyst on its arm, retried once on the next candidate; what happened, for progress."""
    tried: list[str] = []
    failures: list[str] = []
    while arm is not None and not (cancel is not None and cancel.is_set()):
        tried.append(arm)
        with LOCK:
            run.unit(analyst.id, arm=arm, state="running", attempts=list(tried))
        log = f"analyst-{analyst.id}.log" if len(tried) == 1 else f"analyst-{analyst.id}-{len(tried)}.log"
        rc, text = dispatch(run, arm, pools, host, prompt, cwd, log, ANALYST_TIMEOUT, mcp_config)
        try:
            if rc == 124:
                raise TeamAnalysisError("timed out")
            analysis = parse_analysis(text)
        except TeamAnalysisError as exc:
            if cancel is not None and cancel.is_set():
                with LOCK:
                    run.unit(analyst.id, arm=arm, state="interrupted", reason="run interrupted", attempts=list(tried))
                return {"id": analyst.id, "state": "interrupted", "arm": arm, "analysis": None,
                        "reason": "run interrupted", "attempts": tried}
            failures.append(f"{arm}: {exc}" + (f" (exit {rc})" if rc not in (0, 124) else ""))
            arm = (worker_arm(pools, quota, tier=analyst.tier, host=host, allow_lower=allow_lower, exclude=set(tried))
                   if len(tried) < 2 else None)
            continue
        atomic_write(run.dir / "analysts" / f"{analyst.id}.json",
                     json.dumps({**analyst.to_dict(), "arm": arm, "attempts": tried, **analysis.to_dict()},
                                indent=2) + "\n")
        with LOCK:
            run.unit(analyst.id, arm=arm, state="analysed", reason=None, attempts=list(tried),
                     findings=len(analysis.findings))
        return {"id": analyst.id, "state": "analysed", "arm": arm, "analysis": analysis, "attempts": tried}
    reason = "; ".join(failures) or "no arm"
    with LOCK:
        run.unit(analyst.id, arm=tried[-1] if tried else None, state="failed", reason=reason, attempts=list(tried))
    return {"id": analyst.id, "state": "failed", "arm": tried[-1] if tried else None, "analysis": None,
            "reason": reason, "attempts": tried}


def _run_verifier(run: ultra.Run, claim: Claim, index: int, arm: str, independence: str, pools: dict[str, Pool],
                  host: str, prompt: str, cwd: Path, mcp_config: Path | None,
                  cancel: threading.Event | None = None) -> dict:
    """One verifier on one claim, never retried: an unusable verdict is 'uncertain'."""
    rc, text = dispatch(run, arm, pools, host, prompt, cwd, f"verify-{claim.id}-{index}.log", VERIFIER_TIMEOUT,
                        mcp_config)
    if rc == 124:
        row = {"verdict": "uncertain", "reason": "verifier timed out", "sources": []}
    else:
        row = parse_verdict(text)
    return {"claim": claim.id, "arm": arm, "independence": independence, **row}


@dataclass
class _Context:
    """What every step of one run shares; kept together so the helpers stay short."""
    run: ultra.Run
    question: str
    pools: dict[str, Pool]
    quota: dict
    host: str
    allow_lower: bool
    repo: Path | None
    urls: tuple[str, ...]
    context_note: str
    mcp_config: Path | None
    mcp_names: list[str]
    max_parallel: int
    intensity: Intensity

    @property
    def targets(self) -> str:
        return targets_text(self.urls, self.repo)

    def work_dir(self, kind: str, name: str) -> Path:
        """The repo when given, else an empty directory of the run's own: agents never share scratch."""
        if self.repo is not None:
            return self.repo
        path = self.run.dir / kind / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def integrator(self) -> str | None:
        arm = pick("integrator", self.pools, self.quota, host=self.host, allow_lower=self.allow_lower)
        if arm is not None and not supported(arm, self.pools, self.host):
            arm = next(iter(_usable(self.pools, self.quota, host=self.host, allow_lower=self.allow_lower)), None)
        return arm


def _parallel(ctx: _Context, jobs: list[tuple[str, tuple]], worker) -> dict[str, dict]:
    """Run ``worker(*args)`` per job in the run's executor; an interrupt stops every part first."""
    results: dict[str, dict] = {}
    cancel = threading.Event()
    executor = ThreadPoolExecutor(max_workers=max(1, ctx.max_parallel))
    futures: dict[str, object] = {}
    try:
        for key, args in jobs:
            futures[key] = executor.submit(worker, *args, cancel)
        for key, future in futures.items():
            results[key] = future.result()
    except BaseException:
        stop_parts(ctx.run, cancel, executor, list(futures.values()))
        raise
    executor.shutdown(wait=True)
    return results


def _analyse(ctx: _Context, analysts: list[Analyst], arms: dict[str, str | None]) -> dict[str, dict]:
    """Every remote analyst with an arm, in parallel; {id: result}."""
    jobs = []
    for analyst in analysts:
        arm = arms.get(analyst.id)
        if analyst.where == "session" or arm is None:
            continue
        prompt = analyst_prompt(ctx.question, analyst, targets=ctx.targets, context_note=ctx.context_note,
                                mcp_names=ctx.mcp_names)
        jobs.append((analyst.id, (ctx.run, analyst, arm, ctx.pools, ctx.quota, ctx.host, ctx.allow_lower, prompt,
                                  ctx.work_dir("analysts", analyst.id), ctx.mcp_config)))
    ctx.run.step("analysing", f"{len(jobs)} analyst(s), up to {ctx.max_parallel} in parallel")
    return _parallel(ctx, jobs, _run_analyst) if jobs else {}


def _verifier_arms(ctx: _Context, author_arm: str | None, count: int) -> list[tuple[str, str]]:
    """``count`` (arm, independence) pairs: other families first, the author's own family after."""
    family = pool_family(ctx.pools[author_arm]) if author_arm in ctx.pools else None
    cross = _usable(ctx.pools, ctx.quota, host=ctx.host, allow_lower=ctx.allow_lower,
                    exclude_families={family} if family else frozenset())
    same = [arm for arm in _usable(ctx.pools, ctx.quota, host=ctx.host, allow_lower=ctx.allow_lower)
            if arm not in cross]
    ordered = [(arm, "cross-family") for arm in cross] + [(arm, "same-family") for arm in same]
    if not ordered:
        return []
    return [ordered[index % len(ordered)] for index in range(count)]


def _examine(ctx: _Context, claims: list[Claim], arms: dict[str, str | None], cache: dict[frozenset, Claim]) -> None:
    """Cross-examine the top claims in place; verdicts already given (by word set) are kept."""
    budget = ctx.intensity.max_claims
    ordered = _examination_order(claims)
    jobs = []
    pending: dict[str, Claim] = {}
    for claim in ordered:
        key = _words(claim.text)
        if key in cache:
            done = cache[key]
            claim.verdicts, claim.verdict, claim.examined, claim.independence = \
                list(done.verdicts), done.verdict, done.examined, done.independence
        if claim.examined:
            budget -= 1
            continue
        if budget <= 0 or ctx.intensity.verifiers <= 0:
            claim.verdict = NOT_EXAMINED
            continue
        budget -= 1
        pairs = _verifier_arms(ctx, arms.get(claim.author), ctx.intensity.verifiers)
        if not pairs:
            claim.verdict = NOT_EXAMINED
            ctx.run.note(f"claim {claim.id}: no usable verifier has quota")
            continue
        pending[claim.id] = claim
        prompt = verifier_prompt(ctx.question, claim, targets=ctx.targets, mcp_names=ctx.mcp_names)
        for index, (arm, independence) in enumerate(pairs, start=1):
            jobs.append((f"{claim.id}-{index}", (ctx.run, claim, index, arm, independence, ctx.pools, ctx.host, prompt,
                                                 ctx.work_dir("verify", f"{claim.id}-{index}"), ctx.mcp_config)))
    if jobs:
        ctx.run.step("cross-examining", f"{len(pending)} claim(s), {len(jobs)} verifier run(s)")
        results = _parallel(ctx, jobs, _run_verifier)
        for row in results.values():
            pending[row["claim"]].verdicts.append(row)
    for claim in pending.values():
        claim.verdict = decide_verdict([row["verdict"] for row in claim.verdicts])
        claim.examined = True
        claim.independence = "cross-family" if claim.verdicts and \
            all(row["independence"] == "cross-family" for row in claim.verdicts) else "same-family"
        cache[_words(claim.text)] = claim


def _critic(ctx: _Context, analysts: list[Analyst], claims: list[Claim], round_no: int) -> list[Analyst] | None:
    """The critic's new analysts for one round, linted against the team; None when the loop should stop."""
    arm = ctx.integrator()
    if arm is None:
        ctx.run.note("critic: no usable model has quota")
        return None
    surviving = [claim for claim in claims if claim.verdict in ("upheld", "contested")]
    ctx.run.step("critic", f"round {round_no} by {arm}")
    rc, text = dispatch(ctx.run, arm, ctx.pools, ctx.host, critic_prompt(ctx.question, analysts, surviving,
                                                                        cap=CRITIC_CAP),
                        ctx.work_dir("critic", f"round-{round_no}"), f"critic-{round_no}.log", CRITIC_TIMEOUT,
                        ctx.mcp_config)
    missing = parse_critic(text) if rc != 124 else None
    if missing is None:
        ctx.run.note(f"critic round {round_no}: no usable ULTRA-CRITIC block (exit {rc}); stopping")
        return None
    missing = missing[:CRITIC_CAP]
    taken = {analyst.id for analyst in analysts}
    kept: list[Analyst] = []
    for analyst in missing:
        problems = lint_analysts([analyst], taken=taken)
        if problems:
            ctx.run.note(f"critic round {round_no}: dropped {analyst.id!r}: " + "; ".join(problems))
            continue
        taken.add(analyst.id)
        kept.append(analyst)
    return kept


# -- report ----------------------------------------------------------------------

def _counts(claims: list[Claim]) -> dict[str, int]:
    counts = {"upheld": 0, "contested": 0, "refuted": 0, NOT_EXAMINED: 0}
    for claim in claims:
        counts[claim.verdict if claim.verdict in counts else NOT_EXAMINED] += 1
    return counts


def fallback_report(question: str, title: str, claims: list[Claim]) -> str:
    """The report when no synthesizer delivered: the question, the verdict counts, every surviving claim."""
    counts = _counts(claims)
    lines = [f"# {title or question.strip()}", "", question.strip(), "",
             "No synthesizer delivered a report; the team's claims follow as found.", "",
             "Verdicts: " + ", ".join(f"{count} {name}" for name, count in counts.items())]
    surviving = [claim for claim in _examination_order(claims) if claim.verdict != "refuted"]
    for claim in surviving:
        lines += ["", f"## {claim.id} [{claim.severity}, {claim.verdict or NOT_EXAMINED}] {claim.text}", "",
                  f"Supporters: {', '.join(claim.supporters)}; confidence {claim.confidence}"]
        if claim.evidence:
            lines += ["", "Evidence:"] + [f"- {line}" for line in claim.evidence]
        if claim.sources:
            lines += ["", "Sources:"] + [f"- [{src['analyst']}:{src['id']}] {src.get('ref')}: {src.get('note', '')}"
                                         for src in claim.sources]
        if claim.fix:
            lines += ["", f"Fix: {claim.fix}"]
    if not surviving:
        lines += ["", "No claim survived."]
    return "\n".join(lines) + "\n"


def _verdict_section(heading: str, claims: list[Claim], *, both_sides: bool) -> list[str]:
    lines = [heading, ""]
    if not claims:
        return lines + ["(none)"]
    for claim in claims:
        lines.append(f"- {claim.id} [{claim.severity}] {claim.text} ({', '.join(claim.supporters)})")
        if both_sides or heading == "## Refuted":
            lines += [f"  - {row['arm']} ({row['independence']}): {row['verdict']}: {row['reason']}"
                      for row in claim.verdicts]
    return lines


def _summary_md(summary: dict, body: str, claims: list[Claim], session: list[Analyst],
                sources: dict[str, list[dict]]) -> str:
    by_verdict = {name: [c for c in claims if c.verdict == name] for name in ("contested", "refuted", NOT_EXAMINED)}
    lines = [body.rstrip(), ""]
    lines += _verdict_section("## Contested", by_verdict["contested"], both_sides=True) + [""]
    lines += _verdict_section("## Refuted", by_verdict["refuted"], both_sides=True) + [""]
    if by_verdict[NOT_EXAMINED]:
        lines += _verdict_section("## Not cross-examined", by_verdict[NOT_EXAMINED], both_sides=False) + [""]
    lines += ["## For your session", ""]
    if session:
        lines += [f"- {a.id}: {a.role.strip()}: {a.brief.strip()}" for a in session]
    else:
        lines.append("Nothing: every analyst ran in the background.")
    lines += [""] + sources_section(body, sources)
    intensity = summary["intensity"] or {}
    lines += ["", "## Run", "", f"- Run: {summary['run_id']}",
              f"- Layout: {summary['layout'] or 'planner-designed'}" + (f" ({summary['title']})" if summary.get("title")
                                                                        else ""),
              f"- Intensity: {intensity.get('name', '-')} (team {intensity.get('team_size')}, "
              f"{intensity.get('verifiers')} verifier(s) per claim, {intensity.get('rounds')} critic round(s), "
              f"{intensity.get('max_claims')} claims examined); {intensity.get('why', '')}"]
    team = []
    for row in summary["analysts"]:
        state = row["state"]
        team.append(f"{row['id']}: " + (row["arm"] if state in ("analysed", "running") and row.get("arm")
                                        else "session" if state == "session" else "held" if state == "held"
                                        else f"failed ({row.get('arm') or '-'})" if state == "failed" else state))
    lines.append("- Team: " + ("; ".join(team) if team else "none"))
    lines.append(f"- Planner: {summary['planner'] or '-'}; synthesizer: "
                 f"{summary['synthesizer'] or 'none (deterministic report)'}")
    examined = [c for c in claims if c.examined]
    independence = {"cross-family": 0, "same-family": 0}
    for claim in examined:
        if claim.independence in independence:
            independence[claim.independence] += 1
    counts = summary["verdict_counts"]
    lines.append("- Claims: " + ", ".join(f"{count} {name}" for name, count in counts.items())
                 + f"; verifiers per claim: {intensity.get('verifiers')}; independence: "
                 + f"{independence['cross-family']} cross-family, {independence['same-family']} same-family")
    lines.append(f"- Critic rounds run: {summary['critic_rounds']}")
    failed = [f"{row['id']} ({row.get('reason')})" for row in summary["analysts"] if row["state"] == "failed"]
    if failed:
        lines.append("- Failed analysts: " + "; ".join(failed))
    if summary["held"]:
        lines.append("- Held analysts: " + "; ".join(f"{row['id']} ({row['reason']})" for row in summary["held"]))
    mcp = summary["mcp_servers"]
    if mcp.get("loaded"):
        lines.append("- MCP servers loaded: " + ", ".join(mcp["loaded"]))
    else:
        lines.append(f"- MCP servers loaded: none (add `mcp_servers = [\"name\"]` to {mcp.get('path')} "
                     "to let research agents use some; list only read-only servers)")
    if mcp.get("missing"):
        lines.append("- Allowlisted but not found in ~/.claude.json: " + ", ".join(mcp["missing"]))
    if summary["porcelain_changes"]:
        lines += ["- WARNING: the repository changed during the run (git status --porcelain differs; "
                  "ultra never reverts):"] + [f"  - {line}" for line in summary["porcelain_changes"]]
    if summary["quota"]:
        lines.append("- Quota (percent used, before -> after):")
        for provider, labels in summary["quota"].items():
            moved = ", ".join(f"{label} {v['before']} -> {v['after']}" for label, v in labels.items())
            lines.append(f"  - {provider}: {moved}")
    fix = summary.get("fix") or {}
    if fix.get("state") == "built":
        lines += ["- Fix build:", f"  - Run: {fix.get('run_id') or 'not found'}",
                  f"  - Branch: {fix.get('branch') or '-'}", f"  - Exit: {fix.get('exit_code')}",
                  f"  - Merge: {fix.get('merge_command') or '-'}"]
    elif fix.get("state") == "skipped":
        lines.append(f"- Fix build: skipped ({fix.get('reason')})")
    if summary["notes"]:
        lines += ["- Notes:"] + [f"  - {note}" for note in summary["notes"][:10]]
    lines += [f"- Run directory: {summary['run_dir']}", f"- Exit: {summary['exit_code']}"]
    return "\n".join(lines) + "\n"


def _report(ctx: _Context, body: str, claims: list[Claim], analysts: list[Analyst], analyses: dict[str, Analysis],
            synthesizer: str | None, mcp_info: dict, porcelain: list[str], usage_before: dict, exit_code: int,
            json_out: bool, *, layout: TeamLayout | None, title: str, report_shape: str, fix: dict | None) -> int:
    """Write summary.md and summary.json, print one of them, finish the run; the exit code."""
    run = ctx.run
    usage_after = cli.fetch_usage()
    by_id = {analyst.id: analyst for analyst in analysts}
    rows = []
    for unit_id, state in run.progress["units"].items():
        analyst = by_id.get(unit_id)
        rows.append({"id": unit_id, "state": state["state"], "arm": state.get("arm"), "where": state.get("where"),
                     "tier": state.get("tier"), "role": analyst.role if analyst else None,
                     "reason": state.get("reason"), "attempts": state.get("attempts", []),
                     "findings": state.get("findings")})
    session = [analyst for analyst in analysts if analyst.where == "session"]
    summary = {
        "run_id": run.id, "question": ctx.question, "mode": "team", "layout": layout.name if layout else None,
        "title": title, "report_shape": report_shape, "intensity": run.progress.get("intensity"),
        "planner": run.progress.get("planner"), "synthesizer": synthesizer, "analysts": rows,
        "claims": [claim.to_dict() for claim in claims], "verdict_counts": _counts(claims),
        "critic_rounds": run.progress.get("critic_rounds", 0), "held": run.progress.get("held", []),
        "plan_problems": run.progress.get("plan_problems", []), "mcp_servers": dict(mcp_info),
        "porcelain_changes": list(porcelain), "quota": quota_table(usage_before, usage_after),
        "urls": list(ctx.urls), "repo": str(ctx.repo) if ctx.repo else None, "fix": fix,
        "exit_code": exit_code, "run_dir": str(run.dir), "context": run.progress.get("context"),
        "notes": run.progress.get("notes", []),
    }
    sources = {analyst_id: analysis.sources for analyst_id, analysis in analyses.items()}
    text = _summary_md(summary, body, claims, session, sources)
    atomic_write(run.dir / "summary.md", text)
    atomic_write(run.dir / "summary.json", json.dumps(summary, indent=2) + "\n")
    run.finish("done" if exit_code == 0 else "failed", exit_code)
    print(json.dumps(summary, indent=2) if json_out else text, flush=True)
    return exit_code


def _give_up(ctx: _Context, reason: str, mcp_info: dict, usage_before: dict, json_out: bool, *,
             layout: TeamLayout | None, title: str = "", report_shape: str = "",
             analysts: list[Analyst] = ()) -> int:
    """Exit 2: nothing was analysed, and the report says why."""
    ctx.run.note(reason)
    body = fallback_report(ctx.question, title, []).rstrip() + f"\n\nNot run: {reason}\n"
    return _report(ctx, body, [], list(analysts), {}, None, mcp_info, [], usage_before, 2, json_out, layout=layout,
                   title=title, report_shape=report_shape, fix=None)


# -- fix build -------------------------------------------------------------------

def _is_work_tree(repo: Path) -> bool:
    probe = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, errors="replace")
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def fix_eligibility(layout: TeamLayout | None, repo: Path | None, fix: bool) -> str | None:
    """Why ``--fix`` cannot run, or None when it can: a layout that allows it and a git work tree."""
    if not fix:
        return None
    if layout is None:
        return "planner-designed teams never fix; use a layout with fix = true"
    if not layout.fix:
        return f"layout {layout.name!r} has fix = false"
    if repo is None:
        return "--fix needs --repo"
    if not _is_work_tree(repo):
        return f"{repo} is not a git work tree"
    return None


def _find_build(goal: str, own: Path) -> dict:
    """The newest build run whose goal is ``goal``: its id, branch and merge command from summary.json."""
    newest: tuple[float, Path] | None = None
    for progress in (state_dir() / "ultra").glob("*/progress.json"):
        if progress.parent == own:
            continue
        try:
            raw = json.loads(progress.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if raw.get("goal") != goal:
            continue
        stamp = progress.stat().st_mtime
        if newest is None or stamp > newest[0]:
            newest = (stamp, progress.parent)
    if newest is None:
        return {}
    try:
        summary = json.loads((newest[1] / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"run_id": newest[1].name}
    integration = summary.get("integration") or {}
    return {"run_id": summary.get("run_id", newest[1].name), "branch": integration.get("branch"),
            "merge_command": summary.get("merge_command")}


def _run_fix(ctx: _Context, synthesis_text: str) -> dict:
    """Hand the synthesizer's fix goal to a build run; what happened, for the footer.

    The goal check is linted with ``ultra_safety.lint_command``, which takes the
    process's current directory as the repository boundary (as build mode
    does): run ``route ultra --team --fix`` from the repository, or the
    check's paths are judged against wherever the command was started.
    """
    block, why = parse_fix(synthesis_text)
    if block is None:
        return {"state": "skipped", "reason": why}
    reason = ultra_safety.lint_command(block["goal_check"])
    if reason:
        return {"state": "skipped", "reason": f"goal check {block['goal_check']!r} refused: {reason}"}
    goal = block["goal"] + "\n\nGoal check: " + block["goal_check"]
    ctx.run.step("fixing", "build run for the code findings")
    rc = ultra.run_ultra(goal, repo=ctx.repo, base="HEAD", host=ctx.host)
    return {"state": "built", "exit_code": rc, "goal": goal, **_find_build(goal, ctx.run.dir)}


# -- the run ---------------------------------------------------------------------

def _print_plan(analysts: list[Analyst], arms: dict[str, str | None], held: list[dict], intensity: Intensity,
                title: str, layout: TeamLayout | None, json_out: bool) -> None:
    if json_out:
        print(json.dumps({"layout": layout.name if layout else None, "title": title,
                          "analysts": [a.to_dict() for a in analysts], "arms": arms, "held": held,
                          "intensity": intensity.__dict__}, indent=2))
        return
    print(f"team: {title or '(untitled)'} ({len(analysts)} analysts; {layout.name if layout else 'planner-designed'})")
    print(f"intensity: {intensity.name}: {intensity.why}")
    print("analyst\twhere\ttier\tarm\trole")
    for analyst in analysts:
        arm = arms.get(analyst.id) or ("-" if analyst.where == "session" else "held")
        print(f"{analyst.id}\t{analyst.where}\t{analyst.tier}\t{arm}\t{analyst.role}")
    for row in held:
        print(f"held: {row['id']}: {row['reason']}")


def _assign(ctx: _Context, analysts: list[Analyst], arms: dict[str, str | None]) -> None:
    """Record every analyst's arm (or why it is held) in progress.json."""
    run = ctx.run
    for analyst in analysts:
        if analyst.where == "session":
            run.unit(analyst.id, state="session", where="session", tier=analyst.tier, reason="for your session")
            continue
        arm = arms.get(analyst.id)
        if arm is None:
            reason = "no usable model has quota (or none can be made read-only)"
            run.progress["held"].append({"id": analyst.id, "reason": reason})
            run.unit(analyst.id, state="held", where="remote", tier=analyst.tier, reason=reason)
        else:
            run.unit(analyst.id, arm=arm, state="planned", where="remote", tier=analyst.tier)


def _plan_team(ctx: _Context, team_size: int) -> tuple[str, str, list[Analyst], list[str]]:
    """A planner-designed team, re-planned once with the lint problems appended."""
    run = ctx.run
    planner = pick("planner", ctx.pools, ctx.quota, host=ctx.host, allow_lower=ctx.allow_lower)
    if planner is not None and not supported(planner, ctx.pools, ctx.host):
        planner = next(iter(_usable(ctx.pools, ctx.quota, host=ctx.host, allow_lower=ctx.allow_lower)), None)
    if planner is None:
        return "", "", [], ["no usable model has quota to plan (try --allow-lower-models)"]
    run.progress["planner"] = planner
    prompt = team_prompt(ctx.question, team_size=team_size, targets=ctx.targets, context_note=ctx.context_note,
                         mcp_names=ctx.mcp_names)
    problems: list[str] = []
    for attempt in (1, 2):
        run.step("planning", f"planner {planner} (attempt {attempt})")
        text_prompt = prompt if attempt == 1 else prompt + (
            "\n\n# Problems with your previous team (fix all of them)\n\n"
            + "\n".join(f"- {problem}" for problem in problems) + "\n")
        rc, text = dispatch(run, planner, ctx.pools, ctx.host, text_prompt, ctx.work_dir("plan", f"attempt-{attempt}"),
                            f"plan-{attempt}.log", PLANNER_TIMEOUT, ctx.mcp_config)
        try:
            if rc == 124:
                raise TeamPlanError("planner timed out")
            title, report, analysts = parse_team(text)
        except TeamPlanError as exc:
            found = [str(exc)]
        else:
            found = lint_layout(TeamLayout("planned", title, "planner-designed", "any", False, report, analysts))
            found = [p for p in found if not p.startswith("description")]
            if len(analysts) > team_size:
                found.append(f"{len(analysts)} analysts; the budget is {team_size}")
            if not found:
                return title, report, analysts, []
        problems = found
        run.progress["plan_problems"].extend(found)
        if attempt == 1:
            run.note("re-planning once: " + "; ".join(found))
    return "", "", [], problems


def run_team(question: str, *, layout: str | None = None, urls: tuple[str, ...] = (), repo: str | Path | None = None,
             team_size: int | None = None, rounds: int | None = None, intensity: str | None = None,
             fix: bool = False, context: str = "brief", session: str | None = None, max_parallel: int = 6,
             plan_only: bool = False, allow_lower: bool = False, json_out: bool = False,
             host: str = "claude") -> int:
    """One whole team run; exit 0 when every remote analyst answered, nothing was held and the report
    was written (and a --fix build exited 0); 1 when an analyst failed or was held or the build did not;
    2 when the question, layout, context or repository is unusable or nothing has quota."""
    question = question.strip()
    if not question:
        print("route ultra --team: empty question", file=sys.stderr)
        return 2
    if context not in ("brief", "digest"):
        print(f"route ultra: --context must be brief or digest, not {context}", file=sys.stderr)
        return 2
    repo_path = Path(repo).expanduser().resolve() if repo else None
    if repo_path is not None and not repo_path.is_dir():
        print(f"route ultra --team: --repo {repo_path} is not a directory", file=sys.stderr)
        return 2
    layouts = load_layouts()
    chosen: TeamLayout | None = None
    if layout:
        chosen = layouts.get(layout)
        if chosen is None:
            print(f"route ultra --team: unknown layout {layout}; valid: {', '.join(layouts) or '(none)'}",
                  file=sys.stderr)
            return 2
        problems = lint_layout(chosen)
        if problems:
            print(f"route ultra --team: layout {layout} is invalid: " + "; ".join(problems), file=sys.stderr)
            return 2
    run = ultra.Run(ultra.new_run_id(question), question, repo_path or Path.cwd(), "", host)
    run.progress["mode"] = "team"
    run.progress["layout"] = chosen.name if chosen else None
    try:
        run.dir.mkdir(parents=True, mode=0o700)
        (run.dir / "logs").mkdir(mode=0o700)
    except FileExistsError:
        print(f"route ultra: run {run.id} already exists at {run.dir}", file=sys.stderr)
        return 2
    run.save()
    print(f"ultra: run {run.id} at {run.dir}", flush=True)
    previous = trap_sigterm()
    try:
        return _execute(run, question, chosen, tuple(urls), repo_path, team_size, rounds, intensity, fix, context,
                        session, max_parallel, plan_only, allow_lower, json_out, host)
    except KeyboardInterrupt:
        run.finish("interrupted", 130)
        print("ultra: interrupted", file=sys.stderr)
        return 130
    finally:
        (run.dir / MCP_FILE).unlink(missing_ok=True)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def trim_team(analysts: list[Analyst], team_size: int) -> list[Analyst]:
    """The first ``team_size`` remote analysts plus every session analyst, in layout order.

    Session analysts are never dispatched, so they cost no quota: a lean run
    still tells the user what only their own session can check.
    """
    kept = {analyst.id for analyst in [a for a in analysts if a.where != "session"][:team_size]}
    return [analyst for analyst in analysts if analyst.where == "session" or analyst.id in kept]


def _weekly_window(arm: str | None, pools: dict[str, Pool], quota: dict) -> tuple[dict | None, str | None]:
    """The arm's tightest weekly window and its headroom, as ``ultra_select._adjustment`` reads them."""
    if arm is None:
        return None, None
    keys = quota_keys(arm, pools)
    weekly = getattr(quota, "weekly", {}) or {}
    window = max((weekly.get(key, {}) for key in keys), key=lambda w: w.get("percentUsed") or 0, default={})
    headroom = next((quota.get(key) for key in reversed(keys) if quota.get(key)), None)
    return (window or None), headroom


def _execute(run: ultra.Run, question: str, layout: TeamLayout | None, urls: tuple[str, ...], repo: Path | None,
             team_size: int | None, rounds: int | None, intensity_override: str | None, fix: bool, context: str,
             session: str | None, max_parallel: int, plan_only: bool, allow_lower: bool, json_out: bool,
             host: str) -> int:
    pools = pools_for_host(cli.load_pools(), host)
    quota = cli.fetch_quota()
    usage_before = cli.fetch_usage()
    run.progress["headroom"] = dict(quota)
    context_note = ultra._context(run, context, session)
    mcp_config, mcp_info = research_mcp_config(run.dir)
    run.progress["mcp_servers"] = {"loaded": mcp_info["loaded"], "missing": mcp_info["missing"]}

    main_arm = worker_arm(pools, quota, tier="moderate", host=host, allow_lower=allow_lower)
    window, headroom = _weekly_window(main_arm, pools, quota)
    intensity = team_intensity(window, headroom, intensity_override)
    if team_size is not None:
        intensity = replace(intensity, team_size=max(1, team_size), why=intensity.why + f"; --team-size {team_size}")
    if rounds is not None:
        intensity = replace(intensity, rounds=max(0, rounds), why=intensity.why + f"; --rounds {rounds}")
    run.progress["intensity"] = dict(intensity.__dict__)
    ctx = _Context(run, question, pools, quota, host, allow_lower, repo, urls, context_note, mcp_config,
                   list(mcp_info["loaded"]), max_parallel, intensity)

    notes: list[str] = []
    if layout is not None:
        title, report_shape = layout.title, layout.report
        analysts = trim_team(layout.analysts, intensity.team_size)
        if layout.targets == "url" and not urls:
            notes.append(f"layout {layout.name!r} targets a URL but none was given (--url); "
                         "the analysts work from the question alone")
        if layout.targets == "repo" and repo is None:
            notes.append(f"layout {layout.name!r} targets a repository but none was given (--repo); "
                         "the analysts work from the question alone")
        for note in notes:
            run.note(note)
    else:
        title, report_shape, analysts, problems = _plan_team(ctx, intensity.team_size)
        if problems:
            return _give_up(ctx, "could not plan a team: " + "; ".join(problems), mcp_info, usage_before, json_out,
                            layout=None)
    run.progress["title"] = title

    run.step("assigning", "choosing an arm per analyst")
    arms = spread_arms(analysts, pools, quota, host=host, allow_lower=allow_lower)
    _assign(ctx, analysts, arms)
    plan = {"layout": layout.name if layout else None, "title": title, "report": report_shape,
            "analysts": [a.to_dict() for a in analysts], "arms": arms, "held": run.progress["held"],
            "intensity": dict(intensity.__dict__), "urls": list(urls), "repo": str(repo) if repo else None}
    atomic_write(run.dir / "plan.json", json.dumps(plan, indent=2) + "\n")
    if plan_only:
        _print_plan(analysts, arms, run.progress["held"], intensity, title, layout, json_out)
        run.finish("planned", 0)
        return 0
    remote = [a for a in analysts if a.where != "session"]
    if remote and not any(arms.get(a.id) for a in remote):
        return _give_up(ctx, "no usable model has quota for any analyst", mcp_info, usage_before, json_out,
                        layout=layout, title=title, report_shape=report_shape, analysts=analysts)
    fix_skip = fix_eligibility(layout, repo, fix)

    porcelain_before = git_porcelain(repo) if repo is not None else None
    (run.dir / "analysts").mkdir(exist_ok=True)
    results = _analyse(ctx, analysts, arms)
    analyses: dict[str, Analysis] = {key: row["analysis"] for key, row in results.items()
                                     if row["state"] == "analysed"}
    claims = merge_findings(analyses)
    atomic_write(run.dir / "claims.json", json.dumps([c.to_dict() for c in claims], indent=2) + "\n")
    cache: dict[frozenset, Claim] = {}
    _examine(ctx, claims, arms, cache)

    rounds_run = 0
    for round_no in range(1, intensity.rounds + 1):
        missing = _critic(ctx, analysts, claims, round_no)
        if missing is None:
            break
        rounds_run += 1
        if not missing:
            run.note(f"critic round {round_no}: nothing missing")
            break
        run.note(f"critic round {round_no}: adding " + ", ".join(a.id for a in missing))
        more_arms = spread_arms(missing, pools, quota, host=host, allow_lower=allow_lower)
        _assign(ctx, missing, more_arms)
        analysts.extend(missing)
        arms.update(more_arms)
        results.update(_analyse(ctx, missing, more_arms))
        analyses = {key: row["analysis"] for key, row in results.items() if row["state"] == "analysed"}
        claims = merge_findings(analyses)
        _examine(ctx, claims, arms, cache)
    run.progress["critic_rounds"] = rounds_run
    atomic_write(run.dir / "claims.json", json.dumps([c.to_dict() for c in claims], indent=2) + "\n")
    atomic_write(run.dir / "verdicts.json", json.dumps(
        [{"id": c.id, "text": c.text, "verdict": c.verdict, "independence": c.independence, "verdicts": c.verdicts}
         for c in claims], indent=2) + "\n")

    porcelain: list[str] = []
    if porcelain_before is not None:
        after = git_porcelain(repo) or []
        porcelain = [line for line in after if line not in porcelain_before] + \
                    [f"(no longer) {line}" for line in porcelain_before if line not in after]
        if porcelain:
            run.note(f"repository {repo} changed during the run: {len(porcelain)} path(s)")

    session_analysts = [a for a in analysts if a.where == "session"]
    surviving = [c for c in _examination_order(claims) if c.verdict in ("upheld", "contested")]
    refuted = [c for c in claims if c.verdict == "refuted"]
    unexamined = [c for c in _examination_order(claims) if c.verdict == NOT_EXAMINED]
    synthesizer = ctx.integrator()
    body = None
    synthesis_text = ""
    want_fix = fix and fix_skip is None
    if synthesizer is not None and analyses:
        run.step("synthesizing", f"by {synthesizer}")
        prompt = synthesis_prompt(question, title=title, report_shape=report_shape, surviving=surviving,
                                  refuted=refuted, unexamined=unexamined, session=session_analysts,
                                  fix_block=want_fix, repo=repo, notes=notes)
        rc, synthesis_text = dispatch(run, synthesizer, pools, host, prompt, run.dir, "synthesis.log",
                                      SYNTHESIS_TIMEOUT, mcp_config)
        try:
            body = _marked_block(synthesis_text, REPORT_BEGIN, REPORT_END, TeamReportError) if rc == 0 else None
        except TeamReportError:
            body = None
        if not body or not body.strip():
            run.note(f"synthesizer {synthesizer} delivered no report (exit {rc}); writing the claims as found")
            body = None
    if body is None:
        synthesizer = None
        body = fallback_report(question, title, claims)
    run.progress["synthesizer"] = synthesizer

    fix_result: dict | None = None
    if fix:
        fix_result = {"state": "skipped", "reason": fix_skip} if fix_skip else _run_fix(ctx, synthesis_text)
        if fix_result.get("state") == "skipped":
            run.note(f"fix build skipped: {fix_result['reason']}")
    exit_code = 0 if all(results.get(a.id, {}).get("state") == "analysed" for a in analysts if a.where != "session") \
        and not run.progress["held"] and (fix_result is None or fix_result.get("state") != "built"
                                          or fix_result.get("exit_code") == 0) else 1
    return _report(ctx, body, claims, analysts, analyses, synthesizer, mcp_info, porcelain, usage_before, exit_code,
                   json_out, layout=layout, title=title, report_shape=report_shape, fix=fix_result)
