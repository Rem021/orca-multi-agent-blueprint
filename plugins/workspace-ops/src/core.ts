// Pure domain logic for Workspace Ops Radar: zero filesystem or subprocess I/O.

import type {
  AuditSummary,
  DiskStatus,
  DoctorSummary,
  GcCategoryStat,
  GcMode,
  GcReportSummary,
  HygieneConfig,
  HygieneLogRun,
  LaunchJobStatus,
  LiveWorkspace,
  OpsAlert,
  OpsSettings,
  OpsSnapshot,
  OrcaCleanupPreview,
  QemuProc,
  SimDevice,
  StorageOffender,
  TrashBatch,
  TrendDay
} from './protocol.ts'

export const DAY_MS = 24 * 60 * 60_000
export const HOUR_MS = 60 * 60_000
export const MIN_MS = 60_000

const UNIT_MULTIPLIERS: Record<string, number> = {
  B: 1,
  K: 1024,
  KB: 1000,
  KIB: 1024,
  M: 1024 ** 2,
  MB: 1000 ** 2,
  MIB: 1024 ** 2,
  G: 1024 ** 3,
  GB: 1000 ** 3,
  GIB: 1024 ** 3,
  T: 1024 ** 4,
  TB: 1000 ** 4,
  TIB: 1024 ** 4
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes
  let idx = 0
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024
    idx += 1
  }
  const decimals = idx === 0 ? 0 : value >= 100 ? 0 : value >= 10 ? 1 : 2
  return `${value.toFixed(decimals)} ${units[idx]}`
}

export function parseHumanBytes(raw: string): number {
  const m = raw.trim().match(/^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)$/)
  if (!m) return 0
  const num = Number(m[1])
  const unit = m[2].toUpperCase()
  const mult = UNIT_MULTIPLIERS[unit]
  if (!Number.isFinite(num) || !mult) return 0
  return Math.round(num * mult)
}

export function formatDurationCompact(ms: number, zh = true): string {
  if (!Number.isFinite(ms) || ms <= 0) return zh ? '已到期' : 'Due now'
  const totalMin = Math.ceil(ms / MIN_MS)
  if (totalMin < 60) return zh ? `${totalMin} 分钟` : `${totalMin}m`
  const hours = Math.floor(totalMin / 60)
  const mins = totalMin % 60
  if (hours < 24) {
    return mins > 0 ? (zh ? `${hours}h ${mins}m` : `${hours}h ${mins}m`) : `${hours}h`
  }
  const days = Math.floor(hours / 24)
  const remH = hours % 24
  return remH > 0 ? `${days}d ${remH}h` : `${days}d`
}

export function computeNextGcAt(
  lastStampSec: number | null,
  intervalSec: number,
  fallbackHours: number
): number | null {
  if (!lastStampSec || !Number.isFinite(lastStampSec) || lastStampSec <= 0) return null
  const effectiveSec = intervalSec > 0 ? intervalSec : Math.round(fallbackHours * 3600)
  return (lastStampSec + effectiveSec) * 1000
}

/** Parse `df -k <path>` output (macOS/POSIX 1024-byte blocks) into DiskStatus. */
export function parseDfOutput(
  stdout: string,
  storageMode: 'internal' | 'external' | 'auto',
  workspaceRoot: string
): DiskStatus | null {
  const lines = stdout
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter(Boolean)
  if (lines.length < 2) return null
  const row = lines[lines.length - 1]
  // Filesystem 1024-blocks Used Available Capacity ... Mounted on
  const cols = row.split(/\s+/)
  if (cols.length < 6) return null
  const totalBlocks = Number(cols[1])
  const usedBlocks = Number(cols[2])
  const availBlocks = Number(cols[3])
  const capMatch = cols[4].match(/^(\d+)%$/)
  if (!Number.isFinite(totalBlocks) || !Number.isFinite(availBlocks) || totalBlocks <= 0) return null
  const totalBytes = totalBlocks * 1024
  const usedBytes = Number.isFinite(usedBlocks) ? usedBlocks * 1024 : Math.max(0, totalBytes - availBlocks * 1024)
  const availBytes = availBlocks * 1024
  const freePct = Math.max(0, Math.min(100, Math.round((availBytes / totalBytes) * 1000) / 10))
  const capacityPct = capMatch ? Number(capMatch[1]) : Math.round((1 - availBytes / totalBytes) * 100)
  const mount = cols[cols.length - 1]
  return {
    mount,
    totalBytes,
    usedBytes,
    availBytes,
    capacityPct,
    freePct,
    storageMode,
    workspaceRoot
  }
}

const HYGIENE_GC_LINE_RE =
  /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+gc\s+rc=\d+\s+state=(ok|warn|fail):\s+Cache GC:\s+reclaimed\s+(\d+)\s+item\(s\)\s+\(([^)]+)\)(?:,\s+unregistered\s+(\d+)\s+ghost\s+app\(s\))?,\s+(\d+)\s+protected\s+item\(s\)\s+skipped/
const HYGIENE_GC_FAIL_RE =
  /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+gc\s+rc=\d+\s+state=(warn|fail):\s+(.+)$/

function parseLocalDateTime(raw: string): number | null {
  const isoLike = raw.trim().replace(' ', 'T')
  const parsed = Date.parse(isoLike)
  return Number.isFinite(parsed) ? parsed : null
}

