"""The README and the /ultra skill are how sessions learn research mode; keep them in step with it."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SKILL = ROOT / "skill-ultra" / "SKILL.md"


def frontmatter(text: str) -> str:
    return text.split("---", 2)[1]


@pytest.mark.parametrize("doc", [README, SKILL], ids=["readme", "skill"])
@pytest.mark.parametrize("needle", ["--research", "research.toml", "For your session"])
def test_both_docs_cover_research_mode(doc, needle):
    """A session that reads either file must find the flag, the MCP allowlist and the hand-back heading."""
    text = doc.read_text()

    found = needle in text

    assert found, f"{doc.name} does not mention {needle!r}"


def test_skill_description_triggers_on_research():
    """Sessions pick skills from the frontmatter description, so research must be named there."""
    text = SKILL.read_text()

    lines = [line for line in frontmatter(text).splitlines() if line.startswith("description:")]

    assert len(lines) == 1
    assert "research" in lines[0]


def test_skill_has_a_research_mode_section():
    """The session needs its own steps for writing, running and reporting a research run."""
    text = SKILL.read_text()

    lines = text.splitlines()

    assert "## Research mode" in lines


def test_readme_states_the_read_only_rules():
    """Users add MCP servers to research.toml, so the README must say what keeps agents from writing."""
    text = README.read_text()

    mentions = ("--disallowedTools" in text, "read-only" in text)

    assert mentions == (True, True)


SESSION_PHRASES = [
    "headless browsers",
    "claude.ai connectors (Gmail, Calendar, Drive)",
    "signed-in browser or account (Claude in Chrome, logged-in dashboards)",
    "screen, simulator or physical devices",
]


def research_section(doc: Path) -> str:
    text = doc.read_text()
    if doc == README:
        return text.split("\n### Research mode\n", 1)[1].split("\n### Spending the second account first\n", 1)[0]
    return text.split("\n## Research mode\n", 1)[1].split("\n## Teams\n", 1)[0]


@pytest.mark.parametrize("doc", [README, SKILL], ids=["readme", "skill"])
@pytest.mark.parametrize("needle", ["workspace-write", "network_access=true", "empty directory"])
def test_both_docs_give_research_codex_network_outside_the_repo(doc, needle):
    """Codex research arms need the network for curl and Lighthouse, so they run workspace-write in a directory of their own."""
    section = research_section(doc)

    found = needle in section

    assert found, f"{doc.name} research section does not mention {needle!r}"


@pytest.mark.parametrize("doc", [README, SKILL], ids=["readme", "skill"])
def test_neither_doc_says_codex_runs_read_only(doc):
    """A read-only sandbox has no network; docs that still promise it describe a command that no longer runs."""
    text = doc.read_text()

    stale = "--sandbox read-only" in text

    assert not stale, f"{doc.name} still says Codex runs with --sandbox read-only"


@pytest.mark.parametrize("doc", [README, SKILL], ids=["readme", "skill"])
@pytest.mark.parametrize("phrase", SESSION_PHRASES)
def test_both_docs_say_what_counts_as_session_work(doc, phrase):
    """The planner keeps work remote by these words, so the docs must use the same ones for the same rule."""
    section = research_section(doc)

    found = phrase in section

    assert found, f"{doc.name} research section lacks {phrase!r}"


@pytest.mark.parametrize("doc", [README, SKILL], ids=["readme", "skill"])
@pytest.mark.parametrize("needle", ["-repair.log", "S<max+1>"])
def test_both_docs_describe_repairs_and_source_renames(doc, needle):
    """A repaired part or a renamed source shows up in the footer; the docs must say where it came from."""
    section = research_section(doc)

    found = needle in section

    assert found, f"{doc.name} research section does not mention {needle!r}"
