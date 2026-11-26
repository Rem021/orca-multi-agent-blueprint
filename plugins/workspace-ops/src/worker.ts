// Trusted worker for Workspace Ops Radar.
// Orchestrates fast/storage/health/memory collectors, live Orca events, status bar updates,
// safe GC/restore/.keep actions, and automatic or manual alert handoffs to an Orca Bot (e.g. Ram).

import type { JsonValue } from 'orca/host'
import type { Disposable, OrcaWorkerApi } from 'orca/worker'
import {
  createNodeCollector,
  type FastTelemetry,
  type GcPreviewTelemetry,
  type HealthTelemetry,
  type HeavyStorageTelemetry,
  type OpsCollector,
  type ResolvedEnv
} from './collectors.ts'
import {
  buildBotEscalationBrief,
  buildStatusBarState,
  buildStorageOffenders,
  computeNextGcAt,
  DEFAULT_HYGIENE_CONFIG,
  evaluateAlerts,
  formatBytes,
  formatDurationCompact,
  HOUR_MS,
  MIN_MS,
  normalizeHygieneConfig
} from './core.ts'
import type {
  ActionNotice,
  ArchiveRecord,
  BotAlertMode,
  BotDispatchRecord,
  GcMode,
  HygieneConfig,
  LiveWorkspace,
  LiveWorkspaceState,
  MemoryDiagnostics,
  OpsAlert,
  OpsSettings,
  OpsSnapshot,
  RefreshScope,
  TrashBatch
} from './protocol.ts'

const STATE_KEY = 'ops-radar-state-v1'
const ALERT_COOLDOWN_MS = 6 * HOUR_MS
const GC_PREVIEW_REFRESH_MS = 5 * MIN_MS
const HEAVY_STORAGE_REFRESH_MS = 30 * MIN_MS
const HEALTH_REFRESH_MS = 15 * MIN_MS
const MEMORY_CACHE_MS = 60_000
const EVENT_PUBLISH_DEBOUNCE_MS = 400
const DISPATCH_HISTORY_LIMIT = 20
const ARCHIVE_HISTORY_LIMIT = 15
const ARCHIVED_BATCH_LIMIT = 12
const PANEL_WORKSPACES_LIMIT = 60

type PersistedState = {
  version: 1
  dismissed: Record<string, number>
  notified: Record<string, number>
  escalated: Record<string, number>
  botDispatchHistory: BotDispatchRecord[]
  archivedBatches: TrashBatch[]
  archiveHistory: ArchiveRecord[]
}

type LiveAgentOverride = {
  state: LiveWorkspaceState
  stateStartedAt: number
  receivedAt: number
}

const DEFAULT_SETTINGS: OpsSettings = {
  botName: 'Ram',
  botAlertMode: 'critical',
  desktopNotifications: true,
  diskWarnFreePercent: 15,
  waitingAlertMinutes: 15,
  gcSpikeGiB: 5,
  gcIntervalHours: 4,
  autoRefreshSeconds: 60
}

let stopCurrent: (() => void) | null = null

