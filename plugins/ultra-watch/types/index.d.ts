// The shape of `ultra-progress --json` (route-skill), as ultra-watch reads it.
// `totals`, `phases` and `log` are newer: an older ultra-progress leaves them out,
// and the plugin then works them out from `planners` and `units`.

export type Cell = [string, string]

export type UltraSub = { prompt: string; live: boolean; span: string; tools: number }

/** An agent as a unit card lists it (the older, per-unit view). */
export type UltraAgent = {
  role: string
  model: string | null
  state: string
  note?: string | null
  span?: string
  tools?: number
  /** Output tokens, already short ("61k"). */
  tokens?: string
  now?: string
  subs: UltraSub[]
}

export type UltraUnit = {
  id: string
  tier?: string | null
  state: string
  note: string
  check: string
  findings: string[]
  cells: Cell[]
  agents: UltraAgent[]
}

/** Every agent the run has started or will start that can be named. */
export type UltraTotals = {
  agents: number
  running: number
  done: number
  queued: number
  failed: number
  /** Summed output tokens of started agents, sub-agents included. */
  tokens: number
  tokens_h: string
  tools: number
  elapsed: string
}

export type UltraAgentState = 'done' | 'active' | 'queued' | 'failed' | 'skipped'
export type UltraPhaseState = 'done' | 'active' | 'pending' | 'failed' | 'skipped'
export type UltraPhaseTitle = 'Plan' | 'Build' | 'Review' | 'Fix' | 'Integrate'

/** An agent as the workflow view lists it, under its phase. */
export type UltraPhaseAgent = {
  label: string
  unit: string | null
  role: string
  model: string | null
  state: UltraAgentState | string
  span: string
  tokens: number
  tokens_h: string
  tools: number
  now: string
  note: string
  subs: UltraSub[]
}

export type UltraPhase = {
  title: UltraPhaseTitle | string
  state: UltraPhaseState | string
  summary: string
  agents: UltraPhaseAgent[]
}

export type UltraModel = {
  run: string
  state: string
  step: string
  finished: boolean
  exit_code: number | null
  elapsed: string
  updated_secs: number | null
  goal: string
  stages: Cell[]
  planners: UltraAgent[]
  units: UltraUnit[]
  activity: [string, string, string, string, string][]
  messages: [string, string, string][]
  headroom: Record<string, string>
  branch: string | null
  summary: string | null
  totals?: UltraTotals
  phases?: UltraPhase[]
  /** Narrator lines, newest last, at most 12. */
  log?: string[]
  /**
   * Set by ultra-watch, never by ultra-progress: the run is not finished but route's process
   * for it is gone (killed, crashed, or the machine restarted), so nothing will move it on.
   */
  stopped?: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'ultra-watch': {
      model: UltraModel | null
      seen: Record<string, string>
      /** Why there is nothing to show beyond "no run yet", e.g. ultra-progress is not installed. */
      problem: string | null
    }
  }
}
