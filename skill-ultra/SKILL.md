---
name: ultra
description: Run a goal as your own ultracode with `route ultra`. It plans the goal into independent units with checks, builds them in parallel on the best models in whichever pools have quota, has a different model family review each one, fixes confirmed bugs, merges onto an ultra/<run-id> branch and runs a check for the whole goal. It never merges into your branch and never pushes. Use when the user types /ultra, says "ultra this", "run this with ultra" or "my own ultracode", or wants a multi-model build of a goal without approving each step. Also use it for multi-agent research, with `route ultra --research`, when the user says "ultra research", "/ultra --research" or "research this with ultra", or asks for a broad investigation that would otherwise fan out into many agents; it answers read-only on the second account first and writes one cited report. Also use it for analysis teams, with `route ultra --team`, for app reviews, website audits, go-live checks, decisions, due diligence and code audits, when the user says "review my app", "audit this site", "is this ready to go live", "ultra team" or "get a team on this", or wants several expert views on one question; analysts on different models work independently, cross-examine each other's claims and write one report.
---

# /ultra — your own ultracode, on the best models with quota

`route ultra "<goal>"` does the whole job and asks nothing mid-run:

1. A top model plans the goal into independent units, each with a runnable check.
2. The units run as a route swarm on the best models in whichever pools have
   quota: the second Claude account (the `claude-work` arms), Codex, and this
   account (`claude`) when it has headroom. Only Fable, Opus and Codex-level
   models take part. Sonnet and Kimi join only with `--allow-lower-models`,
   and only when nothing better is left. Among equal models it spends the
   second account first: the one whose weekly quota would otherwise go unused.
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

## Research mode

`route ultra --research "<question>"` answers a question instead of building
a goal. A planner splits it into independent parts; background agents on the
best models with quota answer each one read-only, with web search and fetch,
a shell, the MCP servers in `research.toml` and read access to `--repo` when
given; a synthesizer merges the answers into one cited report. Nothing is
committed or merged.

Use it for non-code questions with several independent threads: comparisons,
surveys, due diligence, "find out everything about X". Prefer it to fanning
out your own subagents or the built-in ultracode, which bill this account.

### Write the question

The question is the brief, as in section 1:

- **Scope**: what to find out, and for which period, region, product or
  company.
- **A good answer**: what it must contain, such as a comparison table, numbers
  with dates, or a recommendation.
- **Sources**: which to prefer (primary docs, filings, standards, the repo
  itself) and which to distrust.
- **Exclusions**: what to leave out.

```text
Compare Supabase, Neon and PlanetScale for a multi-tenant B2B app hosted in
the EU, as of November 2026: price at 50 GB and 200 GB, point-in-time
recovery, row-level security, data residency, SOC 2. End with a one-paragraph
recommendation. Prefer vendor docs and pricing pages over blog posts; leave
out self-hosted options.
```

### Run it

Exactly like build mode: the Bash tool with `run_in_background` and the
longest timeout (7200000 ms). Don't poll.

```bash
route ultra --research "<question>" --json
```

- `--repo PATH` when the question is about a repo; the agents can read it.
- `--plan-only` only when the user wants to see the split first.
- `--max-parts N` (default 6), `--max-parallel N` (default 6),
  `--allow-lower-models` (lower Claude arms such as Sonnet; Kimi can't run
  research); `--context` and `--session` as in section 2.
  `--base` and `--max-units` are ignored.

`route ultra status` and `route ultra cancel <run-id>` work as in section 3.

Claude arms run in `--repo` without Edit, Write and NotebookEdit and with a
strict MCP config, read-only by instruction. Codex arms run
`codex exec --sandbox workspace-write -c sandbox_workspace_write.network_access=true`:
the network is on (curl, `npx -y lighthouse` and the PageSpeed API work), and
each one runs in its own empty directory under the run directory, never inside
`--repo`, with the absolute `--repo` path named in its prompt to read from, so
Codex can never write to `--repo`. A Claude agent still has a shell, though,
and a shell can write. So `--repo` is not write-protected: the run compares
`git status --porcelain` before and after and only warns about a change in
`summary.md`, afterwards; it neither prevents nor reverts it.

