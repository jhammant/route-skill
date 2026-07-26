"""SPEC tests 6-7: federation — counts only, k-anonymity, merging, discounts."""

from __future__ import annotations

import json

import pytest

from route.federate import (
    COMMUNITY_DISCOUNT,
    CONTRIBUTION_CAP,
    K_ANONYMITY,
    TEAM_DISCOUNT,
    FederationState,
    aggregate,
    assert_payload_clean,
    export,
    merge_exports,
    pull,
    push,
)
from route.outcomes import record_outcome
from route.router import Router

# Task texts, file paths and repo names that must NEVER appear in a payload.
FORBIDDEN = [
    "refactor the billing service to use Stripe webhooks",
    "/Users/alice/work/secret-repo",
    "secret-repo",
    "billing-service",
]


def _route_task(router, shape, tier, eligible, chosen, outcome="accepted"):
    router.select(shape, tier, eligible)
    record_outcome(router, shape, tier, chosen, outcome)


def _fed_with_contributors(tmp_path, context, n):
    fed = FederationState(tmp_path / "fed.json")
    fed.data["cells"][context] = {"contributors": n, "arms": {}}
    fed.save()
    return fed


def test_export_emits_counts_only(router, pools, tmp_path):
    """Privacy: no task text, file paths, or repo names in the payload."""
    # Route tasks whose TEXT contains the forbidden strings. Only enum-derived
    # counts may reach the payload.
    _route_task(router, "coding:refactor", "hard", ["claude", "codex", "kimi"], "codex")
    _route_task(router, "coding:implement", "moderate", ["claude", "codex"], "claude",
                outcome="verified")

    fed = _fed_with_contributors(tmp_path, "coding:refactor:hard", K_ANONYMITY - 1)
    payload = export(router, pools, fed, k=1)  # k=1: test content, not anonymity

    blob = json.dumps(payload)
    for forbidden in FORBIDDEN:
        assert forbidden not in blob

    for rec in payload["records"]:
        assert set(rec.keys()) == {"schema", "context", "arm", "model", "alpha", "beta"}
    assert_payload_clean(payload)  # the assert-level guarantee itself


def test_payload_clean_rejects_smuggled_content():
    with pytest.raises(AssertionError):
        assert_payload_clean({"records": [{
            "schema": 1, "context": "coding:refactor:hard", "arm": "codex",
            "model": "../secret-repo", "alpha": 1, "beta": 1,
        }]})
    with pytest.raises(AssertionError):
        assert_payload_clean({"records": [{
            "schema": 1, "context": "coding:refactor:hard", "arm": "codex",
            "model": "gpt-5.6-sol", "alpha": 1, "beta": 1,
            "task": "refactor the billing service",
        }]})


def test_k_anonymity_suppresses_cells_below_three_contributors(router, pools, tmp_path):
    _route_task(router, "coding:refactor", "hard", ["claude", "codex"], "codex")
    fed = FederationState(tmp_path / "fed.json")  # fresh: 1 contributor (self)
    payload = export(router, pools, fed)
    assert payload["records"] == []
    assert payload["suppressed"] == [{
        "context": "coding:refactor:hard",
        "reason": f"k-anonymity: 1 contributor(s) < {K_ANONYMITY}",
    }]

    # Once the cell has K contributors, it publishes.
    fed = _fed_with_contributors(tmp_path, "coding:refactor:hard", K_ANONYMITY - 1)
    payload = export(router, pools, fed)
    assert payload["records"]
    assert {r["context"] for r in payload["records"]} == {"coding:refactor:hard"}
    assert payload["suppressed"] == []


def test_merging_two_exports_sums_alpha_and_beta(router, pools, tmp_path):
    """Federation is summation: alpha=sigma, beta=sigma."""
    # Exactly one observation on one arm: a single accepted reward, no
    # impressions recorded, so each export carries exactly one record.
    router.select("coding:refactor", "hard", ["claude", "codex"], record_impression=False)
    router.reward("coding:refactor", "hard", "codex", "accepted")
    payload_a = export(router, pools, FederationState(tmp_path / "a.json"), k=1)
    payload_b = export(router, pools, FederationState(tmp_path / "b.json"), k=1)

    rec = payload_a["records"][0]
    merged = merge_exports(payload_a, payload_b)
    assert len(merged["records"]) == 1
    assert merged["records"][0]["alpha"] == 2 * rec["alpha"]
    assert merged["records"][0]["beta"] == 2 * rec["beta"]
    assert merged["meta"]["contributors"]["coding:refactor:hard"] == 2
    assert_payload_clean(merged)


def test_per_installation_contribution_is_capped(router, pools, tmp_path):
    for _ in range(CONTRIBUTION_CAP + 100):
        router.reward("coding:refactor", "hard", "codex", "accepted")
    payload = export(router, pools, FederationState(tmp_path / "fed.json"), k=1)
    rec = payload["records"][0]
    assert rec["alpha"] + rec["beta"] <= CONTRIBUTION_CAP


def test_community_counts_discounted_04_before_merging(tmp_path):
    source = tmp_path / "community.json"
    source.write_text(json.dumps({
        "schema": 1,
        "records": [{
            "schema": 1, "context": "coding:refactor:hard", "arm": "codex",
            "model": "gpt-5.6-sol", "alpha": 10, "beta": 5,
        }],
        "meta": {"contributors": {"coding:refactor:hard": 50}},
    }))
    fed = FederationState(tmp_path / "fed.json")
    result = pull(str(source), fed, trust="community")
    assert result["discount"] == COMMUNITY_DISCOUNT == 0.4

    arm = fed.data["cells"]["coding:refactor:hard"]["arms"]["codex"]
    assert arm["alpha"] == pytest.approx(4.0)   # 10 * 0.4
    assert arm["beta"] == pytest.approx(2.0)    # 5 * 0.4
    assert fed.contributors("coding:refactor:hard") == 50

    # Team counts get the gentler 0.7x discount.
    fed2 = FederationState(tmp_path / "fed2.json")
    pull(str(source), fed2, trust="team")
    arm2 = fed2.data["cells"]["coding:refactor:hard"]["arms"]["codex"]
    assert arm2["alpha"] == pytest.approx(7.0)  # 10 * 0.7
    assert TEAM_DISCOUNT == 0.7


