import { expect, test } from 'claude-code/testing'

import type { UltraAgent, UltraModel, UltraPhase, UltraPhaseAgent, UltraUnit } from '../types'
import {
  classify, columns, diff, etimeSecs, FIND, folded, goneFromPs, humanTokens, isLive, isRunEnd, isShown, isStopped, MISSING_TOOL,
  MISSING_UV, parseIso, parseTokens, pollArgv, progressPath, runProcess, shortElapsed, STALE_SECS, statusText, view,
} from './logic'

function unit(id: string, state: string, cells: string[], extra: Partial<UltraUnit> = {}): UltraUnit {
  const stages = ['build', 'check', 'review', 'fix', 'merge']
  return { id, state, note: '', check: '24 passed in 5s', findings: [], agents: [],
    cells: stages.map((s, i) => [s, cells[i] ?? 'pending'] as [string, string]), ...extra }
}

function card(role: string, state: string, tokens: string, extra: Partial<UltraAgent> = {}): UltraAgent {
  return { role, model: 'fable · work', state, span: '2m 00s', tools: 4, tokens, subs: [], ...extra }
}

function run(units: UltraUnit[], extra: Partial<UltraModel> = {}): UltraModel {
  return { run: 'r1', state: 'running', step: 'swarm r1: 2 unit(s)', finished: false, exit_code: null, elapsed: '5m 00s',
    updated_secs: 3, goal: 'Do it.', stages: [], planners: [], units, activity: [], messages: [], headroom: {},
    branch: null, summary: null, ...extra }
}

function row(label: string, state: string, tokens = 0, extra: Partial<UltraPhaseAgent> = {}): UltraPhaseAgent {
  return { label, unit: label.split(' · ')[0] ?? null, role: label.split(' · ')[1] ?? label, model: 'codex', state,
    span: '1m 00s', tokens, tokens_h: humanTokens(tokens), tools: 3, now: '', note: '', subs: [], ...extra }
}

function phase(title: string, state: string, agents: UltraPhaseAgent[]): UltraPhase {
  return { title, state, summary: '', agents }
}

// ------------------------------------------------------------------ status line

test('status line reads the new totals and phases: agents, running, tokens, phase progress, time', () => {
  // Arrange: 7 agents, one reviewer still running, contract fields present
  const m = run([unit('a', 'accepted', ['done', 'done', 'done']), unit('b', 'accepted', ['done', 'done', 'done']),
    unit('c', 'accepted', ['done', 'done', 'active'])], {
    state: 'reviewing', elapsed: '34m 10s',
    totals: { agents: 7, running: 1, done: 6, queued: 0, failed: 0, tokens: 182_000, tokens_h: '182k', tools: 120, elapsed: '34m 10s' },
    phases: [
      phase('Plan', 'done', [row('planner #1', 'done', 11_000)]),
      phase('Build', 'done', [row('a · worker', 'done'), row('b · worker', 'done'), row('c · worker', 'done')]),
      phase('Review', 'active', [row('a · reviewer', 'done'), row('b · reviewer', 'done'), row('c · reviewer', 'active')]),
      phase('Fix', 'pending', []),
      phase('Integrate', 'pending', []),
    ],
  })
  // Act
  const text = statusText(m)
  // Assert
  expect(text).toBe('ULTRA reviewing · 7 agents (1 running) · 182k tok · Review 2/3 · 34m')
})

test('status line from an older ultra-progress works the agents out of the unit cards', () => {
  // Arrange: no totals/phases; a planner, two workers, one reviewer running and one still to start
  const m = run([
    unit('a', 'accepted', ['done', 'done', 'active'], { agents: [card('worker', 'done', '61k'), card('reviewer', 'active', '3k', { model: 'codex' })] }),
    unit('b', 'accepted', ['done', 'done', 'pending'], { agents: [card('worker', 'done', '51k')] }),
  ], { state: 'reviewing', planners: [card('planner #1', 'done', '11k')] })
  // Act
  const text = statusText(m)
  // Assert
  expect(text).toBe('ULTRA reviewing · 5 agents (1 running) · 126k tok · Review 0/2 · 5m')
})

