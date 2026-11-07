// Pure helpers for ultra-watch: the workflow view of an /ultra run (phases, agents,
// totals, narrator log), what the status line says, and which toasts a new snapshot
// deserves compared with the last one seen.
//
// A newer `ultra-progress --json` carries `totals`, `phases` and `log`. An older one
// does not, and `view` then works the same view out of `planners` and `units`, so the
// pane and the status line look the same against either.
import type { UltraAgent, UltraModel, UltraPhase, UltraPhaseAgent, UltraTotals, UltraUnit } from '../types'

/** A finished or stopped run stays on the status line this long after its last update. */
export const RECENT_SECS = 15 * 60

/**
 * An unfinished run whose route process cannot be checked counts as stopped once its
 * progress.json has gone this long without a write (route writes it at every step).
 */
export const STALE_SECS = 24 * 60 * 60

/** The phases of a run, in the fixed order the view draws them. */
export const PHASES = ['Plan', 'Build', 'Review', 'Fix', 'Integrate'] as const

/** A run route is still working on: not finished, and its process is still there. */
export function isLive(m: UltraModel | null): boolean {
  return !!m && !m.finished && !m.stopped
}

/** Whether the status line shows the run: while it is live, then for RECENT_SECS after its last update. */
export function isShown(m: UltraModel | null): m is UltraModel {
  return !!m && (isLive(m) || (m.updated_secs ?? Infinity) < RECENT_SECS)
}

/** The run's state as the plugin says it: "fixing", or "stopped while fixing" once its process is gone. */
export function stateLabel(m: UltraModel): string {
  return m.stopped ? `stopped while ${m.state}` : m.state
}

// ---------------------------------------------------------------- numbers and time

/** "61k" → 61000, "1.2M" → 1200000, 850 → 850; anything unreadable is 0. */
export function parseTokens(v: unknown): number {
  if (typeof v === 'number') return Number.isFinite(v) ? v : 0
  if (typeof v !== 'string') return 0
  const m = /^\s*([\d.,]+)\s*([kKmM]?)/.exec(v)
  if (!m) return 0
  const n = parseFloat((m[1] ?? '').replace(/,/g, ''))
  const unit = (m[2] ?? '').toLowerCase()
  const scale = unit === 'k' ? 1e3 : unit === 'm' ? 1e6 : 1
  return Number.isFinite(n) ? Math.round(n * scale) : 0
}