def test_pulled_priors_seed_the_router(router, tmp_path):
    """Community prior, local posterior: pulled counts become prior strength."""
    source = tmp_path / "community.json"
    source.write_text(json.dumps({
        "schema": 1,
        "records": [{
            "schema": 1, "context": "coding:refactor:hard", "arm": "kimi",
            "model": "kimi", "alpha": 100, "beta": 5,
        }],
    }))
    fed = FederationState(tmp_path / "fed.json")
    pull(str(source), fed)

    seeded = Router(storage=router.storage, priors=router.priors,
                    community=fed.community_priors())
    seeded.select("coding:refactor", "hard", ["claude", "codex", "kimi"],
                  record_impression=False)
    alpha, beta = seeded.posterior("coding:refactor", "hard")["kimi"]
    assert alpha == pytest.approx(2.0 + 40.0)   # rule prior 2 + 100 * 0.4
    assert beta == pytest.approx(8.0 + 2.0)     # rule prior 8 + 5 * 0.4


def test_push_is_opt_in_and_strips_meta(router, pools, tmp_path):
    _route_task(router, "coding:refactor", "hard", ["claude", "codex"], "codex")
    fed = FederationState(tmp_path / "fed.json")
    payload = export(router, pools, fed, k=1)

    with pytest.raises(PermissionError):
        push(payload, fed, opt_in=False)

    out = push(payload, fed, opt_in=True, outbox=tmp_path / "submission.json")
    shared = json.loads(out.read_text())
    assert "meta" not in shared
    assert shared["records"] == payload["records"]


# -- aggregate: the publishable community prior --------------------------------


def _rec(alpha, beta, context="coding:refactor:hard", arm="codex", model="gpt-5.6-sol"):
    return {"schema": 1, "context": context, "arm": arm, "model": model,
            "alpha": alpha, "beta": beta}


def _write_export(path, records):
    path.write_text(json.dumps({"schema": 1, "kind": "routing-quality",
                                "records": records}))


def test_aggregate_k_anonymity_suppresses_cells_below_three_contributors(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    _write_export(d / "a.json", [_rec(4, 1)])
    _write_export(d / "b.json", [_rec(3, 2)])
    payload = aggregate(d)
    assert payload["records"] == []
    assert payload["suppressed"] == [{
        "context": "coding:refactor:hard",
        "reason": f"k-anonymity: 2 contributor(s) < {K_ANONYMITY}",
    }]

    _write_export(d / "c.json", [_rec(5, 1)])
    payload = aggregate(d)
    assert len(payload["records"]) == 1
    assert payload["suppressed"] == []
    assert payload["meta"]["contributors"]["coding:refactor:hard"] == 3


def test_aggregate_one_installation_cannot_dominate(tmp_path):
    """Trimmed-mean rates + median totals: an outlier with huge counts loses."""
    d = tmp_path / "exports"
    d.mkdir()
    _write_export(d / "dominant.json", [_rec(10000, 100)])  # 99% at 10100 counts
    for i, (a, b) in enumerate([(4, 6), (5, 5), (3, 7)]):
        _write_export(d / f"user{i}.json", [_rec(a, b)])

    payload = aggregate(d)
    (rec,) = payload["records"]
    rate = rec["alpha"] / (rec["alpha"] + rec["beta"])
    # The 99% outlier is trimmed away; the published rate is the crowd's.
    assert rate == pytest.approx(0.45, abs=0.01)
    assert rate < 0.6
    # And its mountain of counts is gone: total reflects the median
    # contributor, far below even the per-installation cap.
    assert rec["alpha"] + rec["beta"] == pytest.approx(10)
    assert rec["alpha"] + rec["beta"] <= CONTRIBUTION_CAP


def test_aggregate_caps_each_contributor_before_combining(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    for i in range(3):
        _write_export(d / f"user{i}.json", [_rec(CONTRIBUTION_CAP * 10, 0)])
    payload = aggregate(d)
    (rec,) = payload["records"]
    assert rec["alpha"] + rec["beta"] <= CONTRIBUTION_CAP


def test_cli_federate_merge_sums_counts(tmp_path, capsys):
    from route.cli import main

    a, b = tmp_path / "a.json", tmp_path / "b.json"
    _write_export(a, [_rec(4, 1)])
    _write_export(b, [_rec(3, 2)])
    assert main(["federate", "merge", str(a), str(b)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["records"][0]["alpha"] == 7
    assert out["records"][0]["beta"] == 3


def test_cli_federate_aggregate_writes_out_file(tmp_path, capsys):
    from route.cli import main

    d = tmp_path / "exports"
    d.mkdir()
    for i, (x, y) in enumerate([(4, 1), (5, 1), (3, 2)]):
        _write_export(d / f"user{i}.json", [_rec(x, y)])
    out_file = tmp_path / "community.json"
    assert main(["federate", "aggregate", str(d), "--out", str(out_file)]) == 0
    payload = json.loads(out_file.read_text())
    assert len(payload["records"]) == 1
    assert_payload_clean(payload)