A part whose answer block is malformed goes back to the same arm once to be
repaired (`logs/part-<id>-repair.log`, marked repaired in the footer) before
the next arm redoes it. A source id reused for a different ref is renamed to
`S<max+1>` and listed under `## Run`. Claim text is not rewritten, so check a
renamed id's citations against that list.

`research.toml` (in `~/.config/route/`, `mcp_servers = ["name", ...]`) lists
the MCP servers from `~/.claude.json` background agents may use. It must only
contain read-only servers: the disallowed tools don't stop an MCP tool that
sends, posts or writes. If the user asks to add a server, check that first.

| Exit | Meaning |
|---|---|
| 0 | Every remote part was answered and the report written. |
| 1 | Some remote part failed or was held; the report is still written. |
| 2 | Empty question, it could not plan, or no usable model for any remote part. |

Session parts never affect the exit code. On exit 1, report anyway. On exit 2,
tell the user why and, when no top model had quota, offer
`--allow-lower-models`. Don't rerun on your own.

### Report back

Read only `summary.md` in the run directory (`plan.json`, `parts/<id>.json`
and `summary.json` sit next to it). Open a part's log,
`logs/part-<id>.log`, only when that part failed. Then:

1. Answer every item under `## For your session` here. Background agents run
   on this Mac with web search and fetch and a full shell, so they run
   headless browsers (`npx -y lighthouse`, `npx -y playwright` scripts,
   headless Chrome), read HTML, JavaScript bundles, robots.txt and sitemaps,
   query DNS and TLS, and read public documents and policies themselves.
   Session work is only what needs this session:
   claude.ai connectors (Gmail, Calendar, Drive), which the second account's
   inference-only token lacks;
   a signed-in browser or account (Claude in Chrome, logged-in dashboards);
   or the Mac's screen, simulator or physical devices. Session work costs the
   user's own account, so prefer remote: if an item needs none of those, it
   should have run in the background; answer it anyway and say so.
2. Give the user ONE merged answer: the report and your session answers
   together, not two reports. Keep the citations, resolving each
   `[part-id:S1]` from the `## Sources` list into a title and link.
3. Name the parts that failed or were held, with the reason from the footer.
4. If `summary.md` has a porcelain warning (`--repo` changed during the run),
   say so and list the paths. The run never reverts them; don't either.

## Teams

`route ultra --team [LAYOUT] "<question>"` puts a question to a team of
analysts, each with its own role and lens. They work independently and
read-only on the best arms with quota, every third one on a different model
family; their findings are merged into claims, verifiers from another family
try to refute each claim, critic rounds add the angles nobody covered, and a
synthesizer writes one report. Nothing is built unless you add `--fix`.

Use it when the user wants a review, an audit or a decision looked at from
several sides, not just answered: that is what the cross-examination is for.

### Choose a layout

| The user's ask | Layout |
|---|---|
| a website or landing page | `website-audit` |
| an iOS or Android app, or its store listing | `app-review` |
| launching or shipping: "is this ready to go live" | `go-live` |
| a codebase: bugs, security, quality | `code-audit` |
| a system design, a rewrite or a migration | `architecture` |
| a product launch, pricing or positioning | `launch` |
| "should we ...?" | `decision` |
| investing in or acquiring a company | `due-diligence` |
| a broad question that needs sources | `deep-research` |
| an outage or a production bug | `incident` |
| a post, article or announcement | `post-review` |
| none of these | `--team` with no layout: a planner designs the team |

`route ultra teams` (`--json` for the full definitions) lists every layout,
including the user's own; check it when the user names one you don't know.

### Write the question and the targets

The question is the brief, as in research mode: what to look at, what a good
answer contains, what to leave out. Then the targets:

- `--url URL` for each page, site or store listing, repeated for several.
  `website-audit` is about a URL, so always pass one.
- `--repo PATH` for the source. Analysts can read it, and `--fix` builds on it.

```bash
ultra --team website-audit --url https://example.com "Audit the marketing site before launch"
ultra --team go-live --repo . --fix "Is this ready to go live?"
```

With no layout, the question goes straight after `--team`. That works only
when the question is not itself a layout name.

### Run it

