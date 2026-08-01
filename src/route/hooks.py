"""Dispatch hooks — an opt-in seam for external trackers.

route fires two events per decision: ``decision``, once a dispatch is certain
to happen (or the task is staying here), and ``complete``, once it has. Each
hook receives a JSON payload on stdin.

Nothing about any particular tracker lives here. Correlation between the two
events is carried by the hook itself: whatever a hook prints on ``decision``
is handed back to it on ``complete`` as ``hook_context``, so an integration
can thread its own issue id through without inventing side-channel state
keyed on pid.

A ``decision`` hook may also OFFER context to prepend to the dispatched task
by printing a JSON object with a ``prepend`` key (see ``parse_decision``).
That is the one thing a hook can do that is visible to the arm, and it is an
offer, not an instruction: route applies it only for ``data_class`` ``open``
work, caps it, and drops it entirely on the ``stay`` path. A hook still
cannot block a dispatch, redirect it, or change its exit code.

Hooks are advisory. Any failure is reported and ignored — a hook can never be
the reason a dispatch did not happen.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .storage import config_dir

#: Wall-clock ceiling for a single hook invocation.
HOOK_TIMEOUT_S = 15

#: Cap on the stdout carried from ``decision`` to ``complete``.
#:
#: Characters, not bytes: ``subprocess.run(text=True)`` hands back a decoded
#: ``str``, so slicing it counts code points. Counting bytes instead would
#: mean re-encoding and risk cutting a multi-byte character in half, for no
#: gain — the point of the cap is to bound the payload, and a bounded number
#: of characters is a bounded payload.
HOOK_CONTEXT_MAX_CHARS = 4096

#: Cap on context a ``decision`` hook offers for prepending to the task.
#:
#: Sized independently of the context cap because it is spent on a different
#: budget: ``hook_context`` costs route nothing, while this is charged against
#: the arm's own context window. A local arm can be serving 32k, and an
#: injected preamble that crowds out the task it is meant to inform is worse
#: than no preamble at all.
HOOK_PREPEND_MAX_CHARS = 4096

#: Raw stdout cap for the ``decision`` event alone.
#:
#: The structured form carries both fields plus a JSON envelope, and it is
#: parsed AFTER this cap applies — so capping at ``HOOK_CONTEXT_MAX_CHARS``
#: would cut a large object mid-string, fail the parse, and silently demote a
#: well-formed hook to the plain-text path. Both field caps are then enforced
#: on the parsed values, so this bound never widens what actually reaches
#: either consumer.
HOOK_DECISION_MAX_CHARS = HOOK_CONTEXT_MAX_CHARS + HOOK_PREPEND_MAX_CHARS + 512

#: Hook directory, relative to the route config dir.
HOOKS_DIRNAME = "dispatch-hooks.d"


def discover_hooks() -> list[Path]:
    """Hooks to fire, in order.

    ``ROUTE_DISPATCH_HOOKS`` (os.pathsep-separated) REPLACES the directory
    rather than adding to it, so a caller can always pin a known hook set.
    Otherwise: executable files in ``<config_dir>/dispatch-hooks.d``, sorted
    by name.
    """
    override = os.environ.get("ROUTE_DISPATCH_HOOKS")
    if override is not None:
        return [
            Path(os.path.expanduser(part))
            for part in override.split(os.pathsep)
            if part
        ]
    directory = config_dir() / HOOKS_DIRNAME
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and os.access(path, os.X_OK)
    )


def fire(hook: Path, payload: dict, max_chars: int = HOOK_CONTEXT_MAX_CHARS) -> str:
    """Run one hook with ``payload`` on stdin; return its stdout.

    Never raises. A hook that is missing, not executable, slow, or failing
    yields ``""`` and a line on stderr.

    ``max_chars`` bounds the returned stdout. It defaults to the
    ``hook_context`` cap, which is what ``complete`` needs; ``decision``
    passes ``HOOK_DECISION_MAX_CHARS`` because its stdout may still have to
    be parsed after truncation.
    """
    try:
        proc = subprocess.run(
            [str(hook)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=HOOK_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"route: dispatch hook {hook.name}: {exc}", file=sys.stderr)
        return ""
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        print(
            f"route: dispatch hook {hook.name} exited {proc.returncode}",
            file=sys.stderr,
        )
    return proc.stdout[:max_chars].strip()


def parse_decision(raw: str) -> tuple[str, str]:
    """Split a ``decision`` hook's stdout into ``(hook_context, prepend)``.

    Two accepted forms, and the plain one is the default:

    * **plain text** — the whole of stdout is the ``hook_context``, exactly as
      before this function existed, and there is no prepend.
    * **structured** — a JSON *object* carrying a ``prepend`` key. Then
      ``prepend`` is context offered for the dispatched task and
      ``hook_context`` (optional, ``""`` when absent) is the correlation
      token handed back on ``complete``.

    The discriminator is the presence of the ``prepend`` key, not merely
    "parses as JSON". A hook that already emits a JSON object as its opaque
    context — a bare id, a record, an array — keeps meaning exactly what it
    meant before: only a hook that asks for a prepend is read as asking for
    one.

    Fail-open in both directions. Malformed JSON, a non-object, or a
    non-string field is not an error: the text falls back to being an opaque
    context, because a hook that garbles its output must not take the
    dispatch down with it. Each field is capped independently here rather
    than at the pipe, so truncation lands inside a value instead of breaking
    the parse.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return text[:HOOK_CONTEXT_MAX_CHARS], ""
    try:
        obj = json.loads(text)
    except ValueError:
        return text[:HOOK_CONTEXT_MAX_CHARS], ""
    if not isinstance(obj, dict) or "prepend" not in obj:
        return text[:HOOK_CONTEXT_MAX_CHARS], ""
    prepend = obj.get("prepend")
    context = obj.get("hook_context", "")
    if not isinstance(prepend, str):
        prepend = ""
    if not isinstance(context, str):
        context = ""
    return (
        context.strip()[:HOOK_CONTEXT_MAX_CHARS],
        prepend.strip()[:HOOK_PREPEND_MAX_CHARS],
    )
