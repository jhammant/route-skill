"""SPEC-ship section 1: sensitive data PREFERS local, never falls back remote.

Detection is explicit only — a flag, a task field, or the sensitive.toml
path allowlist. Remote third-party arms (codex, kimi, free) are vetoed at
the eligibility stage; claude and local-* survive; local is preferred at
selection; and an empty survivor list raises instead of falling back.
"""

from __future__ import annotations

import pytest

from route.eligibility import SensitiveRoutingError, gate, sensitive_veto
from route.sensitive import detect_sensitive, load_sensitive_patterns, names_sensitive_path


def test_sensitive_vetoes_remote_arms_at_eligibility(pools):
    """codex/kimi are REMOVED (a veto, not a score); claude and local stay."""
    result = gate("rewrite the billing module", "coding:implement", pools, sensitive=True)
    assert result.veto is None
    assert result.data_class == "sensitive"
    assert "codex" not in result.eligible
    assert "kimi" not in result.eligible
    assert "free" not in result.eligible
    assert "claude" in result.eligible
    assert "local-agent" in result.eligible
    assert sorted(result.removed_by_sensitivity) == ["codex", "kimi"]


def test_sensitive_never_selects_remote_even_when_everything_exhausted(pools):
    """All-remote-critical quota cannot resurrect a vetoed remote arm."""
    critical = {"claude": "critical", "codex": "critical", "kimi": "critical"}
    result = gate("rewrite the billing module", "coding:implement", pools,
                  quota=critical, sensitive=True)
    assert result.eligible == ["local-agent"]  # claude fell to quota; only local remains
    for arm in ("codex", "kimi", "free"):
        assert arm not in result.eligible


def test_sensitive_free_arm_vetoed_hardest(pools):
    """Even on a shape free accepts, with the proxy up, free stays out."""
    survivors, removed = sensitive_veto(["claude", "free", "local-batch"], pools)
    assert "free" in removed
    assert survivors == ["claude", "local-batch"]


def test_sensitive_prefers_local_over_claude(router, pools):
    """The priors favour claude for coding:implement — the preference bonus
    lands sensitive work on local-agent anyway."""
    eligible = ["claude", "local-agent"]
    # Without the preference, the rule-table prior picks claude (Beta(8,2)
    # favoured vs Beta(2,8) unfavoured).
    assert router.select("coding:implement", "moderate", eligible) == "claude"
    local = [a for a in eligible if not pools[a].remote]
    assert router.select("coding:implement", "moderate", eligible, prefer=local) == "local-agent"


def test_sensitive_no_eligible_arm_raises(pools):
    """No local arm for the shape + claude quota-critical => loud failure,
    never a silent fall-back to a remote arm."""
    remote_only = {k: pools[k] for k in ("claude", "codex", "kimi")}
    with pytest.raises(SensitiveRoutingError, match="no eligible arm"):
        gate("rewrite the billing module", "coding:implement", remote_only,
             quota={"claude": "critical"}, sensitive=True)


def test_sensitive_batch_shape_without_local_raises(pools):
    """A registry with no local arm at all leaves a sensitive batch task
    with only vetoed remotes — raise, do not fall back."""
    remote_only = {k: pools[k] for k in ("claude", "codex", "kimi")}
    with pytest.raises(SensitiveRoutingError, match="Refusing to fall back"):
        gate("classify these tickets", "batch:classify", remote_only,
             quota={"claude": "critical"}, sensitive=True)


def test_sensitive_subsumes_the_private_data_regex(pools):
    """The content regex is a safety net; an explicit sensitive signal gets
    the better handling instead of a hard pin to claude."""
    result = gate("rotate the API credentials in the production .env file",
                  "coding:implement", pools, sensitive=True)
    assert result.veto is None
    assert "local-agent" in result.eligible and "claude" in result.eligible
    assert "codex" not in result.eligible and "kimi" not in result.eligible


def test_sensitive_does_not_swallow_later_vetoes(pools):
    """Subsuming the private-data net must not hide the orchestration veto:
    a sensitive task with an unclassifiable shape still never routes away."""
    result = gate("rotate the API credentials in the production .env file",
                  "orchestration", pools, sensitive=True)
    assert result.veto is not None and result.veto.name == "orchestration"
    assert result.eligible == ["claude"]
    assert result.data_class == "sensitive"


def test_open_tasks_are_unaffected(pools):
    result = gate("rewrite the billing module", "coding:implement", pools)
    assert result.data_class == "open"
    assert result.removed_by_sensitivity == []
    assert "codex" in result.eligible and "kimi" in result.eligible


# -- detection: explicit signals only ------------------------------------------


def test_flag_marks_sensitive():
    assert detect_sensitive("anything at all", flag=True)


def test_task_field_marks_sensitive():
    assert detect_sensitive("anything at all", task_field=True)


def test_no_signal_means_open():
    assert not detect_sensitive("rewrite the billing module", patterns=[])


def test_path_allowlist_marks_sensitive_without_the_flag(isolated_env):
    config = isolated_env / "config"
    config.mkdir(exist_ok=True)
    clients = isolated_env / "clients"
    (config / "sensitive.toml").write_text(
        f'paths = ["{clients}/*"]\n', encoding="utf-8"
    )
    patterns = load_sensitive_patterns()  # reads ROUTE_CONFIG_DIR
    assert names_sensitive_path(f"rotate credentials in {clients}/acme/.env", patterns)
    assert detect_sensitive(f"rotate credentials in {clients}/acme/.env")
    # A path outside the allowlist stays open.
    assert not detect_sensitive(f"rotate credentials in {isolated_env}/other/.env")


def test_plain_directory_pattern_covers_files_beneath_it(tmp_path):
    finance = tmp_path / "Documents" / "finance"
    patterns = [str(finance)]
    assert names_sensitive_path(f"summarise {finance}/ledger-2024.csv please", patterns)
    assert names_sensitive_path(f"open {finance}", patterns)
    assert not names_sensitive_path(f"summarise {tmp_path}/Documents/public/x.csv", patterns)


def test_tilde_patterns_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    allowlist = tmp_path / "sensitive.toml"
    allowlist.write_text('paths = ["~/dev/clients/*"]\n', encoding="utf-8")
    patterns = load_sensitive_patterns(allowlist)
    assert patterns == [str(tmp_path / "dev" / "clients" / "*")]
    assert names_sensitive_path(f"edit {tmp_path}/dev/clients/acme/main.py", patterns)


def test_missing_allowlist_file_means_no_patterns(isolated_env):
    assert load_sensitive_patterns() == []
