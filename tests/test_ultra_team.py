"""``route ultra --team`` end to end, with shell scripts standing in for every model.

The fake arm reads its role off the prompt markers: a planner prompt gets
``team.json``, an analyst prompt gets ``analyses/<id>.json`` (or garbage when
a ``.bad`` marker exists), a verifier prompt gets ``verdicts/<claim>.json``
(upheld by default), a critic prompt gets ``critic-<n>.json`` (nothing
missing by default) and a synthesis prompt gets ``report.md`` plus
``fix.json`` when present. The scripts are named ``claude-work`` and
``codex`` because ``research_template`` keys on the program name. Nothing
here reaches a network or a real model; quota is monkeypatched, and no test
depends on the packaged ``data/teams.toml``: every layout is written to the
tmp config directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from route import cli, swarm_dispatch, ultra, ultra_team
from route.pools import load_pools, pools_for_host
from route.storage import state_dir
from route.ultra import ultra_status
from route.ultra_team import (
    Analysis,
    Analyst,
    Finding,
    TeamLayout,
    decide_verdict,
    lint_layout,
    load_layouts,
    merge_findings,
    run_team,
    spread_arms,
    team_intensity,
)

FAKE_ARM = r'''#!/bin/bash
# A model stand-in: the last argument is the prompt; the role is read off its markers.
FAKE_DIR="__FAKE_DIR__"
prompt="${@: -1}"
test "$ROUTE_WORKER" = "1" || { echo "ROUTE_WORKER not set" >&2; exit 97; }
case "$prompt" in
  *ULTRA-TEAM-BEGIN*) role=planner ;;
  *ULTRA-CRITIC-BEGIN*) role=critic ;;
  *ULTRA-VERDICT-BEGIN*) role=verifier ;;
  *ULTRA-ANALYSIS-BEGIN*) role=analyst ;;
  *ULTRA-REPORT-BEGIN*) role=synth ;;
  *) role=unknown ;;
esac
id=$(printf '%s\n' "$prompt" | sed -n 's/^Analyst id: //p' | head -1)
claim=$(printf '%s\n' "$prompt" | sed -n 's/^Claim id: //p' | head -1)
me=$(basename "$0")
opts=("${@:1:$#-1}")
echo "$me|$role|${id:-${claim:-$role}}|${opts[*]}|$PWD" >> "$FAKE_DIR/calls.log"
case "$role" in
  planner)
    n=$(cat "$FAKE_DIR/plans.count" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$FAKE_DIR/plans.count"
    team="$FAKE_DIR/team-$n.json"; [ -f "$team" ] || team="$FAKE_DIR/team.json"
    echo "Thinking..."; echo "ULTRA-TEAM-BEGIN"; cat "$team"; echo "ULTRA-TEAM-END"; exit 0 ;;
  critic)
    n=$(cat "$FAKE_DIR/critic.count" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$FAKE_DIR/critic.count"
    f="$FAKE_DIR/critic-$n.json"
    echo "ULTRA-CRITIC-BEGIN"; if [ -f "$f" ]; then cat "$f"; else echo '{"missing": []}'; fi
    echo "ULTRA-CRITIC-END"; exit 0 ;;
  verifier)
    [ -f "$FAKE_DIR/verdicts/$claim.garbage" ] && { echo "no verdict here"; exit 0; }
    f="$FAKE_DIR/verdicts/$claim-$me.json"; [ -f "$f" ] || f="$FAKE_DIR/verdicts/$claim.json"
    echo "ULTRA-VERDICT-BEGIN"
    if [ -f "$f" ]; then cat "$f"; else echo '{"verdict": "upheld", "reason": "checked it myself", "sources": []}'; fi
    echo "ULTRA-VERDICT-END"; exit 0 ;;
  synth)
    [ -f "$FAKE_DIR/report.missing" ] && { echo "I have nothing."; exit 0; }
    echo "ULTRA-REPORT-BEGIN"; cat "$FAKE_DIR/report.md"; echo "ULTRA-REPORT-END"
    if [ -f "$FAKE_DIR/fix.json" ]; then echo "ULTRA-FIX-BEGIN"; cat "$FAKE_DIR/fix.json"; echo "ULTRA-FIX-END"; fi
    exit 0 ;;
esac
printf '%s\n' "$prompt" | grep -o "ultra-fake: touch [^ ]*" | sort -u | while read -r _ _ path; do touch "$path"; done
if [ -f "$FAKE_DIR/analyses/$id.bad" ] || [ -f "$FAKE_DIR/analyses/$id.bad-$me" ]; then
  echo "garbage without an analysis block"; exit 0
fi
echo "Looking..."; echo "ULTRA-ANALYSIS-BEGIN"; cat "$FAKE_DIR/analyses/$id.json"; echo "ULTRA-ANALYSIS-END"; exit 0
'''

SHAPES = '["coding:implement", "coding:review", "research", "writing"]'
QUOTA = {"claude": "critical", "claude-work": "ok", "codex": "ok"}
QUESTION = "Audit the marketing site before launch"
URL = "https://example.test"

LAYOUT_TOML = """
[layouts.site-check]
title = "Site check"
description = "A small website audit"
targets = "url"
fix = false
report = "A scorecard per area, then the fixes ranked by impact"

[[layouts.site-check.analysts]]
id = "seo"
role = "SEO analyst"
lens = "indexing"
brief = "Check robots.txt and the sitemap"

[[layouts.site-check.analysts]]
id = "perf"
role = "Performance analyst"
lens = "speed"
brief = "Check load time on mobile"

[[layouts.site-check.analysts]]
id = "copy"
role = "Copy analyst"
lens = "conversion"
brief = "Check the headline says what the product does"

[[layouts.site-check.analysts]]
id = "gui"
role = "GUI walker"
lens = "clicks"
brief = "Click through the signup in Chrome"
where = "session"
"""

CODE_LAYOUT_TOML = """
[layouts.code-check]
title = "Code check"
description = "A small code audit"
targets = "repo"
fix = true
report = "Findings by severity"

[[layouts.code-check.analysts]]
id = "seo"
role = "Correctness analyst"
lens = "bugs"
brief = "Find bugs"

