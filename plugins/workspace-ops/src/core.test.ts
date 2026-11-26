import { assert, describe, test } from 'orca/testing'
import {
  buildBotEscalationBrief,
  buildGovernorGcArgs,
  buildStatusBarState,
  buildStorageOffenders,
  computeNextGcAt,
  DAY_MS,
  DEFAULT_HYGIENE_CONFIG,
  evaluateAlerts,
  formatBytes,
  formatDurationCompact,
  HOUR_MS,
  isAllowedArchiveWorktree,
  isAllowedKeepTarget,
  isAllowedManifestPath,
  MIN_MS,
  parseDfOutput,
  parseGovernorGcArgs,
  parseHumanBytes,
  parseHygieneLog,
  sanitizeArchiveTopic,
  selectBotChat,
  upsertEnvKeys
} from './core.ts'
import type { BotInfo, OpsSettings } from './protocol.ts'

const BASE_SETTINGS: OpsSettings = {
  botName: 'Ram',
  botChatId: 'auto',
  botAlertMode: 'critical',
  desktopNotifications: true,
  diskWarnFreePercent: 15,
  waitingAlertMinutes: 15,
  gcSpikeGiB: 5,
  gcIntervalHours: 4,
  autoRefreshSeconds: 60
}

describe('formatting & parsing helpers', () => {
  test('formatBytes and parseHumanBytes round-trip common units', () => {
    assert.equal(formatBytes(0), '0 B')
    assert.equal(formatBytes(1024), '1.00 KiB')
    assert.equal(formatBytes(11.5 * 1024 ** 2), '11.5 MiB')
    assert.equal(formatBytes(2.1 * 1024 ** 3), '2.10 GiB')
    assert.equal(parseHumanBytes('11.5 MiB'), Math.round(11.5 * 1024 ** 2))
    assert.equal(parseHumanBytes('2.1 GiB'), Math.round(2.1 * 1024 ** 3))
    assert.equal(parseHumanBytes('0 B'), 0)
    assert.equal(parseHumanBytes('invalid'), 0)
  })

  test('formatDurationCompact and computeNextGcAt', () => {
    assert.equal(formatDurationCompact(-5000, true), '已到期')
    assert.equal(formatDurationCompact(25 * MIN_MS, false), '25m')
    assert.equal(formatDurationCompact(3 * HOUR_MS + 18 * MIN_MS, true), '3h 18m')
    assert.equal(computeNextGcAt(1_700_000_000, 14_400, 4), (1_700_000_000 + 14_400) * 1000)
    assert.equal(computeNextGcAt(null, 14_400, 4), null)
  })

  test('parseDfOutput extracts macOS df -k fields', () => {
    const sample = [
      'Filesystem   1024-blocks      Used Available Capacity iused     ifree %iused  Mounted on',
      '/dev/disk3s5  1942700360 912000000 1009778688    48% 5600000 10000000    0%   /System/Volumes/Data'
    ].join('\n')
    const disk = parseDfOutput(sample, 'internal', '/Users/kafu/AI-Workspace')
    assert.ok(disk)
    assert.equal(disk.capacityPct, 48)
    assert.equal(disk.mount, '/System/Volumes/Data')
    assert.equal(disk.storageMode, 'internal')
    assert.ok(disk.freePct > 50 && disk.freePct < 53)
  })

  test('parseHygieneLog aggregates 24h, 7d, failure lines and daily trend buckets', () => {
    const now = Date.parse('2026-11-25T23:30:00')
    const log = [
      '2026-11-24 19:08:22 gc rc=0 state=ok: Cache GC: reclaimed 21 item(s) (784.0 MiB), 93 protected item(s) skipped',
      '2026-11-25 15:11:03 gc rc=0 state=ok: Cache GC: reclaimed 5 item(s) (2.1 GiB), unregistered 2 ghost app(s), 126 protected item(s) skipped',
      '2026-11-25 20:00:00 gc rc=1 state=fail: Cache GC aborted (error): permission denied',
      '2026-11-25 23:12:08 gc rc=0 state=ok: Cache GC: reclaimed 4 item(s) (11.5 MiB), 161 protected item(s) skipped'
    ].join('\n')
    const parsed = parseHygieneLog(log, now)
    assert.equal(parsed.runs.length, 4)
    assert.equal(parsed.runs[0].reclaimedItems, 4)
    assert.equal(parsed.runs[1].state, 'fail')
    assert.ok(parsed.runs[1].raw.includes('aborted'))
    assert.equal(parsed.runs[2].ghostApps, 2)
    assert.equal(parsed.totalReclaimed24hItems, 9)
    assert.ok(parsed.totalReclaimed24hBytes > 2.1 * 1024 ** 3)
    assert.ok(parsed.totalReclaimed7dBytes > parsed.totalReclaimed24hBytes)
    assert.equal(parsed.trendDays.length, 7)
    assert.equal(parsed.trendDays[6].date, '2026-11-25')
    assert.equal(parsed.trendDays[6].runs, 3)
  })
})

