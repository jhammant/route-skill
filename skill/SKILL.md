---
name: route
description: Decide where a task should run — Claude, Codex, Kimi, DeepSeek, a local model, or the free-llm proxy — from task shape, complexity, and live quota. Explains the decision and waits for confirmation before dispatching. Use when the user types /route or asks where a task should run.
---

# /route — decide and explain, wait for confirmation

## Host and executable

In Codex, pass `--host codex` on every task invocation; in Claude Code use
`--host claude`. `ROUTE_HOST` is an alternative when explicitly set. The
current host means "stay"; the other provider can receive a delegated task.
Resolve the installed package executable (for a virtualenv installation,
`<checkout>/.venv/bin/route` or `auto`). Do not use macOS `/sbin/route`.
Use the resolved path wherever examples below say `route` or `auto`.
Pass task text as a literal argument. Delegates do not inherit the parent
conversation; supply scope and acceptance checks, then review their results.

Run the routing engine on the user's task:

```sh
route <task text>
```

The command prints a JSON plan: `shape`, `tier`, `horizon` (`long` when the
task reads as hours of many-step work), `eligible` arms after veto and quota
gating, the `chosen` arm, and `why` (`veto:<name>`, `prior`, or `posterior`,
with `+long-horizon` when the cue floored the tier at `hard`).

Then:

1. **Explain the decision to the user in one short paragraph** — shape, tier,
   any veto that fired, which arms quota removed, and whether the choice came
   from the rule-table prior or from observed evidence.
2. If the plan says `dispatch: stay` (chosen arm matches the current host), do
   the task here. No dispatch confirmation is needed.
3. Otherwise the CLI asks for confirmation before dispatching. Surface its
   prompt to the user; dispatch only on an explicit yes. The CLI shells out to
   `claude --print --permission-mode auto` when delegating from Codex to Claude,
   or the existing `/codex`, `/kimi`, `/deepseek`, `/local-llm`, or `/free-llm`
   skills. Keep their existing dispatch commands.
4. **Record a graded outcome; the router only learns what you tell it.** The
   exit code records `completed`/`failed` on its own, but the real signal is how
   much fixing the result needed. The plan prints a `decision_id`; after
   verifying, run one of:

   ```sh
   route settle --decision <id> --fix-passes <N> --merged   # kept after N fix passes
   route settle --decision <id> --abandoned                 # supervising agent rewrote it
   route outcome --shape <s> --tier <t> --arm <a> --score 0.85   # any 0..1 verified score
   ```

   Fix passes map to a score: 0 → 1.0, 1 → 0.7, 2 → 0.4, 3 → 0.1, more → 0. The
   score replaces that decision's earlier `completed` rather than adding a
   second trial, so nothing double-counts. `--outcome accepted|verified|failed`
   still works. On failed verification, retry once on the next-stronger eligible
   arm (one hop only). `route stats --quota` shows quota spent per dispatch and
   per accepted result for each arm.

**What the plan JSON tells you** (mention anything unusual when you explain the decision):

- `quota_adjustment`: a signed nudge per eligible arm. `+surplus:<arm>` means
  its pool is on course to leave quota unused at reset; `-pressure:<arm>` means
  it's on course to run out, or quotamax says `constrained`. Bounded at ±0.15;
  it never overrides a veto or the quota gate. Quota is probed with a 15 s
  timeout and cached for 5 minutes.
- `neighbours`: similar past tasks (local memory, never federated; all task
  tokens stored only as salted hashes) and how each arm scored on them. They
  count for this decision only.
- `shape_confidence: low` with a `suggest`: the classifier wasn't sure. Re-run
  with the suggested `--shape/--tier`, or `--llm-shape`, before dispatching.
  Feature builds floor at `moderate`, or `hard` with three distinct component
  kinds (route, endpoint, migration, webhook, worker, test, flag).
- `removed_by_cost`: paid arms (deepseek) are only eligible when no included,
  free or local arm survives. That's a hard rule, not a sampling preference.

