"""SPEC tests 2-3: vetoes short-circuit; eligibility and quota gating."""

from __future__ import annotations

import pytest

from route.eligibility import apply_quota, detect_veto, eligible_for, gate, parse_quota
from route.shapes import SHAPES

BATCH = [s for s in SHAPES if s.startswith("batch:")]
CODING = [s for s in SHAPES if s.startswith("coding:")]

CRITICAL_QUOTA = {"claude": "critical", "codex": "critical", "kimi": "critical"}

VETO_CASES = [
    # (kwargs or text trigger, expected veto name)
    ("Pick up from what we were doing earlier in this conversation", {}, "needs-context"),
    ("Update the shared library across repos and bump every consumer", {}, "cross-repo"),
    ("Rotate the secrets in our .env files", {}, "private-data"),
    ("Clean this up — there is no clear acceptance check for it", {}, "no-acceptance"),
    ("anything", {"needs_context": True}, "needs-context"),
    ("anything", {"cross_repo": True}, "cross-repo"),
    ("anything", {"private": True}, "private-data"),
    ("anything", {"no_acceptance": True}, "no-acceptance"),
]


@pytest.mark.parametrize("text,flags,name", VETO_CASES)
def test_each_veto_short_circuits_to_claude(pools, text, flags, name):
    """Every veto -> claude, regardless of quota (all critical) or complexity."""
    result = gate(text, "coding:implement", pools, quota=CRITICAL_QUOTA, **flags)
    assert result.veto is not None and result.veto.name == name
    assert result.eligible == ["claude"]
    # Complexity is not even consulted: vetoes are evaluated before it.


def test_orchestration_shape_never_routes_away(pools):
    result = gate("Deploy the service and then update the DNS", "orchestration", pools,
                  quota=CRITICAL_QUOTA)
    assert result.veto is not None and result.veto.name == "orchestration"
    assert result.eligible == ["claude"]


def test_user_pin_beats_inference(pools):
    result = gate("implement a thing", "coding:implement", pools, pool_pin="kimi")
    assert result.veto is not None and result.veto.name == "user-pin"
    assert result.eligible == ["kimi"]


def test_veto_does_not_consult_quota(pools):
    """claude critical in quotamax cannot block a veto landing on claude."""
    result = gate("rotate the credentials", "coding:debug", pools,
                  quota={"claude": "critical"})
    assert result.eligible == ["claude"]


# -- eligibility ---------------------------------------------------------------


def test_batch_shapes_never_yield_codex_or_kimi(pools):
    for shape in BATCH:
        eligible = eligible_for(shape, pools)
        assert "codex" not in eligible, shape
        assert "kimi" not in eligible, shape
        assert "local-batch" in eligible, shape


def test_coding_shapes_never_yield_local_batch(pools):
    for shape in CODING:
        eligible = eligible_for(shape, pools)
        assert "local-batch" not in eligible, shape


def test_critical_headroom_removes_the_arm_entirely():
    eligible = ["claude", "codex", "kimi"]
    out = apply_quota(eligible, {"codex": "critical"})
    assert out == ["claude", "kimi"]
    # low headroom is a warning, not a removal
    assert apply_quota(eligible, {"codex": "low"}) == eligible


def test_local_arms_have_no_quota():
    out = apply_quota(["local-batch", "local-agent"], {"local-batch": "critical"})
    assert out == ["local-batch", "local-agent"]


def test_gate_removes_critical_and_reports_it(pools):
    result = gate("implement a handler", "coding:implement", pools,
                  quota={"codex": "critical", "kimi": "critical"})
    assert result.veto is None
    assert "codex" not in result.eligible and "kimi" not in result.eligible
    assert sorted(result.removed_by_quota) == ["codex", "kimi"]
    assert "claude" in result.eligible and "local-agent" in result.eligible


def test_no_survivors_stays_with_claude(pools):
    # Local arms have no quota and always survive, so use a registry of
    # quota-backed pools only: with everything critical, nothing survives.
    remote_only = {k: pools[k] for k in ("claude", "codex", "kimi")}
    result = gate("implement a handler", "coding:implement", remote_only, quota=CRITICAL_QUOTA)
    assert result.veto is not None and result.veto.name == "no-survivors"
    assert result.eligible == ["claude"]


def test_parse_quota_shapes():
    assert parse_quota({"codex": "critical"}) == {"codex": "critical"}
    assert parse_quota({"codex": {"headroom": "low"}}) == {"codex": "low"}
    assert parse_quota({"pools": {"kimi": {"status": "ok"}}}) == {"kimi": "ok"}
    assert parse_quota({}) == {}


def test_detect_veto_order_pin_first(pools):
    veto = detect_veto("secrets across repos", pool_pin="codex")
    assert veto is not None and veto.name == "user-pin" and veto.target == "codex"
