# route-skill — build spec

Decide **where a task should run** — Claude, Codex, Kimi, or a local model — from task
shape, task complexity, and live quota; dispatch it; record what happened; and get
better at the decision over time. Learning is federatable, so a fleet of users
converges on which model is actually good at what.

Ships two skills from one engine:

- **`/route <task>`** — decide and explain, wait for confirmation
- **`/auto <task>`** — decide and dispatch immediately

Package name `route-skill` (free on PyPI and npm). Python, because the learning core
is [`banditry`](https://github.com/jhammant/banditry) (already public, on PyPI).
MIT, matching the rest of the stack.

## The decision, in order

Routing is **not** a single score. It is a filter chain, then a bandit.

```text
task text
   │
   ├─▶ 1. SHAPE      closed vocabulary — what kind of work is this?
   ├─▶ 2. VETO       hard gates: must this stay here?            → claude, done
   ├─▶ 3. ELIGIBLE   which arms can do this shape at all?
   ├─▶ 4. QUOTA      drop arms with no headroom (quotamax)
   ├─▶ 5. SELECT     bandit picks among survivors (banditry)
   ├─▶ 6. DISPATCH   hand to /codex, /kimi, /local-llm, or stay
   └─▶ 7. OUTCOME    record reward, update posterior, maybe escalate
```

A naive complexity+quota score would cheerfully send a 4,000-item batch to Codex.
Steps 1–3 exist to make that impossible.

### 1. Shape — a CLOSED vocabulary (privacy-critical)

`shapes.py` classifies the task into exactly one of a **fixed enum**. This is not
cosmetic: the shape becomes the federation context key, and a free-text key would
leak task content. It also makes k-anonymity reachable — a bounded vocabulary
accumulates contributors per cell instead of a long tail of singletons.

```text
coding:implement  coding:refactor  coding:test  coding:debug  coding:review
batch:classify    batch:summarize  batch:extract  batch:embed
research          writing          orchestration
```

Classify with deterministic heuristics first (verb + object + volume cues); with
`--llm-shape`, fall back to a cheap local LLM call via `local-llm ask --class reflex`
when ambiguous. Never
invent a shape outside the enum — unknown maps to `orchestration`, which never routes
away. Adding a shape is a **schema version bump**, not a config tweak.

#### Shape confidence

`classify_with_confidence` reports `shape`, `confidence` (`high` or `low`) and
`source` (`deterministic`, `fallback` or `llm`). `classify_shape` retains the
shape-only API. Unmatched text and weak noun cues (for example "parser coverage"
or "daily digest for all teams") report low confidence. The plan includes
`shape_confidence`, `shape_source`, and, when low,
`suggest: "pass --shape <shape> --tier <tier>"`. This is heuristic confidence,
not a probability. Explicit `--shape` reports high confidence with source
`explicit` and bypasses classification.

Deterministic classification is the default; `--no-llm` is retained. An opt-in
LLM can resolve both defaults and weak matches. Failed or off-enum responses
retain the low-confidence deterministic result. Feature-building intent takes
precedence over incidental batch vocabulary: "build the trial SMS notices
feature with a signed redirect route" is `coding:implement`, as is a digest
feature build with volume words. Actual batch summaries remain `batch:summarize`.

### 2. Vetoes — hard gates, not scores

A veto means "this cannot leave", regardless of quota or complexity:

| veto | why |
|---|---|
| needs this conversation's context | no other pool has it |
| cross-repo / multi-directory orchestration | Codex and Kimi are dir-sandboxed |
| private data + a remote pool | data must not leave the machine |
| no clear acceptance check | can't verify a hand-off, so don't hand off |
| user pinned a pool (`--pool`) | explicit beats inferred |

Vetoes are evaluated before anything else and short-circuit to `claude`.

### 3–4. Eligibility and quota

`pools.py` holds the arm registry — **config-driven so new models are a config entry,
not a code change** (`~/.config/route/pools.toml`). Each arm declares the shapes it
accepts, whether it needs a repo, whether it is remote, and its cost class.

Shipped arms: `claude`, `codex`, `kimi`, `deepseek`, `local-batch`, `local-agent`, `free`.

Quota comes from `quotamax agent --json`. An arm whose pool reports `critical` headroom
is **removed from eligibility**, not penalised. Quota gates *availability*; the bandit
decides *quality* among what's available. Keeping these separate is what stops a
cheap-but-bad pool winning just because it's idle.

After shape/health, sensitivity and quota gating, apply the **paid backstop
rule**: if any surviving arm has cost `included`, `free` or `local`, remove
every `paid` arm. A lower Beta prior cannot enforce this rule because random
Thompson draws overlap. `GateResult.removed_by_cost` and the plan's
`removed_by_cost` list explain these removals separately from quota failures.
Codex and Kimi defaults are `included` subscription pools alongside Claude;
DeepSeek remains `paid` metered capacity. Configured arms follow their declared
class. A surviving local/free pool also keeps paid capacity out after all
subscriptions are exhausted; an unhealthy or wrong-shape pool does not.
Explicit pool pins still short-circuit inference. `--no-quota` skips headroom
checks but retains the paid backstop rule.

### 5. Select — the bandit, and why there is no "switch to adaptive"

`router.py` wraps `banditry`. **One bandit per context cell**, named
`route:{shape}:{tier}` — so `coding:refactor:hard` learns independently of
`batch:classify:trivial`. Arms are the pools. `Router.select` takes the survivors
of steps 2–4 directly. Its enriched Thompson draw uses the same Beta counts as
banditry, with the decision-local adjustments below.

Complexity tier comes from [`taskgauge`](https://github.com/jhammant/taskgauge)
(`estimateComplexity` → `trivial|simple|moderate|hard|open-ended`), shelled out via
`node`. **Optional** — ship a built-in lexical fallback so the published tool works
standalone.

Apply feature-build floors after either estimator using the same cue as shape
classification: at least `moderate` (the enum's medium tier), or `hard` when the
text names at least three distinct component kinds from route, endpoint,
migration, webhook, worker, test and flag. Normalise case and singular/plural
forms before counting; repeated mentions are one component kind. Long-horizon
work still floors at hard, stronger estimates including open-ended are never
downgraded, and explicit CLI `--tier` overrides remain authoritative. The exact
task "build the trial SMS notices feature with a signed redirect route" must
be `coding:implement:moderate` without an override.

Seed each arm's `Beta(alpha, beta)` prior from a static rule table: a pool the rules
favour for that cell starts at `Beta(8,2)`, one they don't at `Beta(2,8)`. With
`algorithm="thompson"`:

- **cold start** — posterior ≈ prior, so it behaves exactly like hand-written rules
- **with evidence** — observations swamp the prior and it behaves adaptively
- the crossover is **automatic and per-cell** — frequently-used routes go adaptive
  fast, rare ones keep deferring to the rules

There is deliberately **no flag, threshold, or phase-2 migration** for "become
adaptive". That is the entire reason to use a bandit rather than bolt learning on later.

#### Signed quota adjustment: surplus and pressure

Retain weekly windows from the same `quotamax agent --json` and
`quotamax providers --json` snapshots used by headroom gating. Agent windows use
`weekly.percentUsed` and `resetsInMinutes`; provider limits use `percent` and
`resetsInMinutes` or `resetsAt`. No additional quota probe is needed for selection.

For a valid seven-day window with positive elapsed time and time until reset,
project the fraction of total quota consumed by reset at the current pace.
Surplus requires at least two days remaining; pressure applies until reset:

```text
elapsed_fraction = 1 - resetsInMinutes / 10080
used_fraction = percentUsed / 100
projected_end = used_fraction / elapsed_fraction
surplus_fraction = clamp(1 - projected_end, 0, 1)
overuse_fraction = clamp(projected_end - 1, 0, 1)
if elapsed_minutes < 1440:
    surplus_fraction = max(max(0, elapsed_fraction - used_fraction), 0.5 * surplus_fraction)
    overuse_fraction = max(max(0, used_fraction - elapsed_fraction), 0.5 * overuse_fraction)
if resetsInMinutes < 2880:
    surplus_fraction = 0
if headroom == "constrained":
    overuse_fraction = max(overuse_fraction, 0.5)
k = clamp(k, 0, 0.15)
adjustment = -k * overuse_fraction if overuse_fraction > 0 else k * surplus_fraction
selection_value = sampled_beta_value + adjustment
```

Before one full day has elapsed, use the larger of the old pace delta and half
the projected surplus or overuse to damp early noise symmetrically. At exactly
one elapsed day, use the full projection. Zero elapsed time returns zero
projection rather than dividing by zero.
With 7% used and 6.2 days remaining, projected end usage is 61.25%, unused quota
is 38.75%, damped surplus is 19.375%, and the default bonus is 0.0290625.
With 28% used after 0.7 days, projected end usage is 280%, overuse caps at 100%,
first-day damping gives 50%, and the default pressure adjustment is −0.075.

The pure `quota_adjustment` function uses no projection for missing, non-finite,
out-of-range or expired data. Constrained headroom still imposes at least
`0.5 * k` pressure: it may represent a binding session limit even if weekly
quota is under-used. Pressure takes precedence over surplus. `[surplus] k =
0.15` in `config.toml` still controls both directions; zero disables both.
Adjustments are bounded to [−0.15, +0.15] and never remove an arm. Only surviving
candidates may receive them.
Vetoes short-circuit selection; sensitive-local preference narrows candidates
before bonuses or memory. Neither can reintroduce a quota-removed arm.
Plans report `quota_adjustment: {arm: signed_value}` including zeros, plus
`+surplus:<chosen>` or `-pressure:<chosen>` in `why` when applicable.
`surplus_bonus` is a deprecated alias containing the same signed map for one
release; the old Router keyword is also retained as an alias, with the new
keyword taking precedence if both are supplied. Adjustments are not observations.

#### Nearest-neighbour task memory

Keep a separate `task-memory.json` under `ROUTE_STATE_DIR`, bounded to 2,000
decision rows including pending decisions, dropping the oldest on append.
Each row stores `decision_id`, tokens, shape, tier, arm and final `score` (null
while unresolved). A later grade updates that row in place, never appends a
duplicate; settlement of an evicted decision does not resurrect its memory.
Legacy accepted/failed events resolve to 1/0. Completed/verified alone do not
claim a final quality score.

Normalise with Unicode NFKC, case folding, word splitting, duplicate removal
and a small fixed stop-word set. Persist HMAC-SHA256 hashes of these tokens for
**every** task, including sensitive/private tasks, using a random 32-byte
per-install salt in `task-memory.salt`. Memory and salt files are owner-only;
task text and plaintext tokens are never saved. Equal tokens still compare
within an installation; another installation has unrelated hashes.

Find up to five highest-Jaccard neighbours of the same shape (tiers may differ),
with similarity ≥ 0.25 and an eligible, selectable arm. Ignore pending rows.
For each neighbour, temporarily add to its arm's posterior:

```text
weight = 0.5 * similarity
alpha += weight * score
beta  += weight * (1 - score)
```

Plan JSON exposes only `neighbours: [{arm, score, similarity}]`. These additions
exist only for the current draw; persistent counts and federation never see
them. Memory, salt and hashes are local private state, not anonymous public data.

### 6–7. Dispatch, outcome, escalation

Dispatch shells out to the existing skills — `/codex`, `/kimi`, `/local-llm` — or
returns `stay` for Claude. Never reimplement what those already do.

Reward events, ranked (mirroring banditry's own "click first, open second" guidance —
rank by the signal that is hardest to fake):

| event | meaning |
|---|---|
| `accepted` | diff kept, no escalation needed — **the real signal** |
| `verified` | tests passed |
| `completed` | ran without erroring |

#### Graded outcomes and settlement

`route outcome` accepts exactly one of `--outcome`, `--score`, `--fix-passes`,
or standalone `--abandoned` (`--rewritten` alias). Abandonment may accompany
fix passes but not an explicit score or legacy outcome. Validate finite scores
in [0, 1], non-negative integer fix passes, closed-enum shapes and tiers.

Graded quality is one fractional Beta observation: alpha gains `score`, beta
gains `1 - score`. Fix passes use `max(0, 1 - 0.3 * N)`: 0/1/2/3/4+ passes
give 1.0/0.7/0.4/0.1/0.0. Abandoned work or a supervising agent's rewrite scores
0.0. Store `final_score` on the decision; repeated identical scores are no-ops.
Corrections change the existing accepted reward by `new_score - old_score`.
If the old feedback was ungraded accepted, its old reward is 1; otherwise 0.
This replaces plain completed feedback without creating a second trial.

Legacy cumulative events are retained and de-duplicated per decision. After a
grade, a legacy accepted event can update metadata but cannot overwrite the
grade or add reward. An exit-code failure and subsequent repeated failed reports
likewise share the existing trial. No-ID manual legacy event calls remain
supported; a manual grade with no matching decision creates one seeded trial.

Every plan prints a UUID `decision_id`, saved in `decisions.jsonl`. Use:

```bash
route settle --decision <id> --fix-passes 1 --merged
route settle --decision <id> --abandoned
route outcome --decision <id> --score 0.7
```

`--merged` records accepted/verified/completed metadata while retaining the
graded reward. It is mutually exclusive with `--abandoned`. Outcome commands
can alternatively resolve the latest decision matching `--shape`, `--tier`
and `--arm`; IDs are preferred for delayed or interleaved feedback. Unknown
IDs and conflicting identity flags fail without updating counts. Pre-ID rows
receive an ID when first settled; historical counts are not migrated.

CLI plans, declined dispatches and `stay` decisions are pending rather than
negative observations. A dispatch records one trial before launch; settlement
of externally executed work records it if missing. Completed and verified
events alone keep their legacy reward semantics. Settlement reports caller
verification; it does not run tests, merge changes or dispatch agents itself.

**Escalation ladder, capped at one hop.** On failed verification, retry once on the
next-stronger eligible arm. Escalating away from an arm records a *negative* outcome
for it, so unreliable routes decay without anyone tuning weights.

### Swarms — fan-out when the pool has headroom

Some arms can run **many instances at once**. Kimi's `/usages` payload reports
`parallel.limit` (observed: **30** on ADVANCED) — server-authoritative, so read it
rather than guessing. Codex and local have their own, much lower, practical limits.

**A swarm is a property of the dispatch, not a separate arm.** Do NOT add `kimi-swarm`
as its own arm: that would split the bandit's evidence about Kimi's *quality* across two
arms that share one underlying model, and neither would learn properly. The bandit picks
`kimi`; a separate concurrency planner then decides *how many*.

```text
n = min(units, pool.parallel_limit, headroom_cap)
```

**1. `units` — the decomposability gate, and the one that actually matters.**
A swarm only helps when the task genuinely splits into *independent* units ("write tests
for these 8 modules", "migrate these 12 files"). One hard refactor does not parallelise
by running it twelve times. If `units == 1`, there is no swarm regardless of available
headroom. Deriving `units` is part of shape classification: a task is fan-out-able only
when it names a set of separable targets and each unit has its own acceptance check.

**2. `headroom_cap` — scale to quota, because a swarm multiplies burn by n.**

| pool headroom (quotamax) | cap |
|---|---|
| `abundant` | up to `pool.parallel_limit` |
| `comfortable` | 8 |
| `constrained` | 1 (no swarm) |
| `critical` | arm already ineligible |

**3. Safety.** Kimi has no filesystem sandbox, so every swarm member gets **its own
directory and its own branch** (`kimi/<slug>-<i>`), and results are collected and verified
individually. A swarm that writes into one shared tree will corrupt it.

**4. Statistical honesty — do not inflate the posterior.** A 12-unit swarm produces 12
results, but they are **highly correlated**: one task, one model, one moment. Recording
them as 12 independent Bernoulli trials would overstate confidence enormously and let a
single lucky swarm dominate a context cell. Record **one** observation per swarm against
the arm (success = the fraction of units accepted, credited as a single weighted trial),
and keep the per-unit detail in `stats` only. Add a test asserting that an n-unit swarm
advances `alpha + beta` by 1, not by n.

## `route benchmark` — manufacture evidence instead of waiting for it

The bandit only learns from work that actually happens. That leaves three gaps:
rarely-used context cells never accumulate evidence; exploration is limited to
tasks you happen to have; and **a newly added model or pool starts on a guessed
prior with no way to earn a better one except by being given real work and
hoping**. `benchmark` closes all three by generating evidence on demand.

```text
route benchmark                       # every eligible arm, standard suites
route benchmark --arm local-agent     # one arm (e.g. a model just downloaded)
route benchmark --suite instruct      # one suite
route benchmark --seed                # write results in as priors (down-weighted)
route benchmark --compare a,b         # head-to-head on the same tasks
```

### Suites — machine-checkable only

Every suite must be **objectively verifiable**. An LLM judge would import its own
bias into the very numbers used to decide routing, so quality here is measured by
execution, not opinion.

| suite | measures | ground truth |
|---|---|---|
| `throughput` | tok/s single + concurrent, load seconds, peak GB | none needed — mechanical |
| `instruct` | constrained-output adherence: does it answer *only* from a permitted set? | exact set membership |
| `classify` | accuracy on a labelled set | held-out labels |
| `code` | make a failing test pass in a scratch repo | the test suite exits 0 |

`instruct` earns its place from a real finding: on a 3,803-item run a 4-bit local
model silently substituted its own taxonomy (`content`, `infrastructure`,
`security`…) for the categories it was given, 2.3% of the time. Nobody benchmarks
constrained-output adherence, and it is precisely what determines whether a batch
job is usable. Measure it.

### Distribution shift — the reason this needs care

**Benchmark evidence is not real-work evidence.** Benchmark tasks are not drawn
from the distribution of the user's actual work, so feeding them into the
posterior at full weight would let synthetic results drown out the real signal and
make the router confidently wrong.

Therefore:

- benchmark outcomes are recorded in a **separate namespace**
  (`bench:{shape}:{tier}`), never mixed into the live cell
- `--seed` merges them into live priors at **0.3×**, below the 0.4× community
  weight — synthetic evidence is worth less than a stranger's real evidence
- one real observation always outranks a benchmark run of the same size
- `route stats` shows real vs benchmark counts in separate columns, so it is
  always visible how much of a cell's confidence is manufactured

### Feeding the rest of the stack

- `throughput` results populate `~/.local/state/local-llm/throughput.json`, which
  is what makes `local-llm plan` accurate on a model the machine has never run.
- `throughput` is also the **zero-privacy-risk federated dataset** — `(model,
  quant, hardware) → tok/s` contains no task content at all, so it can be shared
  freely and is the wedge that bootstraps adoption.
- `--compare` is the concrete answer to "is this new model actually better?",
  run on identical tasks rather than vibes.

## Stats and federation — built in from day one, not retrofitted

This is a first-class feature, not telemetry bolted on. It must be recording from the
first run or the data is worthless.

### Local stats

`stats.py` records per decision: context cell, eligible arms, chosen arm, why (veto /
prior / posterior), outcome events, wall-clock, tokens, escalations. Plus, for local
runs, `(model, quant, context_length, hardware) → tok/s, items/s, load seconds, peak GB`.

```text
route stats                    # per-cell: obs, success rate, prior vs posterior
route stats --arm codex        # how is one pool doing, by shape
route stats --throughput       # local model performance table
```

Showing **prior vs posterior side by side** is the transparency that makes an adaptive
router trustworthy: you can see exactly when evidence took over from the rules.

### What federates, and why it is safe

The unit of sharing is the **Beta posterior**, because Beta is conjugate to Bernoulli
and therefore **composes by addition**: merging N users is `α=Σαᵢ, β=Σβᵢ`. Federation
is summation — no gradients, no weights, no consensus protocol. And what ships is
**counts, not content**, so privacy is structural rather than bolted on.

Leaves the machine (opt-in, off by default):

```json
{"schema":1, "context":"coding:refactor:hard", "arm":"codex",
 "model":"gpt-6-astra", "alpha":14, "beta":3}
```

**Never** leaves: prompts, diffs, file paths, repo names, task titles, item content,
decision IDs, task-memory tokens/hashes, salt or neighbour pseudo-observations.
Federation reads only persistent bandit counts, not the separate memory store.
Record keys and the federation schema are unchanged; real graded alpha/beta
counts may be fractional.

Two datasets, deliberately separate because their sensitivity differs:

- **throughput** — `(model, quant, hardware) → tok/s`. Zero task content. Ship this
  first: "what tok/s does gpt-oss-120b get on an M4 Max at 4-bit, and will it fit?"
  is a question many people have and nobody has good data for. It also makes
  `local-llm plan` accurate on hardware that has never run the model.
- **routing quality** — the posteriors above. Higher sensitivity, gated harder.

### Community prior, local posterior

Download the community aggregate as your **prior**; your own observations update on top.

- a new user inherits accumulated wisdom immediately — **cold start solved**
- your own data always eventually dominates, because your counts grow and the
  downloaded prior does not
- discount community counts by **0.4×** and team counts by **0.7×** before use — the
  same trust weights `claude-history-cloud` already ships, here as prior-strength
  multipliers

This is hierarchical Bayes, and "own first, team fills gaps, community validates"
falls out of the maths instead of being a rule someone has to enforce.

### Anti-gaming — needed here in a way it isn't for shared knowledge

A public routing registry creates a **vendor incentive to inflate a model's numbers.**

- cap per-installation contribution so one machine cannot dominate a cell
- k-anonymity ≥ 3 contributors before a cell publishes (matches existing practice)
- robust aggregation (trimmed mean / median-of-means), never a raw sum
- pin model identity to `model + quant + version` — `qwen3.6-27b` at 4-bit and bf16
  must never merge into one arm
- publish the aggregate openly so anyone can audit it

### V1 infrastructure: no server

The community prior is a **static JSON file in a public GitHub repo**, rebuilt nightly
by CI from submitted counts. Zero infra, zero hosting cost, auditable, forkable, and
no central database of user behaviour to become a liability. Graduate to
`claude-history-cloud` only if volume demands it.

```text
route federate export        # what WOULD be shared — print it, share nothing
route federate push          # opt-in contribute
route federate pull          # refresh community priors
route federate status        # what's shared, when, what's held back
```

`export` must exist and be the documented first step: **let people see exactly what
would leave their machine before anything does.**

## Module layout

```text
src/route/
  shapes.py       closed-vocabulary shape classification
  complexity.py   taskgauge bridge + built-in fallback scorer
  pools.py        arm registry (config-driven)
  eligibility.py  vetoes + quota gating
  router.py       banditry wrapper, one bandit per context cell
  surplus.py      bounded signed quota adjustments + quota-window extraction
  memory.py       bounded local hashed-token nearest-neighbour memory
  outcomes.py     outcome -> reward events, escalation ladder
  stats.py        local stats + reporting
  federate.py     export / push / pull / status
  storage.py      JSON-file backend (banditry duck-types .incr/.counts)
  cli.py          route / auto / stats / federate
skill/SKILL.md        the /route skill
skill-auto/SKILL.md   the /auto skill
data/priors.toml      static seed rules -> Beta priors
```

`storage.py` matters: banditry accepts "anything with `.incr(key, arm, by)` /
`.counts(key)`", so a small JSON-file backend keeps this local and dependency-free —
no Redis for a single-user CLI.

## Tests

Deterministic, no network, no real dispatch, no money spent.

1. **shape**: every fixture maps into the enum; unknown → `orchestration`.
2. **veto**: each veto short-circuits to `claude` regardless of quota/complexity.
3. **eligibility**: a `batch:*` shape never yields `codex`/`kimi`; a `coding:*` shape
   never yields `local-batch`; `critical` headroom removes an arm entirely.
4. **bandit**: with seeded priors and zero observations, selection matches the static
   rule table (injected RNG); after N successes for a non-favoured arm, selection flips.
5. **escalation**: one hop only; the escalated-from arm receives a negative outcome.
6. **federation**: `export` emits counts only — assert no task text, paths, or repo
   names appear in the payload for any fixture; k-anonymity suppresses cells with
   fewer than 3 contributors; merging two exports sums alpha/beta.
7. **priors**: community counts are discounted 0.4× before merging.

## Out of scope for v1

Adaptive weight tuning beyond the bandit; a hosted federation server; cost-based
optimisation across paid pools (route on quality and eligibility, not price);
auto-spinning rented GPUs — `/route` may never trigger paid provisioning.
