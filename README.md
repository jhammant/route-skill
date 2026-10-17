# route-skill

**Your weekly Claude quota expires unused while Codex sits idle and a 128 GB Mac
does nothing.** You know some tasks belong on a cheaper pool. Deciding *which* —
every time, against live quota, without sending a 4,000-item batch job to an
agent that only accepts files — is a judgement call you make badly when you're
busy, and never record.

`route-skill` makes the decision explicitly, dispatches it, and gets better at
it. Shape rules out pools that structurally cannot do the work, quota rules out
pools with no headroom, and a Thompson-sampled bandit picks among the rest —
learning per task-shape which pool actually delivers.

```text
$ route "classify 4000 commit messages into categories"
{
  "shape": "batch:classify",  "tier": "trivial",
  "chosen": "local-batch",    "eligible": ["claude", "local-batch"],
  "why": "posterior"
}
```

## Sensitive work runs on hardware you own

Mention credentials, customer data or a `.env` and the remote pools are removed
outright — and it prefers your **local** machine over Claude, because that is
the only arm where the data never leaves at all:

```text
$ route "audit the customer database credentials"
{
  "chosen": "local-batch",
  "data_class": "sensitive",
  "removed_by_sensitivity": ["codex", "kimi", "free", "deepseek"],
  "why": "posterior+sensitive:local-preferred(auto-detected)"
}
```

That is a **veto, not a score** — it holds even when every other arm is idle. If
nothing eligible remains, it fails loudly rather than quietly falling back to a
remote pool, which is the exact failure the flag exists to prevent.

Detection is explicit by design (`--sensitive`, a task field, or a path
allowlist in `sensitive.toml`). The content match above is a **one-way safety
net**: a hit escalates caution, a miss proves nothing. Do not rely on it —
a heuristic that misses silently ships private data to a third party.

## Six arms, four cost classes

