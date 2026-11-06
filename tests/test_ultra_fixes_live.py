"""Five problems a live ``route ultra --team website-audit`` run exposed, end to end with fake arms.

The fake arm here serves both research and team roles and, unlike the ones in
``test_ultra_research`` and ``test_ultra_team``, keeps every prompt it was
given (so a test can read what an agent was told and whether it was a repair)
and can answer a first attempt with an INVALID block (``<id>.invalid.json``)
and the repair with the valid one. Nothing reaches a network or a real model;
quota is monkeypatched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from route import cli, swarm_dispatch, ultra_research
from route.eligibility import QuotaSnapshot
from route.pools import load_pools, pools_for_host
from route.storage import state_dir
from route.ultra_research import (
    WHERE_RULES,
    Part,
    parse_answer,
    repair_prompt,
    research_plan_prompt,
    run_research,
)
from route.ultra_team import Analyst, Claim, critic_prompt, parse_analysis, run_team, team_intensity, team_prompt
from tests import test_ultra_research as research
from tests import test_ultra_team as team

FAKE_ARM = r'''#!/bin/bash
# A model stand-in for research and team roles; keeps every prompt; can fail validation once, then repair.
FAKE_DIR="__FAKE_DIR__"
prompt="${@: -1}"
test "$ROUTE_WORKER" = "1" || { echo "ROUTE_WORKER not set" >&2; exit 97; }
me=$(basename "$0")
kind=""
case "$prompt" in
  *ULTRA-RESEARCH-PLAN-BEGIN*) role=planner; b=ULTRA-RESEARCH-PLAN-BEGIN; e=ULTRA-RESEARCH-PLAN-END; f="$FAKE_DIR/plan.json" ;;
  *ULTRA-TEAM-BEGIN*) role=planner; b=ULTRA-TEAM-BEGIN; e=ULTRA-TEAM-END; f="$FAKE_DIR/team.json" ;;
  *ULTRA-CRITIC-BEGIN*) role=critic ;;
  *ULTRA-VERDICT-BEGIN*) role=verifier ;;
  *ULTRA-REPORT-BEGIN*) role=synth ;;
  *ULTRA-ANSWER-BEGIN*) role=part; kind=answers; b=ULTRA-ANSWER-BEGIN; e=ULTRA-ANSWER-END ;;
  *ULTRA-ANALYSIS-BEGIN*) role=analyst; kind=analyses; b=ULTRA-ANALYSIS-BEGIN; e=ULTRA-ANALYSIS-END ;;
  *) role=unknown ;;
esac
id=$(printf '%s\n' "$prompt" | sed -n 's/^Part id: //p; s/^Analyst id: //p' | head -1)
claim=$(printf '%s\n' "$prompt" | sed -n 's/^Claim id: //p' | head -1)
case "$prompt" in *"corrected JSON only"*) repair=1 ;; *) repair=0 ;; esac
unit="${id:-${claim:-$role}}"
opts=("${@:1:$#-1}")
echo "$me|$role|$unit|${opts[*]}|$PWD|$repair" >> "$FAKE_DIR/calls.log"
mkdir -p "$FAKE_DIR/prompts"; printf '%s' "$prompt" > "$FAKE_DIR/prompts/$me-$role-$unit-$repair-$$.txt"
case "$role" in
  planner) echo "$b"; cat "$f"; echo "$e"; exit 0 ;;
  critic) echo "ULTRA-CRITIC-BEGIN"; echo '{"missing": []}'; echo "ULTRA-CRITIC-END"; exit 0 ;;
  verifier) echo "ULTRA-VERDICT-BEGIN"; echo '{"verdict": "upheld", "reason": "checked", "sources": []}'
            echo "ULTRA-VERDICT-END"; exit 0 ;;
  synth) echo "ULTRA-REPORT-BEGIN"; cat "$FAKE_DIR/report.md"; echo "ULTRA-REPORT-END"; exit 0 ;;
esac
printf '%s\n' "$prompt" | grep -o "ultra-fake: touch [^ ]*" | sort -u | while read -r _ _ path; do touch "$path"; done
if [ -f "$FAKE_DIR/$kind/$id.bad" ] || [ -f "$FAKE_DIR/$kind/$id.bad-$me" ]; then
  echo "garbage without a block"; exit 0
fi
f="$FAKE_DIR/$kind/$id.json"
if [ "$repair" = 0 ] && [ -f "$FAKE_DIR/$kind/$id.invalid.json" ]; then f="$FAKE_DIR/$kind/$id.invalid.json"; fi
echo "Looking..."; echo "$b"; cat "$f"; echo "$e"; exit 0
'''

QUOTA = {"claude": "critical", "claude-work": "ok", "codex": "ok"}
URL = team.URL
#: Eight percent used with 4092 minutes left of 10080 is 59% elapsed, and 87% of the budget spare.
FULL_WINDOW = {"percentUsed": 8.0, "resetsInMinutes": 4092}
FULL_LABEL = "full (87% of the week's quota is spare: 8% used, 59% elapsed)"

REPO_LAYOUT = """
[layouts.repo-check]
title = "Repo check"
description = "A small repository audit"
targets = "repo"
fix = false
report = "Findings by severity"