Exactly like build mode: the Bash tool with `run_in_background` and the
longest timeout (7200000 ms). Don't poll; you are told when it exits.

```bash
route ultra --team <layout> "<question>" --url <url> --json
```

- Intensity is automatic: full, standard or lean, from how much of the second
  account's week would otherwise go unused. Pass `--thorough` only when the
  user asks for the most thorough review and `--lean` when they want it cheap.
  `--team-size N` keeps the layout's first N analysts and `--rounds N` sets the
  critic rounds (0 for none).
- `--plan-only` only when the user wants to see the team first.
- `--context`, `--session`, `--max-parallel N` and `--allow-lower-models` as
  in research mode.

Analysts, verifiers, critics and the planner run under research mode's rules:
Claude with no Edit, Write or NotebookEdit; Codex with
`--sandbox workspace-write` and `network_access=true`, each in its own
empty directory under the run directory and never inside `--repo`; only the MCP
servers in `research.toml`; and a porcelain warning when `--repo` changed. A
Claude analyst still has a shell, so `--repo` is not write-protected even
without `--fix`. A malformed analyst answer is repaired by the same arm first
(`logs/analyst-<id>-repair.log`), and a source id reused for a different ref is
renamed and listed under `## Run`. `route ultra status` and
`route ultra cancel <run-id>` work as in section 3.

| Exit | Meaning |
|---|---|
| 0 | Every remote analyst answered and the report was written; with `--fix`, the build also exited 0. |
| 1 | An analyst failed or was held, or the `--fix` build failed; the report is still written. |
| 2 | Empty question, unknown layout, no team could be designed, or no usable arm. |

On exit 1, report anyway. On exit 2 for an unknown layout, stderr lists the
valid names and nothing ran: rerun with the right one. On any other exit 2,
tell the user why and, when no top model had quota, offer
`--allow-lower-models`.

### Report back

Read only `summary.md` (`plan.json`, `analysts/<id>.json`, `claims.json` and
`verdicts.json` sit next to it). Open an analyst's log only when it failed.
Then tell the user, in this order and in a few lines each:

1. The verdict: the scorecard, GO or NO-GO, release readiness or the
   recommendation, whatever shape the layout's report has.
2. The blockers, each with its fix.
3. The contested points from `## Contested`, with both sides.
4. The `## For your session` items, answered here. Session work is only
   claude.ai connectors (Gmail, Calendar, Drive),
   a signed-in browser or account (Claude in Chrome, logged-in dashboards),
   or the Mac's screen, simulator or physical devices, such as app-review's GUI
   walkthrough on the simulator. Lighthouse, crawls and other headless browsers
   run in the background. Do the items before you reply and say if they change
   the verdict.
5. Analysts that failed or were held, with the reason from the `## Run`
   footer, and the claims under `## Not cross-examined`, which nobody checked.

Keep the citations, resolving each `[analyst-id:S1]` from `## Sources`. A
"tracker missing" or "tag not firing" finding may only mean this Mac blocks
trackers at DNS; say so and suggest checking from another network.

### Offer --fix

When the layout supports it (`website-audit`, `app-review`, `go-live`,
`code-audit`, `incident`), the user has a repo and the report found code
blockers, offer a rerun with `--repo PATH --fix`. The synthesizer turns the
code blockers and high-severity findings into a build goal and a goal check
(which must pass the same lint as build checks), and `route ultra` builds it
on an `ultra/<run-id>` branch. Report the branch and the merge command from
`summary.md`, as in section 4. It never merges, never pushes and never
deploys, and you don't either.

### Custom layouts

Users add their own layouts to `~/.config/route/teams.toml`, in the same shape
as the built-ins (title, description, targets, fix, report and 3 to 8 analysts
with id, role, lens, brief, tier and where); the README has an example. A user
layout with a built-in's name replaces it. If the user keeps asking for the
same kind of team, offer to write one.

## When not to use it

- Sequential work, or one small change: use /route or /auto.
- A question one search answers: answer it here, without `--research`.
- A question with one right answer and no sides to weigh: `--research`, not
  `--team`.
- Private or sensitive data: use `/route --sensitive`.
- When the user wants to approve dispatch: use /route, or `route swarm`, which
  asks once for the whole swarm.
