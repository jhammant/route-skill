// ultra-watch: /ultra runs inside Claude Code, the way ultracode workflows are shown.
// Polls `ultra-progress --json` (route-skill), keeps a status-line entry, raises a toast
// when a unit passes or fails its check, is reviewed, fixed or merged and when the run ends
// (or its route process is found gone), and draws a live workflow pane on /ultra-watch: a
// header with the run's agents, tokens and time, one group per phase with a row per agent,
// the confirmed bugs, and the narrator log.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { UltraModel, UltraPhase, UltraPhaseAgent } from '../types'
import {
  classify, columns, diff, folded, goneFromPs, headline, isLive, isShown, isStopped, MAX_SUBS, NO_RICH, pollArgv, progressPath,
  psArgv, runProcess, stateLabel, statusText, stateWord, view,
} from './logic'

const PANE = 'ultra-watch'
const POLL_MS = 4000
/** With no live run, poll every this many ticks (20 s) instead of every one. */
const IDLE_EVERY = 5
/** Failed polls in a row before the pane says why. */
const ERRORS_SHOWN = 3

const model = atom({ plugin: 'ultra-watch', key: 'model' } as const, null as UltraModel | null)
const seen = atom({ plugin: 'ultra-watch', key: 'seen' } as const, {} as Record<string, string>)
const problem = atom({ plugin: 'ultra-watch', key: 'problem' } as const, null as string | null)

const GLYPH: Record<string, string> = { done: '✓', active: '●', pending: '○', queued: '○', failed: '✗', skipped: '·', held: '⏸' }
const COLOUR: Record<string, string> = {
  done: 'success', active: 'warning', pending: 'inactive', queued: 'inactive', failed: 'error', skipped: 'inactive', held: 'remember',
}

/**
 * Whether route's process for an unfinished run is gone: the pid in the run's progress.json
 * no longer runs, is a zombie, or now belongs to a younger process. Null when that cannot be
 * told (no progress.json where ultra-progress would look, no pid, ps failing).
 */
async function processGone($: EngineInterface, run: string): Promise<boolean | null> {
  try {
    const path = progressPath({ HOME: await $.env.get('HOME'), ROUTE_STATE_DIR: await $.env.get('ROUTE_STATE_DIR') }, run)
    if (!path) return null
    const proc = runProcess(await $.fs.read(path))
    if (!proc) return null
    const r = await $.process.run(psArgv(proc.pid), { timeoutMs: 5000 })
    const runSecs = proc.startedMs === null ? null : ((await $.clock.now()) - proc.startedMs) / 1000
    return goneFromPs(r, runSecs)
  } catch {
    return null
  }
}

// The poller's own bookkeeping; a reload starts it over, which is harmless.
let busy = false
let online = false
let live = false
let ticks = 0
let errors = 0

/** The on/off switch lives in $.store; anywhere without a store (tests) counts as on. */
async function isOn($: EngineInterface): Promise<boolean> {
  try {
    return (await $.store.get('enabled')) !== false
  } catch {
    return true
  }
}

async function setOn($: EngineInterface, on: boolean): Promise<void> {
  try {
    await $.store.set('enabled', on)
  } catch {
    // No store here: the switch lasts for this load only.
  }
}

