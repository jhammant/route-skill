from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from route import ultra as u


def test_carried_tests_do_not_follow_symlinks():
    source, target = MagicMock(spec=Path), MagicMock(spec=Path)
    source.is_file.return_value = True
    source.is_symlink.return_value = True
    source.resolve.return_value = Path('/memory/credentials')
    source.read_bytes.return_value = b'SYNTHETIC_SECRET'
    target.exists.return_value = False
    target.is_symlink.return_value = False
    target.resolve.return_value = Path('/memory/unit/tests/leak.py')
    review_wt, unit_wt = MagicMock(spec=Path), MagicMock(spec=Path)
    review_wt.__truediv__.return_value = source
    review_wt.resolve.return_value = Path('/memory/review')
    unit_wt.__truediv__.return_value = target
    unit_wt.resolve.return_value = Path('/memory/unit')
    with patch.object(u, '_git', return_value=NS(stdout='?? tests/leak.py\n')), \
         patch.object(u, '_commit_if_changed', return_value=True):
        copied = u._carry_review_tests(review_wt, unit_wt, 'a')
    assert copied == [] and not target.write_bytes.called