[[layouts.repo-check.analysts]]
id = "seo"
role = "Correctness analyst"
lens = "bugs"
brief = "Find bugs. ultra-fake: touch stray.txt"

[[layouts.repo-check.analysts]]
id = "perf"
role = "Performance analyst"
lens = "speed"
brief = "Find slow paths"

[[layouts.repo-check.analysts]]
id = "copy"
role = "Docs analyst"
lens = "docs"
brief = "Find stale docs"
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Two prompt-keeping fake arms named like the real programs; patched quota; no real models."""
    fake_dir = tmp_path / "fake"
    for name in ("answers", "analyses", "verdicts"):
        (fake_dir / name).mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude-work", "codex"):
        script = bin_dir / name
        script.write_text(FAKE_ARM.replace("__FAKE_DIR__", str(fake_dir)))
        script.chmod(0o700)
    team.write_pools(tmp_path, bin_dir)
    monkeypatch.setattr(cli, "fetch_quota", lambda *a, **k: dict(QUOTA))
    monkeypatch.setattr(cli, "fetch_usage", lambda *a, **k: {"claude-work": {"5h": 10.0, "weekly": 20.0}})
    monkeypatch.setattr(swarm_dispatch, "amail_binary", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("ultra must never prompt"))
    return fake_dir


def calls(fake_dir, role, unit):
    """(arm, cwd, was a repair) per dispatch of one unit, in order."""
    rows = [line.split("|") for line in (fake_dir / "calls.log").read_text().splitlines()]
    return [(row[0], Path(row[4]), row[5] == "1") for row in rows if row[1] == role and row[2] == unit]


def prompts(fake_dir, role, unit):
    return [path.read_text() for path in sorted((fake_dir / "prompts").glob(f"*-{role}-{unit}-*.txt"))]


def latest_run():
    [path] = list((state_dir() / "ultra").glob("*/summary.json"))
    return json.loads(path.read_text()), path.parent


def analysis_text(findings, **fields):
    payload = {"summary": "what I saw", "findings": findings, "gaps": [], **fields}
    return f"ULTRA-ANALYSIS-BEGIN\n{json.dumps(payload)}\nULTRA-ANALYSIS-END"


def answer_text(sources, **fields):
    payload = {"answer": "a [S1]", "sources": sources, "confidence": "high", "gaps": [], **fields}
    return f"ULTRA-ANSWER-BEGIN\n{json.dumps(payload)}\nULTRA-ANSWER-END"


# -- item 1: source ids never sink an answer ------------------------------------------

def test_parse_analysis_renames_an_id_reused_for_another_ref_and_keeps_both():
    """The live failure: two findings both cite S1 for different refs; both survive under distinct ids."""
    text = analysis_text([
        team.finding("F1", "The sitemap is missing", sources=[{"id": "S1", "ref": f"{URL}/robots.txt", "note": "r"}]),
        team.finding("F2", "Mobile is slow", sources=[{"id": "S1", "ref": "lighthouse run", "note": "LCP"}])])

    analysis = parse_analysis(text)

    assert [src["id"] for src in analysis.sources] == ["S1", "S2"]
    assert [src["ref"] for src in analysis.sources] == [f"{URL}/robots.txt", "lighthouse run"]
    assert analysis.findings[1].sources[0]["id"] == "S2" and analysis.findings[0].sources[0]["id"] == "S1"
    assert analysis.renamed == [{"finding": "F2", "from": "S1", "to": "S2", "ref": "lighthouse run"}]
    assert analysis.to_dict()["renamed"] == analysis.renamed
    assert analysis.findings[1].claim == "Mobile is slow"


def test_parse_analysis_renames_past_the_highest_id_seen_including_earlier_renames():
    """S<max+1> counts every id seen so far, so a rename never collides with an id used later in the same finding."""
    text = analysis_text([
        team.finding("F1", "one", sources=[{"id": "S1", "ref": "a"}, {"id": "S4", "ref": "b"}]),
        team.finding("F2", "two", sources=[{"id": "S1", "ref": "c"}]),
        team.finding("F3", "three", sources=[{"id": "S1", "ref": "d"}, {"id": "S5", "ref": "d"}])])

    analysis = parse_analysis(text)

    assert [(row["finding"], row["from"], row["to"]) for row in analysis.renamed] == \
        [("F2", "S1", "S5"), ("F3", "S1", "S6"), ("F3", "S5", "S7")]
    assert [src["id"] for src in analysis.sources] == ["S1", "S4", "S5", "S6", "S7"]


def test_parse_analysis_keeps_an_id_reused_for_the_same_ref_as_one_source():
    text = analysis_text([
        team.finding("F1", "one", sources=[{"id": "S1", "ref": f"{URL}/robots.txt", "note": "r"}]),
        team.finding("F2", "two", sources=[{"id": "S1", "ref": f"  {URL}/robots.txt\n", "note": "again"}])])

    analysis = parse_analysis(text)

    assert analysis.renamed == [] and len(analysis.sources) == 1
    assert [src["id"] for src in analysis.findings[1].sources] == ["S1"]


def test_parse_answer_renames_a_shared_id_and_keeps_a_same_ref_reuse():
    clashing = parse_answer(answer_text([{"id": "S1", "ref": "https://a.test", "note": "a"},
                                         {"id": "S1", "ref": "https://b.test", "note": "b"}]))
    same = parse_answer(answer_text([{"id": "S1", "ref": "https://a.test"}, {"id": "S1", "ref": " https://a.test "}]))

    assert [src["id"] for src in clashing.sources] == ["S1", "S2"]
    assert clashing.renamed == [{"from": "S1", "to": "S2", "ref": "https://b.test"}]
    assert clashing.to_dict()["renamed"] == clashing.renamed and clashing.answer == "a [S1]"
    assert same.renamed == [] and [src["id"] for src in same.sources] == ["S1", "S1"]


def test_team_run_records_renames_in_the_analyst_file_and_the_footer(fake):
    """perf's second finding reuses S1 for the Lighthouse run: the run keeps both and says what it renamed."""
    team.write_layouts()
    team.standard_analyses(fake)
    path = fake / "analyses" / "perf.json"
    raw = json.loads(path.read_text())
    raw["findings"][1]["sources"][0]["id"] = "S1"
    path.write_text(json.dumps(raw))
    (fake / "report.md").write_text("Slow [perf:S2]; no sitemap [perf:S1].\n")

    rc = run_team(team.QUESTION, layout="site-check", urls=(URL,), rounds=0, host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0, text
    assert "- Source ids renamed: perf: S1 -> S2 (lighthouse run)" in text
    assert "- [perf:S1] https://example.test/robots.txt: the robots file" in text
    assert "- [perf:S2] lighthouse run: LCP 4.0s" in text
    assert summary["source_renames"] == [{"unit": "perf", "finding": "F2", "from": "S1", "to": "S2",
                                          "ref": "lighthouse run"}]
    perf = json.loads((run_dir / "analysts" / "perf.json").read_text())
    assert perf["renamed"] == [{"finding": "F2", "from": "S1", "to": "S2", "ref": "lighthouse run"}]
    assert perf["findings"][1]["sources"][0]["id"] == "S2"
    assert next(row for row in summary["analysts"] if row["id"] == "perf")["state"] == "analysed"


def test_research_run_records_renames_in_the_part_file_and_the_footer(fake):
    research.write_plan(fake, [research.part("alpha", "What is alpha?")])
    research.write_answer(fake, "alpha", "Alpha [S1] and again [S1].",
                         [{"id": "S1", "ref": "https://example.test/alpha", "note": "alpha page"},
                          {"id": "S1", "ref": "notes.txt:3", "note": "a note"}])
    (fake / "report.md").write_text("Alpha came first [alpha:S1] [alpha:S2].\n")

    rc = run_research(research.QUESTION, host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0, text
    assert "- Source ids renamed: alpha: S1 -> S2 (notes.txt:3)" in text
    assert "- [alpha:S2] notes.txt:3: a note" in text
    assert summary["source_renames"] == [{"unit": "alpha", "from": "S1", "to": "S2", "ref": "notes.txt:3"}]
    assert json.loads((run_dir / "parts" / "alpha.json").read_text())["renamed"] == \
        [{"from": "S1", "to": "S2", "ref": "notes.txt:3"}]


# -- item 2: repair before redoing ------------------------------------------------

def test_repair_prompt_names_the_unit_the_error_and_the_block():
    text = repair_prompt("'confidence' must be one of high, medium, low", '{"a": 1}', "B", "E", header="Part id: x")

    assert text.startswith("Part id: x\n")
    assert "'confidence' must be one of high, medium, low" in text
    assert '\nB\n{"a": 1}\nE\n' in text
    assert "Return the corrected JSON only, between the same markers; change nothing except what the error " \
           "requires." in text
    assert not repair_prompt("e", "b", "B", "E").startswith("\n")


def test_invalid_part_answer_is_repaired_on_the_same_arm(fake):
    """A block that fails validation goes back to the arm that wrote it; no second arm is spent."""
    research.write_plan(fake, [research.part("alpha", "What is alpha?"), research.part("beta", "What is beta?")])
    research.standard_answers(fake)
    good = json.loads((fake / "answers" / "alpha.json").read_text())
    (fake / "answers" / "alpha.invalid.json").write_text(json.dumps({**good, "confidence": "certain"}))

    rc = run_research(research.QUESTION, host="claude")

    summary, run_dir = latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    alpha = next(row for row in summary["parts"] if row["id"] == "alpha")
    assert alpha["state"] == "answered" and alpha["arm"] == "claude-work" and alpha["repaired"] is True
    assert alpha["attempts"] == ["claude-work"]
    assert [(arm, repair) for arm, _, repair in calls(fake, "part", "alpha")] == [("claude-work", False),
                                                                                 ("claude-work", True)]
    assert (run_dir / "logs" / "part-alpha.log").is_file() and (run_dir / "logs" / "part-alpha-repair.log").is_file()
    repair = next(text for text in prompts(fake, "part", "alpha") if "corrected JSON only" in text)
    assert repair.startswith("Part id: alpha\n") and "'confidence' must be one of" in repair
    assert '"confidence": "certain"' in repair
    assert json.loads((run_dir / "parts" / "alpha.json").read_text())["repaired"] is True
    beta = next(row for row in summary["parts"] if row["id"] == "beta")
    assert beta["repaired"] is False and not (run_dir / "logs" / "part-beta-repair.log").exists()
    assert "- Repaired on the same arm: alpha (claude-work)\n" in (run_dir / "summary.md").read_text()


def test_part_without_any_block_moves_straight_to_the_next_arm(fake):
    research.write_plan(fake, [research.part("alpha", "What is alpha?")])
    research.standard_answers(fake)
    (fake / "answers" / "alpha.bad-claude-work").write_text("")

    rc = run_research(research.QUESTION, host="claude")

    summary, run_dir = latest_run()
    assert rc == 0
    alpha = next(row for row in summary["parts"] if row["id"] == "alpha")
    assert alpha["arm"] == "codex" and alpha["attempts"] == ["claude-work", "codex"] and alpha["repaired"] is False
    assert [(arm, repair) for arm, _, repair in calls(fake, "part", "alpha")] == [("claude-work", False),
                                                                                 ("codex", False)]
    assert not list((run_dir / "logs").glob("*repair*"))


def test_invalid_analysis_is_repaired_on_the_same_arm(fake):
    team.write_layouts()
    team.standard_analyses(fake)
    good = json.loads((fake / "analyses" / "seo.json").read_text())
    broken = json.loads(json.dumps(good))
    del broken["findings"][0]["fix"]
    (fake / "analyses" / "seo.invalid.json").write_text(json.dumps(broken))

    rc = run_team(team.QUESTION, layout="site-check", urls=(URL,), rounds=0, host="claude")

    summary, run_dir = latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    seo = next(row for row in summary["analysts"] if row["id"] == "seo")
    assert seo["state"] == "analysed" and seo["arm"] == "claude-work" and seo["repaired"] is True
    assert seo["attempts"] == ["claude-work"]
    assert [(arm, repair) for arm, _, repair in calls(fake, "analyst", "seo")] == [("claude-work", False),
                                                                                  ("claude-work", True)]
    assert (run_dir / "logs" / "analyst-seo-repair.log").is_file()
    repair = next(text for text in prompts(fake, "analyst", "seo") if "corrected JSON only" in text)
    assert repair.startswith("Analyst id: seo\n") and "missing 'fix'" in repair
    assert json.loads((run_dir / "analysts" / "seo.json").read_text())["repaired"] is True
    perf = next(row for row in summary["analysts"] if row["id"] == "perf")
    assert perf["repaired"] is False
    assert "- Repaired on the same arm: seo (claude-work)\n" in (run_dir / "summary.md").read_text()


def test_analysis_that_fails_repair_falls_back_to_the_next_arm_with_both_reasons(fake):
    """The invalid block stays invalid on repair, and the next arm has no block at all: both reasons recorded."""
    team.write_layouts()
    team.standard_analyses(fake)
    broken = json.loads((fake / "analyses" / "seo.json").read_text())
    broken["findings"][0]["severity"] = "huge"
    (fake / "analyses" / "seo.json").write_text(json.dumps(broken))
    (fake / "analyses" / "seo.invalid.json").write_text(json.dumps(broken))
    (fake / "analyses" / "seo.bad-codex").write_text("")

    rc = run_team(team.QUESTION, layout="site-check", urls=(URL,), rounds=0, host="claude")

    summary, run_dir = latest_run()
    assert rc == 1
    seo = next(row for row in summary["analysts"] if row["id"] == "seo")
    assert seo["state"] == "failed" and seo["attempts"] == ["claude-work", "codex"] and seo["repaired"] is False
    assert "claude-work: finding 0: 'severity' must be one of" in seo["reason"]
    assert "; repair: finding 0: 'severity' must be one of" in seo["reason"]
    assert "codex: no ULTRA-ANALYSIS-BEGIN block" in seo["reason"]
    assert [(arm, repair) for arm, _, repair in calls(fake, "analyst", "seo")] == \
        [("claude-work", False), ("claude-work", True), ("codex", False)]
    assert (run_dir / "logs" / "analyst-seo-repair.log").is_file()
    assert not (run_dir / "logs" / "analyst-seo-2-repair.log").exists()


def test_analyst_without_any_block_moves_straight_to_the_next_arm(fake):
    team.write_layouts()
    team.standard_analyses(fake)
    (fake / "analyses" / "seo.bad-claude-work").write_text("")

    rc = run_team(team.QUESTION, layout="site-check", urls=(URL,), rounds=0, host="claude")

    summary, run_dir = latest_run()
    assert rc == 0
    seo = next(row for row in summary["analysts"] if row["id"] == "seo")
    assert seo["arm"] == "codex" and seo["attempts"] == ["claude-work", "codex"] and seo["repaired"] is False
    assert [arm for arm, _, _ in calls(fake, "analyst", "seo")] == ["claude-work", "codex"]
    assert not list((run_dir / "logs").glob("*repair*"))
    assert "Repaired on the same arm" not in (run_dir / "summary.md").read_text()


def test_a_retry_arm_repair_is_logged_under_the_retry_stem(fake):
    """The first arm prints no block; the second prints an invalid one and repairs it: ``-2-repair.log``."""
    research.write_plan(fake, [research.part("alpha", "What is alpha?")])
    research.standard_answers(fake)
    (fake / "answers" / "alpha.bad-claude-work").write_text("")
    good = json.loads((fake / "answers" / "alpha.json").read_text())
    (fake / "answers" / "alpha.invalid.json").write_text(json.dumps({**good, "gaps": "none"}))

    rc = run_research(research.QUESTION, host="claude")

    summary, run_dir = latest_run()
    assert rc == 0
    alpha = next(row for row in summary["parts"] if row["id"] == "alpha")
    assert alpha["arm"] == "codex" and alpha["attempts"] == ["claude-work", "codex"] and alpha["repaired"] is True
    assert (run_dir / "logs" / "part-alpha-2-repair.log").is_file()
    assert not (run_dir / "logs" / "part-alpha-repair.log").exists()


# -- item 3: Codex needs network, so it never runs in --repo --------------------------

def test_is_codex_and_agent_cwd_follow_the_program_basename(fake, tmp_path):
    pools = pools_for_host(load_pools(), "claude")
    repo = tmp_path / "repo"
    repo.mkdir()
    own = tmp_path / "run" / "analysts" / "x"

    codex_dir = ultra_research.agent_cwd("codex", pools, "claude", repo, own)
    claude_dir = ultra_research.agent_cwd("claude-work", pools, "claude", repo, own)
    claude_alone = ultra_research.agent_cwd("claude-work", pools, "claude", None, tmp_path / "run" / "parts" / "y")

    assert ultra_research.is_codex("codex", pools) and not ultra_research.is_codex("claude-work", pools)
    assert codex_dir == own and own.is_dir() and list(own.iterdir()) == []
    assert claude_dir == repo
    assert claude_alone == tmp_path / "run" / "parts" / "y" and claude_alone.is_dir()


def test_with_repo_codex_analysts_run_in_their_own_empty_directory_and_claude_in_the_repo(fake, tmp_path):
    """Codex may write to its cwd, so --repo is never its cwd; its prompt names the repo path instead.
    The Claude analyst still runs in the repo, so its stray file is caught by the porcelain check."""
    repo = team.git_repo(tmp_path)
    team.write_layouts(REPO_LAYOUT)
    team.standard_analyses(fake)

    rc = run_team("Is this ready?", layout="repo-check", repo=str(repo), rounds=0, host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0, text
    [(copy_arm, copy_cwd, _)] = calls(fake, "analyst", "copy")
    [(seo_arm, seo_cwd, _)] = calls(fake, "analyst", "seo")
    assert copy_arm == "codex" and copy_cwd == run_dir / "analysts" / "copy"
    assert copy_cwd.is_dir() and list(copy_cwd.iterdir()) == []
    assert seo_arm == "claude-work" and seo_cwd == repo.resolve()
    [copy_prompt] = prompts(fake, "analyst", "copy")
    assert f"- Repository (read access): {repo.resolve()}" in copy_prompt
    assert "WARNING: the repository changed during the run" in text and "?? stray.txt" in text
    assert summary["porcelain_changes"] == ["?? stray.txt"] and (repo / "stray.txt").exists()
    assert not (repo / "analysts").exists()
    verifier_dirs = {cwd for arm, cwd, _ in calls(fake, "verifier", "C1") if arm == "codex"}
    assert verifier_dirs and all(cwd.parent == run_dir / "verify" for cwd in verifier_dirs)
    claude_verifiers = {cwd for arm, cwd, _ in calls(fake, "verifier", "C3") if arm == "claude-work"}
    assert claude_verifiers == {repo.resolve()}


def test_with_repo_a_codex_retry_moves_the_part_out_of_the_repo_and_names_it(fake, tmp_path):
    """The cwd is chosen per attempt: Claude ran alpha in the repo, the Codex retry runs in parts/alpha."""
    repo = team.git_repo(tmp_path)
    research.write_plan(fake, [research.part("alpha", "What is alpha?")])
    research.standard_answers(fake)
    (fake / "answers" / "alpha.bad-claude-work").write_text("")

    rc = run_research(research.QUESTION, repo=str(repo), host="claude")

    summary, run_dir = latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    assert [(arm, cwd) for arm, cwd, _ in calls(fake, "part", "alpha")] == \
        [("claude-work", repo.resolve()), ("codex", run_dir / "parts" / "alpha")]
    assert list((run_dir / "parts" / "alpha").iterdir()) == []
    for text in prompts(fake, "part", "alpha"):
        assert f"- Repository (read access): {repo.resolve()}" in text
    assert summary["porcelain_changes"] == []


def test_part_prompt_names_the_repo_only_when_given():
    part = Part(id="alpha", question="q", where="remote")

    with_repo = ultra_research.part_prompt("Q", part, repo=Path("/tmp/some-repo"))
    without = ultra_research.part_prompt("Q", part)

    assert "- Repository (read access): /tmp/some-repo" in with_repo
    assert "Repository (read access)" not in without


def test_codex_planner_and_synthesizer_run_in_their_own_directories(fake, monkeypatch):
    """With only Codex having quota, the planner and the synthesizer get plan/attempt-1 and synthesis."""
    monkeypatch.setattr(cli, "fetch_quota", lambda *a, **k: {**QUOTA, "claude-work": "critical"})
    research.write_plan(fake, [research.part("alpha", "What is alpha?")])
    research.standard_answers(fake)

    rc = run_research(research.QUESTION, host="claude")

    summary, run_dir = latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    [(_, planner_cwd, _)] = calls(fake, "planner", "planner")
    [(_, synth_cwd, _)] = calls(fake, "synth", "synth")
    assert planner_cwd == run_dir / "plan" / "attempt-1" and synth_cwd == run_dir / "synthesis"
    assert summary["planner"] == "codex" and summary["synthesizer"] == "codex"


# -- item 4: where work may run -----------------------------------------------------

WHERE_SUBSTRINGS = ("headless browsers", "npx -y lighthouse", "npx -y playwright",
                    "claude.ai connectors (Gmail, Calendar, Drive)",
                    "signed-in browser or account (Claude in Chrome, logged-in dashboards)",
                    "screen, simulator or physical devices", "costs the user's own account")


@pytest.mark.parametrize("prompt", [
    research_plan_prompt("Q", max_parts=4),
    team_prompt("Q", team_size=5, targets="- URL: https://example.test"),
    critic_prompt("Q", [Analyst("seo", "r", "l", "b")], [Claim("C1", "t", "high", ["seo"])], cap=3),
], ids=["research-planner", "team-planner", "critic"])
def test_every_planner_prompt_says_where_work_may_run(prompt):
    """Headless browsers are background work; only connectors, signed-in accounts and devices are session."""
    for needle in WHERE_SUBSTRINGS:
        assert needle in prompt, needle
    assert WHERE_RULES in prompt
    assert "GUI walkthrough" not in prompt and "or this Mac CANNOT" not in prompt


# -- item 5: intensity printed once ---------------------------------------------------

def test_full_intensity_label_reads_name_then_reason_once():
    intensity = team_intensity(FULL_WINDOW, "ok", None)

    assert intensity.label == FULL_LABEL
    assert intensity.why == "87% of the week's quota is spare: 8% used, 59% elapsed"
    assert intensity.to_dict() == {"name": "full", "team_size": 8, "verifiers": 3, "rounds": 2, "max_claims": 20,
                                   "why": intensity.why, "label": FULL_LABEL}


@pytest.mark.parametrize("window, headroom, override, why", [
    (None, None, "full", "requested (--thorough)"),
    (None, None, "lean", "requested (--lean)"),
    (None, "constrained", None, "headroom is constrained"),
    (None, "ok", None, "no weekly window to pace against"),
    ({"percentUsed": 1.0, "resetsInMinutes": 10080}, "ok", None, "the weekly window has just reset"),
])
def test_intensity_reasons_carry_no_name_prefix(window, headroom, override, why):
    intensity = team_intensity(window, headroom, override)

    assert intensity.why == why and intensity.label == f"{intensity.name} ({why})"


def full_week(monkeypatch):
    monkeypatch.setattr(cli, "fetch_quota", lambda *a, **k: QuotaSnapshot(dict(QUOTA), {"claude-work": FULL_WINDOW}))


def test_plan_only_prints_the_intensity_label_once(fake, monkeypatch, capsys):
    team.write_layouts()
    full_week(monkeypatch)

    rc = run_team(team.QUESTION, layout="site-check", urls=(URL,), plan_only=True, host="claude")

    out = capsys.readouterr().out
    [line] = [line for line in out.splitlines() if line.startswith("intensity:")]
    assert rc == 0 and line == f"intensity: {FULL_LABEL}" and line.count("full") == 1
    [run_dir] = list((state_dir() / "ultra").glob("*/"))
    progress = json.loads((run_dir / "progress.json").read_text())
    plan = json.loads((run_dir / "plan.json").read_text())
    for intensity in (progress["intensity"], plan["intensity"]):
        assert intensity["label"] == FULL_LABEL and intensity["name"] == "full"
        assert intensity["why"] == "87% of the week's quota is spare: 8% used, 59% elapsed"
        assert intensity["label"].count("full") == 1


def test_summary_footer_prints_the_intensity_label_once(fake, monkeypatch):
    team.write_layouts()
    team.standard_analyses(fake)
    full_week(monkeypatch)

    rc = run_team(team.QUESTION, layout="site-check", urls=(URL,), host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0, text
    [line] = [line for line in text.splitlines() if line.startswith("- Intensity:")]
    assert line == f"- Intensity: {FULL_LABEL}; team 8, 3 verifier(s) per claim, 2 critic round(s), 20 claims examined"
    assert line.count("full") == 1
    assert summary["intensity"]["label"] == FULL_LABEL
    assert json.loads((run_dir / "progress.json").read_text())["intensity"]["label"] == FULL_LABEL


def test_team_size_and_rounds_overrides_append_to_the_reason(fake, monkeypatch, capsys):
    team.write_layouts()
    full_week(monkeypatch)

    rc = run_team(team.QUESTION, layout="site-check", urls=(URL,), team_size=2, rounds=0, plan_only=True,
                  host="claude")

    out = capsys.readouterr().out
    assert rc == 0
    assert "intensity: full (87% of the week's quota is spare: 8% used, 59% elapsed; --team-size 2; --rounds 0)\n" in out
