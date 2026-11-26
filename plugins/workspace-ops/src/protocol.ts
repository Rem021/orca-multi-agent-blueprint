// Shared protocol between the trusted worker (src/worker.ts) and the HTML panel (panel/app.js).

export type BotAlertMode = 'critical' | 'manual' | 'all'
export type AlertSeverity = 'critical' | 'warning' | 'info'
export type RefreshScope = 'fast' | 'storage' | 'memory' | 'health' | 'all'
export type GcMode = 'daily' | 'session-end' | 'deep'

export type OpsSettings = {
  botName: string
  botAlertMode: BotAlertMode
  desktopNotifications: boolean
  diskWarnFreePercent: number
  waitingAlertMinutes: number
  gcSpikeGiB: number
  gcIntervalHours: number
  autoRefreshSeconds: number
}

export type DiskStatus = {
  mount: string
  totalBytes: number
  usedBytes: number
  availBytes: number
  capacityPct: number
  freePct: number
  storageMode: 'internal' | 'external' | 'auto'
  workspaceRoot: string
}

export type GcCandidate = {
  category: string
  path: string
  sizeBytes: number
  sizeHuman: string
  ageHours: number | null
  reason: string
}

export type GcCategoryStat = {
  category: string
  count: number
  bytes: number
}

export type GcReportSummary = {
  mode: string
  execution: string
  totalCandidates: number
  reclaimableBytes: number
  reclaimableHuman: string
  protectedSkipsCount: number
  categories: GcCategoryStat[]
  candidates: GcCandidate[]
  ephemeralSimulators: SimDevice[]
  launchservicesGhostsCount: number
  manifestPath: string | null
  errors: string[]
}

export type HygieneLogRun = {
  at: number
  state: 'ok' | 'warn' | 'fail'
  reclaimedItems: number
  reclaimedBytes: number
  protectedSkips: number
  ghostApps: number
  raw: string
}

export type TrendDay = {
  date: string
  reclaimedBytes: number
  reclaimedItems: number
  runs: number
}

export type TrashItem = {
  index: number
  category: string
  reason: string
  originalPath: string
  stagedPath: string
  sizeBytes: number
  stagedAt: number | null
  project: string | null
}

export type TrashBatch = {
  id: string
  manifestPath: string
  createdAt: number
  mode: string
  inTrash: boolean
  totalItems: number
  totalBytes: number
  largestItem: TrashItem | null
  categories: GcCategoryStat[]
  items: TrashItem[]
}

export type OrcaCleanupRow = {
  worktreeId: string
  repoName: string
  displayName: string
  branch: string
  path: string
  sizeBytes: number | null
  outcome: string
  why: string
  reason: string | null
}

export type OrcaCleanupRun = {
  id: string
  trigger: string
  startedAt: number
  finishedAt: number
  removed: number
  archived: number
  kept: number
  failed: number
  freedBytes: number
  rows: OrcaCleanupRow[]
}

export type OrcaCleanupPreview = {
  evaluatedAt: number
  wouldRemoveCount: number
  wouldRemoveBytes: number
  keptCount: number
  rows: OrcaCleanupRow[]
}

export type StorageOffenderKind =
  | 'legacy_work'
  | 'stranded_worktree'
  | 'gc_candidate'
  | 'worktree'
  | 'shared_data'
  | 'derived_data'

export type StorageOffender = {
  id: string
  kind: StorageOffenderKind
  name: string
  project: string
  path: string
  sizeBytes: number
  files: number | null
  ageDays: number | null
  ttlRemainingHours: number | null
  pinned: boolean
  canToggleKeep: boolean
  workspaceId: string | null
  reason: string
}

export type BotChatSummary = {
  id: string
  title: string
  home: boolean
  closed: boolean
  lastActivityAt: number | null
  lastAsked: string
  lastSaid: string
}

export type BotInfo = {
  id: string
  name: string
  agent: string
  paused: boolean
  specialty: string
  chatCount: number
  chats: BotChatSummary[]
}

export type LiveWorkspaceState = 'working' | 'waiting' | 'blocked' | 'done' | 'active' | 'inactive'

export type LiveWorkspace = {
  id: string
  name: string
  project: string
  kind: 'worktree' | 'folder'
  path: string
  branch: string | null
  state: LiveWorkspaceState
  stateStartedAt: number | null
  lastActivityAt: number | null
  unread: boolean
  terminalCount: number
  bot: string | null
  linkedPR: string | null
  agentType: string | null
  lastMessage: string | null
  waitingMinutes: number | null
  memoryBytes: number | null
  cpuPct: number | null
}

export type MemoryWorktreeStat = {
  worktreeId: string
  worktreeName: string
  repoName: string
  cpuPct: number
  memoryBytes: number
  agents: { agent: string; name: string; cpuPct: number; memoryBytes: number }[]
}

export type MemoryDiagnostics = {
  collectedAt: number
  appMemoryBytes: number
  appCpuPct: number
  totalMemoryBytes: number
  totalCpuPct: number
  worktrees: MemoryWorktreeStat[]
}

export type SimDevice = {
  name: string
  udid: string
  state: string
  reason?: string
}