test('a finished run shows merged count and totals, then drops off after 15 minutes', () => {
  // Arrange
  const done = run([unit('a', 'merged', ['done', 'done', 'done', 'skipped', 'done'], {
    agents: [card('worker', 'done', '61k'), card('reviewer', 'done', '12k', { model: 'codex' })],
  })], { state: 'done', finished: true, exit_code: 0 })
  const stale = { ...done, updated_secs: 16 * 60 }
  // Act / Assert
  expect(statusText(done)).toBe('ULTRA ✓ done · 1/1 merged · 2 agents · 73k tok · 5m')
  expect(isShown(stale)).toBe(false)
  expect(statusText(stale)).toBe(undefined)
})

test('a failed check counts as a failure, not as progress, while the rest still build', () => {
  // Arrange: a's check failed, b still building, c passed (as ultra-progress reports it mid-swarm)
  const m = run([unit('a', 'planned', ['done', 'failed']), unit('b', 'planned', ['active']), unit('c', 'planned', ['done', 'done'])], {
    state: 'running', elapsed: '12m 03s',
    totals: { agents: 7, running: 1, done: 2, queued: 3, failed: 1, tokens: 0, tokens_h: '0', tools: 0, elapsed: '12m 03s' },
    phases: [
      phase('Plan', 'done', [row('planner', 'done')]),
      phase('Build', 'active', [row('a · worker', 'failed'), row('b · worker', 'active'), row('c · worker', 'done')]),
      phase('Review', 'pending', [row('b · reviewer', 'queued'), row('c · reviewer', 'queued')]),
      phase('Fix', 'pending', [row('a · fixer', 'queued')]),
      phase('Integrate', 'pending', []),
    ],
  })
  // Act
  const text = statusText(m)
  // Assert: Build is 1 of 3 done, and the failure is counted with the agents
  expect(text).toBe('ULTRA running · 7 agents (1 running, 1 failed) · Build 1/3 · 12m')
})

test('a run whose route process is gone stops polling fast and says so, then drops off like a finished run', () => {
  // Arrange: route was killed while fixing; ultra-progress still reads the run as fixing
  const killed = run([unit('a', 'fixing', ['done', 'done', 'done', 'active'], {
    findings: ['Reserves double-counted'],
    agents: [card('worker', 'done', '61k'), card('reviewer', 'done', '12k', { model: 'codex' }), card('fixer #1', 'active', '9k')],
  }), unit('b', 'accepted', ['done', 'done', 'pending'], { agents: [card('worker', 'done', '40k')] })],
  { state: 'fixing', elapsed: '45m 00s', updated_secs: 60, stopped: true })
  const longAgo = { ...killed, updated_secs: 6 * 24 * 3600 }
  // Act
  const { phases, totals } = view(killed)
  // Assert: not live (so no 4 s polling), shown for a while with nothing running and no ticking clock
  expect(isLive(killed)).toBe(false)
  expect(statusText(killed)).toBe('ULTRA ✗ stopped while fixing · 0/2 merged · 4 agents (1 failed) · 122k tok')
  expect(phases.find(p => p.title === 'Fix')?.agents.map(a => [a.state, a.note])).toEqual([['failed', 'stopped when the run ended']])
  expect(phases.find(p => p.title === 'Review')?.agents.find(a => a.unit === 'b')?.state).toBe('skipped')
  expect([totals.running, totals.elapsed]).toEqual([0, ''])
  expect(isShown(longAgo)).toBe(false)
  expect(statusText(longAgo)).toBe(undefined)
})

test('an unfinished run is stopped when its process is gone, or stale when that cannot be checked', () => {
  // Arrange
  const fresh = run([], { updated_secs: 30 })
  const stale = run([], { updated_secs: STALE_SECS + 1 })
  const finished = run([], { state: 'done', finished: true, updated_secs: STALE_SECS + 1 })
  // Act / Assert
  expect([isStopped(fresh, true), isStopped(fresh, false), isStopped(fresh, null)]).toEqual([true, false, false])
  expect([isStopped(stale, false), isStopped(stale, null)]).toEqual([false, true])
  expect(isStopped(finished, true)).toBe(false)
})

