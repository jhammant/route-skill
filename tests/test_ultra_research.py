"""``route ultra --research`` end to end, with shell scripts standing in for every model.

The fake arm reads its role off the prompt: a planner prompt gets the plan from
a file, a part prompt gets ``answers/<id>.json`` (or garbage when a ``.bad``
marker exists), a synthesis prompt gets ``report.md``. The scripts are named
``claude-work`` and ``codex`` because ``research_template`` keys on the program
name to make the command read-only. Nothing here reaches a network or a real
model; quota is monkeypatched.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from route import cli, swarm_dispatch, ultra_research
from route.pools import load_pools, pools_for_host
from route.storage import state_dir
from route.ultra import ultra_dir, ultra_status
from route.ultra_research import (
    Answer,
    Part,
    ResearchAnswerError,
    ResearchPlan,
    ResearchPlanError,
    lint_research_plan,
    parse_answer,
    parse_research_plan,
    research_mcp_config,
    research_template,
    run_research,
)

FAKE_ARM = r'''#!/bin/bash
# A model stand-in: the last argument is the prompt; the role is read off it.
FAKE_DIR="__FAKE_DIR__"
prompt="${@: -1}"
test "$ROUTE_WORKER" = "1" || { echo "ROUTE_WORKER not set" >&2; exit 97; }
case "$prompt" in
  *ULTRA-RESEARCH-PLAN-BEGIN*) role=planner ;;
  *ULTRA-REPORT-BEGIN*) role=synth ;;
  *ULTRA-ANSWER-BEGIN*) role=part ;;
  *) role=unknown ;;
esac
id=$(printf '%s\n' "$prompt" | sed -n 's/^Part id: //p' | head -1)
me=$(basename "$0")
opts=("${@:1:$#-1}")
echo "$me|$role|${id:-plan}|${opts[*]}|$PWD" >> "$FAKE_DIR/calls.log"
if [ "$role" = planner ]; then
  n=$(cat "$FAKE_DIR/plans.count" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$FAKE_DIR/plans.count"
  plan="$FAKE_DIR/plan-$n.json"; [ -f "$plan" ] || plan="$FAKE_DIR/plan.json"
  echo "Thinking..."; echo "ULTRA-RESEARCH-PLAN-BEGIN"; cat "$plan"; echo "ULTRA-RESEARCH-PLAN-END"; exit 0
fi
if [ "$role" = synth ]; then
  [ -f "$FAKE_DIR/report.missing" ] && { echo "I have nothing."; exit 0; }
  echo "ULTRA-REPORT-BEGIN"; cat "$FAKE_DIR/report.md"; echo "ULTRA-REPORT-END"; exit 0
fi
printf '%s\n' "$prompt" | grep -o "ultra-fake: touch [^ ]*" | sort -u | while read -r _ _ path; do touch "$path"; done
if [ -f "$FAKE_DIR/answers/$id.bad" ] || [ -f "$FAKE_DIR/answers/$id.bad-$me" ]; then
  echo "garbage without an answer block"; exit 0
fi
echo "Looking..."; echo "ULTRA-ANSWER-BEGIN"; cat "$FAKE_DIR/answers/$id.json"; echo "ULTRA-ANSWER-END"; exit 0
'''

SHAPES = '["coding:implement", "coding:review", "research", "writing"]'
QUOTA = {"claude": "critical", "claude-work": "ok", "codex": "ok"}
QUESTION = "Which came first, alpha or beta?"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Two fake arms named like the real programs, patched quota, no a-mail, no real models."""
    fake_dir = tmp_path / "fake"
    (fake_dir / "answers").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude-work", "codex"):
        script = bin_dir / name
        script.write_text(FAKE_ARM.replace("__FAKE_DIR__", str(fake_dir)))
        script.chmod(0o700)
    config = Path(os.environ["ROUTE_CONFIG_DIR"])
    config.mkdir(parents=True, exist_ok=True)
    (config / "pools.toml").write_text(textwrap.dedent(f"""
        [pools.claude-work]
        shapes = {SHAPES}
        remote = true
        dispatch = "{bin_dir}/claude-work --strict-mcp-config --mcp-config {tmp_path}/worker-mcp.json --print --permission-mode auto {{task}}"
        model = {{ name = "claude-opus-5-5" }}
        parallel_limit = 4
        quota_provider = "claude-work"

        [pools.codex]
        shapes = {SHAPES}
        remote = true
        dispatch = "{bin_dir}/codex exec --sandbox workspace-write {{task}}"
        model = {{ name = "gpt-6-astra" }}
        parallel_limit = 4
    """))
    monkeypatch.setattr(cli, "fetch_quota", lambda *a, **k: dict(QUOTA))
    monkeypatch.setattr(cli, "fetch_usage", lambda *a, **k: {"claude-work": {"5h": 10.0, "weekly": 20.0}})
    monkeypatch.setattr(swarm_dispatch, "amail_binary", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("ultra must never prompt"))
    return fake_dir