export function createWorker(collectorFactory: () => OpsCollector = createNodeCollector) {
  return function activate(orca: OrcaWorkerApi): void {
    stopCurrent?.()
    const collector = collectorFactory()

    let stopped = false
    let pollTimer: ReturnType<typeof setTimeout> | undefined
    let eventPublishTimer: ReturnType<typeof setTimeout> | undefined
    let serialQueue: Promise<unknown> = Promise.resolve()
    let lastBarSig = ''
    let lastPostedDigest = ''

    let currentEnv: ResolvedEnv | null = null
    let fastData: FastTelemetry | null = null
    let gcPreviewData: GcPreviewTelemetry | null = null
    let heavyStorageData: HeavyStorageTelemetry | null = null
    let healthData: HealthTelemetry | null = null
    let memoryData: MemoryDiagnostics | null = null

    let gcPreviewUpdatedAt: number | null = null
    let heavyStorageUpdatedAt: number | null = null
    let healthUpdatedAt: number | null = null
    let memoryUpdatedAt: number | null = null
    let lastManualGcAt: number | null = null
    let hostWorkspaces: {
      id: string
      kind: 'worktree' | 'folder'
      name: string
      project?: string | null
      path: string
      branch: string | null
      status?: {
        state: 'working' | 'permission' | 'done' | 'active' | 'inactive'
        unread: boolean
        terminalCount: number
        lastActivityAt: number | null
      }
    }[] = []

    const liveOverrides = new Map<string, LiveAgentOverride>()
    const busySet = new Set<string>()
    let lastNotice: ActionNotice | null = null
    let lastError: string | null = null

    let persisted: PersistedState = {
      version: 1,
      dismissed: {},
      notified: {},
      escalated: {},
      botDispatchHistory: [],
      archivedBatches: [],
      archiveHistory: []
    }
    let persistedLoaded = false

    let currentSettings: OpsSettings = { ...DEFAULT_SETTINGS }
    let latestSnapshot: OpsSnapshot = buildSnapshot()

    function serial<T>(task: () => Promise<T>): Promise<T> {
      const next = serialQueue.then(task, task)
      serialQueue = next.catch(() => undefined)
      return next
    }

    async function loadPersisted(): Promise<PersistedState> {
      if (persistedLoaded) return persisted
      try {
        const raw = (await orca.storage.get(STATE_KEY)) as unknown as Partial<PersistedState> | null
        if (raw && raw.version === 1) {
          persisted = {
            version: 1,
            dismissed: raw.dismissed ?? {},
            notified: raw.notified ?? {},
            escalated: raw.escalated ?? {},
            botDispatchHistory: Array.isArray(raw.botDispatchHistory)
              ? raw.botDispatchHistory.slice(0, DISPATCH_HISTORY_LIMIT)
              : [],
            archivedBatches: Array.isArray(raw.archivedBatches)
              ? raw.archivedBatches.slice(0, ARCHIVED_BATCH_LIMIT)
              : [],
            archiveHistory: Array.isArray(raw.archiveHistory)
              ? raw.archiveHistory.slice(0, ARCHIVE_HISTORY_LIMIT)
              : []
          }
        }
      } catch {
        // Storage unavailable in restricted test/host contexts; keep in-memory state.
      }
      persistedLoaded = true
      return persisted
    }

    function mergeTrashBatches(liveBatches: TrashBatch[]): TrashBatch[] {
      const liveIds = new Set(liveBatches.map((b) => b.id))
      const merged: TrashBatch[] = [...liveBatches]
      for (const old of persisted.archivedBatches) {
        if (!liveIds.has(old.id)) {
          merged.push({ ...old, inTrash: false })
        }
      }
      merged.sort((a, b) => b.createdAt - a.createdAt)
      return merged.slice(0, ARCHIVED_BATCH_LIMIT)
    }

    async function archiveSeenBatches(liveBatches: TrashBatch[]): Promise<void> {
      if (liveBatches.length === 0) return
      const existing = new Map(persisted.archivedBatches.map((b) => [b.id, b]))
      let changed = false
      for (const b of liveBatches) {
        if (!existing.has(b.id)) {
          existing.set(b.id, { ...b, items: b.items.slice(0, 15) })
          changed = true
        }
      }
      if (!changed) return
      persisted.archivedBatches = [...existing.values()]
        .sort((a, b) => b.createdAt - a.createdAt)
        .slice(0, ARCHIVED_BATCH_LIMIT)
      await savePersisted()
    }

    async function savePersisted(): Promise<void> {
      try {
        await orca.storage.set(STATE_KEY, persisted as unknown as JsonValue)
      } catch {
        // Ignore non-fatal persistence errors.
      }
    }

    async function readSettings(): Promise<OpsSettings> {
      try {
        const raw = await orca.settings.get()
        const mode = raw.botAlertMode
        const botAlertMode: BotAlertMode =
          mode === 'critical' || mode === 'manual' || mode === 'all' ? mode : DEFAULT_SETTINGS.botAlertMode
        const num = (v: unknown, def: number, min: number, max: number) => {
          const n = typeof v === 'number' ? v : Number(v)
          return Number.isFinite(n) ? Math.max(min, Math.min(max, n)) : def
        }
        currentSettings = {
          botName: typeof raw.botName === 'string' && raw.botName.trim() ? raw.botName.trim() : DEFAULT_SETTINGS.botName,
          botAlertMode,
          desktopNotifications: raw.desktopNotifications !== false,
          diskWarnFreePercent: Math.round(num(raw.diskWarnFreePercent, DEFAULT_SETTINGS.diskWarnFreePercent, 5, 50)),
          waitingAlertMinutes: Math.round(num(raw.waitingAlertMinutes, DEFAULT_SETTINGS.waitingAlertMinutes, 1, 240)),
          gcSpikeGiB: num(raw.gcSpikeGiB, DEFAULT_SETTINGS.gcSpikeGiB, 1, 100),
          gcIntervalHours: num(raw.gcIntervalHours, DEFAULT_SETTINGS.gcIntervalHours, 0.5, 48),
          autoRefreshSeconds: Math.round(num(raw.autoRefreshSeconds, DEFAULT_SETTINGS.autoRefreshSeconds, 15, 600))
        }
      } catch {
        currentSettings = { ...DEFAULT_SETTINGS }
      }
      return currentSettings
    }

    function mapHostState(raw?: string | null): LiveWorkspaceState {
      switch (raw) {
        case 'working':
          return 'working'
        case 'permission':
        case 'waiting':
          return 'waiting'
        case 'blocked':
          return 'blocked'
        case 'done':
          return 'done'
        case 'active':
          return 'active'
        default:
          return 'inactive'
      }
    }

    function buildWorkspaceAliasMap(): {
      aliasToId: Map<string, string>
      pathToId: Map<string, string>
    } {
      const aliasToId = new Map<string, string>()
      const pathToId = new Map<string, string>()
      for (const ws of hostWorkspaces) {
        aliasToId.set(ws.id, ws.id)
        if (ws.path) pathToId.set(ws.path, ws.id)
      }
      for (const ps of fastData?.worktreePs ?? []) {
        const resolved =
          (ps.canonicalId && aliasToId.get(ps.canonicalId)) ||
          (ps.worktreeId && aliasToId.get(ps.worktreeId)) ||
          (ps.path && pathToId.get(ps.path)) ||
          ps.canonicalId ||
          ps.worktreeId
        if (!resolved) continue
        if (ps.canonicalId) aliasToId.set(ps.canonicalId, resolved)
        if (ps.worktreeId) aliasToId.set(ps.worktreeId, resolved)
        if (ps.path && !pathToId.has(ps.path)) pathToId.set(ps.path, resolved)
      }
      return { aliasToId, pathToId }
    }

    function mergeLiveWorkspaces(
      nowMs: number,
      aliasToId: Map<string, string>
    ): LiveWorkspace[] {
      const psByCanonical = new Map<string, NonNullable<FastTelemetry['worktreePs']>[number]>()
      const psByLegacy = new Map<string, NonNullable<FastTelemetry['worktreePs']>[number]>()
      const psByPath = new Map<string, NonNullable<FastTelemetry['worktreePs']>[number]>()
      for (const row of fastData?.worktreePs ?? []) {
        if (row.canonicalId) psByCanonical.set(row.canonicalId, row)
        if (row.worktreeId) psByLegacy.set(row.worktreeId, row)
        if (row.path) psByPath.set(row.path, row)
      }
      const memById = new Map<string, { memoryBytes: number; cpuPct: number }>()
      for (const m of memoryData?.worktrees ?? []) {
        const canon = aliasToId.get(m.worktreeId) ?? m.worktreeId
        memById.set(canon, { memoryBytes: m.memoryBytes, cpuPct: m.cpuPct })
        memById.set(m.worktreeId, { memoryBytes: m.memoryBytes, cpuPct: m.cpuPct })
      }

      const list: LiveWorkspace[] = []
      const seenIds = new Set<string>()
      const seenPaths = new Set<string>()

      for (const ws of hostWorkspaces) {
        const ps = psByCanonical.get(ws.id) ?? psByLegacy.get(ws.id) ?? psByPath.get(ws.path)
        seenIds.add(ws.id)
        if (ps?.canonicalId) seenIds.add(ps.canonicalId)
        if (ps?.worktreeId) seenIds.add(ps.worktreeId)
        if (ws.path) seenPaths.add(ws.path)

        const ov = liveOverrides.get(ws.id) ?? (ps?.worktreeId ? liveOverrides.get(ps.worktreeId) : undefined)
        const mem = memById.get(ws.id) ?? (ps?.worktreeId ? memById.get(ps.worktreeId) : undefined)

        let state = mapHostState(ws.status?.state ?? ps?.agentState ?? ps?.status)
        const hostActivityAt = ws.status?.lastActivityAt ?? ps?.lastActivityAt ?? null
        let stateStartedAt = ps?.stateStartedAt ?? hostActivityAt

        // Only apply in-memory event override if it is at least as recent as both ps and host activity.
        const newestBaseline = Math.max(ps?.stateStartedAt ?? 0, hostActivityAt ?? 0)
        if (ov && ov.receivedAt >= newestBaseline) {
          state = ov.state
          stateStartedAt = ov.stateStartedAt
        }

        const waitingMinutes =
          state === 'waiting' && stateStartedAt !== null
            ? Math.max(0, Math.floor((nowMs - stateStartedAt) / MIN_MS))
            : null

        list.push({
          id: ws.id,
          name: ws.name || ps?.displayName || ws.branch || ws.path,
          project: ws.project || ps?.repo || '',
          kind: ws.kind,
          path: ws.path,
          branch: ws.branch ?? (ps?.branch || null),
          state,
          stateStartedAt,
          lastActivityAt: hostActivityAt,
          unread: Boolean(ws.status?.unread || ps?.unread),
          terminalCount: ws.status?.terminalCount ?? 0,
          bot: ps?.bot ?? null,
          linkedPR: ps?.linkedPR ?? null,
          agentType: ps?.agentType ?? null,
          lastMessage: ps?.lastMessage ?? null,
          waitingMinutes,
          memoryBytes: mem?.memoryBytes ?? null,
          cpuPct: mem?.cpuPct ?? null
        })
      }

      // Include any worktreePs rows genuinely absent from hostWorkspaces (deduped by canonicalId, worktreeId & path).
      for (const ps of fastData?.worktreePs ?? []) {
        const id = ps.canonicalId || ps.worktreeId
        if (!id) continue
        if (seenIds.has(id) || (ps.worktreeId && seenIds.has(ps.worktreeId))) continue
        if (ps.path && seenPaths.has(ps.path)) continue
        seenIds.add(id)
        if (ps.worktreeId) seenIds.add(ps.worktreeId)
        if (ps.path) seenPaths.add(ps.path)

        const ov = liveOverrides.get(id) ?? liveOverrides.get(ps.worktreeId)
        const mem = memById.get(id) ?? memById.get(ps.worktreeId)
        const baseline = Math.max(ps.stateStartedAt ?? 0, ps.lastActivityAt ?? 0)
        const useOv = Boolean(ov && ov.receivedAt >= baseline)
        const state = useOv && ov ? ov.state : mapHostState(ps.agentState ?? ps.status)
        const stateStartedAt = useOv && ov ? ov.stateStartedAt : (ps.stateStartedAt ?? ps.lastActivityAt)
        list.push({
          id,
          name: ps.displayName || ps.branch || ps.path,
          project: ps.repo,
          kind: id.startsWith('folder:') ? 'folder' : 'worktree',
          path: ps.path,
          branch: ps.branch || null,
          state,
          stateStartedAt,
          lastActivityAt: ps.lastActivityAt,
          unread: ps.unread,
          terminalCount: 0,
          bot: ps.bot,
          linkedPR: ps.linkedPR,
          agentType: ps.agentType,
          lastMessage: ps.lastMessage,
          waitingMinutes:
            state === 'waiting' && stateStartedAt !== null
              ? Math.max(0, Math.floor((nowMs - stateStartedAt) / MIN_MS))
              : null,
          memoryBytes: mem?.memoryBytes ?? null,
          cpuPct: mem?.cpuPct ?? null
        })
      }

      const priority: Record<LiveWorkspaceState, number> = {
        waiting: 0,
        blocked: 1,
        working: 2,
        done: 3,
        active: 4,
        inactive: 5
      }
      list.sort((a, b) => {
        if (priority[a.state] !== priority[b.state]) return priority[a.state] - priority[b.state]
        return (b.lastActivityAt ?? 0) - (a.lastActivityAt ?? 0)
      })
      return list
    }

    function buildSnapshot(): OpsSnapshot {
      const nowMs = Date.now()
      const { aliasToId, pathToId } = buildWorkspaceAliasMap()
      const allWorkspaces = mergeLiveWorkspaces(nowMs, aliasToId)
      const storage = buildStorageOffenders({
        triage: heavyStorageData?.triage ?? null,
        gcDryRun: gcPreviewData?.gcDryRun ?? null,
        orcaPreview: gcPreviewData?.orcaCleanupPreview ?? null,
        extraDirs: heavyStorageData?.extraDirs ?? [],
        workspacePathToId: pathToId,
        workspaceIdByAlias: aliasToId,
        workspaceRoot: currentEnv?.workspaceRoot ?? '',
        homeDir: currentEnv?.homeDir ?? ''
      })

      const intervalSec = currentEnv?.gcIntervalSec ?? Math.round(currentSettings.gcIntervalHours * 3600)
      const lastRunAt = fastData?.gcStampSec ? fastData.gcStampSec * 1000 : null
      const nextRunAt = computeNextGcAt(fastData?.gcStampSec ?? null, intervalSec, currentSettings.gcIntervalHours)

      const doctor = healthData?.doctor ?? {
        checkedAt: null,
        ok: null,
        attention: [],
        advice: [],
        error: null
      }
      const audit = healthData?.audit ?? {
        checkedAt: null,
        healthy: null,
        globalRulesInSync: null,
        globalTokens: 0,
        skillsCount: 0,
        skillsTokens: 0,
        oversizedSkills: [],
        mcpClaude: 0,
        mcpCodex: 0,
        projectsAudited: 0,
        attention: [],
        advice: [],
        flaggedProjects: [],
        error: null
      }

      // Drop dismissed fingerprints older than 24 hours.
      const dismissedSet = new Set<string>()
      for (const [fp, at] of Object.entries(persisted.dismissed)) {
        if (nowMs - at < 24 * HOUR_MS) dismissedSet.add(fp)
      }

      const ephemeralSimulators = gcPreviewData?.gcDryRun?.ephemeralSimulators ?? []
      const runningQemu = fastData?.runningQemu ?? []

      const recentBatches = mergeTrashBatches(fastData?.trashBatches ?? [])
      const alerts = evaluateAlerts({
        disk: fastData?.disk ?? null,
        jobs: fastData?.jobs ?? [],
        doctor,
        audit,
        workspaces: allWorkspaces,
        ephemeralSimulators,
        runningQemu,
        recentRuns: fastData?.hygieneHistory.runs ?? [],
        recentBatches,
        legacyTotalBytes: storage.legacyTotalBytes,
        strandedTotalBytes: storage.strandedTotalBytes,
        settings: currentSettings,
        nowMs,
        dismissedFingerprints: dismissedSet,
        escalatedMap: persisted.escalated
      })

      const activeAlerts = alerts.filter((a) => !a.dismissed)
      const waitThreshold = currentSettings.waitingAlertMinutes

      // Attach resolved workspaceId onto memory rows so clicking always opens the right workspace.
      const resolvedMemory: MemoryDiagnostics | null = memoryData
        ? {
            ...memoryData,
            worktrees: memoryData.worktrees.slice(0, 20).map((m) => ({
              ...m,
              workspaceId: aliasToId.get(m.worktreeId) ?? null
            }))
          }
        : null

      // Keep Orca cleanup preview compact inside the panel payload (sizes are already in storage.topOffenders).
      const compactPreview = gcPreviewData?.orcaCleanupPreview
        ? {
            ...gcPreviewData.orcaCleanupPreview,
            rows: gcPreviewData.orcaCleanupPreview.rows
              .filter((r) => r.outcome !== 'kept' || r.reason === 'merged')
              .slice(0, 20)
          }
        : null

      return {
        generatedAt: nowMs,
        status: !fastData ? (lastError ? 'error' : 'loading') : lastError ? 'degraded' : 'ready',
        error: lastError,
        settings: currentSettings,
        hygieneConfig:
          currentEnv?.hygieneConfig ??
          normalizeHygieneConfig({ gcIntervalHours: currentSettings.gcIntervalHours }),
        counts: {
          criticalAlerts: activeAlerts.filter((a) => a.severity === 'critical').length,
          warnAlerts: activeAlerts.filter((a) => a.severity === 'warning').length,
          workingAgents: allWorkspaces.filter((w) => w.state === 'working').length,
          waitingAgents: allWorkspaces.filter((w) => w.state === 'waiting').length,
          blockedAgents: allWorkspaces.filter((w) => w.state === 'blocked').length,
          staleWaitingAgents: allWorkspaces.filter(
            (w) => w.state === 'waiting' && (w.waitingMinutes ?? 0) >= waitThreshold
          ).length,
          reclaimableBytes: gcPreviewData?.gcDryRun?.reclaimableBytes ?? 0,
          trashBatches: recentBatches.filter((b) => b.inTrash && !b.restoredAt).length
        },
        disk: fastData?.disk ?? null,
        cleanup: {
          governor: {
            lastRunAt,
            lastManualGcAt,
            nextRunAt,
            intervalSec,
            lastState: fastData?.gcLastState ?? null,
            lastMessage: fastData?.gcLastMessage ?? null,
            dryRun: gcPreviewData?.gcDryRun ?? null,
            dryRunAt: gcPreviewUpdatedAt,
            historyRuns: fastData?.hygieneHistory.runs ?? [],
            trendDays: fastData?.hygieneHistory.trendDays ?? [],
            recentBatches,
            totalReclaimed24hBytes: fastData?.hygieneHistory.totalReclaimed24hBytes ?? 0,
            totalReclaimed7dBytes: fastData?.hygieneHistory.totalReclaimed7dBytes ?? 0,
            totalReclaimed24hItems: fastData?.hygieneHistory.totalReclaimed24hItems ?? 0
          },
          orca: {
            nextRunAt: fastData?.orcaCleanupReport.nextRunAt ?? null,
            lastRun: fastData?.orcaCleanupReport.lastRun ?? null,
            earlierRuns: fastData?.orcaCleanupReport.earlierRuns ?? [],
            preview: compactPreview,
            error: fastData?.orcaCleanupReport.error ?? null
          }
        },
        storage: {
          scannedAt: heavyStorageUpdatedAt,
          ...storage
        },
        live: {
          bots: fastData?.bots ?? [],
          totalWorkspaceCount: allWorkspaces.length,
          workspaces: allWorkspaces.slice(0, PANEL_WORKSPACES_LIMIT),
          memory: resolvedMemory,
          testDevices: {
            ephemeralSimulators,
            runningQemu,
            activeLeases: fastData?.activeLeases ?? []
          }
        },
        health: {
          jobs: fastData?.jobs ?? [],
          doctor,
          audit
        },
        alerts,
        botDispatchHistory: persisted.botDispatchHistory,
        archiveHistory: persisted.archiveHistory,
        busyActions: [...busySet],
        lastActionNotice: lastNotice
      }
    }

    async function publish(checkAutoAlerts = false, forcePost = false): Promise<OpsSnapshot> {
      latestSnapshot = buildSnapshot()
      if (checkAutoAlerts && !stopped) {
        await processAlerts(latestSnapshot)
        latestSnapshot = buildSnapshot()
      }
      const digest = JSON.stringify({ ...latestSnapshot, generatedAt: 0 })
      if (forcePost || digest !== lastPostedDigest) {
        lastPostedDigest = digest
        orca.panels.post('ops.changed', latestSnapshot)
      }
      const bar = buildStatusBarState(latestSnapshot, Date.now())
      const sig = JSON.stringify(bar)
      if (sig !== lastBarSig && orca.host.has('statusBar.update')) {
        lastBarSig = sig
        await orca.host.call('statusBar.update', { id: 'ops-radar', ...bar }).catch(() => undefined)
      }
      return latestSnapshot
    }

    async function processAlerts(snap: OpsSnapshot): Promise<void> {
      const now = Date.now()
      const active = snap.alerts.filter((a) => !a.dismissed)
      let stateDirty = false

      // 1. Desktop notifications (grouped, deduplicated per 6h)
      if (currentSettings.desktopNotifications && orca.host.has('notifications.show')) {
        const freshForNotify = active.filter(
          (a) =>
            (a.severity === 'critical' || a.severity === 'warning') &&
            now - (persisted.notified[a.fingerprint] ?? 0) >= ALERT_COOLDOWN_MS
        )
        if (freshForNotify.length > 0) {
          const top = freshForNotify[0]
          const title =
            freshForNotify.length === 1
              ? `Ops Radar · ${top.title}`
              : `Ops Radar · ${freshForNotify.length} 项告警需要关注`
          const body =
            freshForNotify.length === 1
              ? top.detail
              : freshForNotify.slice(0, 3).map((a) => `• ${a.title}`).join('\n')
          await orca.host.call('notifications.show', { title, body }).catch(() => undefined)
          for (const a of freshForNotify) persisted.notified[a.fingerprint] = now
          stateDirty = true
        }
      }

      // 2. Automatic Bot handoff (respects botAlertMode + 6h dedup cooldown)
      if (currentEnv && currentSettings.botAlertMode !== 'manual' && currentSettings.botName) {
        const eligible: OpsAlert[] = active.filter((a) => {
          const matchesMode =
            currentSettings.botAlertMode === 'all'
              ? a.severity === 'critical' || a.severity === 'warning'
              : a.severity === 'critical'
          if (!matchesMode) return false
          return now - (persisted.escalated[a.fingerprint] ?? 0) >= ALERT_COOLDOWN_MS
        })
        if (eligible.length > 0) {
          const brief = buildBotEscalationBrief({ alerts: eligible, snapshot: snap })
          const res = await collector.sendToBot(currentEnv, {
            botName: currentSettings.botName,
            subject: brief.subject,
            body: brief.body,
            mode: 'handoff',
            newChat: false
          })
          const record: BotDispatchRecord = {
            id: `${now.toString(36)}-${Math.random().toString(36).slice(2, 6)}`,
            at: now,
            botName: currentSettings.botName,
            mode: 'handoff',
            auto: true,
            subject: brief.subject,
            alertCount: eligible.length,
            ok: res.ok,
            error: res.error
          }
          persisted.botDispatchHistory = [record, ...persisted.botDispatchHistory].slice(0, DISPATCH_HISTORY_LIMIT)
          if (res.ok) {
            for (const a of eligible) persisted.escalated[a.fingerprint] = now
            lastNotice = {
              tone: 'info',
              message: `已自动将 ${eligible.length} 项告警交给 ${currentSettings.botName} 结合项目分析反馈`,
              at: now
            }
          } else {
            orca.log(`auto bot escalation failed: ${res.error ?? 'unknown'}`)
          }
          stateDirty = true
        }
      }

      if (stateDirty) await savePersisted()
    }

    async function fetchHostWorkspaces(): Promise<void> {
      if (!orca.host.has('workspace.list')) return
      try {
        const res = await orca.host.call('workspace.list', { includeStatus: true })
        hostWorkspaces = res.workspaces
      } catch {
        try {
          const res = await orca.host.call('workspace.list')
          hostWorkspaces = res.workspaces
        } catch {
          // Keep previous list on transient host failure.
        }
      }
    }

    async function refreshScope(scope: RefreshScope, checkAlerts = false, silentBusy = false): Promise<OpsSnapshot> {
      await loadPersisted()
      await readSettings()
      const tag = `refresh:${scope}`
      if (!silentBusy) {
        busySet.add(tag)
        await publish(false, true)
      }

      try {
        if (scope === 'fast' || scope === 'all' || !currentEnv) {
          const [fast] = await Promise.all([
            collector.collectFast(currentSettings.gcIntervalHours),
            fetchHostWorkspaces()
          ])
          fastData = fast
          currentEnv = fast.env
          lastError = null
          await archiveSeenBatches(fast.trashBatches)
        }

        if (currentEnv) {
          const tasks: Promise<void>[] = []
          if (scope === 'gc' || scope === 'all') {
            tasks.push(
              collector.collectGcAndPreview(currentEnv).then((gp) => {
                gcPreviewData = gp
                gcPreviewUpdatedAt = Date.now()
              })
            )
          }
          if (scope === 'storage') {
            tasks.push(
              collector.collectHeavyStorage(currentEnv).then((hs) => {
                heavyStorageData = hs
                heavyStorageUpdatedAt = Date.now()
              })
            )
          } else if (
            scope === 'all' &&
            (heavyStorageUpdatedAt === null || Date.now() - heavyStorageUpdatedAt >= HEAVY_STORAGE_REFRESH_MS)
          ) {
            tasks.push(
              collector.collectHeavyStorage(currentEnv).then((hs) => {
                heavyStorageData = hs
                heavyStorageUpdatedAt = Date.now()
              })
            )
          }
          if (scope === 'health' || scope === 'all') {
            tasks.push(
              collector.collectHealth(currentEnv).then((hl) => {
                healthData = hl
                healthUpdatedAt = Date.now()
              })
            )
          }
          if (scope === 'memory' || scope === 'all') {
            tasks.push(
              collector.collectMemory(currentEnv).then((mem) => {
                if (mem) {
                  memoryData = mem
                  memoryUpdatedAt = Date.now()
                }
              })
            )
          }
          if (tasks.length > 0) await Promise.all(tasks)
        }
      } catch (err) {
        lastError = err instanceof Error ? err.message : 'refresh_failed'
        orca.log(`refresh(${scope}) failed: ${lastError}`)
      } finally {
        busySet.delete(tag)
      }
      return publish(checkAlerts)
    }

    function scheduleNextPoll(): void {
      if (stopped) return
      clearTimeout(pollTimer)
      const delayMs = currentSettings.autoRefreshSeconds * 1000
      pollTimer = setTimeout(() => {
        void serial(async () => {
          const now = Date.now()
          const needGc = gcPreviewUpdatedAt === null || now - gcPreviewUpdatedAt >= GC_PREVIEW_REFRESH_MS
          const needHeavy = heavyStorageUpdatedAt === null || now - heavyStorageUpdatedAt >= HEAVY_STORAGE_REFRESH_MS
          const needHealth = healthUpdatedAt === null || now - healthUpdatedAt >= HEALTH_REFRESH_MS
          await refreshScope('fast', true, true)
          if (needGc) await refreshScope('gc', true, true)
          if (needHeavy) await refreshScope('storage', true, true)
          if (needHealth) await refreshScope('health', true, true)
        }).finally(() => scheduleNextPoll())
      }, delayMs)
      ;(pollTimer as unknown as { unref?: () => void })?.unref?.()
    }

    async function handleRunGc(mode: GcMode, apply: boolean): Promise<OpsSnapshot> {
      if (!currentEnv) await refreshScope('fast', false)
      if (!currentEnv) throw { code: 'unavailable' }
      const tag = `gc:${mode}:${apply ? 'apply' : 'dry'}`
      busySet.add(tag)
      await publish(false)
      try {
        const report = await collector.runGovernorGc(currentEnv, mode, apply)
        gcPreviewData = {
          ...(gcPreviewData ?? { gcDryRun: null, orcaCleanupPreview: null }),
          gcDryRun: report
        }
        gcPreviewUpdatedAt = Date.now()
        lastNotice = {
          tone: report.errors.length ? 'warning' : 'success',
          message: apply
            ? `${mode} 清理完成：回收 ${report.totalCandidates} 项 (${report.reclaimableHuman})，移入废纸篓可随时撤销`
            : `${mode} 预览完成：发现 ${report.totalCandidates} 项可回收 (${report.reclaimableHuman})，${report.protectedSkipsCount} 项受保护`,
          at: Date.now()
        }
        if (apply) {
          lastManualGcAt = Date.now()
          fastData = await collector.collectFast(currentSettings.gcIntervalHours)
          await archiveSeenBatches(fastData.trashBatches)
        }
      } finally {
        busySet.delete(tag)
      }
      return publish(apply)
    }

    async function handleRestoreBatch(manifestPath: string, apply: boolean): Promise<OpsSnapshot> {
      if (!currentEnv) await refreshScope('fast', false)
      if (!currentEnv) throw { code: 'unavailable' }
      busySet.add('restore')
      await publish(false)
      try {
        const res = await collector.restoreTrashBatch(currentEnv, manifestPath, apply)
        lastNotice = {
          tone: res.ok ? 'success' : 'danger',
          message: res.message,
          at: Date.now()
        }
        if (apply && res.ok) {
          fastData = await collector.collectFast(currentSettings.gcIntervalHours)
        }
      } finally {
        busySet.delete('restore')
      }
      return publish(false)
    }

    async function handleToggleKeep(targetPath: string, keep: boolean): Promise<OpsSnapshot> {
      if (!currentEnv) await refreshScope('fast', false)
      if (!currentEnv) throw { code: 'unavailable' }
      busySet.add('keep')
      await publish(false)
      try {
        const res = await collector.toggleKeepMarker(currentEnv, targetPath, keep)
        // Optimistically update heavyStorageData pinned state without forcing an 18s rescan.
        if (heavyStorageData) {
          for (const d of heavyStorageData.extraDirs) {
            if (d.path === targetPath) d.pinned = res.pinned
          }
          for (const proj of heavyStorageData.triage?.projects ?? []) {
            for (const item of proj.largest_items ?? []) {
              if (item.path === targetPath) item.pinned = res.pinned
            }
          }
        }
        lastNotice = {
          tone: 'success',
          message: res.pinned ? `已写入 .keep 保护：${targetPath}` : `已移除 .keep 保护：${targetPath}`,
          at: Date.now()
        }
      } finally {
        busySet.delete('keep')
      }
      return publish(false)
    }

    async function handleArchiveWorktree(worktreePath: string, topic?: string): Promise<OpsSnapshot> {
      await loadPersisted()
      if (!currentEnv) await refreshScope('fast', false)
      if (!currentEnv) throw { code: 'unavailable' }
      busySet.add('archive')
      await publish(false)
      try {
        const res = await collector.archiveWorktree(currentEnv, worktreePath, topic)
        const now = Date.now()
        const rec: ArchiveRecord = {
          id: `${now.toString(36)}-${Math.random().toString(36).slice(2, 6)}`,
          at: now,
          sourcePath: worktreePath,
          archiveDir: res.archiveDir,
          topic: res.topic,
          totalBytes: res.totalBytes,
          ok: res.ok,
          error: res.ok ? null : res.message
        }
        persisted.archiveHistory = [rec, ...persisted.archiveHistory].slice(0, ARCHIVE_HISTORY_LIMIT)
        await savePersisted()
        lastNotice = {
          tone: res.ok ? 'success' : 'danger',
          message: res.message,
          at: now
        }
      } finally {
        busySet.delete('archive')
      }
      return publish(false)
    }

    async function handleRunOrcaCleanup(apply: boolean): Promise<OpsSnapshot> {
      if (!currentEnv) await refreshScope('fast', false)
      if (!currentEnv) throw { code: 'unavailable' }
      busySet.add('orca-cleanup')
      await publish(false)
      try {
        const res = await collector.runOrcaCleanup(currentEnv, apply)
        lastNotice = {
          tone: res.ok ? 'success' : 'danger',
          message: `Orca 工作树清理${apply ? '' : '预览'}：${res.summary}`,
          at: Date.now()
        }
        fastData = await collector.collectFast(currentSettings.gcIntervalHours)
        gcPreviewData = await collector.collectGcAndPreview(currentEnv)
        gcPreviewUpdatedAt = Date.now()
      } finally {
        busySet.delete('orca-cleanup')
      }
      return publish(false)
    }

    async function handleEscalateBot(input: {
      botName?: string
      alertIds?: string[]
      customNote?: string
      mode?: 'handoff' | 'status'
      newChat?: boolean
    }): Promise<OpsSnapshot> {
      await loadPersisted()
      if (!currentEnv) await refreshScope('fast', false)
      if (!currentEnv) throw { code: 'unavailable' }

      const botName = (input.botName || currentSettings.botName || 'Ram').trim()
      const mode = input.mode ?? 'handoff'
      const snap = buildSnapshot()
      const selectedAlerts =
        Array.isArray(input.alertIds) && input.alertIds.length > 0
          ? snap.alerts.filter((a) => input.alertIds!.includes(a.id))
          : snap.alerts.filter((a) => !a.dismissed)

      const brief = buildBotEscalationBrief({
        alerts: selectedAlerts,
        snapshot: snap,
        customNote: input.customNote
      })

      busySet.add('bot-send')
      await publish(false)
      try {
        const res = await collector.sendToBot(currentEnv, {
          botName,
          subject: brief.subject,
          body: brief.body,
          mode,
          newChat: Boolean(input.newChat)
        })
        const now = Date.now()
        const record: BotDispatchRecord = {
          id: `${now.toString(36)}-${Math.random().toString(36).slice(2, 6)}`,
          at: now,
          botName,
          mode,
          auto: false,
          subject: brief.subject,
          alertCount: selectedAlerts.length,
          ok: res.ok,
          error: res.error
        }
        persisted.botDispatchHistory = [record, ...persisted.botDispatchHistory].slice(0, DISPATCH_HISTORY_LIMIT)
        if (res.ok) {
          for (const a of selectedAlerts) persisted.escalated[a.fingerprint] = now
          lastNotice = {
            tone: 'success',
            message:
              mode === 'handoff'
                ? `已交给 ${botName} 分析（${selectedAlerts.length} 项告警），请在 ${botName} 对话中查看反馈`
                : `已向 ${botName} 同步状态摘要`,
            at: now
          }
        } else {
          lastNotice = {
            tone: 'danger',
            message: `发送给 ${botName} 失败：${res.error ?? 'unknown'}`,
            at: now
          }
        }
        await savePersisted()
      } finally {
        busySet.delete('bot-send')
      }
      return publish(false)
    }

    // ─── Panel Request Handlers ───
    orca.panels.handle('ops.snapshot', async () => {
      if (!fastData) {
        return serial(() => refreshScope('fast', false))
      }
      return latestSnapshot
    })

    orca.panels.handle('ops.refresh', (input) =>
      serial(async () => {
        const scope: RefreshScope = input?.scope ?? 'all'
        if (scope === 'memory' && memoryUpdatedAt && Date.now() - memoryUpdatedAt < MEMORY_CACHE_MS && memoryData) {
          return latestSnapshot
        }
        return refreshScope(scope, scope === 'all' || scope === 'fast')
      })
    )

    orca.panels.handle('ops.runGc', (input) =>
      serial(() => handleRunGc(input.mode, Boolean(input.apply)))
    )

    orca.panels.handle('ops.restoreBatch', (input) =>
      serial(() => handleRestoreBatch(input.manifestPath, Boolean(input.apply)))
    )

    orca.panels.handle('ops.toggleKeep', (input) =>
      serial(() => handleToggleKeep(input.path, Boolean(input.keep)))
    )

    orca.panels.handle('ops.archiveWorktree', (input) =>
      serial(() => handleArchiveWorktree(input.path, input.topic))
    )

    orca.panels.handle('ops.openWorkspace', async ({ workspaceId }) => {
      if (!workspaceId || !orca.host.has('workspace.open')) return { opened: false }
      try {
        await orca.host.call('workspace.open', { workspaceId })
        return { opened: true }
      } catch {
        return { opened: false }
      }
    })

    orca.panels.handle('ops.runOrcaCleanup', (input) =>
      serial(() => handleRunOrcaCleanup(Boolean(input.apply)))
    )

    orca.panels.handle('ops.escalateBot', (input) => serial(() => handleEscalateBot(input ?? {})))

    orca.panels.handle('ops.updateSetting', (input) =>
      serial(async () => {
        await orca.settings.set(input.key, input.value)
        await readSettings()
        if (input.key === 'autoRefreshSeconds') scheduleNextPoll()
        return publish(false, true)
      })
    )

    orca.panels.handle('ops.updateSettings', (input) =>
      serial(async () => {
        const entries = Object.entries(input ?? {}) as [keyof OpsSettings, string | number | boolean][]
        const allowed: Set<keyof OpsSettings> = new Set([
          'botName',
          'botAlertMode',
          'desktopNotifications',
          'diskWarnFreePercent',
          'waitingAlertMinutes',
          'gcSpikeGiB',
          'gcIntervalHours',
          'autoRefreshSeconds'
        ])
        for (const [k, v] of entries) {
          if (!allowed.has(k) || v === undefined) continue
          await orca.settings.set(k, v)
        }
        await readSettings()
        scheduleNextPoll()
        lastNotice = {
          tone: 'success',
          message: '监测与通知设置已保存并生效',
          at: Date.now()
        }
        return publish(false, true)
      })
    )

    orca.panels.handle('ops.saveHygieneConfig', (input) =>
      serial(async () => {
        if (!currentEnv) await refreshScope('fast', false, true)
        if (!currentEnv) throw { code: 'unavailable' }
        busySet.add('settings')
        await publish(false, true)
        try {
          const base: HygieneConfig = input?.reset
            ? { ...DEFAULT_HYGIENE_CONFIG, configPath: currentEnv.hygieneConfig.configPath }
            : { ...currentEnv.hygieneConfig, ...(input ?? {}) }
          const saved = await collector.saveHygieneConfig(currentEnv, base)
          currentEnv = {
            ...currentEnv,
            gcIntervalSec: Math.round(saved.gcIntervalHours * 3600),
            hygieneConfig: saved
          }
          await orca.settings.set('gcIntervalHours', saved.gcIntervalHours).catch(() => undefined)
          await readSettings()
          gcPreviewData = await collector.collectGcAndPreview(currentEnv)
          gcPreviewUpdatedAt = Date.now()
          lastNotice = {
            tone: 'success',
            message: input?.reset
              ? '已恢复默认清理周期与 TTL 规则（4h / 3d / 7d / 12h）'
              : `已保存清理策略（每 ${saved.gcIntervalHours}h 清理 · scratch ${saved.scratchTtlDays}d · tests ${saved.testsTtlDays}d）`,
            at: Date.now()
          }
        } finally {
          busySet.delete('settings')
        }
        return publish(false, true)
      })
    )

    orca.panels.handle('ops.dismissAlert', (input) =>
      serial(async () => {
        await loadPersisted()
        persisted.dismissed[input.fingerprint] = Date.now()
        await savePersisted()
        return publish(false)
      })
    )

    // ─── Palette Commands ───
    orca.commands.register('ops.summary', async () => {
      const snap = fastData ? latestSnapshot : await serial(() => refreshScope('fast', false))
      const cd = snap.cleanup.governor.nextRunAt
        ? formatDurationCompact(snap.cleanup.governor.nextRunAt - Date.now(), true)
        : '未知'
      const disk = snap.disk ? `磁盘余 ${formatBytes(snap.disk.availBytes)} (${snap.disk.freePct.toFixed(1)}%)` : '磁盘未知'
      return `Ops Radar · 下次 GC: ${cd} · 近24h回收 ${formatBytes(snap.cleanup.governor.totalReclaimed24hBytes)} · ${disk} · 紧急 ${snap.counts.criticalAlerts} / 提醒 ${snap.counts.warnAlerts}`
    })

    orca.commands.register('ops.refresh', async () => {
      const snap = await serial(() => refreshScope('all', true))
      return `Ops Radar 已刷新：${snap.alerts.filter((a) => !a.dismissed).length} 条活跃告警，${snap.storage.topOffenders.length} 条空间大户记录。`
    })

    orca.commands.register('ops.gcPreview', async () => {
      const snap = await serial(() => handleRunGc('daily', false))
      return snap.lastActionNotice?.message ?? 'GC 预览完成'
    })

    orca.commands.register('ops.gcDaily', async () => {
      const snap = await serial(() => handleRunGc('daily', true))
      return snap.lastActionNotice?.message ?? 'Daily GC 完成'
    })

    orca.commands.register('ops.escalateBot', async () => {
      const snap = await serial(() => handleEscalateBot({ mode: 'handoff' }))
      return snap.lastActionNotice?.message ?? '已发送给 Bot'
    })

    function scheduleEventPublish(): void {
      if (stopped || eventPublishTimer !== undefined) return
      eventPublishTimer = setTimeout(() => {
        eventPublishTimer = undefined
        if (!stopped) void publish(false)
      }, EVENT_PUBLISH_DEBOUNCE_MS)
      ;(eventPublishTimer as unknown as { unref?: () => void })?.unref?.()
    }

    // ─── Live Orca Event Subscriptions ───
    const subscriptions: Disposable[] = [
      orca.events.on('agent.status.changed', (ev) => {
        const wsId = ev.workspaceListId ?? ev.workspaceId
        if (!wsId) return
        const mapped = mapHostState(ev.mainAgent?.state ?? ev.state)
        const startedAt = ev.mainAgent?.stateStartedAt ?? ev.receivedAt ?? Date.now()
        liveOverrides.set(wsId, { state: mapped, stateStartedAt: startedAt, receivedAt: Date.now() })
        latestSnapshot = buildSnapshot()
        scheduleEventPublish()
      }),
      orca.events.on('workspace.created', () => {
        void serial(() => refreshScope('fast', false))
      }),
      orca.events.on('workspace.removed', (ev) => {
        const wsId = ev.workspaceListId ?? ev.workspaceId
        if (wsId) liveOverrides.delete(wsId)
        void serial(() => refreshScope('fast', false))
      }),
      orca.settings.onDidChange(() => {
        void serial(async () => {
          await readSettings()
          await publish(false)
          scheduleNextPoll()
        })
      })
    ]

    stopCurrent = () => {
      stopped = true
      clearTimeout(pollTimer)
      clearTimeout(eventPublishTimer)
      eventPublishTimer = undefined
      for (const s of subscriptions) s.dispose()
    }

    // Boot: fast telemetry (~300ms) -> silent background warmup for gc, health, and heavy triage/du.
    void serial(() => refreshScope('fast', true, true))
      .then(() => (!stopped ? serial(() => refreshScope('gc', true, true)) : undefined))
      .then(() => (!stopped ? serial(() => refreshScope('health', true, true)) : undefined))
      .then(() => (!stopped ? serial(() => refreshScope('storage', true, true)) : undefined))
      .finally(() => scheduleNextPoll())
  }
}

export default createWorker()

export function deactivate(): void {
  stopCurrent?.()
  stopCurrent = null
}