[[layouts.code-check.analysts]]
id = "perf"
role = "Performance analyst"
lens = "speed"
brief = "Find slow paths"

[[layouts.code-check.analysts]]
id = "copy"
role = "Docs analyst"
lens = "docs"
brief = "Find stale docs"
"""


def write_pools(tmp_path: Path, bin_dir: Path, *, fable: bool = False) -> None:
    config = Path(os.environ["ROUTE_CONFIG_DIR"])
    config.mkdir(parents=True, exist_ok=True)
    text = textwrap.dedent(f"""
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
    """)
    if fable:
        text += textwrap.dedent(f"""
        [pools.claude-work-fable]
        shapes = {SHAPES}
        remote = true
        dispatch = "{bin_dir}/claude-work --strict-mcp-config --mcp-config {tmp_path}/worker-mcp.json --print --permission-mode auto {{task}}"
        model = {{ name = "claude-fable-5-1" }}
        parallel_limit = 4
        quota_provider = "claude-work"
        """)
    (config / "pools.toml").write_text(text)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Two fake arms named like the real programs, patched quota, no a-mail, no real models."""
    fake_dir = tmp_path / "fake"
    (fake_dir / "analyses").mkdir(parents=True)
    (fake_dir / "verdicts").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude-work", "codex"):
        script = bin_dir / name
        script.write_text(FAKE_ARM.replace("__FAKE_DIR__", str(fake_dir)))
        script.chmod(0o700)
    write_pools(tmp_path, bin_dir)
    monkeypatch.setattr(cli, "fetch_quota", lambda *a, **k: dict(QUOTA))
    monkeypatch.setattr(cli, "fetch_usage", lambda *a, **k: {"claude-work": {"5h": 10.0, "weekly": 20.0}})
    monkeypatch.setattr(swarm_dispatch, "amail_binary", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("ultra must never prompt"))
    return fake_dir


def write_layouts(text: str = LAYOUT_TOML) -> Path:
    config = Path(os.environ["ROUTE_CONFIG_DIR"])
    config.mkdir(parents=True, exist_ok=True)
    path = config / "teams.toml"
    path.write_text(text)
    return path


def finding(identity, claim, *, severity="medium", sources=(), confidence="high", fix=None, evidence="seen"):
    return {"id": identity, "claim": claim, "evidence": evidence, "sources": list(sources), "confidence": confidence,
            "severity": severity, "fix": fix}


def write_analysis(fake_dir, identity, findings, *, summary="what I saw", gaps=()):
    (fake_dir / "analyses" / f"{identity}.json").write_text(
        json.dumps({"summary": summary, "findings": findings, "gaps": list(gaps)}))


def standard_analyses(fake_dir):
    """Three analysts; seo and perf agree on the sitemap, so the run has three claims."""
    write_analysis(fake_dir, "seo", [finding("F1", "The sitemap is missing from robots.txt", severity="high",
                                             sources=[{"id": "S1", "ref": f"{URL}/robots.txt", "note": "robots file"}],
                                             fix="Add a Sitemap: line to robots.txt")])
    write_analysis(fake_dir, "perf", [
        finding("F1", "Sitemap missing from the robots.txt file", severity="medium",
                sources=[{"id": "S1", "ref": f"{URL}/robots.txt", "note": "the robots file"}]),
        finding("F2", "Largest contentful paint is four seconds on mobile", severity="high",
                sources=[{"id": "S2", "ref": "lighthouse run", "note": "LCP 4.0s"}], confidence="medium")],
        gaps=["no field data"])
    write_analysis(fake_dir, "copy", [finding("F1", "The headline does not say what the product does", severity="low",
                                              sources=[{"id": "S1", "ref": f"{URL}/", "note": "the hero"}])])
    (fake_dir / "verdicts" / "C2.json").write_text(json.dumps(
        {"verdict": "uncertain", "reason": "could not run Lighthouse", "sources": []}))
    (fake_dir / "verdicts" / "C3.json").write_text(json.dumps(
        {"verdict": "refuted", "reason": "the headline names the product", "sources": [{"id": "V1", "ref": URL}]}))
    (fake_dir / "report.md").write_text(
        "Not ready: the sitemap is missing [seo:S1]. Mobile is slow [perf:S2]. Unverified [copy:S9].\n")


def calls(fake_dir):
    if not (fake_dir / "calls.log").exists():
        return []
    return [line.split("|") for line in (fake_dir / "calls.log").read_text().splitlines()]


def latest_run():
    [path] = list((state_dir() / "ultra").glob("*/summary.json"))
    return json.loads(path.read_text()), path.parent


def analyst(identity, **fields):
    return Analyst(id=identity, role=fields.get("role", f"{identity} role"), lens=fields.get("lens", "a lens"),
                   brief=fields.get("brief", "a brief"), tier=fields.get("tier", "moderate"),
                   where=fields.get("where", "remote"))


def layout(analysts, **fields):
    return TeamLayout(name="t", title=fields.get("title", "T"), description=fields.get("description", "d"),
                      targets=fields.get("targets", "any"), fix=fields.get("fix", False),
                      report=fields.get("report", "r"), analysts=analysts)


# -- layouts -------------------------------------------------------------------

def test_load_layouts_merges_builtin_and_user_with_user_winning(tmp_path):
    """Built-ins first in file order; a user layout of the same name replaces the built-in."""
    builtin = tmp_path / "builtin.toml"
    builtin.write_text(textwrap.dedent("""
        [layouts.alpha]
        title = "Alpha"
        description = "built-in alpha"
        targets = "any"
        fix = false
        report = "r"
        [[layouts.alpha.analysts]]
        id = "a"
        role = "r"
        lens = "l"
        brief = "b"
        [layouts.beta]
        title = "Beta"
        description = "built-in beta"
        targets = "url"
        fix = true
        report = "r"
    """))
    user = tmp_path / "user.toml"
    user.write_text(textwrap.dedent("""
        [layouts.beta]
        title = "My beta"
        description = "user beta"
        targets = "repo"
        fix = false
        report = "mine"
        [layouts.gamma]
        title = "Gamma"
        description = "user gamma"
        targets = "any"
        fix = false
        report = "r"
    """))

    layouts = load_layouts(builtin=builtin, user=user)

    assert list(layouts) == ["alpha", "beta", "gamma"]
    assert layouts["beta"].title == "My beta" and layouts["beta"].source == "user"
    assert layouts["alpha"].source == "builtin" and layouts["alpha"].analysts[0].tier == "moderate"
    assert layouts["beta"].to_dict()["targets"] == "repo"


