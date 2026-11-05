import contextlib
import os
import select
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from route import ultra as u


def test_cancel_terminates_descendants():
    script = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(30)']); "
        "print(child.pid,flush=True); time.sleep(30)"
    )
    model = subprocess.Popen([sys.executable, '-B', '-c', script],
                             stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert model.stdout.readline().strip().isdigit()
        progress = dict(state='reviewing', pid=None, pids=[model.pid])
        with patch.object(u, '_load_progress', return_value=(progress, '')), \
             patch.object(u, 'swarm_dir', return_value=Path('/memory/no-swarm')), \
             patch.object(u, 'ultra_dir', return_value=Path('/memory/run')), \
             patch.object(u, 'atomic_write'):
            assert u.ultra_cancel('repro') == 0
        model.wait(timeout=2)
        ready = select.select([model.stdout], [], [], 0.5)[0]
        assert ready and model.stdout.read(1) == '', 'A descendant survived cancellation'
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(model.pid, signal.SIGKILL)
        model.wait(timeout=2)
        model.stdout.close()
