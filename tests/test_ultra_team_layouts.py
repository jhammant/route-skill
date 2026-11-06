"""The built-in team layouts in ``src/route/data/teams.toml``, checked with tomllib alone.

``_lint`` mirrors the rules of ``ultra_team.lint_layout`` so the shipped file
is validated without importing the team module. The layout-specific tests pin
the safety lines in the briefs (never print a secret, never call a payments
API, Lighthouse run by one analyst only, the DNS caveat), because a brief that
loses one of them still parses and lints cleanly.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

PATH = Path(__file__).resolve().parent.parent / "src" / "route" / "data" / "teams.toml"

NAMES = [
    "website-audit", "app-review", "go-live", "code-audit", "architecture", "launch",
    "decision", "due-diligence", "deep-research", "incident", "post-review",
]
FIX = {
    "website-audit": True, "app-review": True, "go-live": True, "code-audit": True,
    "architecture": False, "launch": False, "decision": False, "due-diligence": False,
    "deep-research": False, "incident": True, "post-review": False,
}
TARGETS = {
    "website-audit": "url", "app-review": "any", "go-live": "any", "code-audit": "repo",
    "architecture": "repo", "launch": "any", "decision": "any", "due-diligence": "any",
    "deep-research": "any", "incident": "any", "post-review": "any",
}

ANALYST_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
LAYOUT_KEYS = {"title", "description", "targets", "fix", "report", "analysts"}
ANALYST_KEYS = {"id", "role", "lens", "brief", "tier", "where"}


def _lint(layout: dict) -> list[str]:
    """The shared lint rules, as plain dict checks; one message per problem."""
    problems = []
    for key in ("title", "description", "report"):
        if not isinstance(layout.get(key), str) or not layout[key].strip():
            problems.append(f"{key} is empty")
    if layout.get("targets") not in ("url", "repo", "any"):
        problems.append(f"targets {layout.get('targets')!r}")
    if not isinstance(layout.get("fix"), bool):
        problems.append(f"fix {layout.get('fix')!r} is not a bool")
    analysts = layout.get("analysts") or []
    if not 3 <= len(analysts) <= 8:
        problems.append(f"{len(analysts)} analysts, need 3..8")
    seen = set()
    for analyst in analysts:
        aid = analyst.get("id")
        if not isinstance(aid, str) or not ANALYST_ID.fullmatch(aid):
            problems.append(f"bad id {aid!r}")
        elif aid in seen:
            problems.append(f"duplicate id {aid!r}")
        seen.add(aid)
        for key in ("role", "lens", "brief"):
            if not isinstance(analyst.get(key), str) or not analyst[key].strip():
                problems.append(f"{aid}: {key} is empty")
        if analyst.get("tier") not in ("moderate", "hard"):
            problems.append(f"{aid}: tier {analyst.get('tier')!r}")
        if analyst.get("where") not in ("remote", "session"):
            problems.append(f"{aid}: where {analyst.get('where')!r}")
    return problems


@pytest.fixture(scope="module")
def layouts() -> dict:
    return tomllib.loads(PATH.read_text(encoding="utf-8"))["layouts"]


def _analyst(layouts: dict, name: str, aid: str) -> dict:
    return next(a for a in layouts[name]["analysts"] if a["id"] == aid)


def test_file_parses():
    text = PATH.read_text(encoding="utf-8")

    data = tomllib.loads(text)

    assert isinstance(data.get("layouts"), dict)


def test_exactly_the_built_in_names_in_order(layouts):
    names = list(layouts)

    assert names == NAMES


@pytest.mark.parametrize("name", NAMES)
def test_layout_passes_lint(layouts, name):
    layout = layouts[name]

    problems = _lint(layout)

    assert problems == []


def test_lint_catches_broken_layouts(layouts):
    broken = copy.deepcopy(layouts["incident"])
    broken["fix"] = "yes"
    broken["targets"] = "app"
    broken["report"] = "  "
    broken["analysts"][1]["id"] = broken["analysts"][0]["id"]
    broken["analysts"][2]["id"] = "Bad_ID"
    broken["analysts"][3]["tier"] = "trivial"
    broken["analysts"][3]["where"] = "local"
    broken["analysts"][0]["brief"] = "\n"

    problems = _lint(broken)

    assert "fix 'yes' is not a bool" in problems
    assert "targets 'app'" in problems
    assert "report is empty" in problems
    assert "duplicate id 'hypotheses'" in problems
    assert "bad id 'Bad_ID'" in problems
    assert "red-team: tier 'trivial'" in problems
    assert "red-team: where 'local'" in problems
    assert "hypotheses: brief is empty" in problems
    assert _lint({**layouts["incident"], "analysts": layouts["incident"]["analysts"][:2]}) == [
        "2 analysts, need 3..8"]


@pytest.mark.parametrize("name", NAMES)
def test_built_in_shape_is_tight(layouts, name):
    """Stricter than lint for the shipped file: no stray keys, 4-8 analysts, one-line description."""
    layout = layouts[name]

    analyst_keys = [set(a) for a in layout["analysts"]]

    assert set(layout) == LAYOUT_KEYS
    assert all(keys == ANALYST_KEYS for keys in analyst_keys)
    assert 4 <= len(layout["analysts"]) <= 8
    assert "\n" not in layout["description"].strip()


@pytest.mark.parametrize("name", NAMES)
def test_fix_flag_matches_spec(layouts, name):
    fix = layouts[name]["fix"]

    assert fix is FIX[name]


@pytest.mark.parametrize("name", NAMES)
def test_targets_match_spec(layouts, name):
    targets = layouts[name]["targets"]

    assert targets == TARGETS[name]


def test_app_review_and_go_live_use_the_full_eight(layouts):
    sizes = {name: len(layouts[name]["analysts"]) for name in ("app-review", "go-live")}

    assert sizes == {"app-review": 8, "go-live": 8}


def test_only_app_review_walkthrough_runs_in_the_session(layouts):
    session = {(name, a["id"]) for name, layout in layouts.items()
               for a in layout["analysts"] if a["where"] == "session"}

    walkthrough = _analyst(layouts, "app-review", "walkthrough")["brief"].lower()

    assert session == {("app-review", "walkthrough")}
    assert "never run parallel agents on the screen" in walkthrough


def test_website_audit_has_one_lighthouse_runner(layouts):
    website = layouts["website-audit"]

    runners = [a["id"] for a in website["analysts"] if "lighthouse" in a["brief"].lower()]

    assert website["targets"] == "url" and website["fix"] is True
    assert runners == ["performance"]
    assert "pagespeedonline/v5/runPagespeed" in _analyst(layouts, "website-audit", "performance")["brief"]


def test_website_audit_analysts_in_spec_order(layouts):
    ids = [a["id"] for a in layouts["website-audit"]["analysts"]]

    assert ids == ["seo", "performance", "accessibility", "conversion", "security", "analytics", "mobile"]


def test_analytics_brief_warns_about_blocked_tracker_dns(layouts):
    brief = _analyst(layouts, "website-audit", "analytics")["brief"].lower()

    assert "dns" in brief
    assert "false negative" in brief


def test_security_brief_checks_tls_and_dmarc(layouts):
    brief = _analyst(layouts, "website-audit", "security")["brief"]

    assert "openssl s_client" in brief
    assert "_dmarc" in brief


def test_app_review_reviews_brief_reads_the_itunes_feed(layouts):
    brief = _analyst(layouts, "app-review", "reviews")["brief"]

    assert "itunes.apple.com" in brief


def test_go_live_secrets_brief_never_reports_the_value(layouts):
    brief = _analyst(layouts, "go-live", "secrets")["brief"]

    assert re.search(r"\bnever\b[^.]*\bvalue\b", brief, re.IGNORECASE)


def test_go_live_payments_brief_never_calls_a_payments_api(layouts):
    brief = _analyst(layouts, "go-live", "payments")["brief"].lower()

    assert "never call a payments api" in brief