Overrides: `--pool <name>` pins a pool (explicit beats inferred); `--private`,
`--no-acceptance`, `--needs-context`, `--cross-repo` force the matching veto.
`--sensitive` sets the dataClass to `sensitive`: remote providers other than the
current host are vetoed at the eligibility stage, including `deepseek` and
`free`. The host and local arms survive, and local is PREFERRED — sensitive
work belongs on your own metal. A path allowlist in `~/.config/route/sensitive.toml`
(`paths = ["~/dev/clients/*"]`) marks tasks naming those files sensitive
without the flag. If nothing eligible survives, routing fails loudly — it
never falls back to a remote arm.

The `deepseek` arm is the **backstop**: pay-as-you-go real money via the
`/deepseek` skill (`code --dir . --model deepseek-v4-pro` for coding shapes,
`ask - --model deepseek-flash` with the task on stdin for batch). Its coding
prior remains Beta(4, 6); the `removed_by_cost` rule above enforces its backstop
role before sampling. Quota gating is the credit balance: quotamax's `backstop` block
(`available`/`low`), falling back to the skill's `balance --json` (exit 0 =
eligible); `--no-quota` skips the balance check but retains the cost rule.
Because it is remote, the sensitivity and privacy gates apply to it too.

The `free` arm: a fourth cost class (`free`, alongside `included`/`paid`/`local`)
that dispatches through the free-llm proxy on `127.0.0.1:8080` — hosted free
tiers at £0/month, rate-limited and REMOTE. It takes `batch:*` work plus
`coding:test`/`coding:review` only, and is eligible only while the proxy's
`/healthz` answers. Several free tiers train on submitted prompts, so
sensitive data is excluded from `free` before quota or the bandit are
consulted, subject to the explicit pin policy above. Content matching also
escalates potentially private tasks to sensitive, preferring local arms.
Explicit `--private` pins to the current host. Neither replaces the caller's
explicit sensitivity signal: when in doubt, mark `--sensitive`.

Privacy: classification reads the task locally; delegation sends it to the
selected arm. Raw task text never persists in task memory. Federation exports
shapes, model identities and counts, never task text, hashes, file paths or
repo names. `route federate export` previews that payload and shares nothing.

## Swarms

If the plan's `swarm.suggested` is true, or you can see the task splits into
independent units that each have a check, write the manifest (one unit per
independent piece, each with its own `check`, plus shared interface notes in
`shared`) and run `route swarm` / `auto swarm`. Otherwise dispatch normally.
Don't swarm sequential work.

When the user says to swarm (or not to), pass `--swarm`/`--no-swarm` on the task
command; when they name a model for the swarm, pass `--pool`. An explicit
override beats detection, including quota caps on the hint. The manifest has
`name`, `repo` (default `.`), `base` (default `HEAD`), `shared`, and `units`.
Each unit needs a unique lowercase alphanumeric/hyphen `id` (1–32 characters,
not `hub`), `task`, and `check`; `shape`, `tier`, `pool` and `sensitive` are
optional. A unit's pool/sensitivity overrides the swarm flags.

```bash
route swarm manifest.json --host codex --plan-only
route swarm manifest.json --host codex
route swarm-log <swarm-id>
```

Use `--host claude` in Claude Code. `route swarm` asks once for the whole swarm;
`auto swarm` dispatches immediately. Same-cell units reuse a model until its
capacity is full, then spill to another eligible arm or queue. Non-pin vetoes
are held for you to handle. Each member runs in its own worktree and branch,
gets checked and commits its result. Review the summary and branches afterward.

a-mail addresses are printed so the orchestrating session can message members
with its own amail tools. Members get a peer roster and hub, with group
broadcast when supported; `route swarm-log` reads the hub transcript. Use
`--no-amail` to disable it. Batch/stdin units have no channel. `--max-parallel`
(default 4), `--ttl` (120 minutes), `--unit-timeout` (3600 seconds), and
`--check-timeout` (600 seconds) bound the run. `--allow-network` is Codex-only
and refused for sensitive units. Swarm outcomes are already settled as one
trial per `(shape, tier, arm)` group: do not run `settle` on individual members.
Failed units get a stronger-arm suggestion only; do not retry automatically.
