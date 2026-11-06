"""Independent review repros. All dispatches use the branch's fake model scripts."""

from __future__ import annotations

import json
import re

import pytest

from route import ultra, ultra_team
from route.pools import load_pools, pool_family, pools_for_host
from route.storage import state_dir
from tests import test_ultra_team as fixtures
from tests.test_ultra_team import fake as fake


def test_verifier_family_follows_the_successful_retry(fake):
    # Arrange: the original Claude analyst can only deliver its answer on Codex.
    fixtures.write_layouts()
    fixtures.standard_analyses(fake)
    (fake / "analyses" / "seo.bad-claude-work").touch()

    # Act: use the real dispatcher and retry, then inspect the resulting claim.
    rc = ultra_team.run_team(fixtures.QUESTION, layout="site-check", rounds=0, host="claude")

    # Assert: independence must be measured against the arm that actually answered.
    summary, _ = fixtures.latest_run()
    assert rc == 0
    pools = pools_for_host(load_pools(), "claude")
    answered_arms = {row["id"]: row["arm"] for row in summary["analysts"]}
    claim = next(row for row in summary["claims"] if row["author"] == "seo")
    assert answered_arms["seo"] == "codex"
    for verdict in claim["verdicts"]:
        assert pool_family(pools[verdict["arm"]]) != pool_family(pools[answered_arms["seo"]]), (
            "The retry's own model family verified its claim while being labelled cross-family",
            claim,
        )


def test_cross_family_verifiers_are_preferred_for_every_vote(fake, tmp_path):
    # Arrange: the author has two Anthropic arms and one available OpenAI arm.
    fixtures.write_layouts()
    fixtures.standard_analyses(fake)
    fixtures.write_pools(tmp_path, tmp_path / "bin", fable=True)
    (fake / "verdicts" / "C1-codex.json").write_text(json.dumps({
        "verdict": "refuted", "reason": "The sitemap line is already present", "sources": [],
    }))

    # Act: request the documented three-verifier review.
    rc = ultra_team.run_team(fixtures.QUESTION, layout="site-check", intensity="full", rounds=0, host="claude")

    # Assert: same-family is a fallback when no cross-family arm is usable.
    summary, _ = fixtures.latest_run()
    assert rc == 0
    claim = next(row for row in summary["claims"] if row["id"] == "C1")
    assert len(claim["verdicts"]) == 3
    assert all(row["independence"] == "cross-family" for row in claim["verdicts"]), claim
    assert claim["verdict"] == "refuted"


def test_critic_new_evidence_invalidates_the_cached_verdict(fake):
    # Arrange: initial evidence is inconclusive; a critic analyst finds a concrete repro.
    fixtures.write_layouts()
    claim_text = "The admin endpoint accepts unauthenticated write requests"
    fixtures.write_analysis(fake, "seo", [fixtures.finding("F1", claim_text, severity="high")])
    fixtures.write_analysis(fake, "perf", [])
    fixtures.write_analysis(fake, "copy", [])
    fixtures.write_analysis(fake, "security", [fixtures.finding(
        "F1", claim_text, severity="blocker", evidence="The reproduction returns HTTP 200 without a token",
        sources=[{"id": "S1", "ref": "confirmed-admin-repro.py", "note": "repeatable exploit"}],
    )])
    (fake / "critic-1.json").write_text(json.dumps({"missing": [{
        "id": "security", "role": "Security analyst", "lens": "authentication",
        "brief": "Reproduce the suspected unauthenticated write",
    }]}))
    (fake / "report.md").write_text("Audit completed.\n")
    response = r'''
    if [ "$claim" = "C1" ]; then
      echo ULTRA-VERDICT-BEGIN
      case "$prompt" in
        *confirmed-admin-repro.py*)
          echo '{"verdict":"upheld","reason":"The supplied exploit reproduces the bug","sources":[]}' ;;
        *)
          echo '{"verdict":"refuted","reason":"No concrete reproduction was supplied","sources":[]}' ;;
      esac
      echo ULTRA-VERDICT-END
      exit 0
    fi
'''
    for name in ("claude-work", "codex"):
        script = fake.parent / "bin" / name
        script.write_text(script.read_text().replace("  verifier)\n", "  verifier)\n" + response, 1))

    # Act: merge the additional evidence and run cross-examination again.
    rc = ultra_team.run_team(fixtures.QUESTION, layout="site-check", rounds=1, host="claude")

    # Assert: the old evidence-free refutation cannot decide the strengthened claim.
    summary, _ = fixtures.latest_run()
    assert rc == 0
    [claim] = summary["claims"]
    assert claim["supporters"] == ["seo", "security"]
    assert claim["verdict"] == "upheld", claim
    assert len([row for row in fixtures.calls(fake) if row[1:3] == ["verifier", "C1"]]) == 2


