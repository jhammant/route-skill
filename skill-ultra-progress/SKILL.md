---
name: ultra-progress
description: Show how an /ultra run is going as a live dashboard laid out like an ultracode workflow — a one-line header (state, agents running/queued, total tokens, elapsed), one group per phase (Plan, Build, Review, Fix, Integrate) with a row per agent (planner, workers, reviewers, fixers, resolvers, integrator, and the sub-agents they spawned) showing model, time, tokens, tool calls and what each is doing now, plus queued agents still to come, a stage strip per unit, a narrator log, recent tool calls and the agents' messages. Use for "/ultra-progress", "how's ultra going", "show the ultra run", "what are the ultra agents doing", or whenever the user asks about a running or finished /ultra run.
---

# /ultra-progress — an /ultra run and its agents, live

Read-only. It reads the run's `progress.json` and logs, its swarm's `status.json` and
`channel.jsonl` (under `ROUTE_STATE_DIR`, default `~/.local/state/route`), and the agents'
own transcripts (Claude Code in `~/.claude/projects`, Codex in `~/.codex/sessions`). It
changes nothing. The script is a uv script: `uv` installs its one dependency (rich) on
first run.

## In this session (Claude Code or Codex)

Works the same from either host: invoke as `/ultra-progress` in Claude Code or
`$ultra-progress` in Codex. Run a snapshot and show it to the user inside a ```text
block, exactly as printed:

```bash
UV_OFFLINE=1 ultra-progress --once --width 100            # latest run
UV_OFFLINE=1 ultra-progress --once --width 100 <run-id>   # a given run (or unique prefix)
```

`UV_OFFLINE=1` uses the cached `rich`, so it also runs inside Codex's sandbox, which has
no network. If uv reports that `rich` isn't cached yet (a fresh machine), run the same
command once without `UV_OFFLINE` outside the sandbox, or ask the user to run
`ultra-progress --once` in a terminal; after that the offline form works.

Then add one or two plain sentences on what it means: which units are building, under
review or being fixed, which agents (and sub-agents) are working on what, anything failed
or held and why, and what happens next. Don't read worker logs or worktrees yourself while
the run is going; the dashboard already summarises them.

## Live view for the user

The live view redraws every second with spinners for working agents. Tell the user to run
it in a spare terminal tab (not with `!` here, which can't show a live screen):

```bash
ultra-progress            # latest run; Ctrl-C to close (prints a final snapshot)
ultra-progress <run-id>
```

`ultra-progress` is a symlink in `~/.local/bin` to `skill-ultra-progress/ultra_progress.py`
in the route-skill repo. The skill itself is linked into `~/.claude/skills/ultra-progress`
(Claude Code) and `~/.agents/skills/ultra-progress` (Codex).

## Reading it

It reads like an ultracode workflow view, top to bottom:

```text
ULTRA finish-the-data-layer-an-20… ● fixing · 10 agents (1 running, 2 queued) · 164k tokens · 41m 10s
Finish the data layer and harden the new "Money" section of Schools in Reach (repo root = this repo…

    agent                       model             time  tokens  tools  note
✓ Plan       1/1 · 3 units · 11k tokens · 2m 15s
  ✓ planner                     fable · work    2m 15s     11k      3  3 units
✓ Build      3/3 · 151k tokens · 21m 43s
  ✓ finance-history · worker    fable · work   15m 32s     61k     32  24 passed in 54.31s
  …
● Fix        0/3 · 1 running · 2 queued · 13k tokens · 6m 28s
  ● finance-history · fixer     fable · work    6m 28s     13k     13  pass 1: 3 confirmed bugs
      now Bash Run the unit acceptance check including real-data tests
  ○ pta-matching · fixer        —               queued                 3 confirmed bugs to fix
○ Integrate  not started
```

- Header (one line): run id, state, how many agents the run has started or will start
  (and how many are running, queued or failed), sub-agents, total output tokens, elapsed,
  and the exit code once finished. The goal's first sentence is under it. Tokens and
  elapsed always show: on a narrow screen the sub-agent count goes first, then the queued,
  failed and running counts, then the exit code, and the run id is shortened.
- One group per phase, always in this order: Plan, Build (workers), Review (reviewers),
  Fix (fixers), Integrate (merge-conflict resolvers and the goal-check integrator). The
  group line has the phase's state, done/total agents, what is running, queued or failed,
  phase notes (confirmed bugs, units merged, goal check), tokens and time.
- One row per agent: label (`unit · role`), model, time, output tokens and tool calls
  (both include its sub-agents; blank when no transcript was found), and a note (check
  result, confirmed bugs, fix pass). A working agent has a `now` line with its latest tool
  call. Sub-agents hang under their agent; more than two finished ones fold into one line.
  A phase with more than 16 agents shows the working and failed ones and folds the rest
  into a line per role ("84 verifiers done"). When a reviewer gives no verdict and
  another takes over, each attempt has its own row (`reviewer #1`, `reviewer #2`) with
  its own model, tokens and the reason it failed.
- Queued rows are agents the run will start but hasn't yet: a worker waiting for a slot
  in its model's pool, a reviewer for every unit whose review hasn't started (unless it was
  skipped), and a fixer for every unit with confirmed bugs or a failed check. Their model
  shows `—` until one is picked.
- `✓` done, `●`/spinner working now, `○` queued or not started, `✗` failed, `·` skipped,
  `⏸` held (no model had quota). An agent still running when the run is cancelled shows
  `✗` "stopped when the run ended".
- `units`: one line per unit with its stage strip (build check review fix merge) and
  where it stands. `log`: the last narrator lines ("codex confirmed 3 bugs in …",
  "pta-matching merged"), newest last. `activity`: the latest tool calls across all
  agents. `messages`: what units posted on the swarm channel. Then quota headroom, the
  integration branch and, once finished, the path to `summary.md`; report from that as
  the /ultra skill says.
- Team and research runs (`route ultra --team`, `--research`; the header says `(team)` or
  `(research)`) build nothing, so the phases hold other agents. Plan: the planner (a
  team picked from a layout has none; the line names the layout). Build: one analyst, or
  one researcher per part, with its findings or confidence. Review: a team's verifiers,
  which cross-examine its claims (the line counts upheld, contested and refuted), and its
  critic rounds, which add analysts. Fix: a team's `--fix` build run. Integrate: the
  synthesizer that writes the report. A research run has no Review or Fix. The log ends
  with how many analysts delivered or parts were answered, not merges.
- `--json` prints the same data for the ultra-watch mod: everything above as `totals`,
  `phases` (each with its agent rows) and `log` (up to 12 lines), next to the older
  per-unit keys.
