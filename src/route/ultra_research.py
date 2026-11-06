"""``route ultra --research``: answer a question as a swarm of read-only agents.

A build run spends the second Claude account on code; this spends it on
investigation. A planner splits the question into independent parts, each
remote part runs on the best arm with quota (web search, shell, the MCP servers
the user allowlisted, read access to ``--repo`` when given), a synthesizer
writes one report, and the run itself appends what only this session can do
and a sources section that resolves every citation.

Agents are forced read-only at the command line (``--disallowedTools Edit
Write NotebookEdit`` for Claude, ``--sandbox read-only`` for Codex) and told so
in their brief; a repository handed over with ``--repo`` is checked with ``git
status --porcelain`` before and after, and any difference is reported, never
reverted. Nothing here ever asks for approval.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import signal
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from . import cli, ultra
from .pools import Pool, pools_for_host
from .swarm_dispatch import signal_tree
from .storage import atomic_write, config_dir
from .ultra_plan import _marked_block
from .ultra_select import candidates, dispatch_template, model_rank, pick

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

PLAN_BEGIN = "ULTRA-RESEARCH-PLAN-BEGIN"
PLAN_END = "ULTRA-RESEARCH-PLAN-END"
ANSWER_BEGIN = "ULTRA-ANSWER-BEGIN"
ANSWER_END = "ULTRA-ANSWER-END"
REPORT_BEGIN = "ULTRA-REPORT-BEGIN"
REPORT_END = "ULTRA-REPORT-END"

#: Seconds. Module constants so a test (or an operator) can shorten them.
PLANNER_TIMEOUT = 1800
PART_TIMEOUT = 1800
SYNTHESIS_TIMEOUT = 1800

PART_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
WHERE = ("remote", "session")
TIERS = ("moderate", "hard")
CONFIDENCE = ("high", "medium", "low")
MCP_FILE = "research-mcp.json"
CODEX_SKIP_GIT_CHECK = "--skip-git-repo-check"
#: How long an interrupted run gives its parts to exit on SIGTERM before SIGKILL.
CANCEL_GRACE = 5.0
#: Agents read ``[part-id:S1]`` from the synthesizer's report; the run resolves them.
CITE = re.compile(r"\[([a-z0-9][a-z0-9-]{0,40}):(S\d+)\]")
#: The brief that every read-only agent gets, in one place so parts and the planner agree.
READ_ONLY_RULES = ("You are READ-ONLY. Never send, post, create, book, buy, subscribe, delete or change "
                   "anything anywhere: no email, no messages, no calendar events, no files outside "
                   "scratch output, no commits, no settings. Investigate, then report.")

_LOCK = threading.RLock()


class ResearchPlanError(ValueError):
    """The planner's output cannot be used; the message says what to fix."""


class ResearchAnswerError(ValueError):
    """A part's output has no usable answer block."""


class ResearchReportError(ValueError):
    """The synthesizer's output has no usable report block."""


# -- data ------------------------------------------------------------------------

@dataclass
class Part:
    id: str
    question: str
    where: str
    needs: str = ""
    tier: str = "moderate"

    def to_dict(self) -> dict:
        return {"id": self.id, "question": self.question, "where": self.where, "needs": self.needs,
                "tier": self.tier}


@dataclass
class ResearchPlan:
    title: str
    parts: list[Part]

    def to_dict(self) -> dict:
        return {"title": self.title, "parts": [part.to_dict() for part in self.parts]}

    @property
    def remote(self) -> list[Part]:
        return [part for part in self.parts if part.where == "remote"]

    @property
    def session(self) -> list[Part]:
        return [part for part in self.parts if part.where == "session"]


@dataclass
class Answer:
    answer: str
    sources: list[dict] = field(default_factory=list)
    confidence: str = "low"
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"answer": self.answer, "sources": list(self.sources), "confidence": self.confidence,
                "gaps": list(self.gaps)}


# -- prompts ---------------------------------------------------------------------

def _mcp_note(mcp_names: list[str]) -> str:
    return (f"the MCP servers {', '.join(mcp_names)}" if mcp_names else "no MCP servers")


def research_plan_prompt(question: str, *, max_parts: int, context_note: str = "", repo: Path | None = None,
                         mcp_names: list[str] = ()) -> str:
    """The planner sees the question but not this conversation, so the brief carries every rule."""
    note = f"\n{context_note.strip()}\n" if context_note.strip() else ""
    repo_line = (f"Background agents also have READ access to the repository at {repo}." if repo
                 else "No repository is attached: agents work from the web, their shell and MCP servers.")
    return f"""You are the planner for an automated parallel investigation ("route ultra --research").
{READ_ONLY_RULES}
{note}
# The question

{question.strip()}

# Your job

Split the question into at most {max_parts} INDEPENDENT sub-questions. Independent means: each part
can be answered by a separate agent that sees only its own sub-question and the original question,
and the answers combine into one report without the agents talking. Fewer, well-scoped parts beat
many tiny ones; one part is fine when the question does not split.

Remote parts are answered by background agents with web search and fetch, a shell, {_mcp_note(list(mcp_names))},
and nothing else. {repo_line}
A part that needs claude.ai connectors (Gmail, Calendar, Drive), Claude in Chrome, or this Mac
(local apps, the screen, devices, files outside the repository) CANNOT run in the background: mark it
"where": "session" and say in "needs" exactly which of those it needs, so the user's own session can
do it afterwards.

Each part needs:
- "id": lowercase letters, digits and hyphens, at most 41 characters, unique.
- "question": a complete, self-contained sub-question for an agent that sees ONLY this text and the
  original question.
- "where": "remote" or "session".
- "needs": for session parts, which connector, browser or local resource it needs (non-empty); may be
  empty for remote parts.
- optional "tier": "hard" for a part that needs the strongest model (deep synthesis, conflicting
  sources, careful reasoning); leave it out otherwise.

Also give "title": a short name for the whole investigation.

# Output

Think as you need, then print ONLY this, as the last thing, with nothing after it:

{PLAN_BEGIN}
{{"title": "...", "parts": [{{"id": "...", "question": "...", "where": "remote", "needs": "", "tier": "hard"}}]}}
{PLAN_END}
"""


