// The plugin's worker. It reads workspace names with `workspace.list` and writes them with
// `workspace.rename` (Orca's plugin API, host API 1.5); it imports nothing from Node, and never
// reads Orca's files, starts programs or uses the network. Orca transpiles this file when it
// loads: no build step.
import type { JsonValue, NamingNotice, NamingView } from 'orca'
import type { Disposable, OrcaWorkerApi } from 'orca/worker'
import { parseAliases, planAll, shortHash, type AliasError, type Change, type NameRecord, type Options, type Plan, type Workspace } from './core.ts'

const STATE_KEY = 'state-v2'
const ACTIVE_POLL_MS = 5_000
const IDLE_POLL_MS = 15_000
const CREATED_DEBOUNCE_MS = 400
/** How long after it first appears a new workspace may still be renamed on its own. */
const AUTO_WINDOW_MS = 6 * 60 * 60_000
/** How long a workspace may be missing from the list before the plugin forgets it. */
const FORGET_AFTER_MS = 14 * 24 * 60 * 60_000
const MAX_RENAMES_PER_PASS = 10
const MAX_APPLY = 50
const HISTORY_LIMIT = 200
const DAY_MS = 24 * 60 * 60_000
const DEFAULT_STANDBY_LABEL = 'Standby'

type Reason = NamingView['history'][number]['reason']
type HistoryItem = {
  key: string
  id: string
  before: string
  after: string
  reason: Reason
  at: number
  undone?: boolean
}
type State = {
  version: 2
  /** Workspaces that existed before the plugin could watch them: suggestions only, never automatic. */
  baseline: string[]
  /** False while the first list was truncated: the next complete list fills the baseline in. */
  baselineComplete: boolean
  /** When each newer workspace was first seen while the plugin was on. */
  seen: Record<string, number>
  /** When a known workspace went missing from the list; forgotten after FORGET_AFTER_MS. */
  gone: Record<string, number>
  records: Record<string, NameRecord>
  history: HistoryItem[]
  /** Written before each rename so a crash mid-rename is reconciled on the next pass. */
  pending: (Change & { at: number }) | null
}
type Settings = Options & { enabled: boolean; aliasErrors: AliasError[] }

/** Stops the running activation: its timer, its subscriptions and any rename still queued. */
let stopCurrent: (() => void) | null = null

