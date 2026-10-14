"""Graded feedback replaces a decision's reward without inventing trials."""

import json

import pytest

from route.cli import main, main_auto
from route.memory import TaskMemory
from route.outcomes import record_score, score_from_fix_passes
from route.router import Router, cell_name
from route.stats import Stats
from route.storage import JsonStorage

SHAPE, TIER, ARM = "coding:test", "hard", "codex"
ROUTE = ["--host", "claude", "--no-llm", "--no-quota", "--shape", SHAPE,
         "--tier", TIER, "--pool", ARM]
OUTCOME = ["outcome", "--shape", SHAPE, "--tier", TIER, "--arm", ARM]


def dispatch(monkeypatch, capsys, *, rc=0, task="write parser tests"):
    monkeypatch.setattr("route.cli._dispatch", lambda *a, **k: rc)
    assert main_auto([*ROUTE, task]) == rc
    return json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]


@pytest.mark.parametrize("score", [0.0, 0.4, 0.7, 1.0])
def test_fractional_score_adds_exactly_one_beta_trial(router, score):
    # Arrange
    prior = router.priors.prior(SHAPE, TIER, ARM)

    # Act
    record_score(router, SHAPE, TIER, ARM, score)

    # Assert
    assert router.posterior(SHAPE, TIER)[ARM] == pytest.approx((prior[0] + score, prior[1] + 1 - score))
    assert router.observed_counts(SHAPE, TIER)[ARM] == pytest.approx((score, 1 - score))


@pytest.mark.parametrize("score", [-0.1, 1.1, float("nan"), float("inf"), True])
def test_invalid_score_cannot_change_counts(router, score):
    # Arrange / Act / Assert
    with pytest.raises(ValueError, match="score"):
        record_score(router, SHAPE, TIER, ARM, score)
    assert router.storage.keys() == []


@pytest.mark.parametrize("passes,abandoned,expected", [
    (0, False, 1.0), (1, False, 0.7), (2, False, 0.4),
    (3, False, 0.1), (4, False, 0.0), (50, False, 0.0),
    (0, True, 0.0), (2, True, 0.0),
])
def test_fix_pass_mapping(passes, abandoned, expected):
    # Arrange / Act
    score = score_from_fix_passes(passes, abandoned=abandoned)

    # Assert
    assert score == expected


@pytest.mark.parametrize("passes", [-1, 1.5, True])
def test_invalid_fix_passes(passes):
    # Arrange / Act / Assert
    with pytest.raises(ValueError, match="non-negative integer"):
        score_from_fix_passes(passes)


def test_score_supersedes_completed_and_repeated_calls_are_idempotent(monkeypatch, capsys):
    # Arrange
    plan = dispatch(monkeypatch, capsys)
    command = [*OUTCOME, "--score", "0.7"]

    # Act
    assert main(command) == main(command) == 0

    # Assert
    router = Router()
    assert router.observed_counts(SHAPE, TIER)[ARM] == pytest.approx((0.7, 0.3))
    assert router.storage.counts(f"{cell_name(SHAPE, TIER)}:completed") == {ARM: 1}
    assert Stats().decision(plan["decision_id"])["final_score"] == 0.7
    assert TaskMemory().rows()[0]["score"] == 0.7


@pytest.mark.parametrize("legacy", ["accepted", "verified", "failed"])
def test_grade_replaces_legacy_credit_and_is_authoritative(monkeypatch, capsys, legacy):
    # Arrange
    plan = dispatch(monkeypatch, capsys)
    identity = ["outcome", "--decision", plan["decision_id"]]
    main([*identity, "--outcome", legacy])

    # Act
    main([*identity, "--score", "0.4"])
    main([*identity, "--outcome", "accepted"])
    main([*identity, "--score", "0.4"])

    # Assert
    assert Router().observed_counts(SHAPE, TIER)[ARM] == pytest.approx((0.4, 0.6))
    assert TaskMemory().rows()[0]["score"] == 0.4


