"""Tests for the /ultra-progress diagram (skill-ultra-progress/ultra_progress.py)."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skill-ultra-progress" / "ultra_progress.py"
spec = importlib.util.spec_from_file_location("ultra_progress", SCRIPT)
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)


def lane(unit, swarm=None, **progress):
    progress.setdefault("state", "running")
    cells, note = up.unit_lane({"id": "u1", **unit}, swarm or {}, progress)
    return dict(cells), note


def test_running_unit_is_building():
    # Arrange / Act
    cells, note = lane({"state": "planned"}, {"state": "running", "started_at": "2026-11-06T22:00:00Z"})
    # Assert
    assert cells["build"] == "active" and cells["check"] == "pending"
    assert note.startswith("building for")


def test_checking_unit_has_build_done():
    cells, _ = lane({"state": "planned"}, {"state": "checking"})
    assert cells["build"] == "done" and cells["check"] == "active"


def test_accepted_unit_under_review_is_active_only_for_its_own_step():
    mine, note = lane({"state": "accepted"}, state="reviewing", step="u1 by codex (cross-family)")
    other, _ = lane({"state": "accepted"}, state="reviewing", step="u2 by codex (cross-family)")
    assert mine["review"] == "active" and "codex" in note
    assert other["review"] == "pending"


def test_clean_review_skips_fix_once_integrating():
    cells, note = lane({"state": "accepted"}, state="integrating")
    assert cells["review"] == "done" and cells["fix"] == "skipped" and note == "review clean"


def test_finished_review_with_confirmed_bugs_queues_a_fix():
    cells, note = lane({"state": "accepted", "review": {"reviewer": "codex", "findings": 3,
                                                       "confirmed": [{"title": "Reserves double-counted"}]}},
                       state="reviewing", step="other by codex (cross-family)")
    assert cells["review"] == "done" and cells["fix"] == "pending"
    assert "1 confirmed bug(s) to fix: Reserves double-counted" == note


def test_finished_clean_review_skips_fix():
    cells, note = lane({"state": "accepted", "review": {"reviewer": "codex", "findings": 0, "confirmed": []}},
                       state="reviewing", step="other by codex")
    assert cells["review"] == "done" and cells["fix"] == "skipped" and note == "review clean"


def test_fixing_and_failed_after_fix():
    fixing, note = lane({"state": "fixing", "fix_passes": 2})
    assert fixing["fix"] == "active" and note == "fix pass 2"
    failed, note = lane({"state": "failed", "fix_passes": 2, "reason": "unresolved after fix passes"})
    assert failed["fix"] == "failed" and failed["merge"] == "pending"
    assert note == "unresolved after fix passes"


def test_check_failure_is_marked_on_check():
    cells, _ = lane({"state": "failed"}, {"state": "failed"})
    assert cells["check"] == "failed" and cells["build"] == "done"


def test_merged_and_conflict():
    merged, _ = lane({"state": "merged"}, state="done")
    assert all(v in ("done", "skipped") for v in merged.values())
    conflict, note = lane({"state": "conflict", "reason": "app.py"}, state="failed")
    assert conflict["merge"] == "failed" and "app.py" in note


def test_held_unit():
    cells, note = lane({"state": "held", "reason": "no usable model has quota"})
    assert set(cells.values()) == {"held"} and "quota" in note


def test_cancelled_run_turns_active_into_failed():
    cells, _ = lane({"state": "planned"}, {"state": "running"}, state="cancelled")
    assert cells["build"] == "failed" and cells["merge"] == "skipped"


def test_run_stages_follow_state():
    assert [s for _, _, s in up.run_stages({"state": "reviewing"})][:4] == ["done", "done", "done", "active"]
    assert all(s == "done" for _, _, s in up.run_stages({"state": "done"}))


def test_render_latest_run_from_state_dir(tmp_path, monkeypatch):
    # Arrange
    pytest.importorskip("rich")
    from rich.console import Console
    monkeypatch.setenv("ROUTE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    run = tmp_path / "ultra" / "goal-20261106"
    (run / "logs").mkdir(parents=True)
    (run / "progress.json").write_text(json.dumps({
        "run_id": "goal-20261106", "goal": "Build <it>. More detail.", "state": "running", "step": "swarm",
        "started_at": "2026-11-06T22:00:00Z", "planner": "claude-work-fable",
        "units": {"u1": {"id": "u1", "state": "planned", "arm": "claude-work-fable", "tier": "hard"}}}), encoding="utf-8")
    swarm = tmp_path / "swarms" / "goal-20261106"
    wt = swarm / "wt" / "u1"
    wt.mkdir(parents=True)
    (swarm / "status.json").write_text(json.dumps({"units": [{"id": "u1", "state": "running", "worktree": str(wt)}]}), encoding="utf-8")
    (swarm / "channel.jsonl").write_text(json.dumps({"at": "2026-11-06T22:05:00Z", "from_unit": "u1", "text": "u1 here"}) + "\n", encoding="utf-8")
    # A Claude worker transcript with one tool call and one sub-agent
    proj = up.project_dir(wt)
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "timestamp": "2026-11-06T22:01:00Z", "message": {"content": "do u1"}},
        {"type": "assistant", "timestamp": "2026-11-06T22:02:00Z", "message": {"id": "m1", "usage": {"output_tokens": 1500},
         "content": [{"type": "tool_use", "name": "mcp__amail__amail_inbox", "input": {}},
                     {"type": "tool_use", "name": "Bash", "input": {"description": "Run the tests"}}]}}]) + "\n", encoding="utf-8")
    (proj / "s1" / "subagents").mkdir(parents=True)
    (proj / "s1" / "subagents" / "agent-a.jsonl").write_text(json.dumps(
        {"type": "user", "timestamp": "2026-11-06T22:01:30Z", "message": {"content": "Explore the builder"}}) + "\n", encoding="utf-8")
    # Act
    model = up.collect(up.find_run(None))
    console = Console(width=120, record=True, color_system=None)
    console.print(up.render(model, 120))
    text = console.export_text()
    # Assert
    worker = model["units"][0]["agents"][0]
    assert worker["t"].tool_calls == 2 and worker["t"].tokens == 1500
    assert worker["subs"][0].prompt == "Explore the builder"
    assert "ULTRA goal-20261106" in text and "Build <it>." in text and "More detail" not in text
    assert "worker" in text and "fable · work" in text and "sub-agent Explore the builder" in text
    assert "amail_inbox" in text and "u1 here" in text
    assert not model["finished"]


def test_unknown_run_prefix_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTE_STATE_DIR", str(tmp_path))
    (tmp_path / "ultra" / "a-1").mkdir(parents=True)
    (tmp_path / "ultra" / "a-1" / "progress.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        up.find_run("zzz")


# ------------------------------------------------------------------ workflow view: totals, phases, log

RUN_ID = "goal-20261106"
BUG = {"title": "Off by one", "severity": "high"}


def write_run(tmp_path, monkeypatch, progress, swarm_units=(), logs=None):
    """A run under a throwaway state dir and HOME: progress.json, swarm status, run logs."""
    monkeypatch.setenv("ROUTE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    progress = {"run_id": RUN_ID, "goal": "Build it. More detail.", "started_at": "2026-11-06T22:00:00Z",
                "planner": "claude-work-fable", **progress}
    run = tmp_path / "ultra" / RUN_ID
    (run / "logs").mkdir(parents=True)
    for name, text in {"plan-1.log": "the plan", **(logs or {})}.items():
        (run / "logs" / name).write_text(text, encoding="utf-8")
    swarm = tmp_path / "swarms" / RUN_ID
    rows = []
    for row in swarm_units:
        (swarm / "wt" / row["id"]).mkdir(parents=True)
        rows.append({"arm": "claude-work-fable", "worktree": str(swarm / "wt" / row["id"]), **row})
    swarm.mkdir(parents=True, exist_ok=True)
    (swarm / "status.json").write_text(json.dumps({"units": rows}), encoding="utf-8")
    (run / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
    return swarm


def claude_transcript(cwd, name, start, tokens, tools=1, sub_tokens=None, prompt=None):
    """A Claude Code session in ``cwd`` with one assistant turn; optionally one sub-agent."""
    proj = up.project_dir(cwd)
    proj.mkdir(parents=True, exist_ok=True)
    calls = [{"type": "tool_use", "name": "Bash", "input": {"description": f"step {i}"}} for i in range(tools)]
    (proj / f"{name}.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "timestamp": f"2026-11-06T{start}:00Z", "message": {"content": prompt or f"{name} prompt"}},
        {"type": "assistant", "timestamp": f"2026-11-06T{start}:30Z",
         "message": {"id": f"{name}-m1", "usage": {"output_tokens": tokens}, "content": calls}}]) + "\n", encoding="utf-8")
    if sub_tokens is not None:
        (proj / name / "subagents").mkdir(parents=True)
        (proj / name / "subagents" / "agent-1.jsonl").write_text("\n".join(json.dumps(x) for x in [
            {"type": "user", "timestamp": f"2026-11-06T{start}:10Z", "message": {"content": "Explore the module"}},
            {"type": "assistant", "timestamp": f"2026-11-06T{start}:20Z", "message": {
                "id": f"{name}-s1", "usage": {"output_tokens": sub_tokens},
                "content": [{"type": "tool_use", "name": "Grep", "input": {"pattern": "def "}}]}}]) + "\n", encoding="utf-8")


def unit(uid, state="accepted", **fields):
    return {"id": uid, "state": state, "fix_passes": 0, "arm": "claude-work-fable", "tier": "moderate", **fields}


def agents(phase):
    return {a["label"]: a for a in phase["agents"]}


@pytest.fixture
def reviewing(tmp_path, monkeypatch):
    """Five units mid-review: u1 reviewed (1 confirmed bug), u2 under review, u3 waiting its turn,
    u4's review skipped, u5 held. u1's worker spent 1,500 tokens and its sub-agent 500."""
    swarm = write_run(tmp_path, monkeypatch, {
        "state": "reviewing", "step": "u2 by codex (cross-family)",
        "units": {"u1": unit("u1", review={"reviewer": "codex", "findings": 2, "confirmed": [BUG], "attempts": ["codex"]}),
                  "u2": unit("u2"), "u3": unit("u3"), "u4": unit("u4", review={"skipped": "trivial change"}),
                  "u5": unit("u5", "held", reason="no usable model has quota")}},
        [{"id": u, "state": "accepted", "started_at": "2026-11-06T22:01:00Z", "finished_at": "2026-11-06T22:09:00Z"}
         for u in ("u1", "u2", "u3", "u4")],
        logs={"review-u1-1.log": "verdict", "review-u2-1.log": ""})
    claude_transcript(swarm / "wt" / "u1", "w1", "22:02", 1500, tools=2, sub_tokens=500)
    return up.collect(up.find_run(None))


def test_json_adds_totals_phases_and_log_and_keeps_existing_keys(reviewing):
    # Arrange
    before = {"run", "state", "step", "finished", "exit_code", "elapsed", "updated_secs", "goal", "stages",
              "planners", "units", "activity", "messages", "headroom", "branch", "summary"}
    # Act
    data = json.loads(json.dumps(up.to_json(reviewing)))
    # Assert
    assert before <= set(data) and {"totals", "phases", "log"} <= set(data)
    assert [ph["title"] for ph in data["phases"]] == ["Plan", "Build", "Review", "Fix", "Integrate"]
    assert set(data["totals"]) == {"agents", "running", "done", "queued", "failed", "tokens", "tokens_h", "tools", "elapsed"}
    row = data["phases"][1]["agents"][0]
    assert set(row) == {"label", "unit", "role", "model", "state", "span", "tokens", "tokens_h", "tools", "now", "note", "subs"}
    assert row["label"] == "u1 · worker" and row["unit"] == "u1" and row["model"] == "fable · work"


def test_totals_count_named_agents_and_include_sub_agent_tokens(reviewing):
    # Arrange
    totals = reviewing["totals"]
    worker = agents(reviewing["phases"][1])["u1 · worker"]
    # Act / Assert: planner, 4 workers (u5 held), reviewers u1 done + u2 running + u3 queued, u1's queued fixer
    assert totals["agents"] == 9
    assert (totals["running"], totals["done"], totals["queued"], totals["failed"]) == (1, 6, 2, 0)
    assert worker["tokens"] == 2000 and worker["tools"] == 3 and len(worker["subs"]) == 1
    assert totals["tokens"] == 2000 and totals["tokens_h"] == "2k" and totals["tools"] == 3


def test_review_queues_a_reviewer_per_unit_not_started_and_not_skipped(reviewing):
    # Arrange
    review = reviewing["phases"][2]
    # Act
    rows = agents(review)
    # Assert
    assert review["state"] == "active"
    assert rows["u1 · reviewer"]["state"] == "done" and rows["u1 · reviewer"]["note"] == "1 confirmed bug"
    assert rows["u2 · reviewer"]["state"] == "active" and rows["u2 · reviewer"]["model"] == "codex"
    assert rows["u3 · reviewer"]["state"] == "queued" and rows["u3 · reviewer"]["model"] is None
    assert rows["u4 · reviewer"]["state"] == "skipped" and rows["u4 · reviewer"]["note"] == "trivial change"
    assert "u5 · reviewer" not in rows
    assert review["summary"].startswith("1/3 · 1 running · 1 queued")


def test_fix_queues_a_fixer_only_for_units_with_confirmed_bugs(reviewing):
    # Arrange
    fix = reviewing["phases"][3]
    # Act
    rows = agents(fix)
    # Assert
    assert list(rows) == ["u1 · fixer"]
    assert rows["u1 · fixer"]["state"] == "queued" and rows["u1 · fixer"]["note"] == "1 confirmed bug to fix"
    assert fix["state"] == "pending"
    assert agents(reviewing["phases"][1])["u5 · worker"]["state"] == "skipped"


def test_log_narrates_newest_last_and_is_capped(reviewing):
    # Arrange
    log = reviewing["log"]
    # Act / Assert
    assert 0 < len(log) <= 12
    assert "u5 held: no usable model has quota" in log
    assert "codex confirmed 1 bug in u1: Off by one" in log and "u4 review skipped: trivial change" in log
    assert log.index("u1 built and passed its check") < log.index("codex confirmed 1 bug in u1: Off by one")
    assert log[-1] == "codex is reviewing u2"


def test_fixing_run_queues_fixers_for_confirmed_bugs_and_failed_checks(tmp_path, monkeypatch):
    # Arrange: u1 being fixed, u2 waiting with a confirmed bug, u3 failed its check
    swarm = write_run(tmp_path, monkeypatch, {
        "state": "fixing", "step": "u1 pass 1 by claude-work-fable",
        "units": {"u1": unit("u1", "fixing", fix_passes=1, fixers=["claude-work-fable"],
                             review={"reviewer": "codex", "findings": 1, "confirmed": [BUG]}),
                  "u2": unit("u2", review={"reviewer": "codex", "findings": 1, "confirmed": [BUG, BUG]}),
                  "u3": unit("u3", "failed", reason="check failed (agent exit 1)")}},
        [{"id": "u1", "state": "accepted"}, {"id": "u2", "state": "accepted"}, {"id": "u3", "state": "failed"}],
        logs={"review-u1-1.log": "v", "review-u2-1.log": "v", "fix-u1-1.log": ""})
    claude_transcript(swarm / "wt" / "u1", "w1", "22:02", 1000)
    claude_transcript(swarm / "wt" / "u1", "f1", "22:30", 700, tools=3)
    # Act
    m = up.collect(up.find_run(None))
    # Assert
    plan, build, review, fix, integrate = m["phases"]
    rows = agents(fix)
    assert rows["u1 · fixer"]["state"] == "active" and rows["u1 · fixer"]["tokens"] == 700
    assert rows["u1 · fixer"]["now"] == "Bash step 2"
    assert rows["u2 · fixer"]["state"] == "queued" and rows["u2 · fixer"]["note"] == "2 confirmed bugs to fix"
    assert rows["u3 · fixer"]["state"] == "queued" and rows["u3 · fixer"]["note"] == "check failed"
    assert agents(build)["u3 · worker"]["state"] == "failed"
    assert "u3 · reviewer" not in agents(review)
    assert [ph["state"] for ph in m["phases"]] == ["done", "done", "done", "active", "pending"]
    assert fix["summary"] == "0/3 · 1 running · 2 queued · 700 tokens"
    assert m["totals"]["queued"] == 2 and m["log"][-1] == "u1 fix pass 1 by fable · work"


def test_finished_run_has_no_queued_agents_and_says_where_it_merged(tmp_path, monkeypatch):
    # Arrange
    write_run(tmp_path, monkeypatch, {
        "state": "done", "exit_code": 0, "finished_at": "2026-11-06T23:00:00Z",
        "integration": {"branch": f"ultra/{RUN_ID}", "goal_check_passed": True, "goal_check_rc": 0},
        "units": {"u1": unit("u1", "merged", fix_passes=1, fixers=["codex"],
                             review={"reviewer": "codex", "findings": 1, "confirmed": [BUG]}),
                  "u2": unit("u2", "merged", review={"reviewer": "codex", "findings": 0, "confirmed": []})}},
        [{"id": "u1", "state": "accepted"}, {"id": "u2", "state": "accepted"}],
        logs={"review-u1-1.log": "v", "review-u2-1.log": "v", "fix-u1-1.log": "done"})
    # Act
    m = up.collect(up.find_run(None))
    # Assert
    assert [ph["state"] for ph in m["phases"]] == ["done"] * 5
    assert m["totals"]["queued"] == 0 and m["totals"]["running"] == 0 and m["totals"]["agents"] == 6
    assert agents(m["phases"][3])["u1 · fixer"]["note"] == "fixed"
    assert m["phases"][4]["summary"] == "2/2 merged · goal check passed"
    assert len(m["log"]) == 12   # capped: the oldest lines (the plan, u1 starting) have rolled off
    assert m["log"][-4:] == ["u1 merged", "u2 merged", "goal check passed", f"run finished: 2/2 merged on ultra/{RUN_ID}"]


def test_cancelled_run_marks_working_agents_failed_and_skips_the_rest(tmp_path, monkeypatch):
    # Arrange: cancelled while u1 was building and u2 waited for a slot
    write_run(tmp_path, monkeypatch, {
        "state": "cancelled", "exit_code": 130, "step": "swarm",
        "units": {"u1": unit("u1", "planned"), "u2": unit("u2", "planned")}},
        [{"id": "u1", "state": "running", "started_at": "2026-11-06T22:01:00Z"}, {"id": "u2", "state": "queued"}])
    # Act
    m = up.collect(up.find_run(None))
    # Assert
    build = agents(m["phases"][1])
    assert build["u1 · worker"]["state"] == "failed" and build["u2 · worker"]["state"] == "skipped"
    assert m["phases"][1]["state"] == "failed"
    assert [ph["state"] for ph in m["phases"][2:]] == ["skipped", "skipped", "skipped"]
    assert m["totals"]["queued"] == 0
    assert m["log"] == ["planner split the goal into 2 units", "u1 started on fable · work",
                        "u1 failed: stopped when the run ended", "run cancelled: 0/2 merged (exit 130)"]


def test_review_logs_belong_to_their_own_unit_only(tmp_path):
    # Arrange: unit "a" must not claim unit "a-b"'s review log
    for name in ("review-a-1.log", "review-a-b-1.log", "review-a-b-2.log"):
        (tmp_path / name).write_text("v", encoding="utf-8")
    # Act / Assert
    assert up._attempts(tmp_path, "review-a") == [1]
    assert up._attempts(tmp_path, "review-a-b") == [1, 2]


def test_render_reads_like_a_workflow_and_fits_100_columns(reviewing):
    # Arrange
    pytest.importorskip("rich")
    from rich.cells import cell_len
    from rich.console import Console
    console = Console(width=100, record=True, color_system=None)
    # Act
    console.print(up.render(reviewing, 100))
    lines = console.export_text().splitlines()
    text = "\n".join(lines)
    # Assert
    assert lines[0].startswith("ULTRA goal-20261106 ● reviewing · 9 agents (1 running, 2 queued)")
    assert "1 sub-agent · 2k tokens" in lines[0]
    assert all(cell_len(line) <= 100 for line in lines)
    titles = [words[1] for words in map(str.split, lines) if len(words) > 1 and words[0] in "✓●○✗·" and words[1] in up.PHASES]
    assert titles == up.PHASES
    assert "● Review     1/3 · 1 running · 1 queued · 1 confirmed bug" in text
    assert any("u3 · reviewer" in line and "queued" in line for line in lines)
    assert "sub-agent Explore the module" in text
    assert "u1        ✓build ✓check ✓review ○fix ○merge  1 confirmed bug(s) to fix: Off by one" in text
    assert "codex is reviewing u2" in text


# ------------------------------------------------------------------ a second review attempt

def second_review(tmp_path, monkeypatch, notes):
    """Unit a under its second review: codex (attempt 1) gave no verdict, claude-work (attempt 2) is at work.

    route writes review.attempts only once a review delivers, so the unit's review is still {}.
    """
    run = tmp_path / "ultra" / RUN_ID
    write_run(tmp_path, monkeypatch, {
        "state": "reviewing", "step": "a by claude-work (same-family)", "notes": notes, "units": {"a": unit("a")}},
        [{"id": "a", "state": "accepted", "started_at": "2026-11-06T22:01:00Z", "finished_at": "2026-11-06T22:09:00Z"}],
        logs={"review-a-1.log": "codex output, no verdict", "review-a-2.log": ""})
    claude_transcript(run / "review" / "a", "r2", "22:20", 4200)   # codex leaves no Claude transcript
    return up.collect(up.find_run(None))


def test_second_review_attempt_gives_each_reviewer_its_own_model_and_tokens(tmp_path, monkeypatch):
    # Arrange
    m = second_review(tmp_path, monkeypatch, ["review of a by codex failed: no JSON verdict"])
    # Act
    rows = agents(m["phases"][2])
    first, second = rows["a · reviewer #1"], rows["a · reviewer #2"]
    # Assert
    assert (first["model"], first["state"], first["tokens"], first["tokens_h"]) == ("codex", "failed", 0, "")
    assert first["note"] == "no JSON verdict; another reviewer tried"
    assert (second["model"], second["state"], second["tokens"], second["tools"]) == ("opus · work", "active", 4200, 1)
    assert second["now"] == "Bash step 0"
    assert m["log"][-2:] == ["codex could not review a: no JSON verdict; another reviewer tried",
                             "opus · work is reviewing a"]


def test_second_review_attempt_without_a_note_still_finds_the_live_reviewers_session(tmp_path, monkeypatch):
    # Arrange / Act: no note names the first reviewer
    rows = agents(second_review(tmp_path, monkeypatch, [])["phases"][2])
    # Assert
    assert rows["a · reviewer #1"]["model"] is None and rows["a · reviewer #1"]["tokens"] == 0
    assert rows["a · reviewer #2"]["model"] == "opus · work" and rows["a · reviewer #2"]["tokens"] == 4200


# ------------------------------------------------------------------ the header line

@pytest.mark.parametrize("width", [60, 80, 100])
def test_header_keeps_tokens_and_elapsed_when_the_line_is_too_long(tmp_path, monkeypatch, width):
    # Arrange: a run with agents running, queued and failed, and a worker's sub-agent
    pytest.importorskip("rich")
    from rich.cells import cell_len
    from rich.console import Console
    swarm = write_run(tmp_path, monkeypatch, {
        "state": "reviewing", "step": "b by codex (cross-family)",
        "units": {"a": unit("a", "failed", reason="check failed (agent exit 1)"), "b": unit("b"), "c": unit("c")}},
        [{"id": "a", "state": "failed"}, {"id": "b", "state": "accepted"}, {"id": "c", "state": "accepted"}])
    claude_transcript(swarm / "wt" / "b", "w1", "22:02", 182_000, sub_tokens=500)
    m = up.collect(up.find_run(None))
    console = Console(width=width, record=True, color_system=None)
    # Act
    console.print(up.render(m, width))
    header = console.export_text().splitlines()[0]
    # Assert
    assert m["totals"]["running"] and m["totals"]["queued"] and m["totals"]["failed"]
    assert cell_len(header) <= width and header.startswith("ULTRA goal-")
    assert f"· {m['totals']['tokens_h']} tokens · {m['totals']['elapsed']}" in header
    assert header.endswith(m["totals"]["elapsed"])


# ------------------------------------------------------------------ team and research runs

def write_analysis_run(tmp_path, monkeypatch, progress, logs=None, verdicts=None):
    """A `route ultra --team` or `--research` run: progress.json, its logs and verdicts.json; no swarm."""
    monkeypatch.setenv("ROUTE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path))
    run = tmp_path / "state" / "ultra" / RUN_ID
    (run / "logs").mkdir(parents=True)
    for name, text in (logs or {}).items():
        (run / "logs" / name).write_text(text, encoding="utf-8")
    if verdicts is not None:
        (run / "verdicts.json").write_text(json.dumps(verdicts), encoding="utf-8")
    (run / "progress.json").write_text(json.dumps({
        "run_id": RUN_ID, "goal": "Audit the site. In depth.", "started_at": "2026-11-06T22:00:00Z",
        "repo": str(tmp_path / "repo"), "planner": None, "units": {}, "notes": [], "held": [], **progress}), encoding="utf-8")
    return run


def codex_transcript(cwd, name, start, tokens, tools=1):
    """A Codex rollout whose session ran in ``cwd``."""
    folder = Path(up.Path.home()) / ".codex" / "sessions" / "2026" / "11" / "06"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = f"2026-11-06T{start}:00Z"
    lines = [{"type": "session_meta", "timestamp": stamp, "payload": {"cwd": str(cwd)}}]
    lines += [{"type": "response_item", "timestamp": stamp, "payload": {"type": "function_call", "name": "shell",
                                                                        "arguments": json.dumps({"command": ["ls"]})}}] * tools
    lines.append({"type": "event_msg", "timestamp": stamp, "payload": {
        "type": "token_count", "info": {"total_token_usage": {"output_tokens": tokens}}}})
    (folder / f"rollout-{name}.jsonl").write_text("\n".join(map(json.dumps, lines)) + "\n", encoding="utf-8")


def analyst(uid, state="analysed", arm="claude-work", **fields):
    return {"id": uid, "state": state, "fix_passes": 0, "arm": arm, "where": "remote", "tier": "moderate",
            "attempts": [arm], **fields}


def test_team_run_counts_its_analysts_verifiers_critic_and_synthesizer(tmp_path, monkeypatch):
    # Arrange: a finished website-audit team; seo and access analysed, one analyst held, one left for the session
    run = write_analysis_run(tmp_path, monkeypatch, {
        "mode": "team", "layout": "website-audit", "state": "done", "exit_code": 0, "step": "by claude-work-fable",
        "finished_at": "2026-11-06T23:00:00Z", "synthesizer": "claude-work-fable", "critic_rounds": 1,
        "notes": ["critic round 1: adding access"],
        "units": {"seo": analyst("seo", findings=3), "access": analyst("access", arm="codex", findings=2),
                  "sess": {"id": "sess", "state": "session", "where": "session", "reason": "for your session"},
                  "slow": {"id": "slow", "state": "held", "where": "remote", "reason": "no usable model has quota"}}},
        logs={"analyst-seo.log": "a", "analyst-access.log": "a", "verify-C1-1.log": "v", "verify-C1-2.log": "v",
              "critic-1.log": "c", "synthesis.log": "report"},
        verdicts=[{"id": "C1", "verdict": "upheld", "verdicts": [{"claim": "C1", "arm": "codex", "verdict": "upheld"},
                                                                 {"claim": "C1", "arm": "claude-work", "verdict": "upheld"}]},
                  {"id": "C2", "verdict": "not cross-examined", "verdicts": []}])
    claude_transcript(run / "analysts" / "seo", "seo", "22:05", 1000, tools=2,
                      prompt='You are one analyst in an automated analysis team. Analyst id: seo You are READ-ONLY.')
    codex_transcript(run / "analysts" / "access", "access", "22:05", 500)
    claude_transcript(run / "verify" / "C1-2", "v2", "22:30", 300)
    claude_transcript(run, "synth", "22:50", 700, prompt="You are the synthesizer for an automated analysis team.")
    # Act
    m = up.collect(up.find_run(None))
    plan, build, review, fix, integrate = m["phases"]
    # Assert
    assert (m["totals"]["agents"], m["totals"]["done"], m["totals"]["tokens"]) == (6, 6, 2500)
    assert [ph["state"] for ph in m["phases"]] == ["skipped", "done", "done", "skipped", "done"]
    assert plan["summary"] == "layout website-audit · 4 analysts"
    assert build["summary"].startswith("2/2 · 1 held · 1 for your session · 5 findings")
    assert agents(build)["access · analyst"]["model"] == "codex" and agents(build)["access · analyst"]["tokens"] == 500
    assert agents(review)["C1 · verifier #1"]["model"] == "codex" and agents(review)["C1 · verifier #1"]["note"] == "upheld"
    assert agents(review)["C1 · verifier #2"]["model"] == "opus · work" and agents(review)["C1 · verifier #2"]["tokens"] == 300
    assert "1 upheld" in review["summary"] and "1 critic round" in review["summary"]
    assert fix["summary"] == "no fix build"
    assert agents(integrate)["synthesizer"]["tokens"] == 700 and agents(integrate)["synthesizer"]["note"] == "report written"
    assert "verifiers cross-examined 1 claim: 1 upheld" in m["log"] and "critic round 1: adding access" in m["log"]
    assert m["log"][-1] == "run finished: 2/2 analysts delivered"
    assert not any("merged" in line for line in m["log"])


def test_research_run_counts_its_parts_and_says_how_many_were_answered(tmp_path, monkeypatch):
    # Arrange: planner and synthesizer share the run directory; part p2 ran on codex, which left no rollout
    run = write_analysis_run(tmp_path, monkeypatch, {
        "mode": "research", "state": "done", "exit_code": 0, "planner": "claude-work-fable",
        "synthesizer": "claude-work-fable", "finished_at": "2026-11-06T22:30:00Z",
        "units": {"p1": analyst("p1", "answered", confidence="high"),
                  "p2": analyst("p2", "answered", arm="codex", confidence="medium")}},
        logs={"plan-1.log": "plan", "part-p1.log": "a", "part-p2.log": "a", "synthesis.log": "report"})
    claude_transcript(run, "plan", "22:01", 200, prompt="You are the planner for an automated parallel investigation.")
    claude_transcript(run / "parts" / "p1", "p1", "22:05", 400)
    claude_transcript(run, "synth", "22:20", 600, prompt="You are the synthesizer for an automated parallel investigation.")
    # Act
    m = up.collect(up.find_run(None))
    plan, build, review, fix, integrate = m["phases"]
    # Assert
    assert (m["totals"]["agents"], m["totals"]["tokens"]) == (4, 1200)
    assert agents(plan)["planner"]["tokens"] == 200 and agents(plan)["planner"]["note"] == "2 parts"
    assert agents(build)["p1 · researcher"]["note"] == "confidence high"
    assert agents(build)["p2 · researcher"]["tokens_h"] == ""          # no transcript: unknown, not 0
    assert (review["state"], review["summary"]) == ("skipped", "none in a research run")
    assert (fix["state"], fix["summary"]) == ("skipped", "none in a research run")
    assert agents(integrate)["synthesizer"]["tokens"] == 600
    assert m["log"][0] == "planner split the question into 2 parts"
    assert m["log"][-1] == "run finished: 2/2 parts answered"


def test_live_team_run_shows_working_and_queued_analysts_then_verifiers(tmp_path, monkeypatch):
    # Arrange: a running analyst, one waiting for a slot, one held
    write_analysis_run(tmp_path, monkeypatch, {
        "mode": "team", "layout": "website-audit", "state": "analysing", "step": "2 analyst(s), up to 1 in parallel",
        "units": {"a": analyst("a", "running"), "b": analyst("b", "planned", attempts=[]),
                  "c": {"id": "c", "state": "held", "where": "remote", "reason": "no usable model has quota"}}},
        logs={"analyst-a.log": ""})
    # Act
    m = up.collect(up.find_run(None))
    build = m["phases"][1]
    # Assert
    assert build["state"] == "active" and build["summary"].startswith("0/2 · 1 running · 1 queued · 1 held")
    assert agents(build)["b · analyst"]["note"] == "waiting for a free slot"
    assert (m["totals"]["running"], m["totals"]["queued"]) == (1, 1)
    assert [ph["state"] for ph in m["phases"][2:]] == ["pending", "pending", "pending"]


def test_cross_examining_team_run_has_a_verifier_at_work(tmp_path, monkeypatch):
    # Arrange: both analysts done; one verifier of C1 has answered, the other is still at work
    write_analysis_run(tmp_path, monkeypatch, {
        "mode": "team", "layout": "website-audit", "state": "cross-examining",
        "step": "1 claim(s), 2 verifier run(s)", "units": {"a": analyst("a", findings=1), "b": analyst("b", findings=1)}},
        logs={"analyst-a.log": "a", "analyst-b.log": "a", "verify-C1-1.log": "v", "verify-C1-2.log": ""})
    # Act
    m = up.collect(up.find_run(None))
    review = m["phases"][2]
    # Assert
    assert review["state"] == "active" and review["summary"] == "1/2 · 1 running · 1 claim(s), 2 verifier run(s)"
    assert m["log"][-1] == "1 verifier cross-examining claims"


def test_render_folds_a_long_phase_by_role(tmp_path, monkeypatch):
    # Arrange: twenty finished verifier runs and a critic round
    pytest.importorskip("rich")
    from rich.console import Console
    logs = {f"verify-C{i}-1.log": "v" for i in range(1, 21)}
    write_analysis_run(tmp_path, monkeypatch, {
        "mode": "team", "layout": "website-audit", "state": "critic", "step": "round 1 by codex",
        "units": {"a": analyst("a", findings=20)}}, logs={"analyst-a.log": "a", **logs})
    m = up.collect(up.find_run(None))
    console = Console(width=100, record=True, color_system=None)
    # Act
    console.print(up.render(m, 100))
    text = console.export_text()
    # Assert
    assert "✓ 20 verifiers done" in text and "C7 · verifier" not in text
    assert any("critic round 1" in line and "codex" in line for line in text.splitlines())
    assert "codex is looking for what the team missed (critic round 1)" in m["log"]