describe('storage ranker & security path allowlists', () => {
  test('buildStorageOffenders merges triage, GC candidates, worktrees and extra dirs sorted by size', () => {
    const res = buildStorageOffenders({
      triage: {
        total_legacy_items: 2,
        total_legacy_bytes: 20 * 1024 ** 3,
        projects: [
          {
            project: 'Phayen',
            largest_items: [
              {
                name: 'old-baseline',
                path: '/Users/kafu/AI-Workspace/AI/Projects/Phayen/work/old-baseline',
                size_bytes: 12 * 1024 ** 3,
                files: 500,
                age_days: 15,
                pinned: true
              }
            ]
          }
        ],
        worktree_stranded_work: [
          {
            worktree: 'Orca/Worktrees/Wayline/wt1',
            work_path: '/Users/kafu/AI-Workspace/AI/Orca/Worktrees/Wayline/wt1/work',
            size_bytes: 14 * 1024 ** 3,
            files: 1200,
            age_days: 1
          }
        ]
      },
      gcDryRun: {
        mode: 'daily',
        execution: 'dry-run',
        totalCandidates: 1,
        reclaimableBytes: 500 * 1024 ** 2,
        reclaimableHuman: '500 MiB',
        protectedSkipsCount: 10,
        categories: [{ category: 'work_scratch', count: 1, bytes: 500 * 1024 ** 2 }],
        candidates: [
          {
            category: 'work_scratch',
            path: '/Users/kafu/AI-Workspace/AI/Projects/Mac/work/_scratch/test1',
            sizeBytes: 500 * 1024 ** 2,
            sizeHuman: '500 MiB',
            ageHours: 80,
            reason: 'expired'
          }
        ],
        ephemeralSimulators: [],
        launchservicesGhostsCount: 0,
        manifestPath: null,
        errors: []
      },
      orcaPreview: null,
      extraDirs: [
        {
          kind: 'derived_data',
          name: 'Wayline-abc',
          path: '/Users/kafu/Library/Developer/Xcode/DerivedData/Wayline-abc',
          sizeBytes: 3 * 1024 ** 3,
          pinned: false
        }
      ],
      workspacePathToId: new Map([['/Users/kafu/AI-Workspace/AI/Orca/Worktrees/Wayline/wt1', 'ws-wt1']]),
      workspaceRoot: '/Users/kafu/AI-Workspace'
    })

    assert.equal(res.topOffenders.length, 4)
    assert.equal(res.topOffenders[0].kind, 'stranded_worktree')
    assert.equal(res.topOffenders[0].workspaceId, 'ws-wt1')
    assert.equal(res.topOffenders[1].kind, 'legacy_work')
    assert.equal(res.topOffenders[1].pinned, true)
    assert.equal(res.topOffenders[2].kind, 'derived_data')
    assert.equal(res.topOffenders[3].kind, 'gc_candidate')
    assert.equal(res.strandedWorktreeCount, 1)
  })

  test('isAllowedKeepTarget and isAllowedManifestPath reject escapes and Git roots', () => {
    assert.equal(
      isAllowedKeepTarget('/Users/kafu/AI-Workspace/AI/Projects/Mac/work/_scratch/x', '/Users/kafu/AI-Workspace', '/Users/kafu'),
      true
    )
    assert.equal(
      isAllowedKeepTarget('/Users/kafu/AI-Workspace/AI/Data/BuildCache', '/Users/kafu/AI-Workspace', '/Users/kafu'),
      true
    )
    assert.equal(
      isAllowedKeepTarget('/Users/kafu/Library/Developer/Xcode/DerivedData/App-123', '/Users/kafu/AI-Workspace', '/Users/kafu'),
      true
    )
    // Must refuse Git repo roots, .git internals, bare work/, and outside paths
    assert.equal(
      isAllowedKeepTarget('/Users/kafu/AI-Workspace/AI/Projects/Mac', '/Users/kafu/AI-Workspace', '/Users/kafu'),
      false
    )
    assert.equal(
      isAllowedKeepTarget('/Users/kafu/AI-Workspace/AI/Projects/Mac/work', '/Users/kafu/AI-Workspace', '/Users/kafu'),
      false
    )
    assert.equal(
      isAllowedKeepTarget('/Users/kafu/AI-Workspace/AI/Projects/Mac/work/.git', '/Users/kafu/AI-Workspace', '/Users/kafu'),
      false
    )
    assert.equal(isAllowedKeepTarget('/Users/kafu/.ssh', '/Users/kafu/AI-Workspace', '/Users/kafu'), false)
    assert.equal(
      isAllowedKeepTarget('/Users/kafu/AI-Workspace/AI/../.ssh', '/Users/kafu/AI-Workspace', '/Users/kafu'),
      false
    )

    assert.equal(
      isAllowedManifestPath('/Users/kafu/.Trash/agent-governor-2026-11-25/gc-daily-1/manifest.json', '/Users/kafu'),
      true
    )
    assert.equal(isAllowedManifestPath('/Users/kafu/.Trash/other/manifest.json', '/Users/kafu'), false)
    assert.equal(
      isAllowedManifestPath('/Users/kafu/.Trash/agent-governor-2026-11-25/../secret/manifest.json', '/Users/kafu'),
      false
    )

    // Worktree archiving allowlist & topic sanitizer
    assert.equal(
      isAllowedArchiveWorktree('/Users/kafu/AI-Workspace/AI/Codex/Worktrees/WanFuse/rust-platform', '/Users/kafu/AI-Workspace'),
      true
    )
    assert.equal(
      isAllowedArchiveWorktree('/Users/kafu/AI-Workspace/AI/Orca/Worktrees/Phayen/NEGA/copepod', '/Users/kafu/AI-Workspace'),
      true
    )
    assert.equal(
      isAllowedArchiveWorktree('/Users/kafu/AI-Workspace/AI/Projects/WanFuse', '/Users/kafu/AI-Workspace'),
      false
    )
    assert.equal(
      isAllowedArchiveWorktree('/Users/kafu/AI-Workspace/AI/Codex/Worktrees', '/Users/kafu/AI-Workspace'),
      false
    )
    assert.equal(sanitizeArchiveTopic('codex/wanfuse-rust-platform'), 'wanfuse-rust-platform')
    assert.equal(sanitizeArchiveTopic('Phayen｜Perf promotion'), 'Phayen-Perf-promotion')
    assert.equal(sanitizeArchiveTopic('...///'), 'worktree')
  })
})

