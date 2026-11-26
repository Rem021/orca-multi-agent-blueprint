// Node-side telemetry collectors and safe action runners for Workspace Ops Radar.
// All subprocess calls use execFile (argument arrays, never shell interpolation) with strict timeouts.

import { execFile } from 'node:child_process'
import * as fs from 'node:fs/promises'
import * as os from 'node:os'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  buildGovernorGcArgs,
  inferProjectFromPath,
  isAllowedArchiveWorktree,
  isAllowedKeepTarget,
  isAllowedManifestPath,
  normalizeHygieneConfig,
  parseDfOutput,
  parseGovernorGcArgs,
  parseHygieneLog,
  sanitizeArchiveTopic,
  upsertEnvKeys,
  type TriageRaw
} from './core.ts'
import type {
  AuditProjectSummary,
  AuditSummary,
  BotChatSummary,
  BotInfo,
  DiskStatus,
  DoctorSummary,
  GcCategoryStat,
  GcReportSummary,
  HygieneConfig,
  HygieneLogRun,
  LaunchJobStatus,
  MemoryDiagnostics,
  OrcaCleanupPreview,
  OrcaCleanupRow,
  OrcaCleanupRun,
  QemuProc,
  SimDevice,
  SimLease,
  TrashBatch,
  TrashItem,
  TrendDay
} from './protocol.ts'

export type ResolvedEnv = {
  homeDir: string
  workspaceRoot: string
  projectsRoot: string
  storageMode: 'internal' | 'external' | 'auto'
  codexStorageDir: string
  gcIntervalSec: number
  hygieneConfig: HygieneConfig
  orcaBin: string
  governorBin: string
  doctorScript: string | null
  childEnv: Record<string, string | undefined>
}

export type WorktreePsEntry = {
  canonicalId: string
  worktreeId: string
  repo: string
  path: string
  branch: string
  displayName: string
  bot: string | null
  linkedPR: string | null
  unread: boolean
  lastActivityAt: number | null
  status: string
  agentState: string | null
  agentType: string | null
  stateStartedAt: number | null
  lastMessage: string | null
}

export type FastTelemetry = {
  env: ResolvedEnv
  disk: DiskStatus | null
  gcStampSec: number | null
  gcLastState: string | null
  gcLastMessage: string | null
  hygieneHistory: {
    runs: HygieneLogRun[]
    trendDays: TrendDay[]
    totalReclaimed24hBytes: number
    totalReclaimed7dBytes: number
    totalReclaimed24hItems: number
  }
  trashBatches: TrashBatch[]
  jobs: LaunchJobStatus[]
  bots: BotInfo[]
  worktreePs: WorktreePsEntry[]
  orcaCleanupReport: {
    nextRunAt: number | null
    lastRun: OrcaCleanupRun | null
    earlierRuns: OrcaCleanupRun[]
    error: string | null
  }
  activeLeases: SimLease[]
  runningQemu: QemuProc[]
}

export type GcPreviewTelemetry = {
  gcDryRun: GcReportSummary | null
  orcaCleanupPreview: OrcaCleanupPreview | null
}

export type HeavyStorageTelemetry = {
  triage: TriageRaw | null
  extraDirs: {
    kind: 'shared_data' | 'derived_data'
    name: string
    path: string
    sizeBytes: number
    pinned: boolean
  }[]
}

export type StorageTelemetry = GcPreviewTelemetry & HeavyStorageTelemetry

export type HealthTelemetry = {
  doctor: DoctorSummary
  audit: AuditSummary
}

export interface OpsCollector {
  collectFast(fallbackGcIntervalHours: number): Promise<FastTelemetry>
  collectGcAndPreview(env: ResolvedEnv): Promise<GcPreviewTelemetry>
  collectHeavyStorage(env: ResolvedEnv): Promise<HeavyStorageTelemetry>
  collectStorage(env: ResolvedEnv): Promise<StorageTelemetry>
  collectHealth(env: ResolvedEnv): Promise<HealthTelemetry>
  collectMemory(env: ResolvedEnv): Promise<MemoryDiagnostics | null>
  runGovernorGc(env: ResolvedEnv, mode: 'daily' | 'session-end' | 'deep', apply: boolean): Promise<GcReportSummary>
  restoreTrashBatch(env: ResolvedEnv, manifestPath: string, apply: boolean): Promise<{ ok: boolean; restored: number; message: string }>
  toggleKeepMarker(env: ResolvedEnv, targetPath: string, keep: boolean): Promise<{ ok: boolean; pinned: boolean }>
  archiveWorktree(
    env: ResolvedEnv,
    worktreePath: string,
    topic?: string
  ): Promise<{ ok: boolean; archiveDir: string; topic: string; totalBytes: number; message: string }>
  runOrcaCleanup(env: ResolvedEnv, apply: boolean): Promise<{ ok: boolean; summary: string }>
  saveHygieneConfig(env: ResolvedEnv, next: HygieneConfig): Promise<HygieneConfig>
  sendToBot(
    env: ResolvedEnv,
    params: { botName: string; subject: string; body: string; mode: 'handoff' | 'status'; newChat?: boolean }
  ): Promise<{ ok: boolean; error: string | null }>
}

const LOG_TAIL_BYTES = 256 * 1024

function runExec(
  file: string,
  args: readonly string[],
  env: Record<string, string | undefined>,
  timeout = 25_000
): Promise<{ rc: number; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    execFile(
      file,
      args,
      { env, timeout, maxBuffer: 12 * 1024 * 1024, encoding: 'utf8' },
      (err, stdout, stderr) => {
        if (!err) {
          resolve({ rc: 0, stdout: stdout || '', stderr: stderr || '' })
          return
        }
        const rc = typeof err.code === 'number' ? err.code : 1
        resolve({ rc, stdout: stdout || '', stderr: stderr || err.message || '' })
      }
    )
  })
}

async function exists(p: string): Promise<boolean> {
  try {
    await fs.access(p)
    return true
  } catch {
    return false
  }
}

async function readTextSafe(p: string): Promise<string | null> {
  try {
    return await fs.readFile(p, 'utf8')
  } catch {
    return null
  }
}

async function readTailSafe(p: string, maxBytes = LOG_TAIL_BYTES): Promise<string> {
  let handle: fs.FileHandle | null = null
  try {
    handle = await fs.open(p, 'r')
    const st = await handle.stat()
    const size = st.size
    if (size <= 0) return ''
    const len = Math.min(size, maxBytes)
    const start = size - len
    const buf = new Uint8Array(len)
    const { bytesRead } = await handle.read(buf, 0, len, start)
    return new TextDecoder('utf-8', { fatal: false }).decode(buf.subarray(0, bytesRead))
  } catch {
    return ''
  } finally {
    if (handle) await handle.close().catch(() => undefined)
  }
}

