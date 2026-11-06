"""Independent review regressions; all agents are local fakes and no network is used."""

from __future__ import annotations

import json
import shlex
import threading
from dataclasses import replace

import pytest

from route import ultra, ultra_research, ultra_team
from route.pools import load_pools, pools_for_host
from tests import test_ultra_fixes_live as live
from tests import test_ultra_research as research
from tests import test_ultra_team as team
from tests.test_ultra_fixes_live import fake as fake


def test_research_same_reference_has_one_source_in_the_saved_answer(fake):
    research.write_plan(fake, [research.part("alpha", "What is alpha?")])
    research.write_answer(fake, "alpha", "Alpha [S1]", [
        {"id": "S1", "ref": "https://example.test/alpha", "note": "first citation"},
        {"id": "S1", "ref": "  https://example.test/alpha\n", "note": "same source again"},
    ])
    (fake / "report.md").write_text("Alpha [alpha:S1].\n")

    rc = ultra_research.run_research("Check duplicate references", host="claude")

    _, run_dir = live.latest_run()
    answer = json.loads((run_dir / "parts" / "alpha.json").read_text())
    assert rc == 0
    assert answer["renamed"] == []
    assert len(answer["sources"]) == 1, "The same stripped ref and id must be one research source"


@pytest.mark.parametrize("sandbox", [
    "--sandbox=read-only",
    "--sandbox=danger-full-access",
    "-s read-only",
])
def test_codex_sandbox_option_spellings_are_replaced(fake, sandbox):
    pools = pools_for_host(load_pools(), "claude")
    pools["codex"] = replace(pools["codex"], dispatch=f"codex exec {sandbox} {{task}}")

    command = shlex.split(ultra_research.research_template("codex", pools, host="claude"))

    assert command == [
        "codex", "exec", "--sandbox", "workspace-write", "-c",
        "sandbox_workspace_write.network_access=true", "--skip-git-repo-check", "{task}",
    ], "The rewritten command must contain one sandbox setting, in every accepted spelling"


@pytest.mark.parametrize("kind", ["research", "team"])
@pytest.mark.parametrize("returncode", [-15, -9])
def test_cancellation_during_invalid_output_does_not_start_a_repair(fake, tmp_path, monkeypatch, kind, returncode):
    pools = pools_for_host(load_pools(), "claude")
    run = ultra.Run(f"cancel-{kind}", "Check cancellation", tmp_path, "", "claude")
    run.dir.mkdir(parents=True)
    (run.dir / "logs").mkdir()
    cancel = threading.Event()
    dispatched = []
    if kind == "research":
        bad = research.answer_text(confidence="certain")
        good = research.answer_text()
    else:
        bad = live.analysis_text([team.finding("F1", "A claim", confidence="certain")])
        good = live.analysis_text([team.finding("F1", "A claim")])

    def cancelled_dispatch(template, prompt, **kwargs):
        dispatched.append(prompt)
        if len(dispatched) == 1:
            # The delegate has printed a complete invalid block before cancellation stops it.
            kwargs["log"].write_text(bad)
            cancel.set()
            return returncode
        kwargs["log"].write_text(good)
        return 0

    monkeypatch.setattr(ultra_research.cli, "_dispatch", cancelled_dispatch)

    if kind == "research":
        result = ultra_research._run_part(
            run, ultra_research.Part("alpha", "Question", "remote"), "claude-work", pools,
            live.QUOTA, "claude", False, "Question", "", [], None,
            run.dir / "parts" / "alpha", None, cancel,
        )
    else:
        result = ultra_team._run_analyst(
            run, team.analyst("alpha"), "claude-work", pools, live.QUOTA, "claude", False,
            "Question", None, run.dir / "analysts" / "alpha", None, cancel,
        )

    assert len(dispatched) == 1, "Cancellation must not launch another model to repair the killed model's output"
    assert result["state"] == "interrupted"
    assert not list((run.dir / "logs").glob("*-repair.log"))


@pytest.mark.parametrize("cd_flag", ["--cd", "-C"])
def test_codex_cannot_override_the_isolated_directory_via_template(fake, tmp_path, monkeypatch, cd_flag):
    repo = team.git_repo(tmp_path)
    research.write_plan(fake, [research.part("alpha", "Inspect files. ultra-fake: touch stray.txt")])
    research.standard_answers(fake)
    monkeypatch.setattr(ultra_research.cli, "fetch_quota", lambda: {**live.QUOTA, "claude-work": "critical"})

    # Model the documented Codex --cd/-C working-root override in the fake CLI.
    # It applies after Popen(cwd=...), before the fake agent reads its brief.
    codex = fake.parent / "bin" / "codex"
    cd_option = r'''
next_is_directory=0
for arg in "$@"; do
  if [ "$next_is_directory" = 1 ]; then cd "$arg" || exit 96; next_is_directory=0; continue; fi
  case "$arg" in
    --cd|-C) next_is_directory=1 ;;
    --cd=*) cd "${arg#--cd=}" || exit 96 ;;
  esac
done
'''
    codex.write_text(codex.read_text().replace('prompt="${@: -1}"', cd_option + '\nprompt="${@: -1}"', 1))
    configured = pools_for_host(load_pools(), "claude")
    configured["codex"] = replace(
        configured["codex"],
        dispatch=f"{shlex.quote(str(codex))} exec {cd_flag} {shlex.quote(str(repo))} {{task}}",
    )
    monkeypatch.setattr(ultra_research.cli, "load_pools", lambda: configured)

    rc = ultra_research.run_research("Audit the repository", repo=repo, host="claude")

    _, run_dir = live.latest_run()
    assert rc in (0, 2)  # A template that bypasses isolation may be refused instead of rewritten.
    assert not (repo / "stray.txt").exists(), "Codex's --cd redirected its writable workspace into --repo"
    if rc == 2:
        assert not (fake / "calls.log").exists()
        return
    [(_, cwd, _)] = live.calls(fake, "part", "alpha")
    assert cwd == run_dir / "parts" / "alpha"