test('ps tells a live route process from a gone, zombie or reused pid', () => {
  // Arrange
  const ps = (exitCode: number, stdout: string) => ({ exitCode, stdout })
  // Act
  const got = [
    goneFromPs(ps(0, 'Ss      01:02:03\n'), 3600),   // older than the run: route itself
    goneFromPs(ps(1, ''), 3600),                     // no such process
    goneFromPs(ps(0, 'Z+   10:00'), 60),              // a zombie
    goneFromPs(ps(0, 'S    00:02'), 3600),            // a younger process got the pid after a restart
    goneFromPs(ps(127, ''), 3600),                   // no ps: not known
    goneFromPs(ps(0, 'S    1-00:00:00'), null),       // no start time: not reused as far as anyone can tell
  ]
  // Assert
  expect(got).toEqual([false, true, true, true, null, false])
  expect([etimeSecs('1-02:03:04'), etimeSecs('02:03:04'), etimeSecs('03:04'), etimeSecs('soon')]).toEqual([93784, 7384, 184, null])
})

test('the run\'s progress.json is found where ultra-progress looks, and names route\'s pid and start', () => {
  // Arrange
  const text = JSON.stringify({ pid: 4242, started_at: '2026-11-06T22:00:00.500000Z', state: 'fixing' })
  // Act / Assert
  expect(progressPath({ HOME: '/h' }, 'r1-2026')).toBe('/h/.local/state/route/ultra/r1-2026/progress.json')
  expect(progressPath({ HOME: '/h', ROUTE_STATE_DIR: '~/st/' }, 'r1')).toBe('/h/st/ultra/r1/progress.json')
  expect(progressPath({ HOME: '/h', ROUTE_STATE_DIR: '/s' }, '../x')).toBe(null)
  expect(progressPath({}, 'r1')).toBe(null)
  expect(runProcess(text)).toEqual({ pid: 4242, startedMs: Date.UTC(2026, 10, 6, 22, 0, 0) })
  expect([runProcess('{"pid": null}'), runProcess('not json')]).toEqual([null, null])
  expect(parseIso('2026-11-06T23:00:00+01:00')).toBe(Date.UTC(2026, 10, 6, 22, 0, 0))
})

// ------------------------------------------------------------------ the workflow view

test('the derived view queues a reviewer per unit and a fixer per unit with confirmed bugs', () => {
  // Arrange: unit a reviewed with 2 confirmed bugs, fix not started; unit b still building
  const m = run([
    unit('a', 'accepted', ['done', 'done', 'done', 'pending'], {
      findings: ['Reserves double-counted', 'Wrong trust'],
      agents: [card('worker', 'done', '61k'), card('reviewer', 'done', '12k', { model: 'codex' })],
    }),
    unit('b', 'planned', ['active'], { agents: [card('worker', 'active', '9k', { now: 'Bash pytest -q' })] }),
  ], { state: 'running' })
  // Act
  const { phases, totals } = view(m)
  const by = (t: string) => phases.find(p => p.title === t)
  // Assert
  expect(phases.map(p => p.title)).toEqual(['Plan', 'Build', 'Review', 'Fix', 'Integrate'])
  expect(by('Build')?.state).toBe('active')
  expect(by('Build')?.agents.find(a => a.unit === 'b')?.now).toBe('Bash pytest -q')
  expect(by('Review')?.agents.map(a => [a.label, a.state, a.note])).toEqual([
    ['a · reviewer', 'done', '2 confirmed bugs'], ['b · reviewer', 'queued', ''],
  ])
  expect(by('Fix')?.agents.map(a => [a.label, a.state])).toEqual([['a · fixer', 'queued']])
  expect(by('Fix')?.state).toBe('pending')
  expect(by('Integrate')?.state).toBe('pending')
  expect([totals.agents, totals.running, totals.queued, totals.tokens_h]).toEqual([5, 1, 2, '82k'])
})

