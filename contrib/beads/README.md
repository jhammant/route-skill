# beads integration — closing the learning loop

route records an *impression* every time it picks an arm. `auto outcome` records
a *resolved* trial and, when accepted, its reward. Out of the box nothing calls
it. Impressions accumulate while every arm's Beta posterior stays at its prior —
the router keeps deciding, but it never learns. An unreconciled dispatch is
unknown, not a failed trial.

This is the missing caller, built on [beads](https://github.com/gastownhall/beads)
(`bd`), a Dolt-backed issue tracker. The loop is:

```
auto "<task>"  ──dispatch hook──▶  bead gains routing metadata + route:pending
     │                                        │
     │                                   a human closes,
     │                                   reopens, or abandons
     │                                        │
     ▼                                        ▼
route's bandit  ◀──auto outcome──  route-reconcile reads the lifecycle
```

The signal that makes this worth doing is the *human judgement* in the middle.
A dispatch exiting 0 only means the arm did not crash. A person closing the
work item means the output held up.

## Why the hook never creates a bead

`ROUTE_BEAD=<id>` is the only path: with it unset the hook records nothing.
Auto-creating a bead per dispatch was considered and rejected twice over.

1. **It would be a circular reward.** A bead the integration created is closed
   by nobody but the reconciler reading it back, so "closed → accepted" would
   encode no human judgement at all.
2. **It would leak the task.** Creating a bead means writing the task text as
   title and description to a shared tracker on every dispatch, ignoring
   route's own `data_class` classification. On the `ROUTE_BEAD` path only
   `--metadata` is written — enum values, counts and a timestamp — so no task
   text ever leaves the machine.

## One writer for the reward

The hook records *evidence* (exit code, wall clock, chosen arm). It never
closes or relabels a bead. `route_reconcile.py` is the only thing that decides
an outcome and the only thing that calls `auto outcome`. Two writers would
double-count, and double-counting a bandit reward is the failure mode nobody
can see from the outside.

## Install

```sh
contrib/beads/install.sh            # gated on `bd`; installs nothing without it
contrib/beads/install.sh --uninstall
```

This symlinks `route-bead-hook.sh` into `<config dir>/dispatch-hooks.d/50-beads`
and `route-reconcile` into `~/.local/bin` (override with `ROUTE_BEADS_BIN_DIR`).
Requires `jq`. With `bd` absent the installer exits 0 having done nothing, and
route behaves exactly as it does without this directory.

## Use

Attach a dispatch to a work item:

```sh
ROUTE_BEAD=proj-42 auto "refactor the storage layer"
```

The `decision` event writes `cell`, `shape`, `tier`, `arm`, `why`, `eligible`
and `decision_ts` onto the bead's metadata and adds the `route:pending` label.
The `complete` event appends `exit_code`, `wall_clock_s` and any `exception`.

### Setting `ROUTE_BEAD` without remembering to

Typing `ROUTE_BEAD=` by hand means most dispatches never carry one, and a
dispatch without one contributes no reward — it exists only in
`decisions.jsonl`. Correctness is unaffected either way; this is purely about
how much signal the bandit sees.

Nothing is shipped here to solve that, because the right answer depends on
what "the current work item" means in your setup — the bead you have claimed,
a key parsed out of the branch name, a CI variable, a ticket passed by the
tool that invoked route. Pick the one that matches your workflow and wrap
`auto` in a script that resolves it, exports `ROUTE_BEAD`, and `exec`s the
real binary.

Two things such a wrapper must do:

- **Fail open.** No tracker, no current item, an ambiguous answer, or a
  lookup that errors — all of these mean exec `auto` with `ROUTE_BEAD` unset,
  never refuse to dispatch. Losing a sample is cheap; a wrapper that can
  block routing is not.
- **Never guess.** If two items are plausibly "current", export nothing. A
  reward attributed to the wrong work item is worse than no reward, and
  nothing downstream can detect it.

Note that a wrapper like this necessarily shadows `auto` on `PATH`, which is
why it is left to you rather than installed here.

Then reconcile, from anywhere inside a beads workspace:

```sh
route-reconcile --dry-run    # print the verdicts, change nothing
route-reconcile              # record them
route-reconcile --json       # machine-readable verdicts
route-reconcile --quiet      # silent when there was nothing to do
```

`--quiet` exists for session-start hooks, whose stdout often becomes an agent's
context: a "reconciled 0 of 0" line on every session in every repo is noise.

## The decision table

`route_reconcile.py` fetches every bead labelled `route:pending` and not yet
`route:recorded`, then applies these rules in order. First match wins; the
order is load-bearing.

| # | Condition | Verdict |
|---|-----------|---------|
| 1 | no `exit_code` (dispatch never finished) | leave — or `skip` past `--stale-days` |
| 2 | `exit_code == 130` or `KeyboardInterrupt` | skip: a Ctrl-C is not a verdict on the arm |
| 3 | bead is `deferred` or `blocked` | leave: a scheduling fact, not a verdict |
| 4 | closed, `exit_code == 0` | **`accepted`** — the real signal |
| 5 | closed, non-zero exit | `failed` |
| 6 | reopened after closing | `failed`: the output did not hold |
| 7 | still open, non-zero exit | `failed` |
| 8 | still open, clean exit, older than `--stale-days` (14) | `failed` |
| 9 | otherwise | leave: still in flight |

Rules 1 and 2 sit above every status rule deliberately: neither an in-flight
dispatch nor an aborted one says anything about the arm, whatever the bead's
status happens to be.

A bead is claimed (`route:recorded` / `route:skipped`) *before* the outcome is
recorded. A crash in between loses one sample; the other order double-counts.
Losing a sample is recoverable and invisible in aggregate — double-counting is
neither.

### Attributing the reward to the right decision

`decision_ts` is written by the hook and passed back to `auto outcome` as
`--decision-ts`, so a reward lands on the decision that earned it rather than
on the most recent decision for that cell. Without it a slow dispatch closed a
week later would credit whatever arm ran most recently in the same cell.

Beads annotated by an older route that did not carry `decision_ts` still
reconcile: ageing falls back to the bead's `updated_at`, which moves whenever
anything touches the bead and so can only ever delay ageing, never hasten it.

## Project memory as offered context

An arm dispatched to another machine, another model and another process starts
cold. It gets the task and nothing else — not the convention someone landed on
last week, not the thing that was already tried and did not work. beads has
that: `bd remember` notes are exactly the durable facts a project accumulates
and a fresh context does not have.

So on `decision` the hook searches those memories and offers the hits to route
as `prepend`, and route puts them ahead of the task for that dispatch. Both
the project database and the shared `--global` one are searched; the global
one holds the cross-repo infrastructure facts an arm is likeliest to be
missing.

Search terms are the distinctive words of the task itself (≥5 characters,
stopwords dropped, first 5). Crude on purpose: `bd memories` is a substring
search, and precision buys little that the caps below do not already bound.

What holds it in check:

- **`sensitive` work is never even read for.** The hook checks `data_class`
  *before* touching `bd memories`. route drops the offer for non-`open` work
  regardless, but not assembling one means there is no moment where project
  memory sits next to a task the classifier called sensitive.
- **The budget is divided, not spent front-to-back.** A single memory
  routinely runs past a thousand characters; filling greedily would seat the
  first and truncate the rest away. Each hit is cut to its share of
  `ROUTE_BEAD_CONTEXT_MAX_CHARS` and marked `[…]`, so the arm sees every match
  and can tell which it is seeing only the head of.
- **No memories means the old behaviour, byte for byte** — the bare bead id on
  stdout, no JSON envelope.
- **No `ROUTE_BEAD` is not a blocker.** That variable governs the reward loop;
  an arm dispatched outside it is just as cold. The offer is made with an
  empty correlation token.

Task text still never leaves the machine through this path: it is read to
derive search terms and is otherwise untouched, and nothing is written to
beads that was not written before.

## Fail open, everywhere

A missing `bd`, a missing `jq`, a cwd that is not a beads workspace, an
unreachable beads server, a malformed payload — every one of these exits 0
with a line on stderr. The hook can never stop a dispatch, and the reconciler
can never stop a session from starting. One bad bead does not blank the run:
the per-bead loop catches, reports and moves on.

## Beads workspace requirements

The hook only acts inside a *valid* beads workspace: a directory whose
`.beads/metadata.json` parses as JSON and carries a non-empty `dolt_database`.
A placeholder `{}` is rejected on purpose — real `bd` would silently auto-init
a throwaway database rather than error, polluting shared infrastructure with
writes nobody will ever read.

Both a local Dolt database and a shared Dolt server work; the integration only
ever shells out to `bd`, so whatever `bd` is configured to talk to is what it
uses. See beads' own documentation for server setup.

## Environment

| Variable | Default | Effect |
|----------|---------|--------|
| `ROUTE_BEAD` | unset | bead id to annotate; unset means record nothing |
| `ROUTE_BEAD_BD_TIMEOUT` | `10` | seconds before a `bd` call is abandoned |
| `ROUTE_BEAD_CONTEXT_MAX_CHARS` | `3000` | character budget for offered memory; `0` disables the offer |
| `ROUTE_BEAD_CONTEXT_MAX_TERMS` | `5` | task words searched for |
| `ROUTE_BEAD_CONTEXT_MAX_MEMORIES` | `4` | memories offered at most |
| `ROUTE_AUTO` | unset | path to route's `auto` CLI, if not on PATH |
| `ROUTE_BEADS_BIN_DIR` | `~/.local/bin` | where `install.sh` puts `route-reconcile` |