def test_load_layouts_without_a_builtin_file_gives_only_user_layouts(tmp_path):
    user = tmp_path / "user.toml"
    user.write_text('[layouts.only]\ntitle = "Only"\ndescription = "d"\ntargets = "any"\nfix = false\nreport = "r"\n')

    layouts = load_layouts(builtin=tmp_path / "missing.toml", user=user)

    assert list(layouts) == ["only"]


def test_load_layouts_tolerates_a_broken_user_file(tmp_path):
    user = tmp_path / "user.toml"
    user.write_text("not = [toml\n")

    assert load_layouts(builtin=tmp_path / "missing.toml", user=user) == {}


def three():
    return [analyst("a"), analyst("b"), analyst("c")]


@pytest.mark.parametrize("bad, message", [
    (layout(three(), title=" "), "title must be non-empty"),
    (layout(three(), description=""), "description must be non-empty"),
    (layout(three(), report=""), "report must be non-empty"),
    (layout(three(), targets="web"), "targets must be one of url, repo, any"),
    (layout(three(), fix="yes"), "fix must be a bool"),
    (layout(three()[:2]), "2 analyst(s); at least 3"),
    (layout([analyst(f"a{i}") for i in range(9)]), "9 analysts; at most 8"),
    (layout([analyst("Bad_Id"), analyst("b"), analyst("c")]), "id must match"),
    (layout([analyst("a"), analyst("a"), analyst("c")]), "duplicate id"),
    (layout([analyst("a", role=" "), analyst("b"), analyst("c")]), "role must be non-empty"),
    (layout([analyst("a", lens=""), analyst("b"), analyst("c")]), "lens must be non-empty"),
    (layout([analyst("a", brief=""), analyst("b"), analyst("c")]), "brief must be non-empty"),
    (layout([analyst("a", tier="easy"), analyst("b"), analyst("c")]), "tier must be one of moderate, hard"),
    (layout([analyst("a", where="cloud"), analyst("b"), analyst("c")]), "where must be one of remote, session"),
])
def test_lint_layout_reports_each_rule(bad, message):
    problems = lint_layout(bad)

    assert any(message in problem for problem in problems), problems


def test_lint_layout_accepts_a_good_layout():
    assert lint_layout(layout(three())) == []


# -- intensity -----------------------------------------------------------------

DAY = 24 * 60


@pytest.mark.parametrize("window, headroom, override, name", [
    ({"percentUsed": 10.0, "resetsInMinutes": 2 * DAY}, "ok", None, "full"),
    ({"percentUsed": 40.0, "resetsInMinutes": 3 * DAY}, "ok", None, "standard"),
    ({"percentUsed": 95.0, "resetsInMinutes": 2 * DAY}, "ok", None, "lean"),
    ({"percentUsed": 10.0, "resetsInMinutes": DAY}, "ok", None, "full"),
    (None, None, None, "standard"),
    ({"percentUsed": None, "resetsInMinutes": 3 * DAY}, "ok", None, "standard"),
    ({"percentUsed": 10.0, "resetsInMinutes": 6 * DAY}, "low", None, "lean"),
    ({"percentUsed": 10.0, "resetsInMinutes": 6 * DAY}, "constrained", None, "lean"),
    ({"percentUsed": 95.0, "resetsInMinutes": 2 * DAY}, "low", "full", "full"),
    ({"percentUsed": 10.0, "resetsInMinutes": 6 * DAY}, "ok", "lean", "lean"),
])
def test_team_intensity_thresholds_overrides_and_the_last_two_days(window, headroom, override, name):
    """Spare quota picks the size; an override or low headroom wins; a day before reset is not zeroed."""
    intensity = team_intensity(window, headroom, override)

    assert intensity.name == name and intensity.why.startswith(name + ":")
    assert (intensity.team_size, intensity.verifiers, intensity.rounds, intensity.max_claims) == \
        ultra_team.INTENSITIES[name]


# -- arms ----------------------------------------------------------------------

def test_spread_arms_puts_every_third_analyst_on_another_family(fake):
    pools = pools_for_host(load_pools(), "claude")
    team = [analyst("a"), analyst("b"), analyst("gui", where="session"), analyst("c"), analyst("d"), analyst("e"),
            analyst("f")]

    arms = spread_arms(team, pools, dict(QUOTA), host="claude", allow_lower=False)

    assert arms == {"a": "claude-work", "b": "claude-work", "c": "codex", "d": "claude-work", "e": "claude-work",
                    "f": "codex"}
    assert "gui" not in arms


def test_spread_arms_sends_hard_analysts_to_the_rank_three_arm(fake, tmp_path):
    write_pools(tmp_path, tmp_path / "bin", fable=True)
    pools = pools_for_host(load_pools(), "claude")
    team = [analyst("a"), analyst("deep", tier="hard"), analyst("c")]

    arms = spread_arms(team, pools, dict(QUOTA), host="claude", allow_lower=False)

    assert arms == {"a": "claude-work", "deep": "claude-work-fable", "c": "codex"}


def test_spread_arms_holds_everyone_without_quota(fake):
    pools = pools_for_host(load_pools(), "claude")
    nothing = {"claude": "critical", "claude-work": "critical", "codex": "critical"}

    arms = spread_arms(three(), pools, nothing, host="claude", allow_lower=False)

    assert arms == {"a": None, "b": None, "c": None}


# -- claims --------------------------------------------------------------------

