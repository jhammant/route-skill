import json
from types import SimpleNamespace

import pytest

from route.cli import main, main_auto, _dispatch
from route.eligibility import gate, parse_quota, fetch_quota
from route.pools import pools_for_host


@pytest.mark.parametrize('flag', ['needs_context', 'cross_repo', 'no_acceptance', 'private'])
def test_codex_context_veto_stays_in_codex(pools, flag):
    result = gate('implement an endpoint', 'coding:implement', pools,
                  host='codex', **{flag: True})
    assert result.eligible == ['codex']
    assert result.veto.target == 'codex'


def test_sensitive_codex_work_does_not_leak_to_claude(pools):
    result = gate('implement an endpoint', 'coding:implement', pools,
                  host='codex', sensitive=True)
    assert 'claude' in result.removed_by_sensitivity
    assert 'codex' in result.eligible
    assert 'local-agent' in result.eligible


def test_exhausted_delegates_fall_back_to_current_host(pools):
    remote = {k:pools[k] for k in ['claude', 'codex']}
    result = gate('implement endpoint', 'coding:implement', remote,
                  host='codex', quota={'claude':'critical', 'codex':'critical'})
    assert result.eligible == ['codex']


def test_host_overlay_preserves_provider_identity(pools):
    adapted = pools_for_host(pools, 'codex')
    assert adapted['codex'].accepts('writing')
    assert adapted['codex'].dispatch == ''
    assert adapted['claude'].dispatch.startswith('claude --print')
    assert pools['claude'].dispatch == ''  # source registry not mutated


def test_codex_to_claude_dispatch(monkeypatch, capsys):
    calls=[]
    monkeypatch.setattr('route.cli._dispatch', lambda template,text,**kw: calls.append((template,text,kw)) or 0)
    assert main_auto(['--host','codex','--pool','claude','--no-llm','--no-quota','write a test']) == 0
    assert calls[0][0].startswith('claude --print')
    assert calls[0][1] == 'write a test'
    assert calls[0][2] == {'target':'claude'}


def test_codex_stays_without_recursive_dispatch(monkeypatch, capsys):
    monkeypatch.setattr('route.cli._dispatch', lambda *a,**k: pytest.fail('must not dispatch'))
    assert main_auto(['--host','codex','--pool','codex','--no-llm','--no-quota','write a test']) == 0
    assert 'dispatch: stay' in capsys.readouterr().out


def test_legacy_claude_default_still_stays(monkeypatch, capsys):
    monkeypatch.delenv('ROUTE_HOST', raising=False)
    assert main_auto(['--pool','claude','--no-llm','--no-quota','write a test']) == 0
    assert 'dispatch: stay' in capsys.readouterr().out


def test_plan_only_never_prompts_or_launches(monkeypatch, capsys):
    monkeypatch.setattr('builtins.input',lambda *a: pytest.fail('must not prompt'))
    monkeypatch.setattr('route.cli._dispatch',lambda *a,**k: pytest.fail('must not dispatch'))
    assert main(['--host','codex','--pool','claude','--plan-only','--no-llm','--no-quota','write test']) == 0
    assert 'nothing launched' in capsys.readouterr().out


def test_dispatch_keeps_task_literal_and_sets_child_host(monkeypatch):
    calls=[]
    monkeypatch.setattr('route.cli.subprocess.run',lambda argv,**kw: calls.append((argv,kw)) or SimpleNamespace(returncode=0))
    task='write tests; $(touch should-not-exist)'
    assert _dispatch('claude --print {task}',task,target='claude') == 0
    assert calls[0][0] == ['claude','--print',task]
    assert calls[0][1]['env']['ROUTE_HOST'] == 'claude'


def test_unknown_pin_is_reported(capsys):
    assert main(['--pool','typo','--no-llm','--no-quota','write test']) == 1
    assert 'unknown pinned pool' in capsys.readouterr().err


def test_real_quotamax_formats_and_expired_windows():
    assert parse_quota({'ok':True,'headroom':'critical','advice':{}}) == {'claude':'critical'}
    assert parse_quota([{'id':'codex','ok':True,'limits':[
        {'percent':100,'resetsAt':'2000-01-01T00:00:00Z'},
        {'percent':30,'resetsAt':'2099-01-01T00:00:00Z'}]},
        {'id':'kimi','ok':True,'limits':[{'percent':96}]}]) == {'codex':'ok','kimi':'critical'}


def test_nonzero_critical_quota_is_not_discarded(monkeypatch):
    monkeypatch.setattr('route.eligibility.subprocess.run',lambda *a,**k:
        SimpleNamespace(returncode=3,stdout=json.dumps({'ok':True,'headroom':'critical'})))
    assert fetch_quota(('custom-quotamax','agent')) == {'claude':'critical'}
