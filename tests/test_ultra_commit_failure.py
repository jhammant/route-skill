from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from route import ultra as u


def test_failed_commit_cannot_be_reported_as_fixed():
    def git(cwd, *args):
        return NS(returncode=1 if args[0] == 'commit' else 0,
                  stdout=' M src/fixed.py\n' if args[0] == 'status' else '',
                  stderr='commit rejected' if args[0] == 'commit' else '')

    with patch.object(u, '_pick_fixer', return_value='fixer'), \
         patch.object(u, '_dispatch', return_value=(0, '')), \
         patch.object(u, 'run_checked', return_value=NS(rc=0, output='')), \
         patch.object(u, '_git', side_effect=git):
        sound = u._fix_unit(Mock(), dict(id='a', task='task', check='pytest'),
                            dict(worktree='.', arm='author', accepted=True),
                            [], {}, {}, 'codex', False)
    assert not sound, 'An uncommitted fix was reported as fixed'
