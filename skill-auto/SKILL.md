---
name: auto
description: Decide where a task should run and dispatch it immediately — no confirmation prompt. Use when the user types /auto or asks to just route and run a task without being asked first.
---

# /auto — decide and dispatch immediately

## Host and executable

In Codex, pass `--host codex` on every task invocation; in Claude Code use
`--host claude`. `ROUTE_HOST` is an alternative when explicitly set. The
current host means "stay"; the other provider can receive a delegated task.
Resolve the installed package executable (for a virtualenv installation,
`<checkout>/.venv/bin/route` or `auto`). Do not use macOS `/sbin/route`.
Use the resolved path wherever examples below say `route` or `auto`.
Pass task text as a literal argument. Delegates do not inherit the parent
conversation; supply scope and acceptance checks, then review their results.

Same engine as `/route`, minus the confirmation step:

```sh
auto <task text>
```

The command classifies the task (shape + complexity tier), applies vetoes and
quota gating, lets the bandit pick among the surviving arms, prints the JSON
plan, and dispatches straight away — to `claude`, `codex`, `kimi`, `deepseek`,
`local-llm`, or `free-llm`, or `stay` when the task belongs here. Codex-to-Claude
delegation uses `claude --print --permission-mode auto`; other arms retain
their existing dispatch commands.

`deepseek` is the paid backstop: it is only eligible when no included, free or
local arm survives (a hard rule). When those alternatives are unavailable,
DeepSeek can take the task if it survives the remaining gates. It is gated by
its credit balance (quotamax's `backstop` block, or the skill's
`balance --json` as fallback) and vetoed by `--sensitive`/`--private` like
any remote pool.

Long-horizon tasks (`until all tests pass`, `every call site`, `whole
codebase`, `overnight`) floor the tier at `hard` and favour `codex`, which
runs GPT-6 Astra. Pass `--tier hard` when a task is plainly hours-long but
the cue did not fire.

The exit code records `completed` or `failed` on its own. Afterwards, **record
how much fixing it needed**, using the `decision_id` from the plan, so the router
learns the signal it cannot see:

```sh
route settle --decision <id> --fix-passes <N> --merged    # or --abandoned
```

The router also nudges toward pools with quota about to expire unused and away
from pools on course to run out (`quota_adjustment` in the plan). The quota
probe has a 15 s timeout and a 5-minute cache. A plan with
`shape_confidence: low` should be re-run with its `suggest`ed `--shape/--tier`
or `--llm-shape`. Feature builds floor at `moderate`, or `hard` when they name
three distinct component kinds (route, endpoint, migration, webhook, worker,
test, flag). Similar settled tasks appear in `neighbours`; their evidence is
temporary, and the local hashed-token memory never federates.

Codex runs in a `workspace-write` sandbox with the network off, local sockets
included, so a job whose tests open a port fails there. Pass `--allow-network` to
turn the network on for that one Codex run (`-c sandbox_workspace_write.network_access=true`;
`~/.codex/config.toml` is never changed). Use it only when the brief needs sockets
and touches nothing sensitive. It is refused with `--sensitive`, other pools ignore
it, and the plan records `allow_network: true`. Run the final test pass yourself either way.

Rules that still apply in auto mode:

- Vetoes are hard gates. If a veto fires (needs this conversation's context,
  cross-repo orchestration, private data, no clear acceptance check, user
  pinned a pool), the task stays with the current host unless the user explicitly
  pinned another pool — auto mode never overrides a veto. `--sensitive` keeps
  the current host and local arms, excludes other remote providers, and prefers
  local arms; if no eligible arm survives, `/auto` fails loudly rather than
  falling back to a remote pool.
- Quota-critical arms are removed before the bandit chooses. If no arm survives,
  open tasks stay with the current host; sensitive tasks fail as described above.
- Failed verification escalates at most one hop, to the next-stronger eligible
  arm, and records a negative outcome for the arm it leaves.
- `/auto` never triggers paid provisioning — no spinning up rented GPUs.

If you want to see the decision before it runs, use `/route` instead.