function parseEnvFile(text: string, homeDir: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const raw of text.split(/\r?\n/)) {
    let line = raw.trim()
    if (line.startsWith('export ')) line = line.slice(7).trim()
    if (!line || line.startsWith('#') || !line.includes('=')) continue
    const eq = line.indexOf('=')
    const key = line.slice(0, eq).trim()
    let val = line.slice(eq + 1).trim()
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1)
    } else {
      val = val.replace(/\s+#.*$/, '').trim()
    }
    val = val.replace(/\$\{HOME\}/g, homeDir).replace(/\$HOME/g, homeDir)
    if (key && val) out[key] = val
  }
  return out
}

export async function resolveEnvironment(fallbackGcIntervalHours = 4): Promise<ResolvedEnv> {
  const homeDir = os.homedir()
  const cfg: Record<string, string> = {}
  for (const rel of ['.orca/backup-config.env', '.orca/guard.env', '.orca/hygiene.env']) {
    const txt = await readTextSafe(path.join(homeDir, rel))
    if (txt) Object.assign(cfg, parseEnvFile(txt, homeDir))
  }

  const rawMode = (cfg.STORAGE_MODE || 'auto').toLowerCase()
  const storageMode: 'internal' | 'external' | 'auto' =
    rawMode === 'internal' || rawMode === 'external' ? rawMode : 'auto'

  const workspaceRoot =
    storageMode === 'internal'
      ? cfg.INTERNAL_WORKSPACE_PATH || path.join(homeDir, 'AI-Workspace')
      : cfg.AI_VOLUME_PATH || cfg.INTERNAL_WORKSPACE_PATH || path.join(homeDir, 'AI-Workspace')

  const projectsRoot = path.join(workspaceRoot, 'AI', 'Projects')
  const codexStorageDir = path.join(homeDir, 'Library', 'Application Support', 'Codex Storage')

  const intervalFromEnv = Number(cfg.AGENT_HYGIENE_GC_INTERVAL_SEC)
  const gcIntervalSec =
    Number.isFinite(intervalFromEnv) && intervalFromEnv > 0
      ? intervalFromEnv
      : Math.round(fallbackGcIntervalHours * 3600)

  const hygieneConfig = parseGovernorGcArgs(
    cfg.AGENT_GOVERNOR_GC_ARGS,
    Math.round((gcIntervalSec / 3600) * 10) / 10,
    path.join(homeDir, '.orca', 'hygiene.env')
  )

  const extraPath = [
    path.join(homeDir, '.local', 'bin'),
    '/Applications/Orca.app/Contents/Resources/bin',
    '/opt/homebrew/bin',
    '/usr/local/bin',
    '/usr/bin',
    '/bin',
    '/usr/sbin',
    '/sbin',
    process.env.PATH || ''
  ]
    .filter(Boolean)
    .join(':')

  const childEnv: Record<string, string | undefined> = {
    ...process.env,
    HOME: homeDir,
    PATH: extraPath
  }

  const orcaCandidates = [
    cfg.ORCA_CLI_COMMAND,
    '/Applications/Orca.app/Contents/Resources/bin/orca',
    path.join(homeDir, '.local', 'bin', 'orca')
  ].filter((x): x is string => Boolean(x))
  let orcaBin = 'orca'
  for (const cand of orcaCandidates) {
    if (await exists(cand)) {
      orcaBin = cand
      break
    }
  }

  let bundledBlueprintScripts: string | null = null
  try {
    bundledBlueprintScripts = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'scripts')
  } catch {
    bundledBlueprintScripts = null
  }

  const govCandidates = [
    path.join(homeDir, '.local', 'bin', 'agent-governor'),
    bundledBlueprintScripts ? path.join(bundledBlueprintScripts, 'agent_governor.py') : null,
    path.join(projectsRoot, 'Mac', 'outputs', 'orca-multi-agent-blueprint', 'scripts', 'agent_governor.py')
  ].filter((x): x is string => Boolean(x))
  let governorBin = 'agent-governor'
  for (const cand of govCandidates) {
    if (await exists(cand)) {
      governorBin = cand
      break
    }
  }

  const doctorCandidates = [
    bundledBlueprintScripts ? path.join(bundledBlueprintScripts, 'doctor.py') : null,
    path.join(homeDir, '.local', 'lib', 'orca-blueprint', 'doctor.py'),
    path.join(projectsRoot, 'Mac', 'outputs', 'orca-multi-agent-blueprint', 'scripts', 'doctor.py')
  ].filter((x): x is string => Boolean(x))
  let doctorScript: string | null = null
  for (const cand of doctorCandidates) {
    if (await exists(cand)) {
      doctorScript = cand
      break
    }
  }

  return {
    homeDir,
    workspaceRoot,
    projectsRoot,
    storageMode,
    codexStorageDir,
    gcIntervalSec,
    hygieneConfig,
    orcaBin,
    governorBin,
    doctorScript,
    childEnv
  }
}