export default function activate(orca: OrcaWorkerApi): void {
  stopCurrent?.()
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let dueAt = Infinity
  let queue: Promise<unknown> = Promise.resolve()
  let lastPlan: Plan = { auto: [], suggestions: [], locks: [], waiting: [] }
  let lastBar = ''
  let view: NamingView = {
    status: 'starting',
    error: null,
    checkedAt: null,
    enabled: true,
    standbyNames: false,
    aliasErrors: [],
    counts: { managed: 0, locked: 0, waiting: 0, renamedToday: 0 },
    suggestions: [],
    history: [],
    notice: null
  }

  /** Passes, applies and undos run one at a time, in order. */
  function serial<T>(task: () => Promise<T>): Promise<T> {
    const run = queue.then(task, task)
    queue = run.catch(() => undefined)
    return run
  }

  async function readSettings(): Promise<Settings> {
    const raw = await orca.settings.get()
    const { aliases, errors } = parseAliases(raw.aliases)
    const label = typeof raw.standbyLabel === 'string' ? raw.standbyLabel.replace(/[｜|]/g, ' ').trim().slice(0, 24) : ''
    return {
      enabled: raw.enabled !== false,
      standbyNames: raw.standbyNames === true,
      standbyLabel: label || DEFAULT_STANDBY_LABEL,
      aliases,
      aliasErrors: errors
    }
  }

  async function loadState(): Promise<State | null> {
    const raw = (await orca.storage.get(STATE_KEY)) as unknown as Partial<State> | null
    if (!raw || raw.version !== 2 || !Array.isArray(raw.baseline) || !Array.isArray(raw.history)) return null
    return {
      version: 2,
      baseline: raw.baseline,
      baselineComplete: raw.baselineComplete !== false,
      seen: raw.seen ?? {},
      gone: raw.gone ?? {},
      records: raw.records ?? {},
      history: raw.history,
      pending: raw.pending ?? null
    }
  }

  const saveState = (state: State) => orca.storage.set(STATE_KEY, state as unknown as JsonValue)

  async function listWorkspaces(): Promise<{ workspaces: Workspace[]; truncated: boolean }> {
    const result = await orca.host.call('workspace.list')
    return { workspaces: result.workspaces as Workspace[], truncated: result.truncated }
  }

  /** Workspaces the plugin may still rename on its own: first seen recently, while it was on. */
  function autoIds(state: State, now: number): Set<string> {
    return new Set(Object.keys(state.seen).filter((id) => now - state.seen[id] < AUTO_WINDOW_MS))
  }

  async function rename(state: State, change: Change, record: Partial<NameRecord> = {}): Promise<string> {
    state.pending = { ...change, at: Date.now() }
    await saveState(state)
    try {
      const result = await orca.host.call('workspace.rename', { workspaceId: change.id, name: change.after })
      const actual = typeof result?.name === 'string' ? result.name : change.after
      const at = Date.now()
      state.records[change.id] = { name: actual, stage: change.stage, at, ...record }
      state.history.push({
        key: `${at.toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
        id: change.id,
        before: change.before,
        after: actual,
        reason: change.reason,
        at
      })
      state.history = state.history.slice(-HISTORY_LIMIT)
      // Names come from people's prompts: keep them out of the log.
      orca.log(`renamed a workspace (${change.reason})`)
      return actual
    } finally {
      state.pending = null
      await saveState(state)
    }
  }

  function buildView(state: State, settings: Settings, notice: NamingNotice | null): NamingView {
    const now = Date.now()
    const records = Object.values(state.records)
    return {
      status: settings.enabled ? 'active' : 'paused',
      error: null,
      checkedAt: now,
      enabled: settings.enabled,
      standbyNames: settings.standbyNames,
      aliasErrors: settings.aliasErrors,
      counts: {
        managed: records.filter((r) => !r.locked).length,
        locked: records.filter((r) => r.locked).length,
        waiting: lastPlan.waiting.length,
        renamedToday: state.history.filter((h) => h.reason !== 'undo' && !h.undone && now - h.at < DAY_MS).length
      },
      suggestions: lastPlan.suggestions.slice(0, 200).map((c) => ({ id: c.id, current: c.before, proposed: c.after, reason: c.reason })),
      history: state.history
        .slice(-50)
        .reverse()
        .map((h) => ({ key: h.key, before: h.before, after: h.after, reason: h.reason, at: h.at, undone: Boolean(h.undone) })),
      notice
    }
  }

  async function publish(next: NamingView): Promise<NamingView> {
    view = next
    orca.panels.post('naming.changed', view)
    const s = view.counts.renamedToday
    const bar =
      view.status === 'active'
        ? { text: view.suggestions.length ? `Naming Guard · ${view.suggestions.length}` : 'Naming Guard', icon: 'edit' as const, tone: view.suggestions.length ? ('accent' as const) : ('success' as const), tooltip: `Workspace Naming Guard is on · ${s} renamed in 24 h · ${view.suggestions.length} suggestions · ${view.counts.waiting} waiting for a title` }
        : view.status === 'paused'
          ? { text: 'Naming Guard', icon: 'pause' as const, tone: 'neutral' as const, tooltip: 'Workspace Naming Guard is paused' }
          : view.status === 'unsupported'
            ? { text: 'Naming Guard', icon: 'warning' as const, tone: 'warning' as const, tooltip: 'Workspace Naming Guard needs Orca 1.4.256 or later' }
            : { text: 'Naming Guard', icon: 'warning' as const, tone: 'warning' as const, tooltip: `Workspace Naming Guard could not check names: ${view.error ?? 'unknown error'}` }
    const signature = JSON.stringify(bar)
    if (signature !== lastBar) {
      lastBar = signature
      // Cosmetic: a status bar Orca cannot draw (a headless host) must never stop renaming.
      await orca.host.call('statusBar.update', { id: 'naming', ...bar }).catch(() => undefined)
    }
    return view
  }

  /** One pass: list, update what the plugin knows, plan, rename new workspaces, publish. */
  async function pass(notice: NamingNotice | null = null): Promise<NamingView> {
    const settings = await readSettings()
    if (!orca.host.has('workspace.list') || !orca.host.has('workspace.rename')) {
      return publish({ ...view, status: 'unsupported', error: null, checkedAt: Date.now(), notice })
    }
    const { workspaces, truncated } = await listWorkspaces()
    const now = Date.now()
    const byId = new Map(workspaces.map((w) => [w.id, w]))
    const stored = await loadState()
    const state: State = stored ?? {
      version: 2,
      baseline: workspaces.map((w) => w.id),
      baselineComplete: !truncated,
      seen: {},
      gone: {},
      records: {},
      history: [],
      pending: null
    }
    let dirty = !stored

    if (state.pending) {
      const ws = byId.get(state.pending.id)
      if (ws && ws.name === state.pending.after) {
        state.records[ws.id] = { name: ws.name, stage: state.pending.stage, at: now }
      }
      state.pending = null
      dirty = true
    }

    const known = new Set([...state.baseline, ...Object.keys(state.seen)])
    if (!state.baselineComplete && !truncated) {
      // The first list was cut short: whatever it missed existed before the plugin, too.
      for (const ws of workspaces) {
        if (known.has(ws.id)) continue
        state.baseline.push(ws.id)
        known.add(ws.id)
      }
      state.baselineComplete = true
      dirty = true
    }
    for (const ws of workspaces) {
      if (known.has(ws.id)) continue
      // Only a workspace that appears while the plugin is on, in a complete list, is "new".
      if (truncated || !settings.enabled) state.baseline.push(ws.id)
      else state.seen[ws.id] = now
      dirty = true
    }
    if (!settings.enabled) {
      // Paused: anything not yet named becomes a suggestion, so a name typed meanwhile is safe.
      for (const id of Object.keys(state.seen)) {
        if (state.records[id]) continue
        delete state.seen[id]
        state.baseline.push(id)
        dirty = true
      }
    }
    if (!truncated) {
      // A workspace can drop out of one list (a host offline, Orca starting): forget it only
      // after it has been missing for a long time, so its lock survives.
      for (const id of new Set([...state.baseline, ...Object.keys(state.seen), ...Object.keys(state.records)])) {
        if (byId.has(id)) {
          if (id in state.gone) {
            delete state.gone[id]
            dirty = true
          }
          continue
        }
        const since = (state.gone[id] ??= now)
        if (since === now) dirty = true
        if (now - since < FORGET_AFTER_MS) continue
        delete state.gone[id]
        delete state.seen[id]
        delete state.records[id]
        state.baseline = state.baseline.filter((b) => b !== id)
        dirty = true
      }
    }

    lastPlan = planAll(workspaces, autoIds(state, now), state.records, settings, shortHash)
    for (const id of lastPlan.locks) {
      state.records[id] = { ...state.records[id], name: byId.get(id)?.name ?? '', locked: true, at: now }
      dirty = true
    }
    if (dirty) await saveState(state)

    let failed: string | null = null
    if (settings.enabled) {
      for (const change of lastPlan.auto.slice(0, MAX_RENAMES_PER_PASS)) {
        if (stopped) break
        try {
          await rename(state, change)
        } catch (e) {
          failed = errorCode(e)
          orca.log(`a rename failed: ${failed}`)
        }
      }
      // Renamed ones no longer wait.
      lastPlan = { ...lastPlan, auto: [], waiting: lastPlan.waiting.filter((id) => !state.records[id]) }
    }
    return publish(buildView(state, settings, notice ?? (failed ? { code: 'rename-failed', detail: failed } : null)))
  }

  async function guardedPass(notice: NamingNotice | null = null): Promise<NamingView> {
    try {
      return await pass(notice)
    } catch (e) {
      const code = errorCode(e)
      orca.log(`check failed: ${code}`)
      return publish({ ...view, status: 'error', error: code, checkedAt: Date.now(), notice })
    }
  }

  /** Runs a pass after `delay`, unless one is already due sooner. */
  function schedule(delay: number): void {
    if (stopped) return
    const due = Date.now() + delay
    if (timer !== undefined && dueAt <= due) return
    clearTimeout(timer)
    dueAt = due
    timer = setTimeout(() => {
      timer = undefined
      dueAt = Infinity
      void serial(() => guardedPass()).then((v) => schedule(v.counts.waiting > 0 ? ACTIVE_POLL_MS : IDLE_POLL_MS))
    }, delay)
  }

  /** Re-lists, then renames the given suggestions or locks the given ids. */
  async function act(ids: string[], mode: 'apply' | 'ignore'): Promise<NamingView> {
    const settings = await readSettings()
    const state = await loadState()
    if (!state) return guardedPass()
    const { workspaces } = await listWorkspaces()
    const byId = new Map(workspaces.map((w) => [w.id, w]))
    const wanted = new Set(ids.slice(0, MAX_APPLY))
    let notice: NamingNotice | null = null
    if (mode === 'apply') {
      const fresh = planAll(workspaces, autoIds(state, Date.now()), state.records, settings, shortHash)
      const changes = fresh.suggestions.filter((c) => wanted.has(c.id))
      let failed = 0
      let lastError = ''
      for (const change of changes) {
        try {
          await rename(state, change)
        } catch (e) {
          failed++
          lastError = errorCode(e)
        }
      }
      if (failed) notice = { code: 'apply-failed', detail: `${failed}/${changes.length} · ${lastError}` }
      else if (changes.length < wanted.size) notice = { code: 'stale' }
    } else {
      const now = Date.now()
      for (const id of wanted) {
        const ws = byId.get(id)
        if (ws) state.records[id] = { name: ws.name, stage: 'final', at: now, locked: true }
      }
      await saveState(state)
    }
    return pass(notice)
  }

  async function undo(key: string): Promise<NamingView> {
    const state = await loadState()
    const item = state?.history.find((h) => h.key === key)
    if (!state || !item || item.undone || item.reason === 'undo') return pass({ code: 'already-undone' })
    const { workspaces } = await listWorkspaces()
    const ws = workspaces.find((w) => w.id === item.id)
    if (!ws) return pass({ code: 'gone' })
    if (ws.name !== item.after) return pass({ code: 'renamed-since', detail: ws.name })
    await rename(state, { id: item.id, before: item.after, after: item.before, reason: 'undo', stage: 'final' }, { locked: true })
    item.undone = true
    await saveState(state)
    return pass()
  }

  async function toggle(): Promise<NamingView> {
    const { enabled } = await readSettings()
    await orca.settings.set('enabled', !enabled)
    return guardedPass()
  }

  orca.panels.handle('naming.view', () => (view.status === 'starting' ? serial(() => guardedPass()) : view))
  orca.panels.handle('naming.check', () => serial(() => guardedPass()))
  orca.panels.handle('naming.apply', ({ ids }) => serial(() => act(ids, 'apply')))
  orca.panels.handle('naming.ignore', ({ ids }) => serial(() => act(ids, 'ignore')))
  orca.panels.handle('naming.undo', ({ key }) => serial(() => undo(key)))
  orca.panels.handle('naming.setEnabled', ({ enabled }) =>
    serial(async () => {
      await orca.settings.set('enabled', enabled)
      return guardedPass()
    })
  )

  orca.commands.register('naming.status', async () => describe(await serial(() => guardedPass())))
  orca.commands.register('naming.check', async () => describe(await serial(() => guardedPass())))
  orca.commands.register('naming.toggle', async () => describe(await serial(toggle)))

  const subscriptions: Disposable[] = [
    orca.events.on('workspace.created', () => schedule(CREATED_DEBOUNCE_MS)),
    orca.settings.onDidChange(() => schedule(CREATED_DEBOUNCE_MS))
  ]
  stopCurrent = () => {
    stopped = true
    clearTimeout(timer)
    for (const s of subscriptions) s.dispose()
  }
  schedule(0)
}

export function deactivate(): void {
  stopCurrent?.()
  stopCurrent = null
}

function errorCode(e: unknown): string {
  const code = (e as { code?: unknown } | null)?.code
  return typeof code === 'string' && /^[a-z_]+$/.test(code) ? code : 'action_failed'
}

/** What the palette shows for a command: one plain line. */
function describe(view: NamingView): string {
  switch (view.status) {
    case 'unsupported':
      return 'Workspace Naming Guard needs Orca 1.4.256 or later.'
    case 'error':
      return `Workspace Naming Guard could not check names (${view.error}).`
    default: {
      const state = view.status === 'paused' ? 'paused' : 'on'
      const c = view.counts
      return `Workspace Naming Guard is ${state}: ${c.renamedToday} renamed in 24 h, ${view.suggestions.length} suggestions, ${c.waiting} waiting for a title, ${c.locked} left as you named them. Open the Naming Guard panel in the right sidebar for details.`
    }
  }
}