describe('alert engine, Bot brief & status bar', () => {
  test('evaluateAlerts prioritizes critical over warning and detects all dimensions', () => {
    const now = 1_800_000_000_000
    const alerts = evaluateAlerts({
      disk: {
        mount: '/',
        totalBytes: 1000 * 1024 ** 3,
        usedBytes: 940 * 1024 ** 3,
        availBytes: 60 * 1024 ** 3,
        capacityPct: 94,
        freePct: 6,
        storageMode: 'internal',
        workspaceRoot: '/Users/kafu/AI-Workspace'
      },
      jobs: [
        { name: 'backup-hot', state: 'fail', time: 'now', lastOk: '', message: 'NAS down', seconds: null },
        { name: 'backup-media', state: 'warn', time: 'now', lastOk: 'ok', message: '1 xattr warn', seconds: 60 }
      ],
      doctor: { checkedAt: now, ok: false, attention: ['hook drift detected'], advice: [], error: null },
      audit: {
        checkedAt: now,
        healthy: false,
        globalRulesInSync: true,
        globalTokens: 700,
        skillsCount: 12,
        skillsTokens: 1700,
        oversizedSkills: [],
        mcpClaude: 1,
        mcpCodex: 3,
        projectsAudited: 19,
        attention: ['[WanFuse] missing_zones'],
        advice: [],
        flaggedProjects: [],
        error: null
      },
      workspaces: [
        {
          id: 'ws-1',
          name: 'Wayline｜测试',
          project: 'Wayline',
          kind: 'worktree',
          path: '/tmp/wt',
          branch: 'codex/x',
          state: 'waiting',
          stateStartedAt: now - 30 * MIN_MS,
          lastActivityAt: now - 30 * MIN_MS,
          unread: true,
          terminalCount: 1,
          bot: null,
          linkedPR: null,
          agentType: 'claude',
          lastMessage: null,
          waitingMinutes: 30,
          memoryBytes: null,
          cpuPct: null
        }
      ],
      ephemeralSimulators: [{ name: 'agent-tmp-job-1-abc', udid: 'UUID-1', state: 'Shutdown' }],
      runningQemu: [],
      recentRuns: [
        {
          at: now - HOUR_MS,
          state: 'ok',
          reclaimedItems: 10,
          reclaimedBytes: 6 * 1024 ** 3,
          protectedSkips: 5,
          ghostApps: 0,
          raw: ''
        }
      ],
      recentBatches: [],
      legacyTotalBytes: 40 * 1024 ** 3,
      strandedTotalBytes: 10 * 1024 ** 3,
      settings: BASE_SETTINGS,
      nowMs: now,
      dismissedFingerprints: new Set(),
      escalatedMap: {}
    })

    const crits = alerts.filter((a) => a.severity === 'critical')
    const warns = alerts.filter((a) => a.severity === 'warning')
    assert.equal(crits.length, 3) // disk critical, backup-hot fail, doctor attention
    assert.ok(warns.length >= 5) // backup-media warn, waiting ws, sim leftover, gc spike, audit attention
    assert.equal(alerts[0].severity, 'critical')

    const brief = buildBotEscalationBrief({
      alerts,
      snapshot: {
        disk: {
          mount: '/',
          totalBytes: 1000 * 1024 ** 3,
          usedBytes: 940 * 1024 ** 3,
          availBytes: 60 * 1024 ** 3,
          capacityPct: 94,
          freePct: 6,
          storageMode: 'internal',
          workspaceRoot: '/Users/kafu/AI-Workspace'
        },
        cleanup: {
          governor: {
            lastRunAt: now - HOUR_MS,
            lastManualGcAt: null,
            nextRunAt: now + 3 * HOUR_MS,
            intervalSec: 14400,
            lastState: 'ok',
            lastMessage: 'ok',
            dryRun: null,
            dryRunAt: null,
            historyRuns: [],
            trendDays: [],
            recentBatches: [],
            totalReclaimed24hBytes: 6 * 1024 ** 3,
            totalReclaimed7dBytes: 8 * 1024 ** 3,
            totalReclaimed24hItems: 10
          },
          orca: { nextRunAt: null, lastRun: null, earlierRuns: [], preview: null, error: null }
        },
        storage: {
          scannedAt: now,
          topOffenders: [],
          legacyTotalBytes: 40 * 1024 ** 3,
          legacyCount: 5,
          strandedWorktreeCount: 2,
          strandedTotalBytes: 10 * 1024 ** 3,
          categoryBreakdown: []
        },
        health: {
          jobs: [{ name: 'backup-hot', state: 'fail', time: 'now', lastOk: '', message: 'NAS down', seconds: null }],
          doctor: { checkedAt: now, ok: false, attention: [], advice: [], error: null },
          audit: {
            checkedAt: now,
            healthy: true,
            globalRulesInSync: true,
            globalTokens: 700,
            skillsCount: 12,
            skillsTokens: 1700,
            oversizedSkills: [],
            mcpClaude: 1,
            mcpCodex: 3,
            projectsAudited: 19,
            attention: [],
            advice: [],
            flaggedProjects: [],
            error: null
          }
        },
        live: { bots: [], totalWorkspaceCount: 0, workspaces: [], memory: null, testDevices: { ephemeralSimulators: [], runningQemu: [], activeLeases: [] } }
      },
      customNote: '顺便看下是不是 Phayen 构建占的'
    })

    assert.ok(brief.subject.includes('紧急告警'))
    assert.ok(brief.body.includes('Phayen'))
    assert.ok(brief.body.includes('backup-hot'))

    const bar = buildStatusBarState(
      {
        generatedAt: now,
        status: 'ready',
        error: null,
        settings: BASE_SETTINGS,
        hygieneConfig: DEFAULT_HYGIENE_CONFIG,
        counts: {
          criticalAlerts: crits.length,
          warnAlerts: warns.length,
          workingAgents: 0,
          waitingAgents: 1,
          blockedAgents: 0,
          staleWaitingAgents: 1,
          reclaimableBytes: 0,
          trashBatches: 0
        },
        disk: null,
        cleanup: {
          governor: {
            lastRunAt: now - HOUR_MS,
            lastManualGcAt: null,
            nextRunAt: now + 3 * HOUR_MS,
            intervalSec: 14400,
            lastState: 'ok',
            lastMessage: null,
            dryRun: null,
            dryRunAt: null,
            historyRuns: [],
            trendDays: [],
            recentBatches: [],
            totalReclaimed24hBytes: 0,
            totalReclaimed7dBytes: 0,
            totalReclaimed24hItems: 0
          },
          orca: { nextRunAt: null, lastRun: null, earlierRuns: [], preview: null, error: null }
        },
        storage: {
          scannedAt: null,
          topOffenders: [],
          legacyTotalBytes: 0,
          legacyCount: 0,
          strandedWorktreeCount: 0,
          strandedTotalBytes: 0,
          categoryBreakdown: []
        },
        live: { bots: [], totalWorkspaceCount: 0, workspaces: [], memory: null, testDevices: { ephemeralSimulators: [], runningQemu: [], activeLeases: [] } },
        health: {
          jobs: [],
          doctor: { checkedAt: null, ok: null, attention: [], advice: [], error: null },
          audit: {
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
        },
        alerts,
        botDispatchHistory: [],
        archiveHistory: [],
        busyActions: [],
        lastActionNotice: null
      },
      now
    )
    assert.equal(bar.tone, 'danger')
    assert.ok(bar.text.includes('3 crit'))
    assert.ok(DAY_MS > 0)
  })

  test('parseGovernorGcArgs, buildGovernorGcArgs and upsertEnvKeys round-trip safely', () => {
    const parsed = parseGovernorGcArgs(
      '--scratch-ttl-days 2 --tests-ttl-days=5 --derived-data-ttl-hours 6 --min-idle-hours 1 --skip-xcode',
      2,
      '/Users/test/.orca/hygiene.env'
    )
    assert.equal(parsed.gcIntervalHours, 2)
    assert.equal(parsed.scratchTtlDays, 2)
    assert.equal(parsed.testsTtlDays, 5)
    assert.equal(parsed.derivedDataTtlHours, 6)
    assert.equal(parsed.minIdleHours, 1)
    assert.equal(parsed.skipXcode, true)
    assert.equal(parsed.skipSimulators, false)

    const dailyArgs = buildGovernorGcArgs(parsed, 'daily')
    assert.ok(dailyArgs.includes('--scratch-ttl-days'))
    assert.ok(dailyArgs.includes('--min-idle-hours'))
    assert.ok(dailyArgs.includes('--skip-xcode'))

    // session-end preserves TTL overrides but omits --min-idle-hours so immediate sweep works
    const endArgs = buildGovernorGcArgs(parsed, 'session-end')
    assert.ok(endArgs.includes('--scratch-ttl-days'))
    assert.equal(endArgs.includes('--min-idle-hours'), false)

    // deep mode keeps deep presets and only forwards explicit skip toggles
    const deepArgs = buildGovernorGcArgs(parsed, 'deep')
    assert.deepEqual(deepArgs, ['--skip-xcode'])

    // Default config emits empty CLI args
    assert.deepEqual(buildGovernorGcArgs(DEFAULT_HYGIENE_CONFIG, 'daily'), [])

    // upsertEnvKeys preserves unrelated comments/keys, updates existing, and deletes null keys
    const envBefore = '# comment\nexport STORAGE_MODE="internal"\nexport AGENT_HYGIENE_GC_INTERVAL_SEC="14400"\n'
    const envAfter = upsertEnvKeys(envBefore, {
      AGENT_HYGIENE_GC_INTERVAL_SEC: '7200',
      AGENT_GOVERNOR_GC_ARGS: '--scratch-ttl-days 2'
    })
    assert.ok(envAfter.includes('# comment'))
    assert.ok(envAfter.includes('export STORAGE_MODE="internal"'))
    assert.ok(envAfter.includes('export AGENT_HYGIENE_GC_INTERVAL_SEC="7200"'))
    assert.ok(envAfter.includes('export AGENT_GOVERNOR_GC_ARGS="--scratch-ttl-days 2"'))

    const envCleared = upsertEnvKeys(envAfter, { AGENT_GOVERNOR_GC_ARGS: null })
    assert.equal(envCleared.includes('AGENT_GOVERNOR_GC_ARGS'), false)
  })

  test('selectBotChat routes away from failed/rate-limited Home Chat and prefers another provider', () => {
    const now = 1_800_000_000_000
    const bot: BotInfo = {
      id: 'bot-ram',
      name: 'Ram',
      agent: 'claude',
      paused: false,
      specialty: '',
      chatCount: 3,
      chats: [
        {
          id: 'chat-home',
          title: 'Home',
          displayTitle: '主聊天',
          agent: 'claude',
          home: true,
          closed: false,
          failed: true,
          failedAt: now - 10_000,
          lastActivityAt: now - 10_000,
          lastAsked: '',
          lastSaid: ''
        },
        {
          id: 'chat-claude-alt',
          title: 'Claude Alt',
          displayTitle: 'Claude Alt',
          agent: 'claude',
          home: false,
          closed: false,
          failed: false,
          failedAt: null,
          lastActivityAt: now - 5_000,
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
          lastActivityAt: now - 30_000,
          lastAsked: '',
          lastSaid: ''
        }
      ]
    }

    // Auto mode detects failed claude Home Chat -> skips other claude chat and picks antigravity chat!
    const autoRoute = selectBotChat({ bot, preference: 'auto', nowMs: now })
    assert.equal(autoRoute.chatArg, 'chat-agy')
    assert.equal(autoRoute.chatTitle, 'Antigravity Chat')
    assert.equal(autoRoute.fellBackFromHome, true)

    // Explicit chat selection honors exact chat ID
    const explicit = selectBotChat({ bot, preference: 'chat-claude-alt', nowMs: now })
    assert.equal(explicit.chatArg, 'chat-claude-alt')
    assert.equal(explicit.fellBackFromHome, false)

    // Explicit new chat
    const newRoute = selectBotChat({ bot, preference: 'new', nowMs: now })
    assert.equal(newRoute.chatArg, 'new')

    // Healthy Home Chat uses Home normally in auto mode
    bot.chats[0].failed = false
    const healthy = selectBotChat({ bot, preference: 'auto', nowMs: now })
    assert.equal(healthy.chatArg, null)
    assert.equal(healthy.fellBackFromHome, false)
  })
})