def test_merge_findings_merges_near_duplicates_keeping_supporters_severity_and_sources():
    analyses = {
        "seo": Analysis("s", [Finding("F1", "The sitemap is missing from robots.txt", "saw it",
                                      [{"id": "S1", "ref": "u1", "note": "n1"}], "high", "medium",
                                      "add the line")]),
        "perf": Analysis("s", [Finding("F1", "Sitemap missing from the robots.txt file", "saw it too",
                                       [{"id": "S1", "ref": "u2", "note": "n2"}], "medium", "blocker", None),
                               Finding("F2", "Largest contentful paint is four seconds", "lighthouse",
                                       [{"id": "S2", "ref": "u3"}], "low", "high", None)]),
    }

    claims = merge_findings(analyses)

    assert [claim.id for claim in claims] == ["C1", "C2"]
    merged = claims[0]
    assert merged.supporters == ["seo", "perf"] and merged.author == "seo"
    assert merged.severity == "blocker" and merged.confidence == "high" and merged.fix == "add the line"
    assert [(src["analyst"], src["id"], src["ref"]) for src in merged.sources] == [("seo", "S1", "u1"),
                                                                                   ("perf", "S1", "u2")]
    assert merged.evidence == ["seo: saw it", "perf: saw it too"]
    assert claims[1].supporters == ["perf"] and claims[1].verdict is None


@pytest.mark.parametrize("verdicts, expected", [
    (["upheld", "upheld", "refuted"], "upheld"),
    (["refuted", "refuted", "uncertain"], "refuted"),
    (["upheld", "refuted"], "contested"),
    (["uncertain", "uncertain", "upheld"], "contested"),
    (["garbage", "upheld"], "contested"),
    (["garbage", "garbage", "garbage"], "contested"),
    (["upheld"], "upheld"),
    ([], "contested"),
])
def test_decide_verdict_needs_a_strict_majority(verdicts, expected):
    assert decide_verdict(verdicts) == expected


# -- the whole run ---------------------------------------------------------------