/** Extract recent GC executions and a 7-day daily reclamation chart from agent-hygiene.log. */
export function parseHygieneLog(
  text: string,
  nowMs: number,
  maxRuns = 30
): {
  runs: HygieneLogRun[]
  trendDays: TrendDay[]
  totalReclaimed24hBytes: number
  totalReclaimed7dBytes: number
  totalReclaimed24hItems: number
} {
  const runs: HygieneLogRun[] = []
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(HYGIENE_GC_LINE_RE)
    if (m) {
      const at = parseLocalDateTime(m[1])
      if (at === null) continue
      runs.push({
        at,
        state: m[2] as 'ok' | 'warn' | 'fail',
        reclaimedItems: Number(m[3]) || 0,
        reclaimedBytes: parseHumanBytes(m[4]),
        ghostApps: m[5] ? Number(m[5]) || 0 : 0,
        protectedSkips: Number(m[6]) || 0,
        raw: line.trim()
      })
      continue
    }
    const fm = line.match(HYGIENE_GC_FAIL_RE)
    if (fm) {
      const at = parseLocalDateTime(fm[1])
      if (at === null) continue
      runs.push({
        at,
        state: fm[2] as 'warn' | 'fail',
        reclaimedItems: 0,
        reclaimedBytes: 0,
        ghostApps: 0,
        protectedSkips: 0,
        raw: fm[3].trim()
      })
    }
  }
  runs.sort((a, b) => b.at - a.at)

  // Build 7-day daily buckets ending today (local calendar dates).
  const dayMap = new Map<string, TrendDay>()
  const formatDayKey = (ms: number): string => {
    const d = new Date(ms)
    const y = d.getFullYear()
    const mo = String(d.getMonth() + 1).padStart(2, '0')
    const da = String(d.getDate()).padStart(2, '0')
    return `${y}-${mo}-${da}`
  }
  const orderedKeys: string[] = []
  for (let i = 6; i >= 0; i--) {
    const key = formatDayKey(nowMs - i * DAY_MS)
    orderedKeys.push(key)
    dayMap.set(key, { date: key, reclaimedBytes: 0, reclaimedItems: 0, runs: 0 })
  }

  let totalReclaimed24hBytes = 0
  let totalReclaimed7dBytes = 0
  let totalReclaimed24hItems = 0

  for (const run of runs) {
    const age = nowMs - run.at
    if (age >= 0 && age <= DAY_MS) {
      totalReclaimed24hBytes += run.reclaimedBytes
      totalReclaimed24hItems += run.reclaimedItems
    }
    if (age >= 0 && age <= 7 * DAY_MS) {
      totalReclaimed7dBytes += run.reclaimedBytes
    }
    const key = formatDayKey(run.at)
    const bucket = dayMap.get(key)
    if (bucket) {
      bucket.reclaimedBytes += run.reclaimedBytes
      bucket.reclaimedItems += run.reclaimedItems
      bucket.runs += 1
    }
  }

  return {
    runs: runs.slice(0, maxRuns),
    trendDays: orderedKeys.map((k) => dayMap.get(k)!),
    totalReclaimed24hBytes,
    totalReclaimed7dBytes,
    totalReclaimed24hItems
  }
}

export function inferProjectFromPath(filePath: string): string {
  const mProj = filePath.match(/\/AI\/Projects\/([^/]+)/)
  if (mProj) return mProj[1]
  const mWt = filePath.match(/\/AI\/(?:Orca|Codex|Claude)\/Worktrees\/([^/]+)/)
  if (mWt) return mWt[1]
  if (filePath.includes('/Xcode/DerivedData/')) return 'Xcode DerivedData'
  if (filePath.includes('/AI/Data/')) return 'AI/Data'
  if (filePath.includes('/.claude/')) return 'Claude'
  if (filePath.includes('/.codex/')) return 'Codex'
  if (filePath.includes('/.gemini/')) return 'Gemini'
  return 'System'
}

export type TriageRaw = {
  total_legacy_items?: number
  total_legacy_bytes?: number
  projects?: {
    project?: string
    work_path?: string
    largest_items?: {
      name?: string
      path?: string
      size_bytes?: number
      files?: number
      age_days?: number
      pinned?: boolean
    }[]
  }[]
  worktree_stranded_work?: {
    worktree?: string
    work_path?: string
    size_bytes?: number
    files?: number
    age_days?: number
  }[]
}