test('the view takes the snapshot\'s own phases and fills fields an agent left out', () => {
  // Arrange: a phase agent missing subs, now and tokens_h
  const sparse = { label: 'a · worker', unit: 'a', role: 'worker', model: null, state: 'active', span: '', tokens: 3000 }
  const m = run([], { phases: [{ title: 'Build', state: 'active', summary: '0/1', agents: [sparse as UltraPhaseAgent] }] })
  // Act
  const { phases, totals, log } = view(m)
  // Assert
  expect(phases[0]?.agents[0]).toMatchObject({ subs: [], now: '', note: '', tokens_h: '3k', tools: 0 })
  expect([totals.agents, totals.running, totals.tokens_h]).toEqual([1, 1, '3k'])
  expect(log).toBe(null)
})

test('done phases fold first when the pane is short; the phase at work never folds', () => {
  // Arrange
  const phases = [
    phase('Plan', 'done', [row('planner #1', 'done')]),
    phase('Build', 'done', [row('a · worker', 'done'), row('b · worker', 'done')]),
    phase('Review', 'active', [row('a · reviewer', 'active', 0, { now: 'Bash pytest' }), row('b · reviewer', 'queued')]),
  ]
  // Act / Assert
  expect([...folded(phases, 20)]).toEqual([])
  expect([...folded(phases, 6)]).toEqual(['Plan', 'Build'])
  expect([...folded(phases, 1)]).toEqual(['Plan', 'Build'])
})

test('agent columns fit the widest label, and the model column goes below 80 columns', () => {
  // Arrange
  const agents = [row('finance-history · reviewer', 'done'), row('planner #1', 'done', 0, { model: 'fable · work' })]
  // Act
  const wide = columns(agents, 100)
  const narrow = columns(agents, 60)
  // Assert
  expect(wide).toEqual({ label: 26, model: 12 })
  expect(narrow).toEqual({ label: 26, model: 0 })
})

test('token and time helpers read and write ultra-progress\'s short forms', () => {
  // Arrange / Act / Assert
  expect([parseTokens('61k'), parseTokens('1.2M'), parseTokens(850), parseTokens(''), parseTokens(undefined)]).toEqual([61_000, 1_200_000, 850, 0, 0])
  expect([humanTokens(850), humanTokens(61_400), humanTokens(1_260_000)]).toEqual(['850', '61k', '1.3M'])
  expect([shortElapsed('34m 10s'), shortElapsed('1h 02m'), shortElapsed('45s'), shortElapsed('')]).toEqual(['34m', '1h 02m', '45s', ''])
})

// ------------------------------------------------------------------ toasts

test('the first snapshot of a run records without toasting', () => {
  // Arrange
  const m = run([unit('a', 'accepted', ['done', 'done'])], { log: ['planner split the goal into 1 unit'] })
  // Act
  const { toasts, seen } = diff({}, m)
  // Assert
  expect(toasts).toEqual([])
  expect(Object.keys(seen).length).toBeGreaterThan(0)
})

test('build, review with confirmed bugs, merge and run end each raise one toast', () => {
  // Arrange: a running build is seen first (no narrator log: an older ultra-progress)
  let { seen } = diff({}, run([unit('a', 'planned', ['active'])]))
  // Act / Assert: the check passes
  let d = diff(seen, run([unit('a', 'planned', ['done', 'done'])]))
  expect(d.toasts).toEqual(['ULTRA a: built and passed its check (24 passed in 5s)'])
  seen = d.seen
  // review confirms a bug
  d = diff(seen, run([unit('a', 'accepted', ['done', 'done', 'done'], { findings: ['Reserves double-counted'] })], { state: 'reviewing' }))
  expect(d.toasts).toEqual(['ULTRA a: review confirmed 1 bug(s) to fix — Reserves double-counted'])
  seen = d.seen
  // merged, and the run finishes
  d = diff(seen, run([unit('a', 'merged', ['done', 'done', 'done', 'done', 'done'], { findings: ['Reserves double-counted'] })],
    { state: 'done', finished: true, exit_code: 0, branch: 'ultra/r1' }))
  expect(d.toasts).toEqual(['ULTRA a: merged', 'ULTRA finished: 1/1 units merged on ultra/r1 (exit 0)'])
  // nothing new, nothing raised
  expect(diff(d.seen, run([unit('a', 'merged', ['done', 'done', 'done', 'done', 'done'], { findings: ['Reserves double-counted'] })],
    { state: 'done', finished: true, exit_code: 0, branch: 'ultra/r1' })).toasts).toEqual([])
})