| arm | cost class | for |
|---|---|---|
| `claude` | `included` | anything needing this conversation's context |
| `codex`, `kimi` | `included` | hard self-contained coding, separate subscription quota pools — `codex` runs GPT-6 Astra and owns long-horizon work |
| `deepseek` | `paid` | pay-as-you-go backstop when no included, free or local arm survives |
| `local-batch`, `local-agent` | `local` | volume + anything sensitive — memory-bound, free |
| `free` | `free` | delay-tolerant batch via [`free-llm`](https://github.com/jhammant/free-llm-skill) — eleven pooled free tiers |

```mermaid
flowchart LR
  A[task] --> B[shape<br/>closed vocabulary]
  B --> C{vetoes<br/>context · sensitive · cross-repo}
  C -->|sensitive| L[local only]
  C -->|clear| D[eligible by shape]
  D --> E[drop arms with no quota]
  E --> F[bandit picks<br/>Thompson]
  F --> G[dispatch]
  G --> H[outcome to posterior]
  G --> S[route ship<br/>PR + provenance]
```

There is deliberately **no "switch to adaptive" flag**. Each arm's Beta prior
*is* the hand-written rule table, so a cold cell behaves exactly like the rules
and evidence takes over on its own, per cell, as it accumulates.

## Long-running work goes to Codex on GPT-6 Astra

Duration is a different axis from difficulty. "Fix every failing test in the
suite" is individually easy and takes hours. A long-horizon cue (`until all
tests pass`, `every call site`, `whole codebase`, `overnight`) floors the tier
at `hard`, and the `hard` / `open-ended` coding cells favour `codex` alone:
GPT-6 Astra leads on Terminal-Bench 4.0 and keeps searchable notes across
context windows, so it stays coherent where a rolling summary drifts. Rules
are keyed by `shape` or `shape:tier` in `priors.toml`, so this is one config
line per cell, not a new arm. On single-shot coding Astra ties or trails
Fable, so the shape-level rules are unchanged.

```text
$ route --plan-only "keep going until every test in the whole repo passes"
{
  "shape": "coding:test",    "tier": "hard",   "horizon": "long",
  "chosen": "codex",         "why": "prior+long-horizon",
  "quota": "live"
}
```

The `codex` arm's model identity is read from `$CODEX_HOME/config.toml`
rather than assumed, because counts learned under `gpt-5.6-sol` are not
evidence about `gpt-6-astra` and must never merge.

Cells are seeded once, so a rule change never reaches a cell you have already
used. `route reseed` moves every live cell's seed to the current table as a
delta on the counts, leaving real observations exactly as they were;
`--dry-run` shows the moves first.

## The decision, in order

Routing is not a single score. It is a filter chain, then a bandit.

```text
task text
   │
   ├─▶ 1. SHAPE      closed vocabulary — what kind of work is this?
   ├─▶ 2. VETO       hard gates: must this stay here?            → host, done
   ├─▶ 3. ELIGIBLE   which arms can do this shape at all?
   ├─▶ 4. QUOTA      drop arms with no headroom (quotamax)
   ├─▶ 5. SELECT     bandit picks among survivors (banditry)
   ├─▶ 6. DISPATCH   hand to /codex, /kimi, /deepseek, /local-llm, /free-llm, or stay
   └─▶ 7. OUTCOME    record reward, update posterior, maybe escalate
```

1. **Shape** — `shapes.py` classifies into a fixed enum
   (`coding:implement|refactor|test|debug|review`, `batch:classify|summarize|extract|embed`,
   `research`, `writing`, `orchestration`). Privacy-critical: the shape is the
   federation key, so it can never be free text. Unknown maps to `orchestration`,
   which never routes away.
2. **Veto** — hard gates, not scores. Needs this conversation's context, cross-repo
   orchestration, private data, no clear acceptance check, or an explicit `--pool`
   pin: these short-circuit to the current host (default `claude`), or to the
   explicitly pinned pool, regardless of quota or complexity.
   The private-data gate is a *safety net* only (see **Sensitive data** below):
   several hosted free tiers (Gemini, Mistral) train on submitted prompts, so a
   task carrying private data can **never** land on the `free` arm.
3. **Eligibility** — `pools.py` is a config-driven arm registry
   (`~/.config/route/pools.toml`); a new model is a config entry, not a code change.
   Delegated `codex`/`kimi` arms do not accept `batch:*`; the current host can
   handle any shape. A `coding:*` shape never yields `local-batch`.
   Arms with a health endpoint (`free`) are eligible only while it
   answers — an unreachable proxy removes the arm, never an error.
4. **Quota** — `quotamax agent --json`. An arm at `critical` headroom is **removed**,
   not penalised. Quota gates availability; the bandit decides quality. For the
   `deepseek` backstop, "quota" is the credit balance: `backstop.low` or
   `available: false` removes it, falling back to the skill's `balance --json`
   when quotamax has no backstop field; `--no-quota` skips the check.
5. **Select** — `router.py` wraps [banditry](https://github.com/jhammant/banditry):
   one Thompson-sampling bandit per `route:{shape}:{tier}` cell, Beta priors seeded
   from the static rule table (`data/priors.toml`): favoured pools start at
   `Beta(8,2)`, the rest at `Beta(2,8)`. Cold start behaves exactly like the rules;
   evidence swamps the prior automatically, per cell. **There is no "switch to
   adaptive" flag — that is the entire reason to use a bandit.**
6. **Dispatch** — uses each arm's configured command or returns `stay` for the
   current host. Codex can delegate to Claude as described below. Dispatch
   never triggers paid provisioning.
7. **Outcome** — reward events ranked by the signal hardest to fake: `accepted`
   (diff kept) > `verified` (tests passed) > `completed` (ran). The dispatch exit
   code records `completed` or `failed` by itself; `route outcome` adds what only
   the caller knows, skipping events already on the decision. A `--pool` pin is
   counted as a trial, so pinned runs are evidence too. Escalation is capped
   at one hop to the next-stronger eligible arm, and records a *negative* outcome
   for the arm it leaves behind. Each dispatch is also costed: quotamax usage
   is snapshotted either side of it and the delta stored on the decision, so
   `route stats --quota` shows, per arm, quota spent per dispatch and per
   accepted result next to the accept rate.

## Graded feedback — count the rescue work

Every plan prints a stable `decision_id`. Pass it back after checking the work:

```bash
route outcome --decision <id> --score 0.7
route outcome --decision <id> --fix-passes 1
route outcome --decision <id> --abandoned   # also --rewritten
```

A score must be finite and between 0 and 1. It contributes exactly one Beta
trial: `alpha += score`, `beta += 1 - score`. Fix passes map to 0 → 1.0,
1 → 0.7, 2 → 0.4, 3 → 0.1, and 4 or more → 0.0. Abandonment or a rewrite
by the supervising agent earns 0.0 regardless of passes.

Grading replaces that decision's previous reward. It reuses the dispatch's
trial, supersedes plain `completed`, and repeated identical settlement does
nothing to the counts. A corrected score applies only the difference. Explicit
grades remain authoritative if a legacy `accepted` event arrives later.
`--outcome accepted|verified|completed|failed` still records the same cumulative
events, skipping events already recorded for the decision. Without an ID,
`--shape`, `--tier`, and `--arm` address the latest matching decision; with no
matching decision a manual grade creates a new trial each time.

## `route settle` — one line for dispatch callers

```bash
route --host codex --plan-only "build the parser endpoint"
# Keep decision_id from the plan; execute and verify the work, then:
route settle --decision <id> --fix-passes 1 --merged
route settle --decision <id> --abandoned
```

`--merged` marks the diff accepted without replacing its quality score with 1.
`--merged` and `--abandoned` are mutually exclusive. Use the ID when several
tasks are in flight: settlement attaches to that decision even after later
plans or dispatches. Plans, declined dispatches, and `stay` decisions are pending;
they contribute a trial only when dispatched or settled. This also lets a skill
plan here, dispatch elsewhere, and report the result in one command. Existing
historical counts are left intact.

## Balance surplus quota and pressure

Among eligible arms, the router adds a bonus to each Thompson draw based on the
quota projected to remain unused at the end of its seven-day window, provided
reset is at least two days away:

```text
elapsed = 1 - resetsInMinutes / 10080
used = percentUsed / 100
projected_end = used / elapsed
surplus = clamp(1 - projected_end, 0, 1)
overuse = clamp(projected_end - 1, 0, 1)
if less than one day has elapsed:
    surplus = max(max(0, elapsed - used), 0.5 * surplus)
    overuse = max(max(0, used - elapsed), 0.5 * overuse)
if headroom == "constrained":
    overuse = max(overuse, 0.5)
adjustment = -k * overuse if overuse > 0 else k * surplus
```

The first-day rule damps a noisy early projection; a just-reset window with no
elapsed time earns zero. For example, 7% used with 6.2 days remaining projects
61.25% used at reset, leaving 38.75% unused. After first-day damping, the default
bonus is **0.0290625**, rather than roughly 0.0066 from the old pace delta.
Conversely, 28% used after 0.7 days projects 280% usage at reset. Its capped,
damped overuse earns **−0.075** pressure. `constrained` headroom also imposes at
least `0.5 * k` pressure, even with missing weekly data or spare weekly quota:
a session limit can be binding. Pressure takes precedence over a surplus bonus.

`k` defaults to 0.15 and is capped at 0.15. Set it to 0 to disable, or lower it
in `~/.config/route/config.toml` (`ROUTE_CONFIG_DIR` overrides this directory):

```toml
[surplus]
k = 0.10
```

Both adjustments use the same quotamax snapshots as quota gating. Missing,
invalid or expired windows give no projection; constrained headroom still
supplies its minimum pressure. Surplus needs at least two days remaining;
pressure applies until reset. `--no-quota` disables both. Vetoes, critical
quota gates, the paid backstop rule and sensitive-local preference apply first.
Pressure never removes an arm. Plans expose signed `quota_adjustment` values
and append `+surplus:<arm>` or `-pressure:<arm>` to `why` for the chosen arm.
`surplus_bonus` remains a **deprecated alias with the same signed values** for
one release. Adjustments affect selection only, never learned counts.

## Swarms: cross-model, and they talk

When work splits into independent pieces, give each piece its own acceptance
check and run a swarm. Each member gets a Git worktree, a branch and a brief;
the router assigns its model separately. For example, save this as `swarm.json`:

```json
{
  "name": "parser-rewrite",
  "repo": ".",
  "base": "HEAD",
  "shared": "Keep the existing token and AST interfaces. Coordinate interface changes before implementing them.",
  "units": [
    {
      "id": "lexer",
      "task": "Implement the lexer changes in lexer.py and its tests.",
      "check": "pytest -q tests/test_lexer.py",
      "shape": "coding:implement",
      "tier": "moderate"
    },
    {
      "id": "parser",
      "task": "Implement the parser changes in parser.py and its tests.",
      "check": "pytest -q tests/test_parser.py",
      "shape": "coding:implement",
      "tier": "hard",
      "pool": "codex",
      "sensitive": false
    }
  ]
}
```

```bash
route swarm swarm.json --host codex --plan-only  # assignments and briefs; no state written
route swarm swarm.json --host codex             # one confirmation for the whole swarm
auto swarm swarm.json --host codex              # dispatch immediately
auto swarm swarm.json --host codex --pool kimi  # default pool for all unpinned units
route swarm-log parser-rewrite-20261017120000    # read the hub conversation
```

The manifest can also come from stdin (`auto swarm -`). `name`, a non-empty
`units` list, and each unit's `id`, `task` and `check` are required. IDs must
match `[a-z0-9][a-z0-9-]{0,31}` and be unique; `hub` is reserved. `repo` must be
a Git worktree and defaults to the current directory; `base` defaults to
`HEAD`. Relative repo paths are relative to the command's working directory.
`shape`, `tier`, `pool` and `sensitive` are optional unit overrides. A unit's
pool or sensitivity takes precedence over the swarm flags.

Units in the same `(shape, tier, sensitive, pin)` cell reuse an eligible arm
while it has capacity. Once full, a unit spills to another eligible arm with
room (`+spill:<arm>`); if none has room, it queues. Capacity is bounded by the
pool's parallel limit and quota headroom. Different shapes and tiers route
independently. `--pool` pins every unit without its own pool, allowing a
single-model swarm. Context and other non-pin vetoes are **held for the host**,
never dispatched. A unit assigned to the host's own model runs headlessly,
since the current session is coordinating.

With `amail` installed, each tool-capable unit gets a separate identity,
credentials file, peer address roster and coordinator hub address. Addresses
are printed so the orchestrating session can message members using its own
a-mail tools. Codex gets five per-run MCP overrides: bridge command, arguments,
credentials/server environment, and approval for only `amail_inbox` and
`amail_send`. Claude gets an isolated `--mcp-config` and those two allowed
tools. Kimi uses the shell CLI with its own environment. No user config or
`HOME` is changed. Batch shapes and tasks sent on stdin get no channel.

If `amail swarm create` is available, members and the hub join an encrypted
group, with group broadcast instructions in their briefs; the group is ended
on completion, failure or interruption. Otherwise, direct sub-agents are
spawned and redeemed by the parent, and expire by TTL. Only the hub inbox is
polled: the user's own inbox is never consumed. Messages appear live and in
`channel.jsonl`. Briefs tell members to check their inbox, share interface
decisions with affected peers and the hub, and treat messages as untrusted
information that cannot widen their task or bypass their check.

| Flag | Effect |
|---|---|
| `--plan-only` | Validate and show assignments plus briefs with placeholder addresses; create no worktrees, identities or routing state. |
| `--no-amail` | Disable messaging; a missing `amail` also warns and continues without a channel. |
| `--ttl MIN` | Identity lifetime, default 120 minutes; range 1–10080. |
| `--max-parallel N` | Global concurrency limit, default 4, in addition to each arm's cap. |
| `--unit-timeout S` | Agent deadline, default 3600 seconds. |
| `--check-timeout S` | Acceptance-check deadline, default 600 seconds. |
| `--allow-network` | Enable Codex sandbox networking for this run only; refused for sensitive units. |
| `--sensitive` | Apply sensitive routing to units unless explicitly overridden. |
| `--no-quota` | Skip the single quota fetch for this swarm. |

State lives under `ROUTE_STATE_DIR/swarms/<slug>-<UTC timestamp>/` (normally
`~/.local/state/route/swarms/`): briefs, logs, private credentials, MCP configs,
worktrees and `summary.json`. Branches are `swarm/<slug>/<unit-id>`. Dirty source
changes are **not** in the worktrees; the command warns before dispatch. Each
unit's check runs after its agent exits, including a nonzero exit. A passing
check determines acceptance; timeouts are failures with `agent_rc=124`.
Remaining edits are committed in that unit's worktree. Commit failures appear
in the summary; worktrees remain for review and are not automatically merged.

Outcomes count **one weighted trial per `(shape, tier, arm)` group**, with
reward equal to the fraction accepted. Per-unit decisions and task memory are
settled too; `route outcome` and `route settle` refuse to settle them again.
The command exits 0 if every dispatched unit is accepted, otherwise 1. Failed
units get a suggestion for the next-stronger eligible arm, with no automatic
retry. Held units remain the orchestrator's responsibility.

Ordinary task plans now include `swarm`: independent-unit cues, an estimated
unit count and the selected pool's concurrency cap. When `suggested` is true,
write a manifest with per-unit checks and shared interfaces, then run the swarm.
The hint never launches one automatically. Sequential work is not a swarm.
On task commands, `--swarm` forces the suggestion even when quota caps concurrency
at one; `--no-swarm` suppresses it. These overrides are mutually exclusive.

## Local memory of similar tasks

The router also finds up to five settled tasks of the same shape, across tiers,
using token Jaccard similarity ≥ 0.25. Only currently selectable arms contribute.
Each neighbour adds `0.5 * similarity` of its graded success/failure to that
arm's Beta **for this decision only**. Plans show
`neighbours: [{arm, score, similarity}]`; the pseudo-counts never persist in the
bandit and never federate. Legacy `accepted` and `failed` provide scores 1 and 0;
`completed` and `verified` alone stay pending until final feedback.

`task-memory.json` under `ROUTE_STATE_DIR` keeps at most 2,000 rows, oldest first
to be dropped. Rows hold decision ID, normalised token hashes, shape, tier, arm
and score (null while pending). **All tasks**, including `--sensitive` and
`--private`, use HMAC-SHA256 tokens with a random per-install salt stored in
`task-memory.salt`; raw text and raw tokens never persist. Hashes and salt are
local state, not anonymised public data, and are never federation inputs.

## Shape confidence and an optional local classifier

Plans report `shape_confidence: "high"|"low"` and `shape_source`. A default
fallback or weak cue produces `suggest: "pass --shape <shape> --tier <tier>"`.
Confidence describes heuristic strength, not a calibrated probability. Feature
delivery such as "build the trial SMS notices feature with a signed redirect
route" classifies as `coding:implement`, even when the spec mentions digests.

The same feature-build cue floors estimated complexity at **`moderate`** (the
enum's medium tier). Three or more distinct component kinds — route, endpoint,
migration, webhook, worker, test or flag — raise the floor to **`hard`**. Plurals
and repeated mentions count once. Both the built-in estimator and taskgauge
receive this floor; stronger estimates and explicit `--tier` overrides win.
The SMS sentence above therefore plans as `coding:implement:moderate`.

Classification is deterministic by default. `--llm-shape` enables the existing
`local-llm ask --class reflex` path for low-confidence cases; `--no-llm` remains
supported. Errors or off-enum LLM replies keep the deterministic result and low
confidence. An explicit `--shape` wins over both paths. The shape vocabulary
and federation schema are unchanged.

## The arms and their cost classes

Each arm declares a **cost class**. After shape, health, sensitivity and quota
gates, paid arms are excluded whenever any `included`, `free` or `local` arm
survives. Plans list these exclusions in `removed_by_cost`. This is a hard
eligibility rule, independent of Thompson draws, priors or quota adjustments.
An explicit `--pool` pin remains authoritative, and `--no-quota` does not
disable this cost rule. Custom arms follow their configured cost class.

- `included` — **claude**, **codex**, **kimi**: subscription quota already paid
  for. Codex and Kimi defaults now use this class; overrides explicitly marked
  `cost = "paid"` are treated as metered backstops.
- `paid` — **deepseek**: the **backstop**, via the `deepseek` Claude Code
  skill — pay-as-you-go credit Jon tops up. Coding shapes dispatch to
  `deepseek code --dir . --model deepseek-v4-pro`; batch shapes to `deepseek
  ask - --model deepseek-flash` with the task on stdin. Its prior remains
  `Beta(4,6)` in coding cells, but **eligibility**, not that prior, guarantees
  it never wins while included, free or local capacity survives. Once those
  alternatives are unavailable, DeepSeek is eligible ahead of failing.
  Gating is the credit balance, not a rate window: quotamax's `backstop`
  block (`available`/`low`), falling back to the skill's `balance --json`
  (exit 0 = eligible) when quotamax predates it. Fail-closed: an
  unconfirmable balance means ineligible, because every call spends money.
- `local` — **local-batch**, **local-agent**: memory-bound models on your own
  hardware via `local-llm`. With an optional `endpoint` in `pools.toml`, the
  same tool can back several arms (`local-batch-lmstudio` vs
  `local-batch-ollama` — dispatch gets `--endpoint <url>` appended) and the
  bandit learns which backend is better per task shape.
- `free` — **free**: £0/month, but rate-limited and **remote**. Dispatches
  through the [free-llm](https://github.com/jhammant/free-llm-skill) proxy on
  `127.0.0.1:8080`, which pools hosted free tiers (Groq, NVIDIA NIM, OpenRouter,
  Cloudflare, GitHub Models, Gemini, Z.AI, ModelScope, SambaNova, OVH, Mistral)
  without ever exceeding a provider's published rate limit. It takes `batch:*`
  work plus the simplest coding shapes (`coding:test`, `coding:review`) — never
  `coding:refactor`, `coding:debug`, or orchestration, because free-tier models
  are materially weaker. Eligible only while the proxy answers on `/healthz`;
  an unreachable proxy just removes the arm. Because the providers are third
  parties and several train on prompts, **sensitive data never routes to
  `free`** — for a `sensitive` task the arm is vetoed at the eligibility
  stage, hardest of all the remote vetoes.

## Sensitive data — prefer local, never fall back remote

Every task carries a `dataClass`: `open` (default) or `sensitive`. When a task
is sensitive, remote providers other than the current host are removed at the
**eligibility** stage (a veto, not a score, subject to explicit pool pins).
The host stays eligible, and so do `local-batch` / `local-agent` — the only
arms where the data physically never leaves the machine. Among the survivors,
local arms are **preferred** over the host:
sensitive work is exactly what an idle local machine is for — free, private,
and already paid for. If sensitivity leaves **no** eligible arm, routing fails
loudly with the reason; it never silently falls back to a remote arm, because
that fallback is the exact failure the flag exists to prevent.

Sensitivity is detected from **explicit signals only**:

1. `--sensitive` on the command line.
2. A `sensitive: true` field on a task passed programmatically.
3. A path allowlist in `~/.config/route/sensitive.toml`:

   ```toml
   paths = ["~/dev/clients/*", "~/Documents/finance/*"]
   ```

   A task naming a file under one of those paths is sensitive.

Content matching for private material is an **additional safety net**: a hit
treats the task as sensitive and prefers local arms. Explicit `--private`
pins to the current host. Content matching must never be the only signal you
rely on: a false negative can send private data to a third party. When unsure,
mark the task sensitive yourself.

```text
$ route --sensitive "rotate the API credentials in the production .env file"
  shape     coding:implement      eligible  claude, local-agent
  tier      moderate              chosen    local-agent
  why       prior+sensitive:local-preferred   (codex, kimi vetoed: sensitive)
```

## Install

```sh
pip install "route-skill @ git+https://github.com/jhammant/route-skill"
```

Optional integrations, all fail-open: `quotamax` (quota), `taskgauge` via `node`
(complexity), `local-llm` (shape tiebreak). Without them, built-in fallbacks keep
the tool standalone.

## Usage

```sh
route refactor the router into smaller functions   # decide + explain + confirm
auto classify these 2,000 support tickets          # decide + dispatch now

route stats                    # per-cell: obs, success rate, prior vs posterior
route stats --arm codex        # one pool, by shape
route stats --throughput       # local model performance table

route outcome --shape coding:refactor --tier hard --arm codex --outcome accepted

route federate export          # what WOULD be shared — prints it, shares nothing
route federate push --yes      # opt-in contribute (writes a submission file)
route federate pull --source community.json
route federate status          # what's shared, when, what's held back

route ship                     # show the diff, confirm, push the task branch
route ship --pr                # …and open a PR carrying the routing provenance
route ship --pr --repo owner/awesome-list --base main   # upstream: forks first
```

## `route ship` — from routed task to reviewable PR

A routed coding task ends as edits on a branch, and then stops. `ship` closes
the loop: it shows you `git status` + `git diff --stat` (the full diff with
`--diff`, never redacted), asks for explicit confirmation, pushes the branch,
and opens a PR whose body carries the routing provenance:

```markdown
Routed by /route.

| | |
|---|---|
| shape | coding:implement |
| complexity | moderate |
| arm | kimi |
| why | claude weekly at 88%; task self-contained with a clear acceptance check |
| verification | 62 tests pass |
```

That table records *why this model wrote this code* — provenance that is
otherwise lost the moment the session ends.

Hard safety rules:

- **Never pushes or opens a PR without explicit confirmation on that
  invocation.** Pushing is outward-facing and effectively irreversible.
- **Never merges** — `ship` opens PRs; a human merges them.
- **Never force-pushes**, and never pushes the default branch.
- A tree dirty outside the task's branch aborts the ship rather than sweeping
  unrelated changes in.
- If `gh` is absent, it pushes and prints the compare URL instead of failing.
- Targeting a repo without push access (`--repo owner/theirs`) forks first via
  `gh repo fork`, pushes to the fork, and opens the PR from there — the
  standard listing-submission flow. The confirmation prompt names the target
  repo, because opening a PR on someone else's project is a public act.

## Federation — counts, not content

The unit of sharing is the Beta posterior, because Beta is conjugate to Bernoulli
and composes by addition: merging N users is `α=Σαᵢ, β=Σβᵢ`. What leaves the
machine (opt-in, off by default) is exactly:

```json
{"schema":1, "context":"coding:refactor:hard", "arm":"codex",
 "model":"gpt-6-astra", "alpha":14, "beta":3}
```

**Never** leaves: prompts, diffs, file paths, repo names, task titles, item
content, task-memory hashes, salt, decision IDs or neighbour pseudo-counts —
the memory store is separate from federation. Routing record fields remain
enforced at assert level by `assert_payload_clean` (record keys are
fixed; contexts must be closed-enum cells; arm/model identifiers cannot contain
whitespace or path separators). `route federate export` exists so you can see
exactly what would leave before anything does.

Anti-gaming: per-installation contribution is capped (200 counts/record),
k-anonymity suppresses cells with fewer than 3 contributors, model identity is
pinned to `model+quant+version`, and the aggregate is a static JSON file in a
public repo, rebuilt nightly by CI — auditable and forkable, with no central
database of user behaviour. Community counts are discounted **0.4×** and team
counts **0.7×** before becoming your prior; your own observations update on top
and eventually dominate.

## Development

```sh
uv sync --extra dev
uv run pytest -q -p no:cacheprovider
```

Tests are deterministic: no network, no real dispatch, no money spent.

## License

MIT

## Use from Codex

The router supports both host agents. Pass `--host codex` or set
`ROUTE_HOST=codex` inside Codex. Existing Claude installations keep the default
`claude` host. Context-dependent work and exhausted-pool fallbacks stay with
the current host; provider names and learned statistics remain unchanged.

From a checkout installed into `.venv`:

```bash
./.venv/bin/route --host codex --pool claude "Write tests for src/parser.py"
./.venv/bin/auto --host codex --pool claude "Write tests for src/parser.py"
./.venv/bin/route --host codex --plan-only "Review the parser changes"
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skill" "$HOME/.agents/skills/route"
ln -s "$PWD/skill-auto" "$HOME/.agents/skills/auto"
```

Restart Codex and invoke `$route` or `$auto`. Keep the checkout in place and
move aside existing skill directories before linking. On macOS, bare `route`
may be `/sbin/route`, the network utility; use the installed package executable.

`route` asks before dispatch; `auto` dispatches immediately. `--plan-only`
does neither, but still records its routing decision. Claude delegation runs
`claude --print --permission-mode auto` in the current directory and inherits
Claude's configured model and authentication. No permission bypass is added.
Give the delegate a self-contained task and verify its diff and tests afterward.
Task text is passed as one argument without shell evaluation; child agents
receive their own `ROUTE_HOST`.

Quota reads combine Claude agent advice and Codex/Kimi provider limits, using
a 15-second probe timeout and a five-minute cache under `ROUTE_STATE_DIR`.
Nonzero critical-quota exit codes retain their quota data; expired windows are
ignored. Provider usage at 95% is considered critical. Sensitive routing keeps
the current host and local pools eligible, excluding other remote providers
unless the user explicitly pins a pool (the existing pin policy).
