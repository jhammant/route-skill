import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import type { UltraAgent, UltraModel, UltraPhaseAgent } from '../types'
import { PS } from './logic'

const SURFACES = ['terminal', 'desktop'] as const

function agent(label: string, state: string, tokens: number, extra: Partial<UltraPhaseAgent> = {}): UltraPhaseAgent {
  const [unit, role] = label.includes(' · ') ? label.split(' · ') : [null, label]
  return { label, unit: unit ?? null, role: role ?? label, model: 'fable · work', state, span: '5m 37s', tokens,
    tokens_h: `${Math.round(tokens / 1000)}k`, tools: 14, now: '', note: '', subs: [], ...extra }
}

function card(role: string, state: string, tokens: string, extra: Partial<UltraAgent> = {}): UltraAgent {
  return { role, model: 'fable · work', state, span: '9m 44s', tools: 30, tokens, subs: [], ...extra }
}

const BASE: UltraModel = {
  run: 'finish-the-data-layer-20261106224445', state: 'fixing', step: 'finance-history pass 1 by claude-work-fable',
  finished: false, exit_code: null, elapsed: '47m 49s', updated_secs: 5, goal: 'Finish the data layer.',
  stages: [['Plan', 'done'], ['Build', 'done'], ['Review', 'done'], ['Integrate', 'pending']],
  planners: [card('planner #1', 'done', '11k')],
  units: [
    { id: 'finance-history', tier: 'hard', state: 'fixing', note: 'fix pass 1', check: '24 passed in 54.31s',
      findings: ['Historical AAR reserves are double-counted'],
      cells: [['build', 'done'], ['check', 'done'], ['review', 'done'], ['fix', 'active'], ['merge', 'pending']],
      agents: [card('worker', 'done', '61k'), card('reviewer', 'done', '12k', { model: 'codex' }),
        card('fixer #1', 'active', '12k', { now: 'Bash Regenerate jsonl and report' })] },
    { id: 'pta-matching', tier: 'hard', state: 'accepted', note: '', check: '23 passed in 4.09s', findings: [],
      cells: [['build', 'done'], ['check', 'done'], ['review', 'done'], ['fix', 'skipped'], ['merge', 'pending']],
      agents: [card('worker', 'done', '51k'), card('reviewer', 'done', '10k', { model: 'codex' })] },
  ],
  activity: [['23:32:31', 'finance-history', 'fixer #1', 'Bash', 'Regenerate jsonl and report']],
  messages: [], headroom: {}, branch: null, summary: null,
}

const NEWER: UltraModel = {
  ...BASE,
  totals: { agents: 6, running: 1, done: 5, queued: 0, failed: 0, tokens: 157_000, tokens_h: '157k', tools: 120, elapsed: '47m 49s' },
  phases: [
    { title: 'Plan', state: 'done', summary: '1/1 · 11k tokens', agents: [agent('planner #1', 'done', 11_000)] },
    { title: 'Build', state: 'done', summary: '2/2 · 112k tokens', agents: [
      agent('finance-history · worker', 'done', 61_000), agent('pta-matching · worker', 'done', 51_000)] },
    { title: 'Review', state: 'done', summary: '2/2 · 22k tokens', agents: [
      agent('finance-history · reviewer', 'done', 12_000, { model: 'codex', note: '1 confirmed bug' }),
      agent('pta-matching · reviewer', 'done', 10_000, { model: 'codex', note: 'clean' })] },
    { title: 'Fix', state: 'active', summary: '0/1 · 12k tokens', agents: [
      agent('finance-history · fixer', 'active', 12_000, { now: 'exec python3 -m pytest -q', subs: [
        { prompt: 'check URN 136807 reserves', live: true, span: '0m 40s', tools: 3 }] })] },
    { title: 'Integrate', state: 'pending', summary: '', agents: [] },
  ],
  log: ['planner split the goal into 2 units', 'pta-matching review clean', 'finance-history fix pass 1 by fable · work'],
}

type Said = { status: (string | undefined)[]; toasts: string[]; polls: number }

/** What `ps -o stat= -o etime= -p <pid>` says of route's process: alive since before the run, or gone. */
const ALIVE = { exitCode: 0, stdout: 'Ss   02:00:00\n' }
const GONE = { exitCode: 1, stdout: '' }

