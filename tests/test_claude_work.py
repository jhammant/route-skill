"""A second Claude subscription as worker pools, and handoff context levels.

Covers: pools.toml fields for account pools (quota_provider, quota_scope,
prior_like, claude_cli); per-model quota windows gating only their own arm;
prior aliasing; the digest (conversation minus tool output, mid-turn messages
kept); and how brief/digest/fork shape one dispatch.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

from route import swarm_dispatch
from route.cli import main
from route.digest import build_digest, find_transcript, prepare_handoff, turns_from_transcript
from route.eligibility import gate, parse_quota, quota_keys, quota_provider
from route.pools import load_pools
from route.router import Priors, load_priors
from route.storage import config_dir
from route.surplus import parse_weekly

WORK_POOLS = """
[pools.claude-work]
shapes = ["coding:implement", "coding:refactor", "coding:debug", "research", "writing"]
remote = true
cost = "included"
strength = 6
dispatch = "claude-work --print --permission-mode auto --model claude-opus-5-5 {task}"
model = { name = "claude-opus-5-5" }
parallel_limit = 4
quota_provider = "claude-work"
prior_like = "claude"
claude_cli = true

[pools.claude-work-fable]
shapes = ["coding:implement", "coding:debug"]
remote = true
cost = "included"
strength = 7
dispatch = "claude-work --print --permission-mode auto --model claude-fable-5-1 {task}"
model = { name = "claude-fable-5-1" }
quota_provider = "claude-work"
quota_scope = "Fable"
claude_cli = true
"""


def write_pools(text: str = WORK_POOLS) -> dict:
    cfg = config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "pools.toml").write_text(text, encoding="utf-8")
    return load_pools()


# -- pools.toml fields --------------------------------------------------------

def test_account_pool_fields_load_from_pools_toml():
    pools = write_pools()
    work, fable = pools["claude-work"], pools["claude-work-fable"]
    assert (work.quota_provider, work.quota_scope, work.prior_like, work.claude_cli) == (
        "claude-work", "", "claude", True)
    assert (fable.quota_provider, fable.quota_scope) == ("claude-work", "Fable")
    # Shipped arms keep the built-in mapping.
    assert pools["codex"].quota_provider is None
    assert quota_provider("codex", pools) == "codex"


def test_quota_keys_add_the_model_window_only_for_scoped_arms():
    pools = write_pools()
    assert quota_keys("claude-work", pools) == ["claude-work"]
    assert quota_keys("claude-work-fable", pools) == ["claude-work", "claude-work:Fable"]
    assert quota_keys("local-batch", pools) == []


def test_prior_like_pointing_at_itself_is_refused():
    try:
        write_pools(WORK_POOLS.replace('prior_like = "claude"', 'prior_like = "claude-work"'))
    except ValueError as exc:
        assert "itself" in str(exc)
    else:
        raise AssertionError("expected a ValueError")


# -- per-model quota windows --------------------------------------------------

def _work_provider(weekly: float, fable: float) -> list[dict]:
    return [{"id": "claude-work", "ok": True, "limits": [
        {"label": "weekly", "percent": weekly, "resetsAt": "2099-01-01T00:00:00Z"},
        {"label": "Fable weekly", "percent": fable, "scope": "Fable", "resetsAt": "2099-01-01T00:00:00Z"},
    ]}]


def test_a_spent_model_window_does_not_mark_the_whole_pool_critical():
    quota = parse_quota(_work_provider(weekly=30, fable=99))
    assert quota == {"claude-work": "ok", "claude-work:Fable": "critical"}


def test_agent_scoped_windows_become_claude_model_keys():
    agent = {"ok": True, "headroom": "comfortable", "scoped": [{"label": "Fable", "percentUsed": 82}]}
    assert parse_quota(agent) == {"claude": "comfortable", "claude:Fable": "low"}


def test_spent_fable_week_removes_only_the_fable_arm():
    pools = write_pools()
    result = gate("implement the parser in src/parse.py with tests", "coding:implement", pools,
                  quota=parse_quota(_work_provider(weekly=30, fable=99)))
    assert "claude-work-fable" in result.removed_by_quota
    assert "claude-work" in result.eligible


def test_spent_account_removes_every_arm_on_it():
    pools = write_pools()
    result = gate("implement the parser in src/parse.py with tests", "coding:implement", pools,
                  quota=parse_quota(_work_provider(weekly=97, fable=10)))
    assert {"claude-work", "claude-work-fable"} <= set(result.removed_by_quota)


def test_weekly_windows_keep_model_weeks_apart():
    weekly = parse_weekly({"ok": False}, _work_provider(weekly=30, fable=70))
    assert weekly["claude-work"]["percentUsed"] == 30
    assert weekly["claude-work:Fable"]["percentUsed"] == 70


# -- priors ---------------------------------------------------------------------

def test_prior_like_borrows_the_named_arms_priors():
    priors = load_priors().with_aliases({"claude-work": "claude"})
    assert priors.prior("coding:implement", "moderate", "claude-work") == priors.prior(
        "coding:implement", "moderate", "claude")
    # An arm the rules name explicitly keeps its own prior.
    assert priors.prior("coding:implement", "moderate", "codex") == (8.0, 2.0)


def test_unaliased_new_arm_stays_unfavoured():
    priors = load_priors()
    assert priors.prior("coding:implement", "moderate", "claude-work-fable") == tuple(priors.unfavoured)


def test_alias_cycle_cannot_recurse_forever():
    priors = Priors({"rules": {}}).with_aliases({"a": "b", "b": "a"})
    assert priors.prior("coding:implement", None, "a") == tuple(priors.unfavoured)


# -- digest ------------------------------------------------------------------------

def _transcript(tmp_path: Path, session: str, entries: list[dict]) -> Path:
    project = tmp_path / "claude-home" / "projects" / "-some-project"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


SESSION = "0123abcd-0000-4000-8000-000000000001"


def _entries() -> list[dict]:
    return [
        {"type": "user", "message": {"role": "user", "content":
            "Build the parser.<system-reminder>private memory index</system-reminder>"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Reading the module first."},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/repo/src/parse.py"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "SECRET FILE BODY " * 50}]}},
        {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "use the stdlib only",
                                              "origin": {"kind": "human"}}},
        {"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": "subagent chatter"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q", "description": "Run tests"}},
            {"type": "text", "text": "Agreed: stdlib only, tokens as tuples."},
        ]}},
    ]


def test_digest_keeps_the_conversation_and_drops_tool_output(tmp_path):
    path = _transcript(tmp_path, SESSION, _entries())
    digest = build_digest(path, SESSION)
    assert "Build the parser." in digest
    assert "- Read: /repo/src/parse.py" in digest
    assert "- Bash: Run tests: pytest -q" in digest
    assert "Agreed: stdlib only" in digest
    assert "SECRET FILE BODY" not in digest
    assert "private memory index" not in digest
    assert "subagent chatter" not in digest


def test_digest_keeps_messages_sent_mid_turn(tmp_path):
    turns = turns_from_transcript(_transcript(tmp_path, SESSION, _entries()))
    assert any("(sent mid-turn) use the stdlib only" in t.text and t.role == "user" for t in turns)


def test_digest_over_budget_keeps_the_goal_and_the_newest_turn(tmp_path):
    entries = [{"type": "user", "message": {"role": "user", "content": "GOAL: ship it"}}]
    for i in range(40):
        role = "assistant" if i % 2 == 0 else "user"
        entries.append({"type": role, "message": {"role": role, "content": f"turn {i} " + "x" * 400}})
    entries.append({"type": "assistant", "message": {"role": "assistant", "content": "NEWEST " + "y" * 3000}})
    digest = build_digest(_transcript(tmp_path, SESSION, entries), SESSION, max_chars=2500)
    assert "GOAL: ship it" in digest
    assert "NEWEST" not in digest or digest.index("GOAL") < digest.index("y" * 50)
    assert "y" * 50 in digest  # the newest turn survives, trimmed
    assert "earlier turns omitted" in digest
    assert len(digest) <= 2500 + 200


def test_find_transcript_rejects_path_like_ids(tmp_path):
    _transcript(tmp_path, SESSION, _entries())
    assert find_transcript("../../etc/passwd", tmp_path / "claude-home") is None
    assert find_transcript(SESSION, tmp_path / "claude-home") is not None


# -- handoff levels ------------------------------------------------------------

TEMPLATE = "claude-work --print --permission-mode auto {task}"


def test_fork_on_a_claude_cli_arm_resumes_the_parent_session(tmp_path):
    _transcript(tmp_path, SESSION, _entries())
    h = prepare_handoff("fork", "do it", TEMPLATE, session_id=SESSION, claude_cli=True,
                        home=tmp_path / "claude-home")
    assert h.level == "fork"
    assert h.template == f"claude-work --print --permission-mode auto --resume {SESSION} --fork-session {{task}}"
    assert h.text == "do it"


def test_fork_on_another_arm_becomes_a_digest(tmp_path):
    _transcript(tmp_path, SESSION, _entries())
    h = prepare_handoff("fork", "do it", "codex exec {task}", session_id=SESSION, claude_cli=False,
                        home=tmp_path / "claude-home")
    assert h.level == "digest"
    assert h.digest_path is not None and h.digest_path.is_file()
    assert oct(h.digest_path.stat().st_mode & 0o777) == "0o600"
    assert h.text.startswith("Before you start, read the handoff digest at")
    assert h.text.endswith("do it")
    assert h.template == "codex exec {task}"


def test_missing_transcript_degrades_to_brief_and_says_why(tmp_path):
    h = prepare_handoff("digest", "do it", TEMPLATE, session_id=SESSION, claude_cli=True,
                        home=tmp_path / "nowhere")
    assert (h.level, h.text) == ("brief", "do it")
    assert "no transcript" in h.note


def test_plan_only_digest_writes_nothing(tmp_path):
    _transcript(tmp_path, SESSION, _entries())
    h = prepare_handoff("digest", "do it", TEMPLATE, session_id=SESSION, claude_cli=True,
                        plan_only=True, home=tmp_path / "claude-home")
    assert h.level == "digest" and h.digest_path is None and h.text == "do it"


# -- end to end through the CLI -------------------------------------------------

def test_cli_plan_reports_the_context_level(tmp_path, monkeypatch, capsys):
    write_pools()
    _transcript(tmp_path, SESSION, _entries())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    rc = main(["implement the parser in src/parse.py with tests", "--pool", "claude-work",
               "--context", "fork", "--session", SESSION, "--plan-only", "--no-quota"])
    out = capsys.readouterr().out
    plan = json.loads(out[: out.rindex("}") + 1])
    assert rc == 0
    assert plan["chosen"] == "claude-work"
    assert plan["context"] == {"requested": "fork", "level": "fork", "note": f"forks session {SESSION}"}


def test_cli_digest_command_prints_the_digest(tmp_path, monkeypatch, capsys):
    _transcript(tmp_path, SESSION, _entries())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    assert main(["digest", "--session", SESSION]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"# Handoff digest: session {SESSION}")
    assert "SECRET FILE BODY" not in out


# -- swarms ------------------------------------------------------------------------

def test_swarm_members_on_a_claude_cli_pool_get_the_amail_mcp_config(tmp_path, monkeypatch):
    pools = write_pools()
    monkeypatch.setattr(swarm_dispatch, "mcp_runtime", lambda: ("/usr/bin/node", "/bridge.js"))
    worker = types.SimpleNamespace(environment=lambda: {"AMAIL_CREDENTIALS": "/c.json", "AMAIL_SERVER": "s"})
    ctx = types.SimpleNamespace(template=pools["claude-work"].dispatch, pools=pools)
    assignment = types.SimpleNamespace(arm="claude-work", unit={"id": "u1"}, ctx=ctx)
    argv, env = swarm_dispatch.dispatch_command(assignment, worker, tmp_path, allow_network=False)
    assert argv[0] == "claude-work"
    assert argv[1:3] == ["--mcp-config", str(tmp_path / "mcp" / "u1.json")]
    config = json.loads((tmp_path / "mcp" / "u1.json").read_text())
    assert config["mcpServers"]["amail"]["env"]["AMAIL_SERVER"] == "s"


def test_shipped_example_config_loads(tmp_path):
    example = Path(__file__).resolve().parent.parent / "examples" / "claude-work-pools.toml"
    pools = write_pools(example.read_text(encoding="utf-8").replace("HOME/", f"{tmp_path}/"))
    for name in ("claude-work", "claude-work-fable", "claude-work-sonnet"):
        assert pools[name].claude_cli and pools[name].quota_provider == "claude-work"
        template = pools[name].dispatch
        # --mcp-config is variadic: an option must follow it, never the task.
        after = template.split("--mcp-config", 1)[1].split()
        assert after[1].startswith("--"), template
    assert pools["claude-work-fable"].quota_scope == "Fable"
    assert pools["claude-work-sonnet"].dispatch_for("batch:summarize")[1] is True  # task on stdin
