"""The planner contract: parse strictly, lint every rule at once, merge overlapping units."""

from __future__ import annotations

import json

import pytest

from route.ultra_plan import (
    MANIFEST_BEGIN,
    MANIFEST_END,
    Plan,
    PlanError,
    Unit,
    lint_plan,
    merge_overlaps,
    parse_plan,
    planner_prompt,
    to_manifest,
)


def marked(payload, *, prefix="Some reasoning first.\n", suffix="\n"):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return f"{prefix}{MANIFEST_BEGIN}\n{body}\n{MANIFEST_END}{suffix}"


def raw_unit(identity="alpha", **overrides):
    return {"id": identity, "task": f"Build {identity}", "check": f"pytest -q tests/test_{identity}.py",
            "touches": [f"src/{identity}.py", f"tests/test_{identity}.py"], **overrides}


def raw_plan(*units, **overrides):
    return {"name": "Build", "shared": "Keep it small.", "goal_check": "pytest -q",
            "units": list(units) or [raw_unit()], **overrides}


# -- planner_prompt ------------------------------------------------------------

def test_planner_prompt_carries_the_rules_and_the_markers():
    prompt = planner_prompt("Add rate limiting", repo="/r", base="abc123", max_units=4, context_note="Read digest.md")
    assert "Add rate limiting" in prompt and "/r at revision abc123" in prompt
    assert "at most 4 INDEPENDENT units" in prompt and "Read digest.md" in prompt
    assert "read-only" in prompt and "python3 -m pytest" in prompt
    assert prompt.rstrip().endswith(MANIFEST_END) and prompt.count(MANIFEST_BEGIN) == 1


# -- parse_plan ----------------------------------------------------------------

def test_parse_takes_the_last_marked_block_and_strips_fences():
    first = marked(raw_plan(raw_unit("old")))
    second = marked("```json\n" + json.dumps(raw_plan(raw_unit("new"))) + "\n```")
    plan = parse_plan(first + second)
    assert [unit.id for unit in plan.units] == ["new"]
    assert plan.name == "Build" and plan.shared == "Keep it small." and plan.goal_check == "pytest -q"
    assert plan.units[0].touches == ["src/new.py", "tests/test_new.py"]
    assert plan.units[0].shape is None and plan.units[0].tier is None


def test_parse_keeps_optional_shape_and_tier_and_defaults_shared():
    payload = raw_plan(raw_unit(shape="coding:test", tier="hard"))
    del payload["shared"]
    plan = parse_plan(marked(payload))
    assert plan.shared == "" and plan.units[0].shape == "coding:test" and plan.units[0].tier == "hard"
    assert plan.to_dict()["units"][0] == {**raw_unit(), "shape": "coding:test", "tier": "hard"}


@pytest.mark.parametrize("text,reason", [
    ("no markers here", "no ULTRA-MANIFEST-BEGIN"),
    (f"{MANIFEST_BEGIN}\n{{}}", "no matching"),
    (marked("{not json"), "not valid JSON"),
    (marked("[1, 2]"), "must be a JSON object"),
    (marked({"name": "x", "goal_check": "pytest", "units": "nope"}), "'units' must be a list"),
    (marked(raw_plan(name=3)), "'name' must be a string"),
    (marked(raw_plan(raw_unit(task=["x"]))), "'task' must be a string"),
    (marked(raw_plan(raw_unit(touches="src"))), "'touches' must be a list"),
    (marked(raw_plan("just a string")), "must be an object"),
    (marked({"name": "x", "units": []}), "'goal_check' must be a string"),
])
def test_parse_errors_are_specific(text, reason):
    with pytest.raises(PlanError, match=reason):
        parse_plan(text)


# -- lint_plan -----------------------------------------------------------------

def test_lint_passes_a_sound_plan():
    plan = parse_plan(marked(raw_plan(raw_unit("alpha"), raw_unit("beta", shape="coding:implement", tier="moderate"))))
    assert lint_plan(plan, max_units=10) == []


@pytest.mark.parametrize("units,reason", [
    ([raw_unit("Alpha")], "id must match"),
    ([raw_unit("hub")], "not be 'hub'"),
    ([raw_unit("a"), raw_unit("a")], "duplicate id"),
    ([raw_unit(task="  ")], "task must be non-empty"),
    ([raw_unit(check="")], "check must be non-empty"),
    ([raw_unit(check="echo done")], "not an allowed check"),
    ([raw_unit(check="pytest -q; rm -rf /")], "shell operators"),
    ([raw_unit(check="curl http://example.com")], "not an allowed check"),
    ([raw_unit(touches=["/etc/passwd"])], "absolute path"),
    ([raw_unit(touches=["../other/file.py"])], "parent segment"),
    ([raw_unit(touches=[""])], "empty path"),
    ([raw_unit(shape="coding:dance")], "unknown shape"),
    ([raw_unit(tier="impossible")], "unknown tier"),
    ([], "no units"),
])
def test_lint_reports_each_unit_problem(units, reason):
    plan = parse_plan(marked(raw_plan(*units) if units else {**raw_plan(), "units": []}))
    problems = lint_plan(plan, max_units=10)
    assert any(reason in problem for problem in problems), problems