test('a failed unit says why', () => {
  // Arrange
  const { seen } = diff({}, run([unit('a', 'fixing', ['done', 'done', 'done', 'active'], { note: 'fix pass 2' })]))
  // Act
  const d = diff(seen, run([unit('a', 'failed', ['done', 'done', 'done', 'failed'], { note: 'unresolved after fix passes' })]))
  // Assert
  expect(d.toasts).toEqual(['ULTRA a: failed — unresolved after fix passes'])
})

test('with a narrator log, toasts use its lines for the same events', () => {
  // Arrange: the plan is in, unit a is building
  const lines = ['planner split the goal into 1 unit']
  let { seen } = diff({}, run([unit('a', 'planned', ['active'])], { log: [...lines] }))
  // Act / Assert: the check passes
  lines.push('a built and passed its check (24 passed in 5s)')
  let d = diff(seen, run([unit('a', 'planned', ['done', 'done'])], { log: [...lines] }))
  expect(d.toasts).toEqual(['ULTRA a built and passed its check (24 passed in 5s)'])
  seen = d.seen
  // the review confirms a bug
  lines.push('codex confirmed 1 bug in a: Reserves double-counted')
  d = diff(seen, run([unit('a', 'accepted', ['done', 'done', 'done'], { findings: ['Reserves double-counted'] })], { log: [...lines] }))
  expect(d.toasts).toEqual(['ULTRA codex confirmed 1 bug in a: Reserves double-counted'])
  seen = d.seen
  // merged, and the run finishes
  lines.push('a merged', 'run finished: 1/1 merged on ultra/r1')
  d = diff(seen, run([unit('a', 'merged', ['done', 'done', 'done', 'done', 'done'], { findings: ['Reserves double-counted'] })],
    { state: 'done', finished: true, exit_code: 0, branch: 'ultra/r1', log: [...lines] }))
  expect(d.toasts).toEqual(['ULTRA a merged', 'ULTRA run finished: 1/1 merged on ultra/r1'])
})

test('a unit event with no narrator line keeps the plugin\'s words; unit lines with no event stay quiet; run lines toast', () => {
  // Arrange: units a and api, both building
  const units = (aCells: string[]) => [unit('a', 'planned', aCells), unit('api', 'planned', ['active'])]
  const { seen } = diff({}, run(units(['active']), { log: [] }))
  // Act: a passes its check with no line of its own; a line about api (no event); a run-level line
  const d = diff(seen, run(units(['done', 'done']), { log: ['api worker started on codex', 'integrator queued behind 1 unit'] }))
  // Assert: "a" does not match inside "api", so a falls back to the plugin's wording
  expect(d.toasts).toEqual(['ULTRA integrator queued behind 1 unit', 'ULTRA a: built and passed its check (24 passed in 5s)'])
})