def part(identity, question, where="remote", needs="", tier=None):
    row = {"id": identity, "question": question, "where": where, "needs": needs}
    if tier:
        row["tier"] = tier
    return row


def three_parts():
    return [part("alpha", "What is alpha?"), part("beta", "What is beta?", tier="hard"),
            part("inbox", "Find the thread about alpha in my mail", where="session", needs="Gmail connector")]


def write_plan(fake_dir, parts, *, title="Alpha and beta", attempt=None):
    name = "plan.json" if attempt is None else f"plan-{attempt}.json"
    (fake_dir / name).write_text(json.dumps({"title": title, "parts": parts}))


def write_answer(fake_dir, identity, answer, sources, confidence="high", gaps=()):
    (fake_dir / "answers" / f"{identity}.json").write_text(json.dumps(
        {"answer": answer, "sources": sources, "confidence": confidence, "gaps": list(gaps)}))


def standard_answers(fake_dir):
    write_answer(fake_dir, "alpha", "Alpha is the first letter [S1].",
                 [{"id": "S1", "ref": "https://example.test/alpha", "note": "alpha page"}])
    write_answer(fake_dir, "beta", "Beta follows alpha [S1]; some say otherwise [S2].",
                 [{"id": "S1", "ref": "https://example.test/beta", "note": "beta page"},
                  {"id": "S2", "ref": "notes.txt:12", "note": "a dissenting note"}], confidence="medium",
                 gaps=["no primary source"])
    (fake_dir / "report.md").write_text("Alpha came first [alpha:S1], then beta [beta:S1]. Unverified claim [beta:S9].\n")


def calls(fake_dir):
    return [line.split("|") for line in (fake_dir / "calls.log").read_text().splitlines()]


def latest_run():
    [path] = list((state_dir() / "ultra").glob("*/summary.json"))
    return json.loads(path.read_text()), path.parent


def plan_text(payload):
    return f"some thinking\nULTRA-RESEARCH-PLAN-BEGIN\n```json\n{json.dumps(payload)}\n```\nULTRA-RESEARCH-PLAN-END\n"


# -- plan parsing and lint -----------------------------------------------------

def test_parse_research_plan_reads_the_last_fenced_block():
    """A planner may quote the format before printing the plan; the last block is the plan."""
    quoted = 'ULTRA-RESEARCH-PLAN-BEGIN\n{"title": "draft", "parts": []}\nULTRA-RESEARCH-PLAN-END\n'
    text = quoted + plan_text({"title": "Final", "parts": three_parts()})

    plan = parse_research_plan(text)

    assert plan.title == "Final"
    assert [p.id for p in plan.parts] == ["alpha", "beta", "inbox"]
    assert plan.parts[1].tier == "hard" and plan.parts[0].tier == "moderate"
    assert [p.id for p in plan.remote] == ["alpha", "beta"] and [p.id for p in plan.session] == ["inbox"]


