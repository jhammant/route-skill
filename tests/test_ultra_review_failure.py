import contextlib
from pathlib import Path
from unittest.mock import Mock, patch
from route import ultra as u
from route.ultra_plan import Plan, Unit


def test_failed_review_cannot_merge():
    plan = Plan('p', '', 'pytest', [Unit('a', 'task', 'pytest', tier='hard')])
    row = dict(id='a', arm='author', accepted=True, worktree='.', branch='unit-a',
               log='unused', decision_id='d', agent_rc=0)

    def integrate(*args):
        return dict(merged=list(args[3]), goal_check_passed=True)

    replacements = {
        'ultra_dir': Mock(return_value=Path('/memory/run')),
        'pools_for_host': Mock(return_value={}),
        '_context': Mock(return_value=''),
        'pick': Mock(return_value='model'),
        '_plan': Mock(return_value=(plan, [])),
        'atomic_write': Mock(),
        'run_swarm_manifest': Mock(return_value={'units': [row]}),
        'swarm_dir': Mock(return_value=Path('/memory/swarm')),
        'unit_diff': Mock(return_value='+changed\n'),
        'review_independence': Mock(return_value='cross-family'),
        '_throwaway': Mock(side_effect=lambda *a: contextlib.nullcontext(Path('.'))),
        '_dispatch': Mock(return_value=(1, 'provider authentication failed')),
        '_carry_review_tests': Mock(return_value=[]),
        '_fix_unit': Mock(return_value=False),
        '_integrate': Mock(side_effect=integrate),
        '_settle': Mock(),
        '_report': Mock(side_effect=lambda *a: a[-2]),
    }
    with patch.multiple(u, **replacements), patch.object(u.Run, 'save'), \
         patch.multiple(u.cli, load_pools=Mock(return_value={}),
                        fetch_quota=Mock(return_value={}), fetch_usage=Mock(return_value={})):
        run = u.Run('repro', 'g', Path('.'), 'HEAD', 'codex')
        try:
            rc = u._execute(run, 'g', Path('.'), 'HEAD', 'sha', 'brief', None,
                            10, 2, False, False, False, 'codex')
        except (RuntimeError, ValueError):
            return
    assert rc != 0, 'A failed reviewer was accepted and merged'