/**
 * The world beneath the plugin: ultra-progress answering `snapshot`, route's progress.json
 * (pid 4242, started an hour before the clock) under HOME=/h, ps answering `ps`, and the
 * display calls and polls recorded.
 */
function world(on: On, snapshot: UltraModel | null, said: Said, ps: { exitCode: number; stdout: string } = ALIVE) {
  const now = Date.UTC(2026, 10, 7, 0, 0, 0)
  const clock = mock.clock(on, { now })
  on('process.run', async ($, e) => {
    const isPs = e.argv[2] === PS
    if (!isPs) said.polls += 1
    const r = isPs ? { ...ps, stderr: '' } : {
      exitCode: snapshot ? 0 : 1, stdout: snapshot ? JSON.stringify(snapshot) : '',
      stderr: snapshot ? '' : 'ultra-progress: no /ultra runs under /x/ultra',
    }
    return { value: { ...r, isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('env.get', async ($, e) => ({ value: e.name === 'HOME' ? '/h' : undefined }))
  on('fs.read', async ($, e) => {
    if (!snapshot || e.path !== `/h/.local/state/route/ultra/${snapshot.run}/progress.json`) return { deny: 'no such file' }
    return { value: JSON.stringify({ pid: 4242, state: snapshot.state, started_at: new Date(now - 3600_000).toISOString() }) }
  })
  on('command.register', async ($, e) => ({ value: { command: e.name } }))
  on('ui.status', async ($, e) => {
    said.status.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', async ($, e) => {
    said.toasts.push(e.text)
    return { value: undefined }
  })
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('ui.open', async () => ({ value: { isPlaced: true as const } }))
  return clock
}

for (const surface of SURFACES) {
  test(`the pane draws a newer run as a workflow on ${surface}`, async ($, on) => {
    // Arrange
    const said: Said = { status: [], toasts: [], polls: 0 }
    const clock = world(on, NEWER, said)
    // Act
    await $.session.start({ cwd: '/tmp', surface, isInteractive: true })
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'ultra-watch', surface, component: 'Pane', requestId: 'ultra-watch',
      viewport: { columns: 100, rows: 40 },
      props: { title: 'ULTRA', isFocused: false, bodyColumns: 100, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} } })
    // Assert
    expect(said.status.at(-1)).toBe('ULTRA fixing · 6 agents (1 running) · 157k tok · Fix 0/1 · 47m')
    expect(said.toasts).toEqual([])
    expect(await ui.find({ text: /6 agents · 1 running · 157k tokens · 47m/ })).toBeDefined()
    expect(await ui.find({ text: /finance-history · fixer/ })).toBeDefined()
    expect(await ui.find({ text: 'exec python3 -m pytest -q' })).toBeDefined()
    expect(await ui.find({ text: /sub-agent check URN 136807 reserves/ })).toBeDefined()
    expect(await ui.find({ text: /Historical AAR reserves are double-counted/ })).toBeDefined()
    expect(await ui.find({ text: /› finance-history fix pass 1 by fable · work/ })).toBeDefined()
  })

  test(`the pane works the workflow out of an older run on ${surface}`, async ($, on) => {
    // Arrange: no totals, phases or log
    const said: Said = { status: [], toasts: [], polls: 0 }
    const clock = world(on, BASE, said)
    // Act
    await $.session.start({ cwd: '/tmp', surface, isInteractive: true })
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'ultra-watch', surface, component: 'Pane', requestId: 'ultra-watch',
      viewport: { columns: 100, rows: 40 },
      props: { title: 'ULTRA', isFocused: false, bodyColumns: 100, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} } })
    // Assert: 6 agents worked out (planner, 2 workers, 2 reviewers, 1 fixer), the activity feed stands in for the log
    expect(said.status.at(-1)).toBe('ULTRA fixing · 6 agents (1 running) · 157k tok · Fix 0/1 · 47m')
    expect(await ui.find({ text: /pta-matching · reviewer/ })).toBeDefined()
    expect(await ui.find({ text: /clean/ })).toBeDefined()
    expect(await ui.find({ text: 'activity' })).toBeDefined()
  })

  test(`with no run the pane says how to start one on ${surface}`, async ($, on) => {
    // Arrange
    const said: Said = { status: [], toasts: [], polls: 0 }
    const clock = world(on, null, said)
    // Act
    await $.session.start({ cwd: '/tmp', surface, isInteractive: true })
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'ultra-watch', surface, component: 'Pane', requestId: 'ultra-watch',
      viewport: { columns: 100, rows: 40 },
      props: { title: 'ULTRA', isFocused: false, bodyColumns: 100, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} } })
    // Assert
    expect(said.status.at(-1)).toBe(undefined)
    expect(await ui.find({ text: /No \/ultra run found/ })).toBeDefined()
  })
}