def test_full_run_analyses_cross_examines_critiques_and_reports(fake, capsys):
    """A user layout, three remote analysts (one on another family), one session analyst, three claims."""
    write_layouts()
    standard_analyses(fake)

    rc = run_team(QUESTION, layout="site-check", urls=(URL,), host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0, text
    assert "Not ready: the sitemap is missing [seo:S1]." in text
    for heading in ("## Contested", "## Refuted", "## For your session", "## Sources", "## Run"):
        assert heading in text, heading
    assert "## Not cross-examined" not in text
    contested = text.split("## Contested")[1].split("## Refuted")[0]
    assert "C2 [high] Largest contentful paint is four seconds on mobile (perf)" in contested
    assert "codex (cross-family): uncertain: could not run Lighthouse" in contested
    refuted = text.split("## Refuted")[1].split("## For your session")[0]
    assert "C3 [low] The headline does not say what the product does (copy)" in refuted
    assert "claude-work (cross-family): refuted: the headline names the product" in refuted
    assert "- gui: GUI walker: Click through the signup in Chrome" in text
    assert f"- [seo:S1] {URL}/robots.txt: robots file" in text
    assert "- [perf:S2] lighthouse run: LCP 4.0s" in text
    assert "- [copy:S9] UNRESOLVED" in text
    assert "- Team: seo: claude-work; perf: claude-work; copy: codex; gui: session" in text
    assert "- Layout: site-check (Site check)" in text
    assert "- Intensity: standard (team 5, 1 verifier(s) per claim, 1 critic round(s), 12 claims examined)" in text
    assert "- Claims: 1 upheld, 1 contested, 1 refuted, 0 not cross-examined; verifiers per claim: 1; " \
           "independence: 3 cross-family, 0 same-family" in text
    assert "- Critic rounds run: 1" in text
    assert "MCP servers loaded: none (add `mcp_servers = [\"name\"]`" in text
    assert "- claude-work: 5h 10.0 -> 10.0, weekly 20.0 -> 20.0" in text
    assert text.rstrip().endswith("- Exit: 0") and capsys.readouterr().out.rstrip().endswith("- Exit: 0")
    assert summary["mode"] == "team" and summary["layout"] == "site-check" and summary["exit_code"] == 0
    assert summary["synthesizer"] == "claude-work" and summary["critic_rounds"] == 1
    assert summary["verdict_counts"] == {"upheld": 1, "contested": 1, "refuted": 1, "not cross-examined": 0}
    by_id = {row["id"]: row for row in summary["analysts"]}
    assert by_id["seo"]["state"] == "analysed" and by_id["copy"]["arm"] == "codex"
    assert by_id["gui"]["state"] == "session" and by_id["gui"]["role"] == "GUI walker"
    claims = json.loads((run_dir / "claims.json").read_text())
    assert [c["id"] for c in claims] == ["C1", "C2", "C3"]
    assert claims[0]["supporters"] == ["seo", "perf"] and claims[0]["verdict"] == "upheld"
    verdicts = json.loads((run_dir / "verdicts.json").read_text())
    assert verdicts[0]["verdicts"][0]["arm"] == "codex" and verdicts[0]["independence"] == "cross-family"
    assert json.loads((run_dir / "analysts" / "seo.json").read_text())["findings"][0]["id"] == "F1"
    assert (run_dir / "analysts" / "perf.json").is_file() and (run_dir / "analysts" / "copy.json").is_file()
    plan = json.loads((run_dir / "plan.json").read_text())
    assert plan["arms"] == {"seo": "claude-work", "perf": "claude-work", "copy": "codex"}
    assert plan["intensity"]["name"] == "standard" and plan["urls"] == [URL]
    progress = json.loads((run_dir / "progress.json").read_text())
    assert progress["mode"] == "team" and progress["state"] == "done" and progress["pids"] == []
    assert not (run_dir / "research-mcp.json").exists()
    rows = calls(fake)
    assert [row[1] for row in rows].count("planner") == 0
    analysts = {row[2]: row for row in rows if row[1] == "analyst"}
    assert analysts["copy"][0] == "codex" and Path(analysts["seo"][4]) == run_dir / "analysts" / "seo"
    assert analysts["seo"][3].startswith("--strict-mcp-config --mcp-config")
    assert "--disallowedTools Edit Write NotebookEdit" in analysts["seo"][3]
    assert [row[0] for row in rows if row[1] == "verifier" and row[2] == "C1"] == ["codex"]
    assert [row[0] for row in rows if row[1] == "verifier" and row[2] == "C3"] == ["claude-work"]
    assert [row[1] for row in rows if row[1] in ("critic", "synth")] == ["critic", "synth"]
    for name in ("logs/analyst-seo.log", "logs/verify-C1-1.log", "logs/critic-1.log", "logs/synthesis.log"):
        assert (run_dir / name).is_file(), name


def test_claims_beyond_the_budget_are_not_cross_examined(fake, monkeypatch):
    write_layouts()
    standard_analyses(fake)
    monkeypatch.setitem(ultra_team.INTENSITIES, "standard", (5, 1, 0, 2))

    rc = run_team(QUESTION, layout="site-check", urls=(URL,), host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0
    assert "## Not cross-examined" in text
    skipped = text.split("## Not cross-examined")[1].split("## For your session")[0]
    assert "C3 [low] The headline does not say what the product does (copy)" in skipped
    assert summary["verdict_counts"]["not cross-examined"] == 1
    assert sorted(row[2] for row in calls(fake) if row[1] == "verifier") == ["C1", "C2"]


def test_malformed_analysis_is_retried_on_the_other_arm(fake):
    write_layouts()
    standard_analyses(fake)
    (fake / "analyses" / "seo.bad-claude-work").write_text("")

    rc = run_team(QUESTION, layout="site-check", urls=(URL,), host="claude")

    summary, run_dir = latest_run()
    assert rc == 0, (run_dir / "summary.md").read_text()
    seo_calls = [row for row in calls(fake) if row[1] == "analyst" and row[2] == "seo"]
    assert [row[0] for row in seo_calls] == ["claude-work", "codex"]
    assert seo_calls[1][3] == "exec --sandbox read-only --skip-git-repo-check"
    seo = next(row for row in summary["analysts"] if row["id"] == "seo")
    assert seo["arm"] == "codex" and seo["attempts"] == ["claude-work", "codex"]
    assert (run_dir / "logs" / "analyst-seo-2.log").is_file()


def test_analyst_failing_on_both_arms_exits_one_with_the_report(fake):
    write_layouts()
    standard_analyses(fake)
    (fake / "analyses" / "seo.bad").write_text("")

    rc = run_team(QUESTION, layout="site-check", urls=(URL,), host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 1
    seo = next(row for row in summary["analysts"] if row["id"] == "seo")
    assert seo["state"] == "failed" and "claude-work: no ULTRA-ANALYSIS-BEGIN" in seo["reason"]
    assert "Failed analysts: seo" in text and "seo: failed (codex)" in text
    assert "## Contested" in text and "## For your session" in text and "## Sources" in text
    assert not (run_dir / "analysts" / "seo.json").exists() and (run_dir / "analysts" / "perf.json").is_file()
    assert json.loads((run_dir / "progress.json").read_text())["state"] == "failed"


def test_unparseable_verdict_counts_as_uncertain(fake):
    write_layouts()
    standard_analyses(fake)
    (fake / "verdicts" / "C1.garbage").write_text("")

    rc = run_team(QUESTION, layout="site-check", urls=(URL,), host="claude")

    summary, _ = latest_run()
    assert rc == 0
    first = next(claim for claim in summary["claims"] if claim["id"] == "C1")
    assert first["verdict"] == "contested" and first["verdicts"][0]["verdict"] == "uncertain"
    assert "unparseable verdict" in first["verdicts"][0]["reason"]


def test_synthesis_failure_falls_back_to_the_deterministic_report(fake):
    write_layouts()
    standard_analyses(fake)
    (fake / "report.missing").write_text("")

    rc = run_team(QUESTION, layout="site-check", urls=(URL,), host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0 and summary["synthesizer"] is None
    assert "No synthesizer delivered a report" in text
    assert "## C1 [high, upheld] The sitemap is missing from robots.txt" in text
    assert "Fix: Add a Sitemap: line to robots.txt" in text
    assert "## C3" not in text.split("## Contested")[0]
    assert "## Contested" in text and "## For your session" in text and "## Sources" in text
    assert f"- [seo:S1] {URL}/robots.txt: robots file" in text


def test_missing_url_target_is_a_note_not_an_error(fake):
    write_layouts()
    standard_analyses(fake)

    rc = run_team(QUESTION, layout="site-check", host="claude")

    summary, run_dir = latest_run()
    assert rc == 0
    assert any("targets a URL but none was given" in note for note in summary["notes"])
    assert "targets a URL but none was given" in (run_dir / "summary.md").read_text()


# -- the critic ------------------------------------------------------------------

CLAIMS = {"a11y": "Button contrast fails the accessibility ratio", "legal": "No privacy policy link in the footer",
          "never": "Nothing here"}


def critic_round(fake_dir, number, identity):
    (fake_dir / f"critic-{number}.json").write_text(json.dumps({"missing": [
        {"id": identity, "role": f"{identity} analyst", "lens": identity, "brief": f"Check {identity}"}]}))
    write_analysis(fake_dir, identity, [finding("F1", CLAIMS[identity], severity="medium",
                                                sources=[{"id": "S1", "ref": f"{URL}/{identity}", "note": "page"}])])


def test_critic_adds_analysts_until_the_round_limit(fake):
    """Two rounds, two additions; claims already examined are not verified again."""
    write_layouts()
    standard_analyses(fake)
    critic_round(fake, 1, "a11y")
    critic_round(fake, 2, "legal")
    critic_round(fake, 3, "never")

    rc = run_team(QUESTION, layout="site-check", urls=(URL,), rounds=2, host="claude")

    summary, run_dir = latest_run()
    assert rc == 0
    rows = calls(fake)
    assert [row[1] for row in rows].count("critic") == 2 and summary["critic_rounds"] == 2
    analysed = [row[2] for row in rows if row[1] == "analyst"]
    assert sorted(analysed[:3]) == ["copy", "perf", "seo"] and analysed[3:] == ["a11y", "legal"]
    verified = [row[2] for row in rows if row[1] == "verifier"]
    assert sorted(verified[:3]) == ["C1", "C2", "C3"] and verified[3:] == ["C4", "C5"]
    by_id = {row["id"]: row for row in summary["analysts"]}
    assert by_id["a11y"]["state"] == "analysed" and by_id["legal"]["state"] == "analysed"
    assert [c["id"] for c in summary["claims"]] == ["C1", "C2", "C3", "C4", "C5"]
    assert "- Critic rounds run: 2" in (run_dir / "summary.md").read_text()
    assert (run_dir / "analysts" / "legal.json").is_file()


def test_critic_stops_when_nothing_is_missing(fake):
    write_layouts()
    standard_analyses(fake)
    critic_round(fake, 2, "never")

    rc = run_team(QUESTION, layout="site-check", urls=(URL,), rounds=2, host="claude")

    summary, _ = latest_run()
    assert rc == 0
    assert [row[1] for row in calls(fake)].count("critic") == 1 and summary["critic_rounds"] == 1
    assert "never" not in {row["id"] for row in summary["analysts"]}


def test_critic_drops_duplicate_ids_and_rounds_zero_skips_it(fake):
    write_layouts()
    standard_analyses(fake)
    (fake / "critic-1.json").write_text(json.dumps({"missing": [
        {"id": "seo", "role": "again", "lens": "l", "brief": "b"}]}))

    rc_zero = run_team(QUESTION, layout="site-check", urls=(URL,), rounds=0, host="claude")
    zero_calls = [row[1] for row in calls(fake)].count("critic")
    (fake / "calls.log").unlink()
    (fake / "critic.count").unlink(missing_ok=True)
    rc_one = run_team("Second look at the site", layout="site-check", urls=(URL,), rounds=1, host="claude")

    assert rc_zero == 0 and zero_calls == 0
    assert rc_one == 0
    runs = sorted((state_dir() / "ultra").glob("*/summary.json"), key=lambda p: p.stat().st_mtime)
    summary = json.loads(runs[-1].read_text())
    assert any("dropped 'seo'" in note and "duplicate id" in note for note in summary["notes"])
    assert sorted(row[2] for row in calls(fake) if row[1] == "analyst") == ["copy", "perf", "seo"]


# -- planner-designed teams --------------------------------------------------------

def planned_team():
    return {"title": "Launch check", "report": "A verdict first", "analysts": [
        {"id": "seo", "role": "SEO analyst", "lens": "indexing", "brief": "Check robots"},
        {"id": "perf", "role": "Performance analyst", "lens": "speed", "brief": "Check LCP"},
        {"id": "copy", "role": "Copy analyst", "lens": "conversion", "brief": "Check the hero"},
        {"id": "gui", "role": "GUI walker", "lens": "clicks", "brief": "Click the signup", "where": "session"}]}


def test_planner_designed_team_runs_from_the_team_block(fake):
    standard_analyses(fake)
    (fake / "team.json").write_text(json.dumps(planned_team()))

    rc = run_team(QUESTION, urls=(URL,), host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0, text
    assert summary["layout"] is None and summary["planner"] == "claude-work" and summary["title"] == "Launch check"
    assert "- Layout: planner-designed (Launch check)" in text
    assert "- gui: GUI walker: Click the signup" in text
    rows = calls(fake)
    assert rows[0][1] == "planner" and Path(rows[0][4]) == run_dir / "plan" / "attempt-1"
    plan = json.loads((run_dir / "plan.json").read_text())
    assert plan["layout"] is None and [a["id"] for a in plan["analysts"]] == ["seo", "perf", "copy", "gui"]
    assert plan["report"] == "A verdict first"


def test_bad_team_is_replanned_once_then_given_up(fake):
    bad = {"title": "t", "report": "r", "analysts": [{"id": "Bad", "role": "r", "lens": "l", "brief": "b"}]}
    (fake / "team-1.json").write_text(json.dumps(bad))
    (fake / "team-2.json").write_text(json.dumps({"title": "t", "report": "r", "analysts": []}))

    rc = run_team(QUESTION, host="claude")

    summary, run_dir = latest_run()
    assert rc == 2
    assert [row[1] for row in calls(fake)] == ["planner", "planner"]
    assert any("id must match" in p for p in summary["plan_problems"])
    assert any("at least 3" in p for p in summary["plan_problems"])
    text = (run_dir / "summary.md").read_text()
    assert "could not plan a team" in text and "## For your session" in text and "## Sources" in text


def test_planner_team_larger_than_the_budget_is_a_problem(fake):
    team = planned_team()
    team["analysts"] = [{"id": f"a{i}", "role": "r", "lens": "l", "brief": "b"} for i in range(6)]
    (fake / "team.json").write_text(json.dumps(team))

    rc = run_team(QUESTION, team_size=4, host="claude")

    summary, _ = latest_run()
    assert rc == 2 and any("the budget is 4" in p for p in summary["plan_problems"])


# -- --fix -----------------------------------------------------------------------

def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.name", "Ultra Test"),
                 ("config", "user.email", "ultra@example.test"), ("config", "commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "README.md").write_text("docs\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True, capture_output=True)
    return repo


def fake_build(monkeypatch, seen, *, rc=0):
    """A stand-in for the build run: records the goal and leaves a run dir behind like the real one."""
    def run_ultra(goal, **kwargs):
        seen.update(goal=goal, **kwargs)
        run_dir = state_dir() / "ultra" / "fake-build"
        run_dir.mkdir(parents=True)
        (run_dir / "progress.json").write_text(json.dumps({"goal": goal, "state": "done"}))
        (run_dir / "summary.json").write_text(json.dumps({
            "run_id": "fake-build", "integration": {"branch": "ultra/fake-build"},
            "merge_command": "git merge --no-ff ultra/fake-build"}))
        return rc

    monkeypatch.setattr(ultra, "run_ultra", run_ultra)


def test_fix_hands_the_code_findings_to_a_build_run(fake, tmp_path, monkeypatch):
    repo = git_repo(tmp_path)
    write_layouts(CODE_LAYOUT_TOML)
    standard_analyses(fake)
    (fake / "fix.json").write_text(json.dumps({"goal": "Add a Sitemap line to robots.txt", "goal_check": "pytest -q"}))
    seen: dict = {}
    fake_build(monkeypatch, seen)

    rc = run_team("Is this ready?", layout="code-check", repo=str(repo), fix=True, host="claude")

    runs = {p.parent.name: json.loads(p.read_text()) for p in (state_dir() / "ultra").glob("*/summary.json")}
    summary = next(s for name, s in runs.items() if name != "fake-build")
    text = (Path(summary["run_dir"]) / "summary.md").read_text()
    assert rc == 0, text
    assert seen["goal"] == "Add a Sitemap line to robots.txt\n\nGoal check: pytest -q"
    assert seen["repo"] == repo.resolve() and seen["base"] == "HEAD" and seen["host"] == "claude"
    assert summary["fix"]["state"] == "built" and summary["fix"]["run_id"] == "fake-build"
    assert summary["fix"]["branch"] == "ultra/fake-build" and summary["fix"]["exit_code"] == 0
    assert "- Fix build:" in text and "  - Run: fake-build" in text and "  - Branch: ultra/fake-build" in text
    assert "  - Merge: git merge --no-ff ultra/fake-build" in text
    synth = next(row for row in calls(fake) if row[1] == "synth")
    assert synth[0] == "claude-work"


def test_fix_build_failure_exits_one(fake, tmp_path, monkeypatch):
    repo = git_repo(tmp_path)
    write_layouts(CODE_LAYOUT_TOML)
    standard_analyses(fake)
    (fake / "fix.json").write_text(json.dumps({"goal": "Fix it", "goal_check": "pytest -q"}))
    fake_build(monkeypatch, {}, rc=1)

    rc = run_team("Is this ready?", layout="code-check", repo=str(repo), fix=True, host="claude")

    assert rc == 1


@pytest.mark.parametrize("fix_block, reason", [
    ({"goal": "Fix it", "goal_check": "rm -rf /"}, "refused: command is not an allowed check"),
    ({"goal": "Fix it", "goal_check": "pytest -q; rm -rf /"}, "refused: shell operators are not allowed"),
    ({"goal": "Fix it"}, "fix block has no 'goal_check'"),
    (None, "no ULTRA-FIX-BEGIN block"),
])
def test_fix_is_skipped_with_a_note_when_the_check_or_block_is_unusable(fake, tmp_path, monkeypatch, fix_block,
                                                                          reason):
    repo = git_repo(tmp_path)
    write_layouts(CODE_LAYOUT_TOML)
    standard_analyses(fake)
    if fix_block is not None:
        (fake / "fix.json").write_text(json.dumps(fix_block))
    seen: dict = {}
    fake_build(monkeypatch, seen)

    rc = run_team("Is this ready?", layout="code-check", repo=str(repo), fix=True, host="claude")

    summary, run_dir = latest_run()
    text = (run_dir / "summary.md").read_text()
    assert rc == 0 and not seen
    assert summary["fix"]["state"] == "skipped" and reason in summary["fix"]["reason"]
    assert "- Fix build: skipped (" in text and reason in text


def test_fix_is_skipped_when_the_layout_does_not_allow_it(fake, tmp_path, monkeypatch):
    repo = git_repo(tmp_path)
    write_layouts()
    standard_analyses(fake)
    (fake / "fix.json").write_text(json.dumps({"goal": "Fix it", "goal_check": "pytest -q"}))
    seen: dict = {}
    fake_build(monkeypatch, seen)

    rc = run_team(QUESTION, layout="site-check", urls=(URL,), repo=str(repo), fix=True, host="claude")

    summary, run_dir = latest_run()
    assert rc == 0 and not seen
    assert summary["fix"] == {"state": "skipped", "reason": "layout 'site-check' has fix = false"}
    assert "- Fix build: skipped (layout 'site-check' has fix = false)" in (run_dir / "summary.md").read_text()


def test_fix_is_skipped_for_planner_designed_teams_and_non_git_repos(fake, tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    write_layouts(CODE_LAYOUT_TOML)
    standard_analyses(fake)
    (fake / "team.json").write_text(json.dumps(planned_team()))
    seen: dict = {}
    fake_build(monkeypatch, seen)

    rc_planned = run_team(QUESTION, repo=str(plain), fix=True, host="claude")
    rc_plain = run_team("Is this ready?", layout="code-check", repo=str(plain), fix=True, host="claude")

    assert rc_planned == 0 and rc_plain == 0 and not seen
    reasons = {json.loads(p.read_text())["fix"]["reason"] for p in (state_dir() / "ultra").glob("*/summary.json")}
    assert any("planner-designed teams never fix" in reason for reason in reasons)
    assert any("is not a git work tree" in reason for reason in reasons)


# -- plan only, listing, status, validation ----------------------------------------

def test_plan_only_writes_the_plan_and_dispatches_nothing(fake, capsys):
    write_layouts()

    rc = run_team(QUESTION, layout="site-check", urls=(URL,), plan_only=True, host="claude")

    [run_dir] = list((state_dir() / "ultra").glob("*/"))
    plan = json.loads((run_dir / "plan.json").read_text())
    assert rc == 0 and calls(fake) == []
    assert plan["arms"] == {"seo": "claude-work", "perf": "claude-work", "copy": "codex"} and plan["held"] == []
    assert plan["layout"] == "site-check" and plan["intensity"]["name"] == "standard"
    out = capsys.readouterr().out
    assert "copy\tremote\tmoderate\tcodex\tCopy analyst" in out and "gui\tsession\tmoderate\t-\tGUI walker" in out
    assert "intensity: standard: standard: no weekly window" in out
    progress = json.loads((run_dir / "progress.json").read_text())
    assert progress["state"] == "planned" and progress["mode"] == "team"
    assert not (run_dir / "summary.md").exists() and not (run_dir / "research-mcp.json").exists()


def test_plan_only_json_prints_the_team(fake, capsys):
    write_layouts()

    rc = run_team(QUESTION, layout="site-check", urls=(URL,), plan_only=True, json_out=True, host="claude")

    payload = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert rc == 0 and payload["layout"] == "site-check" and payload["intensity"]["why"].startswith("standard")


def test_no_usable_arm_for_any_analyst_exits_two(fake, monkeypatch):
    write_layouts()
    monkeypatch.setattr(cli, "fetch_quota", lambda *a, **k: {"claude": "critical", "claude-work": "critical",
                                                            "codex": "critical"})

    rc = run_team(QUESTION, layout="site-check", host="claude")

    summary, _ = latest_run()
    assert rc == 2 and summary["held"] and summary["synthesizer"] is None


@pytest.mark.parametrize("question, kwargs, message", [
    ("   ", {}, "empty question"),
    (QUESTION, {"context": "full"}, "--context must be brief or digest"),
    (QUESTION, {"repo": "/nowhere/at/all"}, "is not a directory"),
    (QUESTION, {"layout": "nope"}, "unknown layout nope; valid:"),
])
def test_validation_failures_exit_two_without_a_run_dir(fake, capsys, question, kwargs, message):
    write_layouts()

    rc = run_team(question, host="claude", **kwargs)

    err = capsys.readouterr().err
    assert rc == 2 and message in err
    if "layout" in kwargs:
        assert "site-check" in err
    assert not list((state_dir() / "ultra").glob("*/"))


def test_invalid_user_layout_exits_two_without_a_run_dir(fake, capsys):
    write_layouts(LAYOUT_TOML.replace('targets = "url"', 'targets = "web"'))

    rc = run_team(QUESTION, layout="site-check", host="claude")

    err = capsys.readouterr().err
    assert rc == 2 and "layout site-check is invalid: targets must be one of url, repo, any" in err
    assert not list((state_dir() / "ultra").glob("*/")) and calls(fake) == []


def test_team_size_trims_remote_analysts_but_keeps_session_ones(fake, capsys):
    write_layouts()

    rc = run_team(QUESTION, layout="site-check", urls=(URL,), team_size=2, plan_only=True, host="claude")

    [run_dir] = list((state_dir() / "ultra").glob("*/"))
    plan = json.loads((run_dir / "plan.json").read_text())
    assert rc == 0
    assert [a["id"] for a in plan["analysts"]] == ["seo", "perf", "gui"]
    assert plan["intensity"]["team_size"] == 2 and "--team-size 2" in plan["intensity"]["why"]
    assert "gui\tsession" in capsys.readouterr().out


def test_route_ultra_teams_lists_user_layouts(fake, capsys):
    write_layouts()

    rc = cli.main(["ultra", "teams"])
    plain = capsys.readouterr().out
    rc_json = cli.main(["ultra", "teams", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0 and rc_json == 0
    assert plain.splitlines()[0] == "name\ttitle\tanalysts\tdescription"
    assert "site-check\tSite check\t4\tA small website audit" in plain
    mine = next(row for row in payload if row["name"] == "site-check")
    assert mine["source"] == "user" and [a["id"] for a in mine["analysts"]] == ["seo", "perf", "copy", "gui"]


def test_ultra_status_shows_a_team_run(fake, capsys):
    write_layouts()
    standard_analyses(fake)
    run_team(QUESTION, layout="site-check", urls=(URL,), host="claude")
    capsys.readouterr()

    rc = ultra_status(None)

    out = capsys.readouterr().out
    assert rc == 0 and ": done" in out and f"goal: {QUESTION}" in out
    assert "seo\tclaude-work\tanalysed" in out and "copy\tcodex\tanalysed" in out and "gui\t-\tsession" in out


# -- CLI ---------------------------------------------------------------------------

def test_parser_team_flags():
    parser = cli._parser("route")

    with_layout = parser.parse_args(["ultra", "--team", "website-audit", "--url", URL, "--url", URL + "/b", "q"])
    bare = parser.parse_args(["ultra", "--team", "q", "--rounds", "0", "--lean", "--fix", "--team-size", "4"])
    const = parser.parse_args(["ultra", "q", "--team", "--thorough"])

    assert with_layout.team == "website-audit" and with_layout.goal == ["q"] and with_layout.urls == [URL, URL + "/b"]
    assert bare.team == "q" and bare.goal == [] and bare.rounds == 0 and bare.intensity == "lean" and bare.fix
    assert bare.team_size == 4
    assert const.team == "" and const.goal == ["q"] and const.intensity == "full"
    assert parser.parse_args(["ultra", "q"]).team is None and parser.parse_args(["ultra", "q"]).urls is None


def test_parser_thorough_and_lean_are_exclusive(capsys):
    with pytest.raises(SystemExit):
        cli._parser("route").parse_args(["ultra", "--team", "q", "--thorough", "--lean"])

    assert "not allowed with argument" in capsys.readouterr().err


def test_cli_team_with_layout_and_question_reaches_run_team(fake, monkeypatch):
    write_layouts()
    seen: dict = {}

    def fake_run_team(question, **kwargs):
        seen.update(question=question, **kwargs)
        return 0

    monkeypatch.setattr("route.ultra_team.run_team", fake_run_team)

    rc = cli.main(["ultra", "--team", "site-check", "Audit", "the", "site", "--plan-only", "--url", URL, "--fix",
                   "--rounds", "0", "--lean"])

    assert rc == 0
    assert seen["layout"] == "site-check" and seen["question"] == "Audit the site"
    assert seen["urls"] == (URL,) and seen["plan_only"] and seen["fix"] and seen["rounds"] == 0
    assert seen["intensity"] == "lean" and seen["host"] == "claude" and seen["repo"] is None


def test_ultra_entry_point_team_without_layout_reaches_run_team(fake, monkeypatch):
    write_layouts()
    seen: dict = {}

    def fake_run_team(question, **kwargs):
        seen.update(question=question, **kwargs)
        return 0

    monkeypatch.setattr("route.ultra_team.run_team", fake_run_team)

    rc = cli.main_ultra(["--team", "Is this ready to go live?", "--plan-only", "--team-size", "3"])

    assert rc == 0
    assert seen["layout"] is None and seen["question"] == "Is this ready to go live?"
    assert seen["team_size"] == 3 and seen["plan_only"] and seen["urls"] == ()


def test_cli_unknown_layout_with_a_question_exits_two(fake, capsys):
    write_layouts()

    rc = cli.main(["ultra", "--team", "nope", "Is this ready?"])

    err = capsys.readouterr().err
    assert rc == 2 and "unknown layout nope; valid:" in err and "site-check" in err
    assert calls(fake) == [] and not list((state_dir() / "ultra").glob("*/"))


def test_cli_research_and_team_together_is_an_error(fake, capsys):
    rc = cli.main(["ultra", "--research", QUESTION, "--team", "site-check"])

    assert rc == 2 and "--research and --team cannot be combined" in capsys.readouterr().err
    assert calls(fake) == []


def test_cli_teams_with_extra_words_is_an_error(fake, capsys):
    rc = cli.main(["ultra", "teams", "extra"])

    assert rc == 2 and "usage: route ultra teams" in capsys.readouterr().err