def test_settle_uses_id_after_another_dispatch_and_updates_a_previous_grade(monkeypatch, capsys):
    # Arrange
    first = dispatch(monkeypatch, capsys, task="write parser tests")
    second = dispatch(monkeypatch, capsys, task="write invoice tests")

    # Act
    command = ["settle", "--decision", first["decision_id"], "--merged"]
    assert main([*command, "--fix-passes", "1"]) == 0
    assert main([*command, "--fix-passes", "2"]) == 0
    assert main([*command, "--fix-passes", "2"]) == 0

    # Assert
    settled = Stats().decision(first["decision_id"])
    untouched = Stats().decision(second["decision_id"])
    assert settled["final_score"] == 0.4 and settled["merged"] is True
    assert settled["outcome_events"] == ["accepted", "completed", "graded", "verified"]
    assert untouched["final_score"] is None and untouched["outcome_events"] == ["completed"]
    assert Router().observed_counts(SHAPE, TIER)[ARM] == pytest.approx((0.4, 1.6))
    assert [r["score"] for r in TaskMemory().rows()] == [0.4, None]


@pytest.mark.parametrize("flag", ["--abandoned", "--rewritten"])
def test_abandon_or_rewrite_is_zero(monkeypatch, capsys, flag):
    # Arrange
    plan = dispatch(monkeypatch, capsys)

    # Act
    assert main(["settle", "--decision", plan["decision_id"], "--fix-passes", "0", flag]) == 0

    # Assert
    assert Router().observed_counts(SHAPE, TIER)[ARM] == (0, 1)
    assert TaskMemory().rows()[0]["score"] == 0


@pytest.mark.parametrize("pin", ["codex", "claude"])
def test_plan_is_pending_until_settled_even_when_pinned_or_staying(capsys, pin):
    # Arrange
    route = [*ROUTE[:-1], pin, "--plan-only", "write parser tests"]

    # Act
    assert main(route) == 0
    plan = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]

    # Assert pending plans are not negative observations or neighbours.
    assert Router().observed_counts(SHAPE, TIER) == {}
    assert TaskMemory().neighbours("write parser tests", SHAPE, [pin]) == []

    # Act / Assert settlement creates one seeded trial for external callers.
    assert main(["settle", "--decision", plan["decision_id"], "--fix-passes", "0"]) == 0
    assert Router().observed_counts(SHAPE, TIER)[pin] == (1, 0)


def test_declined_dispatch_does_not_count_as_failure(monkeypatch, capsys):
    # Arrange
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    monkeypatch.setattr("route.cli._dispatch", lambda *a, **k: pytest.fail("must not launch"))

    # Act
    assert main([*ROUTE, "write parser tests"]) == 0

    # Assert
    assert Router().observed_counts(SHAPE, TIER) == {}
    assert Stats().decisions()[-1]["impression_recorded"] is False


def test_legacy_outcomes_still_skip_already_recorded_events(monkeypatch, capsys):
    # Arrange
    dispatch(monkeypatch, capsys)

    # Act
    main([*OUTCOME, "--outcome", "verified"])
    main([*OUTCOME, "--outcome", "accepted"])
    main([*OUTCOME, "--outcome", "accepted"])

    # Assert
    counts = JsonStorage()
    cell = cell_name(SHAPE, TIER)
    assert counts.counts(f"{cell}:completed")[ARM] == 1
    assert counts.counts(f"{cell}:verified")[ARM] == 1
    assert Router().observed_counts(SHAPE, TIER)[ARM] == (1, 0)
    assert TaskMemory().rows()[0]["score"] == 1


def test_repeated_failure_after_failed_dispatch_does_not_add_trials(monkeypatch, capsys):
    # Arrange
    dispatch(monkeypatch, capsys, rc=1)

    # Act
    main([*OUTCOME, "--outcome", "failed"])
    main([*OUTCOME, "--outcome", "failed"])

    # Assert
    assert Router().observed_counts(SHAPE, TIER)[ARM] == (0, 1)
    assert TaskMemory().rows()[0]["score"] == 0