def part_prompt(question: str, part: Part, *, context_note: str = "", mcp_names: list[str] = ()) -> str:
    """One remote part's brief: the question, its sub-question, the rules and the answer format.

    ``Part id:`` is on its own line so the log and the retry can be matched to the part.
    """
    note = f"\n{context_note.strip()}\n" if context_note.strip() else ""
    return f"""You are one agent in an automated parallel investigation ("route ultra --research").
Part id: {part.id}
{READ_ONLY_RULES}
{note}
# The original question (for context; other agents cover the other parts)

{question.strip()}

# Your part

{part.question.strip()}

# Rules

- Read only: use web search and fetch, your shell for reading and computing, {_mcp_note(list(mcp_names))},
  and the files in your working directory. Write no files: your answer goes in your output.
- Cite every claim with a source id from your "sources" list; a claim without a source is a gap.
- Say what you could not find under "gaps" instead of guessing.
- Finish with ONE JSON object between the markers, as the last thing you print, nothing after it:

{ANSWER_BEGIN}
{{"answer": "markdown, citing [S1] style ids from sources",
 "sources": [{{"id": "S1", "ref": "url | path:line | record id", "note": "what it shows"}}],
 "confidence": "high|medium|low",
 "gaps": ["what is still unknown"]}}
{ANSWER_END}
"""


def synthesis_prompt(question: str, answered: dict[str, tuple[Part, Answer]], session_parts: list[Part]) -> str:
    """Everything the parts found, for one report that cites as [part-id:S1]."""
    blocks = []
    for part_id, (part, answer) in answered.items():
        sources = "\n".join(f"  - [{part_id}:{src.get('id')}] {src.get('ref')}: {src.get('note', '')}"
                            for src in answer.sources) or "  (none)"
        gaps = "\n".join(f"  - {gap}" for gap in answer.gaps) or "  (none)"
        blocks.append(f"## Part {part_id}: {part.question.strip()}\n\nConfidence: {answer.confidence}\n\n"
                      f"{answer.answer.strip()}\n\nSources:\n{sources}\n\nGaps:\n{gaps}")
    session = "\n".join(f"- {part.id}: {part.question.strip()} (needs: {part.needs})" for part in session_parts) \
        or "(none)"
    return f"""You are the synthesizer for an automated parallel investigation ("route ultra --research").
{READ_ONLY_RULES} Do not research further: write from the parts below only.

# The question

{question.strip()}

# What the parts found

{chr(10).join(blocks) if blocks else '(no part answered)'}

# Parts left for the user's own session (do not answer them; the report may mention they are pending)

{session}

# Output

Write the report in markdown: a short direct answer first (two to five sentences), then sections as
the material needs, then disagreements and gaps. Cite every claim as [part-id:S1] using exactly the
source ids listed above; never invent one. Print ONLY the report between the markers, as the last
thing, nothing after it:

{REPORT_BEGIN}
...markdown...
{REPORT_END}
"""


# -- parsing and lint -----------------------------------------------------------

def _string(raw: dict, key: str, where: str, error: type[ValueError], *, default: str | None = None) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise error(f"{where}: {key!r} must be a string")
    return value


def _part_from(raw: object, index: int) -> Part:
    if not isinstance(raw, dict):
        raise ResearchPlanError(f"part {index}: must be an object")
    where = f"part {raw['id']!r}" if isinstance(raw.get("id"), str) else f"part {index}"
    tier = raw.get("tier")
    return Part(id=_string(raw, "id", where, ResearchPlanError),
                question=_string(raw, "question", where, ResearchPlanError),
                where=_string(raw, "where", where, ResearchPlanError, default="remote"),
                needs=_string(raw, "needs", where, ResearchPlanError, default=""),
                tier=_string(raw, "tier", where, ResearchPlanError) if tier is not None else "moderate")