@pytest.mark.parametrize("text, message", [
    ("no markers here", "no ULTRA-RESEARCH-PLAN-BEGIN"),
    ("ULTRA-RESEARCH-PLAN-BEGIN\n{not json\nULTRA-RESEARCH-PLAN-END", "not valid JSON"),
    ("ULTRA-RESEARCH-PLAN-BEGIN\n[]\nULTRA-RESEARCH-PLAN-END", "must be a JSON object"),
    ('ULTRA-RESEARCH-PLAN-BEGIN\n{"title": "x", "parts": {}}\nULTRA-RESEARCH-PLAN-END', "'parts' must be a list"),
    ('ULTRA-RESEARCH-PLAN-BEGIN\n{"title": "x", "parts": [{"id": 1}]}\nULTRA-RESEARCH-PLAN-END', "must be a string"),
    ("ULTRA-RESEARCH-PLAN-BEGIN\n{}", "no matching"),
])
def test_parse_research_plan_rejects_bad_shapes(text, message):
    """Shape problems are parser errors: there is nothing to lint without a plan."""
    with pytest.raises(ResearchPlanError, match=message):
        parse_research_plan(text)


@pytest.mark.parametrize("parts, max_parts, message", [
    ([part("Bad_Id", "q")], 6, "id must match"),
    ([part("a", "q"), part("a", "q2")], 6, "duplicate id"),
    ([part("a", "   ")], 6, "question must be non-empty"),
    ([part("a", "q", where="cloud")], 6, "where must be one of remote, session"),
    ([part("a", "q", where="session")], 6, "session part must say in 'needs'"),
    ([part("a", "q"), part("b", "q")], 1, "2 parts; at most 1 are allowed"),
    ([], 6, "plan has no parts"),
    ([part("a", "q", tier="easy")], 6, "tier must be one of moderate, hard"),
])
def test_lint_research_plan_reports_each_rule(parts, max_parts, message):
    """Every content rule is a lint problem the planner can be handed back."""
    plan = parse_research_plan(plan_text({"title": "t", "parts": parts}))

    problems = lint_research_plan(plan, max_parts=max_parts)

    assert any(message in problem for problem in problems), problems


def test_lint_research_plan_accepts_a_good_plan():
    plan = parse_research_plan(plan_text({"title": "t", "parts": three_parts()}))

    assert lint_research_plan(plan, max_parts=6) == []


# -- answers -------------------------------------------------------------------

def test_parse_answer_reads_every_field():
    text = ('ULTRA-ANSWER-BEGIN\n{"answer": "A [S1]", "sources": [{"id": "S1", "ref": "u", "note": "n"}], '
            '"confidence": "medium", "gaps": ["g"]}\nULTRA-ANSWER-END\n')

    answer = parse_answer(text)

    assert answer == Answer(answer="A [S1]", sources=[{"id": "S1", "ref": "u", "note": "n"}], confidence="medium",
                            gaps=["g"])


def answer_text(**fields):
    """A complete answer block with some fields overridden (or dropped with None)."""
    payload = {"answer": "a", "sources": [], "confidence": "high", "gaps": [], **fields}
    payload = {key: value for key, value in payload.items() if value is not None}
    return f"ULTRA-ANSWER-BEGIN\n{json.dumps(payload)}\nULTRA-ANSWER-END"


@pytest.mark.parametrize("payload, message", [
    ("nothing", "no ULTRA-ANSWER-BEGIN"),
    ("ULTRA-ANSWER-BEGIN\n{bad\nULTRA-ANSWER-END", "not valid JSON"),
    (answer_text(answer=""), "'answer' must be a non-empty string"),
    (answer_text(sources=None), "missing required field\\(s\\): sources"),
    (answer_text(confidence=None), "missing required field\\(s\\): confidence"),
    (answer_text(gaps=None), "missing required field\\(s\\): gaps"),
    (answer_text(sources=[{"id": 1}]), "source 0"),
    (answer_text(confidence="sure"), "'confidence' must be one of"),
    (answer_text(gaps="none"), "'gaps' must be a list"),
])
def test_parse_answer_rejects_bad_blocks(payload, message):
    """An unusable answer is a retry on another arm, so every bad or missing field must be caught."""
    with pytest.raises(ResearchAnswerError, match=message):
        parse_answer(payload)


# -- read-only command lines ---------------------------------------------------