export type SimLease = {
  id: string
  udid: string | null
  owner: string | null
  pid: number | null
  createdAt: string | null
}

export type QemuProc = {
  pid: number
  command: string
}

export type LaunchJobStatus = {
  name: string
  state: 'ok' | 'warn' | 'fail' | 'stale' | 'skipped' | 'running' | 'absent' | 'unknown'
  time: string
  lastOk: string
  message: string
  seconds: number | null
}

export type DoctorSummary = {
  checkedAt: number | null
  ok: boolean | null
  attention: string[]
  advice: string[]
  error: string | null
}

export type AuditProjectSummary = {
  name: string
  path: string
  agentsLines: number
  agentsTokens: number
  issues: string[]
  smells: string[]
}

export type AuditSummary = {
  checkedAt: number | null
  healthy: boolean | null
  globalRulesInSync: boolean | null
  globalTokens: number
  skillsCount: number
  skillsTokens: number
  oversizedSkills: string[]
  mcpClaude: number
  mcpCodex: number
  projectsAudited: number
  attention: string[]
  advice: string[]
  flaggedProjects: AuditProjectSummary[]
  error: string | null
}

export type OpsAlert = {
  id: string
  fingerprint: string
  severity: AlertSeverity
  category: 'disk' | 'backup' | 'cleanup' | 'agent' | 'device' | 'doctor' | 'audit'
  title: string
  detail: string
  metric: string | null
  workspaceId: string | null
  path: string | null
  suggestedAction: string | null
  dismissed: boolean
  escalatedAt: number | null
}

export type BotDispatchRecord = {
  id: string
  at: number
  botName: string
  mode: 'handoff' | 'status'
  auto: boolean
  subject: string
  alertCount: number
  ok: boolean
  error: string | null
}

export type ActionNotice = {
  tone: 'success' | 'warning' | 'danger' | 'info'
  message: string
  at: number
}

export type OpsSnapshot = {
  generatedAt: number
  status: 'loading' | 'ready' | 'degraded' | 'error'
  error: string | null
  settings: OpsSettings
  counts: {
    criticalAlerts: number
    warnAlerts: number
    workingAgents: number
    waitingAgents: number
    blockedAgents: number
    staleWaitingAgents: number
    reclaimableBytes: number
    trashBatches: number
  }
  disk: DiskStatus | null
  cleanup: {
    governor: {
      lastRunAt: number | null
      nextRunAt: number | null
      intervalSec: number
      lastState: string | null
      lastMessage: string | null
      dryRun: GcReportSummary | null
      dryRunAt: number | null
      historyRuns: HygieneLogRun[]
      trendDays: TrendDay[]
      recentBatches: TrashBatch[]
      totalReclaimed24hBytes: number
      totalReclaimed7dBytes: number
      totalReclaimed24hItems: number
    }
    orca: {
      nextRunAt: number | null
      lastRun: OrcaCleanupRun | null
      earlierRuns: OrcaCleanupRun[]
      preview: OrcaCleanupPreview | null
      error: string | null
    }
  }
  storage: {
    scannedAt: number | null
    topOffenders: StorageOffender[]
    legacyTotalBytes: number
    legacyCount: number
    strandedWorktreeCount: number
    strandedTotalBytes: number
    categoryBreakdown: GcCategoryStat[]
  }
  live: {
    bots: BotInfo[]
    workspaces: LiveWorkspace[]
    memory: MemoryDiagnostics | null
    testDevices: {
      ephemeralSimulators: SimDevice[]
      runningQemu: QemuProc[]
      activeLeases: SimLease[]
    }
  }
  health: {
    jobs: LaunchJobStatus[]
    doctor: DoctorSummary
    audit: AuditSummary
  }
  alerts: OpsAlert[]
  botDispatchHistory: BotDispatchRecord[]
  busyActions: string[]
  lastActionNotice: ActionNotice | null
}

declare module 'orca' {
  interface PluginRequests {
    'ops.snapshot': { input: undefined; output: OpsSnapshot }
    'ops.refresh': { input: { scope?: RefreshScope }; output: OpsSnapshot }
    'ops.runGc': { input: { mode: GcMode; apply: boolean }; output: OpsSnapshot }
    'ops.restoreBatch': { input: { manifestPath: string; apply: boolean }; output: OpsSnapshot }
    'ops.toggleKeep': { input: { path: string; keep: boolean }; output: OpsSnapshot }
    'ops.openWorkspace': { input: { workspaceId: string }; output: { opened: boolean } }
    'ops.runOrcaCleanup': { input: { apply: boolean }; output: OpsSnapshot }
    'ops.escalateBot': {
      input: {
        botName?: string
        alertIds?: string[]
        customNote?: string
        mode?: 'handoff' | 'status'
        newChat?: boolean
      }
      output: OpsSnapshot
    }
    'ops.updateSetting': {
      input: { key: keyof OpsSettings; value: string | number | boolean }
      output: OpsSnapshot
    }
    'ops.dismissAlert': { input: { fingerprint: string }; output: OpsSnapshot }
  }

  interface PluginPanelEvents {
    'ops.changed': OpsSnapshot
  }
}
