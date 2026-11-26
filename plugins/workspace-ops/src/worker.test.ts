import { assert, describe, mockHost, mountWorker, test } from 'orca/testing'
import type { FastTelemetry, HealthTelemetry, OpsCollector, ResolvedEnv, StorageTelemetry } from './collectors.ts'
import { DEFAULT_HYGIENE_CONFIG, normalizeHygieneConfig } from './core.ts'
import type { HygieneConfig, OpsSnapshot } from './protocol.ts'
import { createWorker, deactivate } from './worker.ts'

function makeEnv(): ResolvedEnv {
  return {
    homeDir: '/Users/test',
    workspaceRoot: '/Users/test/AI-Workspace',
    projectsRoot: '/Users/test/AI-Workspace/AI/Projects',
    storageMode: 'internal',
    codexStorageDir: '/Users/test/Library/Application Support/Codex Storage',
    gcIntervalSec: 14_400,
    hygieneConfig: { ...DEFAULT_HYGIENE_CONFIG, configPath: '/Users/test/.orca/hygiene.env' },
    orcaBin: '/Applications/Orca.app/Contents/Resources/bin/orca',
    governorBin: '/Users/test/.local/bin/agent-governor',
    doctorScript: '/Users/test/doctor.py',
    childEnv: {}
  }
}