def test_manual_grade_without_decision_seeds_a_trial(capsys):
    # Arrange / Act
    assert main([*OUTCOME, "--fix-passes", "1"]) == 0

    # Assert
    assert Router().posterior(SHAPE, TIER)[ARM] == pytest.approx((8.7, 2.3))


@pytest.mark.parametrize("flags", [
    ["--score", "nan"], ["--score", "inf"], ["--score", "-0.1"], ["--score", "1.1"],
    ["--fix-passes", "-1"], ["--fix-passes", "1.5"], ["--score", "1", "--outcome", "accepted"],
    ["--score", "1", "--abandoned"], ["--outcome", "accepted", "--fix-passes", "1"], [],
])
def test_bad_feedback_is_rejected_before_writes(flags):
    # Arrange / Act / Assert
    with pytest.raises(SystemExit) as exc:
        main([*OUTCOME, *flags])
    assert exc.value.code == 2
    assert JsonStorage().keys() == []
    assert Stats().decisions() == []


def test_unknown_id_or_mismatched_identity_cannot_modify_a_decision(monkeypatch, capsys):
    # Arrange
    plan = dispatch(monkeypatch, capsys)
    before = Router().observed_counts(SHAPE, TIER)

    # Act
    assert main(["settle", "--decision", "missing", "--fix-passes", "0"]) == 2
    assert main(["outcome", "--decision", plan["decision_id"], "--arm", "kimi", "--score", "1"]) == 2

    # Assert
    assert Router().observed_counts(SHAPE, TIER) == before
    assert Stats().decision(plan["decision_id"])["final_score"] is None


def test_old_log_row_can_be_graded_without_changing_newer_decision(monkeypatch, capsys):
    # Arrange
    router = Router()
    router.observe(SHAPE, TIER, ARM, [ARM])
    old = {"cell": cell_name(SHAPE, TIER), "shape": SHAPE, "tier": TIER,
           "chosen": ARM, "why": "prior", "outcome_events": ["completed"]}
    stats = Stats()
    stats.decisions_path.parent.mkdir(parents=True, exist_ok=True)
    stats.decisions_path.write_text(json.dumps(old) + "\n")
    main([*ROUTE[:-1], "kimi", "--plan-only", "write unrelated tests"])

    # Act
    assert main([*OUTCOME, "--score", "0.7"]) == 0

    # Assert
    rows = stats.decisions()
    assert rows[0]["decision_id"] and rows[0]["final_score"] == 0.7
    assert rows[1]["final_score"] is None
    assert Router().observed_counts(SHAPE, TIER)[ARM] == pytest.approx((0.7, 0.3))


def test_auto_completion_preserves_feedback_received_while_dispatch_runs(monkeypatch, capsys):
    # Arrange: simulate a caller settling this decision and creating another
    # plan while the external agent is still running.
    def finish_while_running(*args, **kwargs):
        decision_id = Stats().decisions()[-1]["decision_id"]
        assert main(["settle", "--decision", decision_id, "--fix-passes", "1", "--merged"]) == 0
        assert main([*ROUTE, "--plan-only", "write later tests"]) == 0
        return 0

    monkeypatch.setattr("route.cli._dispatch", finish_while_running)

    # Act
    assert main_auto([*ROUTE, "write parser tests"]) == 0

    # Assert
    assert Router().observed_counts(SHAPE, TIER)[ARM] == pytest.approx((0.7, 0.3))
    assert JsonStorage().counts(f"{cell_name(SHAPE, TIER)}:completed")[ARM] == 1
    first, second = Stats().decisions()
    assert first["final_score"] == 0.7 and first["wall_clock_s"] is not None
    assert second["outcome_events"] == [] and second["wall_clock_s"] is None
