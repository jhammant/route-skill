---
name: route
description: Decide where a task should run — Claude, Codex, Kimi, a local model, or the free-llm proxy — from task shape, complexity, and live quota. Explains the decision and waits for confirmation before dispatching. Use when the user types /route or asks where a task should run.
---

# /route — decide and explain, wait for confirmation

Run the routing engine on the user's task:

```sh
route <task text>
```

The command prints a JSON plan: `shape`, `tier`, `eligible` arms after veto and
quota gating, the `chosen` arm, and `why` (`veto:<name>`, `prior`, or
`posterior`).

Then:

1. **Explain the decision to the user in one short paragraph** — shape, tier,
   any veto that fired, which arms quota removed, and whether the choice came
   from the rule-table prior or from observed evidence.
2. If the plan says `dispatch: stay` (chosen arm is `claude`): do the task
   here. No confirmation needed — a veto means it cannot leave.
3. Otherwise the CLI asks for confirmation before dispatching. Surface its
   prompt to the user; dispatch only on an explicit yes. The CLI shells out to
   the existing `/codex`, `/kimi`, `/local-llm`, or `/free-llm` skills — never
   reimplement what they do.
4. **Record the outcome afterwards** so the router learns:

   ```sh
   route outcome --shape <shape> --tier <tier> --arm <arm> --outcome <accepted|verified|completed|failed>
   ```

   `accepted` (diff kept, no escalation) is the real signal; `verified` means
   tests passed; `completed` means it ran. On failed verification, retry once
   on the next-stronger eligible arm — one hop only.

Overrides: `--pool <name>` pins a pool (explicit beats inferred); `--private`,
`--no-acceptance`, `--needs-context`, `--cross-repo` force the matching veto.

The `free` arm: a fourth cost class (`free`, alongside `included`/`paid`/`local`)
that dispatches through the free-llm proxy on `127.0.0.1:8080` — hosted free
tiers at £0/month, rate-limited and REMOTE. It takes `batch:*` work plus
`coding:test`/`coding:review` only, and is eligible only while the proxy's
`/healthz` answers. Several free tiers train on submitted prompts, so the
private-data veto is absolute: a task carrying private data NEVER routes to
`free` — the veto short-circuits to `claude` before quota or the bandit are
consulted.

Privacy: routing and federation see shapes and counts only — never task text,
file paths, or repo names. `route federate export` shows exactly what would be
shared and shares nothing.