async function poll($: EngineInterface): Promise<void> {
  if (busy) return
  if (!(await isOn($))) return
  busy = true
  try {
    const r = await $.process.run(pollArgv($.plugin.root, online), { timeoutMs: 30_000 })
    const got = classify(r)
    if (got.kind === 'offline' || (online && got.kind === 'error')) {
      // Fetch rich once with the network; if that fails too, say so and keep trying slowly.
      if (online) await update($, problem, () => NO_RICH)
      online = true
      return
    }
    if (got.kind === 'error') {
      // A run being written mid-read: keep what we had and try again next tick.
      if (++errors >= ERRORS_SHOWN) await update($, problem, () => `ultra-progress failed: ${got.why}`)
      return
    }
    errors = 0
    online = false
    let m = got.kind === 'run' ? got.model : null
    // ultra-progress only knows what route last wrote: a run route was killed in, or that a
    // crash or a restart cut off, still reads as running. Check route's process is there.
    if (m && !m.finished && isStopped(m, await processGone($, m.run))) m = { ...m, stopped: true }
    await update($, problem, () => (got.kind === 'missing' ? got.why : null))
    await update($, model, () => m)
    live = isLive(m)
    await $.ui.status(statusText(m))
    let toasts: string[] = []
    await update($, seen, s => {
      const d = diff(s ?? {}, m)
      toasts = d.toasts
      return d.seen
    })
    for (const text of toasts) await $.ui.toast(text)
  } catch {
    // The process could not be started or the state write raced: next tick.
  } finally {
    busy = false
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'ultra-watch',
      description: 'Watch the latest /ultra run live in a pane, like a workflow; "off" stops polling, the status line and toasts, "on" turns them back on',
      argumentHint: '[on|off]',
    })
    void poll($)
    $.clock.every(POLL_MS, () => {
      ticks += 1
      if (live || ticks % IDLE_EVERY === 0) void poll($)
    })
    return next(e)
  })

  on('command.run', { command: 'ultra-watch' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    if (arg === 'off') {
      await setOn($, false)
      await $.ui.status(undefined)
      await $.ui.close({ id: PANE })
      return { text: 'ultra-watch is off: no polling, status line or toasts. Turn it back on with /ultra-watch on.' }
    }
    if (arg === 'on') {
      await setOn($, true)
      void poll($)
      return { text: 'ultra-watch is on: status line and toasts are back; /ultra-watch opens the pane.' }
    }
    if (!(await isOn($))) return { text: 'ultra-watch is off. Turn it on with /ultra-watch on.' }
    await $.ui.open({ id: PANE, title: 'ULTRA' })
    await poll($)
    const m = await read($, model)
    if (isShown(m)) return { text: `Watching /ultra run ${m.run} (${stateLabel(m)}).` }
    const why = await read($, problem)
    return { text: why ?? 'No /ultra run right now; the pane fills in when one starts.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const m = await read($, model)
    const why = await read($, problem)
    const width = Math.max(40, e.props.bodyColumns ?? e.viewport?.columns ?? 100)
    const cut = (s: string, n: number) => (s.length > n ? s.slice(0, Math.max(0, n - 1)) + '…' : s)
    if (!m) {
      return (
        <Box flexDirection="column" width={width}>
          <Text bold color="claude">ULTRA</Text>
          <Text dimColor wrap="wrap">{why ?? 'No /ultra run found. Start one with /ultra; this pane fills in as it goes.'}</Text>
        </Box>
      )
    }

    const { totals, phases, log } = view(m)
    const agents = phases.flatMap(p => p.agents)
    const col = columns(agents, width)
    const label = stateLabel(m)
    const stateColour = m.stopped ? 'error' : ['done', 'planned'].includes(m.state) ? 'success' : m.finished ? 'error' : 'warning'
    const right = `${label}  ${headline(totals)}`
    const gone = m.stopped ? 'route is no longer running this run: it was killed, crashed or the machine restarted.' : ''
    const findings = m.units.filter(u => u.findings.length)
    const unitWidth = Math.min(24, findings.reduce((n, u) => Math.max(n, u.id.length), 0))
    const tail = (log ?? []).slice(-5)
    const feed = log === null ? m.activity.slice(0, 4) : []
    const footer = (m.branch ? 1 : 0) + (m.summary ? 1 : 0) + (why ? 1 : 0) + (gone ? 1 : 0)
    const findingRows = findings.length ? 1 + findings.reduce((n, u) => n + u.findings.length, 0) : 0
    const below = findingRows + (tail.length || feed.length ? 1 + tail.length + feed.length : 0) + footer
    const room = (e.props.scroll?.bodyRows ?? e.viewport?.rows ?? 40) - 3 - below
    const fold = folded(phases, room)

    const agentRow = (a: UltraPhaseAgent) => {
      const st = a.state
      const subs = [...a.subs].sort((x, y) => Number(y.live) - Number(x.live))
      return (
        <Box flexDirection="column">
          <Text wrap="truncate-end">
            <Text>{'  '}</Text>
            <Text color={COLOUR[st] ?? 'inactive'}>{`${GLYPH[st] ?? '?'} `}</Text>
            <Text bold={st === 'active'} dimColor={st === 'queued' || st === 'skipped'}>{cut(a.label, col.label).padEnd(col.label)}</Text>
            {col.model ? <Text color={a.model ? 'suggestion' : 'inactive'}>{`  ${cut(a.model ?? '—', col.model).padEnd(col.model)}`}</Text> : ''}
            <Text color={COLOUR[st] ?? 'inactive'}>{`  ${stateWord(st).padEnd(7)}`}</Text>
            <Text dimColor>{`  ${a.span.padStart(7)}  ${a.tokens_h.padStart(5)}`}</Text>
            {a.note ? <Text color={/bug|fail|conflict/i.test(a.note) ? 'error' : 'inactive'}>{`  ${a.note}`}</Text> : ''}
          </Text>
          {st === 'active' && a.now ? (
            <Text wrap="truncate-end">
              <Text dimColor>{'      ⎿ '}</Text>
              <Text color="warning">{a.now}</Text>
            </Text>
          ) : ''}
          {subs.slice(0, MAX_SUBS).map(s => (
            <Text wrap="truncate-end">
              <Text dimColor>{'      ↳ '}</Text>
              <Text color={s.live ? 'warning' : 'success'}>{s.live ? '● ' : '✓ '}</Text>
              <Text>{`sub-agent ${s.prompt}`}</Text>
              <Text dimColor>{`  ${s.span} · ${s.tools} tools`}</Text>
            </Text>
          ))}
          {subs.length > MAX_SUBS ? <Text dimColor>{`      ↳ ${subs.length - MAX_SUBS} more sub-agents`}</Text> : ''}
        </Box>
      )
    }

    const phaseGroup = (p: UltraPhase) => {
      const quiet = p.state === 'pending' || p.state === 'skipped'
      const said = p.summary || (p.agents.length ? '' : p.state === 'skipped' ? 'skipped' : p.state === 'pending' ? 'not started' : '')
      return (
        <Box flexDirection="column">
          <Text wrap="truncate-end">
            <Text color={COLOUR[p.state] ?? 'inactive'}>{`${GLYPH[p.state] ?? '?'} `}</Text>
            <Text bold={!quiet} dimColor={quiet}>{p.title.padEnd(10)}</Text>
            <Text dimColor>{said}</Text>
            {fold.has(p.title) ? <Text dimColor>{'  (folded)'}</Text> : ''}
          </Text>
          {fold.has(p.title) ? '' : p.agents.map(agentRow)}
        </Box>
      )
    }

    return (
      <Box flexDirection="column" width={width}>
        <Text wrap="truncate-end">
          <Text bold color="claude">ULTRA </Text>
          <Text bold>{cut(m.run, Math.max(12, width - right.length - 8)).padEnd(Math.max(0, width - right.length - 8))}</Text>
          <Text>{'  '}</Text>
          <Text bold color={stateColour}>{label}</Text>
          <Text dimColor>{`  ${headline(totals)}`}</Text>
        </Text>
        <Text dimColor wrap="truncate-end">{m.goal}</Text>
        <Box flexDirection="column" marginTop={1}>
          {phases.map(phaseGroup)}
        </Box>
        {findings.length ? (
          <Box flexDirection="column" marginTop={1}>
            <Text bold>confirmed bugs</Text>
            {findings.flatMap(u => {
              const fixed = u.state === 'fixed' || u.state === 'merged'
              return u.findings.map((f, i) => (
                <Text wrap="truncate-end">
                  <Text color={fixed ? 'success' : 'error'}>{fixed ? '  ✓ ' : '  • '}</Text>
                  <Text color="suggestion">{(i ? '' : cut(u.id, unitWidth)).padEnd(unitWidth)}</Text>
                  <Text dimColor={fixed}>{`  ${f}`}</Text>
                </Text>
              ))
            })}
          </Box>
        ) : ''}
        {tail.length ? (
          <Box flexDirection="column" marginTop={1}>
            {tail.map((line, i) => (
              <Text wrap="truncate-end" dimColor={i < tail.length - 1}>{`› ${line}`}</Text>
            ))}
          </Box>
        ) : ''}
        {feed.length ? (
          <Box flexDirection="column" marginTop={1}>
            <Text bold>activity</Text>
            {feed.map(([t, who, role, tool, desc]) => (
              <Text wrap="truncate-end">
                <Text dimColor>{`${t}  `}</Text>
                <Text color="suggestion">{`${who} · ${role}  `}</Text>
                <Text bold>{`${tool} `}</Text>
                <Text dimColor>{desc}</Text>
              </Text>
            ))}
          </Box>
        ) : ''}
        {m.branch ? <Text wrap="truncate-end">{`branch   ${m.branch}`}</Text> : ''}
        {m.summary ? <Text dimColor wrap="truncate-end">{`summary  ${m.summary}`}</Text> : ''}
        {gone ? <Text color="error" wrap="truncate-end">{gone}</Text> : ''}
        {why ? <Text color="error" wrap="truncate-end">{why}</Text> : ''}
      </Box>
    )
  })
}
