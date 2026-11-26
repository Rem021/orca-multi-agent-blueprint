// Workspace Ops Radar — HTML sidebar panel controller (zero external dependencies).
// Talks exclusively to src/worker.ts through `window.orca.plugin.request` and `orca.plugin.on`.

const appEl = document.getElementById('app')

const CATEGORY_COLORS = [
  'var(--ops-info)',
  'var(--ops-purple)',
  'var(--ops-warn)',
  'var(--ops-teal)',
  'var(--ops-ok)',
  'var(--ops-danger)'
]

const CATEGORY_LABELS_ZH = {
  legacy_work: '历史受保护 work/',
  stranded_worktree: 'Worktree 滞留 work/',
  worktree: 'Worktree 体积',
  shared_data: '共享缓存 AI/Data',
  derived_data: 'Xcode DerivedData',
  work_scratch: '临时 _scratch',
  work_tests: '测试 _tests',
  xcode_test_runner: 'XCTest Runner',
  claude_session_env: 'Claude 会话环境',
  codex_tmp: 'Codex 临时文件',
  codex_log: 'Codex 日志',
  orca_chat_scratch: 'Orca 聊天缓存',
  antigravity_scratch: 'Antigravity 缓存',
  antigravity_log: 'Antigravity 日志',
  gc_candidate: '可回收缓存'
}

const CATEGORY_LABELS_EN = {
  legacy_work: 'Legacy work/',
  stranded_worktree: 'Stranded work/',
  worktree: 'Worktree size',
  shared_data: 'AI/Data cache',
  derived_data: 'Xcode DerivedData',
  work_scratch: '_scratch',
  work_tests: '_tests',
  xcode_test_runner: 'XCTest Runner',
  claude_session_env: 'Claude session-env',
  codex_tmp: 'Codex .tmp',
  codex_log: 'Codex logs',
  orca_chat_scratch: 'Orca chat scratch',
  antigravity_scratch: 'Antigravity scratch',
  antigravity_log: 'Antigravity logs',
  gc_candidate: 'GC candidate'
}

const state = {
  snapshot: null,
  pendingSnapshot: null,
  composing: false,
  tab: 'overview',
  storageFilter: 'all',
  storageSearch: '',
  expandedBatchId: null,
  confirmRestorePath: null,
  botNote: '',
  botNewChat: false,
  localError: null
}

function isUserEditing() {
  if (state.composing) return true
  const active = document.activeElement
  if (!active || !active.tagName) return false
  const tag = active.tagName.toUpperCase()
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
}

function flushPendingSnapshot() {
  if (!state.pendingSnapshot || isUserEditing()) return
  state.snapshot = state.pendingSnapshot
  state.pendingSnapshot = null
  render()
}

async function copyTextSafe(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // Fallback below when Permissions-Policy blocks async clipboard in sandboxed frames.
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

const isZh = () => String(window.orca?.locale || 'zh').toLowerCase().startsWith('zh')

function t(zh, en) {
  return isZh() ? zh : en
}

function catLabel(cat) {
  return (isZh() ? CATEGORY_LABELS_ZH : CATEGORY_LABELS_EN)[cat] || cat
}

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let v = bytes
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  const d = i === 0 ? 0 : v >= 100 ? 0 : v >= 10 ? 1 : 2
  return `${v.toFixed(d)} ${units[i]}`
}

