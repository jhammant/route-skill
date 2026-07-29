# route: sensitive-data routing + ship (PR workflow) — spec

Two additions. The first corrects a design mistake in the private-data veto; the
second closes the loop from "route decided" to "reviewable PR".

---

## 1. Sensitive data should PREFER local, not avoid everything

### The mistake being corrected

The current veto treats "this task carries private data" as "keep it on Claude".
That conflates two different things:

- **remote** pools — `codex`, `kimi`, `free` — send your data to a third party.
  Several free tiers (Gemini, Mistral) explicitly train on submitted prompts.
- **local** pools — `local-batch`, `local-agent` — run on hardware you own. The
  data never leaves the machine.

Local is the *only* arm where sensitive data is safe **by construction** rather
than by policy. Excluding it was backwards.

### The rule

Introduce a `dataClass` on the routing decision: `open` (default) or `sensitive`.

| arm | `open` | `sensitive` |
|---|---|---|
| `claude` | eligible | eligible |
| `local-batch` / `local-agent` | eligible | **eligible, and PREFERRED** |
| `codex`, `kimi` | eligible | **vetoed** |
| `free` | eligible | **vetoed — hardest of all** |

- When `dataClass == 'sensitive'`, remote arms are removed at the **eligibility**
  stage, not down-weighted. A veto, not a score.
- Among the survivors, apply a **preference bonus to local arms** so sensitive
  work lands locally rather than defaulting to Claude. Sensitive work is exactly
  what you want on your own metal — it is free, private, and the machine is idle.
- If `sensitive` leaves no eligible arm (e.g. no local backend reachable and the
  shape can't stay on Claude), **fail loudly** with the reason. Never silently
  fall back to a remote arm. That failure mode is the whole point of the flag.

### Detecting sensitive data

Do NOT try to be clever. Three explicit signals only:

1. `--sensitive` on the command line.
2. A `sensitive: true` field on a task passed programmatically.
3. A configurable path allowlist in `~/.config/route/sensitive.toml` — e.g.
   `paths = ["~/dev/clients/*", "~/Documents/finance/*"]`. A task naming a file
   under one of those paths is sensitive.

Never infer sensitivity from content heuristics. A false negative silently sends
private data to a third party, and no regex is worth that risk. When unsure, the
user marks it.

### Tests

- a `sensitive` task never selects `codex`, `kimi`, or `free`, even when every
  other arm is exhausted or rate-limited
- among eligible arms, a `sensitive` task prefers `local-*` over `claude`
- `sensitive` with no eligible arm raises with a clear reason rather than
  falling back
- a path matching `sensitive.toml` marks the task sensitive without the flag

---

## 2. `route ship` — from routed task to reviewable PR

### Why

A routed coding task ends as edits on a branch, and then stops. The user still
has to review, push, write a PR body, and remember which arm did the work and
why. `ship` closes that gap and carries the routing provenance with it.

### Flow

```
route ship [--pr] [--repo <owner/name>] [--base <branch>] [--draft]
```

1. **Show what changed first.** `git status` + `git diff --stat`, then the full
   diff on request. Nothing is pushed before the user has seen it.
2. **Refuse to ship a dirty unrelated tree** — if changes exist outside the
   task's branch, say so and stop rather than sweeping them in.
3. **Push the branch** (never `--force`, never to `main`/`master`).
4. **Open a PR** via `gh`, with a body that includes the routing provenance:

```markdown
Routed by /route.

| | |
|---|---|
| shape | coding:implement |
| complexity | moderate |
| arm | kimi |
| why | claude weekly at 88%; task self-contained with a clear acceptance check |
| verification | 62 tests pass |

<task description>
```

That table is the interesting part — it records *why this model wrote this code*,
which is otherwise lost the moment the session ends.

### Hard safety rules

- **Never push or open a PR without explicit confirmation** on that invocation.
  Pushing is outward-facing and effectively irreversible once seen.
- **Never merge.** `ship` opens PRs; a human merges them.
- **Never force-push**, and never push directly to the default branch.
- If `gh` is absent, push the branch and print the compare URL instead of failing.
- Redact nothing from the diff — the user must see exactly what they are shipping.

### Upstream / listing submissions

The same command handles contributing to someone else's repo, which is just a PR
to an upstream:

```
route ship --pr --repo owner/awesome-list --base main
```

- if the user lacks push access, fork first via `gh repo fork`, push to the fork,
  and open the PR from there — this is the standard submission flow for adding an
  entry to a public listing
- state clearly in the confirmation prompt which repo the PR will target, since
  opening a PR on someone else's project is a public act

### Tests

Fakes only — **no test may run `git push`, call `gh`, or touch a network.**

1. `ship` without confirmation pushes nothing — assert the fake runner was never
   called
2. a dirty tree outside the task branch aborts with a clear message
3. the PR body contains shape, tier, arm and rationale
4. `--force` is never present in any constructed git command
5. the default branch is never a push target
6. missing `gh` degrades to "pushed; open a PR here: <url>" rather than failing
7. targeting a repo without push access forks first