function makeMockCollector(overrides: {
  jobState?: 'ok' | 'warn' | 'fail'
  sentCalls?: { botName: string; subject: string; mode: string; chatId?: string | null }[]
  gcCalls?: { mode: string; apply: boolean }[]
  keepCalls?: { path: string; keep: boolean }[]
  restoreCalls?: { manifestPath: string; apply: boolean }[]
  archiveCalls?: { path: string; topic?: string }[]
  savedHygiene?: HygieneConfig[]
  homeChatFailed?: boolean
  heavyCount?: { n: number }
} = {}): OpsCollector {
  const env = makeEnv()
  const nowSec = Math.floor(Date.now() / 1000) - 1800
  const sentCalls = overrides.sentCalls ?? []
  const gcCalls = overrides.gcCalls ?? []
  const keepCalls = overrides.keepCalls ?? []
  const restoreCalls = overrides.restoreCalls ?? []
  const archiveCalls = overrides.archiveCalls ?? []
  const savedHygiene = overrides.savedHygiene ?? []
  const heavyCount = overrides.heavyCount ?? { n: 0 }

  return {
    async collectFast(): Promise<FastTelemetry> {
      return {
        env,
        disk: {
          mount: '/',
          totalBytes: 1000 * 1024 ** 3,
          usedBytes: 500 * 1024 ** 3,
          availBytes: 500 * 1024 ** 3,
          capacityPct: 50,
          freePct: 50,
          storageMode: 'internal',
          workspaceRoot: env.workspaceRoot
        },
        gcStampSec: nowSec,
        gcLastState: 'ok',
        gcLastMessage: 'Cache GC: reclaimed 4 item(s) (11.5 MiB), 161 protected item(s) skipped',
        hygieneHistory: {
          runs: [],
          trendDays: [],
          totalReclaimed24hBytes: 11.5 * 1024 ** 2,
          totalReclaimed7dBytes: 2 * 1024 ** 3,
          totalReclaimed24hItems: 4
        },
        trashBatches: [
          {
            id: 'agent-governor-2026-11-25/gc-daily-1',
            manifestPath: '/Users/test/.Trash/agent-governor-2026-11-25/gc-daily-1/manifest.json',
            createdAt: Date.now() - 1800_000,
            mode: 'daily',
            inTrash: true,
            restoredAt: null,
            totalItems: 2,
            totalBytes: 10 * 1024 ** 2,
            largestItem: null,
            categories: [{ category: 'work_tests', count: 2, bytes: 10 * 1024 ** 2 }],
            items: []
          }
        ],
        jobs: [
          {
            name: 'backup-hot',
            state: overrides.jobState ?? 'ok',
            time: '2026-11-25 23:00:00',
            lastOk: '2026-11-25 23:00:00',
            message: overrides.jobState === 'fail' ? 'NAS offline' : 'ok',
            seconds: 40
          }
        ],
        bots: [
          {
            id: 'bot-ram',
            name: 'Ram',
            agent: 'claude',
            paused: false,
            specialty: 'Cross-project progress monitor',
            chatCount: 2,
            chats: [
              {
                id: 'chat-home',
                title: 'Home',
                displayTitle: '主聊天',
                agent: 'claude',
                home: true,
                closed: false,
                failed: Boolean(overrides.homeChatFailed),
                failedAt: overrides.homeChatFailed ? Date.now() - 5_000 : null,
                lastActivityAt: Date.now() - 5_000,
                lastAsked: '',
                lastSaid: ''
              },
              {
                id: 'chat-agy',
                title: '',
                displayTitle: 'Antigravity Chat',
                agent: 'antigravity',
                home: false,
                closed: false,
                failed: false,
                failedAt: null,
                lastActivityAt: Date.now() - 10_000,
                lastAsked: '',
                lastSaid: ''
              }
            ]
          }
        ],
        worktreePs: [],
        orcaCleanupReport: { nextRunAt: Date.now() + 1800_000, lastRun: null, earlierRuns: [], error: null },
        activeLeases: [],
        runningQemu: []
      }
    },
    async collectGcAndPreview() {
      return {
        gcDryRun: {
          mode: 'daily',
          execution: 'dry-run',
          totalCandidates: 2,
          reclaimableBytes: 50 * 1024 ** 2,
          reclaimableHuman: '50.0 MiB',
          protectedSkipsCount: 10,
          categories: [{ category: 'work_scratch', count: 2, bytes: 50 * 1024 ** 2 }],
          candidates: [
            {
              category: 'work_scratch',
              path: '/Users/test/AI-Workspace/AI/Projects/Mac/work/_scratch/old',
              sizeBytes: 50 * 1024 ** 2,
              sizeHuman: '50.0 MiB',
              ageHours: 80,
              reason: 'expired'
            }
          ],
          ephemeralSimulators: [],
          launchservicesGhostsCount: 0,
          manifestPath: null,
          errors: []
        },
        orcaCleanupPreview: null
      }
    },
    async collectHeavyStorage() {
      heavyCount.n += 1
      return {
        triage: {
          total_legacy_items: 1,
          total_legacy_bytes: 5 * 1024 ** 3,
          projects: [
            {
              project: 'Phayen',
              largest_items: [
                {
                  name: 'legacy-dir',
                  path: '/Users/test/AI-Workspace/AI/Projects/Phayen/work/legacy-dir',
                  size_bytes: 5 * 1024 ** 3,
                  files: 100,
                  age_days: 10,
                  pinned: false
                }
              ]
            }
          ],
          worktree_stranded_work: []
        },
        extraDirs: []
      }
    },
    async collectStorage(): Promise<StorageTelemetry> {
      const [gp, hs] = await Promise.all([this.collectGcAndPreview(env), this.collectHeavyStorage(env)])
      return { ...gp, ...hs }
    },
    async collectHealth(): Promise<HealthTelemetry> {
      return {
        doctor: { checkedAt: Date.now(), ok: true, attention: [], advice: [], error: null },
        audit: {
          checkedAt: Date.now(),
          healthy: true,
          globalRulesInSync: true,
          globalTokens: 703,
          skillsCount: 12,
          skillsTokens: 1773,
          oversizedSkills: [],
          mcpClaude: 1,
          mcpCodex: 3,
          projectsAudited: 19,
          attention: [],
          advice: [],
          flaggedProjects: [],
          error: null
        }
      }
    },
    async collectMemory() {
      return {
        collectedAt: Date.now(),
        appMemoryBytes: 500 * 1024 ** 2,
        appCpuPct: 2.5,
        totalMemoryBytes: 1500 * 1024 ** 2,
        totalCpuPct: 12,
        worktrees: []
      }
    },
    async runGovernorGc(_env, mode, apply) {
      gcCalls.push({ mode, apply })
      return {
        mode,
        execution: apply ? 'applied' : 'dry-run',
        totalCandidates: 1,
        reclaimableBytes: 10 * 1024 ** 2,
        reclaimableHuman: '10.0 MiB',
        protectedSkipsCount: 10,
        categories: [],
        candidates: [],
        ephemeralSimulators: [],
        launchservicesGhostsCount: 0,
        manifestPath: apply ? '/Users/test/.Trash/agent-governor-2026-11-25/gc-daily-2/manifest.json' : null,
        errors: []
      }
    },
    async restoreTrashBatch(_env, manifestPath, apply) {
      restoreCalls.push({ manifestPath, apply })
      return { ok: true, restored: 2, message: apply ? 'Restored 2 item(s)' : 'Preview: would restore 2 item(s)' }
    },
    async toggleKeepMarker(_env, targetPath, keep) {
      keepCalls.push({ path: targetPath, keep })
      return { ok: true, pinned: keep }
    },
    async archiveWorktree(_env, worktreePath, topic) {
      archiveCalls.push({ path: worktreePath, topic })
      return {
        ok: true,
        archiveDir: `/Users/test/AI-Workspace/AI/Archive/2026-11-26-${topic || 'wt'}`,
        topic: topic || 'wt',
        totalBytes: 4096,
        message: '已安全归档'
      }
    },
    async runOrcaCleanup(_env, apply) {
      return { ok: true, summary: apply ? 'Removed 0, kept 5, failed 0' : 'Preview evaluated 5 worktree(s)' }
    },
    async saveHygieneConfig(_env, next) {
      const normalized = normalizeHygieneConfig(next)
      savedHygiene.push(normalized)
      return normalized
    },
    async sendToBot(_env, params) {
      sentCalls.push({ botName: params.botName, subject: params.subject, mode: params.mode, chatId: params.chatId ?? null })
      return { ok: true, error: null }
    }
  }
}