export function buildStorageOffenders(params: {
  triage: TriageRaw | null
  gcDryRun: GcReportSummary | null
  orcaPreview: OrcaCleanupPreview | null
  extraDirs: { kind: 'shared_data' | 'derived_data'; name: string; path: string; sizeBytes: number; pinned: boolean }[]
  workspacePathToId: Map<string, string>
  workspaceIdByAlias?: Map<string, string>
  workspaceRoot: string
  homeDir?: string
}): {
  topOffenders: StorageOffender[]
  legacyTotalBytes: number
  legacyCount: number
  strandedWorktreeCount: number
  strandedTotalBytes: number
  categoryBreakdown: GcCategoryStat[]
} {
  const byPath = new Map<string, StorageOffender>()
  const categoryTotals = new Map<string, { count: number; bytes: number }>()
  const addCategory = (cat: string, bytes: number) => {
    const prev = categoryTotals.get(cat) ?? { count: 0, bytes: 0 }
    prev.count += 1
    prev.bytes += Math.max(0, bytes)
    categoryTotals.set(cat, prev)
  }

  const inferredHome =
    params.homeDir ||
    (params.workspaceRoot.match(/^(\/Users\/[^/]+)/)?.[1] ?? '')
  const canKeepPath = (p: string) => isAllowedKeepTarget(p, params.workspaceRoot, inferredHome)

  const findWsId = (p: string, fallbackId?: string): string | null => {
    if (fallbackId && params.workspaceIdByAlias?.has(fallbackId)) {
      return params.workspaceIdByAlias.get(fallbackId) ?? null
    }
    if (params.workspacePathToId.has(p)) return params.workspacePathToId.get(p) ?? null
    for (const [wsPath, id] of params.workspacePathToId.entries()) {
      if (p === wsPath || p.startsWith(`${wsPath}/`)) return id
    }
    return fallbackId ?? null
  }

  let legacyTotalBytes = 0
  let legacyCount = 0
  let strandedWorktreeCount = 0
  let strandedTotalBytes = 0

  if (params.triage) {
    legacyTotalBytes = Number(params.triage.total_legacy_bytes ?? 0)
    legacyCount = Number(params.triage.total_legacy_items ?? 0)
    for (const proj of params.triage.projects ?? []) {
      const projName = proj.project || 'Unknown'
      for (const item of proj.largest_items ?? []) {
        if (!item.path) continue
        const bytes = Number(item.size_bytes ?? 0)
        addCategory('legacy_work', bytes)
        byPath.set(item.path, {
          id: `legacy:${item.path}`,
          kind: 'legacy_work',
          name: item.name || item.path.split('/').pop() || item.path,
          project: projName,
          path: item.path,
          sizeBytes: bytes,
          files: typeof item.files === 'number' ? item.files : null,
          ageDays: typeof item.age_days === 'number' ? item.age_days : null,
          ttlRemainingHours: null,
          pinned: Boolean(item.pinned),
          canToggleKeep: canKeepPath(item.path),
          canArchive: false,
          workspaceId: findWsId(item.path),
          reason: 'Grandfathered work/* (never auto-deleted)'
        })
      }
    }
    for (const stranded of params.triage.worktree_stranded_work ?? []) {
      if (!stranded.work_path) continue
      const bytes = Number(stranded.size_bytes ?? 0)
      strandedWorktreeCount += 1
      strandedTotalBytes += bytes
      addCategory('stranded_worktree', bytes)
      byPath.set(stranded.work_path, {
        id: `stranded:${stranded.work_path}`,
        kind: 'stranded_worktree',
        name: `${stranded.worktree || stranded.work_path.split('/').slice(-2).join('/')}/work`,
        project: inferProjectFromPath(stranded.work_path),
        path: stranded.work_path,
        sizeBytes: bytes,
        files: typeof stranded.files === 'number' ? stranded.files : null,
        ageDays: typeof stranded.age_days === 'number' ? stranded.age_days : null,
        ttlRemainingHours: null,
        pinned: false,
        canToggleKeep: canKeepPath(stranded.work_path),
        canArchive: false,
        workspaceId: findWsId(stranded.work_path),
        reason: 'Stranded work/ inside worktree'
      })
    }
  }

  if (params.gcDryRun) {
    for (const cand of params.gcDryRun.candidates) {
      if (!cand.path || cand.sizeBytes <= 0) continue
      addCategory(cand.category, cand.sizeBytes)
      const existing = byPath.get(cand.path)
      if (!existing || cand.sizeBytes > existing.sizeBytes) {
        byPath.set(cand.path, {
          id: `gc:${cand.path}`,
          kind: 'gc_candidate',
          name: cand.path.split('/').pop() || cand.path,
          project: inferProjectFromPath(cand.path),
          path: cand.path,
          sizeBytes: cand.sizeBytes,
          files: null,
          ageDays: cand.ageHours !== null ? Math.round((cand.ageHours / 24) * 10) / 10 : null,
          ttlRemainingHours: 0,
          pinned: false,
          canToggleKeep: canKeepPath(cand.path),
          canArchive: false,
          workspaceId: findWsId(cand.path),
          reason: cand.reason
        })
      }
    }
  }

  if (params.orcaPreview) {
    for (const row of params.orcaPreview.rows) {
      if (!row.path || !row.sizeBytes || row.sizeBytes <= 0) continue
      addCategory('worktree', row.sizeBytes)
      if (!byPath.has(row.path)) {
        byPath.set(row.path, {
          id: `wt:${row.worktreeId}`,
          kind: 'worktree',
          name: row.displayName || row.branch || row.path.split('/').pop() || row.path,
          project: row.repoName || inferProjectFromPath(row.path),
          path: row.path,
          sizeBytes: row.sizeBytes,
          files: null,
          ageDays: null,
          ttlRemainingHours: null,
          pinned: false,
          canToggleKeep: false,
          canArchive: isAllowedArchiveWorktree(row.path, params.workspaceRoot),
          workspaceId: findWsId(row.path, row.worktreeId),
          reason: `Worktree (${row.outcome}: ${row.why})`
        })
      }
    }
  }

  for (const dir of params.extraDirs) {
    if (dir.sizeBytes <= 0) continue
    addCategory(dir.kind, dir.sizeBytes)
    if (!byPath.has(dir.path)) {
      byPath.set(dir.path, {
        id: `${dir.kind}:${dir.path}`,
        kind: dir.kind,
        name: dir.name,
        project: dir.kind === 'derived_data' ? 'Xcode DerivedData' : 'AI/Data',
        path: dir.path,
        sizeBytes: dir.sizeBytes,
        files: null,
        ageDays: null,
        ttlRemainingHours: null,
        pinned: dir.pinned,
        canToggleKeep: canKeepPath(dir.path),
        canArchive: false,
        workspaceId: null,
        reason: dir.kind === 'derived_data' ? 'Xcode DerivedData build cache' : 'Shared workspace cache (AI/Data)'
      })
    }
  }

  const topOffenders = [...byPath.values()]
    .sort((a, b) => b.sizeBytes - a.sizeBytes)
    .slice(0, 60)

  const categoryBreakdown: GcCategoryStat[] = [...categoryTotals.entries()]
    .map(([category, stats]) => ({ category, count: stats.count, bytes: stats.bytes }))
    .sort((a, b) => b.bytes - a.bytes)

  return {
    topOffenders,
    legacyTotalBytes,
    legacyCount,
    strandedWorktreeCount,
    strandedTotalBytes,
    categoryBreakdown
  }
}