/** Tokens the way ultra-progress writes them: 850, 61k, 1.2M. */
export function humanTokens(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`
  return String(n)
}

/** "34m 10s" → "34m", "1h 02m" → "1h 02m", "45s" → "45s". */
export function shortElapsed(s: string | undefined | null): string {
  const parts = (s ?? '').trim().split(/\s+/).filter(Boolean)
  const head = parts[0] ?? ''
  return head.endsWith('h') ? parts.slice(0, 2).join(' ') : head
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

// ---------------------------------------------------------------- the workflow view

export type View = { totals: UltraTotals; phases: UltraPhase[]; log: string[] | null }

const AGENT_STATE: Record<string, string> = {
  active: 'active', running: 'active', done: 'done', failed: 'failed', skipped: 'skipped',
  queued: 'queued', pending: 'queued', held: 'queued',
}

/** The word a row shows for an agent's state. */
export function stateWord(state: string): string {
  return state === 'active' ? 'running' : state
}

type Title = (typeof PHASES)[number]

function phaseOf(role: string): Title {
  if (/^planner/.test(role)) return 'Plan'
  if (/^review/.test(role)) return 'Review'
  if (/^fix/.test(role)) return 'Fix'
  if (/^(resolver|integrat)/.test(role)) return 'Integrate'
  return 'Build'
}

function cell(u: UltraUnit, stage: string): string {
  return u.cells?.find(c => c[0] === stage)?.[1] ?? 'pending'
}

function fromCard(a: UltraAgent, unit: string | null): UltraPhaseAgent {
  const tokens = parseTokens(a.tokens)
  return {
    label: unit ? `${unit} · ${a.role}` : a.role, unit, role: a.role.replace(/\s*#\d+$/, ''),
    model: a.model ?? null, state: AGENT_STATE[a.state] ?? a.state, span: a.span ?? '',
    tokens, tokens_h: tokens ? humanTokens(tokens) : '', tools: a.tools ?? 0,
    now: a.now ?? '', note: a.note ?? '', subs: a.subs ?? [],
  }
}

function waiting(unit: string, role: string, cellState: string): UltraPhaseAgent {
  return {
    label: `${unit} · ${role}`, unit, role, model: null, state: AGENT_STATE[cellState] ?? 'queued',
    span: '', tokens: 0, tokens_h: '', tools: 0, now: '', note: cellState === 'held' ? 'held: no model had quota' : '', subs: [],
  }
}

function normaliseAgent(a: Partial<UltraPhaseAgent>): UltraPhaseAgent {
  const tokens = typeof a.tokens === 'number' ? a.tokens : parseTokens(a.tokens_h)
  return {
    label: a.label ?? a.role ?? '?', unit: a.unit ?? null, role: a.role ?? '', model: a.model ?? null,
    state: AGENT_STATE[a.state ?? ''] ?? a.state ?? 'queued', span: a.span ?? '', tokens,
    tokens_h: a.tokens_h ?? (tokens ? humanTokens(tokens) : ''), tools: a.tools ?? 0,
    now: a.now ?? '', note: a.note ?? '', subs: Array.isArray(a.subs) ? a.subs : [],
  }
}

/**
 * A phase's progress the way ultra-progress counts it: agents done out of those that will run
 * (skipped ones left out). A failed agent is not progress.
 */
export function progressOf(agents: UltraPhaseAgent[]): { done: number; of: number; failed: number } {
  const counted = agents.filter(a => a.state !== 'skipped')
  return { done: counted.filter(a => a.state === 'done').length, of: counted.length, failed: counted.filter(a => a.state === 'failed').length }
}

/** "1/3 · 3k tokens · 1 failed", the way the newer ultra-progress sums a phase up. */
export function summarise(agents: UltraPhaseAgent[]): string {
  if (!agents.length) return ''
  const { done, of, failed } = progressOf(agents)
  const tokens = agents.reduce((n, a) => n + a.tokens, 0)
  return [of ? `${done}/${of}` : '', tokens ? `${humanTokens(tokens)} tokens` : '', failed ? `${failed} failed` : '']
    .filter(Boolean).join(' · ')
}

function phaseState(title: string, agents: UltraPhaseAgent[], m: UltraModel): string {
  const n = (s: string) => agents.filter(a => a.state === s).length
  if (n('active')) return 'active'
  if (!agents.length) {
    const stage = m.stages?.find(c => c[0] === title)?.[1]
    if (stage && ['active', 'done', 'failed', 'skipped'].includes(stage)) return stage
    return m.finished ? 'skipped' : 'pending'
  }
  if (n('queued')) return n('done') + n('failed') ? 'active' : 'pending'
  if (n('skipped') === agents.length) return 'skipped'
  if (n('failed') && !n('done')) return 'failed'
  return 'done'
}

/** The phases worked out from an older snapshot's planners and unit cards. */
export function derivePhases(m: UltraModel): UltraPhase[] {
  const by: Record<Title, UltraPhaseAgent[]> = { Plan: [], Build: [], Review: [], Fix: [], Integrate: [] }
  for (const a of m.planners ?? []) by.Plan.push(fromCard(a, null))
  for (const u of m.units ?? []) {
    const agents = (u.agents ?? []).map(a => fromCard(a, u.id))
    const reviewers = agents.filter(a => phaseOf(a.role) === 'Review')
    const last = reviewers[reviewers.length - 1]
    if (last && !last.note && cell(u, 'review') === 'done') {
      last.note = u.findings.length ? plural(u.findings.length, 'confirmed bug') : 'clean'
    }
    for (const a of agents) by[phaseOf(a.role)].push(a)
    if (m.finished) continue // nothing more will start
    const has = (p: Title) => agents.some(a => phaseOf(a.role) === p)
    const broken = cell(u, 'build') === 'failed' || cell(u, 'check') === 'failed'
    if (!has('Build') && cell(u, 'build') !== 'skipped') by.Build.push(waiting(u.id, 'worker', cell(u, 'build')))
    if (!has('Review') && !broken && cell(u, 'review') !== 'skipped') by.Review.push(waiting(u.id, 'reviewer', cell(u, 'review')))
    if (!has('Fix') && u.findings.length && !['skipped', 'done'].includes(cell(u, 'fix'))) {
      by.Fix.push(waiting(u.id, 'fixer', cell(u, 'fix')))
    }
  }
  return PHASES.map(title => ({ title, state: phaseState(title, by[title], m), summary: summarise(by[title]), agents: by[title] }))
}

/** Totals summed over the phases' agents (the ones that will run: skipped ones are left out). */
export function deriveTotals(phases: UltraPhase[], elapsed: string): UltraTotals {
  const all = phases.flatMap(p => p.agents).filter(a => a.state !== 'skipped')
  const n = (s: string) => all.filter(a => a.state === s).length
  const tokens = all.reduce((t, a) => t + a.tokens, 0)
  return {
    agents: all.length, running: n('active'), done: n('done'), queued: n('queued'), failed: n('failed'),
    tokens, tokens_h: humanTokens(tokens), tools: all.reduce((t, a) => t + a.tools, 0), elapsed,
  }
}

/** What ultra-progress notes on an agent that was still working when its run ended. */
const STOPPED_NOTE = 'stopped when the run ended'

/**
 * The phases of a run whose process is gone, the way ultra-progress draws a run that ended:
 * an agent still running then is failed, one still queued will never start.
 */
function asStopped(phases: UltraPhase[]): UltraPhase[] {
  return phases.map(p => {
    const agents = p.agents.map(a => a.state === 'active' ? { ...a, state: 'failed', now: '', note: STOPPED_NOTE }
      : a.state === 'queued' ? { ...a, state: 'skipped', note: 'not started' } : a)
    const state = p.state === 'active' ? 'failed' : p.state === 'pending' ? 'skipped' : p.state
    return { ...p, state, summary: summarise(agents), agents }
  })
}

/** The run as a workflow: from the snapshot's own `totals`, `phases` and `log`, or worked out. */
export function view(m: UltraModel): View {
  let phases = Array.isArray(m.phases) && m.phases.length
    ? m.phases.map(p => {
      const agents = (Array.isArray(p.agents) ? p.agents : []).map(normaliseAgent)
      return { title: p.title, state: p.state ?? 'pending', summary: p.summary ?? summarise(agents), agents }
    })
    : derivePhases(m)
  let totals = m.totals && typeof m.totals.agents === 'number'
    ? { ...deriveTotals(phases, m.elapsed), ...m.totals }
    : deriveTotals(phases, m.elapsed)
  if (m.stopped) {
    // Nothing runs any more, and ultra-progress's elapsed would keep counting to now.
    phases = asStopped(phases)
    totals = { ...deriveTotals(phases, ''), elapsed: '' }
  }
  if (!totals.tokens_h) totals.tokens_h = humanTokens(totals.tokens)
  if (!totals.elapsed && !m.stopped) totals.elapsed = m.elapsed
  const log = Array.isArray(m.log) ? m.log.filter((s): s is string => typeof s === 'string') : null
  return { totals, phases, log }
}

const STATE_PHASE: Record<string, string> = {
  starting: 'Plan', planning: 'Plan', assigning: 'Build', running: 'Build', reviewing: 'Review',
  fixing: 'Fix', integrating: 'Integrate', settling: 'Integrate',
}

/** The phase the run is in now: the one its state names, else the last one working. */
export function currentPhase(m: UltraModel, phases: UltraPhase[]): UltraPhase | undefined {
  const named = phases.find(p => p.title === STATE_PHASE[m.state])
  if (named && (named.state === 'active' || named.agents.length)) return named
  return phases.filter(p => p.state === 'active').pop()
}

/** "7 agents · 1 running · 182k tokens · 34m", the pane's header. */
export function headline(t: UltraTotals): string {
  return [plural(t.agents, 'agent'), t.running ? `${t.running} running` : '',
    t.failed ? `${t.failed} failed` : '', t.tokens ? `${t.tokens_h} tokens` : '', shortElapsed(t.elapsed)]
    .filter(Boolean).join(' · ')
}

/**
 * One line for the status bar, e.g. "ULTRA reviewing · 7 agents (1 running, 1 failed) · 182k tok · Review 2/3 · 34m".
 * A phase's count is its agents done, so a failed one is not progress; failures are counted with the agents.
 */
export function statusText(m: UltraModel | null): string | undefined {
  if (!isShown(m)) return undefined
  const { totals: t, phases } = view(m)
  const counts = [t.running ? `${t.running} running` : '', t.failed ? `${t.failed} failed` : ''].filter(Boolean).join(', ')
  const agents = t.agents ? plural(t.agents, 'agent') + (counts ? ` (${counts})` : '') : ''
  const tokens = t.tokens ? `${t.tokens_h} tok` : ''
  const time = shortElapsed(t.elapsed)
  if (!isLive(m)) {
    const merged = m.units.filter(u => u.state === 'merged').length
    const mark = m.state === 'done' && !m.stopped ? '✓' : '✗'
    const units = m.units.length ? `${merged}/${m.units.length} merged` : ''
    return [`ULTRA ${mark} ${stateLabel(m)}`, units, agents, tokens, time].filter(Boolean).join(' · ')
  }
  const p = currentPhase(m, phases)
  const { done, of } = progressOf(p?.agents ?? [])
  const phase = p && of ? `${p.title} ${done}/${of}` : p ? p.title : ''
  return [`ULTRA ${m.state}`, agents, tokens, phase, time].filter(Boolean).join(' · ')
}

// ---------------------------------------------------------------- toasts

/** A unit's state as the toasts care about it: its unit state plus whether its review is in. */
export function unitKey(u: UltraUnit): string {
  return `${u.state}|${cell(u, 'check')}|${cell(u, 'review')}|${u.findings.length}`
}

const FAILED_STATES = ['failed', 'conflict', 'unmerged', 'review failed']

/** The toasts a unit's change from `before` to now deserves, in the plugin's own words. */
function unitToasts(u: UltraUnit, before: string | undefined): string[] {
  const [state = '', check = '', review = ''] = unitKey(u).split('|')
  const [wasState = '', wasCheck = '', wasReview = ''] = (before ?? '').split('|')
  const out: string[] = []
  const nowFailing = state !== wasState && FAILED_STATES.includes(state)
  // A unit whose check failed goes straight to a fixer, and ultra-progress then draws its
  // check and review cells done: that is the fixer taking over (told as "fixing"), not a
  // passed check or a clean review.
  const fixerTookOver = wasCheck === 'failed'
  if (check === 'done' && wasCheck !== 'done' && !fixerTookOver) {
    out.push(`ULTRA ${u.id}: built and passed its check (${u.check || 'tests pass'})`)
  }
  // route marks a unit failed only once the whole swarm returns, which can be many minutes
  // after its check failed: the check cell turning failed is the first sign, so it is told then.
  if (check === 'failed' && wasCheck !== 'failed' && !nowFailing) out.push(`ULTRA ${u.id}: failed — ${u.note || 'check failed'}`)
  if (review === 'done' && wasReview !== 'done' && !fixerTookOver) {
    out.push(u.findings.length
      ? `ULTRA ${u.id}: review confirmed ${u.findings.length} bug(s) to fix — ${u.findings[0]}`
      : `ULTRA ${u.id}: review clean`)
  }
  if (state !== wasState) {
    if (state === 'fixing') out.push(`ULTRA ${u.id}: fixing (${u.note})`)
    if (state === 'fixed') out.push(`ULTRA ${u.id}: fixed — ${u.note}`)
    if (state === 'merged') out.push(`ULTRA ${u.id}: merged`)
    // ...and route's own "failed" for that same failed check is not told twice.
    const toldAtCheck = state === 'failed' && check === 'failed' && wasCheck === 'failed'
    if (nowFailing && !toldAtCheck) out.push(`ULTRA ${u.id}: ${state} — ${u.note}`)
    if (state === 'held') out.push(`ULTRA ${u.id}: held — ${u.note}`)
  }
  return out
}

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** Whether a narrator line names this unit (as a whole word, so "api" is not "api-tests"). */
export function mentions(line: string, unit: string): boolean {
  return new RegExp(`(^|[^\\w-])${escape(unit)}($|[^\\w-])`).test(line)
}

/**
 * Whether a narrator line is the run's last: "run finished: 3/3 merged on ultra/x",
 * "run failed: 1/3 merged (exit 1)", "plan only: 2 units planned, nothing built". Matched on
 * that whole shape, so a unit's own line ("run-tests merged", "run failed: check failed"
 * for a unit called "run") never reads as the run's.
 */
export function isRunEnd(line: string): boolean {
  return /^run [a-z]+: \d+\/\d+ merged\b/i.test(line) || /^plan only: \d+ units? planned\b/i.test(line)
}

/**
 * The toasts for a new snapshot, and what to remember.
 *
 * The events are the same as ever: a unit built and checked, its check failed, reviewed,
 * fixing, fixed, merged, failed or held, and the run's end (finished, or stopped when its
 * process went). Where the snapshot carries narrator lines, a new line naming the unit
 * (or, for the run's end, the narrator's closing line, see isRunEnd) is toasted in place of
 * the plugin's own wording; a new line naming no unit (the planner's split, say) is toasted
 * too. Nothing is toasted on the first snapshot of a run, and the first snapshot that
 * carries a log only records its lines, so a reload, a late start or an upgraded
 * ultra-progress never floods. A run no longer shown is still told once if it was last seen
 * running, so a run that ended or died while nobody was polling gets its end toast. Remembered
 * keys of other runs are dropped.
 */
export function diff(seen: Record<string, string>, m: UltraModel | null): { toasts: string[]; seen: Record<string, string> } {
  if (!m) return { toasts: [], seen }
  const prefix = `${m.run}:`
  if (!isShown(m) && seen[`${prefix}__run`] !== 'running') return { toasts: [], seen }
  const mine = Object.keys(seen).filter(k => k.startsWith(prefix))
  const first = !mine.length
  const next: Record<string, string> = {}
  for (const k of mine) next[k] = seen[k] ?? ''

  const events = new Map<string, string[]>()
  for (const u of m.units) {
    const key = prefix + u.id
    const before = seen[key]
    next[key] = unitKey(u)
    if (first || before === next[key]) continue
    const texts = unitToasts(u, before)
    if (texts.length) events.set(u.id, texts)
  }

  const runKey = `${prefix}__run`
  const runNow = m.finished ? `finished:${m.state}` : m.stopped ? 'stopped' : 'running'
  const runEnded = !first && seen[runKey] !== runNow && !isLive(m)
  next[runKey] = runNow

  const log = Array.isArray(m.log) ? m.log : null
  const logKey = `${prefix}__log`
  const firstLog = !!log && !(logKey in seen)
  if (log) next[logKey] = '1'
  const fresh: string[] = []
  for (const line of log ?? []) {
    const key = `${prefix}log:${line}`
    if (!(key in seen) && !firstLog) fresh.push(line)
    next[key] = '1'
  }
  if (first) return { toasts: [], seen: next }

  const toasts: string[] = []
  const told = new Set<string>()
  let runTold = false
  for (const line of fresh) {
    if (isRunEnd(line)) {
      if (runEnded && m.finished) {
        runTold = true
        toasts.push(`ULTRA ${line}`)
      }
      continue
    }
    const units = m.units.filter(u => mentions(line, u.id)).map(u => u.id)
    const forEvent = units.filter(id => events.has(id))
    if (forEvent.length) forEvent.forEach(id => told.add(id))
    else if (units.length) continue // names a unit, but nothing the toasts follow happened to it
    toasts.push(`ULTRA ${line}`)
  }
  for (const [id, texts] of events) if (!told.has(id)) toasts.push(...texts)
  if (runEnded && !runTold) {
    const merged = `${m.units.filter(u => u.state === 'merged').length}/${m.units.length} units merged`
    toasts.push(m.stopped
      ? `ULTRA ${stateLabel(m)}: its route process is gone (killed, crashed or a restart), ${merged}`
      : `ULTRA ${m.state === 'done' ? 'finished' : m.state}: ${merged}` +
        (m.branch ? ` on ${m.branch}` : '') + (m.exit_code !== null ? ` (exit ${m.exit_code})` : ''))
  }
  return { toasts, seen: next }
}

// ---------------------------------------------------------------- one poll

/**
 * Finds and runs `ultra-progress --json`, a uv script. Where it looks, in order:
 *
 * 1. PATH, with ~/.local/bin and Homebrew's bin added, since a GUI-started session has a thin PATH.
 * 2. The route-skill checkout the plugin runs from in place (a local marketplace, --plugin-dir).
 * 3. The plugin's own scripts/ultra_progress.py, a link to the checkout's copy. Installed from
 *    GitHub, the plugin runs from a copy under the plugins cache, and Claude Code copies a
 *    link's target into that copy when the target is elsewhere in the same marketplace repo.
 * 4. The marketplace's clone, under the plugins root: CLAUDE_CODE_PLUGIN_CACHE_DIR, else
 *    CLAUDE_CONFIG_DIR/plugins (a second config such as claude-work), else ~/.claude/plugins.
 *
 * UV_OFFLINE keeps a poll to the cached `rich`, so it never waits on the network; `online`
 * lifts it once, for a machine whose cache does not have rich yet. $1 is the plugin's folder,
 * $2 the mode.
 */
export const FIND = [
  'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"',
  '[ "$2" = online ] || export UV_OFFLINE=1',
  'plugins="${CLAUDE_CODE_PLUGIN_CACHE_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins}"',
  'for c in "$(command -v ultra-progress 2>/dev/null)" "$1/../../skill-ultra-progress/ultra_progress.py" \\',
  '    "$1/scripts/ultra_progress.py" "$plugins/marketplaces/route-skill/skill-ultra-progress/ultra_progress.py"; do',
  '  [ -n "$c" ] && [ -f "$c" ] || continue',
  '  [ -x "$c" ] && exec "$c" --json',
  '  exec uv run --quiet --script "$c" --json',
  'done',
  'echo "ultra-progress: not found" >&2',
  'exit 127',
].join('\n')

/** The argv of one poll: the plugin's folder and whether uv may fetch. */
export function pollArgv(root: string, online: boolean): string[] {
  return ['/bin/sh', '-c', FIND, 'ultra-watch', root, online ? 'online' : 'offline']
}

/** Asks for one process's state and age; prints nothing (exit 1) when there is no such process. */
export const PS = 'PATH="/bin:/usr/bin:$PATH" exec ps -o stat= -o etime= -p "$1"'

export function psArgv(pid: number): string[] {
  return ['/bin/sh', '-c', PS, 'ultra-watch', String(pid)]
}

/** Where route keeps a run's progress.json, found the way ultra-progress finds it; null when it cannot be. */
export function progressPath(env: { HOME?: string; ROUTE_STATE_DIR?: string }, run: string): string | null {
  if (!/^[\w.-]+$/.test(run) || run.startsWith('.')) return null
  const home = env.HOME ?? ''
  let dir = env.ROUTE_STATE_DIR || (home ? `${home}/.local/state/route` : '')
  if (dir === '~' || dir.startsWith('~/')) dir = home ? home + dir.slice(1) : ''
  return dir ? `${dir.replace(/\/+$/, '')}/ultra/${run}/progress.json` : null
}

/** "2026-11-06T22:00:00.123456Z" (or with an offset) → ms since the epoch; null when unreadable. */
export function parseIso(s: string): number | null {
  const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)$/.exec(s.trim())
  if (!m) return null
  const [, y, mo, d, h, mi, se, zone = 'Z'] = m
  const offset = zone === 'Z' ? 0 : (zone.startsWith('-') ? -1 : 1) * (Number(zone.slice(1, 3)) * 60 + Number(zone.slice(4, 6)))
  return Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(se)) - offset * 60_000
}

/** The route process a run's progress.json names, and when the run started (ms); null when it names none. */
export function runProcess(text: string): { pid: number; startedMs: number | null } | null {
  try {
    const p = JSON.parse(text) as { pid?: unknown; started_at?: unknown }
    if (typeof p.pid !== 'number' || !Number.isInteger(p.pid) || p.pid <= 0) return null
    return { pid: p.pid, startedMs: typeof p.started_at === 'string' ? parseIso(p.started_at) : null }
  } catch {
    return null
  }
}

/** ps's elapsed time, "[[dd-]hh:]mm:ss", in seconds; null when unreadable. */
export function etimeSecs(s: string): number | null {
  const m = /^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$/.exec(s.trim())
  if (!m) return null
  const [, d = '0', h = '0', mi = '0', se = '0'] = m
  return ((Number(d) * 24 + Number(h)) * 60 + Number(mi)) * 60 + Number(se)
}

/**
 * Whether a run's route process is gone, from `ps -o stat= -o etime= -p <pid>`: no such
 * process, a zombie (what route's own `ultra status` checks), or a process younger than the
 * run, which is another process that got the pid after a restart. `runSecs` is how long ago
 * the run started. Anything unclear (ps missing or failing) is null: not known.
 */
export function goneFromPs(r: { exitCode: number; stdout: string }, runSecs: number | null): boolean | null {
  const out = r.stdout.trim()
  if (!out) return r.exitCode === 1 ? true : null
  const [stat = '', etime = ''] = out.split(/\s+/)
  if (stat.startsWith('Z')) return true
  const age = etimeSecs(etime)
  return age !== null && runSecs !== null && age + 120 < runSecs
}

/**
 * Whether an unfinished run is stopped: its process is gone, or, when that cannot be
 * checked (`gone` null), it has not written its progress for STALE_SECS.
 */
export function isStopped(m: UltraModel, gone: boolean | null): boolean {
  if (m.finished) return false
  return gone ?? (m.updated_secs ?? 0) > STALE_SECS
}

// ---------------------------------------------------------------- one poll's result

export const MISSING_TOOL = 'ultra-progress is not installed. It ships with route-skill: link ' +
  'skill-ultra-progress/ultra_progress.py into ~/.local/bin as ultra-progress (see the route-skill README).'
export const MISSING_UV = 'ultra-progress needs uv on PATH: https://docs.astral.sh/uv/'
export const NO_RICH = 'ultra-progress could not fetch its one dependency (rich). Run `ultra-progress --once` ' +
  'in a terminal once, then it works offline.'

export type Polled =
  | { kind: 'run'; model: UltraModel }
  | { kind: 'none' }
  | { kind: 'offline' }
  | { kind: 'missing'; why: string }
  | { kind: 'error'; why: string }

/** What one run of `ultra-progress --json` came to. */
export function classify(r: { exitCode: number; stdout: string; stderr: string }): Polled {
  const out = r.stdout.trim()
  if (r.exitCode === 0 && out.startsWith('{')) {
    try {
      return { kind: 'run', model: JSON.parse(out) as UltraModel }
    } catch {
      return { kind: 'error', why: 'its output was cut short' }
    }
  }
  const said = `${r.stderr}\n${r.stdout}`
  if (/no \/ultra runs/.test(said)) return { kind: 'none' }
  if (/network (was|is) disabled|not found in the cache|--offline/i.test(said)) return { kind: 'offline' }
  if (r.exitCode === 127) return { kind: 'missing', why: /ultra-progress: not found/.test(said) ? MISSING_TOOL : MISSING_UV }
  const line = said.split('\n').map(s => s.trim()).find(Boolean) ?? `exit ${r.exitCode}`
  return { kind: 'error', why: line.slice(0, 200) }
}

// ---------------------------------------------------------------- layout

/**
 * Column widths for the agent rows, sized to the widest value and the room there is.
 * Below 80 columns the model column goes (width 0), so the labels keep their words.
 */
export function columns(agents: UltraPhaseAgent[], width: number): { label: number; model: number } {
  const widest = (xs: string[]) => xs.reduce((n, s) => Math.max(n, s.length), 0)
  const wide = width >= 80
  const model = wide ? Math.min(14, widest(agents.map(a => a.model ?? '—'))) : 0
  // "  ✓ " (4), label, "  model", "  state" (7), "  span" (7), "  tokens" (5), then the note
  const fixed = 4 + (model ? 2 + model : 0) + 2 + 7 + 2 + 7 + 2 + 5
  const label = Math.max(12, Math.min(widest(agents.map(a => a.label)), width - fixed - (wide ? 12 : 4), 40))
  return { label, model }
}

/** Sub-agents drawn under one agent before the rest are counted on one row. */
export const MAX_SUBS = 3

/** The rows a phase takes drawn open: its own, then each agent's, its "now" and its sub-agents. */
export function rowsOf(p: UltraPhase): number {
  return 1 + p.agents.reduce((n, a) => n + 1 + (a.state === 'active' && a.now ? 1 : 0) +
    Math.min(a.subs.length, MAX_SUBS) + (a.subs.length > MAX_SUBS ? 1 : 0), 0)
}

/**
 * The phases to draw folded to their one summary row so the tree fits `room` rows:
 * done ones first, then skipped, pending and failed ones; the phase at work never.
 */
export function folded(phases: UltraPhase[], room: number): Set<string> {
  const out = new Set<string>()
  let total = phases.reduce((n, p) => n + rowsOf(p), 0)
  for (const state of ['done', 'skipped', 'pending', 'failed']) {
    for (const p of phases) {
      if (total <= room) return out
      if (p.state === state && p.agents.length) {
        out.add(p.title)
        total -= rowsOf(p) - 1
      }
    }
  }
  return out
}
