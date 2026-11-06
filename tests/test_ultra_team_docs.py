"""The README and the /ultra skill are how sessions learn team mode; keep them in step with it."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SKILL = ROOT / "skill-ultra" / "SKILL.md"

EXAMPLES = [
    'ultra --team website-audit --url https://example.com "Audit the marketing site before launch"',
    'ultra --team go-live --repo . --fix "Is this ready to go live?"',
]


def frontmatter(text: str) -> str:
    return text.split("---", 2)[1]


@pytest.mark.parametrize("doc", [README, SKILL], ids=["readme", "skill"])
@pytest.mark.parametrize("needle", ["--team", "website-audit", "app-review", "go-live", "teams.toml", "--fix"])
def test_both_docs_cover_team_mode(doc, needle):
    """A session that reads either file must find the flag, the main layouts, the user file and --fix."""
    text = doc.read_text()

    found = needle in text

    assert found, f"{doc.name} does not mention {needle!r}"


@pytest.mark.parametrize("doc", [README, SKILL], ids=["readme", "skill"])
@pytest.mark.parametrize("example", EXAMPLES, ids=["website-audit", "go-live"])
def test_both_docs_show_the_example_commands(doc, example):
    """The examples are copied as written, so they must stay exact."""
    text = doc.read_text()

    found = example in text

    assert found, f"{doc.name} lacks the example {example!r}"


def test_skill_description_triggers_on_teams():
    """Sessions pick skills from the frontmatter description, so teams and an audit must be named there."""
    text = SKILL.read_text()

    lines = [line for line in frontmatter(text).splitlines() if line.startswith("description:")]

    assert len(lines) == 1
    assert "team" in lines[0]
    assert "website audit" in lines[0] or "website-audit" in lines[0]


def test_skill_has_a_teams_section_before_when_not_to_use_it():
    """The session needs its own steps for choosing a layout, running a team and reporting back."""
    lines = SKILL.read_text().splitlines()

    teams, when_not = lines.index("## Teams"), lines.index("## When not to use it")

    assert teams < when_not


def test_readme_teams_section_sits_before_swarms():
    """Teams belong to the /ultra section, after research mode's own subsections."""
    lines = README.read_text().splitlines()

    order = [lines.index(heading) for heading in (
        "## /ultra: your own ultracode", "### Spending the second account first",
        "### Teams", "## Swarms: cross-model, and they talk")]

    assert order == sorted(order)


@pytest.mark.parametrize("layout", [
    "website-audit", "app-review", "go-live", "code-audit", "architecture", "launch",
    "decision", "due-diligence", "deep-research", "incident", "post-review",
])
def test_readme_table_lists_every_built_in_layout(layout):
    """Users pick a layout from the README table, so it must name all eleven."""
    text = README.read_text()

    row = f"| `{layout}` |"

    assert row in text, f"README layout table has no row for {layout}"


SESSION_PHRASES = [
    "headless browsers",
    "claude.ai connectors (Gmail, Calendar, Drive)",
    "signed-in browser or account (Claude in Chrome, logged-in dashboards)",
    "screen, simulator or physical devices",
]


def teams_section(doc: Path) -> str:
    text = doc.read_text()
    if doc == README:
        return text.split("\n### Teams\n", 1)[1].split("\n## Swarms:", 1)[0]
    return text.split("\n## Teams\n", 1)[1].split("\n## When not to use it\n", 1)[0]


@pytest.mark.parametrize("doc", [README, SKILL], ids=["readme", "skill"])
@pytest.mark.parametrize("needle", ["workspace-write", "network_access=true", "empty directory"])
def test_both_docs_give_team_codex_network_outside_the_repo(doc, needle):
    """Team Codex agents share research's command, so their docs must name the same sandbox and directory."""
    section = teams_section(doc)

    found = needle in section

    assert found, f"{doc.name} teams section does not mention {needle!r}"


@pytest.mark.parametrize("doc", [README, SKILL], ids=["readme", "skill"])
@pytest.mark.parametrize("stale", ["--sandbox read-only", "Codex read-only"])
def test_neither_doc_calls_team_codex_read_only(doc, stale):
    """Team Codex agents now have the network, so a read-only sandbox claim is out of date."""
    section = teams_section(doc)

    found = stale in section

    assert not found, f"{doc.name} teams section still says {stale!r}"


@pytest.mark.parametrize("doc", [README, SKILL], ids=["readme", "skill"])
@pytest.mark.parametrize("phrase", SESSION_PHRASES)
def test_both_docs_keep_the_session_rule_for_teams(doc, phrase):
    """Layouts mark analysts remote or session by the planner's rule, so the teams docs must state it in its words."""
    section = teams_section(doc)

    found = phrase in section

    assert found, f"{doc.name} teams section lacks {phrase!r}"