def test_research_template_rewrites_the_claude_work_template(fake, tmp_path):
    """The worker's own MCP config and strict flag go; the run's block lands right after the program."""
    pools = pools_for_host(load_pools(), "claude")
    mcp = tmp_path / "run" / "research-mcp.json"

    tokens = shlex.split(research_template("claude-work", pools, host="claude", mcp_config=mcp))

    assert tokens[0].endswith("/bin/claude-work")
    assert tokens[1:8] == ["--strict-mcp-config", "--mcp-config", str(mcp), "--disallowedTools", "Edit", "Write",
                           "NotebookEdit"]
    assert tokens[8] == "--print" and tokens[-1] == "{task}"
    assert "worker-mcp.json" not in " ".join(tokens) and tokens.count("--strict-mcp-config") == 1


def test_research_template_for_the_host_arm_uses_the_default_template():
    """The host's pool has no dispatch; the default `claude --print ...` is made read-only with no servers."""
    pools = pools_for_host(load_pools(), "claude")

    template = research_template("claude", pools, host="claude", mcp_config=None)

    assert template == ('claude --strict-mcp-config --mcp-config \'{"mcpServers":{}}\' --disallowedTools Edit Write '
                        "NotebookEdit --print --permission-mode auto {task}")
    assert shlex.split(template)[3] == '{"mcpServers":{}}'


def test_research_template_forces_codex_read_only(fake):
    """Codex gets a read-only sandbox and skips its git check: research directories are not repositories."""
    pools = pools_for_host(load_pools(), "claude")
    bare = {**pools, "codex": replace(pools["codex"], dispatch="codex exec {task}")}
    no_exec = {**pools, "codex": replace(pools["codex"], dispatch="codex {task}")}

    forced = research_template("codex", pools, host="claude", mcp_config=None)
    inserted = research_template("codex", bare, host="claude", mcp_config=None)
    without_exec = research_template("codex", no_exec, host="claude", mcp_config=None)

    assert shlex.split(forced)[1:] == ["exec", "--sandbox", "read-only", "--skip-git-repo-check", "{task}"]
    assert inserted == "codex exec --sandbox read-only --skip-git-repo-check {task}"
    assert without_exec == "codex --sandbox read-only --skip-git-repo-check {task}"


def test_research_template_refuses_unsupported_programs(fake):
    """An arm whose command cannot be made read-only must not run research at all."""
    pools = pools_for_host(load_pools(), "claude")
    kimi = {**pools, "codex": replace(pools["codex"], dispatch="kimi --yolo {task}")}

    with pytest.raises(ValueError, match="cannot make 'kimi' read-only"):
        research_template("codex", kimi, host="claude", mcp_config=None)


def test_research_template_variadic_rule(fake, tmp_path):
    """--mcp-config and --disallowedTools swallow following values, so {task} may never follow the block."""
    pools = pools_for_host(load_pools(), "claude")
    bare = {**pools, "claude-work": replace(pools["claude-work"], dispatch="claude {task}")}
    stripped = {**pools, "claude-work": replace(pools["claude-work"],
                                                dispatch="claude --strict-mcp-config --mcp-config x.json {task}")}

    with pytest.raises(ValueError, match="must be an option"):
        research_template("claude-work", bare, host="claude", mcp_config=None)
    with pytest.raises(ValueError, match="must be an option"):
        research_template("claude-work", stripped, host="claude", mcp_config=None)
    tokens = shlex.split(research_template("claude-work", pools, host="claude", mcp_config=tmp_path / "m.json"))
    assert tokens[tokens.index("NotebookEdit") + 1].startswith("-")


# -- MCP allowlist -------------------------------------------------------------

def write_allowlist(names):
    config = Path(os.environ["ROUTE_CONFIG_DIR"])
    config.mkdir(parents=True, exist_ok=True)
    (config / "research.toml").write_text("mcp_servers = " + json.dumps(list(names)) + "\n")


def write_claude_json(tmp_path, monkeypatch, servers):
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"mcpServers": servers, "other": True}))
    monkeypatch.setenv("ROUTE_CLAUDE_JSON", str(path))
    return path


