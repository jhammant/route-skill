"""Handoff context for a delegated task: brief, digest or fork.

A Claude Code transcript is mostly tool output. Across Jon's twelve largest
recent sessions, tool results were 91.7% of the text, tool calls 5.7%, and what
the user and assistant actually said 2.4%. A *digest* keeps the conversation and
a one-line note per tool call, and drops the tool output: about a tenth of a
full fork. The worker knows what was discussed and decided, and re-reads any
file it needs (it gets the current version, not a stale copy).

A *fork* (``claude --resume <id> --fork-session``) hands over the whole
transcript. It is only cheap on the same account and the same model: the prompt
cache is scoped to both, so anywhere else every token bills uncached.

``brief`` (the default) adds nothing: the task text is the handoff.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .storage import state_dir

CONTEXT_LEVELS = ("brief", "digest", "fork")
DEFAULT_MAX_CHARS = 60_000  # ~15k tokens

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_NOISE = re.compile(r"<(local-command-stdout|local-command-stderr|command-message)>.*?</\1>", re.S)


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def find_transcript(session_id: str, home: Path | None = None) -> Path | None:
    """The transcript for a session ID, in whichever project directory holds it."""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id or ""):
        return None
    projects = (home or claude_home()) / "projects"
    matches = sorted(projects.glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_line(block: dict) -> str:
    """One line per tool call: what was done, never the output."""
    name = str(block.get("name", "tool"))
    data = block.get("input") or {}
    if not isinstance(data, dict):
        return f"- {name}"
    for key in ("description", "file_path", "path", "pattern", "url", "query", "command", "prompt"):
        if data.get(key):
            detail = data[key]
            if key == "description" and data.get("command"):
                detail = f"{data['description']}: {data['command']}"
            return f"- {name}: {_clip(detail, 240)}"
    return f"- {name}: {_clip(json.dumps(data, sort_keys=True), 160)}"


@dataclass
class Turn:
    role: str
    text: str


def turns_from_transcript(path: Path) -> list[Turn]:
    turns: list[Turn] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue
        # Messages typed while a turn was running arrive as queued_command
        # attachments, not user entries; they are often the instructions that matter.
        attachment = entry.get("attachment") if entry.get("type") == "attachment" else None
        if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
            if (attachment.get("origin") or {}).get("kind", "human") == "human" and attachment.get("prompt"):
                prompt = f"(sent mid-turn) {str(attachment['prompt']).strip()}"
                if turns and turns[-1].role == "user":
                    turns[-1].text += "\n" + prompt
                else:
                    turns.append(Turn("user", prompt))
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue
        message = entry.get("message") or {}
        role = message.get("role")
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        parts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = _NOISE.sub("", _REMINDER.sub("", str(block.get("text", "")))).strip()
                if text:
                    parts.append(text)
            elif kind == "tool_use" and role == "assistant":
                parts.append(_tool_line(block))
            # tool_result, thinking and images are dropped on purpose.
        if not parts:
            continue
        text = "\n".join(parts)
        if turns and turns[-1].role == role:
            turns[-1].text += "\n" + text
        else:
            turns.append(Turn(role, text))
    return turns


def build_digest(path: Path, session_id: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Markdown digest of a transcript, newest turns kept when it must be cut."""
    turns = turns_from_transcript(path)
    rendered = [f"**{'User' if t.role == 'user' else 'Assistant'}:** {t.text}" for t in turns]
    header = (f"# Handoff digest: session {session_id}\n\n"
              "The conversation that led to your task, with tool output removed. Each tool call "
              "is one line saying what was done. Re-read any file you need rather than trusting "
              "an old copy; decisions and constraints below still apply.\n")
    kept: list[str] = []
    budget = max_chars - len(header)
    # The first user turn usually states the goal, so it survives any cut.
    first = rendered[:1]
    budget -= sum(len(r) + 2 for r in first)
    for chunk in reversed(rendered[1:]):
        if budget - len(chunk) - 2 < 0:
            # The newest turn is the most relevant: trim it from the front rather than lose it.
            if not kept and budget > 200:
                kept.append("…" + chunk[-(budget - 3):])
            break
        kept.append(chunk)
        budget -= len(chunk) + 2
    kept.reverse()
    omitted = len(rendered) - len(first) - len(kept)
    body = first + ([f"*[{omitted} earlier turns omitted to fit the budget]*"] if omitted else []) + kept
    return header + "\n" + "\n\n".join(body) + "\n"


def write_digest(session_id: str, *, home: Path | None = None, max_chars: int = DEFAULT_MAX_CHARS) -> Path:
    """Write the digest under the state dir (owner-only) and return its path."""
    path = find_transcript(session_id, home)
    if path is None:
        raise FileNotFoundError(f"no transcript for session {session_id}")
    out = state_dir() / "digests" / f"{session_id}-{int(time.time())}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_digest(path, session_id, max_chars=max_chars), encoding="utf-8")
    out.chmod(0o600)
    return out


def digest_preamble(path: Path) -> str:
    return (f"Before you start, read the handoff digest at {path}: it is the conversation that led "
            "to this task (decisions, constraints, what was already tried). Then do the task below.")


@dataclass
class Handoff:
    """What a dispatch carries: possibly a longer task text and extra CLI flags."""

    level: str
    text: str
    template: str
    note: str = ""
    digest_path: Path | None = None


def prepare_handoff(level: str, text: str, template: str, *, session_id: str | None,
                    claude_cli: bool, plan_only: bool = False, home: Path | None = None) -> Handoff:
    """Apply a context level to one dispatch. Never fails the dispatch: it degrades.

    fork needs a Claude Code CLI arm and a findable transcript; otherwise it
    becomes a digest. A digest needs the transcript; otherwise it becomes a brief.
    """
    if level not in CONTEXT_LEVELS:
        raise ValueError(f"unknown context level: {level}")
    if level == "brief":
        return Handoff("brief", text, template)
    transcript = find_transcript(session_id, home) if session_id else None
    if transcript is None:
        why = "no session id (set CLAUDE_CODE_SESSION_ID or pass --session)" if not session_id \
            else f"no transcript for session {session_id}"
        return Handoff("brief", text, template, note=f"context {level} -> brief: {why}")
    if level == "fork" and claude_cli:
        forked = template.replace("{task}", f"--resume {session_id} --fork-session {{task}}") \
            if "{task}" in template else f"{template} --resume {session_id} --fork-session"
        return Handoff("fork", text, forked, note=f"forks session {session_id}")
    note = "" if level == "digest" else "context fork -> digest: this arm is not the Claude Code CLI"
    if plan_only:
        return Handoff("digest", text, template, note=note or "digest written at dispatch")
    path = write_digest(session_id, home=home)
    size = path.stat().st_size
    return Handoff("digest", f"{digest_preamble(path)}\n\n{text}", template,
                   note=(note + "; " if note else "") + f"digest {size:,} chars at {path}", digest_path=path)
