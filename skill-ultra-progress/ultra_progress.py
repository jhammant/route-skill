#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich>=13"]
# ///
"""Live view of an /ultra run, laid out like an ultracode workflow: a one-line header (run, state,
agents, tokens, elapsed), then one group per phase (Plan, Build, Review, Fix, Integrate) with a
row per agent (model, time, output tokens, tool calls, what a working agent is doing now, and the
sub-agents it spawned), a line per unit with its stage strip, a narrator log, the latest tool calls
and the agents' channel. Team and research runs (`route ultra --team`, `--research`) show their
analysts or researchers, verifiers, critic rounds and synthesizer in the same phases.

Read-only. Sources: the run's progress.json and logs, its swarm's status.json and
channel.jsonl, and the agents' own transcripts (Claude Code under ~/.claude/projects,
Codex under ~/.codex/sessions). Nothing is written.

  ultra-progress              live view of the latest run (snapshot when not a terminal)
  ultra-progress RUN          a given run (id or unique prefix)
  ultra-progress --once       one snapshot, even in a terminal
  ultra-progress --json       the same data as JSON (totals, phases, log, units, ...)
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

RUN_STAGES = [("plan", "Plan"), ("assign", "Assign"), ("build", "Build"), ("review", "Review"),
              ("integrate", "Integrate"), ("settle", "Settle"), ("done", "Done")]
RUN_STATE_STAGE = {"starting": "plan", "planning": "plan", "assigning": "assign", "running": "build",
                   "reviewing": "review", "fixing": "review", "integrating": "integrate", "settling": "settle",
                   "analysing": "build", "cross-examining": "review", "critic": "review", "synthesizing": "integrate"}
FINISHED = {"done", "failed", "planned", "cancelled", "interrupted"}
LANE = ["build", "check", "review", "fix", "merge"]
# The workflow view's phases, in run order, and the phase each run state is in (5 = past them all).
PHASES = ["Plan", "Build", "Review", "Fix", "Integrate"]
RUN_PHASE = {"starting": 0, "planning": 0, "assigning": 1, "running": 1, "reviewing": 2, "fixing": 3,
             "integrating": 4, "settling": 5, "done": 5}
# Unit states a unit only reaches once its worker has built it and its check has passed.
BUILT = {"accepted", "review failed", "fixing", "fixed", "merged", "unmerged", "conflict", "resolving"}
GLYPH = {"done": "✓", "active": "●", "pending": "○", "queued": "○", "failed": "✗", "skipped": "·", "held": "⏸"}
STYLE = {"done": "green", "active": "bold yellow", "pending": "grey50", "queued": "grey50", "failed": "bold red",
         "skipped": "grey35", "held": "magenta"}
# `route ultra --team` and `--research` runs (progress.json "mode") build nothing: a team of analysts
# (cross-examined by verifiers, with a critic adding analysts) or a set of research parts, then one
# synthesizer writes the report. They use the same five phases: Build holds the analysts or parts,
# Review the verifiers and critic rounds (a team only) and Integrate the synthesizer.
ANALYSIS_MODES = {
    "team": {"unit": "analyst", "worker": "analyst", "stage": "analyse", "delivered": "analysed",
             "marker": "Analyst id", "dir": "analysts", "log": "analyst", "uses": PHASES},
    "research": {"unit": "part", "worker": "researcher", "stage": "research", "delivered": "answered",
                 "marker": "Part id", "dir": "parts", "log": "part", "uses": ["Plan", "Build", "Integrate"]},
}
ANALYSIS_PHASE = {"starting": 0, "planning": 0, "assigning": 1, "running": 1, "analysing": 1, "cross-examining": 2,
                  "critic": 2, "fixing": 3, "synthesizing": 4, "settling": 5, "done": 5}
VERDICTS = ("upheld", "contested", "refuted", "uncertain")
FOLD = 16                 # a phase with more agent rows than this folds its finished ones, a line per role
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ACTIVE_SECS = 90          # a transcript written to this recently counts as a live agent
MODELS = {"claude-work-fable": "fable · work", "claude-work": "opus · work", "claude-work-sonnet": "sonnet · work",
          "claude": "claude", "codex": "codex", "kimi": "kimi", "deepseek": "deepseek"}


# ------------------------------------------------------------------ helpers

def state_dir() -> Path:
    override = os.environ.get("ROUTE_STATE_DIR")
    return Path(override).expanduser() if override else Path.home() / ".local/state/route"


def find_run(run: str | None) -> Path:
    root = state_dir() / "ultra"
    runs = [p for p in root.iterdir() if (p / "progress.json").is_file()] if root.is_dir() else []
    if run:
        hits = [p for p in runs if p.name == run] or [p for p in runs if p.name.startswith(run)]
        if len(hits) != 1:
            raise SystemExit(f"ultra-progress: {'no' if not hits else 'more than one'} run matches {run!r}")
        return hits[0]
    if not runs:
        raise SystemExit(f"ultra-progress: no /ultra runs under {root}")
    return max(runs, key=lambda p: (p / "progress.json").stat().st_mtime)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def parse_time(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def now() -> datetime:
    return datetime.now(timezone.utc)


def span(start, end=None) -> str:
    start = parse_time(start) if not isinstance(start, datetime) else start
    if not start:
        return ""
    end = (parse_time(end) if not isinstance(end, datetime) else end) or now()
    secs = max(0, int((end - start).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {secs % 3600 // 60:02d}m"


def tokens(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def model(arm: str | None) -> str:
    return MODELS.get(arm or "", arm or "?")


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def short_tool(name: str) -> str:
    """mcp__amail__amail_inbox -> amail_inbox."""
    return name.split("__", 2)[-1] if name.startswith("mcp__") else name


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def project_dir(cwd: Path) -> Path:
    """Claude Code keeps a session's transcript under ~/.claude/projects/<cwd with / and . as ->."""
    return Path.home() / ".claude/projects" / re.sub(r"[/.]", "-", str(cwd))


# ------------------------------------------------------------------ transcripts

