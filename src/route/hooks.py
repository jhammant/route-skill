"""Dispatch hooks — an opt-in seam for external trackers.

route fires two events per decision: ``decision``, once a dispatch is certain
to happen (or the task is staying here), and ``complete``, once it has. Each
hook receives a JSON payload on stdin.

Nothing about any particular tracker lives here. Correlation between the two
events is carried by the hook itself: whatever a hook prints on ``decision``
is handed back to it on ``complete`` as ``hook_context``, so an integration
can thread its own issue id through without inventing side-channel state
keyed on pid.

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
HOOK_CONTEXT_MAX_BYTES = 4096

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
            Path(os.path.expanduser(part)) for part in override.split(os.pathsep) if part
        ]
    directory = config_dir() / HOOKS_DIRNAME
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and os.access(path, os.X_OK)
    )
