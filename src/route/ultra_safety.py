"""Keep generated checks and shared digests within ultra's safety policy.

Checks run without a shell, with credentials removed, and within a time budget.
The allowlist is not a sandbox: test runners still execute repository code.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .eligibility import probably_private
from .sensitive import detect_sensitive


ALLOWED: list[tuple[str, ...]] = [
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("pytest",),
    (".venv/bin/python", "-m", "pytest"),
    (".venv/bin/python3", "-m", "pytest"),
    ("uv", "run", "pytest"),
    ("node", "--test"),
    ("npm", "test"),
    ("npm", "run"),
    ("go", "test"),
    ("cargo", "test"),
    ("make", "test"),
    ("make", "check"),
    ("bash", "test/"),
    ("bash", "tests/"),
    ("zsh", "test/"),
    ("zsh", "tests/"),
    ("ruff", "check"),
    ("mypy",),
]

_OUTPUT_LIMIT = 20_000
_FORBIDDEN = re.compile(r"[;|><`&\r\n\x00]|\$\(")
# The existing safety net handles quoted colon assignments. Digests can also
# contain YAML-style, unquoted values, which need the same conservative check.
_UNQUOTED_SECRET = re.compile(
    r"\b[\w-]*(?:password|passwd|secret|token|api[_-]?key|credential)s?"
    r"\s*:\s*(?=[^\s\"']{12,})(?=[^\s\"']*\d)[^\s\"']+",
    re.IGNORECASE,
)


def _allowed(argv: list[str]) -> bool:
    for prefix in ALLOWED:
        if len(argv) < len(prefix):
            continue
        if prefix[0] in {"bash", "zsh"}:
            if argv[0] == prefix[0] and argv[1].startswith(prefix[1]):
                return True
        elif tuple(argv[:len(prefix)]) == prefix:
            return True
    return False


_NPM_SCRIPT = re.compile(r"^(test|check|lint|typecheck)([:._-][\w.:-]*)?$")


def _argument_reason(argv: list[str]) -> str | None:
    """Some allowed programs can still run arbitrary code through an argument.

    `npm run <script>` runs whatever package.json names (deploy, postinstall), and
    pytest's `-p NAME` imports a plugin by module name, so both are narrowed to
    the forms a check actually needs.
    """
    if argv[:2] == ["npm", "run"] and (len(argv) < 3 or not _NPM_SCRIPT.match(argv[2])):
        return "npm run is limited to test, check, lint and typecheck scripts"
    if any("pytest" in token for token in argv[:3]):
        for i, token in enumerate(argv):
            value = argv[i + 1] if token == "-p" and i + 1 < len(argv) else token[2:] if token.startswith("-p") and len(token) > 2 else None
            if value is not None and not value.startswith("no:"):
                return "pytest -p may only disable plugins (-p no:NAME)"
    return None


def _path_reason(argv: list[str], repo: Path) -> str | None:
    """Check flag values too, so --rootdir=/outside cannot evade the boundary."""
    for token in argv[1:]:
        for value in re.split(r"[=,]", token):
            # Attached short options (-c/path, -C../repo) also name paths.
            value = re.sub(r"^-[A-Za-z]+(?=/|\.{1,2}/)", "", value)
            path = Path(value.split("::", 1)[0])
            if ".." in path.parts:
                return "parent path segments are not allowed"
            if path.is_absolute() or "/" in value:
                try:
                    resolved = (repo / path).resolve()
                except (OSError, RuntimeError, ValueError):
                    return "path cannot be resolved safely"
                if not resolved.is_relative_to(repo):
                    return "paths must stay inside the repository"
    return None


def _lint_command(cmd: str, repo: Path) -> str | None:
    for part in cmd.split("&&"):
        if not part.strip():
            return "empty check or chain segment"
        if _FORBIDDEN.search(part):
            return "shell operators are not allowed (only && may join checks)"
        try:
            argv = shlex.split(part)
        except ValueError:
            return "invalid command quoting"
        if not argv or not _allowed(argv):
            return "command is not an allowed check"
        reason = _argument_reason(argv) or _path_reason(argv, repo)
        if reason:
            return reason
    return None


def lint_command(cmd: str) -> str | None:
    """Reject shell syntax and non-check commands before anything can execute.

    The current directory is the repository boundary. ``run_checked`` uses its
    supplied working directory instead, without changing the caller's directory.
    """
    return _lint_command(cmd, Path.cwd().resolve())


def safe_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Checks must not inherit the orchestrator's provider or messaging secrets."""
    source = os.environ if base is None else base
    env = {
        key: value for key, value in source.items()
        if not key.upper().startswith(("ANTHROPIC_", "AWS_", "AMAIL_"))
        and not key.upper().endswith(("_TOKEN", "_SECRET", "_API_KEY"))
    }
    env["ROUTE_WORKER"] = "1"
    return env


@dataclass
class RunResult:
    rc: int
    output: str
    timed_out: bool


def _output_text(output: str | bytes | None) -> str:
    # TimeoutExpired may carry bytes even when the process used text=True.
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output or ""


def run_checked(cmd: str, cwd: Path, *, timeout: float = 600) -> RunResult:
    """Validate the entire chain first; share one timeout across all its checks.

    Keeping only the output tail preserves failure details without bloating the
    review context. Each check inherits the same stripped environment.
    """
    reason = _lint_command(cmd, cwd.resolve())
    if reason:
        return RunResult(rc=126, output=f"refused: {reason}", timed_out=False)

    deadline = time.monotonic() + timeout
    env = safe_env()
    output = ""
    for part in cmd.split("&&"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return RunResult(rc=124, output=output, timed_out=True)
        try:
            proc = subprocess.run(
                shlex.split(part), cwd=cwd, env=env, shell=False,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            output = (output + _output_text(exc.output))[-_OUTPUT_LIMIT:]
            return RunResult(rc=124, output=output, timed_out=True)
        except OSError as exc:
            output = (output + f"could not run check: {exc}")[-_OUTPUT_LIMIT:]
            rc = 127 if isinstance(exc, FileNotFoundError) else 126
            return RunResult(rc=rc, output=output, timed_out=False)
        output = (output + _output_text(proc.stdout))[-_OUTPUT_LIMIT:]
        if proc.returncode:
            return RunResult(rc=proc.returncode, output=output, timed_out=False)
    return RunResult(rc=0, output=output, timed_out=False)


def screen_digest(text: str) -> str | None:
    """A content heuristic may block sharing; a miss never proves data public."""
    if detect_sensitive(text):
        return "digest names a configured sensitive path"
    if probably_private(text) or _UNQUOTED_SECRET.search(text):
        return "digest may contain credentials or private data"
    return None