test('a unit whose id starts with "run" keeps its toasts, and the run\'s own last line is still told once', () => {
  // Arrange: units run-tests and run (yes, "run"), both building
  const lines = ['planner split the goal into 2 units']
  const units = (cells: string[], state = 'planned') => [unit('run-tests', state, cells), unit('run', state, cells)]
  let { seen } = diff({}, run(units(['active']), { log: [...lines] }))
  // Act: both pass their checks
  lines.push('run-tests built and passed its check (5 passed in 1s)', 'run built and passed its check (5 passed in 1s)')
  let d = diff(seen, run(units(['done', 'done']), { log: [...lines] }))
  // Assert
  expect(d.toasts).toEqual(['ULTRA run-tests built and passed its check (5 passed in 1s)', 'ULTRA run built and passed its check (5 passed in 1s)'])
  seen = d.seen
  // Act: both merge and the run finishes
  lines.push('run-tests merged', 'run merged', 'run finished: 2/2 merged on ultra/r1')
  d = diff(seen, run(units(['done', 'done', 'done', 'skipped', 'done'], 'merged'),
    { state: 'done', finished: true, exit_code: 0, branch: 'ultra/r1', log: [...lines] }))
  // Assert: each unit's line, then the run's closing line in place of the plugin's own
  expect(d.toasts).toEqual(['ULTRA run-tests merged', 'ULTRA run merged', 'ULTRA run finished: 2/2 merged on ultra/r1'])
  expect([isRunEnd('run failed: 1/3 merged (exit 1)'), isRunEnd('plan only: 2 units planned, nothing built'),
    isRunEnd('run failed: check failed'), isRunEnd('run-tests merged')]).toEqual([true, true, false, false])
})

test('a check that fails while others still build is told then, and route\'s later "failed" is not told again', () => {
  // Arrange: a and b building (an older ultra-progress: no narrator log)
  let { seen } = diff({}, run([unit('a', 'planned', ['active']), unit('b', 'planned', ['active'])]))
  // Act: a's check fails; route keeps a "planned" until the whole swarm returns
  let d = diff(seen, run([unit('a', 'planned', ['done', 'failed'], { note: 'check failed' }), unit('b', 'planned', ['active'])]))
  // Assert
  expect(d.toasts).toEqual(['ULTRA a: failed — check failed'])
  seen = d.seen
  // Act: the swarm returns; route marks a failed for that same check, and b passed
  d = diff(seen, run([unit('a', 'failed', ['done', 'failed'], { note: 'check failed (agent exit 1)' }),
    unit('b', 'accepted', ['done', 'done', 'pending'])], { state: 'reviewing' }))
  // Assert: only b's news
  expect(d.toasts).toEqual(['ULTRA b: built and passed its check (24 passed in 5s)'])
  seen = d.seen
  // Act: route sends a fixer at a's failing check
  d = diff(seen, run([unit('a', 'fixing', ['done', 'done', 'done', 'active'], { note: 'fix pass 1' }),
    unit('b', 'accepted', ['done', 'done', 'pending'])], { state: 'fixing' }))
  // Assert
  expect(d.toasts).toEqual(['ULTRA a: fixing (fix pass 1)'])
})

test('with a narrator log, a failed check is told in the narrator\'s words, once', () => {
  // Arrange
  const lines = ['planner split the goal into 2 units', 'a started on codex', 'b started on codex']
  let { seen } = diff({}, run([unit('a', 'planned', ['active']), unit('b', 'planned', ['active'])], { log: [...lines] }))
  // Act: a's check fails mid-swarm
  lines.push('a failed: check failed')
  let d = diff(seen, run([unit('a', 'planned', ['done', 'failed'], { note: 'check failed' }), unit('b', 'planned', ['active'])],
    { log: [...lines] }))
  // Assert
  expect(d.toasts).toEqual(['ULTRA a failed: check failed'])
  seen = d.seen
  // Act: route marks it failed when the swarm returns, and the narrator says so again
  lines.splice(lines.indexOf('a failed: check failed'), 1, 'a failed: check failed (agent exit 1)')
  d = diff(seen, run([unit('a', 'failed', ['done', 'failed'], { note: 'check failed (agent exit 1)' }), unit('b', 'planned', ['active'])],
    { log: [...lines] }))
  // Assert
  expect(d.toasts).toEqual([])
})

