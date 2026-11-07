from threading import RLock
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from route import ultra as u


def test_regrading_preserves_aggregate_weight():
    router, stats = Mock(), Mock()
    decisions = {
        key: dict(decision_id=key, shape='code', tier='hard', chosen='arm', swarm='run',
                  impression_recorded=True, final_score=1.0,
                  outcome_events=['completed', 'verified', 'accepted'])
        for key in ('a', 'b')
    }
    stats.decision.side_effect = decisions.__getitem__
    run = NS(id='run', progress={'units': {k: {'state': 'conflict'} for k in decisions}},
             lock=RLock(), save=Mock())
    with patch.object(u.cli, '_build_router', return_value=router), \
         patch.object(u.cli, 'Stats', return_value=stats), \
         patch.object(u.cli, 'TaskMemory'):
        u._settle(run, {k: {'decision_id': k} for k in decisions})
    corrections = [c.args[2] for c in router.storage.incr.call_args_list
                   if c.args[0].endswith(':accepted')]
    # Two accepted units originally contributed one aggregate reward.
    assert 1.0 + sum(corrections) == 0.0