export function evaluateAlerts(params: {
  disk: DiskStatus | null
  jobs: LaunchJobStatus[]
  doctor: DoctorSummary
  audit: AuditSummary
  workspaces: LiveWorkspace[]
  ephemeralSimulators: SimDevice[]
  runningQemu: QemuProc[]
  recentRuns: HygieneLogRun[]
  recentBatches: TrashBatch[]
  legacyTotalBytes: number
  strandedTotalBytes: number
  settings: OpsSettings
  nowMs: number
  dismissedFingerprints: Set<string>
  escalatedMap: Record<string, number>
}): OpsAlert[] {
  const alerts: OpsAlert[] = []
  const push = (a: Omit<OpsAlert, 'id' | 'dismissed' | 'escalatedAt'>) => {
    alerts.push({
      ...a,
      id: a.fingerprint,
      dismissed: params.dismissedFingerprints.has(a.fingerprint),
      escalatedAt: params.escalatedMap[a.fingerprint] ?? null
    })
  }

  // 1. Disk space
  if (params.disk) {
    const warnPct = params.settings.diskWarnFreePercent
    const critPct = Math.max(4, Math.floor(warnPct / 2))
    if (params.disk.freePct <= critPct) {
      push({
        fingerprint: 'disk:critical',
        severity: 'critical',
        category: 'disk',
        title: `磁盘剩余空间严重告急 (${params.disk.freePct.toFixed(1)}%)`,
        detail: `${params.disk.workspaceRoot} 所在磁盘仅余 ${formatBytes(params.disk.availBytes)} / ${formatBytes(params.disk.totalBytes)}`,
        metric: formatBytes(params.disk.availBytes),
        workspaceId: null,
        path: params.disk.workspaceRoot,
        suggestedAction: '立即运行 Deep GC 并检查空间大户排行'
      })
    } else if (params.disk.freePct <= warnPct) {
      push({
        fingerprint: 'disk:warning',
        severity: 'warning',
        category: 'disk',
        title: `磁盘剩余空间偏低 (${params.disk.freePct.toFixed(1)}%)`,
        detail: `仅余 ${formatBytes(params.disk.availBytes)}，低于告警阈值 ${warnPct}%`,
        metric: formatBytes(params.disk.availBytes),
        workspaceId: null,
        path: params.disk.workspaceRoot,
        suggestedAction: '检查空间大户并执行 Daily GC'
      })
    }
  }

  // 2. Blueprint LaunchAgents (backups & hygiene)
  for (const job of params.jobs) {
    if (job.state === 'fail' || job.state === 'stale' || job.state === 'absent') {
      push({
        fingerprint: `job:${job.name}:${job.state}`,
        severity: 'critical',
        category: 'backup',
        title: `后台任务 ${job.name} 状态异常 (${job.state})`,
        detail: job.message || `上次成功: ${job.lastOk || '无记录'}`,
        metric: job.state,
        workspaceId: null,
        path: null,
        suggestedAction: '让 Ram 排查日志并核对备份/磁盘状态'
      })
    } else if (job.state === 'warn') {
      push({
        fingerprint: `job:${job.name}:warn`,
        severity: 'warning',
        category: 'backup',
        title: `后台任务 ${job.name} 有告警`,
        detail: job.message || '查看任务日志了解详情',
        metric: 'warn',
        workspaceId: null,
        path: null,
        suggestedAction: '检查日志或交给 Ram 评估是否需处理'
      })
    }
  }

  // 3. Doctor attention items
  for (const item of params.doctor.attention) {
    const key = item.slice(0, 80)
    push({
      fingerprint: `doctor:${key}`,
      severity: 'critical',
      category: 'doctor',
      title: '健康体检发现必须处理项',
      detail: item,
      metric: 'doctor',
      workspaceId: null,
      path: null,
      suggestedAction: '按 doctor.py 提示修复或交给 Ram 处理'
    })
  }

  // 4. Stale waiting/blocked agents
  const waitThresholdMs = params.settings.waitingAlertMinutes * MIN_MS
  for (const ws of params.workspaces) {
    if (ws.state === 'waiting' && ws.stateStartedAt !== null) {
      const waitMs = params.nowMs - ws.stateStartedAt
      if (waitMs >= waitThresholdMs) {
        const mins = Math.floor(waitMs / MIN_MS)
        push({
          fingerprint: `agent-wait:${ws.id}`,
          severity: 'warning',
          category: 'agent',
          title: `工作区等待你确认已超 ${mins} 分钟`,
          detail: `${ws.name}${ws.project ? ` (${ws.project})` : ''} 处于 waiting/permission 状态`,
          metric: `${mins}m`,
          workspaceId: ws.id,
          path: ws.path,
          suggestedAction: '点击直达该工作区处理授权或回复'
        })
      }
    } else if (ws.state === 'blocked') {
      push({
        fingerprint: `agent-blocked:${ws.id}`,
        severity: 'warning',
        category: 'agent',
        title: `工作区 Agent 已阻塞`,
        detail: `${ws.name}${ws.project ? ` (${ws.project})` : ''} 需要人工介入`,
        metric: 'blocked',
        workspaceId: ws.id,
        path: ws.path,
        suggestedAction: '打开工作区或让 Ram 汇总阻塞原因'
      })
    }
  }

  // 5. Ephemeral simulators / QEMU leftovers
  if (params.ephemeralSimulators.length > 0) {
    push({
      fingerprint: `sim-leftover:${params.ephemeralSimulators.length}`,
      severity: 'warning',
      category: 'device',
      title: `检测到 ${params.ephemeralSimulators.length} 个残留临时 iOS 模拟器`,
      detail: params.ephemeralSimulators.map((s) => `${s.name} (${s.state})`).slice(0, 3).join(', '),
      metric: `${params.ephemeralSimulators.length} sim`,
      workspaceId: null,
      path: null,
      suggestedAction: '确认无活跃租约后清理或交给 Ram 核查'
    })
  }
  if (params.runningQemu.length > 0) {
    push({
      fingerprint: `qemu-running:${params.runningQemu.length}`,
      severity: 'info',
      category: 'device',
      title: `当前有 ${params.runningQemu.length} 个运行中的 QEMU 进程`,
      detail: params.runningQemu.map((q) => `PID ${q.pid}`).slice(0, 4).join(', '),
      metric: `${params.runningQemu.length} vm`,
      workspaceId: null,
      path: null,
      suggestedAction: '测试结束后确认通过 agent-test-device 正常回收'
    })
  }

  // 6. GC spike within last 24h
  const spikeBytes = params.settings.gcSpikeGiB * 1024 ** 3
  const recentSpike = params.recentRuns.find(
    (r) => params.nowMs - r.at <= DAY_MS && r.reclaimedBytes >= spikeBytes
  )
  if (recentSpike) {
    push({
      fingerprint: `gc-spike:${recentSpike.at}`,
      severity: 'warning',
      category: 'cleanup',
      title: `24 小时内单次清理体积激增 (${formatBytes(recentSpike.reclaimedBytes)})`,
      detail: `共清理 ${recentSpike.reclaimedItems} 项，建议查看是否为构建缓存暴涨或测试产物流失`,
      metric: formatBytes(recentSpike.reclaimedBytes),
      workspaceId: null,
      path: null,
      suggestedAction: '在「清理记录」核查最大文件，必要时一键撤销'
    })
  }

  // 7. Audit attention items
  for (const item of params.audit.attention) {
    push({
      fingerprint: `audit:${item.slice(0, 80)}`,
      severity: 'warning',
      category: 'audit',
      title: '工作区规范审计待修复',
      detail: item,
      metric: 'audit',
      workspaceId: null,
      path: null,
      suggestedAction: '运行 agent-governor adopt 或交给 Ram 修复'
    })
  }

  // 8. Large legacy/stranded work storage notice
  const strandedCombined = params.legacyTotalBytes + params.strandedTotalBytes
  if (strandedCombined >= 30 * 1024 ** 3) {
    push({
      fingerprint: `storage-legacy:${Math.floor(strandedCombined / (10 * 1024 ** 3))}`,
      severity: 'info',
      category: 'cleanup',
      title: `历史与 worktree 滞留 work/ 占用达 ${formatBytes(strandedCombined)}`,
      detail: `受保护历史目录 ${formatBytes(params.legacyTotalBytes)}，活跃 worktree 滞留 ${formatBytes(params.strandedTotalBytes)}`,
      metric: formatBytes(strandedCombined),
      workspaceId: null,
      path: null,
      suggestedAction: '在「空间大户」逐项审查，可给重要目录加 .keep 保护或清理已废弃目录'
    })
  }

  const order: Record<string, number> = { critical: 0, warning: 1, info: 2 }
  alerts.sort((a, b) => {
    if (a.dismissed !== b.dismissed) return a.dismissed ? 1 : -1
    return order[a.severity] - order[b.severity]
  })
  return alerts
}