def test_research_mcp_config_copies_only_allowlisted_servers(tmp_path, monkeypatch):
    """Server definitions carry tokens: only the allowlisted ones are copied, owner-only."""
    write_claude_json(tmp_path, monkeypatch, {"ro": {"command": "ro-server", "env": {"TOKEN": "secret"}},
                                              "rw": {"command": "dangerous"}})
    write_allowlist(["ro", "ghost"])
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    path, info = research_mcp_config(run_dir)

    assert path == run_dir / "research-mcp.json"
    assert json.loads(path.read_text()) == {"mcpServers": {"ro": {"command": "ro-server", "env": {"TOKEN": "secret"}}}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert info["loaded"] == ["ro"] and info["missing"] == ["ghost"] and info["allowlist_exists"]


def test_research_mcp_config_without_allowlist_loads_nothing(tmp_path, monkeypatch):
    write_claude_json(tmp_path, monkeypatch, {"ro": {"command": "ro-server"}})
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    path, info = research_mcp_config(run_dir)

    assert path is None and info["loaded"] == [] and not info["allowlist_exists"]
    assert not (run_dir / "research-mcp.json").exists()


def test_research_mcp_config_with_an_empty_allowlist_loads_nothing(tmp_path, monkeypatch):
    write_claude_json(tmp_path, monkeypatch, {"ro": {"command": "ro-server"}})
    write_allowlist([])
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    path, info = research_mcp_config(run_dir)

    assert path is None and info["allowlist_exists"] and info["requested"] == []


# -- the whole run -------------------------------------------------------------

def test_full_run_plans_answers_synthesizes_and_reports(fake, tmp_path, monkeypatch, capsys):
    """Two remote parts and one session part: the report, the session list, resolved sources, the footer."""
    write_plan(fake, three_parts())
    standard_answers(fake)
    write_claude_json(tmp_path, monkeypatch, {"ro": {"command": "ro-server"}})
    write_allowlist(["ro", "ghost"])

    rc = run_research(QUESTION, host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0, text
    assert "Alpha came first [alpha:S1], then beta [beta:S1]." in text
    assert "## For your session" in text and "## Sources" in text
    assert "- inbox: Find the thread about alpha in my mail (needs: Gmail connector)" in text
    assert "- [alpha:S1] https://example.test/alpha: alpha page" in text
    assert "- [beta:S9] UNRESOLVED" in text
    assert "- [beta:S2] notes.txt:12: a dissenting note" in text
    assert "Arm per part: alpha: claude-work; beta: claude-work" in text
    assert "MCP servers loaded: ro" in text and "not found in ~/.claude.json: ghost" in text
    assert "- claude-work: 5h 10.0 -> 10.0, weekly 20.0 -> 20.0" in text
    assert text.rstrip().endswith("- Exit: 0") and capsys.readouterr().out.rstrip().endswith("- Exit: 0")
    assert summary["mode"] == "research" and summary["synthesizer"] == "claude-work" and summary["planner"] == "claude-work"
    by_id = {row["id"]: row for row in summary["parts"]}
    assert by_id["alpha"]["state"] == "answered" and by_id["beta"]["confidence"] == "medium"
    assert by_id["inbox"]["state"] == "session" and by_id["inbox"]["where"] == "session"
    assert summary["mcp_servers"]["loaded"] == ["ro"] and summary["exit_code"] == 0
    assert json.loads((run_dir / "parts" / "alpha.json").read_text())["answer"].startswith("Alpha is")
    assert (run_dir / "parts" / "beta.json").is_file()
    assert not (run_dir / "research-mcp.json").exists()
    progress = json.loads((run_dir / "progress.json").read_text())
    assert progress["mode"] == "research" and progress["state"] == "done" and progress["pids"] == []
    assert run_dir == ultra_dir(summary["run_id"])
    for name in ("plan.json", "summary.json", "logs/plan-1.log", "logs/part-alpha.log", "logs/part-beta.log",
                 "logs/synthesis.log"):
        assert (run_dir / name).is_file(), name
    rows = calls(fake)
    planner = next(row for row in rows if row[1] == "planner")
    assert planner[3].startswith(f"--strict-mcp-config --mcp-config {run_dir / 'research-mcp.json'} "
                                 "--disallowedTools Edit Write NotebookEdit --print")
    alpha = next(row for row in rows if row[1] == "part" and row[2] == "alpha")
    assert alpha[0] == "claude-work" and Path(alpha[4]) == run_dir / "parts" / "alpha"
    synth = next(row for row in rows if row[1] == "synth")
    assert Path(synth[4]) == run_dir
    assert "secret" not in text and "ro-server" not in text


def test_malformed_answer_is_retried_on_the_other_arm(fake):
    """A part without a usable block gets one more try on the next candidate, tier preserved."""
    write_plan(fake, three_parts())
    standard_answers(fake)
    (fake / "answers" / "alpha.bad-claude-work").write_text("")

    rc = run_research(QUESTION, host="claude")

    summary, run_dir = latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    alpha_calls = [row for row in calls(fake) if row[1] == "part" and row[2] == "alpha"]
    assert [row[0] for row in alpha_calls] == ["claude-work", "codex"]
    assert alpha_calls[1][3] == "exec --sandbox read-only --skip-git-repo-check"
    alpha = next(row for row in summary["parts"] if row["id"] == "alpha")
    assert alpha["arm"] == "codex" and alpha["attempts"] == ["claude-work", "codex"]
    assert (run_dir / "logs" / "part-alpha-2.log").is_file()


def test_part_failing_on_both_arms_exits_one_with_the_report(fake):
    write_plan(fake, three_parts())
    standard_answers(fake)
    (fake / "answers" / "alpha.bad").write_text("")

    rc = run_research(QUESTION, host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 1
    alpha = next(row for row in summary["parts"] if row["id"] == "alpha")
    assert alpha["state"] == "failed" and "claude-work: no ULTRA-ANSWER-BEGIN" in alpha["reason"]
    assert "codex: no ULTRA-ANSWER-BEGIN" in alpha["reason"]
    assert "Failed parts: alpha" in text and "## For your session" in text and "## Sources" in text
    assert not (run_dir / "parts" / "alpha.json").exists() and (run_dir / "parts" / "beta.json").is_file()
    assert json.loads((run_dir / "progress.json").read_text())["state"] == "failed"


def test_synthesis_failure_falls_back_to_the_deterministic_report(fake):
    """No report block from the synthesizer: the parts' answers are written as found, headings intact."""
    write_plan(fake, three_parts())
    standard_answers(fake)
    (fake / "report.missing").write_text("")

    rc = run_research(QUESTION, host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0
    assert summary["synthesizer"] is None
    assert "No synthesizer delivered a report" in text
    assert "## alpha: What is alpha?" in text and "Alpha is the first letter [S1]." in text
    assert "## For your session" in text and "## Sources" in text
    assert "- [alpha:S1] https://example.test/alpha: alpha page" in text
    assert any("delivered no report" in note for note in summary["notes"])


def test_explicit_repo_is_watched_with_git_status(fake, tmp_path):
    """A part that writes into --repo is reported as a warning and never reverted."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.name", "Ultra Test"),
                 ("config", "user.email", "ultra@example.test"), ("config", "commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "README.md").write_text("docs\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True, capture_output=True)
    write_plan(fake, [part("alpha", "What is here? ultra-fake: touch stray.txt"), part("beta", "What is beta?")])
    standard_answers(fake)

    rc = run_research(QUESTION, repo=str(repo), host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0
    assert "WARNING: the repository changed during the run" in text and "?? stray.txt" in text
    assert summary["porcelain_changes"] == ["?? stray.txt"]
    assert (repo / "stray.txt").exists()
    alpha = next(row for row in calls(fake) if row[1] == "part" and row[2] == "alpha")
    assert Path(alpha[4]) == repo.resolve()


def test_plan_only_writes_the_plan_and_dispatches_nothing(fake, capsys):
    write_plan(fake, three_parts())

    rc = run_research(QUESTION, plan_only=True, host="claude")

    [run_dir] = list((state_dir() / "ultra").glob("*/"))
    plan = json.loads((run_dir / "plan.json").read_text())
    assert rc == 0
    assert plan["arms"] == {"alpha": "claude-work", "beta": "claude-work"} and plan["planner"] == "claude-work"
    assert [p["id"] for p in plan["plan"]["parts"]] == ["alpha", "beta", "inbox"]
    assert [row[1] for row in calls(fake)] == ["planner"]
    out = capsys.readouterr().out
    assert "inbox\tsession\tmoderate\t-\tGmail connector" in out and "beta\tremote\thard\tclaude-work" in out
    progress = json.loads((run_dir / "progress.json").read_text())
    assert progress["state"] == "planned" and progress["mode"] == "research"
    assert not (run_dir / "summary.md").exists() and not (run_dir / "research-mcp.json").exists()


def test_bad_plan_is_replanned_once_then_given_up(fake):
    """Problems go back to the planner once; a second bad plan is exit 2 with the reasons."""
    write_plan(fake, [part("Bad", "q")], attempt=1)
    write_plan(fake, [], attempt=2)

    rc = run_research(QUESTION, host="claude")

    summary, run_dir = latest_run()
    assert rc == 2
    assert [row[1] for row in calls(fake)] == ["planner", "planner"]
    progress = json.loads((run_dir / "progress.json").read_text())
    assert any("id must match" in p for p in progress["plan_problems"])
    assert any("no parts" in p for p in progress["plan_problems"])
    text = (run_dir / "summary.md").read_text()
    assert "could not plan" in text and "## For your session" in text and "## Sources" in text
    assert "MCP servers loaded: none (add `mcp_servers = [\"name\"]`" in text and "research.toml" in text


def test_no_usable_arm_for_any_remote_part_exits_two(fake, monkeypatch):
    write_plan(fake, three_parts())
    monkeypatch.setattr(cli, "fetch_quota", lambda *a, **k: {"claude": "critical", "claude-work": "critical",
                                                            "codex": "critical"})

    rc = run_research(QUESTION, host="claude")

    summary, _ = latest_run()
    assert rc == 2 and summary["planner"] is None


def test_empty_question_exits_two(fake, capsys):
    rc = run_research("   ", host="claude")

    assert rc == 2 and "empty question" in capsys.readouterr().err
    assert not list((state_dir() / "ultra").glob("*/"))


def test_ultra_status_shows_a_research_run(fake, capsys):
    write_plan(fake, three_parts())
    standard_answers(fake)
    run_research(QUESTION, host="claude")
    capsys.readouterr()

    rc = ultra_status(None)

    out = capsys.readouterr().out
    assert rc == 0
    assert ": done" in out and f"goal: {QUESTION}" in out
    assert "alpha\tclaude-work\tanswered" in out and "inbox\t-\tsession" in out


# -- CLI -----------------------------------------------------------------------

def test_cli_research_plan_only(fake):
    write_plan(fake, three_parts())

    rc = cli.main(["ultra", "--research", QUESTION, "--plan-only"])

    assert rc == 0
    assert [row[1] for row in calls(fake)] == ["planner"]


def test_ultra_entry_point_research_with_max_parts(fake):
    write_plan(fake, [part("alpha", "What is alpha?"), part("beta", "What is beta?")])

    rc = cli.main_ultra(["--research", QUESTION, "--plan-only", "--max-parts", "2"])

    assert rc == 0
    [run_dir] = list((state_dir() / "ultra").glob("*/"))
    assert len(json.loads((run_dir / "plan.json").read_text())["plan"]["parts"]) == 2


def test_max_parts_is_enforced_by_the_lint(fake):
    write_plan(fake, three_parts())

    rc = cli.main_ultra(["--research", QUESTION, "--plan-only", "--max-parts", "2"])

    assert rc == 2
    assert [row[1] for row in calls(fake)] == ["planner", "planner"]


def test_build_mode_argv_leaves_repo_unset_for_cmd_ultra_to_default():
    """The parser no longer defaults --repo; build mode resolves None to '.' itself."""
    args = cli._parser("route").parse_args(["ultra", "Create two files"])

    assert args.repo is None and args.research is None and args.max_parts == 6


def test_build_mode_still_resolves_repo_to_cwd(monkeypatch):
    seen = {}

    def fake_run_ultra(goal, **kwargs):
        seen.update(goal=goal, **kwargs)
        return 0

    monkeypatch.setattr("route.ultra.run_ultra", fake_run_ultra)

    rc = cli.main(["ultra", "Create two files"])

    assert rc == 0 and seen["repo"] == "." and seen["goal"] == "Create two files"


def test_research_with_positional_words_is_an_error(fake, capsys):
    rc = cli.main(["ultra", "stray", "words", "--research", QUESTION])

    assert rc == 2 and "--research takes the question" in capsys.readouterr().err
    assert not (fake / "calls.log").exists()
