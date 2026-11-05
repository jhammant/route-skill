import os
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, Mock, patch
from route import ultra as u
from route.ultra_plan import Plan, Unit


def test_initial_checks_use_safety():
    real_swarm = u.run_swarm_manifest
    ns = real_swarm.__globals__
    unit = Unit('u', 'task', 'pytest -q', tier='hard')
    plan = Plan('p', '', 'pytest -q', [unit])
    assignment = ns['Assignment'](
        unit=unit.to_dict(), text='task',
        plan=dict(decision_id='d', chosen='arm', shape='code', tier='hard', why='pinned'),
        ctx=Mock(pools={'arm': NS(parallel_limit=1)}))
    directory = MagicMock(spec=Path)
    directory.name = 'repro'
    directory.exists.return_value = False
    proc = MagicMock(returncode=0, stdout='', stderr='')
    proc.__enter__.return_value = proc
    proc.communicate.return_value = ('', None)
    runner = Mock(return_value=proc)
    dependencies = dict(
        load_manifest=lambda value: value, assign_units=Mock(return_value=([assignment], [])),
        swarm_dir=Mock(return_value=directory), _git=Mock(return_value=proc),
        atomic_write=Mock(), Stats=Mock(), TaskMemory=Mock(), _settle=Mock(return_value=[]),
        dispatch_command=Mock(return_value=(['worker'], {'AWS_SECRET_ACCESS_KEY': 'SYNTHETIC'})),
        render_brief=Mock(return_value='brief'), _print_assignments=Mock(),
        SwarmStatus=Mock(), quota_provider=Mock(return_value='arm'))

    def swarm(manifest, **kwargs):
        kwargs['no_amail'] = True
        return real_swarm(manifest, **kwargs)

    orchestration = dict(
        ultra_dir=Mock(return_value=Path('/memory/run')), pools_for_host=Mock(return_value={}),
        _context=Mock(return_value=''), pick=Mock(return_value='arm'),
        _plan=Mock(return_value=(plan, [])), atomic_write=Mock(),
        run_swarm_manifest=swarm, _review_unit=Mock(return_value=[]),
        _integrate=Mock(return_value={'merged': ['u'], 'goal_check_passed': True}),
        _settle=Mock(), _report=Mock(side_effect=lambda *args: args[-2]))
    with patch.dict(ns, **dependencies), patch.multiple(u, **orchestration), \
         patch.object(u.Run, 'save'), patch.object(subprocess, 'run', runner), \
         patch.object(subprocess, 'Popen', runner), \
         patch.dict(os.environ, AWS_SECRET_ACCESS_KEY='SYNTHETIC'), \
         patch.multiple(u.cli, load_pools=Mock(return_value={}), fetch_quota=Mock(return_value={}),
                        fetch_usage=Mock(return_value={}), _dispatch=Mock(return_value=0)):
        run = u.Run('repro', 'g', Path('.'), 'HEAD', 'codex')
        u._execute(run, 'g', Path('.'), 'HEAD', 'sha', 'brief', None,
                   10, 2, False, False, False, 'codex')
    unsafe = [c for c in runner.call_args_list
              if c.kwargs.get('shell') and
              (c.kwargs.get('env') is None or 'AWS_SECRET_ACCESS_KEY' in c.kwargs['env'])]
    assert not unsafe, 'Model-written check ran in a raw shell with credentials'
    checks = [c for c in runner.call_args_list if c.args[0] == ['pytest', '-q']]
    assert len(checks) == 1, 'The initial check must actually pass through the safe runner'
    assert checks[0].kwargs['shell'] is False
    assert checks[0].kwargs['start_new_session'] is True
    assert 'AWS_SECRET_ACCESS_KEY' not in checks[0].kwargs['env']
    assert checks[0].kwargs['env']['ROUTE_WORKER'] == '1'
