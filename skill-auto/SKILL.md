---
name: auto
description: Decide where a task should run and dispatch it immediately — no confirmation prompt. Use when the user types /auto or asks to just route and run a task without being asked first.
---

# /auto — decide and dispatch immediately

Same engine as `/route`, minus the confirmation step:

```sh
auto <task text>
```

The command classifies the task (shape + complexity tier), applies vetoes and
quota gating, lets the bandit pick among the surviving arms, prints the JSON
plan, and dispatches straight away — to `codex`, `kimi`, `deepseek`, or
`local-llm` via their existing skills, or `stay` when the task belongs here.

`deepseek` is the paid backstop: it is only eligible when no included, free or
local arm survives (a hard rule), but when quota gating has removed `claude`/`codex`/`kimi` it is
chosen ahead of failing, so work always has somewhere to go. It is gated by
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
from pools on course to run out (`quota_adjustment` in the plan). A plan with
`shape_confidence: low` should be re-run with its `suggest`ed `--shape/--tier`.

Rules that still apply in auto mode:

- Vetoes are hard gates. If a veto fires (needs this conversation's context,
  cross-repo orchestration, private data, no clear acceptance check, user
  pinned a pool), the task stays with `claude` — auto mode never overrides a
  veto. `--sensitive` instead vetoes the remote third-party pools and prefers
  local arms; if no eligible arm survives, `/auto` fails loudly rather than
  falling back to a remote pool.
- Quota-critical arms are removed before the bandit chooses, so auto mode can
  never route into a pool with no headroom.
- Failed verification escalates at most one hop, to the next-stronger eligible
  arm, and records a negative outcome for the arm it leaves.
- `/auto` never triggers paid provisioning — no spinning up rented GPUs.

If you want to see the decision before it runs, use `/route` instead.