test('/ultra-watch opens the pane and names the run it is watching', async ($, on) => {
  // Arrange
  const said: Said = { status: [], toasts: [], polls: 0 }
  const clock = world(on, NEWER, said)
  await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
  await clock.settle()
  // Act
  const result = await $.command.run({ command: 'ultra-watch', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 160 } })
  // Assert
  expect(result.text).toBe('Watching /ultra run finish-the-data-layer-20261106224445 (fixing).')
})

test('/ultra-watch says when ultra-progress is not installed', async ($, on) => {
  // Arrange: the shell finds no ultra-progress anywhere
  const said = { status: [] as (string | undefined)[], toasts: [] as string[] }
  mock.clock(on)
  on('process.run', async () => ({
    value: { exitCode: 127, stdout: '', stderr: 'ultra-progress: not found', isStdoutTruncated: false, isStderrTruncated: false },
  }))
  on('command.register', async ($, e) => ({ value: { command: e.name } }))
  on('ui.status', async ($, e) => {
    said.status.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', async () => ({ value: undefined }))
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('ui.open', async () => ({ value: { isPlaced: true as const } }))
  await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
  // Act
  const result = await $.command.run({ command: 'ultra-watch', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 160 } })
  // Assert
  expect(result.text).toMatch(/^ultra-progress is not installed/)
  expect(said.status.at(-1)).toBe(undefined)
})

for (const surface of SURFACES) {
  test(`a run whose route process is gone is shown as stopped and polled slowly on ${surface}`, async ($, on) => {
    // Arrange: ultra-progress still reads the run as fixing, but ps finds no route process
    const said: Said = { status: [], toasts: [], polls: 0 }
    const clock = world(on, NEWER, said, GONE)
    await $.session.start({ cwd: '/tmp', surface, isInteractive: true })
    await clock.settle()
    const first = said.polls
    // Act: 8 s pass, two ticks of the 4 s clock
    await clock.advance(8000)
    const ui = await $.ui.mount({ plugin: 'ultra-watch', surface, component: 'Pane', requestId: 'ultra-watch',
      viewport: { columns: 100, rows: 40 },
      props: { title: 'ULTRA', isFocused: false, bodyColumns: 100, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} } })
    // Assert: no 4 s polling, the status line says it stopped, the pane says why, nothing still "running"
    expect(said.polls).toBe(first)
    expect(said.status.at(-1)).toBe('ULTRA ✗ stopped while fixing · 0/2 merged · 6 agents (1 failed) · 157k tok')
    expect(await ui.find({ text: /stopped while fixing/ })).toBeDefined()
    expect(await ui.find({ text: /route is no longer running this run/ })).toBeDefined()
    expect(await ui.find({ text: /stopped when the run ended/ })).toBeDefined()
  })
}

test('a live run whose route process is there keeps the 4 s polling', async ($, on) => {
  // Arrange
  const said: Said = { status: [], toasts: [], polls: 0 }
  const clock = world(on, NEWER, said, ALIVE)
  await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const first = said.polls
  // Act
  await clock.advance(8000)
  // Assert
  expect(said.polls).toBe(first + 2)
  expect(said.status.at(-1)).toBe('ULTRA fixing · 6 agents (1 running) · 157k tok · Fix 0/1 · 47m')
})

test('a run route stops while it is watched gets one toast', async ($, on) => {
  // Arrange: watched while route is alive
  const said: Said = { status: [], toasts: [], polls: 0 }
  const ps = { ...ALIVE }
  const clock = world(on, NEWER, said, ps)
  await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
  await clock.settle()
  // Act: route is killed; the next polls find its process gone
  Object.assign(ps, GONE)
  await clock.advance(4000)
  await clock.advance(20_000)
  // Assert
  expect(said.toasts).toEqual(['ULTRA stopped while fixing: its route process is gone (killed, crashed or a restart), 0/2 units merged'])
})
