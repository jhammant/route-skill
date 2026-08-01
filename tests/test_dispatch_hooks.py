"""SPEC 6-7: the dispatch hook seam.

Hooks are an opt-in, vendor-neutral observability point. These tests pin the
two things a consumer depends on: which hooks get discovered, and the fact
that a broken hook cannot affect routing.
"""

from __future__ import annotations

import json as _json
import os
import stat
import subprocess as _subprocess
import time
from pathlib import Path

import pytest

from route import cli
from route.hooks import (
    HOOK_CONTEXT_MAX_CHARS,
    HOOK_DECISION_MAX_CHARS,
    HOOK_PREPEND_MAX_CHARS,
    HOOKS_DIRNAME,
    discover_hooks,
    fire,
    parse_decision,
)
from route.storage import config_dir


def _make_hook(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_no_hooks_configured_returns_empty():
    assert discover_hooks() == []


def test_discovers_executables_from_config_dir_in_sorted_order():
    d = config_dir() / HOOKS_DIRNAME
    _make_hook(d / "20-second")
    _make_hook(d / "10-first")
    assert [p.name for p in discover_hooks()] == ["10-first", "20-second"]


def test_ignores_non_executable_files():
    d = config_dir() / HOOKS_DIRNAME
    _make_hook(d / "10-yes")
    (d / "20-no").write_text("#!/bin/sh\n")
    assert [p.name for p in discover_hooks()] == ["10-yes"]


def test_ignores_subdirectories():
    d = config_dir() / HOOKS_DIRNAME
    _make_hook(d / "10-yes")
    (d / "20-dir").mkdir()
    assert [p.name for p in discover_hooks()] == ["10-yes"]


def test_env_override_replaces_the_directory(monkeypatch, tmp_path):
    _make_hook(config_dir() / HOOKS_DIRNAME / "10-ignored")
    a = _make_hook(tmp_path / "a")
    b = _make_hook(tmp_path / "b")
    monkeypatch.setenv("ROUTE_DISPATCH_HOOKS", os.pathsep.join([str(a), str(b)]))
    assert discover_hooks() == [a, b]


def test_env_override_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ROUTE_DISPATCH_HOOKS", "~/myhook")
    assert discover_hooks() == [tmp_path / "myhook"]


def test_fire_passes_payload_as_json_on_stdin(tmp_path):
    seen = tmp_path / "seen.json"
    hook = _make_hook(tmp_path / "h", f"#!/bin/sh\ncat > {seen}\n")
    fire(hook, {"event": "decision", "plan": {"chosen": "local"}})
    assert _json.loads(seen.read_text())["plan"]["chosen"] == "local"


def test_fire_returns_stripped_stdout(tmp_path):
    hook = _make_hook(tmp_path / "h", "#!/bin/sh\necho '  ISSUE-42  '\n")
    assert fire(hook, {}) == "ISSUE-42"


def test_fire_truncates_oversized_stdout(tmp_path):
    hook = _make_hook(
        tmp_path / "h", "#!/bin/sh\nhead -c 9000 /dev/zero | tr '\\0' 'x'\n"
    )
    assert len(fire(hook, {})) == 4096


def test_fire_truncation_counts_characters_not_bytes(tmp_path):
    """The cap is documented in characters, so multi-byte output must not be
    cut short at 4096/N characters — nor allowed past 4096 characters."""
    hook = _make_hook(
        tmp_path / "h",
        "#!/bin/sh\nfor i in $(seq 1 5000); do printf '\\303\\251'; done\n",
    )
    out = fire(hook, {})
    assert len(out) == 4096
    assert set(out) == {"\u00e9"}


def test_fire_returns_empty_on_nonzero_exit_but_does_not_raise(tmp_path):
    hook = _make_hook(tmp_path / "h", "#!/bin/sh\necho out\nexit 3\n")
    assert fire(hook, {}) == "out"


def test_fire_survives_missing_hook(tmp_path):
    assert fire(tmp_path / "nope", {}) == ""


def test_fire_survives_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("route.hooks.HOOK_TIMEOUT_S", 1)
    hook = _make_hook(tmp_path / "h", "#!/bin/sh\nsleep 5\n")
    assert fire(hook, {}) == ""


def _recording_hook(tmp_path, monkeypatch):
    """Install a hook that appends each payload to events.jsonl and echoes an id.

    ``fire()`` sends the payload with no trailing newline, so the hook adds
    one itself before appending — otherwise back-to-back invocations (decision
    then complete) would concatenate onto a single line.
    """
    log = tmp_path / "events.jsonl"
    hook = _make_hook(
        tmp_path / "rec",
        f"#!/bin/sh\ncat >> '{log}'\nprintf '\\n' >> '{log}'\necho 'ISSUE-1'\n",
    )
    monkeypatch.setenv("ROUTE_DISPATCH_HOOKS", str(hook))
    return log


def _events(log: Path) -> list[dict]:
    return [_json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_stay_path_fires_both_events(tmp_path, monkeypatch, capsys):
    log = _recording_hook(tmp_path, monkeypatch)
    rc = cli.main_auto(["--pool", "claude", "write the docs"])
    assert rc == 0
    events = _events(log)
    assert [e["event"] for e in events] == ["decision", "complete"]
    assert events[0]["plan"]["chosen"] == "claude"
    assert events[1]["exit_code"] == 0


def test_hook_context_round_trips_from_decision_to_complete(tmp_path, monkeypatch):
    log = _recording_hook(tmp_path, monkeypatch)
    cli.main_auto(["--pool", "claude", "write the docs"])
    assert _events(log)[1]["hook_context"] == "ISSUE-1"


def test_dispatch_path_reports_real_exit_code(tmp_path, monkeypatch):
    log = _recording_hook(tmp_path, monkeypatch)

    def _slow_dispatch(template, task):
        time.sleep(0.05)
        return 7

    monkeypatch.setattr(cli, "_dispatch", _slow_dispatch)
    rc = cli.main_auto(["--pool", "local-agent", "write the docs"])
    assert rc == 7
    complete = _events(log)[1]
    assert complete["exit_code"] == 7
    assert complete["wall_clock_s"] >= 0.05


def test_broken_hook_does_not_change_return_code(tmp_path, monkeypatch):
    hook = _make_hook(tmp_path / "bad", "#!/bin/sh\nexit 9\n")
    monkeypatch.setenv("ROUTE_DISPATCH_HOOKS", str(hook))
    monkeypatch.setattr(cli, "_dispatch", lambda template, task: 0)
    assert cli.main_auto(["--pool", "local-agent", "write the docs"]) == 0


def test_keyboard_interrupt_fires_complete_as_130_and_propagates(
    tmp_path, monkeypatch
):
    """decision fired -> complete must fire even if _dispatch raises.

    Ctrl-C is the one case that reports 130, the shell's SIGINT convention.
    The exception must still propagate unchanged — this seam is advisory,
    never a replacement for real error handling.
    """
    log = _recording_hook(tmp_path, monkeypatch)

    def _raising_dispatch(template, task):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_dispatch", _raising_dispatch)
    with pytest.raises(KeyboardInterrupt):
        cli.main_auto(["--pool", "local-agent", "write the docs"])
    events = _events(log)
    assert [e["event"] for e in events] == ["decision", "complete"]
    assert events[1]["exit_code"] == 130
    assert events[1]["exception"] == "KeyboardInterrupt"


def test_other_exception_is_not_reported_as_an_interrupt(tmp_path, monkeypatch):
    """A broken pool config must not look like the user pressing Ctrl-C.

    ``shlex.split`` on a malformed dispatch template raises ValueError. If
    that reported 130 too, a consumer scoring arms could not tell a user's
    interrupt from a configuration bug and would punish the arm for both.
    """
    log = _recording_hook(tmp_path, monkeypatch)

    def _raising_dispatch(template, task):
        raise ValueError("No closing quotation")

    monkeypatch.setattr(cli, "_dispatch", _raising_dispatch)
    with pytest.raises(ValueError):
        cli.main_auto(["--pool", "local-agent", "write the docs"])
    complete = _events(log)[1]
    assert complete["exit_code"] == 1
    assert complete["exception"] == "ValueError"


def test_successful_dispatch_reports_no_exception(tmp_path, monkeypatch):
    log = _recording_hook(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_dispatch", lambda template, task: 0)
    cli.main_auto(["--pool", "local-agent", "write the docs"])
    assert _events(log)[1]["exception"] is None


def test_no_hooks_configured_does_not_invoke_subprocess(monkeypatch):
    """The default path must not gain a subprocess call.

    ``ROUTE_DISPATCH_HOOKS`` is deliberately unset (not set to ``""``) —
    ``discover_hooks()`` treats an empty string as an override to an empty
    hook list, which is a different code path from the unset default real
    users hit. ``--shape``/``--tier``/``--no-quota`` sidestep the
    *pre-existing* subprocess calls in
    ``classify_shape``/``estimate_complexity``/``fetch_quota`` so this test
    isolates the one thing it's checking: that the hook seam itself doesn't
    invoke a subprocess when unconfigured.
    """
    monkeypatch.delenv("ROUTE_DISPATCH_HOOKS", raising=False)
    calls = []
    real_run = _subprocess.run
    monkeypatch.setattr(
        _subprocess, "run", lambda *a, **k: (calls.append(a), real_run(*a, **k))[1]
    )
    cli.main_auto(
        [
            "--pool",
            "claude",
            "--shape",
            "writing",
            "--tier",
            "trivial",
            "--no-quota",
            "write the docs",
        ]
    )
    assert calls == []


# --- offered context (`prepend`) ----------------------------------------
#
# A `decision` hook may offer context to prepend to the dispatched task. The
# tests below pin the two halves of that bargain: a hook can offer, and route
# alone decides whether the offer is taken.


def test_plain_stdout_is_an_opaque_context_with_no_prepend():
    assert parse_decision("ISSUE-1") == ("ISSUE-1", "")


def test_json_object_without_a_prepend_key_stays_opaque():
    """A hook already emitting JSON as its context keeps meaning that.

    The discriminator is the `prepend` key, not "looks like JSON" — otherwise
    this seam would silently redefine the output of every hook that happens
    to serialise its correlation token.
    """
    raw = '{"issue": "ISSUE-1"}'
    assert parse_decision(raw) == (raw, "")


def test_structured_form_splits_context_from_prepend():
    raw = _json.dumps({"hook_context": "ISSUE-1", "prepend": "## Memory\nthing"})
    assert parse_decision(raw) == ("ISSUE-1", "## Memory\nthing")


def test_prepend_without_a_context_yields_an_empty_context():
    assert parse_decision('{"prepend": "background"}') == ("", "background")


@pytest.mark.parametrize(
    "raw",
    [
        '{"prepend": "unterminated',
        '{"prepend": ["not", "a", "string"]}',
        '{"prepend": null}',
    ],
)
def test_malformed_or_mistyped_prepend_never_raises(raw):
    """Fail open: a garbled hook loses its prepend, not the dispatch."""
    context, prepend = parse_decision(raw)
    assert prepend == ""
    assert isinstance(context, str)


def test_each_field_is_capped_independently():
    raw = _json.dumps({"hook_context": "c" * 9000, "prepend": "p" * 9000})
    context, prepend = parse_decision(raw)
    assert len(context) == HOOK_CONTEXT_MAX_CHARS
    assert len(prepend) == HOOK_PREPEND_MAX_CHARS


def test_a_large_structured_payload_survives_the_pipe(tmp_path, monkeypatch):
    """Truncation must land inside a value, not break the parse.

    Capping `decision` stdout at the context cap would cut a full-sized
    object mid-string and demote a well-formed hook to the plain-text path,
    where its entire JSON body would become the correlation token.
    """
    body = "P" * (HOOK_PREPEND_MAX_CHARS + 2000)
    payload = _json.dumps({"hook_context": "ISSUE-1", "prepend": body})
    hook = _make_hook(
        tmp_path / "big", f"#!/bin/sh\ncat > /dev/null\ncat <<'EOF'\n{payload}\nEOF\n"
    )
    monkeypatch.setenv("ROUTE_DISPATCH_HOOKS", str(hook))
    context, prepend = parse_decision(
        fire(hook, {"event": "decision"}, max_chars=HOOK_DECISION_MAX_CHARS)
    )
    assert context == "ISSUE-1"
    assert prepend == "P" * HOOK_PREPEND_MAX_CHARS


def _prepending_hook(tmp_path, monkeypatch, prepend="## Memory\nlocal arm is slow"):
    payload = _json.dumps({"hook_context": "ISSUE-1", "prepend": prepend})
    hook = _make_hook(
        tmp_path / "prep",
        f"#!/bin/sh\ncat > /dev/null\ncat <<'EOF'\n{payload}\nEOF\n",
    )
    monkeypatch.setenv("ROUTE_DISPATCH_HOOKS", str(hook))
    return hook


def _capture_dispatch(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        cli, "_dispatch", lambda template, task: (seen.append(task), 0)[1]
    )
    return seen


def test_offered_context_reaches_the_arm_ahead_of_the_task(tmp_path, monkeypatch):
    _prepending_hook(tmp_path, monkeypatch)
    seen = _capture_dispatch(monkeypatch)
    assert cli.main_auto(["--pool", "local-agent", "write the docs"]) == 0
    assert seen == ["## Memory\nlocal arm is slow\n\nwrite the docs"]


def test_no_prepend_dispatches_the_task_byte_for_byte(tmp_path, monkeypatch):
    _recording_hook(tmp_path, monkeypatch)
    seen = _capture_dispatch(monkeypatch)
    cli.main_auto(["--pool", "local-agent", "write the docs"])
    assert seen == ["write the docs"]


def test_the_recorded_task_is_the_one_the_user_typed(tmp_path, monkeypatch):
    """Injected context is dispatch-only.

    It must not reach the hook payloads, or a consumer would attribute
    routing evidence to text the user never wrote — and, once federated,
    so would everyone else.
    """
    log = tmp_path / "events.jsonl"
    payload = _json.dumps({"hook_context": "ISSUE-1", "prepend": "background"})
    hook = _make_hook(
        tmp_path / "prep",
        f"#!/bin/sh\ncat >> '{log}'\nprintf '\\n' >> '{log}'\n"
        f"cat <<'EOF'\n{payload}\nEOF\n",
    )
    monkeypatch.setenv("ROUTE_DISPATCH_HOOKS", str(hook))
    _capture_dispatch(monkeypatch)
    cli.main_auto(["--pool", "local-agent", "write the docs"])
    assert [e["task"] for e in _events(log)] == ["write the docs", "write the docs"]


def test_structured_context_still_round_trips_to_complete(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    payload = _json.dumps({"hook_context": "ISSUE-1", "prepend": "background"})
    hook = _make_hook(
        tmp_path / "prep",
        f"#!/bin/sh\ncat >> '{log}'\nprintf '\\n' >> '{log}'\n"
        f"cat <<'EOF'\n{payload}\nEOF\n",
    )
    monkeypatch.setenv("ROUTE_DISPATCH_HOOKS", str(hook))
    _capture_dispatch(monkeypatch)
    cli.main_auto(["--pool", "local-agent", "write the docs"])
    assert _events(log)[1]["hook_context"] == "ISSUE-1"


def test_sensitive_work_takes_no_offered_context(tmp_path, monkeypatch):
    """route enforces this, not the hook.

    The hook is handed `data_class` and can be well-behaved about it, but a
    guarantee that depends on every hook being well-behaved is not a
    guarantee.
    """
    _prepending_hook(tmp_path, monkeypatch)
    seen = _capture_dispatch(monkeypatch)
    cli.main_auto(["--pool", "local-agent", "--sensitive", "write the docs"])
    assert seen == ["write the docs"]


def test_stay_path_ignores_an_offer(tmp_path, monkeypatch, capsys):
    """Nothing is dispatched, so there is nothing to prepend to."""
    _prepending_hook(tmp_path, monkeypatch)
    assert cli.main_auto(["--pool", "claude", "write the docs"]) == 0
    assert "prepended" not in capsys.readouterr().out


def test_offers_are_applied_in_hook_order(tmp_path, monkeypatch):
    hooks = []
    for name, body in (("10-first", "FIRST"), ("20-second", "SECOND")):
        payload = _json.dumps({"prepend": body})
        hooks.append(
            str(
                _make_hook(
                    tmp_path / name,
                    f"#!/bin/sh\ncat > /dev/null\ncat <<'EOF'\n{payload}\nEOF\n",
                )
            )
        )
    monkeypatch.setenv("ROUTE_DISPATCH_HOOKS", os.pathsep.join(hooks))
    seen = _capture_dispatch(monkeypatch)
    cli.main_auto(["--pool", "local-agent", "write the docs"])
    assert seen == ["FIRST\n\nSECOND\n\nwrite the docs"]