function parseJsonSafe<T>(raw: string): T | null {
  try {
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

function parseGcReport(raw: Record<string, unknown>): GcReportSummary {
  const categoriesObj = (raw.categories as Record<string, { count?: number; bytes?: number }>) || {}
  const categories: GcCategoryStat[] = Object.entries(categoriesObj)
    .map(([category, v]) => ({
      category,
      count: Number(v?.count ?? 0),
      bytes: Number(v?.bytes ?? 0)
    }))
    .sort((a, b) => b.bytes - a.bytes)

  const candidates = Array.isArray(raw.candidates)
    ? raw.candidates
        .map((c: Record<string, unknown>) => ({
          category: String(c.category ?? 'other'),
          path: String(c.path ?? ''),
          sizeBytes: Number(c.size_bytes ?? 0),
          sizeHuman: String(c.size_human ?? ''),
          ageHours: typeof c.age_hours === 'number' ? c.age_hours : null,
          reason: String(c.reason ?? '')
        }))
        .sort((a, b) => b.sizeBytes - a.sizeBytes)
        .slice(0, 40)
    : []

  const ephemeralSimulators: SimDevice[] = Array.isArray(raw.ephemeral_simulators)
    ? raw.ephemeral_simulators.map((s: Record<string, unknown>) => ({
        name: String(s.name ?? ''),
        udid: String(s.udid ?? ''),
        state: String(s.state ?? 'Shutdown'),
        reason: typeof s.reason === 'string' ? s.reason : undefined
      }))
    : []

  const errors = Array.isArray(raw.errors)
    ? raw.errors.map((e) => (typeof e === 'string' ? e : JSON.stringify(e)))
    : []

  return {
    mode: String(raw.mode ?? 'daily'),
    execution: String(raw.execution ?? 'dry-run'),
    totalCandidates: Number(raw.total_candidates ?? 0),
    reclaimableBytes: Number(raw.reclaimable_bytes ?? 0),
    reclaimableHuman: String(raw.reclaimable_human ?? '0 B'),
    protectedSkipsCount: Number(raw.protected_skips_count ?? 0),
    categories,
    candidates,
    ephemeralSimulators,
    launchservicesGhostsCount: Array.isArray(raw.launchservices_ghosts) ? raw.launchservices_ghosts.length : 0,
    manifestPath: typeof raw.manifest === 'string' ? raw.manifest : null,
    errors
  }
}

async function collectTrashBatches(homeDir: string): Promise<TrashBatch[]> {
  const trashDir = path.join(homeDir, '.Trash')
  let topEntries: fs.Dirent[]
  try {
    topEntries = await fs.readdir(trashDir, { withFileTypes: true })
  } catch {
    return []
  }

  const dayDirs = topEntries
    .filter((d) => d.isDirectory() && d.name.startsWith('agent-governor-'))
    .map((d) => d.name)
    .sort()
    .reverse()
    .slice(0, 14)

  const batches: TrashBatch[] = []
  for (const dayName of dayDirs) {
    const dayPath = path.join(trashDir, dayName)
    let subEntries: fs.Dirent[]
    try {
      subEntries = await fs.readdir(dayPath, { withFileTypes: true })
    } catch {
      continue
    }
    for (const sub of subEntries) {
      if (!sub.isDirectory()) continue
      const manifestPath = path.join(dayPath, sub.name, 'manifest.json')
      const txt = await readTextSafe(manifestPath)
      if (!txt) continue
      const parsed = parseJsonSafe<{
        created_at?: string
        restored_at?: string
        mode?: string
        total_items?: number
        total_bytes?: number
        items?: {
          index?: number
          category?: string
          reason?: string
          original_path?: string
          staged_path?: string
          size_bytes?: number
          staged_at?: string
        }[]
      }>(txt)
      if (!parsed) continue

      const items: TrashItem[] = (parsed.items ?? []).map((it, idx) => {
        const orig = String(it.original_path ?? '')
        const stagedMs = it.staged_at ? Date.parse(it.staged_at) : NaN
        return {
          index: Number(it.index ?? idx + 1),
          category: String(it.category ?? 'other'),
          reason: String(it.reason ?? ''),
          originalPath: orig,
          stagedPath: String(it.staged_path ?? ''),
          sizeBytes: Number(it.size_bytes ?? 0),
          stagedAt: Number.isFinite(stagedMs) ? stagedMs : null,
          project: orig ? inferProjectFromPath(orig) : null
        }
      })
      items.sort((a, b) => b.sizeBytes - a.sizeBytes)

      const catMap = new Map<string, { count: number; bytes: number }>()
      for (const it of items) {
        const prev = catMap.get(it.category) ?? { count: 0, bytes: 0 }
        prev.count += 1
        prev.bytes += it.sizeBytes
        catMap.set(it.category, prev)
      }
      const createdAt = parsed.created_at ? Date.parse(parsed.created_at) : Date.now()
      const restoredMs = parsed.restored_at ? Date.parse(parsed.restored_at) : NaN
      batches.push({
        id: `${dayName}/${sub.name}`,
        manifestPath,
        createdAt: Number.isFinite(createdAt) ? createdAt : Date.now(),
        mode: String(parsed.mode ?? 'daily'),
        inTrash: true,
        restoredAt: Number.isFinite(restoredMs) ? restoredMs : null,
        totalItems: Number(parsed.total_items ?? items.length),
        totalBytes: Number(parsed.total_bytes ?? items.reduce((s, i) => s + i.sizeBytes, 0)),
        largestItem: items[0] ?? null,
        categories: [...catMap.entries()]
          .map(([category, v]) => ({ category, count: v.count, bytes: v.bytes }))
          .sort((a, b) => b.bytes - a.bytes),
        items: items.slice(0, 25)
      })
    }
    if (batches.length >= 15) break
  }

  batches.sort((a, b) => b.createdAt - a.createdAt)
  return batches.slice(0, 15)
}

async function collectLaunchJobs(codexStorageDir: string): Promise<LaunchJobStatus[]> {
  const statusDir = path.join(codexStorageDir, 'status')
  let entries: string[]
  try {
    entries = await fs.readdir(statusDir)
  } catch {
    return []
  }

  const jobs: LaunchJobStatus[] = []
  for (const file of entries.sort()) {
    if (!file.endsWith('.json') || file === 'volume-usb-observation.json') continue
    const txt = await readTextSafe(path.join(statusDir, file))
    if (!txt) continue
    const d = parseJsonSafe<Record<string, unknown>>(txt)
    if (!d) continue
    const rawState = String(d.state ?? 'unknown')
    const validStates = ['ok', 'warn', 'fail', 'stale', 'skipped', 'running', 'absent']
    jobs.push({
      name: String(d.name ?? file.replace(/\.json$/, '')),
      state: (validStates.includes(rawState) ? rawState : 'unknown') as LaunchJobStatus['state'],
      time: String(d.time ?? d.observed_at ?? ''),
      lastOk: String(d.last_ok ?? ''),
      message: String(d.message ?? ''),
      seconds: typeof d.seconds === 'number' ? d.seconds : null
    })
  }
  return jobs
}

async function collectBots(env: ResolvedEnv): Promise<BotInfo[]> {
  const listRes = await runExec(env.orcaBin, ['bot', 'list', '--json'], env.childEnv, 15_000)
  if (listRes.rc !== 0) return []
  const parsed = parseJsonSafe<{
    ok?: boolean
    result?: {
      bots?: {
        id?: string
        name?: string
        agent?: string
        paused?: boolean
        specialty?: string
        chats?: { count?: number }
      }[]
    }
  }>(listRes.stdout)
  const rawBots = parsed?.result?.bots ?? []
  const bots: BotInfo[] = []

  for (const b of rawBots) {
    const name = String(b.name ?? '')
    if (!name) continue
    const chatsRes = await runExec(env.orcaBin, ['bot', 'chats', '--bot', name, '--json'], env.childEnv, 15_000)
    let chats: BotChatSummary[] = []
    if (chatsRes.rc === 0) {
      const cp = parseJsonSafe<{
        result?: {
          chats?: {
            id?: string
            title?: string
            home?: boolean
            closed?: boolean
            lastActivityAt?: number
            lastAsked?: string
            lastSaid?: string
          }[]
        }
      }>(chatsRes.stdout)
      chats = (cp?.result?.chats ?? []).slice(0, 6).map((c) => ({
        id: String(c.id ?? ''),
        title: String(c.title ?? 'Home'),
        home: Boolean(c.home),
        closed: Boolean(c.closed),
        lastActivityAt: typeof c.lastActivityAt === 'number' ? c.lastActivityAt : null,
        lastAsked: String(c.lastAsked ?? '').slice(0, 240),
        lastSaid: String(c.lastSaid ?? '').slice(0, 320)
      }))
    }
    bots.push({
      id: String(b.id ?? name),
      name,
      agent: String(b.agent ?? 'unknown'),
      paused: Boolean(b.paused),
      specialty: String(b.specialty ?? ''),
      chatCount: Number(b.chats?.count ?? chats.length),
      chats
    })
  }
  return bots
}

async function collectWorktreePs(env: ResolvedEnv): Promise<WorktreePsEntry[]> {
  const res = await runExec(env.orcaBin, ['worktree', 'ps', '--limit', '200', '--json'], env.childEnv, 20_000)
  if (res.rc !== 0) return []
  const parsed = parseJsonSafe<{
    result?: {
      worktrees?: {
        worktreeId?: string
        address?: { environment?: string; worktree?: string } | null
        repo?: string
        path?: string
        branch?: string
        displayName?: string
        bot?: string | null
        linkedPR?: { url?: string; number?: number } | string | null
        unread?: boolean
        lastActivityAt?: number | null
        status?: string
        agents?: {
          parentPaneKey?: string | null
          state?: string
          agentType?: string
          stateStartedAt?: number | null
          lastAssistantMessage?: string | null
        }[]
      }[]
    }
  }>(res.stdout)

  return (parsed?.result?.worktrees ?? []).map((w) => {
    const primaryAgent = (w.agents ?? []).find((a) => !a.parentPaneKey) ?? (w.agents ?? [])[0]
    let linkedPR: string | null = null
    if (typeof w.linkedPR === 'string') linkedPR = w.linkedPR
    else if (w.linkedPR && typeof w.linkedPR === 'object') {
      linkedPR = w.linkedPR.url || (w.linkedPR.number ? `#${w.linkedPR.number}` : null)
    }
    const rawAddr = w.address?.worktree || ''
    const canonicalId = rawAddr.startsWith('id:')
      ? rawAddr.slice(3)
      : rawAddr || String(w.worktreeId ?? '')
    return {
      canonicalId,
      worktreeId: String(w.worktreeId ?? ''),
      repo: String(w.repo ?? ''),
      path: String(w.path ?? ''),
      branch: String(w.branch ?? ''),
      displayName: String(w.displayName ?? ''),
      bot: w.bot ?? null,
      linkedPR,
      unread: Boolean(w.unread),
      lastActivityAt: typeof w.lastActivityAt === 'number' ? w.lastActivityAt : null,
      status: String(w.status ?? 'inactive'),
      agentState: primaryAgent?.state ?? null,
      agentType: primaryAgent?.agentType ?? null,
      stateStartedAt: typeof primaryAgent?.stateStartedAt === 'number' ? primaryAgent.stateStartedAt : null,
      lastMessage: primaryAgent?.lastAssistantMessage ? String(primaryAgent.lastAssistantMessage).slice(0, 160) : null
    }
  })
}

function mapOrcaCleanupRow(r: Record<string, unknown>): OrcaCleanupRow {
  const ev = (r.evaluation as { action?: string; cause?: string } | undefined) ?? undefined
  const rawOutcome = String(r.outcome ?? ev?.action ?? 'kept')
  const outcome = rawOutcome === 'keep' ? 'kept' : rawOutcome
  return {
    worktreeId: String(r.worktreeId ?? ''),
    repoName: String(r.repoName ?? ''),
    displayName: String(r.displayName ?? r.branch ?? ''),
    branch: String(r.branch ?? ''),
    path: String(r.path ?? ''),
    sizeBytes: typeof r.sizeBytes === 'number' ? r.sizeBytes : null,
    outcome,
    why: String(r.keepCause ?? ev?.cause ?? ''),
    reason: typeof r.reason === 'string' ? r.reason : null
  }
}

async function collectOrcaCleanupReport(env: ResolvedEnv): Promise<FastTelemetry['orcaCleanupReport']> {
  const res = await runExec(env.orcaBin, ['workspace', 'cleanup', 'report', '--json'], env.childEnv, 15_000)
  if (res.rc !== 0) {
    return { nextRunAt: null, lastRun: null, earlierRuns: [], error: res.stderr.trim() || 'cleanup_report_unavailable' }
  }
  const parsed = parseJsonSafe<{
    result?: {
      nextRunAt?: number | null
      runs?: {
        id?: string
        trigger?: string
        startedAt?: number
        finishedAt?: number
        counts?: { removed?: number; archived?: number; kept?: number; failed?: number }
        freedBytes?: number
        rows?: Record<string, unknown>[]
      }[]
    }
  }>(res.stdout)
  const runs: OrcaCleanupRun[] = (parsed?.result?.runs ?? []).map((run, idx) => {
    const mappedRows = idx === 0
      ? (run.rows ?? [])
          .map(mapOrcaCleanupRow)
          .sort((a, b) => (a.outcome === 'kept' ? 1 : 0) - (b.outcome === 'kept' ? 1 : 0))
          .slice(0, 15)
      : []
    return {
      id: String(run.id ?? ''),
      trigger: String(run.trigger ?? 'hourly'),
      startedAt: Number(run.startedAt ?? 0),
      finishedAt: Number(run.finishedAt ?? 0),
      removed: Number(run.counts?.removed ?? 0),
      archived: Number(run.counts?.archived ?? 0),
      kept: Number(run.counts?.kept ?? 0),
      failed: Number(run.counts?.failed ?? 0),
      freedBytes: Number(run.freedBytes ?? 0),
      rows: mappedRows
    }
  })
  const lastRun = runs[0] ?? null
  const nextRunAt =
    typeof parsed?.result?.nextRunAt === 'number'
      ? parsed.result.nextRunAt
      : lastRun && lastRun.startedAt > 0
        ? lastRun.startedAt + 3600_000
        : null
  return {
    nextRunAt,
    lastRun,
    earlierRuns: runs.slice(1, 12),
    error: null
  }
}

async function collectActiveLeases(workspaceRoot: string): Promise<SimLease[]> {
  const coordDir = path.join(workspaceRoot, 'AI', 'Data', 'SimulatorCoordination')
  let entries: fs.Dirent[]
  try {
    entries = await fs.readdir(coordDir, { withFileTypes: true })
  } catch {
    return []
  }
  const leases: SimLease[] = []
  for (const d of entries) {
    if (!d.isDirectory() || !d.name.startsWith('active-')) continue
    const txt = await readTextSafe(path.join(coordDir, d.name, 'lease.json'))
    if (!txt) continue
    const p = parseJsonSafe<Record<string, unknown>>(txt)
    leases.push({
      id: d.name,
      udid: typeof p?.udid === 'string' ? p.udid : null,
      owner: typeof p?.owner === 'string' ? p.owner : null,
      pid: typeof p?.pid === 'number' ? p.pid : null,
      createdAt: typeof p?.created_at === 'string' ? p.created_at : null
    })
  }
  return leases
}

async function collectRunningQemu(env: ResolvedEnv): Promise<QemuProc[]> {
  const res = await runExec('/usr/bin/pgrep', ['-fl', 'qemu-system-'], env.childEnv, 5_000)
  if (res.rc !== 0 || !res.stdout.trim()) return []
  return res.stdout
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter(Boolean)
    .map((line) => {
      const sp = line.indexOf(' ')
      const pid = Number(sp > 0 ? line.slice(0, sp) : line)
      const command = sp > 0 ? line.slice(sp + 1).slice(0, 140) : line
      return { pid, command }
    })
    .filter((q) => Number.isFinite(q.pid) && q.pid > 0)
}

async function scanTopDirSizes(
  parentDir: string,
  kind: 'shared_data' | 'derived_data',
  env: ResolvedEnv
): Promise<StorageTelemetry['extraDirs']> {
  let entries: fs.Dirent[]
  try {
    entries = await fs.readdir(parentDir, { withFileTypes: true })
  } catch {
    return []
  }
  const dirs = entries
    .filter((e) => e.isDirectory() && !e.name.startsWith('.'))
    .map((e) => path.join(parentDir, e.name))
    .slice(0, 30)
  if (dirs.length === 0) return []

  const du = await runExec('/usr/bin/du', ['-sk', ...dirs], env.childEnv, 35_000)
  if (!du.stdout.trim()) return []

  const results: StorageTelemetry['extraDirs'] = []
  for (const line of du.stdout.split(/\r?\n/)) {
    const m = line.match(/^(\d+)\s+(.+)$/)
    if (!m) continue
    const sizeBytes = Number(m[1]) * 1024
    const dirPath = m[2].trim()
    if (sizeBytes < 10 * 1024 * 1024) continue // skip tiny < 10 MiB dirs
    const pinned = await exists(path.join(dirPath, '.keep'))
    results.push({
      kind,
      name: path.basename(dirPath),
      path: dirPath,
      sizeBytes,
      pinned
    })
  }
  return results.sort((a, b) => b.sizeBytes - a.sizeBytes).slice(0, 15)
}

export function createNodeCollector(): OpsCollector {
  return {
    async collectFast(fallbackGcIntervalHours: number): Promise<FastTelemetry> {
      const env = await resolveEnvironment(fallbackGcIntervalHours)
      const statusDir = path.join(env.codexStorageDir, 'status')
      const logPath = path.join(env.codexStorageDir, 'logs', 'agent-hygiene.log')

      const [
        dfRes,
        stampTxt,
        lastTxt,
        logTail,
        trashBatches,
        jobs,
        bots,
        worktreePs,
        orcaCleanupReport,
        activeLeases,
        runningQemu
      ] = await Promise.all([
        runExec('/bin/df', ['-k', env.workspaceRoot], env.childEnv, 5_000),
        readTextSafe(path.join(statusDir, '.agent-gc.stamp')),
        readTextSafe(path.join(statusDir, '.agent-gc.last')),
        readTailSafe(logPath),
        collectTrashBatches(env.homeDir),
        collectLaunchJobs(env.codexStorageDir),
        collectBots(env),
        collectWorktreePs(env),
        collectOrcaCleanupReport(env),
        collectActiveLeases(env.workspaceRoot),
        collectRunningQemu(env)
      ])

      const disk = dfRes.rc === 0 ? parseDfOutput(dfRes.stdout, env.storageMode, env.workspaceRoot) : null
      const parsedStamp = stampTxt ? Number(stampTxt.trim()) : NaN
      const gcStampSec = Number.isFinite(parsedStamp) && parsedStamp > 0 ? parsedStamp : null
      const lastLines = lastTxt ? lastTxt.split(/\r?\n/).map((l) => l.trim()).filter(Boolean) : []
      const gcLastState = lastLines[0] ?? null
      const gcLastMessage = lastLines[1] ?? null
      const hygieneHistory = parseHygieneLog(logTail, Date.now())

      return {
        env,
        disk,
        gcStampSec,
        gcLastState,
        gcLastMessage,
        hygieneHistory,
        trashBatches,
        jobs,
        bots,
        worktreePs,
        orcaCleanupReport,
        activeLeases,
        runningQemu
      }
    },

    async collectGcAndPreview(env: ResolvedEnv): Promise<GcPreviewTelemetry> {
      const govRunner = env.governorBin.endsWith('.py') ? 'python3' : env.governorBin
      const govPrefix = env.governorBin.endsWith('.py') ? [env.governorBin] : []
      const extraGcArgs = buildGovernorGcArgs(env.hygieneConfig, 'daily')

      const [gcRes, previewRes] = await Promise.all([
        runExec(
          govRunner,
          [
            ...govPrefix,
            'gc',
            '--mode',
            'daily',
            '--root',
            env.projectsRoot,
            '--home',
            env.homeDir,
            ...extraGcArgs,
            '--dry-run',
            '--json'
          ],
          env.childEnv,
          45_000
        ),
        runExec(env.orcaBin, ['workspace', 'cleanup', 'preview', '--json'], env.childEnv, 20_000)
      ])

      const gcParsed = gcRes.stdout.trim() ? parseJsonSafe<Record<string, unknown>>(gcRes.stdout) : null
      const gcDryRun = gcParsed ? parseGcReport(gcParsed) : null

      let orcaCleanupPreview: OrcaCleanupPreview | null = null
      if (previewRes.rc === 0) {
        const pp = parseJsonSafe<{
          result?: { evaluatedAt?: number; rows?: Record<string, unknown>[] }
        }>(previewRes.stdout)
        if (pp?.result) {
          const rows = (pp.result.rows ?? []).map(mapOrcaCleanupRow)
          const removeRows = rows.filter((r) => r.outcome === 'remove' || r.outcome === 'archive')
          orcaCleanupPreview = {
            evaluatedAt: Number(pp.result.evaluatedAt ?? Date.now()),
            wouldRemoveCount: removeRows.length,
            wouldRemoveBytes: removeRows.reduce((s, r) => s + (r.sizeBytes ?? 0), 0),
            keptCount: rows.length - removeRows.length,
            rows: [
              ...removeRows,
              ...rows
                .filter((r) => r.outcome === 'kept' && (r.sizeBytes ?? 0) > 0)
                .sort((a, b) => (b.sizeBytes ?? 0) - (a.sizeBytes ?? 0))
                .slice(0, 35)
            ]
          }
        }
      }

      return { gcDryRun, orcaCleanupPreview }
    },

    async collectHeavyStorage(env: ResolvedEnv): Promise<HeavyStorageTelemetry> {
      const govRunner = env.governorBin.endsWith('.py') ? 'python3' : env.governorBin
      const govPrefix = env.governorBin.endsWith('.py') ? [env.governorBin] : []

      const [triageRes, dataDirs, ddDirs] = await Promise.all([
        runExec(
          govRunner,
          [...govPrefix, 'triage', '--root', env.projectsRoot, '--json'],
          env.childEnv,
          45_000
        ),
        scanTopDirSizes(path.join(env.workspaceRoot, 'AI', 'Data'), 'shared_data', env),
        scanTopDirSizes(path.join(env.homeDir, 'Library', 'Developer', 'Xcode', 'DerivedData'), 'derived_data', env)
      ])

      const triage = triageRes.stdout.trim() ? parseJsonSafe<TriageRaw>(triageRes.stdout) : null
      return {
        triage,
        extraDirs: [...dataDirs, ...ddDirs]
      }
    },

    async collectStorage(env: ResolvedEnv): Promise<StorageTelemetry> {
      const [gcPrev, heavy] = await Promise.all([
        this.collectGcAndPreview(env),
        this.collectHeavyStorage(env)
      ])
      return { ...gcPrev, ...heavy }
    },

    async collectHealth(env: ResolvedEnv): Promise<HealthTelemetry> {
      const govRunner = env.governorBin.endsWith('.py') ? 'python3' : env.governorBin
      const govPrefix = env.governorBin.endsWith('.py') ? [env.governorBin] : []

      const [doctorRes, auditRes] = await Promise.all([
        env.doctorScript
          ? runExec('python3', [env.doctorScript], env.childEnv, 45_000)
          : Promise.resolve({ rc: 2, stdout: '', stderr: 'doctor.py not found' }),
        runExec(
          govRunner,
          [...govPrefix, 'audit', '--root', env.projectsRoot, '--home', env.homeDir, '--json'],
          env.childEnv,
          35_000
        )
      ])

      const now = Date.now()
      let doctor: DoctorSummary = { checkedAt: now, ok: null, attention: [], advice: [], error: null }
      if (doctorRes.rc <= 1 && doctorRes.stdout.trim()) {
        const dp = parseJsonSafe<{ summary?: { ok?: boolean; attention?: string[]; advice?: string[] } }>(doctorRes.stdout)
        if (dp?.summary) {
          doctor = {
            checkedAt: now,
            ok: Boolean(dp.summary.ok),
            attention: Array.isArray(dp.summary.attention) ? dp.summary.attention : [],
            advice: Array.isArray(dp.summary.advice) ? dp.summary.advice : [],
            error: null
          }
        } else {
          doctor.error = 'doctor_json_invalid'
        }
      } else {
        doctor.error = doctorRes.stderr.trim() || `doctor_exit_${doctorRes.rc}`
      }

      let audit: AuditSummary = {
        checkedAt: now,
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
      if (auditRes.stdout.trim()) {
        const ap = parseJsonSafe<{
          global_rules?: { in_sync?: boolean; files?: Record<string, { estimated_tokens?: number }> }
          skills_budget?: {
            installed_count?: number
            startup_frontmatter_tokens_est?: number
            oversized_descriptions?: string[]
            oversized_skill_files?: string[]
          }
          mcp_servers?: { claude_global?: number; codex_global?: number }
          projects_audited?: number
          projects?: {
            name?: string
            path?: string
            agents_lines?: number
            agents_tokens?: number
            issues?: string[]
            smells?: string[]
          }[]
          summary?: { healthy?: boolean; attention?: string[]; advice?: string[] }
        }>(auditRes.stdout)
        if (ap) {
          const firstRuleFile = Object.values(ap.global_rules?.files ?? {})[0]
          const flaggedProjects: AuditProjectSummary[] = (ap.projects ?? [])
            .filter((p) => (p.issues?.length ?? 0) > 0 || (p.smells?.length ?? 0) > 0)
            .map((p) => ({
              name: String(p.name ?? ''),
              path: String(p.path ?? ''),
              agentsLines: Number(p.agents_lines ?? 0),
              agentsTokens: Number(p.agents_tokens ?? 0),
              issues: p.issues ?? [],
              smells: p.smells ?? []
            }))
            .sort((a, b) => b.agentsTokens - a.agentsTokens)
          audit = {
            checkedAt: now,
            healthy: ap.summary?.healthy ?? null,
            globalRulesInSync: ap.global_rules?.in_sync ?? null,
            globalTokens: Number(firstRuleFile?.estimated_tokens ?? 0),
            skillsCount: Number(ap.skills_budget?.installed_count ?? 0),
            skillsTokens: Number(ap.skills_budget?.startup_frontmatter_tokens_est ?? 0),
            oversizedSkills: [
              ...(ap.skills_budget?.oversized_descriptions ?? []),
              ...(ap.skills_budget?.oversized_skill_files ?? [])
            ],
            mcpClaude: Number(ap.mcp_servers?.claude_global ?? 0),
            mcpCodex: Number(ap.mcp_servers?.codex_global ?? 0),
            projectsAudited: Number(ap.projects_audited ?? 0),
            attention: ap.summary?.attention ?? [],
            advice: ap.summary?.advice ?? [],
            flaggedProjects,
            error: null
          }
        } else {
          audit.error = 'audit_json_invalid'
        }
      } else {
        audit.error = auditRes.stderr.trim() || `audit_exit_${auditRes.rc}`
      }

      return { doctor, audit }
    },

    async collectMemory(env: ResolvedEnv): Promise<MemoryDiagnostics | null> {
      const res = await runExec(env.orcaBin, ['diagnostics', 'memory', '--json'], env.childEnv, 20_000)
      if (res.rc !== 0) return null
      const parsed = parseJsonSafe<{
        result?: {
          collectedAt?: number
          totalCpu?: number
          totalMemory?: number
          app?: { cpu?: number; memory?: number }
          worktrees?: {
            worktreeId?: string
            worktreeName?: string
            repoName?: string
            cpu?: number
            memory?: number
            chats?: { agent?: string; name?: string; cpu?: number; memory?: number }[]
            sessions?: { agent?: string; name?: string; cpu?: number; memory?: number }[]
          }[]
        }
      }>(res.stdout)
      const r = parsed?.result
      if (!r) return null
      return {
        collectedAt: Number(r.collectedAt ?? Date.now()),
        appMemoryBytes: Number(r.app?.memory ?? 0),
        appCpuPct: Number(r.app?.cpu ?? 0),
        totalMemoryBytes: Number(r.totalMemory ?? 0),
        totalCpuPct: Number(r.totalCpu ?? 0),
        worktrees: (r.worktrees ?? [])
          .map((w) => ({
            worktreeId: String(w.worktreeId ?? ''),
            workspaceId: null,
            worktreeName: String(w.worktreeName ?? ''),
            repoName: String(w.repoName ?? ''),
            cpuPct: Number(w.cpu ?? 0),
            memoryBytes: Number(w.memory ?? 0),
            agents: [...(w.chats ?? []), ...(w.sessions ?? [])].map((a) => ({
              agent: String(a.agent ?? 'agent'),
              name: String(a.name ?? ''),
              cpuPct: Number(a.cpu ?? 0),
              memoryBytes: Number(a.memory ?? 0)
            }))
          }))
          .sort((a, b) => b.memoryBytes - a.memoryBytes)
      }
    },

    async runGovernorGc(
      env: ResolvedEnv,
      mode: 'daily' | 'session-end' | 'deep',
      apply: boolean
    ): Promise<GcReportSummary> {
      const govRunner = env.governorBin.endsWith('.py') ? 'python3' : env.governorBin
      const govPrefix = env.governorBin.endsWith('.py') ? [env.governorBin] : []
      const extraGcArgs = buildGovernorGcArgs(env.hygieneConfig, mode)
      const args = [
        ...govPrefix,
        'gc',
        '--mode',
        mode,
        '--root',
        env.projectsRoot,
        '--home',
        env.homeDir,
        ...extraGcArgs,
        apply ? '--apply' : '--dry-run',
        '--json'
      ]
      const res = await runExec(govRunner, args, env.childEnv, 90_000)
      const parsed = parseJsonSafe<Record<string, unknown>>(res.stdout)
      if (!parsed) {
        throw Object.assign(new Error(res.stderr.trim() || 'gc_failed'), { code: 'action_failed' })
      }
      return parseGcReport(parsed)
    },

    async restoreTrashBatch(
      env: ResolvedEnv,
      manifestPath: string,
      apply: boolean
    ): Promise<{ ok: boolean; restored: number; message: string }> {
      const resolvedManifest = await fs.realpath(manifestPath).catch(() => path.resolve(manifestPath))
      if (!isAllowedManifestPath(resolvedManifest, env.homeDir)) {
        throw Object.assign(new Error('Invalid manifest path'), { code: 'invalid_params' })
      }
      const govRunner = env.governorBin.endsWith('.py') ? 'python3' : env.governorBin
      const govPrefix = env.governorBin.endsWith('.py') ? [env.governorBin] : []
      const args = [...govPrefix, 'restore', resolvedManifest, apply ? '--apply' : '--dry-run', '--json']
      const res = await runExec(govRunner, args, env.childEnv, 60_000)
      const parsed = parseJsonSafe<{
        restored_count?: number
        failed_count?: number
        skipped?: { target?: string; reason?: string }[]
        mode?: string
      }>(res.stdout)
      if (!parsed) {
        return { ok: false, restored: 0, message: res.stderr.trim() || `restore failed (rc=${res.rc})` }
      }
      const count = Number(parsed.restored_count ?? 0)
      const failedCount = Number(parsed.failed_count ?? 0)
      const skipped = Array.isArray(parsed.skipped) ? parsed.skipped : []
      const firstFail = skipped.find(
        (s) =>
          s.reason &&
          !s.reason.startsWith('non-filesystem action') &&
          !s.reason.startsWith('staged item missing')
      )
      const detail = firstFail?.reason ? `（${firstFail.reason}）` : ''
      return {
        ok: res.rc === 0 && failedCount === 0,
        restored: count,
        message: apply
          ? `已还原 ${count} 项${failedCount ? `，${failedCount} 项失败${detail}` : ''}`
          : `还原预览：可还原 ${count} 项${failedCount ? `，${failedCount} 项冲突${detail}` : ''}`
      }
    },

    async toggleKeepMarker(
      env: ResolvedEnv,
      targetPath: string,
      keep: boolean
    ): Promise<{ ok: boolean; pinned: boolean }> {
      const resolved = await fs.realpath(targetPath).catch(() => path.resolve(targetPath))
      if (!isAllowedKeepTarget(resolved, env.workspaceRoot, env.homeDir)) {
        throw Object.assign(new Error('Path is outside allowed work/AI/Data/DerivedData directories'), {
          code: 'invalid_params'
        })
      }
      const st = await fs.stat(resolved)
      if (!st.isDirectory()) {
        throw Object.assign(new Error('Target must be a directory'), { code: 'invalid_params' })
      }
      const keepFile = path.join(resolved, '.keep')
      if (keep) {
        await fs.writeFile(keepFile, `pinned by Workspace Ops Radar at ${new Date().toISOString()}\n`, 'utf8')
        return { ok: true, pinned: true }
      }
      try {
        const kst = await fs.lstat(keepFile)
        if (!kst.isFile() || kst.isSymbolicLink()) {
          throw Object.assign(new Error('.keep is not a regular file'), { code: 'invalid_params' })
        }
        await fs.unlink(keepFile)
      } catch (err) {
        if ((err as { code?: string })?.code === 'invalid_params') throw err
      }
      return { ok: true, pinned: false }
    },

    async archiveWorktree(
      env: ResolvedEnv,
      worktreePath: string,
      rawTopic?: string
    ): Promise<{ ok: boolean; archiveDir: string; topic: string; totalBytes: number; message: string }> {
      const resolved = await fs.realpath(worktreePath).catch(() => path.resolve(worktreePath))
      if (!isAllowedArchiveWorktree(resolved, env.workspaceRoot)) {
        throw Object.assign(new Error('Only task worktrees under AI/{Orca,Codex,Claude}/Worktrees may be archived'), {
          code: 'invalid_params'
        })
      }
      const st = await fs.stat(resolved)
      if (!st.isDirectory()) {
        throw Object.assign(new Error('Worktree path must be a directory'), { code: 'invalid_params' })
      }

      const topic = sanitizeArchiveTopic(rawTopic || path.basename(resolved))
      const now = new Date()
      const pad = (n: number) => String(n).padStart(2, '0')
      const dateStr = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
      const archiveRoot = path.join(env.workspaceRoot, 'AI', 'Archive')
      await fs.mkdir(archiveRoot, { recursive: true })
      let dest = path.join(archiveRoot, `${dateStr}-${topic}`)
      if (await exists(dest)) {
        const timeStr = `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
        dest = path.join(archiveRoot, `${dateStr}-${topic}-${timeStr}`)
      }
      await fs.mkdir(dest, { recursive: true })

      const artifacts: string[] = []
      const gitCheck = await runExec('/usr/bin/git', ['-C', resolved, 'rev-parse', '--is-inside-work-tree'], env.childEnv, 10_000)
      let branch = ''
      if (gitCheck.rc === 0) {
        const bundlePath = path.join(dest, 'repo.bundle')
        const bundleRes = await runExec('/usr/bin/git', ['-C', resolved, 'bundle', 'create', bundlePath, '--all'], env.childEnv, 90_000)
        if (bundleRes.rc !== 0) {
          return { ok: false, archiveDir: dest, topic, totalBytes: 0, message: `git bundle 失败：${bundleRes.stderr.trim()}` }
        }
        const verifyRes = await runExec('/usr/bin/git', ['-C', resolved, 'bundle', 'verify', bundlePath], env.childEnv, 30_000)
        if (verifyRes.rc !== 0) {
          return { ok: false, archiveDir: dest, topic, totalBytes: 0, message: `git bundle 校验失败：${verifyRes.stderr.trim()}` }
        }
        artifacts.push('repo.bundle')
        const statusRes = await runExec('/usr/bin/git', ['-C', resolved, 'status', '--porcelain=v1', '--branch'], env.childEnv, 15_000)
        await fs.writeFile(path.join(dest, 'git-status.txt'), statusRes.stdout || '', 'utf8')
        artifacts.push('git-status.txt')
        const brMatch = (statusRes.stdout || '').match(/^##\s+([^\s.]+)/)
        if (brMatch) branch = brMatch[1]
      }

      const tarPath = path.join(dest, 'worktree-snapshot.tar.gz')
      const tarRes = await runExec(
        '/usr/bin/tar',
        [
          '-czf',
          tarPath,
          '--exclude=work/_scratch',
          '--exclude=work/_tests',
          '--exclude=node_modules',
          '--exclude=.build',
          '-C',
          path.dirname(resolved),
          path.basename(resolved)
        ],
        env.childEnv,
        120_000
      )
      if (tarRes.rc !== 0) {
        return { ok: false, archiveDir: dest, topic, totalBytes: 0, message: `tar 打包失败：${tarRes.stderr.trim()}` }
      }
      artifacts.push('worktree-snapshot.tar.gz')

      const receipt = {
        archived_at: now.toISOString(),
        source_path: resolved,
        branch,
        topic,
        excluded_transient_caches: ['work/_scratch', 'work/_tests', 'node_modules', '.build'],
        worktree_kept: true
      }
      await fs.writeFile(path.join(dest, 'archive-receipt.json'), `${JSON.stringify(receipt, null, 2)}\n`, 'utf8')
      artifacts.push('archive-receipt.json')

      const shaRes = await runExec(
        '/usr/bin/shasum',
        ['-a', '256', ...artifacts.map((a) => path.join(dest, a))],
        env.childEnv,
        30_000
      )
      if (shaRes.rc !== 0) {
        return { ok: false, archiveDir: dest, topic, totalBytes: 0, message: `SHA-256 生成失败：${shaRes.stderr.trim()}` }
      }
      const normalizedSha = shaRes.stdout
        .split(/\r?\n/)
        .filter(Boolean)
        .map((line) => {
          const sp = line.indexOf('  ')
          return sp > 0 ? `${line.slice(0, sp)}  ${path.basename(line.slice(sp + 2))}` : line
        })
        .join('\n')
      await fs.writeFile(path.join(dest, 'SHA256SUMS.txt'), `${normalizedSha}\n`, 'utf8')

      let totalBytes = 0
      for (const file of [...artifacts, 'SHA256SUMS.txt']) {
        const fst = await fs.stat(path.join(dest, file)).catch(() => null)
        if (fst) totalBytes += fst.size
      }

      return {
        ok: true,
        archiveDir: dest,
        topic,
        totalBytes,
        message: `已安全归档到 ${dest.replace(env.homeDir, '~')}（含 bundle + snapshot + SHA256，原工作树保留）`
      }
    },

    async runOrcaCleanup(env: ResolvedEnv, apply: boolean): Promise<{ ok: boolean; summary: string }> {
      const res = await runExec(
        env.orcaBin,
        ['workspace', 'cleanup', apply ? 'run' : 'preview', '--json'],
        env.childEnv,
        45_000
      )
      if (res.rc !== 0) {
        return { ok: false, summary: res.stderr.trim() || 'Orca workspace cleanup failed' }
      }
      const parsed = parseJsonSafe<{
        result?: { counts?: { removed?: number; kept?: number; failed?: number }; rows?: unknown[] }
      }>(res.stdout)
      if (apply && parsed?.result?.counts) {
        const c = parsed.result.counts
        return { ok: true, summary: `Removed ${c.removed ?? 0}, kept ${c.kept ?? 0}, failed ${c.failed ?? 0}` }
      }
      const rows = parsed?.result?.rows?.length ?? 0
      return { ok: true, summary: `Preview evaluated ${rows} worktree(s)` }
    },

    async saveHygieneConfig(env: ResolvedEnv, next: HygieneConfig): Promise<HygieneConfig> {
      const orcaDir = path.join(env.homeDir, '.orca')
      await fs.mkdir(orcaDir, { recursive: true })
      const hygienePath = path.join(orcaDir, 'hygiene.env')
      const normalized = normalizeHygieneConfig({ ...next, configPath: hygienePath })
      const intervalSec = Math.round(normalized.gcIntervalHours * 3600)
      const extraArgs = buildGovernorGcArgs(normalized, 'daily').join(' ')

      const writeAtomicPrivate = async (targetPath: string, content: string): Promise<void> => {
        const tmpPath = path.join(
          path.dirname(targetPath),
          `.${path.basename(targetPath)}.tmp.${process.pid}.${Date.now()}`
        )
        await fs.writeFile(tmpPath, content, { encoding: 'utf8', mode: 0o600 })
        await fs.chmod(tmpPath, 0o600).catch(() => undefined)
        await fs.rename(tmpPath, targetPath)
      }

      const hygieneContent = [
        '# Managed by Workspace Ops Radar — sourced by agent-hygiene.sh via lib.sh',
        `export AGENT_HYGIENE_GC_INTERVAL_SEC="${intervalSec}"`,
        `export AGENT_GOVERNOR_GC_ARGS="${extraArgs}"`,
        ''
      ].join('\n')
      await writeAtomicPrivate(hygienePath, hygieneContent)

      // Mirror the two managed keys into ~/.orca/backup-config.env if it already exists so installed
      // lib.sh picks up changes immediately even before setup_backups.py refreshes Codex Storage/agents/lib.sh.
      const backupCfgPath = path.join(orcaDir, 'backup-config.env')
      const existingBackupCfg = await readTextSafe(backupCfgPath)
      if (existingBackupCfg !== null) {
        const updated = upsertEnvKeys(existingBackupCfg, {
          AGENT_HYGIENE_GC_INTERVAL_SEC: String(intervalSec),
          AGENT_GOVERNOR_GC_ARGS: extraArgs || null
        })
        await writeAtomicPrivate(backupCfgPath, updated)
      }

      return normalized
    },

    async sendToBot(
      env: ResolvedEnv,
      params: { botName: string; subject: string; body: string; mode: 'handoff' | 'status'; newChat?: boolean }
    ): Promise<{ ok: boolean; error: string | null }> {
      const cleanName = params.botName.trim()
      if (!cleanName || /[\s/\\]/.test(cleanName)) {
        return { ok: false, error: 'invalid_bot_name' }
      }
      const args = [
        'orchestration',
        'send',
        '--to',
        `bot:${cleanName}`,
        '--type',
        params.mode,
        '--subject',
        params.subject.slice(0, 180),
        '--body',
        params.body.slice(0, 8000),
        '--json'
      ]
      if (params.newChat) args.push('--chat', 'new')
      const res = await runExec(env.orcaBin, args, env.childEnv, 25_000)
      if (res.rc !== 0) {
        return { ok: false, error: res.stderr.trim() || `bot_send_exit_${res.rc}` }
      }
      return { ok: true, error: null }
    }
  }
}