const CAPABILITIES = [
  'workspace:read',
  'workspace:write',
  'agent:read',
  'notifications:show',
  'storage:own',
  'settings:own'
] as const

describe('Workspace Ops Radar worker', () => {
  test('boots, populates snapshot, handles live agent status event and actions', async () => {
    const gcCalls: { mode: string; apply: boolean }[] = []
    const keepCalls: { path: string; keep: boolean }[] = []
    const restoreCalls: { manifestPath: string; apply: boolean }[] = []
    const sentCalls: { botName: string; subject: string; mode: string }[] = []

    const host = mockHost({
      capabilities: CAPABILITIES,
      settings: { botName: 'Ram', botAlertMode: 'manual', desktopNotifications: true },
      results: {
        'workspace.list': {
          workspaces: [
            {
              id: 'ws-1',
              kind: 'worktree',
              name: 'Mac｜Ops Radar',
              project: 'Mac',
              path: '/Users/test/AI-Workspace/AI/Codex/Worktrees/Mac/ops',
              branch: 'codex/ops',
              hostId: 'local',
              status: { state: 'working', unread: false, terminalCount: 1, lastActivityAt: Date.now() }
            }
          ],
          truncated: false
        },
        'workspace.open': { opened: true },
        'statusBar.update': { ok: true },
        'notifications.show': { delivered: true }
      }
    })

    const worker = await mountWorker(
      { default: createWorker(() => makeMockCollector({ gcCalls, keepCalls, restoreCalls, sentCalls })), deactivate },
      {
        host,
        events: ['workspace.created', 'workspace.removed', 'agent.status.changed'],
        commands: ['ops.summary', 'ops.refresh', 'ops.gcPreview', 'ops.gcDaily', 'ops.escalateBot']
      }
    )

    try {
      const snap = (await worker.request('ops.refresh', { scope: 'all' })) as OpsSnapshot
      assert.equal(snap.status, 'ready')
      assert.equal(snap.live.bots[0].name, 'Ram')
      assert.equal(snap.live.workspaces[0].state, 'working')
      assert.equal(snap.storage.topOffenders.length, 2)

      // Real-time agent event transitions workspace to waiting immediately
      await worker.emit('agent.status.changed', {
        workspaceId: 'ws-1',
        workspaceListId: 'ws-1',
        paneKey: 'pane-1',
        state: 'waiting',
        receivedAt: Date.now() - 20 * 60_000
      })
      const afterEvent = (await worker.request('ops.snapshot')) as OpsSnapshot
      assert.equal(afterEvent.live.workspaces[0].state, 'waiting')
      assert.equal(afterEvent.counts.staleWaitingAgents, 1)

      // Run GC preview & apply
      const afterGc = (await worker.request('ops.runGc', { mode: 'daily', apply: false })) as OpsSnapshot
      assert.equal(gcCalls.length, 1)
      assert.equal(gcCalls[0].apply, false)
      assert.ok(afterGc.lastActionNotice?.message.includes('预览完成'))

      // Toggle .keep marker
      await worker.request('ops.toggleKeep', {
        path: '/Users/test/AI-Workspace/AI/Projects/Phayen/work/legacy-dir',
        keep: true
      })
      assert.equal(keepCalls.length, 1)
      assert.equal(keepCalls[0].keep, true)

      // Restore Trash batch preview
      await worker.request('ops.restoreBatch', {
        manifestPath: '/Users/test/.Trash/agent-governor-2026-11-25/gc-daily-1/manifest.json',
        apply: false
      })
      assert.equal(restoreCalls.length, 1)

      // Open workspace
      const opened = (await worker.request('ops.openWorkspace', { workspaceId: 'ws-1' })) as { opened: boolean }
      assert.equal(opened.opened, true)

      // Manual escalate to Ram
      const afterBot = (await worker.request('ops.escalateBot', {
        mode: 'handoff',
        customNote: '请帮我核对等待中的工作区'
      })) as OpsSnapshot
      assert.equal(sentCalls.length, 1)
      assert.equal(sentCalls[0].botName, 'Ram')
      assert.equal(afterBot.botDispatchHistory.length, 1)

      // Palette command summary
      const summary = await worker.command('ops.summary')
      assert.equal(typeof summary, 'string')
      assert.ok(String(summary).includes('Ops Radar'))
    } finally {
      await worker.deactivate()
    }
  })

  test('auto-escalates critical alert to Ram once and deduplicates within cooldown', async () => {
    const sentCalls: { botName: string; subject: string; mode: string }[] = []
    const host = mockHost({
      capabilities: CAPABILITIES,
      settings: { botName: 'Ram', botAlertMode: 'critical', desktopNotifications: true },
      results: {
        'workspace.list': { workspaces: [], truncated: false },
        'statusBar.update': { ok: true },
        'notifications.show': { delivered: true }
      }
    })

    const worker = await mountWorker(
      { default: createWorker(() => makeMockCollector({ jobState: 'fail', sentCalls })), deactivate },
      {
        host,
        events: ['workspace.created', 'workspace.removed', 'agent.status.changed'],
        commands: ['ops.summary', 'ops.refresh', 'ops.gcPreview', 'ops.gcDaily', 'ops.escalateBot']
      }
    )

    try {
      const snap = (await worker.request('ops.snapshot')) as OpsSnapshot
      assert.equal(snap.counts.criticalAlerts, 1)
      assert.equal(sentCalls.length, 1)
      assert.equal(sentCalls[0].botName, 'Ram')

      // Trigger another fast refresh — cooldown must prevent duplicate spam to Ram
      await worker.request('ops.refresh', { scope: 'fast' })
      assert.equal(sentCalls.length, 1)
    } finally {
      await worker.deactivate()
    }
  })

  test('retains archived Trash batch metadata after Trash is emptied', async () => {
    let trashPresent = true
    const base = makeMockCollector()
    const collector: OpsCollector = {
      ...base,
      async collectFast(hours) {
        const res = await base.collectFast(hours)
        return trashPresent ? res : { ...res, trashBatches: [] }
      }
    }
    const host = mockHost({
      capabilities: CAPABILITIES,
      settings: { botName: 'Ram', botAlertMode: 'manual', desktopNotifications: false },
      results: {
        'workspace.list': { workspaces: [], truncated: false },
        'statusBar.update': { ok: true }
      }
    })
    const worker = await mountWorker(
      { default: createWorker(() => collector), deactivate },
      {
        host,
        events: ['workspace.created', 'workspace.removed', 'agent.status.changed'],
        commands: ['ops.summary', 'ops.refresh', 'ops.gcPreview', 'ops.gcDaily', 'ops.escalateBot']
      }
    )
    try {
      const first = (await worker.request('ops.refresh', { scope: 'fast' })) as OpsSnapshot
      assert.equal(first.cleanup.governor.recentBatches.length, 1)
      assert.equal(first.cleanup.governor.recentBatches[0].inTrash, true)

      trashPresent = false
      const second = (await worker.request('ops.refresh', { scope: 'fast' })) as OpsSnapshot
      assert.equal(second.cleanup.governor.recentBatches.length, 1)
      assert.equal(second.cleanup.governor.recentBatches[0].inTrash, false)
      assert.equal(second.counts.trashBatches, 0)
    } finally {
      await worker.deactivate()
    }
  })

  test('deduplicates canonical identity vs legacy repoId::path worktrees and attaches memory stats', async () => {
    const base = makeMockCollector()
    const wtPath = '/Users/test/AI-Workspace/AI/Orca/Worktrees/WanFuse/seabiscuit'
    const canonicalId = 'identity:wt2:local:b9303dd3-3bad-4091-a392-1c62d225cbfa'
    const legacyId = `repo-1::${wtPath}`

    const collector: OpsCollector = {
      ...base,
      async collectFast(hours) {
        const res = await base.collectFast(hours)
        return {
          ...res,
          worktreePs: [
            {
              canonicalId,
              worktreeId: legacyId,
              repo: 'WanFuse',
              path: wtPath,
              branch: 'codex/mvp',
              displayName: 'WanFuse｜MVP',
              bot: null,
              linkedPR: '#2',
              unread: false,
              lastActivityAt: Date.now() - 60_000,
              status: 'active',
              agentState: 'working',
              agentType: 'claude',
              stateStartedAt: Date.now() - 60_000,
              lastMessage: 'building'
            }
          ]
        }
      },
      async collectMemory() {
        return {
          collectedAt: Date.now(),
          appMemoryBytes: 500 * 1024 ** 2,
          appCpuPct: 1.5,
          totalMemoryBytes: 1200 * 1024 ** 2,
          totalCpuPct: 10,
          worktrees: [
            {
              worktreeId: legacyId,
              workspaceId: null,
              worktreeName: 'WanFuse｜MVP',
              repoName: 'WanFuse',
              cpuPct: 8.5,
              memoryBytes: 700 * 1024 ** 2,
              agents: []
            }
          ]
        }
      }
    }

    const host = mockHost({
      capabilities: CAPABILITIES,
      settings: { botName: 'Ram', botAlertMode: 'manual', desktopNotifications: false },
      results: {
        'workspace.list': {
          workspaces: [
            {
              id: canonicalId,
              kind: 'worktree',
              name: 'WanFuse｜MVP',
              project: 'WanFuse',
              path: wtPath,
              branch: 'codex/mvp',
              hostId: 'local',
              status: { state: 'working', unread: false, terminalCount: 1, lastActivityAt: Date.now() - 30_000 }
            }
          ],
          truncated: false
        },
        'statusBar.update': { ok: true }
      }
    })

    const worker = await mountWorker(
      { default: createWorker(() => collector), deactivate },
      {
        host,
        events: ['workspace.created', 'workspace.removed', 'agent.status.changed'],
        commands: ['ops.summary', 'ops.refresh', 'ops.gcPreview', 'ops.gcDaily', 'ops.escalateBot']
      }
    )

    try {
      const snap = (await worker.request('ops.refresh', { scope: 'all' })) as OpsSnapshot
      // Must NOT duplicate the worktree under both canonicalId and legacyId
      assert.equal(snap.live.totalWorkspaceCount, 1)
      assert.equal(snap.live.workspaces.length, 1)
      assert.equal(snap.live.workspaces[0].id, canonicalId)
      assert.equal(snap.live.workspaces[0].linkedPR, '#2')
      assert.equal(snap.live.workspaces[0].memoryBytes, 700 * 1024 ** 2)
      assert.equal(snap.live.memory?.worktrees[0].workspaceId, canonicalId)
    } finally {
      await worker.deactivate()
    }
  })

  test('caches heavy storage for 30m on normal refresh and archives task worktrees safely', async () => {
    const heavyCount = { n: 0 }
    const archiveCalls: { path: string; topic?: string }[] = []
    const host = mockHost({
      capabilities: CAPABILITIES,
      settings: { botName: 'Ram', botAlertMode: 'manual', desktopNotifications: false },
      results: {
        'workspace.list': { workspaces: [], truncated: false },
        'statusBar.update': { ok: true }
      }
    })
    const worker = await mountWorker(
      { default: createWorker(() => makeMockCollector({ heavyCount, archiveCalls })), deactivate },
      {
        host,
        events: ['workspace.created', 'workspace.removed', 'agent.status.changed'],
        commands: ['ops.summary', 'ops.refresh', 'ops.gcPreview', 'ops.gcDaily', 'ops.escalateBot']
      }
    )
    try {
      // Explicit storage scan runs heavy once
      await worker.request('ops.refresh', { scope: 'storage' })
      const afterFirst = heavyCount.n
      // Subsequent scope='all' within 30m must NOT re-run heavy storage
      await worker.request('ops.refresh', { scope: 'all' })
      assert.equal(heavyCount.n, afterFirst)
      // Explicit scope='storage' forces a fresh heavy rescan
      await worker.request('ops.refresh', { scope: 'storage' })
      assert.equal(heavyCount.n, afterFirst + 1)

      // Archive worktree records history
      const afterArchive = (await worker.request('ops.archiveWorktree', {
        path: '/Users/test/AI-Workspace/AI/Codex/Worktrees/Mac/ops',
        topic: 'ops-radar'
      })) as OpsSnapshot
      assert.equal(archiveCalls.length, 1)
      assert.equal(afterArchive.archiveHistory.length, 1)
      assert.equal(afterArchive.archiveHistory[0].ok, true)
    } finally {
      await worker.deactivate()
    }
  })

  test('saves hygiene schedule/TTL config and batch plugin settings live', async () => {
    const savedHygiene: HygieneConfig[] = []
    const host = mockHost({
      capabilities: CAPABILITIES,
      settings: { botName: 'Ram', botAlertMode: 'critical', desktopNotifications: true, autoRefreshSeconds: 60 },
      results: {
        'workspace.list': { workspaces: [], truncated: false },
        'statusBar.update': { ok: true }
      }
    })
    const worker = await mountWorker(
      { default: createWorker(() => makeMockCollector({ savedHygiene })), deactivate },
      {
        host,
        events: ['workspace.created', 'workspace.removed', 'agent.status.changed'],
        commands: ['ops.summary', 'ops.refresh', 'ops.gcPreview', 'ops.gcDaily', 'ops.escalateBot']
      }
    )
    try {
      const updated = (await worker.request('ops.saveHygieneConfig', {
        gcIntervalHours: 2,
        scratchTtlDays: 2,
        testsTtlDays: 5,
        derivedDataTtlHours: 6,
        skipXcode: true
      })) as OpsSnapshot
      assert.equal(savedHygiene.length, 1)
      assert.equal(updated.hygieneConfig.gcIntervalHours, 2)
      assert.equal(updated.hygieneConfig.scratchTtlDays, 2)
      assert.equal(updated.hygieneConfig.skipXcode, true)
      assert.equal(updated.cleanup.governor.intervalSec, 7200)

      const reset = (await worker.request('ops.saveHygieneConfig', { reset: true })) as OpsSnapshot
      assert.equal(reset.hygieneConfig.gcIntervalHours, 4)
      assert.equal(reset.hygieneConfig.scratchTtlDays, 3)
      assert.equal(reset.hygieneConfig.skipXcode, false)

      const settingsOut = (await worker.request('ops.updateSettings', {
        diskWarnFreePercent: 20,
        waitingAlertMinutes: 10,
        autoRefreshSeconds: 120,
        desktopNotifications: false
      })) as OpsSnapshot
      assert.equal(settingsOut.settings.diskWarnFreePercent, 20)
      assert.equal(settingsOut.settings.waitingAlertMinutes, 10)
      assert.equal(settingsOut.settings.autoRefreshSeconds, 120)
      assert.equal(settingsOut.settings.desktopNotifications, false)
    } finally {
      await worker.deactivate()
    }
  })

  test('automatically falls back to Antigravity Chat when Home Chat is failed and allows pinning any chat', async () => {
    const sentCalls: { botName: string; subject: string; mode: string; chatId?: string | null }[] = []
    const host = mockHost({
      capabilities: CAPABILITIES,
      settings: { botName: 'Ram', botAlertMode: 'manual', desktopNotifications: false },
      results: {
        'workspace.list': { workspaces: [], truncated: false },
        'statusBar.update': { ok: true }
      }
    })
    const worker = await mountWorker(
      { default: createWorker(() => makeMockCollector({ homeChatFailed: true, sentCalls })), deactivate },
      {
        host,
        events: ['workspace.created', 'workspace.removed', 'agent.status.changed'],
        commands: ['ops.summary', 'ops.refresh', 'ops.gcPreview', 'ops.gcDaily', 'ops.escalateBot']
      }
    )
    try {
      await worker.request('ops.refresh', { scope: 'fast' })
      // Auto mode must bypass failed Claude Home Chat and dispatch straight to Antigravity Chat
      const afterAuto = (await worker.request('ops.escalateBot', { mode: 'handoff' })) as OpsSnapshot
      assert.equal(sentCalls.length, 1)
      assert.equal(sentCalls[0].chatId, 'chat-agy')
      assert.equal(afterAuto.botDispatchHistory[0].chatTitle, 'Antigravity Chat')
      assert.equal(afterAuto.botDispatchHistory[0].fallbackFromHome, true)

      // User can also explicitly pin a chat or '+ new' via ops.updateSetting('botChatId', ...)
      const pinnedNew = (await worker.request('ops.updateSetting', { key: 'botChatId', value: 'new' })) as OpsSnapshot
      assert.equal(pinnedNew.settings.botChatId, 'new')
      await worker.request('ops.escalateBot', { mode: 'handoff' })
      assert.equal(sentCalls[1].chatId, 'new')
    } finally {
      await worker.deactivate()
    }
  })
})
