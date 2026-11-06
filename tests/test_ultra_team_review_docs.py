"""Regression checks for the team docs' loading and write-safety claims."""

from __future__ import annotations

import re
import shlex
from itertools import takewhile
from pathlib import Path

from route.pools import load_pools, pools_for_host
from route.ultra_research import research_template

ROOT = Path(__file__).resolve().parent.parent


def test_skill_description_has_no_unquoted_yaml_mapping_separator():
    """An unquoted colon followed by whitespace prevents YAML skill loading."""
    text = (ROOT / "skill-ultra" / "SKILL.md").read_text()
    frontmatter = text.split("---", 2)[1]
    line = next(line for line in frontmatter.splitlines() if line.startswith("description:"))

    description = line.partition(":")[2].lstrip()
    # Check the YAML plain-scalar rule without adding a PyYAML dependency.
    plain_scalar = description[:1] not in {"'", '"', "|", ">"}
    mapping_separator = re.search(r":(?:\s|$)", description)

    assert not (plain_scalar and mapping_separator), (
        "The skill description contains ': ' inside an unquoted YAML scalar. "
        "Quote the complete description correctly or remove that separator; "
        "Markdown backticks do not quote YAML."
    )


def test_team_readme_does_not_claim_fix_is_the_only_possible_writer():
    """Team mode reuses research controls, which still allow Claude's Bash."""
    readme = (ROOT / "README.md").read_text()
    teams = readme.split("### Teams\n", 1)[1].split("\n## Swarms:", 1)[0]
    pools = pools_for_host(load_pools(), "claude")

    tokens = shlex.split(research_template("claude", pools, host="claude"))
    tools_start = tokens.index("--disallowedTools") + 1
    denied_tools = list(takewhile(lambda token: not token.startswith("-"), tokens[tools_start:]))
    shell_can_write = "Bash" not in denied_tools
    unconditional_promise = re.search(r"\bOnly\s+`?--fix`?\s+writes\b", teams)

    assert not (shell_can_write and unconditional_promise), (
        "README says only --fix writes, but the reused research dispatch does "
        "not disable Bash. Claude can still write through its shell without "
        "--fix, and git status only warns afterwards. Describe that limit "
        "instead of promising write protection."
    )