class Transcript:
    """Incrementally parsed agent transcript (Claude Code or Codex JSONL)."""

    def __init__(self, path: Path, kind: str):
        self.path, self.kind = path, kind
        self.offset = 0
        self.first_ts = self.last_ts = None
        self.tool_calls = 0
        self.out_tokens: dict[str, int] = {}
        self.events: list[tuple[datetime, str, str]] = []
        self.prompt = ""

    def refresh(self) -> "Transcript":
        try:
            size = self.path.stat().st_size
        except OSError:
            return self
        if size <= self.offset:
            return self
        with self.path.open("rb") as f:
            f.seek(self.offset)
            chunk = f.read(size - self.offset)
        end = chunk.rfind(b"\n") + 1
        self.offset += end
        for line in chunk[:end].splitlines():
            try:
                self._line(json.loads(line))
            except (ValueError, TypeError, AttributeError):
                continue
        self.events = self.events[-40:]
        return self

    def _line(self, d: dict) -> None:
        ts = parse_time(d.get("timestamp"))
        if ts:
            self.first_ts = self.first_ts or ts
            self.last_ts = ts
        if self.kind == "codex":
            p = d.get("payload") or {}
            if d.get("type") == "response_item" and p.get("type") in ("function_call", "local_shell_call", "custom_tool_call"):
                self.tool_calls += 1
                self.events.append((ts or now(), p.get("name") or "shell", _codex_desc(p)))
            if d.get("type") == "event_msg" and p.get("type") == "token_count":
                info = p.get("info") or {}
                total = (info.get("total_token_usage") or {}).get("output_tokens")
                if total:
                    self.out_tokens["total"] = total
            return
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        content = msg.get("content")
        if d.get("type") == "user" and not self.prompt:
            text = content if isinstance(content, str) else next((c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"), "")
            self.prompt = " ".join(str(text).split())[:200]
        if d.get("type") == "assistant":
            usage = msg.get("usage") or {}
            if msg.get("id") and usage.get("output_tokens"):
                self.out_tokens[msg["id"]] = max(self.out_tokens.get(msg["id"], 0), usage["output_tokens"])
            for c in content or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    self.tool_calls += 1
                    inp = c.get("input") or {}
                    desc = inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("query") or inp.get("prompt") or ""
                    self.events.append((ts or now(), c.get("name") or "tool", " ".join(str(desc).split())))

    @property
    def tokens(self) -> int:
        return sum(self.out_tokens.values())

    def recent(self) -> bool:
        return bool(self.last_ts) and (now() - self.last_ts).total_seconds() < ACTIVE_SECS


_JS_CMD = re.compile(r"""\bcmd\s*:\s*(["'`])((?:\\.|(?!\1).)*)\1""", re.S)


def _codex_desc(p: dict) -> str:
    raw = p.get("arguments") or p.get("input") or p.get("action") or ""
    # Codex's exec tool wraps shell calls in JavaScript: tools.exec_command({cmd: "..."}).
    text = raw if isinstance(raw, str) else json.dumps(raw)
    cmds = [m.group(2).encode().decode("unicode_escape", errors="replace") for m in _JS_CMD.finditer(text)]
    if cmds:
        return " ; ".join(" ".join(c.split()) for c in cmds)
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return " ".join(str(raw).split())
    cmd = (args or {}).get("command") if isinstance(args, dict) else None
    if isinstance(cmd, list):
        cmd = " ".join(cmd[2:] if cmd[:2] in (["bash", "-lc"], ["zsh", "-lc"]) else cmd)
    return " ".join(str(cmd or args).split())


_CACHE: dict[Path, Transcript] = {}
_CODEX_CWD: dict[Path, str] = {}


def transcript(path: Path, kind: str) -> Transcript:
    t = _CACHE.get(path)
    if t is None:
        t = _CACHE[path] = Transcript(path, kind)
    return t.refresh()


def claude_sessions(cwd: Path) -> list[Transcript]:
    """Main sessions for a working directory, oldest first (worker, then any fixers)."""
    d = project_dir(cwd)
    found = [transcript(p, "claude") for p in d.glob("*.jsonl")] if d.is_dir() else []
    return sorted((t for t in found if t.first_ts), key=lambda t: t.first_ts)


def sub_agents(session: Transcript) -> list[Transcript]:
    d = session.path.with_suffix("") / "subagents"
    found = [transcript(p, "claude") for p in d.glob("*.jsonl")] if d.is_dir() else []
    return sorted((t for t in found if t.first_ts), key=lambda t: t.first_ts)


def codex_sessions(cwd: Path, since: datetime | None) -> list[Transcript]:
    root = Path.home() / ".codex/sessions"
    if not root.is_dir():
        return []
    day = (since or now()) - timedelta(days=1)
    out = []
    for n in range(4):
        folder = root / (day + timedelta(days=n)).strftime("%Y/%m/%d")
        for p in folder.glob("rollout-*.jsonl") if folder.is_dir() else []:
            if p not in _CODEX_CWD:
                try:
                    with p.open() as f:
                        _CODEX_CWD[p] = (json.loads(f.readline()).get("payload") or {}).get("cwd") or ""
                except (OSError, ValueError):
                    _CODEX_CWD[p] = ""
            if _CODEX_CWD[p] == str(cwd):
                out.append(transcript(p, "codex"))
    return sorted((t for t in out if t.first_ts), key=lambda t: t.first_ts)


def claude_sessions_since(cwd: Path, since: datetime | None) -> list[Transcript]:
    """Main sessions for a directory other agents (or the user) also work in, written since ``since``.

    Older transcripts are not parsed at all: a repository can hold hundreds of them.
    """
    d = project_dir(cwd)
    if not d.is_dir():
        return []
    floor = since.timestamp() - 60 if since else 0
    found = []
    for p in d.glob("*.jsonl"):
        try:
            if p.stat().st_mtime >= floor:
                found.append(transcript(p, "claude"))
        except OSError:
            continue
    return sorted((t for t in found if t.first_ts), key=lambda t: t.first_ts)


class Merged:
    """One agent's several sessions (a try, its repair, a retry on another model) read as one."""

    kind = "merged"

    def __init__(self, parts: list[Transcript]):
        self.parts = parts

    first_ts = property(lambda self: min(t.first_ts for t in self.parts))
    last_ts = property(lambda self: max(t.last_ts for t in self.parts))
    tokens = property(lambda self: sum(t.tokens for t in self.parts))
    tool_calls = property(lambda self: sum(t.tool_calls for t in self.parts))
    prompt = property(lambda self: self.parts[0].prompt)
    events = property(lambda self: sorted((e for t in self.parts for e in t.events), key=lambda e: e[0])[-40:])

    def recent(self) -> bool:
        return any(t.recent() for t in self.parts)


# ------------------------------------------------------------------ model

def run_stages(progress: dict) -> list[tuple[str, str, str]]:
    state = progress.get("state", "starting")
    keys = [k for k, _ in RUN_STAGES]
    if state == "done":
        return [(k, label, "done") for k, label in RUN_STAGES]
    if state in FINISHED:
        reached = [k for k in keys if k != "done" and _stage_seen(progress, k)]
        idx = keys.index(reached[-1] if reached else "plan")
        return [(k, label, "done" if i < idx else "failed" if i == idx else "skipped") for i, (k, label) in enumerate(RUN_STAGES)]
    idx = keys.index(RUN_STATE_STAGE.get(state, "plan"))
    return [(k, label, "done" if i < idx else "active" if i == idx else "pending") for i, (k, label) in enumerate(RUN_STAGES)]


def _stage_seen(progress: dict, key: str) -> bool:
    units = progress.get("units") or {}
    states = {u.get("state") for u in units.values()}
    if key in ("plan", "assign"):
        return bool(units)
    if key == "build":
        return bool(progress.get("swarm_dir")) or (state_dir() / "swarms" / progress.get("run_id", "-")).is_dir()
    if key == "review":
        return bool(states & {"accepted", "review failed", "fixing", "fixed", "merged", "unmerged", "conflict"})
    if key == "integrate":
        return bool(progress.get("integration")) or bool(states & {"merged", "unmerged", "conflict", "resolving"})
    if key == "settle":
        return any(u.get("settled") is not None for u in units.values())
    return False


def unit_lane(unit: dict, swarm_unit: dict, progress: dict) -> tuple[list[tuple[str, str]], str]:
    """Five (stage, state) cells plus a short note."""
    cells = {k: "pending" for k in LANE}
    note = ""
    pstate = unit.get("state", "planned")
    sstate = (swarm_unit or {}).get("state")
    run_state = progress.get("state")
    step = progress.get("step") or ""
    reason = unit.get("reason") or ""

    def upto(stage, state="done"):
        for k in LANE[:LANE.index(stage)]:
            cells[k] = "done"
        cells[stage] = state

    if pstate == "held":
        return [(k, "held") for k in LANE], f"held: {reason}" if reason else "held"
    if pstate == "planned":
        if sstate == "checking":
            upto("check", "active")
            note = "running its check"
        elif sstate == "running":
            cells["build"] = "active"
            note = f"building for {span(swarm_unit.get('started_at'))}"
        elif sstate == "accepted":
            upto("review", "pending")
            note = "check passed"
        elif sstate == "failed":
            upto("check", "failed")
            note = "check failed"
        else:
            note = "queued: its model's pool is full" if run_state == "running" else ""
    elif pstate == "accepted":
        mine = run_state == "reviewing" and step.startswith(unit["id"] + " ")
        review = unit.get("review") or {}
        confirmed = review.get("confirmed") or []
        upto("review", "active" if mine else "pending")
        if mine:
            note = "under review by " + step.split(" by ", 1)[-1]
        elif review.get("skipped"):
            cells["review"], cells["fix"] = "skipped", "skipped"
            note = f"review skipped: {review['skipped']}"
        elif review.get("reviewer") and confirmed:
            cells["review"] = "done"
            first = confirmed[0].get("title", "") if isinstance(confirmed[0], dict) else str(confirmed[0])
            note = f"{len(confirmed)} confirmed bug(s) to fix: {first}"
        elif review.get("reviewer") or run_state in ("integrating", "settling") or run_state in FINISHED:
            cells["review"], cells["fix"] = "done", "skipped"
            note = "review clean" + (f" ({review['findings']} finding(s), none confirmed)" if review.get("findings") else "")
        else:
            note = "check passed, waiting for review"
    elif pstate == "review failed":
        upto("review", "failed")
        note = reason or "review failed"
    elif pstate == "fixing":
        upto("fix", "active")
        note = f"fix pass {unit.get('fix_passes', 1)}"
    elif pstate == "fixed":
        upto("merge", "pending")
        note = f"fixed after {unit.get('fix_passes', 1)} pass(es)"
    elif pstate == "failed":
        if unit.get("fix_passes"):
            upto("fix", "failed")
        elif sstate == "failed":
            upto("check", "failed")
        else:
            upto("build", "failed")
        note = reason or "failed"
    elif pstate == "resolving":
        upto("merge", "active")
        note = "resolving merge conflicts"
    elif pstate == "merged":
        upto("merge", "done")
        if not unit.get("fix_passes"):
            cells["fix"] = "skipped"
        note = "merged"
    elif pstate in ("unmerged", "conflict"):
        upto("merge", "failed")
        note = f"{pstate}: {reason}" if reason else pstate
    if run_state in FINISHED - {"done"}:
        for k, v in cells.items():
            cells[k] = "failed" if v == "active" else "skipped" if v == "pending" else v
    return [(k, cells[k]) for k in LANE], note



def _log_done(path: Path | None) -> bool:
    try:
        return bool(path) and path.stat().st_size > 0
    except OSError:
        return False


def _log_end(path: Path | None) -> datetime | None:
    """When a finished agent's log was last written: its end time when no transcript was found."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) if _log_done(path) else None
    except OSError:
        return None


def _log_start(path: Path | None) -> datetime | None:
    """When an agent's log was created (route opens it as the agent starts), where the filesystem says."""
    try:
        born = getattr(path.stat(), "st_birthtime", None) if path else None
    except OSError:
        return None
    return datetime.fromtimestamp(born, timezone.utc) if born else None


def _agent(role: str, arm: str | None, sessions: list[Transcript], log: Path | None, live_pid=None) -> dict:
    main = sessions[0] if sessions else None
    done = _log_done(log)
    alive = (live_pid is not None and pid_alive(live_pid)) or (main is not None and main.recent() and not done)
    state = "done" if done else "active" if alive else "pending" if main is None else "done"
    subs = sub_agents(main) if main and main.kind == "claude" else []
    return {"role": role, "arm": arm, "state": state, "t": main, "subs": subs, "log": log}


def check_summary(log: Path) -> str:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")[-3000:]
    except OSError:
        return ""
    hits = re.findall(r"(\d+ passed[^\n]*?in [\d.]+s)", text)
    return hits[-1] if hits else ""


def _family(arm: str | None) -> str:
    """Where an arm's transcripts live: Codex rollouts, or Claude Code projects."""
    return "codex" if arm == "codex" else "claude"


def _attempts(logs: Path, prefix: str) -> list[int]:
    """Attempt numbers of ``<prefix>-<n>.log``; exact, so unit 'a' never claims unit 'a-b''s logs."""
    pattern = re.compile(re.escape(prefix) + r"-(\d+)\.log")
    return sorted(int(m.group(1)) for p in logs.glob(f"{prefix}-*.log") if (m := pattern.fullmatch(p.name)))


def _review_tries(uid: str, review: dict, notes: list) -> tuple[list[str | None], list[tuple[str, str]]]:
    """The arms that reviewed a unit, in attempt order, and (arm, why) for each that failed to deliver.

    route records a review's ``attempts`` only once it has delivered or every reviewer has
    failed; until then the failed attempts are known from the run's notes alone
    (``review of <unit> by <arm> failed: <why>``).
    """
    pattern = re.compile(rf"review of {re.escape(uid)} by (\S+) failed: (.*)", re.S)
    failures = [(m.group(1), " ".join(m.group(2).split())) for note in notes if (m := pattern.fullmatch(str(note)))]
    if not failures:
        # A review that failed outright keeps its reasons as "<arm>: <why>".
        failures = [(arm, " ".join(why.split())) for arm, _, why in
                    (str(f).partition(": ") for f in review.get("failed") or [])]
    tried = list(review.get("attempts") or [arm for arm, _ in failures]
                 + ([review["reviewer"]] if review.get("reviewer") else []))
    return tried, failures


def collect(run_dir: Path) -> dict:
    progress = read_json(run_dir / "progress.json")
    if progress.get("mode") in ANALYSIS_MODES:
        return collect_analysis(run_dir, progress)
    swarm_dir = Path(progress["swarm_dir"]) if progress.get("swarm_dir") else state_dir() / "swarms" / run_dir.name
    swarm = read_json(swarm_dir / "status.json")
    swarm_units = {u.get("id"): u for u in swarm.get("units") or []}
    logs = run_dir / "logs"
    started = parse_time(progress.get("started_at"))
    step = progress.get("step") or ""
    run_state = progress.get("state")

    def sessions(cwd: Path, arm: str | None) -> list[Transcript]:
        return codex_sessions(cwd, started) if arm == "codex" else claude_sessions(cwd)

    # Resolvers, then the integrator, take turns in the integration worktree: hand out its sessions in order.
    in_integration: dict[str, list[Transcript]] = {}

    def next_integration_session(arm: str) -> list[Transcript]:
        found = in_integration.setdefault(_family(arm), list(sessions(run_dir / "integration", arm)))
        return [found.pop(0)] if found else []

    planners = []
    for log in sorted(logs.glob("plan-*.log")) or ([logs / "plan-1.log"] if run_state in ("starting", "planning") else []):
        n = log.stem.split("-")[-1]
        arm = progress.get("planner")
        planners.append(_agent(f"planner #{n}", arm, sessions(run_dir / f"plan-{n}", arm), log))
    units = []
    for uid, unit in (progress.get("units") or {}).items():
        su = swarm_units.get(uid, {})
        cells, note = unit_lane(unit, su, progress)
        agents = []
        wt = Path(su["worktree"]) if su.get("worktree") else swarm_dir / "wt" / uid
        arm = unit.get("arm") or su.get("arm")
        wt_sessions = sessions(wt, arm)
        worker = _agent("worker", arm, wt_sessions[:1], Path(su["log"]) if su.get("log") else swarm_dir / "logs" / f"{uid}.log",
                        su.get("pid") if su.get("state") in ("running", "checking") else None)
        if su.get("state") in ("running", "checking", None) and unit.get("state") == "planned" and not wt_sessions:
            worker["state"] = "active" if su.get("state") else "pending"
        agents.append(worker)
        review = unit.get("review") or {}
        reviewing_now = run_state == "reviewing" and step.startswith(uid + " by ")
        tried, failures = _review_tries(uid, review, progress.get("notes") or [])
        # Attempt n logs to review-<unit>-n.log; the current attempt counts before its log appears.
        attempts = max(_attempts(logs, f"review-{uid}") + [len(tried)])
        if reviewing_now:
            current = step.split(" by ", 1)[1].split(" ", 1)[0]
            if tried[-1:] != [current]:
                tried.append(current)
            attempts = max(attempts, len(tried))
            # The reviewer at work is the latest attempt; an earlier one route has not named yet is unknown.
            tried = tried[:-1] + [None] * (attempts - len(tried)) + [current]
        for n in range(1, attempts + 1):
            r_arm = tried[n - 1] if n - 1 < len(tried) else None
            live = reviewing_now and n == attempts
            found: list[Transcript] = []
            if r_arm:
                # Every attempt runs in the same throwaway directory: its family's sessions, oldest first.
                k = sum(_family(a) == _family(r_arm) for a in tried[:n - 1] if a)
                mine = sessions(run_dir / "review" / uid, r_arm)[k:]
                found = mine[-1:] if live else mine[:1]
            a = _agent("reviewer" + (f" #{n}" if n > 1 else ""), r_arm, found, logs / f"review-{uid}-{n}.log")
            a["state"] = "active" if live else "done"
            if n - 1 < len(failures) and failures[n - 1][0] == r_arm:
                a["failure"] = failures[n - 1][1]
            agents.append(a)
        if review.get("skipped"):
            agents.append({"role": "reviewer", "arm": None, "state": "skipped", "t": None, "subs": [], "note": review["skipped"]})
        fixers = list(unit.get("fixers") or [])
        for n, f_arm in enumerate(fixers, 1):
            later = [t for t in sessions(wt, f_arm) if not wt_sessions or t is not wt_sessions[0]]
            k = sum(_family(a) == _family(f_arm) for a in fixers[:n - 1])
            a = _agent(f"fixer #{n}", f_arm, later[k:k + 1], logs / f"fix-{uid}-{n}.log")
            a["state"] = "active" if unit.get("state") == "fixing" and n == len(fixers) else "done"
            agents.append(a)
        if unit.get("resolver"):
            a = _agent("resolver", unit["resolver"], next_integration_session(unit["resolver"]), logs / f"merge-{uid}.log")
            a["state"] = "active" if unit.get("state") == "resolving" else "done"
            agents.append(a)
        units.append({"id": uid, "unit": unit, "swarm": su, "cells": cells, "note": note, "agents": agents,
                      "check": check_summary(Path(su["log"])) if su.get("log") else ""})
    # The integrator: one pass at the goal check after the merges, when it failed.
    integration = progress.get("integration") or {}
    fixing_goal = re.search(r"fix pass by (\S+)", step) if run_state == "integrating" else None
    i_arm = (integration.get("fix") or {}).get("arm") or (fixing_goal.group(1) if fixing_goal else None)
    integrator = None
    if i_arm:
        integrator = _agent("integrator", i_arm, next_integration_session(i_arm), logs / "integration-fix.log")
        integrator["state"] = "active" if fixing_goal and not integration.get("fix") else "done"
    channel = []
    try:
        for line in (swarm_dir / "channel.jsonl").read_text(encoding="utf-8").splitlines()[-30:]:
            channel.append(json.loads(line))
    except (OSError, ValueError):
        pass
    m = {"progress": progress, "swarm": swarm, "planners": planners, "units": units, "integrator": integrator,
         "channel": channel, "stages": run_stages(progress), "finished": run_state in FINISHED, "run": run_dir}
    m["phases"] = build_phases(m)
    m["totals"] = totals(m)
    m["log"] = narrate(m)
    return m


# ------------------------------------------------------------------ team and research runs

def _named(prompt: str, label: str, ident: str) -> bool:
    """Whether a brief carries ``<label>: <ident>`` (``Analyst id: seo``) as a whole id."""
    return re.search(rf"{re.escape(label)}: {re.escape(ident)}(\s|$)", prompt or "") is not None


def _number(path: Path) -> int:
    tail = path.stem.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _analysis_unit(unit: dict, words: dict, run_state: str | None) -> tuple[str, str]:
    """An analyst's or research part's row state and note, from its unit state."""
    ustate, reason = unit.get("state", "planned"), unit.get("reason") or ""
    tries = len(unit.get("attempts") or [])
    if ustate == words["delivered"]:
        what = (plural(unit.get("findings") or 0, "finding") if "findings" in unit
                else f"confidence {unit['confidence']}" if unit.get("confidence") else "answered")
        return "done", what + (", repaired" if unit.get("repaired") else "") + (f", on try {tries}" if tries > 1 else "")
    if ustate == "running":
        return "active", f"try {tries}" if tries > 1 else ""
    if ustate in ("failed", "interrupted"):
        return "failed", reason or ustate
    if ustate == "held":
        return "skipped", "held: " + (reason or "no model had quota")
    if ustate == "session":
        return "skipped", "for your session"
    return "queued", "waiting for a free slot" if run_state in ("running", "analysing") else ""


def collect_analysis(run_dir: Path, progress: dict) -> dict:
    """A team or research run as the workflow view shows it.

    Its agents are found from the run's logs and progress.json: planners (plan-N.log),
    one analyst or researcher per unit (analyst-<id>[-2][-repair].log), the verifiers
    (verify-<claim>-<n>.log) and critic rounds (critic-<n>.log) of a team, the synthesizer
    (synthesis.log) and a team's fix build. Codex works in a directory of its own under the
    run; a Claude agent there too, or in the repository (or the run directory), where its
    brief tells its sessions apart.
    """
    mode = progress["mode"]
    words = ANALYSIS_MODES[mode]
    logs = run_dir / "logs"
    run_state, step = progress.get("state"), progress.get("step") or ""
    finished = run_state in FINISHED
    started, ended_at = parse_time(progress.get("started_at")), parse_time(progress.get("finished_at"))
    repo = Path(progress["repo"]) if progress.get("repo") else None
    shared = [run_dir] + ([repo] if repo and repo != run_dir else [])
    notes = [" ".join(str(n).split()) for n in progress.get("notes") or []]

    def find(arms, own: Path, brief, nth: int | None = 0) -> list[Transcript]:
        """An agent's sessions: Codex's in ``own``; Claude's in ``own``, else the ones in a shared
        directory, during this run, whose brief is this agent's (the ``nth`` of them, or all)."""
        known = {_family(a) for a in arms if a}    # none known: Codex's, else Claude's
        found = codex_sessions(own, started) if not known or "codex" in known else []
        if "claude" in known or not (known or found):
            mine = claude_sessions(own)
            if not mine:
                named = [t for cwd in shared for t in claude_sessions_since(cwd, started)
                         if brief(t.prompt) and (not ended_at or t.first_ts <= ended_at)]
                mine = named if nth is None else named[nth:nth + 1]
            found = found + mine
        return sorted(found, key=lambda t: t.first_ts)

    def agent(role: str, arm, found: list[Transcript], log: Path | None, state: str, note: str = "", **extra) -> dict:
        t = None if not found else found[0] if len(found) == 1 else Merged(found)
        subs = [s for x in found if x.kind == "claude" for s in sub_agents(x)]
        if not arm and found:
            arm = found[0].kind    # the family, when route has not said which arm it was
        return {"role": role, "arm": arm, "state": state, "t": t, "subs": subs, "log": log, "note": note,
                "start": None if found else _log_start(log), **extra}

    planners = []
    plan_logs = sorted(logs.glob("plan-*.log"), key=_number)
    if not plan_logs and run_state in ("starting", "planning") and not progress.get("layout"):
        plan_logs = [logs / "plan-1.log"]
    for log in plan_logs:
        n = _number(log)
        found = find([progress.get("planner")], run_dir / "plan" / f"attempt-{n}",
                     lambda s: s.startswith("You are the planner for an automated"), nth=n - 1)
        planners.append(agent(f"planner #{n}", progress.get("planner"), found, log, "done"))

    units = []
    for uid, unit in (progress.get("units") or {}).items():
        st, note = _analysis_unit(unit, words, run_state)
        tried = list(unit.get("attempts") or ([unit["arm"]] if unit.get("arm") else []))
        stem = f"{words['log']}-{uid}"
        names = [f"{stem}.log", f"{stem}-repair.log"]
        for k in range(2, len(tried) + 1):
            names += [f"{stem}-{k}.log", f"{stem}-{k}-repair.log"]
        written = [logs / name for name in names if (logs / name).is_file()]
        found = [] if st in ("queued", "skipped") else find(
            tried, run_dir / words["dir"] / uid, lambda s, uid=uid: _named(s, words["marker"], uid), nth=None)
        a = agent(words["worker"], unit.get("arm"), found, max(written, key=lambda p: p.stat().st_mtime, default=None), st,
                  note, start=None if found else _log_start(written[0] if written else None))
        cell = {"done": "done", "active": "active", "failed": "failed", "queued": "pending"}.get(
            st, "held" if unit.get("state") == "held" else "skipped")
        if finished:
            cell = "failed" if cell == "active" else "skipped" if cell == "pending" else cell
        units.append({"id": uid, "unit": unit, "swarm": {}, "cells": [(words["stage"], cell)], "note": note,
                      "agents": [a], "check": ""})

    others = []
    raw = read_json(run_dir / "verdicts.json")
    verdicts = {c.get("id"): c for c in raw if isinstance(c, dict)} if isinstance(raw, list) else {}
    for log in logs.glob("verify-*.log"):
        hit = re.fullmatch(r"verify-(.+)-(\d+)\.log", log.name)
        if not hit:
            continue
        claim, n = hit.group(1), int(hit.group(2))
        votes = (verdicts.get(claim) or {}).get("verdicts") or []
        vote = votes[n - 1] if n - 1 < len(votes) and isinstance(votes[n - 1], dict) else {}
        found = find([vote.get("arm")], run_dir / "verify" / f"{claim}-{n}",
                     lambda s, c=claim: _named(s, "Claim id", c), nth=n - 1)
        if _log_done(log):
            st, note = "done", vote.get("verdict") or ""
        elif run_state == "cross-examining":
            st, note = "active", ""
        else:
            st, note = "failed", "no verdict"
        others.append(agent("verifier", vote.get("arm"), found, log, st, note, phase="Review", n=n, who=claim,
                            label=f"{claim} · verifier #{n}"))

    critic_now = re.search(r"round (\d+) by (\S+)", step) if run_state == "critic" else None
    rounds = {_number(p): p for p in logs.glob("critic-*.log")}
    if critic_now:
        rounds.setdefault(int(critic_now.group(1)), logs / f"critic-{critic_now.group(1)}.log")
    for n, log in sorted(rounds.items()):
        live = bool(critic_now) and int(critic_now.group(1)) == n
        arm = critic_now.group(2) if live else None
        found = find([arm], run_dir / "critic" / f"round-{n}", lambda s: s.startswith("You are the critic"), nth=n - 1)
        said = [note.split(": ", 1)[1] for note in notes if note.startswith(f"critic round {n}: ")]
        if live:
            st, note = "active", "looking for what the team missed"
        elif any(s.startswith("no usable") for s in said):
            st, note = "failed", "no usable answer; no more rounds"
        else:
            st, note = ("done" if _log_done(log) else "failed"), "; ".join(said)
        others.append(agent("critic", arm, found, log, st, note, phase="Review", n=n, who="critic",
                            label=f"critic round {n}"))

    synth_now = re.match(r"by (\S+)", step) if run_state == "synthesizing" else None
    no_report = next((hit for note in notes if (hit := re.match(r"synthesizer (\S+) delivered no report", note))), None)
    synth_log = logs / "synthesis.log"
    if synth_now or synth_log.is_file():
        arm = synth_now.group(1) if synth_now else progress.get("synthesizer") or (no_report.group(1) if no_report else None)
        found = find([arm], run_dir / "synthesis", lambda s: s.startswith("You are the synthesizer"), nth=None)
        if synth_now:
            st, note = "active", "writing the report"
        elif no_report or not _log_done(synth_log):
            st, note = "failed", "no report; the findings are written as found"
        else:
            st, note = "done", "report written"
        others.append(agent("synthesizer", arm, found, synth_log, st, note, phase="Integrate", who="report",
                            label="synthesizer"))

    summary = read_json(run_dir / "summary.json")
    fix = summary.get("fix") if isinstance(summary, dict) and isinstance(summary.get("fix"), dict) else {}
    if run_state == "fixing" or fix.get("state") == "built":
        rc = fix.get("exit_code")
        st = "active" if run_state == "fixing" else "done" if rc == 0 else "failed"
        note = ("a build run for the code findings" if st == "active"
                else f"build run {fix.get('run_id') or ''} exit {rc}".replace("  ", " "))
        others.append(agent("build run", None, [], None, st, note, phase="Fix", who="fix", label="fix build"))

    m = {"progress": progress, "swarm": {}, "planners": planners, "units": units, "integrator": None, "channel": [],
         "stages": run_stages(progress), "finished": finished, "run": run_dir, "mode": mode, "others": others,
         "verdicts": verdicts}
    m["phases"] = build_phases(m)
    m["totals"] = totals(m)
    m["log"] = narrate(m)
    return m


# ------------------------------------------------------------------ the workflow view: phases, totals, narrator

STOPPED = "stopped when the run ended"


def _title(finding) -> str:
    return finding.get("title", "") if isinstance(finding, dict) else str(finding)


def _review_note(review: dict) -> str:
    confirmed = review.get("confirmed") or []
    if confirmed:
        return plural(len(confirmed), "confirmed bug")
    if review.get("findings"):
        return f"clean ({plural(review['findings'], 'finding')}, none confirmed)"
    return "clean" if review.get("reviewer") else ""


def _row(label: str, unit: str | None, role: str, arm: str | None, state: str, a: dict | None = None, note: str = "",
         start=None, end=None, n: int = 1, idle: bool = False) -> dict:
    """One agent as the workflow view shows it.

    ``a`` is what collect() found for it (transcript, sub-agents, log); ``start``/``end``
    stand in for the transcript's times when there is none (a worker's swarm times). Tokens
    and tool calls include the agent's sub-agents. ``idle``: the agent has finished but its
    step has not (route is re-running checks), so there is no "now".
    """
    a = a or {}
    started = state not in ("queued", "skipped")
    t = a.get("t") if started else None
    subs = list(a.get("subs") or []) if started else []
    first = (t.first_ts if t else None) or parse_time(start)
    last = (t.last_ts if t else None) or parse_time(end) or _log_end(a.get("log"))
    tok = (t.tokens if t else 0) + sum(s.tokens for s in subs)
    calls = (t.tool_calls if t else 0) + sum(s.tool_calls for s in subs)
    known = t is not None or bool(subs)   # no transcript found: tokens and tool calls are unknown, not 0
    if not started or not first:
        took = ""
    else:
        took = span(first) if state == "active" else span(first, last) if last else ""
    doing = ("", "", "")
    if state == "active" and not idle:
        latest = [(t.events[-1], "")] if t and t.events else []
        latest += [(s.events[-1], "sub-agent › ") for s in subs if s.events and s.recent()]
        if latest:
            (_, tool, desc), prefix = max(latest, key=lambda e: e[0][0])
            doing = (prefix, short_tool(tool), desc)
    return {"label": label, "unit": unit, "role": role, "model": model(arm) if arm else None, "state": state,
            "span": took, "tokens": tok, "tokens_h": tokens(tok) if known else "", "tools": calls,
            "now": f"{doing[0]}{doing[1]} {doing[2]}".strip(), "note": note or "",
            "subs": [{"prompt": s.prompt, "live": s.recent(), "span": span(s.first_ts, None if s.recent() else s.last_ts),
                      "tools": s.tool_calls} for s in subs],
            "_t": t, "_subs": subs, "_n": n, "_now": doing, "_known": known,
            "_start": parse_time(start) or first, "_end": None if state == "active" else (parse_time(end) or last)}


def build_phases(m: dict) -> list[dict]:
    """The run as Plan, Build, Review, Fix and Integrate, with one row per agent.

    Agents that have not started yet are queued rows: a worker whose pool is full, a reviewer
    for every unit whose review has not started (unless it was skipped), and a fixer for
    every unit with confirmed bugs, or a failed check, and no fixer yet.
    """
    if m.get("mode") in ANALYSIS_MODES:
        return analysis_phases(m)
    p = m["progress"]
    state, step, finished = p.get("state", "starting"), p.get("step") or "", m["finished"]
    at = RUN_PHASE.get(state, len(PHASES))
    units = m["units"]
    groups, add = _groups(finished)
    _plan_rows(m, add, "unit")

    for u in units:
        unit, su, uid = u["unit"], u["swarm"], u["id"]
        pstate, sstate = unit.get("state", "planned"), su.get("state")
        reason = unit.get("reason") or ""

        worker = next((a for a in u["agents"] if a["role"] == "worker"), {})
        if pstate == "held":
            st, note = "skipped", "held: " + (reason or "no model had quota")
        elif sstate in ("running", "checking"):
            st, note = "active", "running its check" if sstate == "checking" else "building"
        elif sstate == "failed" or (pstate == "failed" and not sstate):
            st, note = "failed", reason or "check failed"
        elif sstate == "accepted" or pstate in BUILT:
            st, note = "done", u["check"] or "check passed"
        elif sstate == "queued":
            st, note = "queued", "waiting for a free slot in its pool"
        elif worker.get("t") is not None:
            st, note = ("active" if worker["t"].recent() else "done"), ""
        else:
            st, note = "queued", "waiting for a free slot in its pool" if state == "running" else ""
        add("Build", f"{uid} · worker", uid, "worker", worker.get("arm") or unit.get("arm") or su.get("arm"), st, worker,
            note, start=su.get("started_at"), end=su.get("finished_at"),
            idle=sstate == "checking")

        review = unit.get("review") or {}
        confirmed = review.get("confirmed") or []
        reviewers = [a for a in u["agents"] if a["role"].startswith("reviewer") and a["state"] != "skipped"]
        reviewing_now = state == "reviewing" and step.startswith(uid + " by ")
        for n, a in enumerate(reviewers, 1):
            idle = False
            if n < len(reviewers):
                st, note = "failed", (a.get("failure") or "no verdict") + "; another reviewer tried"
            elif reviewing_now:
                idle = _log_done(a.get("log"))
                how = re.search(r"\(([^)]+)\)\s*$", step)
                st, note = "active", "confirming its findings" if idle else (how.group(1) if how else "")
            elif pstate == "review failed":
                st, note = "failed", reason or "review failed"
            else:
                st, note = "done", _review_note(review)
            add("Review", f"{uid} · reviewer" + (f" #{n}" if len(reviewers) > 1 else ""), uid, "reviewer", a.get("arm"),
                st, a, note, n=n, idle=idle)
        if review.get("skipped"):
            add("Review", f"{uid} · reviewer", uid, "reviewer", None, "skipped", None, review["skipped"])
        elif (not reviewers and not review and not finished and at <= 2 and pstate in ("planned", "accepted")
              and sstate != "failed"):
            add("Review", f"{uid} · reviewer", uid, "reviewer", None, "queued", None,
                "waiting for its turn" if pstate == "accepted" else "after its build")

        fixers = [a for a in u["agents"] if a["role"].startswith("fixer")]
        for n, a in enumerate(fixers, 1):
            idle = False
            if n < len(fixers):
                st, note = "done", f"pass {n} left issues"
            elif pstate == "fixing":
                idle = _log_done(a.get("log"))
                st, note = "active", "re-running the check and repros" if idle else f"pass {n}: " + (
                    plural(len(confirmed), "confirmed bug") if confirmed else "failing check")
            elif pstate == "failed":
                st, note = "failed", reason or "unresolved"
            else:
                st, note = "done", "fixed"
            add("Fix", f"{uid} · fixer" + (f" #{n}" if len(fixers) > 1 else ""), uid, "fixer", a.get("arm"), st, a, note,
                n=n, idle=idle)
        if not fixers and not finished and at <= 3:
            if pstate == "accepted" and confirmed:
                add("Fix", f"{uid} · fixer", uid, "fixer", None, "queued", None,
                    f"{plural(len(confirmed), 'confirmed bug')} to fix")
            elif pstate in ("failed", "planned") and (sstate == "failed" or reason.startswith("check failed")):
                add("Fix", f"{uid} · fixer", uid, "fixer", None, "queued", None, "check failed")

        resolver = next((a for a in u["agents"] if a["role"] == "resolver"), None)
        if resolver:
            if pstate == "resolving":
                st, note = "active", "resolving merge conflicts"
            elif pstate in ("conflict", "unmerged"):
                st, note = "failed", reason or pstate
            else:
                st, note = "done", "conflicts resolved"
            add("Integrate", f"{uid} · resolver", uid, "resolver", resolver.get("arm"), st, resolver, note)

    integration = p.get("integration") or {}
    integrator = m.get("integrator")
    if integrator:
        if integrator["state"] == "active":
            st, note = "active", "making the goal check pass"
        elif integration.get("goal_check_passed"):
            st, note = "done", "goal check passes"
        else:
            st, note = "failed", "goal check still failing"
        add("Integrate", "integrator", None, "integrator", integrator.get("arm"), st, integrator, note)

    return _assemble(groups, m, at, _phase_brief)


def _groups(finished: bool):
    """Empty phase groups, and the function that adds an agent row to one.

    Once the run has ended, an agent still marked working was stopped by it, and one still
    queued never started.
    """
    groups: dict[str, list[dict]] = {title: [] for title in PHASES}

    def add(phase: str, label: str, unit: str | None, role: str, arm: str | None, st: str, a: dict | None = None,
            note: str = "", **kw) -> None:
        if finished and st == "active":
            st, note = "failed", STOPPED
        elif finished and st == "queued":
            st, note = "skipped", "not started"
        groups[phase].append(_row(label, unit, role, arm, st, a, note, **kw))

    return groups, add


def _plan_rows(m: dict, add, noun: str) -> None:
    """The planner rows: a rejected plan, then the planner whose plan the run went with."""
    p, units, planners = m["progress"], m["units"], m["planners"]
    state, finished = p.get("state", "starting"), m["finished"]
    held = sum(u["unit"].get("state") == "held" for u in units)
    for n, a in enumerate(planners, 1):
        if n < len(planners):
            st, note = "failed", "plan rejected; planned again"
        elif state in ("starting", "planning"):
            st, note = "active", ""
        elif units:
            st, note = "done", plural(len(units), noun) + (f", {held} held" if held else "")
        elif finished:
            st, note = "failed", "; ".join(p.get("plan_problems") or []) or "no plan"
        else:
            st, note = "done", ""
        add("Plan", "planner" + (f" #{n}" if len(planners) > 1 else ""), None, "planner", a.get("arm"), st, a, note, n=n)


def _assemble(groups: dict[str, list[dict]], m: dict, at: int, brief, unused=()) -> list[dict]:
    """The five phases in order, each with its state, summary line, rows and time span.

    ``unused`` phases are ones this kind of run never has: skipped, with ``unused`` saying why.
    """
    phases = []
    for i, title in enumerate(PHASES):
        agents = groups[title]
        if title in unused:
            phases.append({"title": title, "state": "skipped", "summary": unused[title], "agents": agents, "_span": ""})
            continue
        ph_state = _phase_state(i, title, agents, m, at)
        tok = sum(a["tokens"] for a in agents)
        starts = [a["_start"] for a in agents if a["_start"]]
        ends = [a["_end"] for a in agents if a["_end"]]
        took = span(min(starts), None if ph_state == "active" else max(ends)) if starts and (ph_state == "active" or ends) else ""
        summary = " · ".join(x for x in (brief(title, ph_state, agents, m), f"{tokens(tok)} tokens" if tok else "") if x)
        phases.append({"title": title, "state": ph_state, "summary": summary, "agents": agents, "_span": took})
    return phases


def analysis_phases(m: dict) -> list[dict]:
    """A team or research run as the five phases: planners; the analysts or researchers;
    a team's verifiers and critic rounds; its fix build; the synthesizer."""
    p, words = m["progress"], ANALYSIS_MODES[m["mode"]]
    at = ANALYSIS_PHASE.get(p.get("state", "starting"), len(PHASES))
    groups, add = _groups(m["finished"])
    _plan_rows(m, add, words["unit"])
    for u in m["units"]:
        a = u["agents"][0]
        add("Build", f"{u['id']} · {words['worker']}", u["id"], words["worker"], a["arm"], a["state"], a, a["note"],
            start=a.get("start"))
    for a in m["others"]:
        add(a["phase"], a["label"], None, a["role"], a["arm"], a["state"], a, a["note"], start=a.get("start"),
            n=a.get("n", 1))
    # Verifiers and critic rounds take turns: in the order they started.
    groups["Review"].sort(key=lambda r: (r["_start"] is None, r["_start"].timestamp() if r["_start"] else 0,
                                         re.sub(r"\d+", lambda d: d.group().zfill(6), r["label"])))
    unused = {title: f"none in a {m['mode']} run" for title in PHASES if title not in words["uses"]}
    return _assemble(groups, m, at, _analysis_brief, unused)


def _phase_state(i: int, title: str, agents: list[dict], m: dict, at: int) -> str:
    finished = m["finished"]
    counted = [a for a in agents if a["state"] != "skipped"]
    states = {a["state"] for a in counted}
    if "active" in states or (not finished and at == i):
        return "active"
    if title == "Integrate" and m.get("mode") not in ANALYSIS_MODES:
        integration = m["progress"].get("integration") or {}
        if not integration.get("branch"):
            return "skipped" if finished or at > i else "pending"
        stuck = any(u["unit"].get("state") in ("conflict", "unmerged") for u in m["units"])
        check_failed = integration.get("goal_check_rc") is not None and not integration.get("goal_check_passed")
        if integration.get("error") or stuck or check_failed or "failed" in states:
            return "failed"
        return "done" if integration.get("goal_check_passed") else "failed" if finished else "pending"
    if not counted:
        return "skipped" if finished or at > i else "pending"
    if "queued" in states:
        return "pending"
    if states == {"failed"} or any(a["note"] == STOPPED for a in counted):
        return "failed"
    return "done"


def _counts(agents: list[dict]) -> list[str]:
    """'2/3', '1 running', '1 queued', '1 failed': how a phase's agents (bar skipped ones) stand."""
    counted = [a for a in agents if a["state"] != "skipped"]
    parts = [f"{sum(a['state'] == 'done' for a in counted)}/{len(counted)}"] if counted else []
    for st, word in (("active", "running"), ("queued", "queued"), ("failed", "failed")):
        k = sum(a["state"] == st for a in counted)
        if k:
            parts.append(f"{k} {word}")
    return parts


def _phase_brief(title: str, state: str, agents: list[dict], m: dict) -> str:
    """'2/3 · 1 running · 3 confirmed bugs': the phase's summary, without its tokens."""
    counted = [a for a in agents if a["state"] != "skipped"]
    parts = _counts(agents)
    skipped = sum(a["state"] == "skipped" for a in agents)
    units = m["units"]
    if title == "Plan" and units:
        parts.append(plural(len(units), "unit"))
    elif title == "Build" and skipped:
        parts.append(f"{skipped} not built")
    elif title == "Review":
        bugs = sum(len((u["unit"].get("review") or {}).get("confirmed") or []) for u in units)
        if bugs:
            parts.append(plural(bugs, "confirmed bug"))
        if skipped:
            parts.append(f"{skipped} skipped")
    elif title == "Fix" and not counted and state == "skipped" and any(u["unit"].get("review") for u in units):
        parts.append("nothing to fix")
    elif title == "Integrate":
        p = m["progress"]
        integration = p.get("integration") or {}
        if integration.get("branch"):
            parts.append(f"{sum(u['unit'].get('state') == 'merged' for u in units)}/{len(units)} merged")
        if integration.get("goal_check_rc") is not None:
            parts.append("goal check passed" if integration.get("goal_check_passed") else "goal check failed")
        elif state == "active" and p.get("step"):
            parts.append(p["step"])
    return " · ".join(parts) or {"pending": "not started", "skipped": "skipped"}.get(state, "")


def _analysis_brief(title: str, state: str, agents: list[dict], m: dict) -> str:
    """A team or research phase's summary: '13/13 · 158 findings', '93/93 · 18 upheld, 6 refuted · 2 critic rounds'."""
    p, mode = m["progress"], m["mode"]
    words = ANALYSIS_MODES[mode]
    units = [u["unit"] for u in m["units"]]
    parts = _counts(agents)
    if title == "Plan":
        if p.get("layout"):
            parts.append(f"layout {p['layout']}")
        if units:
            parts.append(plural(len(units), words["unit"]))
    elif title == "Build":
        held = sum(u.get("state") == "held" for u in units)
        session = sum(u.get("state") == "session" for u in units)
        found = sum(u.get("findings") or 0 for u in units)
        if held:
            parts.append(f"{held} held")
        if session:
            parts.append(f"{session} for your session")
        if found:
            parts.append(plural(found, "finding"))
    elif title == "Review":
        tally = [(v, sum(c.get("verdict") == v for c in m["verdicts"].values())) for v in VERDICTS]
        if any(k for _, k in tally):
            parts.append(", ".join(f"{k} {v}" for v, k in tally if k))
        rounds = sum(a["role"] == "critic" for a in agents)
        if rounds:
            parts.append(plural(rounds, "critic round"))
        if state == "active" and p.get("state") in ("cross-examining", "critic") and p.get("step"):
            parts.append(p["step"])
    elif title == "Fix" and not agents and state == "skipped":
        parts.append("no fix build")
    return " · ".join(parts) or {"pending": "not started", "skipped": "skipped"}.get(state, "")


def totals(m: dict) -> dict:
    """Run-wide counts: agents started or still to start, and the output tokens and tool calls spent."""
    rows = [a for ph in m["phases"] for a in ph["agents"] if a["state"] != "skipped"]
    spent = sum(a["tokens"] for a in rows)
    p = m["progress"]
    return {"agents": len(rows), **{key: sum(a["state"] == st for a in rows) for key, st in
                                    (("running", "active"), ("done", "done"), ("queued", "queued"), ("failed", "failed"))},
            "tokens": spent, "tokens_h": tokens(spent), "tools": sum(a["tools"] for a in rows),
            "elapsed": span(p.get("started_at"), p.get("finished_at"))}


def narrate(m: dict, limit: int = 12) -> list[str]:
    """What happened, oldest first, as a narrator would say it; the last ``limit`` lines.

    The run's phases happen one after another, so lines sort by phase, then by time.
    Integration has no per-merge times: its lines keep merge (plan) order.
    """
    if m.get("mode") in ANALYSIS_MODES:
        return narrate_analysis(m, limit)
    p = m["progress"]
    by_phase = {ph["title"]: ph["agents"] for ph in m["phases"]}
    units = {u["id"]: u for u in m["units"]}
    events: list[tuple[int, float, int, str]] = []

    def add(rank: int, ts: datetime | None, text: str) -> None:
        events.append((rank, ts.timestamp() if ts else 0.0, len(events), text))

    def ended(a: dict) -> datetime | None:
        """When an agent finished; its start when that is unknown, so the end never sorts before it."""
        return a["_end"] or a["_start"]

    for a in by_phase["Plan"]:
        if a["state"] == "active":
            add(0, a["_start"] or now(), f"{a['model'] or 'the planner'} is planning the goal")
        elif a["state"] == "failed":
            add(0, a["_end"], f"{a['label']}: {a['note']}")
        elif a["state"] == "done" and units:
            held = sum(u["unit"].get("state") == "held" for u in units.values())
            add(0, a["_end"], f"planner split the goal into {plural(len(units), 'unit')}" + (f", {held} held" if held else ""))
    for a in by_phase["Build"]:
        uid = a["unit"]
        if a["state"] == "skipped" and a["note"].startswith("held"):
            add(1, None, f"{uid} {a['note']}")
        if a["state"] in ("queued", "skipped"):
            continue
        add(1, a["_start"], f"{uid} started on {a['model'] or '?'}")
        if a["state"] == "done":
            check = units[uid]["check"]
            add(1, ended(a), f"{uid} built and passed its check" + (f" ({check})" if check else ""))
        elif a["state"] == "failed":
            add(1, ended(a), f"{uid} failed: {a['note']}")
    reviews = by_phase["Review"]
    for i, a in enumerate(reviews):
        uid, who = a["unit"], a["model"] or "a reviewer"
        retry = reviews[i + 1] if i + 1 < len(reviews) and reviews[i + 1]["unit"] == uid else None
        if a["state"] == "failed" and retry and retry["_start"] and ended(a):
            # A reviewer that gave up did so before the next one started, whatever its log's time says.
            add(2, min(ended(a), retry["_start"]), f"{who} could not review {uid}: {a['note']}")
        elif a["state"] == "active":
            add(2, a["_start"] or now(), f"{who} is reviewing {uid}")
        elif a["state"] == "done":
            confirmed = (units[uid]["unit"].get("review") or {}).get("confirmed") or []
            add(2, ended(a), f"{who} confirmed {plural(len(confirmed), 'bug')} in {uid}: {_title(confirmed[0])}"
                if confirmed else f"{uid} review clean")
        elif a["state"] == "failed":
            add(2, ended(a), f"{who} could not review {uid}: {a['note']}")
        elif a["state"] == "skipped" and a["note"] != "not started":
            add(2, None, f"{uid} review skipped: {a['note']}")
    for a in by_phase["Fix"]:
        if a["state"] in ("queued", "skipped"):
            continue
        uid, n = a["unit"], a["_n"]
        add(3, a["_start"] or now(), f"{uid} fix pass {n} by {a['model'] or '?'}")
        passes = f"{n} fix pass{'es' if n > 1 else ''}"
        if a["state"] == "done" and a["note"] == "fixed":
            add(3, ended(a), f"{uid} fixed after {passes}")
        elif a["state"] == "failed":
            add(3, ended(a), f"{uid} still failing after {passes}: {a['note']}")
    integration = p.get("integration") or {}
    resolvers = {a["unit"]: a for a in by_phase["Integrate"] if a["role"] == "resolver"}
    if integration.get("branch"):
        add(4, None, f"merging onto {integration['branch']}")
        for uid, u in units.items():
            if uid in resolvers:
                add(4, None, f"{resolvers[uid]['model'] or '?'} resolving merge conflicts in {uid}")
            ustate = u["unit"].get("state")
            if ustate == "merged":
                add(4, None, f"{uid} merged")
            elif ustate in ("conflict", "unmerged"):
                add(4, None, f"{uid} not merged: {u['unit'].get('reason') or ustate}")
    integrator = next((a for a in by_phase["Integrate"] if a["role"] == "integrator"), None)
    if integrator:
        add(4, None, f"goal check failed; {integrator['model'] or '?'} making it pass")
    if integration.get("goal_check_rc") is not None:
        add(4, None, "goal check passed" if integration.get("goal_check_passed")
            else f"goal check failed (exit {integration['goal_check_rc']})")
    if integration.get("error"):
        add(4, None, integration["error"])
    if m["finished"]:
        state, merged = p.get("state"), sum(u["unit"].get("state") == "merged" for u in units.values())
        where = f" on {integration['branch']}" if integration.get("branch") else ""
        if state == "done":
            add(5, None, f"run finished: {merged}/{len(units)} merged{where}")
        elif state == "planned":
            add(5, None, f"plan only: {plural(len(units), 'unit')} planned, nothing built")
        else:
            add(5, None, f"run {state}: {merged}/{len(units)} merged{where}"
                + (f" (exit {p['exit_code']})" if p.get("exit_code") is not None else ""))
    events.sort(key=lambda e: e[:3] if e[0] != 4 else (4, 0.0, e[2]))
    return [e[3] for e in events][-limit:]


def narrate_analysis(m: dict, limit: int = 12) -> list[str]:
    """A team or research run as a narrator tells it, oldest first; the last ``limit`` lines.

    A team goes round (analysts, verifiers, a critic adding analysts, verifiers again), so
    lines sort by time alone; one with no time of its own (a held analyst) takes the run's start.
    """
    p, mode = m["progress"], m["mode"]
    words = ANALYSIS_MODES[mode]
    by_phase = {ph["title"]: ph["agents"] for ph in m["phases"]}
    units = {u["id"]: u["unit"] for u in m["units"]}
    begun = parse_time(p.get("started_at")) or now()
    events: list[tuple[float, int, str]] = []

    def add(ts: datetime | None, text: str) -> None:
        events.append(((ts or begun).timestamp(), len(events), text))

    def ended(a: dict) -> datetime | None:
        return a["_end"] or a["_start"]

    for a in by_phase["Plan"]:
        if a["state"] == "active":
            add(a["_start"] or now(), f"{a['model'] or 'the planner'} is planning the " + ("team" if mode == "team" else "question"))
        elif a["state"] == "failed":
            add(ended(a), f"{a['label']}: {a['note']}")
        elif a["state"] == "done" and units:
            add(ended(a), f"planner split the question into {plural(len(units), 'part')}" if mode == "research"
                else f"planner chose a team of {plural(len(units), 'analyst')}")
    if not by_phase["Plan"] and p.get("layout") and units:
        add(begun, f"layout {p['layout']}: a team of {plural(len(units), 'analyst')}")
    for a in by_phase["Build"]:
        uid = a["unit"]
        if a["state"] == "skipped":
            if a["note"].startswith("held"):
                add(begun, f"{uid} {a['note']}")
            continue
        if a["state"] == "queued":
            continue
        if a["_start"] or a["state"] == "active":
            add(a["_start"] or now(), f"{uid} started on {a['model'] or '?'}")
        if a["state"] == "done":
            add(ended(a), f"{uid} delivered {a['note']}" if "findings" in units[uid] else f"{uid} answered ({a['note']})")
        elif a["state"] == "failed":
            add(ended(a), f"{uid} failed: {a['note']}")
    verifiers = [a for a in by_phase["Review"] if a["role"] == "verifier"]
    live = [a for a in verifiers if a["state"] == "active"]
    if live:
        add(min(a["_start"] or now() for a in live), f"{plural(len(live), 'verifier')} cross-examining claims")
    elif verifiers:
        tally = [(v, sum(c.get("verdict") == v for c in m["verdicts"].values())) for v in VERDICTS]
        examined = sum(k for _, k in tally)
        last = max((ended(a) for a in verifiers if ended(a)), default=None)
        add(last, f"verifiers cross-examined {plural(examined, 'claim')}: " + ", ".join(f"{k} {v}" for v, k in tally if k)
            if examined else f"{plural(len(verifiers), 'verifier run')} finished")
    for a in by_phase["Review"]:
        if a["role"] != "critic":
            continue
        if a["state"] == "active":
            add(a["_start"] or now(), f"{a['model'] or 'the critic'} is looking for what the team missed ({a['label']})")
        elif a["note"]:
            add(ended(a), f"{a['label']}: {a['note']}")
    for a in by_phase["Fix"]:
        when = (a["_start"] or now()) if a["state"] == "active" else ended(a)
        add(when, f"fix build: {a['note']}")
    for a in by_phase["Integrate"]:
        if a["state"] == "active":
            add(a["_start"] or now(), f"{a['model'] or 'the synthesizer'} is writing the report")
        elif a["state"] == "done":
            add(ended(a), f"{a['model'] or 'the synthesizer'} wrote the report")
        else:
            add(ended(a), f"synthesizer: {a['note']}")
    final = []
    if m["finished"]:
        state = p.get("state")
        remote = [u for u in units.values() if u.get("state") not in ("session", "held")]
        delivered = sum(u.get("state") == words["delivered"] for u in remote)
        verb = "parts answered" if mode == "research" else "analysts delivered"
        if state == "planned":
            final.append(f"plan only: {plural(len(units), words['unit'])} planned, nothing run")
        else:
            final.append(f"run {'finished' if state == 'done' else state}: {delivered}/{len(remote)} {verb}"
                         + (f" (exit {p['exit_code']})" if state != "done" and p.get("exit_code") is not None else ""))
    events.sort()
    return ([e[2] for e in events] + final)[-limit:]


def activity(model_: dict, limit: int = 8) -> list[tuple[datetime, str, str, str, str]]:
    rows = []
    for a in model_["planners"]:
        if a.get("t"):
            rows += [(ts, "planner", a["role"], tool, desc) for ts, tool, desc in a["t"].events]
    for u in model_["units"]:
        for a in u["agents"]:
            if a.get("t"):
                rows += [(ts, u["id"], a["role"], tool, desc) for ts, tool, desc in a["t"].events]
            for s in a.get("subs") or []:
                rows += [(ts, u["id"], f"{a['role']} › sub-agent", tool, desc) for ts, tool, desc in s.events]
    for a in model_.get("others") or []:
        if a.get("t"):
            rows += [(ts, a["who"], a["role"], tool, desc) for ts, tool, desc in a["t"].events]
        for s in a.get("subs") or []:
            rows += [(ts, a["who"], f"{a['role']} › sub-agent", tool, desc) for ts, tool, desc in s.events]
    integrator = model_.get("integrator") or {}
    if integrator.get("t"):
        rows += [(ts, "integration", "integrator", tool, desc) for ts, tool, desc in integrator["t"].events]
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows[:limit]


# ------------------------------------------------------------------ JSON (for the Claude Code mod)

def _public(row: dict) -> dict:
    """A phase or agent row without the fields that are only there for rendering."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


def to_json(model_: dict) -> dict:
    """Everything the dashboard shows, as plain data: what the ultra-watch mod polls."""
    p = model_["progress"]

    def agent(a: dict) -> dict:
        t = a.get("t")
        out = {"role": a["role"], "model": model(a.get("arm")) if a.get("arm") else None, "state": a["state"], "note": a.get("note")}
        if t:
            out.update(span=span(t.first_ts, None if a["state"] == "active" else t.last_ts), tools=t.tool_calls, tokens=tokens(t.tokens))
            if a["state"] == "active" and t.events:
                _, tool, desc = t.events[-1]
                out["now"] = f"{short_tool(tool)} {desc}".strip()
        out["subs"] = [{"prompt": s.prompt, "live": s.recent(), "span": span(s.first_ts, None if s.recent() else s.last_ts),
                        "tools": s.tool_calls} for s in a.get("subs") or []]
        return out

    goal = " ".join(str(p.get("goal", "")).split())
    updated = parse_time(p.get("updated_at"))
    return {
        "run": model_["run"].name, "state": p.get("state"), "step": p.get("step") or "", "finished": model_["finished"],
        "exit_code": p.get("exit_code"), "elapsed": span(p.get("started_at"), p.get("finished_at")),
        "updated_secs": int((now() - updated).total_seconds()) if updated else None,
        "goal": goal.split(". ", 1)[0].rstrip(".") + "." if goal else "",
        "stages": [[label, st] for _, label, st in model_["stages"]],
        "planners": [agent(a) for a in model_["planners"]],
        "units": [{"id": u["id"], "tier": u["unit"].get("tier"), "state": u["unit"].get("state"), "note": u["note"], "check": u["check"],
                   "findings": [_title(f) for f in (u["unit"].get("review") or {}).get("confirmed") or []],
                   "cells": [[stage, st] for stage, st in u["cells"]], "agents": [agent(a) for a in u["agents"]]} for u in model_["units"]],
        "activity": [[ts.astimezone().strftime("%H:%M:%S"), who, role, short_tool(tool), desc] for ts, who, role, tool, desc in activity(model_)],
        "messages": [[(parse_time(m.get("at")) or now()).astimezone().strftime("%H:%M:%S"), m.get("from_unit") or "hub",
                      " ".join(str(m.get("text", "")).split())] for m in model_["channel"][-4:]],
        "headroom": p.get("headroom") or {},
        "branch": (p.get("integration") or {}).get("branch"),
        "summary": str(model_["run"] / "summary.md") if (model_["run"] / "summary.md").is_file() else None,
        "totals": model_["totals"],
        "phases": [{**_public(ph), "agents": [_public(a) for a in ph["agents"]]} for ph in model_["phases"]],
        "log": model_["log"],
    }


# ------------------------------------------------------------------ rendering (rich)

def _fit(text: str, width: int) -> str:
    """``text`` cut to ``width`` columns, with an ellipsis when cut."""
    text = " ".join(str(text or "").split())
    if width <= 0:
        return ""
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def _fold(rows: list[dict]) -> tuple[list[dict], list[tuple[str, list[dict]]]]:
    """A long phase's rows to draw one by one, and the rest as (state, rows) to draw one line each.

    Working and failed agents are drawn (up to FOLD of them); the others fold by state and role
    ("84 verifiers done", "2 critics done").
    """
    if len(rows) <= FOLD:
        return rows, []
    shown = [a for a in rows if a["state"] in ("active", "failed")][:FOLD]
    kept = {id(a) for a in shown}
    rest = [a for a in rows if id(a) not in kept]
    roles = list(dict.fromkeys(a["role"] for a in rest))
    order = ("active", "failed", "queued", "done", "skipped")
    return shown, [(st, group) for st in order for role in roles
                   if (group := [a for a in rest if a["state"] == st and a["role"] == role])]


def render(model_: dict, width: int, animate: bool = False):
    """The workflow view: header, one group per phase with a row per agent, then units, log, activity."""
    from rich.console import Group
    from rich.text import Text

    width = max(60, width)
    tick = SPINNER[int(time.time() * 8) % len(SPINNER)] if animate else GLYPH["active"]

    def glyph(state: str) -> str:
        return tick if state == "active" else GLYPH.get(state, "?")

    def line() -> Text:
        return Text(no_wrap=True, overflow="ellipsis", end="\n")

    p, tot, finished = model_["progress"], model_["totals"], model_["finished"]
    out: list[Text] = []

    # Header: run, state, agents (running / queued / failed), sub-agents, tokens, elapsed. One line.
    rs = p.get("state", "?")
    run_state = "done" if rs in ("done", "planned") else "failed" if finished else "active"
    subs = sum(len(a["_subs"]) for ph in model_["phases"] for a in ph["agents"])
    mode = model_.get("mode")

    def header_tail(counts: tuple[str, ...], with_subs: bool, with_exit: bool) -> Text:
        tail = line()
        tail.append(f" {glyph(run_state)} {rs}" + (f" ({mode})" if mode else ""), STYLE[run_state])
        tail.append(" · ", "grey50")
        tail.append(plural(tot["agents"], "agent"), "bold")
        extra = [(f"{tot[key]} {key}", style) for key, style in
                 (("running", "yellow"), ("queued", "grey62"), ("failed", "red")) if key in counts and tot[key]]
        if extra:
            tail.append(" (")
            for i, (text, style) in enumerate(extra):
                tail.append((", " if i else "") + text, style)
            tail.append(")")
        for text in ([plural(subs, "sub-agent")] if with_subs and subs else []) + [f"{tot['tokens_h']} tokens", tot["elapsed"]] + (
                [f"exit {p['exit_code']}"] if with_exit and p.get("exit_code") is not None else []):
            tail.append(" · ", "grey50")
            tail.append(text, "bold")
        return tail

    # Tokens and elapsed time always show. When the line is too long, the sub-agent count goes
    # first, then the queued, failed and running counts (the phases list them), then the exit code,
    # while the run name gives way from 16 columns to 8.
    name = model_["run"].name
    for counts, with_subs, with_exit, room in ((("running", "queued", "failed"), True, True, 16),
                                               (("running", "queued", "failed"), False, True, 16),
                                               (("running", "failed"), False, True, 16),
                                               (("running",), False, True, 12),
                                               ((), False, True, 10), ((), False, False, 8)):
        tail = header_tail(counts, with_subs, with_exit)
        if 6 + min(len(name), room) + tail.cell_len <= width:
            break
    head = line()
    head.append("ULTRA ", "bold magenta")
    head.append(_fit(name, max(8, width - 6 - tail.cell_len)), "bold")
    head.append_text(tail)
    out.append(head)
    goal_text = " ".join(str(p.get("goal", "")).split())
    if goal_text:
        out.append(Text(_fit(goal_text.split(". ", 1)[0].rstrip(".") + ".", width), style="grey58", no_wrap=True))
    out.append(Text(""))

    # Columns shared by every agent row, so the numbers line up down the page.
    rows = [a for ph in model_["phases"] for a in ph["agents"]]
    mw, tw, kw, lw = 13, 7, 6, 5
    lab = min(30, max([len(a["label"]) for a in rows] + [20]))
    lab = max(14, min(lab, width - (4 + 2 + mw + 2 + tw + 2 + kw + 2 + lw + 2) - 16))
    left = 4 + lab + 2 + mw

    def numbers(t: Text, took: str, tok: str, calls: str, style: str) -> None:
        t.append(f"  {took:>{tw}}  {tok:>{kw}}  {calls:>{lw}}", style)

    hdr = line()
    hdr.append(f"    {'agent':<{lab}}  {'model':<{mw}}", "grey42")
    numbers(hdr, "time", "tokens", "tools", "grey42")
    hdr.append("  note", "grey42")
    out.append(hdr)
    for ph in model_["phases"]:
        st = ph["state"]
        t = line()
        t.append(f"{glyph(st)} ", STYLE[st])
        t.append(f"{ph['title']:<11}", "bold" if st in ("active", "done", "failed") else "grey50")
        t.append(ph["summary"], "grey62")
        if ph["_span"]:
            t.append(f" · {ph['_span']}", "grey50")
        out.append(t)
        shown, folded = _fold(ph["agents"])
        for a in shown:
            st = a["state"]
            started = st not in ("queued", "skipped")
            t = line()
            t.append(f"  {glyph(st)} ", STYLE[st])
            t.append(f"{_fit(a['label'], lab):<{lab}}", "bold" if st == "active" else "grey50" if not started else "")
            t.append(f"  {_fit(a['model'] or '—', mw):<{mw}}", "cyan" if a["model"] else "grey42")
            if started:
                numbers(t, a["span"], a["tokens_h"], str(a["tools"]) if a["_known"] else "", "grey70")
            else:
                numbers(t, st, "", "", "grey42")
            if a["note"]:
                t.append(f"  {a['note']}", "red" if st == "failed" else "grey62")
            out.append(t)
            prefix, tool, desc = a["_now"]
            if tool:
                t = line()
                t.append("      now ", "yellow")
                t.append(prefix, "grey62")
                t.append(f"{tool} ", "bold")
                t.append(desc, "grey70")
                out.append(t)
            live = [s for s in a["_subs"] if s.recent()]
            ended = [s for s in a["_subs"] if not s.recent()]
            for s in live + (ended if len(ended) <= 2 else []):
                on = s in live
                t = line()
                t.append(f"    {tick if on else GLYPH['done']} ", "bold yellow" if on else "green")
                t.append(f"{_fit('sub-agent ' + (s.prompt or ''), left - 6):<{left - 6}}", "grey70")
                numbers(t, span(s.first_ts, None if on else s.last_ts), tokens(s.tokens), str(s.tool_calls), "grey58")
                out.append(t)
            if len(ended) > 2:
                t = line()
                t.append(f"    {GLYPH['done']} ", "green")
                t.append(f"{_fit(plural(len(ended), 'sub-agent') + ' finished', left - 6):<{left - 6}}", "grey70")
                numbers(t, "", tokens(sum(s.tokens for s in ended)), str(sum(s.tool_calls for s in ended)), "grey58")
                out.append(t)
        for st, group in folded:
            t = line()
            t.append(f"  {glyph(st)} ", STYLE[st])
            t.append(f"{_fit(plural(len(group), group[0]['role']) + ' ' + st, lab + 2 + mw):<{lab + 2 + mw}}", "grey62")
            known = [a for a in group if a["_known"]]
            if known:
                numbers(t, "", tokens(sum(a["tokens"] for a in group)), str(sum(a["tools"] for a in known)), "grey58")
            out.append(t)

    gutter = 10

    def section(name: str, items: list[Text]) -> None:
        if not items:
            return
        out.append(Text(""))
        for i, item in enumerate(items):
            t = line()
            t.append(f"{name if i == 0 else '':<{gutter}}", "grey50")
            t.append_text(item)
            out.append(t)

    # One line per unit: its stage strip and where it stands.
    units = model_["units"]
    uw = min(24, max([len(u["id"]) for u in units] + [8]))
    strips = []
    for u in units:
        t = Text()
        t.append(f"{_fit(u['id'], uw):<{uw}}  ", "bold")
        for i, (stage, st) in enumerate(u["cells"]):
            t.append((" " if i else "") + glyph(st) + stage, STYLE.get(st, ""))
        if u["note"]:
            t.append(f"  {u['note']}", "grey62")
        strips.append(t)
    noun = ANALYSIS_MODES[mode]["unit"] if mode in ANALYSIS_MODES else "unit"
    if not units:
        strips.append(Text(f"{noun}s appear once the plan is made", style="grey50"))
    section(f"{noun}s", strips)
    log = model_["log"][-5:]
    section("log", [Text(s, style="" if i == len(log) - 1 else "grey70") for i, s in enumerate(log)])
    calls = []
    recent = sorted(activity(model_, 5), key=lambda r: r[0])   # oldest first, like the log
    ww = min(30, max([len(f"{who} · {role}") for _, who, role, _, _ in recent] + [1]))
    for ts, who, role, tool, desc in recent:
        t = Text()
        t.append(ts.astimezone().strftime("%H:%M:%S") + "  ", "grey50")
        t.append(f"{_fit(f'{who} · {role}', ww):<{ww}}  ", "cyan")
        t.append(f"{short_tool(tool)}  ", "bold")
        t.append(" ".join(str(desc).split()), "grey70")
        calls.append(t)
    section("activity", calls)
    chat = []
    said = model_["channel"][-3:]
    fw = min(18, max([len(m.get("from_unit") or "hub") for m in said] + [1]))
    for m in said:
        ts = parse_time(m.get("at"))
        t = Text()
        t.append((ts.astimezone().strftime("%H:%M:%S") if ts else "") + "  ", "grey50")
        t.append(f"{_fit(m.get('from_unit') or 'hub', fw):<{fw}}  ", "magenta")
        t.append(" ".join(str(m.get("text", "")).split()), "grey70")
        chat.append(t)
    section("messages", chat)
    foot = []
    if p.get("headroom"):
        t = Text()
        for i, (k, v) in enumerate(p["headroom"].items()):
            t.append(("  " if i else "") + f"{k} ", "grey70")
            t.append(str(v), "green" if v in ("ok", "comfortable") else "yellow" if v == "constrained" else "red")
        foot.append(("quota", t))
    integration = p.get("integration") or {}
    if integration.get("branch"):
        foot.append(("branch", Text(integration["branch"], style="bold")))
    if finished and (model_["run"] / "summary.md").is_file():
        foot.append(("summary", Text(str(model_["run"] / "summary.md"))))
    if foot:
        out.append(Text(""))
        for name, item in foot:
            t = line()
            t.append(f"{name:<{gutter}}", "grey50")
            t.append_text(item)
            out.append(t)
    return Group(*out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Live view of an /ultra run and its agents.")
    parser.add_argument("run", nargs="?", help="run id or unique prefix (default: the latest run)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="print one snapshot and exit")
    mode.add_argument("--watch", action="store_true", help="live view until Ctrl-C")
    mode.add_argument("--json", action="store_true", help="print the dashboard's data as JSON (for the ultra-watch mod)")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--width", type=int, default=0, help="snapshot width (default: terminal, or 120)")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)
    run_dir = find_run(args.run)
    if args.json:
        print(json.dumps(to_json(collect(run_dir)), separators=(",", ":")))
        return 0
    from rich.console import Console
    from rich.live import Live

    tty = sys.stdout.isatty()
    plain = args.no_color or bool(os.environ.get("NO_COLOR")) or not tty
    console = Console(width=args.width or (None if tty else 120), color_system=None if plain else "auto",
                      force_terminal=tty and not plain, highlight=False)
    if not (args.watch or (tty and not args.once)):
        console.print(render(collect(run_dir), console.width))
        return 0
    try:
        with Live(console=console, screen=True, auto_refresh=False, transient=False) as live:
            while True:
                m = collect(run_dir)
                live.update(render(m, console.width, animate=True), refresh=True)
                time.sleep(args.interval if not m["finished"] else 5)
    except KeyboardInterrupt:
        pass
    console.print(render(collect(run_dir), console.width))
    return 0


if __name__ == "__main__":
    sys.exit(main())
