from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from route import ultra as u, ultra_review as review


def test_timeout_does_not_resolve_confirmed_bug():
    finding = review.Finding('bug', 'high', '', 'pytest tests/bug.py')
    unit = dict(id='a', task='task', check='pytest')
    row = dict(worktree='.', arm='author', accepted=True)
    with patch.object(u, '_pick_fixer', return_value='fixer'), \
         patch.object(u, '_dispatch', return_value=(0, '')), \
         patch.object(u, 'run_checked', return_value=NS(rc=0, output='', timed_out=False)), \
         patch.object(review, 'run_checked', return_value=NS(rc=124, output='', timed_out=True)), \
         patch.object(u, '_commit_if_changed', return_value=True):
        sound = u._fix_unit(Mock(), unit, row, [finding], {}, {}, 'codex', False)
    assert not sound, 'A timed-out repro was treated as resolved'


def test_second_fix_rechecks_previously_passing_repros():
    stage = [0]

    def dispatch(*args):
        stage[0] += 1
        return 0, ''

    def check(cmd, *args, **kwargs):
        passed = (cmd.endswith('a.py') and stage[0] == 1) or (cmd.endswith('b.py') and stage[0] == 2)
        return NS(rc=0 if passed else 1, output='', timed_out=False)

    findings = [review.Finding(x, 'high', '', f'pytest tests/{x}.py') for x in ('a', 'b')]
    with patch.object(u, '_pick_fixer', return_value='fixer'), \
         patch.object(u, '_dispatch', side_effect=dispatch), \
         patch.object(u, 'run_checked', return_value=NS(rc=0, output='', timed_out=False)), \
         patch.object(review, 'run_checked', side_effect=check), \
         patch.object(u, '_commit_if_changed', return_value=True):
        sound = u._fix_unit(Mock(), dict(id='a', task='task', check='pytest'),
                            dict(worktree='.', arm='author', accepted=True),
                            findings, {}, {}, 'codex', False)
    assert not sound, 'The second fix regressed a repro that was never rerun'
