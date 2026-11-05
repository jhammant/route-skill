"""Regression: a summary that could not be persisted must not look like a completed run."""

from pathlib import Path
from unittest.mock import patch

import pytest

from route import swarm_dispatch as s


def test_summary_write_failure_reaches_the_api_caller():
    manifest = {"name": "review", "swarm_id": "review", "units": [
        {"id": "unit", "task": "check", "check": "true"}]}
    with patch.object(s, "state_dir", return_value=Path("/unused-route-review")), \
         patch.object(s, "assign_units", return_value=([], [{"id": "unit", "reason": "held for host"}])), \
         patch.object(Path, "mkdir"), patch.object(s, "SwarmStatus"), \
         patch.object(s, "_settle", return_value=[]), \
         patch.object(s, "atomic_write", side_effect=OSError("disk full")):
        with pytest.raises(RuntimeError):
            s.run_swarm_manifest(manifest, no_quota=True, no_amail=True)
