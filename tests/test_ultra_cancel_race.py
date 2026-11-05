import copy
import json
from pathlib import Path
from unittest.mock import patch
from route import ultra as u


def test_cancel_preserves_concurrent_completion():
    latest = dict(schema=u.SCHEMA, state='reviewing', pid=123456, pids=[], exit_code=None)

    def write(path, text):
        latest.clear()
        latest.update(json.loads(text))

    def exited(pid):
        latest.update(state='done', exit_code=0, finished_at='finished')
        return False

    with patch.object(u, '_load_progress', side_effect=lambda _: (copy.deepcopy(latest), '')), \
         patch.object(u, '_alive', side_effect=exited), \
         patch.object(u, 'swarm_dir', return_value=Path('/memory/no-swarm')), \
         patch.object(u, 'ultra_dir', return_value=Path('/memory/run')), \
         patch.object(u, 'atomic_write', side_effect=write):
        u.ultra_cancel('repro')
    assert latest['state'] == 'done' and latest['exit_code'] == 0