function formatCountdown(targetMs) {
  if (!targetMs) return '—'
  const diff = targetMs - Date.now()
  if (diff <= 0) return t('即将执行', 'Due now')
  const mins = Math.ceil(diff / 60_000)
  if (mins < 60) return isZh() ? `${mins} 分钟` : `${mins}m`
  const h = Math.floor(mins / 60)
  const m = mins % 60
  if (h < 24) return m ? `${h}h ${m}m` : `${h}h`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

function formatRelative(ms) {
  if (!ms) return '—'
  const diff = Date.now() - ms
  if (diff < 60_000) return t('刚刚', 'just now')
  const mins = Math.floor(diff / 60_000)
  if (mins < 60) return isZh() ? `${mins} 分钟前` : `${mins}m ago`
  const h = Math.floor(mins / 60)
  if (h < 24) return isZh() ? `${h} 小时前` : `${h}h ago`
  return isZh() ? `${Math.floor(h / 24)} 天前` : `${Math.floor(h / 24)}d ago`
}

function formatClock(ms) {
  if (!ms) return '—'
  const d = new Date(ms)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function shortPath(p) {
  if (!p) return ''
  return p.replace(/^\/Users\/[^/]+/, '~')
}

async function req(name, input, timeoutMs = 90_000) {
  state.localError = null
  try {
    const out = await window.orca.plugin.request(name, input, { timeoutMs })
    if (out && typeof out === 'object' && 'generatedAt' in out) {
      state.snapshot = out
    }
    render()
    return out
  } catch (err) {
    state.localError = err?.message || err?.code || 'request_failed'
    render()
    return null
  }
}

function renderStackedBar(categories) {
  const total = categories.reduce((s, c) => s + (c.bytes || 0), 0)
  if (!total) return ''
  const top = categories.slice(0, 5)
  const restBytes = categories.slice(5).reduce((s, c) => s + (c.bytes || 0), 0)
  const segs = restBytes > 0 ? [...top, { category: 'other', bytes: restBytes, count: 0 }] : top

  const bars = segs
    .map((c, idx) => {
      const pct = Math.max(2, (c.bytes / total) * 100)
      const color = CATEGORY_COLORS[idx % CATEGORY_COLORS.length]
      return `<div class="bar-fill" style="width:${pct.toFixed(1)}%;background:${color}" title="${esc(catLabel(c.category))}: ${esc(formatBytes(c.bytes))}"></div>`
    })
    .join('')

  const legend = segs
    .map((c, idx) => {
      const color = CATEGORY_COLORS[idx % CATEGORY_COLORS.length]
      return `<span class="legend-item"><span class="legend-swatch" style="background:${color}"></span><span>${esc(catLabel(c.category))}</span><strong class="mono">${esc(formatBytes(c.bytes))}</strong></span>`
    })
    .join('')

  return `<div class="bar-track">${bars}</div><div class="legend-row">${legend}</div>`
}

function renderTrendChart(trendDays) {
  if (!trendDays || trendDays.length === 0) {
    return `<div class="empty-box">${t('暂无 7 天清理趋势记录', 'No 7-day cleanup trend yet')}</div>`
  }
  const maxBytes = Math.max(1, ...trendDays.map((d) => d.reclaimedBytes))
  const w = 320
  const h = 92
  const barW = 28
  const gap = (w - barW * trendDays.length) / (trendDays.length + 1)
  const plotH = 58
  const baseY = 72

  const bars = trendDays
    .map((d, i) => {
      const x = gap + i * (barW + gap)
      const bh = d.reclaimedBytes > 0 ? Math.max(5, Math.round((d.reclaimedBytes / maxBytes) * plotH)) : 2
      const y = baseY - bh
      const label = d.date.slice(5)
      const fill = d.reclaimedBytes > 0 ? 'var(--ops-info)' : 'var(--orca-border)'
      const topTxt = d.reclaimedBytes > 0 ? formatBytes(d.reclaimedBytes).replace(' ', '') : ''
      return `
        <g>
          <title>${esc(d.date)}: ${esc(formatBytes(d.reclaimedBytes))} (${d.reclaimedItems} items, ${d.runs} runs)</title>
          ${topTxt ? `<text x="${x + barW / 2}" y="${Math.max(10, y - 4)}" text-anchor="middle" font-size="8.5" fill="var(--orca-muted-foreground)">${esc(topTxt)}</text>` : ''}
          <rect x="${x}" y="${y}" width="${barW}" height="${bh}" rx="3" fill="${fill}" opacity="${d.reclaimedBytes > 0 ? '0.88' : '0.45'}"></rect>
          <text x="${x + barW / 2}" y="${h - 4}" text-anchor="middle" font-size="9" fill="var(--orca-muted-foreground)">${esc(label)}</text>
        </g>`
    })
    .join('')

  return `<div class="chart-wrap"><svg class="trend-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${bars}</svg></div>`
}

function renderAlertBanner(snap) {
  const active = (snap.alerts || []).filter((a) => !a.dismissed && a.severity !== 'info')
  if (active.length === 0) return ''
  const hasCrit = active.some((a) => a.severity === 'critical')
  const botName = snap.settings?.botName || 'Ram'
  const busyBot = snap.busyActions?.includes('bot-send')

  const rows = active
    .slice(0, 5)
    .map((a) => {
      const pillCls = a.severity === 'critical' ? 'danger' : 'warn'
      const pillTxt = a.severity === 'critical' ? t('紧急', 'CRIT') : t('警告', 'WARN')
      return `
        <div class="alert-row">
          <div class="alert-body">
            <div class="alert-title"><span class="pill ${pillCls}">${pillTxt}</span> ${esc(a.title)}</div>
            <div class="xs muted">${esc(a.detail)}</div>
            ${a.escalatedAt ? `<div class="xs" style="color:var(--ops-info)">${t(`已交给 ${botName}：`, `Sent to ${botName}: `)}${esc(formatRelative(a.escalatedAt))}</div>` : ''}
          </div>
          <div class="alert-actions">
            ${a.workspaceId ? `<button class="xs-btn" data-action="open-ws" data-ws="${esc(a.workspaceId)}">${t('打开', 'Open')}</button>` : ''}
            <button class="xs-btn" data-action="escalate-one" data-alert="${esc(a.id)}" ${busyBot ? 'disabled' : ''}>${t(`问 ${botName}`, `Ask ${botName}`)}</button>
            <button class="xs-btn ghost" data-action="dismiss-alert" data-fp="${esc(a.fingerprint)}" title="${t('暂忽略 24h', 'Dismiss 24h')}">✕</button>
          </div>
        </div>`
    })
    .join('')

  return `
    <div class="alert-banner ${hasCrit ? 'critical' : ''}">
      <div class="alert-banner-head">
        <strong style="font-size:12px">${hasCrit ? '🚨' : '⚠️'} ${t(`检测到 ${active.length} 项异常/提醒`, `${active.length} active alert(s)`)}</strong>
        <button class="xs-btn primary" data-action="escalate-all" ${busyBot ? 'disabled' : ''}>
          ${busyBot ? t('正在发送…', 'Sending…') : t(`让 ${botName} 结合分析并反馈`, `Ask ${botName} to review`)}
        </button>
      </div>
      <div class="alert-list">${rows}</div>
    </div>`
}

function renderOverview(snap) {
  const gov = snap.cleanup.governor
  const orcaC = snap.cleanup.orca
  const disk = snap.disk
  const busy = (snap.busyActions || []).length > 0

  // Compute progress percentage inside current GC interval
  let govPct = 0
  if (gov.lastRunAt && gov.nextRunAt && gov.nextRunAt > gov.lastRunAt) {
    govPct = Math.max(0, Math.min(100, ((Date.now() - gov.lastRunAt) / (gov.nextRunAt - gov.lastRunAt)) * 100))
  }
  let orcaPct = 0
  if (orcaC.lastRun?.startedAt && orcaC.nextRunAt && orcaC.nextRunAt > orcaC.lastRun.startedAt) {
    orcaPct = Math.max(0, Math.min(100, ((Date.now() - orcaC.lastRun.startedAt) / (orcaC.nextRunAt - orcaC.lastRun.startedAt)) * 100))
  }

  const diskTone = !disk ? 'ok' : disk.freePct <= snap.settings.diskWarnFreePercent / 2 ? 'danger' : disk.freePct <= snap.settings.diskWarnFreePercent ? 'warn' : 'ok'
  const dry = gov.dryRun
  const topPreview = (dry?.candidates || []).filter((c) => c.sizeBytes > 0).sort((a, b) => b.sizeBytes - a.sizeBytes).slice(0, 5)

  return `
    <div class="grid-2">
      <div class="card">
        <div class="card-head">
          <span class="card-title">🧹 ${t('缓存/临时目录 GC', 'Agent Governor GC')}</span>
          <span class="pill ${gov.lastState === 'fail' ? 'danger' : gov.lastState === 'warn' ? 'warn' : 'ok'}">${esc(gov.lastState || 'ready')}</span>
        </div>
        <div class="metric-big">${esc(formatCountdown(gov.nextRunAt))}</div>
        <div class="bar-track"><div class="bar-fill ok" style="width:${govPct.toFixed(0)}%"></div></div>
        <div class="xs muted">
          ${t('周期', 'Every')} ${(gov.intervalSec / 3600).toFixed(1).replace(/\.0$/, '')}h · ${t('上次定时', 'Scheduled')} ${esc(formatRelative(gov.lastRunAt))}${gov.lastManualGcAt ? ` · ${t('手动', 'Manual')} ${esc(formatRelative(gov.lastManualGcAt))}` : ''}
        </div>
        ${gov.lastMessage ? `<div class="xs muted truncate" title="${esc(gov.lastMessage)}">${esc(gov.lastMessage)}</div>` : ''}
      </div>

      <div class="card">
        <div class="card-head">
          <span class="card-title">🌿 ${t('Orca 工作树清理', 'Orca Worktree Cleanup')}</span>
          <span class="pill info">${t('每小时', 'Hourly')}</span>
        </div>
        <div class="metric-big">${esc(formatCountdown(orcaC.nextRunAt))}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${orcaPct.toFixed(0)}%"></div></div>
        <div class="xs muted">
          ${orcaC.lastRun
            ? t(`上次保留 ${orcaC.lastRun.kept} · 移除 ${orcaC.lastRun.removed} · 失败 ${orcaC.lastRun.failed}`, `Kept ${orcaC.lastRun.kept} · Removed ${orcaC.lastRun.removed} · Failed ${orcaC.lastRun.failed}`)
            : esc(orcaC.error || t('暂无记录', 'No run yet'))}
        </div>
        <div class="xs muted">${orcaC.preview ? t(`预览待清 ${orcaC.preview.wouldRemoveCount} 个`, `Preview: ${orcaC.preview.wouldRemoveCount} removable`) : ''}</div>
      </div>
    </div>

    ${disk ? `
      <div class="card">
        <div class="card-head">
          <span class="card-title">💾 ${t('工作区磁盘水位', 'Workspace Disk')}</span>
          <span class="pill ${diskTone}">${t(`剩余 ${disk.freePct.toFixed(1)}%`, `${disk.freePct.toFixed(1)}% free`)}</span>
        </div>
        <div class="bar-track"><div class="bar-fill ${diskTone}" style="width:${disk.capacityPct}%"></div></div>
        <div class="item-meta">
          <span>${t('已用', 'Used')} <strong class="mono">${esc(formatBytes(disk.usedBytes))}</strong> / ${esc(formatBytes(disk.totalBytes))}</span>
          <span>${t('可用', 'Avail')} <strong class="mono">${esc(formatBytes(disk.availBytes))}</strong></span>
        </div>
        <div class="xs muted truncate">${esc(shortPath(disk.workspaceRoot))} (${esc(disk.storageMode)})</div>
      </div>` : ''}

    <div class="grid-4">
      <div class="kpi-box">
        <div class="kpi-val mono">${esc(formatBytes(gov.totalReclaimed24hBytes))}</div>
        <div class="kpi-lbl">${t(`近 24h 回收 (${gov.totalReclaimed24hItems}项)`, `24h Reclaimed (${gov.totalReclaimed24hItems})`)}</div>
      </div>
      <div class="kpi-box">
        <div class="kpi-val mono">${esc(formatBytes(gov.totalReclaimed7dBytes))}</div>
        <div class="kpi-lbl">${t('近 7 天累计回收', '7d Reclaimed')}</div>
      </div>
      <div class="kpi-box">
        <div class="kpi-val mono">${esc(formatBytes(dry?.reclaimableBytes || 0))}</div>
        <div class="kpi-lbl">${t(`当前可清 (${dry?.totalCandidates || 0}项)`, `Reclaimable (${dry?.totalCandidates || 0})`)}</div>
      </div>
      <div class="kpi-box">
        <div class="kpi-val mono">${esc(formatBytes((snap.storage.legacyTotalBytes || 0) + (snap.storage.strandedTotalBytes || 0)))}</div>
        <div class="kpi-lbl">${t('受保护/滞留 work/', 'Legacy & Stranded')}</div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">📊 ${t('近 7 天清理回收趋势', '7-Day Cleanup Trend')}</span>
        <span class="xs muted">${t(`${gov.historyRuns.length} 轮记录`, `${gov.historyRuns.length} runs`)}</span>
      </div>
      ${renderTrendChart(gov.trendDays)}
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">⚡ ${t('快捷清理操作（5 道安全门 · 移入废纸篓可撤销）', 'Quick Cleanup Actions (Safe Trash + Undo)')}</span>
      </div>
      <div class="btn-row">
        <button data-action="gc" data-mode="daily" data-apply="0" ${busy ? 'disabled' : ''}>${t('预览 Daily', 'Preview Daily')}</button>
        <button class="primary" data-action="gc" data-mode="daily" data-apply="1" ${busy ? 'disabled' : ''}>${t('立即 Daily GC', 'Run Daily GC')}</button>
        <button data-action="gc" data-mode="session-end" data-apply="1" ${busy ? 'disabled' : ''}>${t('测试收尾 (Session-end)', 'Session-end GC')}</button>
        <button data-action="gc" data-mode="deep" data-apply="0" ${busy ? 'disabled' : ''}>${t('预览 Deep', 'Preview Deep')}</button>
        <button class="warn" data-action="gc" data-mode="deep" data-apply="1" ${busy ? 'disabled' : ''}>${t('深度 Deep GC', 'Run Deep GC')}</button>
      </div>
      ${dry ? `
        <div class="xs muted">
          ${t(`最近扫描 (${dry.mode})：${dry.totalCandidates} 项可清 (${dry.reclaimableHuman}) · ${dry.protectedSkipsCount} 项被安全门保护 · ${dry.launchservicesGhostsCount} 个幽灵图标`, `Last scan (${dry.mode}): ${dry.totalCandidates} candidates (${dry.reclaimableHuman}) · ${dry.protectedSkipsCount} protected · ${dry.launchservicesGhostsCount} ghost apps`)}
        </div>
        ${dry.categories.length ? renderStackedBar(dry.categories) : ''}
        ${topPreview.length ? `
          <div class="row-list">
            ${topPreview.map((c) => `
              <div class="item-row">
                <div class="item-top">
                  <span class="item-name">${esc(c.path.split('/').pop() || c.path)}</span>
                  <strong class="mono">${esc(formatBytes(c.sizeBytes))}</strong>
                </div>
                <div class="item-meta"><span>${esc(catLabel(c.category))} · ${esc(c.reason)}</span></div>
                <div class="path-line mono">${esc(shortPath(c.path))}</div>
              </div>`).join('')}
          </div>` : ''}
      ` : `<div class="xs muted">${t('点击预览查看当前可回收文件明细', 'Run a preview to inspect reclaimable items')}</div>`}
    </div>`
}

function renderCleanupTab(snap) {
  const batches = snap.cleanup.governor.recentBatches || []
  const historyRuns = snap.cleanup.governor.historyRuns || []
  const orcaC = snap.cleanup.orca
  const busy = (snap.busyActions || []).length > 0

  const batchHtml = batches.length === 0
    ? `<div class="empty-box">${t('废纸篓中暂无 agent-governor 清理批次（已清空废纸篓后会在此保留此后捕获的批次摘要，下方可看完整日志流水）', 'No Trash batches captured yet; full hygiene log runs are listed below.')}</div>`
    : batches.map((b) => {
        const expanded = state.expandedBatchId === b.id || (state.expandedBatchId === null && batches[0]?.id === b.id)
        const confirming = state.confirmRestorePath === b.manifestPath
        const maxItemSize = b.largestItem?.sizeBytes || 1
        return `
          <div class="batch-card">
            <div class="batch-summary" data-action="toggle-batch" data-batch="${esc(b.id)}">
              <div class="item-top">
                <div class="item-main">
                  <span>${expanded ? '▾' : '▸'}</span>
                  <span class="item-name">${esc(formatClock(b.createdAt))}</span>
                  <span class="pill">${esc(b.mode)}</span>
                  <span class="pill info">${b.totalItems} ${t('项', 'items')}</span>
                  ${b.restoredAt
                    ? `<span class="pill">${t('已还原', 'Restored')}</span>`
                    : b.inTrash
                      ? `<span class="pill ok">${t('可撤销', 'In Trash')}</span>`
                      : `<span class="pill">${t('已清空', 'Emptied')}</span>`}
                </div>
                <strong class="mono">${esc(formatBytes(b.totalBytes))}</strong>
              </div>
              ${b.largestItem ? `
                <div class="xs muted truncate">
                  ${t('最大项', 'Largest')}: <strong style="color:var(--orca-foreground)">${esc(b.largestItem.originalPath.split('/').pop())}</strong>
                  (${esc(formatBytes(b.largestItem.sizeBytes))}) · ${esc(b.largestItem.project || '')}
                </div>` : ''}
              ${b.categories.length ? renderStackedBar(b.categories) : ''}
            </div>
            ${expanded ? `
              <div class="batch-body">
                <div class="btn-row" style="justify-content:space-between">
                  <span class="xs muted mono truncate">${esc(shortPath(b.manifestPath))}</span>
                  ${b.restoredAt
                    ? `<span class="xs muted">${t('该批次已于 ', 'Restored ')}${esc(formatClock(b.restoredAt))}${t(' 还原', '')}</span>`
                    : b.inTrash
                      ? `
                    <div class="btn-row">
                      <button class="xs-btn" data-action="restore-preview" data-manifest="${esc(b.manifestPath)}" ${busy ? 'disabled' : ''}>${t('预览还原', 'Preview Restore')}</button>
                      ${confirming
                        ? `<button class="xs-btn primary" data-action="restore-confirm" data-manifest="${esc(b.manifestPath)}" ${busy ? 'disabled' : ''}>${t('确认还原全部', 'Confirm Restore')}</button>
                           <button class="xs-btn ghost" data-action="restore-cancel">${t('取消', 'Cancel')}</button>`
                        : `<button class="xs-btn warn" data-action="restore-arm" data-manifest="${esc(b.manifestPath)}" ${busy ? 'disabled' : ''}>${t('一键撤销还原', 'Undo / Restore')}</button>`}
                    </div>`
                      : `<span class="xs muted">${t('废纸篓已清空，仅保留记录摘要', 'Trash emptied; archived summary only')}</span>`}
                </div>
                <div class="row-list">
                  ${b.items.map((it) => {
                    const pct = Math.max(2, Math.round((it.sizeBytes / maxItemSize) * 100))
                    return `
                      <div class="item-row">
                        <div class="item-top">
                          <div class="item-main">
                            <span class="pill">${esc(catLabel(it.category))}</span>
                            <span class="item-name">${esc(it.originalPath.split('/').pop() || it.originalPath)}</span>
                          </div>
                          <strong class="mono">${esc(formatBytes(it.sizeBytes))}</strong>
                        </div>
                        <div class="mini-bar"><div style="width:${pct}%"></div></div>
                        <div class="item-meta">
                          <span>${esc(it.project || '')} · ${esc(it.reason)}</span>
                        </div>
                        <div class="path-line mono">${esc(shortPath(it.originalPath))}</div>
                      </div>`
                  }).join('')}
                </div>
              </div>` : ''}
          </div>`
      }).join('')

  const orcaRuns = [orcaC.lastRun, ...(orcaC.earlierRuns || [])].filter(Boolean).slice(0, 8)
  const maxRunBytes = Math.max(1, ...historyRuns.map((r) => r.reclaimedBytes))

  return `
    <div class="card">
      <div class="card-head">
        <span class="card-title">🗑️ ${t('清理明细批次（含最大项与一键撤销）', 'Cleanup Batches (Details & Undo)')}</span>
        <button class="xs-btn" data-action="refresh" data-scope="fast" ${busy ? 'disabled' : ''}>${t('刷新', 'Refresh')}</button>
      </div>
      <div class="row-list">${batchHtml}</div>
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">📜 ${t(`定时清理执行流水 (${historyRuns.length} 轮)`, `Scheduled GC Runs (${historyRuns.length})`)}</span>
        <span class="xs muted">agent-hygiene.log</span>
      </div>
      ${historyRuns.length === 0
        ? `<div class="empty-box">${t('暂无定时清理日志', 'No hygiene log runs yet')}</div>`
        : `<div class="row-list">
            ${historyRuns.slice(0, 15).map((r) => {
              const pct = r.reclaimedBytes > 0 ? Math.max(3, Math.round((r.reclaimedBytes / maxRunBytes) * 100)) : 0
              return `
                <div class="item-row">
                  <div class="item-top">
                    <div class="item-main">
                      <span class="pill ${r.state === 'fail' ? 'danger' : r.state === 'warn' ? 'warn' : 'ok'}">${esc(r.state)}</span>
                      <span class="item-name">${esc(formatClock(r.at))}</span>
                      <span class="xs muted">${r.reclaimedItems} ${t('项', 'items')}</span>
                    </div>
                    <strong class="mono">${esc(formatBytes(r.reclaimedBytes))}</strong>
                  </div>
                  ${pct > 0 ? `<div class="mini-bar"><div style="width:${pct}%"></div></div>` : ''}
                  <div class="item-meta">
                    <span>${r.state === 'fail' && r.reclaimedItems === 0 ? esc(r.raw) : `${t(`安全门保护跳过 ${r.protectedSkips} 项`, `${r.protectedSkips} protected skipped`)}${r.ghostApps ? ` · ${t(`注销 ${r.ghostApps} 个幽灵 App`, `${r.ghostApps} ghost apps`)}` : ''}`}</span>
                    <span>${esc(formatRelative(r.at))}</span>
                  </div>
                </div>`
            }).join('')}
          </div>`}
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">🌿 ${t('Orca 工作树自动清理记录', 'Orca Worktree Cleanup Runs')}</span>
        <div class="btn-row">
          <button class="xs-btn" data-action="orca-cleanup" data-apply="0" ${busy ? 'disabled' : ''}>${t('预览规则', 'Preview')}</button>
          <button class="xs-btn warn" data-action="orca-cleanup" data-apply="1" ${busy ? 'disabled' : ''}>${t('立即执行', 'Run Now')}</button>
        </div>
      </div>
      ${orcaRuns.length === 0
        ? `<div class="empty-box">${esc(orcaC.error || t('暂无 Orca 清理记录', 'No Orca cleanup history'))}</div>`
        : `<div class="row-list">
            ${orcaRuns.map((r) => `
              <div class="item-row">
                <div class="item-top">
                  <span class="item-name">${esc(formatClock(r.startedAt))} · <span class="pill">${esc(r.trigger)}</span></span>
                  <span class="xs mono">${t(`移除 ${r.removed} · 保留 ${r.kept}`, `rm ${r.removed} · kept ${r.kept}`)}</span>
                </div>
                ${r.freedBytes > 0 ? `<div class="xs" style="color:var(--ops-ok)">${t('释放', 'Freed')} ${esc(formatBytes(r.freedBytes))}</div>` : ''}
              </div>`).join('')}
          </div>`}
    </div>`
}

function renderStorageTab(snap) {
  const st = snap.storage
  const busy = (snap.busyActions || []).length > 0
  const all = st.topOffenders || []
  const q = state.storageSearch.trim().toLowerCase()
  const filtered = all.filter((item) => {
    if (state.storageFilter !== 'all' && item.kind !== state.storageFilter) return false
    if (q && !`${item.name} ${item.project} ${item.path} ${item.reason}`.toLowerCase().includes(q)) return false
    return true
  })
  const maxBytes = Math.max(1, ...filtered.map((x) => x.sizeBytes))

  const filters = [
    ['all', t(`全部 (${all.length})`, `All (${all.length})`)],
    ['legacy_work', t('历史 work/', 'Legacy work/')],
    ['stranded_worktree', t('滞留 work/', 'Stranded work/')],
    ['worktree', t('Worktree', 'Worktree')],
    ['gc_candidate', t('可清理', 'Reclaimable')],
    ['shared_data', 'AI/Data'],
    ['derived_data', 'DerivedData']
  ]

  const scanningStorage = (snap.busyActions || []).includes('refresh:storage') || (snap.busyActions || []).includes('refresh:all')

  return `
    ${scanningStorage ? `<div class="notice"><span>⏳ ${t('正在后台扫描项目 work/、AI/Data 与 DerivedData 体积（约 10–20 秒）…', 'Scanning work/, AI/Data & DerivedData in background (10–20s)…')}</span></div>` : ''}
    <div class="grid-2">
      <div class="kpi-box">
        <div class="kpi-val mono">${esc(formatBytes(st.legacyTotalBytes))}</div>
        <div class="kpi-lbl">${t(`受保护历史 work/* (${st.legacyCount} 项)`, `Grandfathered work/* (${st.legacyCount})`)}</div>
      </div>
      <div class="kpi-box">
        <div class="kpi-val mono">${esc(formatBytes(st.strandedTotalBytes))}</div>
        <div class="kpi-lbl">${t(`Worktree 滞留 work/ (${st.strandedWorktreeCount} 处)`, `Stranded work/ (${st.strandedWorktreeCount})`)}</div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">📦 ${t('空间分类占比', 'Storage Breakdown')}</span>
        <button class="xs-btn" data-action="refresh" data-scope="storage" ${busy ? 'disabled' : ''}>${t('重新扫描', 'Rescan')}</button>
      </div>
      ${st.categoryBreakdown.length ? renderStackedBar(st.categoryBreakdown) : `<div class="xs muted">${t('正在扫描空间占用…', 'Scanning storage…')}</div>`}
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">🏆 ${t('最大文件/目录排行（谁占得最大）', 'Top Storage Offenders')}</span>
        <span class="xs muted">${st.scannedAt ? formatRelative(st.scannedAt) : t('扫描中…', 'Scanning…')}</span>
      </div>
      <input type="text" id="storage-search" value="${esc(state.storageSearch)}" placeholder="${t('按项目、目录名或路径筛选…', 'Filter by project, folder or path…')}" />
      <div class="filter-bar">
        ${filters.map(([k, lbl]) => `<button class="chip ${state.storageFilter === k ? 'active' : ''}" data-action="storage-filter" data-filter="${k}">${esc(lbl)}</button>`).join('')}
      </div>
      ${filtered.length === 0
        ? `<div class="empty-box">${t('当前筛选条件下没有记录', 'No matching storage items')}</div>`
        : `<div class="row-list">
            ${filtered.slice(0, 40).map((it, idx) => {
              const pct = Math.max(2, Math.round((it.sizeBytes / maxBytes) * 100))
              return `
                <div class="item-row">
                  <div class="item-top">
                    <div class="item-main">
                      <span class="xs mono muted">#${idx + 1}</span>
                      <span class="item-name" title="${esc(it.name)}">${esc(it.name)}</span>
                      ${it.pinned ? `<span class="pill ok">🔒 .keep</span>` : ''}
                    </div>
                    <strong class="mono">${esc(formatBytes(it.sizeBytes))}</strong>
                  </div>
                  <div class="mini-bar"><div style="width:${pct}%;background:${it.kind === 'gc_candidate' ? 'var(--ops-warn)' : 'var(--ops-purple)'}"></div></div>
                  <div class="item-meta">
                    <span>${esc(it.project)} · ${esc(catLabel(it.kind))}${it.files !== null ? ` · ${it.files.toLocaleString()} ${t('文件', 'files')}` : ''}${it.ageDays !== null ? ` · ${it.ageDays}d` : ''}</span>
                    <div class="btn-row">
                      <button class="xs-btn ghost" data-action="copy-path" data-path="${esc(it.path)}" title="${t('复制完整路径', 'Copy full path')}">📋</button>
                      ${it.workspaceId ? `<button class="xs-btn" data-action="open-ws" data-ws="${esc(it.workspaceId)}">${t('打开工作区', 'Open')}</button>` : ''}
                      ${it.canToggleKeep ? `<button class="xs-btn ${it.pinned ? '' : 'ok'}" data-action="toggle-keep" data-path="${esc(it.path)}" data-keep="${it.pinned ? '0' : '1'}" ${busy ? 'disabled' : ''}>${it.pinned ? t('取消 .keep', 'Unpin') : t('加 .keep 保护', 'Pin .keep')}</button>` : ''}
                    </div>
                  </div>
                  <div class="path-line mono">${esc(shortPath(it.path))}</div>
                </div>`
            }).join('')}
          </div>`}
    </div>`
}

function renderLiveTab(snap) {
  const { bots, workspaces, memory, testDevices } = snap.live
  const busy = (snap.busyActions || []).length > 0
  const botName = snap.settings?.botName || 'Ram'
  const botMode = snap.settings?.botAlertMode || 'critical'

  const stateBadge = (ws) => {
    if (ws.state === 'waiting') {
      const stale = (ws.waitingMinutes ?? 0) >= snap.settings.waitingAlertMinutes
      return `<span class="pill ${stale ? 'danger' : 'warn'}">${t(`等你确认 ${ws.waitingMinutes ?? 0}m`, `Waiting ${ws.waitingMinutes ?? 0}m`)}</span>`
    }
    if (ws.state === 'blocked') return `<span class="pill danger">${t('阻塞', 'Blocked')}</span>`
    if (ws.state === 'working') return `<span class="pill info">${t('运行中', 'Working')}</span>`
    if (ws.state === 'done') return `<span class="pill ok">${t('已完成', 'Done')}</span>`
    if (ws.state === 'active') return `<span class="pill ok">${t('活跃', 'Active')}</span>`
    return `<span class="pill">${t('空闲', 'Idle')}</span>`
  }

  return `
    <div class="card">
      <div class="card-head">
        <span class="card-title">🤖 ${t('Orca Bots 协同指挥台', 'Orca Bots Console')}</span>
        <select id="bot-mode-select" style="width:auto;padding:2px 6px;font-size:11px">
          <option value="critical" ${botMode === 'critical' ? 'selected' : ''}>${t('紧急告警自动交 Bot', 'Auto: Critical')}</option>
          <option value="all" ${botMode === 'all' ? 'selected' : ''}>${t('全部告警自动交 Bot', 'Auto: All')}</option>
          <option value="manual" ${botMode === 'manual' ? 'selected' : ''}>${t('仅手动交给 Bot', 'Manual only')}</option>
        </select>
      </div>
      ${bots.length === 0
        ? `<div class="empty-box">${t('未检测到已配置的 Orca Bot', 'No Orca Bots found')}</div>`
        : bots.map((b) => `
          <div class="item-row">
            <div class="item-top">
              <div class="item-main">
                <span class="item-name">${esc(b.name)}</span>
                <span class="pill info">${esc(b.agent)}</span>
                <span class="pill ${b.paused ? 'warn' : 'ok'}">${b.paused ? t('已暂停', 'Paused') : t('在线', 'Online')}</span>
              </div>
              <span class="xs muted">${b.chatCount} ${t('会话', 'chats')}</span>
            </div>
            ${b.specialty ? `<div class="xs muted">${esc(b.specialty)}</div>` : ''}
            ${b.chats.slice(0, 2).map((c) => `
              <div style="margin-top:4px;padding:5px 7px;border-radius:6px;background:var(--orca-muted)">
                <div class="item-top">
                  <strong class="xs">${c.home ? '🏠 ' : '💬 '}${esc(c.title)}</strong>
                  <span class="xs muted">${esc(formatRelative(c.lastActivityAt))}</span>
                </div>
                ${c.lastSaid ? `<div class="xs muted" style="margin-top:2px">${esc(c.lastSaid)}</div>` : ''}
              </div>`).join('')}
          </div>`).join('')}

      <div style="display:flex;flex-direction:column;gap:6px;margin-top:2px">
        <textarea id="bot-note-input" placeholder="${t(`给 ${botName} 补充一句话（可选），让它结合当前工作区、清理和备份情况给你反馈…`, `Optional note for ${botName}…`)}">${esc(state.botNote)}</textarea>
        <div class="btn-row" style="justify-content:space-between">
          <label class="xs muted" style="display:inline-flex;align-items:center;gap:5px;cursor:pointer">
            <input type="checkbox" id="bot-new-chat" ${state.botNewChat ? 'checked' : ''} style="width:auto" />
            ${t('新开独立话题聊天', 'Start new chat thread')}
          </label>
          <div class="btn-row">
            <button class="xs-btn" data-action="escalate-status" ${busy ? 'disabled' : ''}>${t('仅同步状态', 'Send Status')}</button>
            <button class="xs-btn primary" data-action="escalate-custom" ${busy ? 'disabled' : ''}>${t(`让 ${botName} 分析反馈`, `Handoff to ${botName}`)}</button>
          </div>
        </div>
      </div>

      ${(snap.botDispatchHistory || []).length ? `
        <div class="xs muted" style="margin-top:2px">${t('最近派单记录', 'Recent Bot Dispatches')}</div>
        <div class="row-list">
          ${snap.botDispatchHistory.slice(0, 4).map((d) => `
            <div class="item-meta">
              <span class="truncate" title="${esc(d.subject)}">${d.ok ? '✅' : '❌'} ${d.auto ? '[自动] ' : ''}${esc(d.subject)}</span>
              <span class="mono">${esc(formatRelative(d.at))}</span>
            </div>`).join('')}
        </div>` : ''}
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">⚡ ${t(`全工作区 Agent 实时态势 (${snap.live.totalWorkspaceCount ?? workspaces.length})`, `Live Workspaces (${snap.live.totalWorkspaceCount ?? workspaces.length})`)}</span>
        <span class="xs muted">${t(`运行 ${snap.counts.workingAgents} · 等你 ${snap.counts.waitingAgents} · 阻塞 ${snap.counts.blockedAgents}`, `Work ${snap.counts.workingAgents} · Wait ${snap.counts.waitingAgents} · Block ${snap.counts.blockedAgents}`)}</span>
      </div>
      ${workspaces.length === 0
        ? `<div class="empty-box">${t('暂无工作区数据', 'No workspaces found')}</div>`
        : `<div class="row-list">
            ${workspaces.slice(0, 30).map((ws) => `
              <div class="item-row clickable" data-action="open-ws" data-ws="${esc(ws.id)}">
                <div class="item-top">
                  <div class="item-main">
                    ${stateBadge(ws)}
                    <span class="item-name" title="${esc(ws.name)}">${esc(ws.name)}</span>
                  </div>
                  ${ws.memoryBytes ? `<span class="xs mono muted">${esc(formatBytes(ws.memoryBytes))}</span>` : ''}
                </div>
                <div class="item-meta">
                  <span>${esc(ws.project || ws.kind)}${ws.branch ? ` · ${esc(ws.branch)}` : ''}${ws.agentType ? ` · ${esc(ws.agentType)}` : ''}${ws.linkedPR ? ` · PR ${esc(ws.linkedPR)}` : ''}</span>
                  <span>${esc(formatRelative(ws.lastActivityAt))}</span>
                </div>
                ${ws.lastMessage ? `<div class="xs muted truncate">${esc(ws.lastMessage)}</div>` : ''}
              </div>`).join('')}
          </div>`}
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">🧠 ${t('内存 / CPU 实时进程诊断', 'Memory & CPU Diagnostics')}</span>
        <button class="xs-btn" data-action="refresh" data-scope="memory" ${busy ? 'disabled' : ''}>${t('拉取进程快照', 'Sample Memory')}</button>
      </div>
      ${memory ? `
        <div class="grid-2">
          <div class="kpi-box">
            <div class="kpi-val mono">${esc(formatBytes(memory.totalMemoryBytes))}</div>
            <div class="kpi-lbl">${t(`总内存 · CPU ${memory.totalCpuPct.toFixed(1)}%`, `Total RAM · CPU ${memory.totalCpuPct.toFixed(1)}%`)}</div>
          </div>
          <div class="kpi-box">
            <div class="kpi-val mono">${esc(formatBytes(memory.appMemoryBytes))}</div>
            <div class="kpi-lbl">${t(`Orca 本体 · CPU ${memory.appCpuPct.toFixed(1)}%`, `Orca App · CPU ${memory.appCpuPct.toFixed(1)}%`)}</div>
          </div>
        </div>
        <div class="row-list">
          ${memory.worktrees.slice(0, 10).map((w) => {
            const maxMem = Math.max(1, memory.worktrees[0]?.memoryBytes || 1)
            const pct = Math.max(3, Math.round((w.memoryBytes / maxMem) * 100))
            const openId = w.workspaceId || w.worktreeId
            return `
              <div class="item-row clickable" data-action="open-ws" data-ws="${esc(openId)}">
                <div class="item-top">
                  <span class="item-name">${esc(w.worktreeName || w.repoName)}</span>
                  <span class="mono xs"><strong>${esc(formatBytes(w.memoryBytes))}</strong> · ${w.cpuPct.toFixed(1)}% CPU</span>
                </div>
                <div class="mini-bar"><div style="width:${pct}%;background:var(--ops-teal)"></div></div>
              </div>`
          }).join('')}
        </div>` : `<div class="xs muted">${t('点击「拉取进程快照」按需采样（避免后台频繁扫描进程）', 'Click "Sample Memory" for an on-demand process snapshot.')}</div>`}
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">📱 ${t('临时测试设备（模拟器 / QEMU）', 'Disposable Test Devices')}</span>
      </div>
      <div class="grid-4">
        <div class="kpi-box">
          <div class="kpi-val mono">${testDevices.activeLeases.length}</div>
          <div class="kpi-lbl">${t('活跃模拟器租约', 'Active Leases')}</div>
        </div>
        <div class="kpi-box">
          <div class="kpi-val mono">${testDevices.ephemeralSimulators.length}</div>
          <div class="kpi-lbl">${t('残留临时模拟器', 'Leftover Sims')}</div>
        </div>
        <div class="kpi-box">
          <div class="kpi-val mono">${testDevices.runningQemu.length}</div>
          <div class="kpi-lbl">${t('运行中 QEMU', 'Running QEMU')}</div>
        </div>
      </div>
    </div>`
}

function renderHealthTab(snap) {
  const { jobs, doctor, audit } = snap.health
  const busy = (snap.busyActions || []).length > 0
  const sortedJobs = [...jobs].sort((a, b) => {
    const score = (s) => (s === 'fail' || s === 'stale' || s === 'absent' ? 0 : s === 'warn' ? 1 : 2)
    return score(a.state) - score(b.state)
  })

  return `
    <div class="card">
      <div class="card-head">
        <span class="card-title">🛡️ ${t('Blueprint 后台守护任务 (8 项)', 'Blueprint LaunchAgents')}</span>
        <button class="xs-btn" data-action="refresh" data-scope="health" ${busy ? 'disabled' : ''}>${t('重新体检', 'Run Health Check')}</button>
      </div>
      ${sortedJobs.length === 0
        ? `<div class="empty-box">${t('未读取到 Codex Storage 状态文件', 'No LaunchAgent status files found')}</div>`
        : `<div class="job-grid">
            ${sortedJobs.map((j) => {
              const cls = j.state === 'fail' || j.state === 'stale' || j.state === 'absent' ? 'danger' : j.state === 'warn' ? 'warn' : 'ok'
              return `
                <div class="job-card ${cls !== 'ok' ? cls : ''}">
                  <div class="item-top">
                    <strong class="xs truncate">${esc(j.name)}</strong>
                    <span class="pill ${cls}">${esc(j.state)}</span>
                  </div>
                  <div class="xs muted mono">${esc(j.time || '—')}</div>
                  <div class="xs muted truncate" title="${esc(j.message)}">${esc(j.message || '—')}</div>
                </div>`
            }).join('')}
          </div>`}
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">🩺 ${t('Doctor 系统体检', 'Doctor Health Check')}</span>
        <span class="pill ${doctor.ok === true ? 'ok' : doctor.ok === false ? 'danger' : ''}">${doctor.ok === true ? 'OK' : doctor.ok === false ? `${doctor.attention.length} ${t('待处理', 'attention')}` : t('未运行', 'Pending')}</span>
      </div>
      ${doctor.error ? `<div class="xs" style="color:var(--ops-danger)">${esc(doctor.error)}</div>` : ''}
      ${doctor.attention.length > 0 ? `<div class="row-list">${doctor.attention.map((a) => `<div class="item-row"><span class="xs">🔴 ${esc(a)}</span></div>`).join('')}</div>` : `<div class="xs muted">${t('当前无必须处理项', 'Nothing needs attention')}</div>`}
      ${doctor.advice.length > 0 ? `<div class="xs muted">${t(`优化建议 (${doctor.advice.length})：`, `Advice (${doctor.advice.length}): `)}${esc(doctor.advice.slice(0, 3).join(' · '))}</div>` : ''}
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">🧬 ${t('启动上下文 Token 瘦身雷达', 'Startup Context Token Radar')}</span>
        <span class="pill ${audit.globalRulesInSync === false ? 'warn' : 'ok'}">${audit.globalRulesInSync === false ? t('规则未同步', 'Drift') : t('规则已同步', 'In Sync')}</span>
      </div>
      <div class="grid-4">
        <div class="kpi-box">
          <div class="kpi-val mono">${audit.globalTokens}</div>
          <div class="kpi-lbl">${t('全局规则 Token', 'Global Rules Tok')}</div>
        </div>
        <div class="kpi-box">
          <div class="kpi-val mono">${audit.skillsTokens}</div>
          <div class="kpi-lbl">${t(`Skills (${audit.skillsCount}个)`, `Skills (${audit.skillsCount})`)}</div>
        </div>
        <div class="kpi-box">
          <div class="kpi-val mono">${audit.mcpClaude} / ${audit.mcpCodex}</div>
          <div class="kpi-lbl">MCP (Cl / Cx)</div>
        </div>
        <div class="kpi-box">
          <div class="kpi-val mono">${audit.projectsAudited}</div>
          <div class="kpi-lbl">${t('已审计项目', 'Projects Audited')}</div>
        </div>
      </div>
      ${audit.attention.length > 0 ? `
        <div class="row-list">
          ${audit.attention.map((x) => `<div class="item-row"><span class="xs">🟡 ${esc(x)}</span></div>`).join('')}
        </div>` : ''}
      ${audit.flaggedProjects.length > 0 ? `
        <div class="xs muted">${t('规则/目录建议关注项目', 'Flagged Projects')}</div>
        <div class="row-list">
          ${audit.flaggedProjects.slice(0, 8).map((p) => `
            <div class="item-row">
              <div class="item-top">
                <span class="item-name">${esc(p.name)}</span>
                <span class="xs mono muted">${p.agentsLines}L · ~${p.agentsTokens} tok</span>
              </div>
              <div class="xs muted">${esc([...p.issues, ...p.smells].join(' · '))}</div>
            </div>`).join('')}
        </div>` : ''}
    </div>`
}

function render() {
  const snap = state.snapshot
  if (!snap) {
    appEl.innerHTML = `
      <div class="loading-shell">
        <div class="spinner"></div>
        <div class="muted">${esc(state.localError || t('正在加载工作区监测数据…', 'Loading workspace telemetry…'))}</div>
      </div>`
    return
  }

  const busy = (snap.busyActions || []).length > 0
  const dotCls = busy
    ? 'loading'
    : snap.counts.criticalAlerts > 0
      ? 'danger'
      : snap.counts.warnAlerts > 0 || snap.counts.staleWaitingAgents > 0
        ? 'warn'
        : ''

  const alertCount = snap.counts.criticalAlerts + snap.counts.warnAlerts
  const tabs = [
    { id: 'overview', label: t('总览', 'Overview'), badge: null },
    { id: 'cleanup', label: t('清理记录', 'Cleanup'), badge: snap.counts.trashBatches || null, tone: '' },
    { id: 'storage', label: t('空间大户', 'Storage'), badge: null },
    {
      id: 'live',
      label: t('Bots & 实时', 'Bots & Live'),
      badge: snap.counts.waitingAgents > 0 ? snap.counts.waitingAgents : null,
      tone: snap.counts.staleWaitingAgents > 0 ? 'danger' : 'warn'
    },
    {
      id: 'health',
      label: t('健康体检', 'Health'),
      badge: alertCount > 0 ? alertCount : null,
      tone: snap.counts.criticalAlerts > 0 ? 'danger' : 'warn'
    }
  ]

  let body = ''
  if (state.tab === 'overview') body = renderOverview(snap)
  else if (state.tab === 'cleanup') body = renderCleanupTab(snap)
  else if (state.tab === 'storage') body = renderStorageTab(snap)
  else if (state.tab === 'live') body = renderLiveTab(snap)
  else body = renderHealthTab(snap)

  const prevScrollY = typeof window.scrollY === 'number' ? window.scrollY : 0

  appEl.innerHTML = `
    <div class="topbar">
      <div class="brand">
        <span class="status-dot ${dotCls}"></span>
        <span class="brand-title">${t('工作区运维雷达', 'Ops Radar')}</span>
        <span class="xs muted">${esc(formatRelative(snap.generatedAt))}</span>
      </div>
      <div class="topbar-actions">
        <button class="xs-btn" data-action="refresh" data-scope="all" ${busy ? 'disabled' : ''}>
          ${busy ? t('刷新中…', 'Refreshing…') : t('↻ 刷新全部', '↻ Refresh')}
        </button>
      </div>
    </div>

    ${state.localError ? `<div class="notice danger"><span>${esc(state.localError)}</span></div>` : ''}
    ${snap.lastActionNotice ? `<div class="notice ${esc(snap.lastActionNotice.tone)}"><span>${esc(snap.lastActionNotice.message)}</span><span class="xs muted">${esc(formatRelative(snap.lastActionNotice.at))}</span></div>` : ''}

    ${renderAlertBanner(snap)}

    <div class="tabs" role="tablist">
      ${tabs
        .map(
          (tb) => `
          <button class="tab-btn ${state.tab === tb.id ? 'active' : ''}" data-action="tab" data-tab="${tb.id}" role="tab">
            ${esc(tb.label)}
            ${tb.badge ? `<span class="tab-badge ${tb.tone || ''}">${tb.badge}</span>` : ''}
          </button>`
        )
        .join('')}
    </div>

    ${body}
  `

  if (prevScrollY > 0 && typeof window.scrollTo === 'function') {
    window.scrollTo(0, prevScrollY)
  }
}

// Event delegation for all interactive controls
appEl.addEventListener('click', (ev) => {
  const target = ev.target.closest('[data-action]')
  if (!target) return
  const action = target.dataset.action

  if (action === 'tab') {
    state.tab = target.dataset.tab
    render()
    return
  }
  if (action === 'refresh') {
    void req('ops.refresh', { scope: target.dataset.scope || 'all' })
    return
  }
  if (action === 'gc') {
    void req('ops.runGc', { mode: target.dataset.mode, apply: target.dataset.apply === '1' }, 110_000)
    return
  }
  if (action === 'toggle-batch') {
    const id = target.dataset.batch
    state.expandedBatchId = state.expandedBatchId === id ? '__none__' : id
    state.confirmRestorePath = null
    render()
    return
  }
  if (action === 'restore-preview') {
    void req('ops.restoreBatch', { manifestPath: target.dataset.manifest, apply: false })
    return
  }
  if (action === 'restore-arm') {
    state.confirmRestorePath = target.dataset.manifest
    render()
    return
  }
  if (action === 'restore-cancel') {
    state.confirmRestorePath = null
    render()
    return
  }
  if (action === 'restore-confirm') {
    state.confirmRestorePath = null
    void req('ops.restoreBatch', { manifestPath: target.dataset.manifest, apply: true })
    return
  }
  if (action === 'storage-filter') {
    state.storageFilter = target.dataset.filter
    render()
    return
  }
  if (action === 'copy-path') {
    const p = target.dataset.path || ''
    if (p) {
      void copyTextSafe(p).then((ok) => {
        target.textContent = ok ? '✓' : '!'
        setTimeout(() => { target.textContent = '📋' }, 1200)
      })
    }
    return
  }
  if (action === 'toggle-keep') {
    void req('ops.toggleKeep', { path: target.dataset.path, keep: target.dataset.keep === '1' })
    return
  }
  if (action === 'open-ws') {
    void req('ops.openWorkspace', { workspaceId: target.dataset.ws })
    return
  }
  if (action === 'orca-cleanup') {
    void req('ops.runOrcaCleanup', { apply: target.dataset.apply === '1' })
    return
  }
  if (action === 'dismiss-alert') {
    void req('ops.dismissAlert', { fingerprint: target.dataset.fp })
    return
  }
  if (action === 'escalate-all') {
    void req('ops.escalateBot', { mode: 'handoff' })
    return
  }
  if (action === 'escalate-one') {
    void req('ops.escalateBot', { alertIds: [target.dataset.alert], mode: 'handoff' })
    return
  }
  if (action === 'escalate-custom') {
    const note = state.botNote
    state.botNote = ''
    void req('ops.escalateBot', { mode: 'handoff', customNote: note, newChat: state.botNewChat })
    return
  }
  if (action === 'escalate-status') {
    const note = state.botNote
    state.botNote = ''
    void req('ops.escalateBot', { mode: 'status', customNote: note, newChat: false })
  }
})

appEl.addEventListener('compositionstart', () => {
  state.composing = true
})

appEl.addEventListener('compositionend', () => {
  state.composing = false
  if (document.activeElement?.id === 'storage-search') {
    state.storageSearch = document.activeElement.value
    const pos = document.activeElement.selectionStart
    render()
    const next = document.getElementById('storage-search')
    if (next) {
      next.focus()
      if (pos !== null) next.setSelectionRange(pos, pos)
    }
  }
})

appEl.addEventListener('focusout', () => {
  setTimeout(flushPendingSnapshot, 50)
})

appEl.addEventListener('input', (ev) => {
  if (ev.target.id === 'storage-search') {
    state.storageSearch = ev.target.value
    if (state.composing) return
    const pos = ev.target.selectionStart
    render()
    const next = document.getElementById('storage-search')
    if (next) {
      next.focus()
      if (pos !== null) next.setSelectionRange(pos, pos)
    }
  } else if (ev.target.id === 'bot-note-input') {
    state.botNote = ev.target.value
  }
})

appEl.addEventListener('change', (ev) => {
  if (ev.target.id === 'bot-mode-select') {
    void req('ops.updateSetting', { key: 'botAlertMode', value: ev.target.value })
  } else if (ev.target.id === 'bot-new-chat') {
    state.botNewChat = Boolean(ev.target.checked)
  }
})

// Boot: subscribe to worker pushes and theme changes, then fetch initial snapshot
if (window.orca) {
  window.orca.plugin.on('ops.changed', (next) => {
    if (isUserEditing()) {
      state.pendingSnapshot = next
      return
    }
    state.pendingSnapshot = null
    state.snapshot = next
    render()
  })
  window.orca.on('theme', () => {
    if (!isUserEditing()) render()
  })
  // Refresh relative countdown labels every 30s locally without polling the worker.
  setInterval(() => {
    if (state.snapshot && !isUserEditing()) {
      render()
    }
  }, 30_000)
  void req('ops.snapshot')
}
