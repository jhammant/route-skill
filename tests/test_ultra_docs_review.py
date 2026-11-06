"""Research guidance must describe the supported arms and actual write controls."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_research_fallback_does_not_offer_kimi():
    """The research command contract rejects Kimi's dispatch program."""
    readme = (ROOT / "README.md").read_text()
    research = readme.split("### Research mode", 1)[1].split(
        "### Spending the second account first", 1
    )[0]

    flag_help = re.search(
        r"(?ms)^- `--allow-lower-models`.*?(?=\n- |\n\n)", research
    )
    assert flag_help is not None
    advertised_help = " ".join(flag_help.group().split())

    assert "lets Sonnet and Kimi in" not in advertised_help, (
        "Research accepts only claude, claude-work and codex dispatch programs; "
        "--allow-lower-models cannot enable the kimi program. Document supported "
        "lower-ranked arms such as Claude Sonnet instead."
    )


def test_research_skill_does_not_promise_shell_write_protection():
    """Removing Claude's file tools leaves shell writes possible, as README says."""
    readme = (ROOT / "README.md").read_text()
    skill = (ROOT / "skill-ultra" / "SKILL.md").read_text()
    research = skill.split("## Research mode", 1)[1].split(
        "## When not to use it", 1
    )[0]

    denied_tools = re.search(r"`--disallowedTools ([^`]+)`", readme)
    assert denied_tools is not None
    shell_is_available = "Bash" not in denied_tools.group(1).split()
    blanket_promise = "Background agents can't edit" in " ".join(research.split())

    assert not (shell_is_available and blanket_promise), (
        "The skill claims agents cannot edit, but the documented controls leave "
        "Bash available. Explain that shell writes remain possible and that the "
        "porcelain comparison only warns afterwards; it does not prevent them."
    )
