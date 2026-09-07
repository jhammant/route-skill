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
plan, and dispatches straight away — to `claude`, `codex`, `kimi`, or `local-llm` via
their existing skills, or `stay` when the task belongs here.

Afterwards, **record the outcome** so the router learns:

```sh
route outcome --shape <shape> --tier <tier> --arm <arm> --outcome <accepted|verified|completed|failed>
```

Rules that still apply in auto mode:

- Vetoes are hard gates. If a veto fires (needs this conversation's context,
  cross-repo orchestration, private data, no clear acceptance check, user
  pinned a pool), the task stays with the current host unless the user explicitly
  pinned another pool — auto mode never overrides a veto. `--sensitive` instead vetoes the remote third-party pools and prefers
  local arms; if no eligible arm survives, `/auto` fails loudly rather than
  falling back to a remote pool.
- Quota-critical arms are removed before the bandit chooses, so auto mode can
  never route into a pool with no headroom.
- Failed verification escalates at most one hop, to the next-stronger eligible
  arm, and records a negative outcome for the arm it leaves.
- `/auto` never triggers paid provisioning — no spinning up rented GPUs.

If you want to see the decision before it runs, use `/route` instead.