test('a run seen running whose process is found gone is told once, however long ago it last wrote', () => {
  // Arrange: seen running; then route is killed in the middle of a long swarm (no write for an hour)
  const { seen } = diff({}, run([unit('a', 'planned', ['active'])]))
  const killed = run([unit('a', 'planned', ['active'])], { updated_secs: 3600, stopped: true })
  // Act
  const d = diff(seen, killed)
  // Assert
  expect(d.toasts).toEqual(['ULTRA stopped while running: its route process is gone (killed, crashed or a restart), 0/1 units merged'])
  expect(diff(d.seen, killed).toasts).toEqual([])
  expect(diff({}, { ...killed, updated_secs: 6 * 24 * 3600 })).toEqual({ toasts: [], seen: {} })
})

test('the first snapshot carrying a log only records it, so an upgraded ultra-progress never floods', () => {
  // Arrange: seen from an older ultra-progress (no log)
  const { seen } = diff({}, run([unit('a', 'planned', ['active'])]))
  const backlog = Array.from({ length: 12 }, (_, i) => `line ${i}`).concat('a built and passed its check (24 passed in 5s)')
  // Act: the upgrade lands with a full log, and a really does pass its check now
  const d = diff(seen, run([unit('a', 'planned', ['done', 'done'])], { log: backlog }))
  // Assert: the event still toasts, in the plugin's words; the backlog does not
  expect(d.toasts).toEqual(['ULTRA a: built and passed its check (24 passed in 5s)'])
  expect(diff(d.seen, run([unit('a', 'planned', ['done', 'done'])], { log: backlog })).toasts).toEqual([])
})

test('a new run drops what was remembered about the last one', () => {
  // Arrange
  const { seen } = diff({}, run([unit('a', 'planned', ['active'])]))
  // Act
  const d = diff(seen, run([unit('b', 'planned', ['active'])], { run: 'r2' }))
  // Assert
  expect(Object.keys(d.seen).every(k => k.startsWith('r2:'))).toBe(true)
  expect(d.toasts).toEqual([])
})

// ------------------------------------------------------------------ polling

test('a poll looks on PATH, in the checkout, in the plugin\'s own copy, then in the marketplace clone under the plugins root', () => {
  // Arrange
  const places = ['command -v ultra-progress', '"$1/../../skill-ultra-progress/ultra_progress.py"', '"$1/scripts/ultra_progress.py"',
    '"$plugins/marketplaces/route-skill/skill-ultra-progress/ultra_progress.py"']
  // Act
  const argv = pollArgv('/p/ultra-watch', false)
  const at = places.map(p => FIND.indexOf(p))
  // Assert: in that order, the plugins root honouring CLAUDE_CODE_PLUGIN_CACHE_DIR and CLAUDE_CONFIG_DIR
  expect(argv).toEqual(['/bin/sh', '-c', FIND, 'ultra-watch', '/p/ultra-watch', 'offline'])
  expect(at.every((n, i) => n > 0 && (i === 0 || n > (at[i - 1] ?? 0)))).toBe(true)
  expect(FIND).toContain('plugins="${CLAUDE_CODE_PLUGIN_CACHE_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins}"')
  expect(FIND).not.toContain('"$HOME/.claude/plugins/marketplaces')
})

test('a poll result is a run, no run, a cache miss offline, a missing tool or an error', () => {
  // Arrange
  const res = (exitCode: number, stdout: string, stderr = '') => ({ exitCode, stdout, stderr })
  // Act
  const results = [
    classify(res(0, JSON.stringify(run([])))).kind,
    classify(res(1, '', 'ultra-progress: no /ultra runs under /x/ultra')).kind,
    classify(res(1, '', '  hint: Packages were unavailable because the network was disabled.')).kind,
    classify(res(127, '', 'ultra-progress: not found')),
    classify(res(127, '', 'env: uv: No such file or directory')),
    classify(res(1, '{"run": "r1", "st', 'Traceback (most recent call last):\n  boom')),
  ]
  // Assert
  expect(results.slice(0, 3)).toEqual(['run', 'none', 'offline'])
  expect(results[3]).toEqual({ kind: 'missing', why: MISSING_TOOL })
  expect(results[4]).toEqual({ kind: 'missing', why: MISSING_UV })
  expect(results[5]).toEqual({ kind: 'error', why: 'Traceback (most recent call last):' })
})