def test_merging_keeps_opposite_claims_separate():
    # Arrange: analysts disagree about the same endpoint, rather than corroborating it.
    analyses = {
        "security": ultra_team.Analysis("Checked the API", [ultra_team.Finding(
            "F1", "The admin endpoint requires authentication before accepting write requests", "401", severity="info",
        )]),
        "adversary": ultra_team.Analysis("Checked a bypass", [ultra_team.Finding(
            "F1", "The admin endpoint does not require authentication before accepting write requests", "200",
            severity="blocker",
        )]),
    }

    # Act.
    claims = ultra_team.merge_findings(analyses)

    # Assert: a contradictory blocker must not become support for the opposite claim.
    assert len(claims) == 2, [claim.to_dict() for claim in claims]
    assert [claim.supporters for claim in claims] == [["security"], ["adversary"]]


@pytest.mark.parametrize("missing", ["gaps", "evidence", "sources", "id", "fix"])
def test_incomplete_analysis_is_retried_instead_of_accepted(fake, missing):
    # Arrange: both arms return the same incomplete output envelope.
    fixtures.write_layouts()
    fixtures.standard_analyses(fake)
    path = fake / "analyses" / "seo.json"
    raw = json.loads(path.read_text())
    if missing == "gaps":
        del raw[missing]
    else:
        del raw["findings"][0][missing]
    path.write_text(json.dumps(raw))

    # Act.
    rc = ultra_team.run_team(fixtures.QUESTION, layout="site-check", rounds=0, host="claude")

    # Assert: required analysis fields must not be silently fabricated by the parser.
    summary, _ = fixtures.latest_run()
    assert rc == 1, f"The incomplete {missing!r} envelope was counted as an answered analyst"
    seo = next(row for row in summary["analysts"] if row["id"] == "seo")
    assert seo["state"] == "failed"
    assert seo["attempts"] == ["claude-work", "codex"]


@pytest.mark.parametrize("malformed", [
    {"verdict": "refuted"},
    {"verdict": "refuted", "reason": ["not a string"], "sources": []},
    {"verdict": "refuted", "reason": "Checked", "sources": "not a list"},
])
def test_malformed_verdict_cannot_decide_a_claim(fake, malformed):
    # Arrange: the verifier supplies valid JSON but not the required verdict schema.
    fixtures.write_layouts()
    fixtures.standard_analyses(fake)
    (fake / "verdicts" / "C1.json").write_text(json.dumps(malformed))

    # Act.
    rc = ultra_team.run_team(fixtures.QUESTION, layout="site-check", rounds=0, host="claude")

    # Assert: an unusable verification must produce uncertainty, not reject the finding.
    summary, _ = fixtures.latest_run()
    assert rc == 0
    claim = next(row for row in summary["claims"] if row["id"] == "C1")
    assert claim["verdict"] == "contested", claim
    assert claim["verdicts"][0]["verdict"] == "uncertain"


def test_conflicting_source_ids_do_not_silently_drop_a_reference(fake):
    # Arrange: the finding-local source lists both use S1 for different references.
    fixtures.write_layouts()
    fixtures.standard_analyses(fake)
    path = fake / "analyses" / "perf.json"
    raw = json.loads(path.read_text())
    raw["findings"][1]["sources"][0]["id"] = "S1"
    path.write_text(json.dumps(raw))
    (fake / "report.md").write_text("Mobile loading is slow [perf:S1].\n")

    # Act: build the per-analyst citation index and final report.
    rc = ultra_team.run_team(fixtures.QUESTION, layout="site-check", rounds=0, host="claude")

    # Assert: either retry/reject ambiguous citations or normalize them without loss.
    summary, run_dir = fixtures.latest_run()
    perf = next(row for row in summary["analysts"] if row["id"] == "perf")
    if perf["state"] == "failed":
        assert rc == 1 and perf["attempts"] == ["claude-work", "codex"]
    else:
        assert rc == 0
        sources = (run_dir / "summary.md").read_text().split("## Sources", 1)[1].split("## Run", 1)[0]
        assert "lighthouse run: LCP 4.0s" in sources, "The second S1 reference disappeared from Sources"
        assert f"{fixtures.URL}/robots.txt: the robots file" in sources
        identifiers = re.findall(r"^- \[(perf:[^]]+)\]", sources, re.MULTILINE)
        assert len(identifiers) == len(set(identifiers)), "One citation label resolved to two different refs"


def test_interrupt_during_fix_preserves_team_interrupt_status(fake, tmp_path, monkeypatch):
    # Arrange: enter the real build wrapper, then interrupt its execution.
    repo = fixtures.git_repo(tmp_path)
    fixtures.write_layouts(fixtures.CODE_LAYOUT_TOML)
    fixtures.standard_analyses(fake)
    (fake / "fix.json").write_text(json.dumps({"goal": "Repair the code", "goal_check": "pytest -q"}))

    def interrupt_build(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ultra, "_execute", interrupt_build)

    # Act: run_ultra catches the interrupt and returns its documented exit code 130.
    rc = ultra_team.run_team(fixtures.QUESTION, layout="code-check", repo=repo, fix=True, rounds=0, host="claude")

    # Assert: the containing team operation must remain interrupted as well.
    progress = [json.loads(path.read_text()) for path in (state_dir() / "ultra").glob("*/progress.json")]
    team = next(row for row in progress if row.get("mode") == "team")
    assert rc == 130, f"Ctrl-C became ordinary failure {rc}"
    assert team["state"] == "interrupted" and team["exit_code"] == 130
