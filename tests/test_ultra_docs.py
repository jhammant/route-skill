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