def test_lint_reports_plan_level_problems_together():
    plan = parse_plan(marked(raw_plan(raw_unit("a"), raw_unit("b"), raw_unit("c"), name=" ", goal_check="true")))
    problems = lint_plan(plan, max_units=2)
    assert [p.split(";")[0].split(":")[0] for p in problems] == ["name must be non-empty", "3 units", "goal_check 'true'"]


def test_lint_requires_a_goal_check():
    plan = parse_plan(marked(raw_plan(goal_check="")))
    assert lint_plan(plan, max_units=10) == ["goal_check must be non-empty"]


# -- merge_overlaps ------------------------------------------------------------

def test_disjoint_units_are_left_alone():
    plan = parse_plan(marked(raw_plan(raw_unit("alpha"), raw_unit("beta"))))
    merged = merge_overlaps(plan)
    assert merged == plan and merged is not plan


def test_same_path_and_nested_paths_merge_transitively_in_plan_order():
    plan = parse_plan(marked(raw_plan(
        raw_unit("api", touches=["src/api/"], check="pytest -q tests/api", tier="simple", shape="coding:implement"),
        raw_unit("docs", touches=["docs/"]),
        raw_unit("auth", touches=["src/api/auth.py"], check="pytest -q tests/auth.py", tier="hard",
                 shape="coding:implement"),
        raw_unit("models", touches=["./src/api/auth.py", "src/models.py"], check="pytest -q tests/api",
                 shape="coding:refactor"),
    )))
    merged = merge_overlaps(plan)
    assert [unit.id for unit in merged.units] == ["api-auth-models", "docs"]
    unit = merged.units[0]
    assert unit.task == "## api\nBuild api\n\n## auth\nBuild auth\n\n## models\nBuild models"
    assert unit.check == "pytest -q tests/api && pytest -q tests/auth.py"
    assert unit.touches == ["src/api/", "src/api/auth.py", "./src/api/auth.py", "src/models.py"]
    assert unit.tier == "hard" and unit.shape is None
    assert merged.units[1] == plan.units[1]


def test_repo_root_touch_merges_everything_and_long_ids_stay_valid():
    plan = parse_plan(marked(raw_plan(
        raw_unit("a-very-long-unit-identifier", touches=["."]),
        raw_unit("another-long-identifier", touches=["x/"]),
        raw_unit("third", touches=["y/"]),
    )))
    merged = merge_overlaps(plan)
    assert len(merged.units) == 1
    assert merged.units[0].id == "a-very-long-unit-identifier-anot"
    assert lint_plan(merged, max_units=10) == []


def test_merged_id_avoids_an_existing_id():
    plan = parse_plan(marked(raw_plan(
        raw_unit("a", touches=["s/"]), raw_unit("b", touches=["s/x"]), raw_unit("a-b", touches=["t/"]))))
    merged = merge_overlaps(plan)
    assert [unit.id for unit in merged.units] == ["a-b-2", "a-b"]


# -- to_manifest ---------------------------------------------------------------

def test_manifest_pins_each_unit_to_its_arm_and_drops_held_units():
    plan = Plan(name="Build", shared="S", goal_check="pytest -q", units=[
        Unit("alpha", "A", "pytest -q tests/a.py", ["a"], shape="coding:implement", tier="hard"),
        Unit("beta", "B", "pytest -q tests/b.py", ["b"]),
        Unit("held", "H", "pytest -q tests/h.py", ["h"]),
    ])
    manifest = to_manifest(plan, repo="/repo", base="abc", swarm_id="run-1",
                           arms={"alpha": "claude-work", "beta": "codex"})
    assert manifest == {"name": "Build", "repo": "/repo", "base": "abc", "shared": "S", "swarm_id": "run-1",
                        "units": [
                            {"id": "alpha", "task": "A", "check": "pytest -q tests/a.py", "pool": "claude-work",
                             "shape": "coding:implement", "tier": "hard"},
                            {"id": "beta", "task": "B", "check": "pytest -q tests/b.py", "pool": "codex"}]}