def parse_research_plan(text: str) -> ResearchPlan:
    """The last marked JSON object in the planner's output, checked for shape only.

    Content rules live in ``lint_research_plan`` so their problems can be
    reported together and handed back to the planner in one re-plan.
    """
    block = _marked_block(text or "", PLAN_BEGIN, PLAN_END, ResearchPlanError)
    if block is None:
        raise ResearchPlanError(f"no {PLAN_BEGIN} block in the planner output")
    try:
        raw = json.loads(block)
    except ValueError as exc:
        raise ResearchPlanError(f"plan is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ResearchPlanError("plan must be a JSON object")
    parts = raw.get("parts")
    if not isinstance(parts, list):
        raise ResearchPlanError("plan 'parts' must be a list")
    return ResearchPlan(title=_string(raw, "title", "plan", ResearchPlanError, default=""),
                        parts=[_part_from(part, index) for index, part in enumerate(parts)])


def lint_research_plan(plan: ResearchPlan, *, max_parts: int) -> list[str]:
    """Every reason the plan cannot run, so the planner can fix them all at once."""
    problems: list[str] = []
    if not plan.parts:
        problems.append("plan has no parts")
    elif len(plan.parts) > max_parts:
        problems.append(f"{len(plan.parts)} parts; at most {max_parts} are allowed")
    seen: set[str] = set()
    for part in plan.parts:
        label = f"part {part.id!r}"
        if not PART_ID.fullmatch(part.id):
            problems.append(f"{label}: id must match [a-z0-9][a-z0-9-]{{0,40}}")
        if part.id in seen:
            problems.append(f"{label}: duplicate id")
        seen.add(part.id)
        if not part.question.strip():
            problems.append(f"{label}: question must be non-empty")
        if part.where not in WHERE:
            problems.append(f"{label}: where must be one of {', '.join(WHERE)}, not {part.where!r}")
        elif part.where == "session" and not part.needs.strip():
            problems.append(f"{label}: a session part must say in 'needs' which connector or local resource it needs")
        if part.tier not in TIERS:
            problems.append(f"{label}: tier must be one of {', '.join(TIERS)}, not {part.tier!r}")
    return problems


def parse_answer(text: str) -> Answer:
    """The last marked JSON object in a part's output, with every field checked."""
    block = _marked_block(text or "", ANSWER_BEGIN, ANSWER_END, ResearchAnswerError)
    if block is None:
        raise ResearchAnswerError(f"no {ANSWER_BEGIN} block in the output")
    try:
        raw = json.loads(block)
    except ValueError as exc:
        raise ResearchAnswerError(f"answer is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ResearchAnswerError("answer must be a JSON object")
    answer = raw.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise ResearchAnswerError("'answer' must be a non-empty string")
    missing = [key for key in ("sources", "confidence", "gaps") if key not in raw]
    if missing:
        raise ResearchAnswerError("answer is missing required field(s): " + ", ".join(missing))
    sources = raw["sources"]
    if not isinstance(sources, list):
        raise ResearchAnswerError("'sources' must be a list")
    for index, source in enumerate(sources):
        if not isinstance(source, dict) or not isinstance(source.get("id"), str) \
                or not isinstance(source.get("ref"), str):
            raise ResearchAnswerError(f"source {index}: must be an object with string 'id' and 'ref'")
        if "note" in source and not isinstance(source["note"], str):
            raise ResearchAnswerError(f"source {source['id']!r}: 'note' must be a string")
    confidence = raw["confidence"]
    if confidence not in CONFIDENCE:
        raise ResearchAnswerError(f"'confidence' must be one of {', '.join(CONFIDENCE)}")
    gaps = raw["gaps"]
    if not isinstance(gaps, list) or not all(isinstance(gap, str) for gap in gaps):
        raise ResearchAnswerError("'gaps' must be a list of strings")
    return Answer(answer=answer, sources=[dict(source) for source in sources], confidence=confidence,
                  gaps=list(gaps))


# -- read-only command lines -----------------------------------------------------

def _join(tokens: list[str]) -> str:
    """shlex.join, except ``{task}`` stays a bare placeholder for ``cli._dispatch`` to substitute."""
    return " ".join(tok if tok == "{task}" else shlex.quote(tok) for tok in tokens)


def research_template(arm: str, pools: dict[str, Pool], *, host: str = "claude",
                      mcp_config: Path | None = None) -> str:
    """The arm's dispatch template made read-only, with only the allowlisted MCP servers.

    Claude arms get ``--strict-mcp-config --mcp-config <file> --disallowedTools
    Edit Write NotebookEdit`` right after the program; both options are
    variadic, so the template's own next token must be an option, never
    ``{task}`` (which would be swallowed as a tool name). Codex arms get
    ``--sandbox read-only --skip-git-repo-check``. Anything else cannot be
    made read-only here.
    """
    tokens = shlex.split(dispatch_template(arm, pools, host=host))
    if not tokens:
        raise ValueError(f"empty dispatch template for arm: {arm}")
    program = os.path.basename(tokens[0])
    if program in {"claude", "claude-work"}:
        rest: list[str] = []
        index = 1
        while index < len(tokens):
            tok = tokens[index]
            if tok == "--strict-mcp-config":
                index += 1
            elif tok == "--mcp-config":
                index += 2
            else:
                rest.append(tok)
                index += 1
        if not rest or not rest[0].startswith("-"):
            raise ValueError(f"{arm}: the token after the inserted --mcp-config/--disallowedTools block must be "
                             f"an option of the template, not {rest[0] if rest else 'the end'!r}")
        value = str(mcp_config) if mcp_config is not None else '{"mcpServers":{}}'
        block = ["--strict-mcp-config", "--mcp-config", value, "--disallowedTools", "Edit", "Write", "NotebookEdit"]
        return _join([tokens[0], *block, *rest])
    if program == "codex":
        if "--sandbox" in tokens:
            at = tokens.index("--sandbox")
            if at + 1 < len(tokens) and tokens[at + 1] != "{task}":
                tokens[at + 1] = "read-only"
            else:
                tokens.insert(at + 1, "read-only")
        else:
            at = tokens.index("exec") + 1 if "exec" in tokens else 1
            tokens[at:at] = ["--sandbox", "read-only"]
        if CODEX_SKIP_GIT_CHECK not in tokens:
            # Research parts, the planner and the synthesizer run in fresh run
            # directories that are not git work trees; Codex refuses those
            # without this flag. The sandbox is read-only, so the check that the
            # flag skips (a writable agent outside version control) does not apply.
            tokens.insert(tokens.index("read-only") + 1, CODEX_SKIP_GIT_CHECK)
        return _join(tokens)
    raise ValueError(f"{arm}: cannot make {program!r} read-only; only claude, claude-work and codex are supported")


# -- MCP allowlist ---------------------------------------------------------------

def research_allowlist() -> tuple[bool, list[str]]:
    """(the allowlist file exists, the server names it lists) from ``research.toml``."""
    path = config_dir() / "research.toml"
    if not path.is_file():
        return False, []
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers", [])
    except (OSError, ValueError):
        return True, []
    if not isinstance(raw, list):
        return True, []
    return True, [name for name in raw if isinstance(name, str) and name.strip()]


def _claude_json_path() -> Path:
    return Path(os.environ.get("ROUTE_CLAUDE_JSON") or Path.home() / ".claude.json")


def research_mcp_config(run_dir: Path) -> tuple[Path | None, dict]:
    """Write the run's MCP config from the allowlist; (path or None, what the report should say).

    Only names listed in ``research.toml`` are copied out of ``~/.claude.json``,
    and the file is owner-only: server definitions carry tokens. Its contents
    are never printed or logged.
    """
    exists, names = research_allowlist()
    info = {"allowlist_exists": exists, "requested": list(names), "loaded": [], "missing": [],
            "path": str(config_dir() / "research.toml")}
    if not names:
        return None, info
    try:
        raw = json.loads(_claude_json_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        servers = {}
    chosen = {name: servers[name] for name in names if name in servers}
    info["loaded"] = list(chosen)
    info["missing"] = [name for name in names if name not in servers]
    path = run_dir / MCP_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": chosen}, handle)
    os.chmod(path, 0o600)
    return path, info


# -- dispatch --------------------------------------------------------------------

def _dispatch(run: ultra.Run, arm: str, pools: dict[str, Pool], host: str, prompt: str, cwd: Path,
              log_name: str, timeout: float, mcp_config: Path | None) -> tuple[int, str]:
    """Run one read-only model on a prompt in a directory, logging to the run dir; (exit code, output)."""
    log = run.dir / "logs" / log_name
    template = research_template(arm, pools, host=host, mcp_config=mcp_config)
    pid_box: list[int] = []

    def started(pid: int) -> None:
        with _LOCK:
            pid_box.append(pid)
            run.track(pid)

    try:
        rc = cli._dispatch(template, prompt, target=arm, cwd=cwd, log=log, timeout=timeout, on_start=started)
    finally:
        with _LOCK:
            for pid in pid_box:
                run.untrack(pid)
    try:
        return rc, log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rc, ""


def _supported(arm: str, pools: dict[str, Pool], host: str) -> bool:
    try:
        research_template(arm, pools, host=host, mcp_config=None)
    except ValueError:
        return False
    return True


def _worker_arm(pools: dict[str, Pool], quota: dict, *, tier: str, host: str, allow_lower: bool,
                exclude: set[str] = frozenset()) -> str | None:
    """The arm ``pick`` would choose for the part, skipping arms whose command cannot be made read-only.

    The first choice is ``pick``'s own; after it (or on retry) the remaining
    candidates are walked in their order with the same tier preference.
    """
    first = pick("worker", pools, quota, tier=tier, host=host, allow_lower=allow_lower) if not exclude else None
    if first is not None and first not in exclude and _supported(first, pools, host):
        return first
    skipped = set(exclude) | ({first} if first is not None else set())
    usable = [arm for arm in candidates(pools, quota, host=host, allow_lower=allow_lower, exclude_arms=skipped)
              if _supported(arm, pools, host)]
    if not usable:
        return None
    preferred = 3 if tier in {"hard", "open-ended"} else 2
    return next((arm for arm in usable if model_rank(pools[arm]) == preferred), usable[0])


# -- plan ------------------------------------------------------------------------

def _plan_once(run: ultra.Run, planner: str, pools: dict[str, Pool], host: str, prompt: str, cwd: Path,
               max_parts: int, attempt: int, mcp_config: Path | None) -> tuple[ResearchPlan | None, list[str]]:
    rc, text = _dispatch(run, planner, pools, host, prompt, cwd, f"plan-{attempt}.log", PLANNER_TIMEOUT, mcp_config)
    if rc == 124:
        return None, ["planner timed out"]
    try:
        plan = parse_research_plan(text)
    except ResearchPlanError as exc:
        return None, [str(exc)]
    problems = lint_research_plan(plan, max_parts=max_parts)
    return (plan, []) if not problems else (None, problems)


def _plan(run: ultra.Run, planner: str, pools: dict[str, Pool], host: str, prompt: str, cwd: Path,
          max_parts: int, mcp_config: Path | None) -> tuple[ResearchPlan | None, list[str]]:
    """Plan, and on problems re-plan once with them appended; the planner sees its own mistakes."""
    run.step("planning", f"planner {planner} (attempt 1)")
    plan, problems = _plan_once(run, planner, pools, host, prompt, cwd, max_parts, 1, mcp_config)
    if plan is not None:
        return plan, []
    run.progress["plan_problems"] = list(problems)
    run.note("re-planning once: " + "; ".join(problems))
    run.step("planning", f"planner {planner} (attempt 2)")
    retry = prompt + ("\n\n# Problems with your previous plan (fix all of them)\n\n"
                      + "\n".join(f"- {problem}" for problem in problems) + "\n")
    plan, problems = _plan_once(run, planner, pools, host, retry, cwd, max_parts, 2, mcp_config)
    if plan is None:
        run.progress["plan_problems"].extend(problems)
    return plan, problems


def _print_plan(plan: ResearchPlan, arms: dict[str, str], held: list[dict], planner: str, json_out: bool) -> None:
    if json_out:
        print(json.dumps({"plan": plan.to_dict(), "arms": arms, "held": held, "planner": planner}, indent=2))
        return
    print(f"plan: {plan.title or '(untitled)'} ({len(plan.parts)} parts)")
    print("part\twhere\ttier\tarm\tneeds\tquestion")
    for part in plan.parts:
        arm = arms.get(part.id, "held" if part.where == "remote" else "-")
        print(f"{part.id}\t{part.where}\t{part.tier}\t{arm}\t{part.needs or '-'}\t{part.question}")
    for row in held:
        print(f"held: {row['id']}: {row['reason']}")


# -- parts -----------------------------------------------------------------------

def _git_porcelain(repo: Path) -> list[str] | None:
    """``git status --porcelain`` lines, or None when the directory is not a git work tree."""
    probe = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, errors="replace")
    if probe.returncode or probe.stdout.strip() != "true":
        return None
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, text=True, errors="replace")
    return [line for line in status.stdout.splitlines() if line.strip()]


def _run_part(run: ultra.Run, part: Part, arm: str, pools: dict[str, Pool], quota: dict, host: str,
              allow_lower: bool, question: str, context_note: str, mcp_names: list[str], cwd: Path,
              mcp_config: Path | None, cancel: threading.Event | None = None) -> dict:
    """One remote part on its arm, retried once on the next candidate; what happened, for progress.

    Once ``cancel`` is set the part stops after its current dispatch (which the
    run has already signalled) instead of retrying on another arm.
    """
    prompt = part_prompt(question, part, context_note=context_note, mcp_names=mcp_names)
    tried: list[str] = []
    failures: list[str] = []
    while arm is not None and not (cancel is not None and cancel.is_set()):
        tried.append(arm)
        with _LOCK:
            run.unit(part.id, arm=arm, state="running", attempts=list(tried))
        rc, text = _dispatch(run, arm, pools, host, prompt, cwd, f"part-{part.id}.log"
                             if len(tried) == 1 else f"part-{part.id}-{len(tried)}.log", PART_TIMEOUT, mcp_config)
        try:
            if rc == 124:
                raise ResearchAnswerError("timed out")
            answer = parse_answer(text)
        except ResearchAnswerError as exc:
            if cancel is not None and cancel.is_set():
                with _LOCK:
                    run.unit(part.id, arm=arm, state="interrupted", reason="run interrupted", attempts=list(tried))
                return {"id": part.id, "state": "interrupted", "arm": arm, "answer": None,
                        "reason": "run interrupted", "attempts": tried}
            failures.append(f"{arm}: {exc}" + (f" (exit {rc})" if rc not in (0, 124) else ""))
            arm = (_worker_arm(pools, quota, tier=part.tier, host=host, allow_lower=allow_lower, exclude=set(tried))
                   if len(tried) < 2 else None)
            continue
        atomic_write(run.dir / "parts" / f"{part.id}.json",
                     json.dumps({"id": part.id, "question": part.question, "arm": arm, "attempts": tried,
                                 **answer.to_dict()}, indent=2) + "\n")
        with _LOCK:
            run.unit(part.id, arm=arm, state="answered", reason=None, confidence=answer.confidence,
                     attempts=list(tried))
        return {"id": part.id, "state": "answered", "arm": arm, "answer": answer, "attempts": tried}
    reason = "; ".join(failures) or "no arm"
    with _LOCK:
        run.unit(part.id, arm=tried[-1] if tried else None, state="failed", reason=reason, attempts=list(tried))
    return {"id": part.id, "state": "failed", "arm": tried[-1] if tried else None, "answer": None,
            "reason": reason, "attempts": tried}


def _stop_parts(run: ultra.Run, cancel: threading.Event, executor: ThreadPoolExecutor, futures: list) -> None:
    """Stop every part, running or queued, and return only once their threads have ended.

    ``executor.shutdown(wait=True)`` alone would wait for the running delegates
    (up to their timeouts and retries); they lead their own sessions, so they
    are signalled with their groups, SIGTERM first and SIGKILL after a grace.
    """
    cancel.set()
    executor.shutdown(wait=False, cancel_futures=True)
    with _LOCK:
        pids = list(run.progress["pids"])
    for pid in pids:
        signal_tree(pid, signal.SIGTERM)
    _, pending = wait(futures, timeout=CANCEL_GRACE)
    if pending:
        with _LOCK:
            pids = list(run.progress["pids"])
        for pid in pids:
            signal_tree(pid, signal.SIGKILL)
    executor.shutdown(wait=True)


# -- report ----------------------------------------------------------------------

def fallback_report(question: str, parts: list[tuple[Part, Answer]]) -> str:
    """The report when no synthesizer delivered: the question, then every answer and its sources."""
    lines = [f"# {question.strip()}", "", "No synthesizer delivered a report; the parts' answers follow as found."]
    for part, answer in parts:
        lines += ["", f"## {part.id}: {part.question.strip()}", "", f"Confidence: {answer.confidence}", "",
                  answer.answer.strip()]
        if answer.sources:
            lines += ["", "Sources:"] + [f"- [{part.id}:{src.get('id')}] {src.get('ref')}: {src.get('note', '')}"
                                         for src in answer.sources]
        if answer.gaps:
            lines += ["", "Gaps:"] + [f"- {gap}" for gap in answer.gaps]
    if not parts:
        lines += ["", "No part answered."]
    return "\n".join(lines) + "\n"


def _sources_section(body: str, answered: dict[str, tuple[Part, Answer]]) -> list[str]:
    """Every cite in the body resolved (or flagged), then every source the body never cited."""
    index = {(part_id, src.get("id")): src for part_id, (_, answer) in answered.items() for src in answer.sources}
    lines = ["## Sources", ""]
    cited: list[tuple[str, str]] = []
    for match in CITE.finditer(body):
        key = (match.group(1), match.group(2))
        if key not in cited:
            cited.append(key)
    for part_id, src_id in cited:
        src = index.get((part_id, src_id))
        if src is None:
            lines.append(f"- [{part_id}:{src_id}] UNRESOLVED")
        else:
            lines.append(f"- [{part_id}:{src_id}] {src.get('ref')}: {src.get('note', '')}".rstrip(": "))
    uncited = [(key, src) for key, src in index.items() if key not in cited]
    if uncited:
        lines += ["", "Found by the parts but not cited above:"]
        lines += [f"- [{part_id}:{src_id}] {src.get('ref')}: {src.get('note', '')}".rstrip(": ")
                  for (part_id, src_id), src in uncited]
    if not cited and not uncited:
        lines.append("(no sources)")
    return lines


def _quota_table(usage_before: dict, usage_after: dict) -> dict:
    quota = {}
    for provider in sorted(set(usage_before) | set(usage_after)):
        before, after = usage_before.get(provider, {}), usage_after.get(provider, {})
        quota[provider] = {label: {"before": before.get(label), "after": after.get(label)}
                           for label in sorted(set(before) | set(after))}
    return quota


def _summary_md(summary: dict, body: str, answered: dict[str, tuple[Part, Answer]], session_parts: list[Part]) -> str:
    lines = [body.rstrip(), "", "## For your session", ""]
    if session_parts:
        lines += [f"- {part.id}: {part.question.strip()} (needs: {part.needs.strip()})" for part in session_parts]
    else:
        lines.append("Nothing: every part ran in the background.")
    lines += [""] + _sources_section(body, answered)
    lines += ["", "## Run", "", f"- Run: {summary['run_id']}",
              f"- Planner: {summary['planner'] or '-'}; synthesizer: {summary['synthesizer'] or 'none (deterministic report)'}"]
    arms = [f"{part['id']}: {part['arm']}" for part in summary["parts"] if part.get("arm")]
    lines.append("- Arm per part: " + ("; ".join(arms) if arms else "none"))
    failed = [f"{part['id']} ({part.get('reason')})" for part in summary["parts"] if part["state"] == "failed"]
    if failed:
        lines.append("- Failed parts: " + "; ".join(failed))
    if summary["held"]:
        lines.append("- Held parts: " + "; ".join(f"{row['id']} ({row['reason']})" for row in summary["held"]))
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
    if summary["notes"]:
        lines += ["- Notes:"] + [f"  - {note}" for note in summary["notes"][:8]]
    lines += [f"- Run directory: {summary['run_dir']}", f"- Exit: {summary['exit_code']}"]
    return "\n".join(lines) + "\n"


def _report(run: ultra.Run, question: str, plan: ResearchPlan | None, body: str,
            answered: dict[str, tuple[Part, Answer]], synthesizer: str | None, mcp_info: dict,
            porcelain: list[str], usage_before: dict, exit_code: int, json_out: bool) -> int:
    """Write summary.md and summary.json, print one of them, finish the run; the exit code."""
    usage_after = cli.fetch_usage()
    session_parts = plan.session if plan else []
    by_part = {part.id: part for part in plan.parts} if plan else {}
    rows = []
    for part_id, state in run.progress["units"].items():
        part = by_part.get(part_id)
        rows.append({"id": part_id, "state": state["state"], "arm": state.get("arm"), "where": state.get("where"),
                     "tier": state.get("tier"), "confidence": state.get("confidence"), "reason": state.get("reason"),
                     "question": part.question if part else None, "needs": part.needs if part else None,
                     "attempts": state.get("attempts", [])})
    summary = {
        "run_id": run.id, "question": question, "mode": "research", "title": plan.title if plan else None,
        "planner": run.progress.get("planner"), "synthesizer": synthesizer, "parts": rows,
        "held": run.progress.get("held", []), "plan_problems": run.progress.get("plan_problems", []),
        "mcp_servers": {k: v for k, v in mcp_info.items()}, "porcelain_changes": list(porcelain),
        "quota": _quota_table(usage_before, usage_after), "exit_code": exit_code, "run_dir": str(run.dir),
        "context": run.progress.get("context"), "notes": run.progress.get("notes", []),
    }
    text = _summary_md(summary, body, answered, session_parts)
    atomic_write(run.dir / "summary.md", text)
    atomic_write(run.dir / "summary.json", json.dumps(summary, indent=2) + "\n")
    run.finish("done" if exit_code == 0 else "failed", exit_code)
    print(json.dumps(summary, indent=2) if json_out else text, flush=True)
    return exit_code


def _give_up(run: ultra.Run, reason: str, question: str, plan: ResearchPlan | None, mcp_info: dict,
             usage_before: dict, json_out: bool) -> int:
    """Exit 2: nothing was researched, and the report says why."""
    run.note(reason)
    body = fallback_report(question, []).rstrip() + f"\n\nNot run: {reason}\n"
    return _report(run, question, plan, body, {}, None, mcp_info, [], usage_before, 2, json_out)


# -- the run ---------------------------------------------------------------------

def run_research(question: str, *, repo: str | Path | None = None, context: str = "brief",
                 session: str | None = None, max_parts: int = 6, max_parallel: int = 6, plan_only: bool = False,
                 allow_lower: bool = False, json_out: bool = False, host: str = "claude") -> int:
    """One whole research run; exit 0 when every remote part answered and the report was written,
    1 when a remote part failed or was held, 2 when it could not plan or nothing was usable."""
    question = question.strip()
    if not question:
        print("route ultra --research: empty question", file=sys.stderr)
        return 2
    if context not in ("brief", "digest"):
        print(f"route ultra: --context must be brief or digest, not {context}", file=sys.stderr)
        return 2
    repo_path = Path(repo).expanduser().resolve() if repo else None
    if repo_path is not None and not repo_path.is_dir():
        print(f"route ultra --research: --repo {repo_path} is not a directory", file=sys.stderr)
        return 2
    run = ultra.Run(ultra.new_run_id(question), question, repo_path or Path.cwd(), "", host)
    run.progress["mode"] = "research"
    try:
        run.dir.mkdir(parents=True, mode=0o700)
        (run.dir / "logs").mkdir(mode=0o700)
    except FileExistsError:
        print(f"route ultra: run {run.id} already exists at {run.dir}", file=sys.stderr)
        return 2
    run.save()
    print(f"ultra: run {run.id} at {run.dir}", flush=True)
    previous = _trap_sigterm()
    try:
        return _execute(run, question, repo_path, context, session, max_parts, max_parallel, plan_only,
                        allow_lower, json_out, host)
    except KeyboardInterrupt:
        run.finish("interrupted", 130)
        print("ultra: interrupted", file=sys.stderr)
        return 130
    finally:
        (run.dir / MCP_FILE).unlink(missing_ok=True)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _trap_sigterm():
    """Make SIGTERM (what ``route ultra cancel`` sends) behave like Ctrl-C; the handler it replaced.

    The default action would end the process at once, skipping the ``finally``
    that deletes the copied MCP server definitions and stops the parts. Only
    the main thread may install handlers; elsewhere nothing changes.
    """
    if threading.current_thread() is not threading.main_thread():
        return None

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    try:
        return signal.signal(signal.SIGTERM, interrupt)
    except (ValueError, OSError):
        return None


def _execute(run: ultra.Run, question: str, repo: Path | None, context: str, session: str | None, max_parts: int,
             max_parallel: int, plan_only: bool, allow_lower: bool, json_out: bool, host: str) -> int:
    pools = pools_for_host(cli.load_pools(), host)
    quota = cli.fetch_quota()
    usage_before = cli.fetch_usage()
    run.progress["headroom"] = dict(quota)
    context_note = ultra._context(run, context, session)
    mcp_config, mcp_info = research_mcp_config(run.dir)
    run.progress["mcp_servers"] = {"loaded": mcp_info["loaded"], "missing": mcp_info["missing"]}
    mcp_names = list(mcp_info["loaded"])
    work_dir = repo if repo is not None else run.dir

    planner = pick("planner", pools, quota, host=host, allow_lower=allow_lower)
    if planner is not None and not _supported(planner, pools, host):
        planner = next((arm for arm in candidates(pools, quota, host=host, allow_lower=allow_lower)
                        if _supported(arm, pools, host)), None)
    if planner is None:
        return _give_up(run, "no usable model has quota to plan (try --allow-lower-models)", question, None,
                        mcp_info, usage_before, json_out)
    run.progress["planner"] = planner
    prompt = research_plan_prompt(question, max_parts=max_parts, context_note=context_note, repo=repo,
                                  mcp_names=mcp_names)
    plan, problems = _plan(run, planner, pools, host, prompt, work_dir, max_parts, mcp_config)
    if plan is None:
        return _give_up(run, "could not plan: " + "; ".join(problems), question, None, mcp_info, usage_before,
                        json_out)

    run.step("assigning", "choosing an arm per part")
    arms: dict[str, str] = {}
    for part in plan.parts:
        if part.where == "session":
            run.unit(part.id, state="session", where="session", tier=part.tier, reason=f"needs: {part.needs}")
            continue
        arm = _worker_arm(pools, quota, tier=part.tier, host=host, allow_lower=allow_lower)
        if arm is None:
            reason = "no usable model has quota (or none can be made read-only)"
            run.progress["held"].append({"id": part.id, "reason": reason})
            run.unit(part.id, state="held", where="remote", tier=part.tier, reason=reason)
        else:
            arms[part.id] = arm
            run.unit(part.id, arm=arm, state="planned", where="remote", tier=part.tier)
    atomic_write(run.dir / "plan.json", json.dumps(
        {"plan": plan.to_dict(), "arms": arms, "held": run.progress["held"], "planner": planner}, indent=2) + "\n")
    if plan_only:
        _print_plan(plan, arms, run.progress["held"], planner, json_out)
        run.finish("planned", 0)
        return 0
    if plan.remote and not arms:
        return _give_up(run, "no usable model has quota for any part", question, plan, mcp_info, usage_before,
                        json_out)

    porcelain_before = _git_porcelain(repo) if repo is not None else None
    (run.dir / "parts").mkdir(exist_ok=True)
    run.step("running", f"{len(arms)} part(s), up to {max_parallel} in parallel")
    results: dict[str, dict] = {}
    cancel = threading.Event()
    executor = ThreadPoolExecutor(max_workers=max(1, max_parallel))
    futures: dict[str, object] = {}
    try:
        for part in plan.remote:
            if part.id not in arms:
                continue
            cwd = repo if repo is not None else run.dir / "parts" / part.id
            if repo is None:
                cwd.mkdir(parents=True, exist_ok=True)
            futures[part.id] = executor.submit(_run_part, run, part, arms[part.id], pools, quota, host, allow_lower,
                                               question, context_note, mcp_names, cwd, mcp_config, cancel)
        for part_id, future in futures.items():
            results[part_id] = future.result()
    except BaseException:
        _stop_parts(run, cancel, executor, list(futures.values()))
        raise
    executor.shutdown(wait=True)
    porcelain: list[str] = []
    if porcelain_before is not None:
        after = _git_porcelain(repo) or []
        porcelain = [line for line in after if line not in porcelain_before] + \
                    [f"(no longer) {line}" for line in porcelain_before if line not in after]
        if porcelain:
            run.note(f"repository {repo} changed during the run: {len(porcelain)} path(s)")

    answered = {part.id: (part, results[part.id]["answer"]) for part in plan.remote
                if part.id in results and results[part.id]["state"] == "answered"}
    synthesizer = pick("integrator", pools, quota, host=host, allow_lower=allow_lower)
    if synthesizer is not None and not _supported(synthesizer, pools, host):
        synthesizer = next((arm for arm in candidates(pools, quota, host=host, allow_lower=allow_lower)
                            if _supported(arm, pools, host)), None)
    body = None
    if synthesizer is not None and answered:
        run.step("synthesizing", f"by {synthesizer}")
        rc, text = _dispatch(run, synthesizer, pools, host, synthesis_prompt(question, answered, plan.session),
                             run.dir, "synthesis.log", SYNTHESIS_TIMEOUT, mcp_config)
        try:
            body = _marked_block(text, REPORT_BEGIN, REPORT_END, ResearchReportError) if rc == 0 else None
        except ResearchReportError:
            body = None
        if not body or not body.strip():
            run.note(f"synthesizer {synthesizer} delivered no report (exit {rc}); writing the parts as found")
            body = None
    if body is None:
        synthesizer = None
        body = fallback_report(question, [answered[key] for key in answered])
    run.progress["synthesizer"] = synthesizer
    exit_code = 0 if all(results.get(part.id, {}).get("state") == "answered" for part in plan.remote) \
        and not run.progress["held"] else 1
    return _report(run, question, plan, body, answered, synthesizer, mcp_info, porcelain, usage_before,
                   exit_code, json_out)
