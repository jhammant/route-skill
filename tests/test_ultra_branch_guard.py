from pathlib import Path
from threading import RLock
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from route import ultra as u
from route.ultra_plan import Plan, Unit


def test_integrator_cannot_commit_on_main():
    head = ['ultra/repro']
    main_commits = []

    def git(cwd, *args):
        if args[0] == 'status':
            return NS(returncode=0, stdout=' M fixed.py\n', stderr='')
        if args[0] == 'commit' and head[0] == 'main':
            main_commits.append(args)
        name = 'refs/heads/' + head[0] if args[0] == 'symbolic-ref' else head[0]
        return NS(returncode=0, stdout=name + '\n', stderr='')

    def dispatch(*args):
        head[0] = 'main'
        return 0, ''

    run = NS(id='repro', dir=Path('/memory/run'), progress={}, lock=RLock(),
             step=Mock(), save=Mock(), unit=Mock())
    plan = Plan('p', '', 'pytest', [Unit('u', 'task', 'pytest')])
    with patch.object(u, '_git', side_effect=git), \
         patch.object(u, 'pick', return_value='integrator'), \
         patch.object(u, '_dispatch', side_effect=dispatch), \
         patch.object(u, 'run_checked', side_effect=[NS(rc=1, output='failed'), NS(rc=0, output='')]):
        try:
            u._integrate(run, plan, {'u': {'branch': 'unit-u'}}, {'u'},
                         Path('.'), 'base-sha', {}, {}, 'codex', False, 'goal')
        except (RuntimeError, ValueError):
            pass
    assert not main_commits, 'The orchestrator committed directly on main'