/** Build a clean, structured Chinese escalation brief for Ram Bot. */
export function buildBotEscalationBrief(params: {
  alerts: OpsAlert[]
  snapshot: Pick<OpsSnapshot, 'disk' | 'cleanup' | 'storage' | 'health' | 'live'>
  customNote?: string
}): { subject: string; body: string } {
  const active = params.alerts.filter((a) => !a.dismissed)
  const critCount = active.filter((a) => a.severity === 'critical').length
  const warnCount = active.filter((a) => a.severity === 'warning').length

  const subject =
    active.length === 1
      ? `[Ops Radar] ${active[0].title}`
      : critCount > 0
        ? `[Ops Radar] 发现 ${critCount} 项紧急告警 / ${warnCount} 项提醒，请结合项目进度反馈`
        : `[Ops Radar] 工作区与清理监测巡检摘要（${active.length} 项提醒）`

  const lines: string[] = [
    '来自 Orca 插件 **Workspace Ops Radar** 的实时监测告警，请你结合当前各项目与工作区进展，给主人汇总原因、影响范围与下一步建议（只读分析，不要擅自删除文件或改代码）：',
    ''
  ]

  if (params.customNote && params.customNote.trim()) {
    lines.push(`### 主人补充说明`, params.customNote.trim(), '')
  }

  if (active.length > 0) {
    lines.push('### 当前告警明细')
    for (const a of active.slice(0, 12)) {
      const tag = a.severity === 'critical' ? '🔴 紧急' : a.severity === 'warning' ? '🟡 警告' : '🔵 提示'
      lines.push(`- ${tag} **${a.title}**：${a.detail}${a.path ? `（路径：\`${a.path}\`）` : ''}`)
    }
    lines.push('')
  }

  lines.push('### 实时环境快照')
  if (params.snapshot.disk) {
    const d = params.snapshot.disk
    lines.push(
      `- **磁盘**：已用 ${formatBytes(d.usedBytes)} / ${formatBytes(d.totalBytes)}（剩余 ${formatBytes(d.availBytes)}，${d.freePct.toFixed(1)}%）`
    )
  }
  const gov = params.snapshot.cleanup.governor
  lines.push(
    `- **清理引擎**：近 24h 回收 ${formatBytes(gov.totalReclaimed24hBytes)}（${gov.totalReclaimed24hItems} 项）；当前可清 ${formatBytes(gov.dryRun?.reclaimableBytes ?? 0)}`
  )
  lines.push(
    `- **空间滞留**：历史 legacy work/ ${formatBytes(params.snapshot.storage.legacyTotalBytes)}（${params.snapshot.storage.legacyCount} 项），worktree 滞留 ${formatBytes(params.snapshot.storage.strandedTotalBytes)}（${params.snapshot.storage.strandedWorktreeCount} 处）`
  )
  const top3 = params.snapshot.storage.topOffenders.slice(0, 3)
  if (top3.length > 0) {
    lines.push(
      `- **Top 3 空间大户**：${top3.map((o) => `${o.project}/${o.name} (${formatBytes(o.sizeBytes)})`).join('；')}`
    )
  }
  const unhealthyJobs = params.snapshot.health.jobs.filter((j) => j.state !== 'ok' && j.state !== 'running')
  if (unhealthyJobs.length > 0) {
    lines.push(
      `- **异常后台任务**：${unhealthyJobs.map((j) => `${j.name}=${j.state}`).join('，')}`
    )
  }
  const waitingWs = params.snapshot.live.workspaces.filter((w) => w.state === 'waiting' || w.state === 'blocked')
  if (waitingWs.length > 0) {
    lines.push(
      `- **等待/阻塞工作区**：${waitingWs.slice(0, 5).map((w) => `${w.name}(${w.state})`).join('；')}`
    )
  }

  lines.push(
    '',
    '请回复主人：1）哪些需要立刻拍板或处理；2）哪些空间大户属于仍在进行中的项目、哪些可以安全归档或清理；3）是否需要派 worker 修复。'
  )

  return { subject, body: lines.join('\n') }
}

export function buildStatusBarState(snapshot: OpsSnapshot, nowMs: number): {
  text: string
  icon: 'gauge' | 'warning' | 'error'
  tone: 'neutral' | 'accent' | 'success' | 'warning' | 'danger'
  tooltip: string
} {
  const nextAt = snapshot.cleanup.governor.nextRunAt
  const cd = nextAt ? formatDurationCompact(nextAt - nowMs, false) : '—'
  const crit = snapshot.counts.criticalAlerts
  const warn = snapshot.counts.warnAlerts
  const waiting = snapshot.counts.staleWaitingAgents
  const freeStr = snapshot.disk ? `${Math.round(snapshot.disk.freePct)}% free` : ''

  if (crit > 0) {
    return {
      text: `Ops · ${crit} crit`,
      icon: 'error',
      tone: 'danger',
      tooltip: `Workspace Ops Radar: ${crit} critical, ${warn} warning · Next GC ${cd}${freeStr ? ` · ${freeStr}` : ''}`
    }
  }
  if (warn > 0 || waiting > 0) {
    const badge = waiting > 0 ? `${waiting} wait` : `${warn} warn`
    return {
      text: `Ops · ${cd} · ${badge}`,
      icon: 'warning',
      tone: 'warning',
      tooltip: `Workspace Ops Radar: ${warn} warnings, ${waiting} waiting agents · Next GC ${cd}${freeStr ? ` · ${freeStr}` : ''}`
    }
  }
  return {
    text: `Ops · ${cd}`,
    icon: 'gauge',
    tone: 'success',
    tooltip: `Workspace Ops Radar healthy · Next GC ${cd} · 24h reclaimed ${formatBytes(snapshot.cleanup.governor.totalReclaimed24hBytes)}${freeStr ? ` · ${freeStr}` : ''}`
  }
}

export function isAllowedKeepTarget(targetPath: string, workspaceRoot: string, homeDir: string): boolean {
  const clean = targetPath.replace(/\/+$/, '')
  if (!clean || clean.includes('\0') || clean.includes('/../') || clean.endsWith('/..')) return false
  if (clean.includes('/.git/') || clean.endsWith('/.git')) return false
  const aiRoot = workspaceRoot.replace(/\/+$/, '')
  if (aiRoot) {
    // Allow subdirectories inside any project/worktree `work/` folder (never the repo root itself).
    if (clean.startsWith(`${aiRoot}/AI/`) && /\/work\/.+/.test(clean)) return true
    // Allow subdirectories inside shared AI/Data cache.
    if (clean.startsWith(`${aiRoot}/AI/Data/`)) return true
  }
  const cleanHome = homeDir.replace(/\/+$/, '')
  if (cleanHome) {
    const ddPrefix = `${cleanHome}/Library/Developer/Xcode/DerivedData/`
    if (clean.startsWith(ddPrefix)) return true
  }
  return false
}

export function isAllowedManifestPath(manifestPath: string, homeDir: string): boolean {
  const clean = manifestPath.replace(/\/+$/, '')
  if (!clean || clean.includes('\0') || clean.includes('/../')) return false
  const trashPrefix = `${homeDir.replace(/\/+$/, '')}/.Trash/agent-governor-`
  return clean.startsWith(trashPrefix) && clean.endsWith('/manifest.json')
}

/** Allow archiving only task worktrees inside `<workspaceRoot>/AI/{Orca,Codex,Claude}/Worktrees/...`, never main clones. */
export function isAllowedArchiveWorktree(worktreePath: string, workspaceRoot: string): boolean {
  const clean = worktreePath.replace(/\/+$/, '')
  const root = workspaceRoot.replace(/\/+$/, '')
  if (!clean || !root || clean.includes('\0') || clean.includes('/../') || clean.endsWith('/..')) return false
  if (clean.includes('/.git/') || clean.endsWith('/.git')) return false
  const prefixes = [
    `${root}/AI/Orca/Worktrees/`,
    `${root}/AI/Codex/Worktrees/`,
    `${root}/AI/Claude/Worktrees/`
  ]
  for (const pfx of prefixes) {
    if (clean.startsWith(pfx) && clean.length > pfx.length) return true
  }
  return false
}

/** Sanitize a worktree branch/displayName into a safe single path segment for AI/Archive/<date>-<topic>. */
export function sanitizeArchiveTopic(raw: string): string {
  const normalized = raw
    .trim()
    .replace(/^(?:codex|claude|feat|fix|chore)\//i, '')
    .replace(/[｜|/\s]+/g, '-')
    .replace(/[^A-Za-z0-9_\u4e00-\u9fff-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^[.-]+|[.-]+$/g, '')
    .slice(0, 60)
  return normalized || 'worktree'
}

export const DEFAULT_HYGIENE_CONFIG: HygieneConfig = {
  gcIntervalHours: 4,
  scratchTtlDays: 3,
  testsTtlDays: 7,
  derivedDataTtlHours: 12,
  systemDerivedDataTtlHours: 168,
  cacheTtlDays: 7,
  tmpTtlHours: 24,
  simTtlDays: 3,
  minIdleHours: 2,
  skipXcode: false,
  skipSimulators: false,
  rawExtraArgs: '',
  configPath: '~/.orca/hygiene.env'
}

function clampNum(v: unknown, def: number, min: number, max: number, decimals = 2): number {
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return def
  const clamped = Math.max(min, Math.min(max, n))
  const scale = 10 ** decimals
  return Math.round(clamped * scale) / scale
}

export function normalizeHygieneConfig(input?: Partial<HygieneConfig> | null): HygieneConfig {
  const src = input ?? {}
  const cfg: HygieneConfig = {
    gcIntervalHours: clampNum(src.gcIntervalHours, DEFAULT_HYGIENE_CONFIG.gcIntervalHours, 1, 48, 1),
    scratchTtlDays: clampNum(src.scratchTtlDays, DEFAULT_HYGIENE_CONFIG.scratchTtlDays, 0.5, 30, 1),
    testsTtlDays: clampNum(src.testsTtlDays, DEFAULT_HYGIENE_CONFIG.testsTtlDays, 1, 60, 1),
    derivedDataTtlHours: clampNum(src.derivedDataTtlHours, DEFAULT_HYGIENE_CONFIG.derivedDataTtlHours, 1, 168, 1),
    systemDerivedDataTtlHours: clampNum(
      src.systemDerivedDataTtlHours,
      DEFAULT_HYGIENE_CONFIG.systemDerivedDataTtlHours,
      12,
      720,
      0
    ),
    cacheTtlDays: clampNum(src.cacheTtlDays, DEFAULT_HYGIENE_CONFIG.cacheTtlDays, 1, 60, 1),
    tmpTtlHours: clampNum(src.tmpTtlHours, DEFAULT_HYGIENE_CONFIG.tmpTtlHours, 1, 168, 1),
    simTtlDays: clampNum(src.simTtlDays, DEFAULT_HYGIENE_CONFIG.simTtlDays, 0.5, 30, 1),
    minIdleHours: clampNum(src.minIdleHours, DEFAULT_HYGIENE_CONFIG.minIdleHours, 0.25, 48, 2),
    skipXcode: Boolean(src.skipXcode),
    skipSimulators: Boolean(src.skipSimulators),
    rawExtraArgs: '',
    configPath: typeof src.configPath === 'string' && src.configPath ? src.configPath : DEFAULT_HYGIENE_CONFIG.configPath
  }
  cfg.rawExtraArgs = buildGovernorGcArgs(cfg, 'daily').join(' ')
  return cfg
}

export function parseGovernorGcArgs(
  rawArgs: string | undefined | null,
  gcIntervalHours = DEFAULT_HYGIENE_CONFIG.gcIntervalHours,
  configPath = DEFAULT_HYGIENE_CONFIG.configPath
): HygieneConfig {
  const partial: Partial<HygieneConfig> = { gcIntervalHours, configPath }
  const tokens = String(rawArgs ?? '')
    .trim()
    .split(/\s+/)
    .filter(Boolean)

  for (let i = 0; i < tokens.length; i += 1) {
    let tok = tokens[i]
    let eqVal: string | null = null
    const eqIdx = tok.indexOf('=')
    if (eqIdx > 0) {
      eqVal = tok.slice(eqIdx + 1)
      tok = tok.slice(0, eqIdx)
    }
    const readVal = (): string | null => {
      if (eqVal !== null) return eqVal
      if (i + 1 < tokens.length) {
        i += 1
        return tokens[i]
      }
      return null
    }
    switch (tok) {
      case '--scratch-ttl-days': {
        const v = readVal()
        if (v !== null) partial.scratchTtlDays = Number(v)
        break
      }
      case '--tests-ttl-days': {
        const v = readVal()
        if (v !== null) partial.testsTtlDays = Number(v)
        break
      }
      case '--derived-data-ttl-hours': {
        const v = readVal()
        if (v !== null) partial.derivedDataTtlHours = Number(v)
        break
      }
      case '--system-derived-data-ttl-hours': {
        const v = readVal()
        if (v !== null) partial.systemDerivedDataTtlHours = Number(v)
        break
      }
      case '--cache-ttl-days': {
        const v = readVal()
        if (v !== null) partial.cacheTtlDays = Number(v)
        break
      }
      case '--tmp-ttl-hours': {
        const v = readVal()
        if (v !== null) partial.tmpTtlHours = Number(v)
        break
      }
      case '--sim-ttl-days': {
        const v = readVal()
        if (v !== null) partial.simTtlDays = Number(v)
        break
      }
      case '--min-idle-hours': {
        const v = readVal()
        if (v !== null) partial.minIdleHours = Number(v)
        break
      }
      case '--skip-xcode':
        partial.skipXcode = true
        break
      case '--skip-simulators':
        partial.skipSimulators = true
        break
      default:
        break
    }
  }
  return normalizeHygieneConfig(partial)
}

/** Build validated CLI flags for `agent-governor gc` from HygieneConfig. */
export function buildGovernorGcArgs(cfg: HygieneConfig, mode: GcMode = 'daily'): string[] {
  const args: string[] = []
  if (mode !== 'deep') {
    if (cfg.scratchTtlDays !== DEFAULT_HYGIENE_CONFIG.scratchTtlDays) {
      args.push('--scratch-ttl-days', String(cfg.scratchTtlDays))
    }
    if (cfg.testsTtlDays !== DEFAULT_HYGIENE_CONFIG.testsTtlDays) {
      args.push('--tests-ttl-days', String(cfg.testsTtlDays))
    }
    if (cfg.derivedDataTtlHours !== DEFAULT_HYGIENE_CONFIG.derivedDataTtlHours) {
      args.push('--derived-data-ttl-hours', String(cfg.derivedDataTtlHours))
    }
    if (cfg.systemDerivedDataTtlHours !== DEFAULT_HYGIENE_CONFIG.systemDerivedDataTtlHours) {
      args.push('--system-derived-data-ttl-hours', String(cfg.systemDerivedDataTtlHours))
    }
    if (cfg.cacheTtlDays !== DEFAULT_HYGIENE_CONFIG.cacheTtlDays) {
      args.push('--cache-ttl-days', String(cfg.cacheTtlDays))
    }
    if (cfg.tmpTtlHours !== DEFAULT_HYGIENE_CONFIG.tmpTtlHours) {
      args.push('--tmp-ttl-hours', String(cfg.tmpTtlHours))
    }
    if (cfg.simTtlDays !== DEFAULT_HYGIENE_CONFIG.simTtlDays) {
      args.push('--sim-ttl-days', String(cfg.simTtlDays))
    }
    if (mode === 'daily' && cfg.minIdleHours !== DEFAULT_HYGIENE_CONFIG.minIdleHours) {
      args.push('--min-idle-hours', String(cfg.minIdleHours))
    }
  }
  if (cfg.skipXcode) args.push('--skip-xcode')
  if (cfg.skipSimulators) args.push('--skip-simulators')
  return args
}

/** Safely upsert/remove specific `export KEY="value"` assignments in a shell env file while preserving all other lines. */
export function upsertEnvKeys(existing: string, updates: Record<string, string | null>): string {
  const quote = (v: string) => v.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\$/g, '\\$').replace(/`/g, '\\`')
  const remaining = new Map<string, string | null>(Object.entries(updates))
  const lines = existing ? existing.split(/\r?\n/) : []
  // Drop a single trailing empty split artifact so we re-add clean trailing newline below.
  if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop()

  const out: string[] = []
  for (const line of lines) {
    const m = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=/)
    if (m && remaining.has(m[1])) {
      const val = remaining.get(m[1]) ?? null
      remaining.delete(m[1])
      if (val !== null) out.push(`export ${m[1]}="${quote(val)}"`)
      continue
    }
    out.push(line)
  }

  for (const [k, val] of remaining.entries()) {
    if (val !== null) out.push(`export ${k}="${quote(val)}"`)
  }
  return out.length > 0 ? `${out.join('\n')}\n` : ''
}
