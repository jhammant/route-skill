---
name: ultra
description: Run a goal as your own ultracode with `route ultra`. It plans the goal into independent units with checks, builds them in parallel on the best models in whichever pools have quota, has a different model family review each one, fixes confirmed bugs, merges onto an ultra/<run-id> branch and runs a check for the whole goal. It never merges into your branch and never pushes. Use when the user types /ultra, says "ultra this", "run this with ultra" or "my own ultracode", or wants a multi-model build of a goal without approving each step.
---

# /ultra — your own ultracode, on the best models with quota

`route ultra "<goal>"` does the whole job and asks nothing mid-run:

1. A top model plans the goal into independent units, each with a runnable check.
2. The units run as a route swarm on the best models in whichever pools have
   quota: the second Claude account (the `claude-work` arms), Codex, and this
   account (`claude`) when it has headroom. Only Fable, Opus and Codex-level
   models take part. Sonnet and Kimi join only with `--allow-lower-models`,
   and only when nothing better is left.
3. Each non-trivial unit that passes its check is reviewed by a model from a
   different family (Anthropic work goes to OpenAI, and the other way round).
   A finding counts only when running its repro command shows the bug.
4. Confirmed findings go back for at most two fix passes per unit.
5. Accepted branches are merged onto `ultra/<run-id>`. Build artefacts
   (`__pycache__`, `*.pyc`, caches, `node_modules`) are never staged, and
   checks run with `PYTHONDONTWRITEBYTECODE=1`, so they cannot cause add/add
   conflicts. A conflict on real files gets the integrator model one pass to
   resolve it; if that fails the unit is marked `conflict` and the merge is
   undone. Then the goal check runs there, outcomes are settled for the
   router, and `summary.md` is written.

It never merges into the base branch or main, and never pushes. Merging is the
user's call.

## Host and executable

As with /route, resolve the installed package executable
(`<checkout>/.venv/bin/route`, or the `ultra` console script, which equals
`route ultra`); macOS `/sbin/route` is the network utility. In Claude Code the
host is `claude`, the default; in Codex set `ROUTE_HOST=codex`. Pass the goal
as one literal argument.

## 1. Write the goal

Workers do not see this conversation unless you pass a digest, so the goal is
the brief. Make it precise:

- **Scope**: what to build or change, and which files or directories are in
  and out of bounds.
- **Constraints**: interfaces to keep, dependencies allowed, style to match,
  anything already decided here.
- **Acceptance check for the whole goal**: one command that passes only when
  the goal is done, such as `pytest -q tests/api`.

Checks run without a shell and must pass the safety lint: test runners only
(`pytest`, `python3 -m pytest`, `npm test`, `go test`, `cargo test`,
`make test` and the like), joined with `&&` and nothing else. Pipes,
redirects, `curl`, `rm`, `..` and paths outside the repo are refused, and so
is a check that only echoes or exits 0.

```text
Add rate limiting to the public API in src/api/. Keep the existing handler
signatures; stdlib only, no new dependencies. Per-key token bucket, limits
read from config/limits.toml, 429 with Retry-After when exceeded. Tests go
next to the existing ones in tests/api/. Goal check: pytest -q tests/api
```

## 2. Pick the context

- `--context brief` (default): the goal text only. Use it when the goal stands
  on its own.
- `--context digest`: each unit also gets this conversation minus tool output.
  Use it when the job depends on what was decided here. A digest that looks
  like it holds credentials or private data is refused; write those decisions
  into the goal instead.

`--session ID` takes the digest from another session (default: this one).

/ultra sends work to remote pools and has no sensitive mode. For private code
or data, use `/route --sensitive`.

## 3. Run it

Only when the user asked to see the plan first:

```bash
route ultra "<goal>" --plan-only --json
```

Summarise the units, their checks and their arms, and run it once they agree.
Otherwise go straight to:

```bash
route ultra "<goal>" --json
```

Run it with the Bash tool's `run_in_background` and the longest timeout
(7200000 ms): a run takes tens of minutes, often more. Don't poll; you are
told when it exits. Don't read worker logs or worktrees while it runs.

Other flags: `--repo PATH` and `--base REV` (default: this repo at `HEAD`),
`--max-units N` (default 10), `--max-parallel N` (default 6),
`--allow-lower-models`.

If the user asks how it is going, `route ultra status` shows the latest run
(`route ultra status <run-id>` for another one). `route ultra cancel <run-id>`
stops a run.

| Exit | Meaning |
|---|---|
| 0 | Every unit merged and the goal check passed on the integration branch. |
| 1 | Some units were not merged (failed, held or conflict), or the goal check failed. |
| 2 | It could not plan, or no usable model had quota. |

On exit 2, tell the user why and, when no top model had quota, offer
`--allow-lower-models`. Don't rerun on your own.

## 4. Report back

Each run lives in `~/.local/state/route/ultra/<run-id>/` (under
`ROUTE_STATE_DIR` when set) with `plan.json`, `manifest.json`,
`progress.json`, `summary.md` and `summary.json`. Read only `summary.md`. Open
a worker log only when a unit failed, and then only that unit's log.

Tell the user, in a few lines rather than pasted JSON:

- the integration branch, `ultra/<run-id>`, and what the goal check proved:
  `summary.md` says "passed on N of M units" and lists the units that were not
  merged. A pass on fewer than all units is NOT the goal passing; say so;
- which units passed, and which failed, conflicted or were held, with the
  reason;
- any paths a unit changed outside its planned `touches` (the "out of scope"
  column); they do not fail the unit, but the user should look at them;
- which reviews found real bugs (confirmed by their repro) and whether the
  fixes held; say so when a review was same-family rather than cross-family;
- quota spent per pool;
- the exact merge command from `summary.md`, such as
  `git merge --no-ff ultra/<run-id>` on the base branch. Don't run it.

Outcomes are already settled: don't run `route settle` on the units. Failed
units are not retried automatically; suggest a narrower goal or a rerun when
more quota is free.

## When not to use it

- Sequential work, or one small change: use /route or /auto.
- Private or sensitive data: use `/route --sensitive`.
- When the user wants to approve dispatch: use /route, or `route swarm`, which
  asks once for the whole swarm.
