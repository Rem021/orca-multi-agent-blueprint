// Workspace Ops Radar — streamlined 3-view cockpit panel with crisp SVG icons & micro-interactions.
// Communicates solely with src/worker.ts via `window.orca.plugin.request` and `orca.plugin.on`.

const appEl = document.getElementById('app')

const SPECTRUM_COLORS = [
  'var(--ops-accent)',
  'var(--ops-violet)',
  'var(--ops-teal)',
  'var(--ops-warn)',
  'var(--ops-ok)',
  'var(--ops-danger)'
]

const CAT_ZH = {
  legacy_work: '历史 work/',
  stranded_worktree: '滞留 work/',
  worktree: 'Worktree',
  shared_data: 'AI/Data',
  derived_data: 'DerivedData',
  work_scratch: '_scratch',
  work_tests: '_tests',
  xcode_test_runner: 'XCTest',
  claude_session_env: 'Claude',
  codex_tmp: 'Codex .tmp',
  codex_log: 'Codex 日志',
  orca_chat_scratch: 'Orca 缓存',
  antigravity_scratch: 'AGY 缓存',
  antigravity_log: 'AGY 日志',
  gc_candidate: '可回收',
  other: '其他'
}

const CAT_EN = {
  legacy_work: 'Legacy work/',
  stranded_worktree: 'Stranded work/',
  worktree: 'Worktree',
  shared_data: 'AI/Data',
  derived_data: 'DerivedData',
  work_scratch: '_scratch',
  work_tests: '_tests',
  xcode_test_runner: 'XCTest',
  claude_session_env: 'Claude',
  codex_tmp: 'Codex .tmp',
  codex_log: 'Codex logs',
  orca_chat_scratch: 'Orca cache',
  antigravity_scratch: 'AGY cache',
  antigravity_log: 'AGY logs',
  gc_candidate: 'Reclaimable',
  other: 'Other'
}

// Crisp Lucide-style stroke icons (zero system emojis)
const ICON_PATHS = {
  radar: '<circle cx="12" cy="12" r="9"/><path d="M12 12l5-5"/><path d="M12 7a5 5 0 1 0 5 5"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
  sparkles: '<path d="M12 3l1.8 4.2L18 9l-4.2 1.8L12 15l-1.8-4.2L6 9l4.2-1.8z"/><path d="M5 17l.9 2.1L8 20l-2.1.9L5 23l-.9-2.1L2 20l2.1-.9z"/>',
  trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/>',
  hardDrive: '<line x1="22" y1="12" x2="2" y2="12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  bot: '<rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8.01" y2="16"/><line x1="16" y1="16" x2="16.01" y2="16"/>',
  activity: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  unlock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>',
  archive: '<rect x="2" y="4" width="20" height="5" rx="1"/><path d="M4 9v9a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9"/><path d="M10 13h4"/>',
  external: '<path d="M15 3h6v6"/><path d="M10 14L21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
  chevronDown: '<polyline points="6 9 12 15 18 9"/>',
  chevronRight: '<polyline points="9 18 15 12 9 6"/>',
  send: '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
  eye: '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
  alert: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  undo: '<path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13"/>',
  search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/>',
  layers: '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>'
}

function icon(name, cls = 'ico') {
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[name] || ''}</svg>`
}

const state = {
  snapshot: null,
  pendingSnapshot: null,
  composing: false,
  tab: 'overview', // 'overview' | 'space' | 'live'
  spaceSub: 'offenders', // 'offenders' | 'history' | 'archive'
  gcMode: 'daily', // 'daily' | 'session-end' | 'deep'
  storageFilter: 'all',
  storageSearch: '',
  wsFilter: 'active', // 'active' | 'all'
  expandedAlertId: null,
  expandedBatchId: null,
  expandedJobName: null,
  showMemory: false,
  showAuditDetails: false,
  confirmRestorePath: null,
  botNote: '',
  botNewChat: false,
  toast: null, // { tone, message, at }
  dismissedNoticeAt: 0,
  lastRenderedView: '',
  localError: null
}

let toastTimer = null
let toastLeaveTimer = null
let shellMounted = false

const GC_MODE_HINTS = {
  daily: ['日常：回收过期缓存、日志与已完成临时目录', 'Daily: expired caches, logs & temp scratch'],
  'session-end': ['收尾：额外清理本次会话测试产物（均进废纸篓可撤销）', 'End: also cleans session test temp dirs (reversible)'],
  deep: ['深度：额外清理过期 DerivedData 与深度缓存', 'Deep: also sweeps stale DerivedData & deep caches']
}

const isZh = () => String(window.orca?.locale || 'zh').toLowerCase().startsWith('zh')
const t = (zh, en) => (isZh() ? zh : en)
const catLabel = (c) => (isZh() ? CAT_ZH : CAT_EN)[c] || c

function esc(v) {
  return String(v ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let v = bytes
  let i = 0
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i++
  }
  const d = i === 0 ? 0 : v >= 100 ? 0 : v >= 10 ? 1 : 2
  return `${v.toFixed(d)} ${u[i]}`
}

function formatCountdown(targetMs) {
  if (!targetMs) return '—'
  const diff = targetMs - Date.now()
  if (diff <= 0) return t('即将', 'Due')
  const mins = Math.ceil(diff / 60_000)
  if (mins < 60) return `${mins}m`
  const h = Math.floor(mins / 60)
  const m = mins % 60
  if (h < 24) return m ? `${h}h ${String(m).padStart(2, '0')}m` : `${h}h`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

function formatRelative(ms) {
  if (!ms) return '—'
  const diff = Date.now() - ms
  if (diff < 60_000) return t('刚刚', 'now')
  const mins = Math.floor(diff / 60_000)
  if (mins < 60) return isZh() ? `${mins}m 前` : `${mins}m`
  const h = Math.floor(mins / 60)
  if (h < 24) return isZh() ? `${h}h 前` : `${h}h`
  return isZh() ? `${Math.floor(h / 24)}d 前` : `${Math.floor(h / 24)}d`
}

function formatClock(ms) {
  if (!ms) return '—'
  const d = new Date(ms)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function shortPath(p) {
  return p ? p.replace(/^\/Users\/[^/]+/, '~') : ''
}

function hasBusy(snap, keyOrPrefix, prefix = false) {
  const list = snap?.busyActions || []
  return prefix ? list.some((b) => String(b).startsWith(keyOrPrefix)) : list.includes(keyOrPrefix)
}

function itemSubtitle(it, includeKind = true) {
  const parts = []
  const dupProject = it.project && it.name && it.name.toLowerCase().startsWith(it.project.toLowerCase())
  if (it.project && !dupProject) parts.push(it.project)
  if (includeKind) parts.push(catLabel(it.kind))
  if (it.ageDays !== null && it.ageDays !== undefined) parts.push(`${it.ageDays}d`)
  return parts.join(' · ')
}

function isUserEditing() {
  if (state.composing) return true
  const el = document.activeElement
  if (!el || !el.tagName) return false
  const tag = el.tagName.toUpperCase()
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
}

function flushPendingSnapshot() {
  if (!state.pendingSnapshot || isUserEditing()) return
  applySnapshot(state.pendingSnapshot)
  state.pendingSnapshot = null
}

function dismissToast() {
  clearTimeout(toastTimer)
  clearTimeout(toastLeaveTimer)
  const el = document.querySelector('#ops-toast-wrap .toast')
  if (el) {
    el.classList.add('leaving')
    toastLeaveTimer = setTimeout(() => {
      state.toast = null
      renderToast()
    }, 180)
  } else {
    state.toast = null
    renderToast()
  }
}

function showToast(tone, message, at = Date.now()) {
  clearTimeout(toastTimer)
  clearTimeout(toastLeaveTimer)
  state.toast = { tone, message, at }
  renderToast()
  toastTimer = setTimeout(dismissToast, 4200)
}

async function copyTextSafe(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // Fallback below for restricted frame clipboard policies.
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

function applySnapshot(next) {
  state.snapshot = next
  if (next?.lastActionNotice && next.lastActionNotice.at > state.dismissedNoticeAt) {
    state.dismissedNoticeAt = next.lastActionNotice.at
    showToast(next.lastActionNotice.tone, next.lastActionNotice.message, next.lastActionNotice.at)
  }
  render()
}

async function req(name, input, timeoutMs = 95_000) {
  state.localError = null
  try {
    const out = await window.orca.plugin.request(name, input, { timeoutMs })
    if (out && typeof out === 'object' && 'generatedAt' in out) {
      applySnapshot(out)
    }
    return out
  } catch (err) {
    const msg = err?.message || err?.code || 'request_failed'
    state.localError = msg
    showToast('danger', msg)
    render()
    return null
  }
}

/* ────────────────── Visual Components ────────────────── */

function renderSpectrum(categories) {
  const total = categories.reduce((s, c) => s + (c.bytes || 0), 0)
  if (!total) return ''
  const top = categories.slice(0, 4)
  const rest = categories.slice(4).reduce((s, c) => s + (c.bytes || 0), 0)
  const segs = rest > 0 ? [...top, { category: 'other', bytes: rest, count: 0 }] : top

  const track = segs
    .map((c, i) => {
      const pct = Math.max(3, (c.bytes / total) * 100)
      return `<div class="spectrum-seg" style="width:${pct.toFixed(1)}%;background:${SPECTRUM_COLORS[i % SPECTRUM_COLORS.length]}" title="${esc(catLabel(c.category))}: ${esc(formatBytes(c.bytes))}"></div>`
    })
    .join('')

  const legend = segs
    .map((c, i) => `
      <span class="legend-chip">
        <span class="dot-sq" style="background:${SPECTRUM_COLORS[i % SPECTRUM_COLORS.length]}"></span>
        <span>${esc(catLabel(c.category))}</span>
        <strong class="num">${esc(formatBytes(c.bytes))}</strong>
      </span>`)
    .join('')

  return `<div class="spectrum-track">${track}</div><div class="legend-wrap">${legend}</div>`
}

function renderTrendSvg(days) {
  if (!days || days.length === 0) {
    return `<div class="empty-line">${t('暂无 7 日清理趋势', 'No 7-day cleanup trend yet')}</div>`
  }
  const maxB = Math.max(1, ...days.map((d) => d.reclaimedBytes))
  const w = 316
  const h = 88
  const barW = 24
  const gap = (w - barW * days.length) / (days.length + 1)
  const plotH = 50
  const baseY = 68

  const bars = days
    .map((d, i) => {
      const x = gap + i * (barW + gap)
      const bh = d.reclaimedBytes > 0 ? Math.max(6, Math.round((d.reclaimedBytes / maxB) * plotH)) : 3
      const y = baseY - bh
      const isPeak = d.reclaimedBytes > 0 && d.reclaimedBytes === maxB
      const topLbl = d.reclaimedBytes > 0 ? formatBytes(d.reclaimedBytes).replace(' ', '') : ''
      return `
        <g>
          <title>${esc(d.date)}: ${esc(formatBytes(d.reclaimedBytes))} (${d.reclaimedItems} items)</title>
          ${topLbl ? `<text x="${x + barW / 2}" y="${Math.max(11, y - 5)}" text-anchor="middle" font-size="8.5" font-weight="${isPeak ? '650' : '500'}" fill="${isPeak ? 'var(--orca-foreground)' : 'var(--orca-muted-foreground)'}">${esc(topLbl)}</text>` : ''}
          <rect class="trend-bar" x="${x}" y="${y}" width="${barW}" height="${bh}" rx="4" fill="${d.reclaimedBytes > 0 ? 'url(#opsBarGrad)' : 'var(--ops-hairline)'}" opacity="${isPeak ? '1' : d.reclaimedBytes > 0 ? '0.78' : '0.45'}"></rect>
          <text x="${x + barW / 2}" y="${h - 4}" text-anchor="middle" font-size="9" fill="var(--orca-muted-foreground)">${esc(d.date.slice(5))}</text>
        </g>`
    })
    .join('')

  return `
    <svg class="trend-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">
      <defs>
        <linearGradient id="opsBarGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--ops-accent)" />
          <stop offset="100%" stop-color="var(--ops-violet)" stop-opacity="0.72" />
        </linearGradient>
      </defs>
      <line x1="4" y1="${baseY}" x2="${w - 4}" y2="${baseY}" stroke="var(--ops-hairline)" stroke-dasharray="2 3" />
      ${bars}
    </svg>`
}

function renderAlertsCard(snap) {
  const active = (snap.alerts || []).filter((a) => !a.dismissed && a.severity !== 'info')
  if (active.length === 0) return ''
  const hasCrit = active.some((a) => a.severity === 'critical')
  const botName = snap.settings?.botName || 'Ram'
  const busyBot = (snap.busyActions || []).includes('bot-send')

  const items = active
    .slice(0, 4)
    .map((a) => {
      const open = state.expandedAlertId === a.id
      const dotCls = a.severity === 'critical' ? 'danger pulse' : 'warn'
      return `
        <div class="alert-item">
          <div class="alert-item-head" data-action="toggle-alert" data-alert="${esc(a.id)}">
            <div class="alert-item-title">
              <span class="live-dot ${dotCls}"></span>
              <span class="truncate">${esc(a.title)}</span>
            </div>
            <div class="row-actions">
              <button class="icon-btn" data-action="escalate-one" data-alert="${esc(a.id)}" title="${t(`交给 ${botName}`, `Ask ${botName}`)}" ${busyBot ? 'disabled' : ''}>${icon('bot', 'ico-sm')}</button>
              <button class="icon-btn" data-action="dismiss-alert" data-fp="${esc(a.fingerprint)}" title="${t('忽略 24h', 'Dismiss 24h')}">${icon('x', 'ico-xs')}</button>
            </div>
          </div>
          <div class="accordion ${open ? 'open' : ''}" data-accordion="alert:${esc(a.id)}">
            <div class="accordion-inner">
              <div class="alert-detail">
                <div>${esc(a.detail)}</div>
                ${a.workspaceId ? `<div><button class="link-btn" data-action="open-ws" data-ws="${esc(a.workspaceId)}">${t('打开对应工作区 →', 'Open workspace →')}</button></div>` : ''}
                ${a.escalatedAt ? `<div style="color:var(--ops-accent)">${t(`已派给 ${botName} · `, `Sent to ${botName} · `)}${esc(formatRelative(a.escalatedAt))}</div>` : ''}
              </div>
            </div>
          </div>
        </div>`
    })
    .join('')

  return `
    <div class="alert-card ${hasCrit ? 'critical' : ''}">
      <div class="card-head">
        <span style="display:inline-flex;align-items:center;gap:6px;font-weight:620;font-size:12px;color:${hasCrit ? 'var(--ops-danger)' : 'var(--ops-warn)'}">
          ${icon('alert', 'ico-sm')}
          <span>${t(`${active.length} 项待关注`, `${active.length} active alert(s)`)}</span>
        </span>
        <button class="pill-btn primary" data-action="escalate-all" ${busyBot ? 'disabled' : ''}>
          ${icon('bot', 'ico-xs')}
          <span>${busyBot ? t('发送中…', 'Sending…') : t(`让 ${botName} 分析`, `Ask ${botName}`)}</span>
        </button>
      </div>
      <div class="list">${items}</div>
    </div>`
}

/* ────────────────── View 1: Overview Cockpit ────────────────── */

function renderOverviewView(snap) {
  const gov = snap.cleanup.governor
  const orcaC = snap.cleanup.orca
  const disk = snap.disk
  const busyGc = hasBusy(snap, 'gc:', true)

  // Disk circular gauge math (radius = 33, circumference ≈ 207.35)
  const R = 33
  const C = 2 * Math.PI * R
  const freePct = disk ? disk.freePct : 0
  const usedRatio = disk ? Math.max(0.03, Math.min(1, 1 - freePct / 100)) : 0
  const dashOffset = C * (1 - usedRatio)
  const diskColor = !disk
    ? 'var(--ops-accent)'
    : freePct <= snap.settings.diskWarnFreePercent / 2
      ? 'var(--ops-danger)'
      : freePct <= snap.settings.diskWarnFreePercent
        ? 'var(--ops-warn)'
        : 'var(--ops-ok)'

  let govPct = 10
  if (gov.lastRunAt && gov.nextRunAt && gov.nextRunAt > gov.lastRunAt) {
    govPct = Math.max(4, Math.min(100, ((Date.now() - gov.lastRunAt) / (gov.nextRunAt - gov.lastRunAt)) * 100))
  }
  let orcaPct = 10
  if (orcaC.lastRun?.startedAt && orcaC.nextRunAt && orcaC.nextRunAt > orcaC.lastRun.startedAt) {
    orcaPct = Math.max(4, Math.min(100, ((Date.now() - orcaC.lastRun.startedAt) / (orcaC.nextRunAt - orcaC.lastRun.startedAt)) * 100))
  }

  const dry = gov.dryRun
  const top3 = (snap.storage.topOffenders || []).slice(0, 3)
  const maxOffender = Math.max(1, top3[0]?.sizeBytes || 1)
  const previewItems = (dry?.candidates || []).filter((c) => c.sizeBytes > 0).slice(0, 3)

  return `
    ${renderAlertsCard(snap)}

    <div class="card">
      <div class="hero-grid">
        <div class="ring-box" title="${disk ? `${formatBytes(disk.usedBytes)} / ${formatBytes(disk.totalBytes)}` : ''}">
          <svg class="ring-svg" viewBox="0 0 82 82">
            <circle class="ring-track" cx="41" cy="41" r="${R}" />
            <circle class="ring-arc" cx="41" cy="41" r="${R}" stroke="${diskColor}" stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${dashOffset.toFixed(1)}" data-dash-full="${C.toFixed(1)}" data-dash-target="${dashOffset.toFixed(1)}" />
          </svg>
          <div class="ring-center">
            <div class="ring-pct num">${disk ? `${Math.round(freePct)}%` : '—'}</div>
            <div class="ring-sub">${t('磁盘可用', 'Free')}</div>
          </div>
        </div>

        <div class="timers-col">
          <div class="timer-row">
            <div class="timer-top">
              <span class="timer-name"><span class="live-dot ${gov.lastState === 'fail' ? 'danger' : gov.lastState === 'warn' ? 'warn' : 'ok'}"></span>${t('缓存 GC', 'Governor GC')}</span>
              <span class="timer-val num">${esc(formatCountdown(gov.nextRunAt))}</span>
            </div>
            <div class="thin-bar"><div style="width:${govPct.toFixed(0)}%;background:var(--ops-ok)"></div></div>
          </div>

          <div class="timer-row">
            <div class="timer-top">
              <span class="timer-name"><span class="live-dot accent"></span>${t('工作树清理', 'Worktrees')}</span>
              <span class="timer-val num">${esc(formatCountdown(orcaC.nextRunAt))}</span>
            </div>
            <div class="thin-bar"><div style="width:${orcaPct.toFixed(0)}%;background:var(--ops-accent)"></div></div>
          </div>

          <div class="xxs muted truncate num">
            ${disk ? `${t('余', 'Avail')} ${esc(formatBytes(disk.availBytes))} / ${esc(formatBytes(disk.totalBytes))}` : ''}
            ${gov.lastManualGcAt ? ` · ${t('手动', 'Manual')} ${esc(formatRelative(gov.lastManualGcAt))}` : ''}
          </div>
        </div>
      </div>

      <div class="divider"></div>

      <div class="action-strip">
        <div class="mode-pills" role="group" aria-label="GC mode">
          ${[
            ['daily', t('日常', 'Daily')],
            ['session-end', t('收尾', 'End')],
            ['deep', t('深度', 'Deep')]
          ]
            .map(([m, lbl]) => `<button class="mode-pill ${state.gcMode === m ? 'active' : ''}" data-action="gc-mode" data-mode="${m}">${lbl}</button>`)
            .join('')}
        </div>
        <div class="action-right">
          <button class="icon-btn" data-action="gc" data-apply="0" title="${t('预览可清理项（不改动磁盘）', 'Preview (Dry Run)')}" ${busyGc ? 'disabled' : ''}>${icon('eye', 'ico-sm')}</button>
          <button class="primary" data-action="gc" data-apply="1" ${busyGc ? 'disabled' : ''}>
            ${icon('sparkles', 'ico-sm')}
            <span>${busyGc ? t('清理中…', 'Cleaning…') : t('立即清理', 'Clean Now')}</span>
          </button>
        </div>
      </div>
      <div id="gc-mode-hint" class="xxs muted truncate">${esc(t(...(GC_MODE_HINTS[state.gcMode] || GC_MODE_HINTS.daily)))}</div>

      ${previewItems.length > 0 ? `
        <div class="list">
          <div class="row-sub">
            <span>${t(`预览命中 ${dry.reclaimableCount} 项`, `Preview: ${dry.reclaimableCount} item(s)`)}</span>
            <strong class="mono">${esc(formatBytes(dry.reclaimableBytes))}</strong>
          </div>
          ${previewItems
            .map(
              (c) => `
              <div class="row-card">
                <div class="row-top">
                  <div class="row-left">
                    <span class="tag warn">${esc(catLabel(c.category))}</span>
                    <span class="row-title">${esc(c.path.split('/').pop() || c.path)}</span>
                  </div>
                  <strong class="mono xs">${esc(formatBytes(c.sizeBytes))}</strong>
                </div>
              </div>`
            )
            .join('')}
        </div>` : ''}
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-label">${icon('activity', 'ico-sm')}${t('回收趋势与滞留', 'Reclamation & Space')}</span>
        <span class="xxs muted num">${t(`${gov.historyRuns.length} 轮记录`, `${gov.historyRuns.length} runs`)}</span>
      </div>

      <div class="kpi-strip">
        <div class="kpi-cell">
          <div class="kpi-num num">${esc(formatBytes(gov.totalReclaimed24hBytes))}</div>
          <div class="kpi-cap">${t('24h 回收', '24h Freed')}</div>
        </div>
        <div class="kpi-cell">
          <div class="kpi-num num">${esc(formatBytes(gov.totalReclaimed7dBytes))}</div>
          <div class="kpi-cap">${t('7日累计', '7d Freed')}</div>
        </div>
        <div class="kpi-cell">
          <div class="kpi-num num">${snap.storage.scannedAt ? esc(formatBytes((snap.storage.legacyTotalBytes || 0) + (snap.storage.strandedTotalBytes || 0))) : '<span class="shimmer"></span>'}</div>
          <div class="kpi-cap">${t('滞留 work/', 'Stranded')}</div>
        </div>
      </div>

      ${renderTrendSvg(gov.trendDays)}
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-label">${icon('hardDrive', 'ico-sm')}${t('最大空间占用 Top 3', 'Top Storage')}</span>
        <button class="link-btn" data-action="goto-space">${t('查看全部 →', 'All →')}</button>
      </div>
      ${top3.length === 0
        ? `<div class="xs muted">${snap.storage.scannedAt ? t('暂无大体积目录', 'No large folders found') : t('后台扫描大户中…', 'Scanning storage…')}</div>`
        : `<div class="list">
            ${top3
              .map((it) => {
                const pct = Math.max(4, Math.round((it.sizeBytes / maxOffender) * 100))
                const sub = itemSubtitle(it, true)
                const clickAttr = it.workspaceId
                  ? `data-action="open-ws" data-ws="${esc(it.workspaceId)}" title="${t('点击打开工作区', 'Click to open workspace')}"`
                  : `data-action="goto-space" title="${t('在空间清理中查看', 'View in Space')}"`
                return `
                  <div class="row-card interactive" ${clickAttr}>
                    <div class="row-top">
                      <div class="row-left">
                        <span class="row-title">${esc(it.name)}</span>
                        <span class="xxs muted truncate">${esc(sub)}</span>
                      </div>
                      <strong class="mono xs">${esc(formatBytes(it.sizeBytes))}</strong>
                    </div>
                    <div class="thin-bar"><div style="width:${pct}%;background:linear-gradient(90deg,var(--ops-accent),var(--ops-violet))"></div></div>
                  </div>`
              })
              .join('')}
          </div>`}
    </div>`
}

/* ────────────────── View 2: Space & Cleanup ────────────────── */

function renderStorageItemsHtml(snap) {
  const busyArchive = hasBusy(snap, 'archive')
  const busyKeep = hasBusy(snap, 'keep')
  const all = snap.storage.topOffenders || []
  const q = state.storageSearch.trim().toLowerCase()
  const filtered = all.filter((it) => {
    if (state.storageFilter !== 'all' && it.kind !== state.storageFilter) return false
    if (q && !`${it.name} ${it.project} ${it.path} ${it.reason}`.toLowerCase().includes(q)) return false
    return true
  })
  if (filtered.length === 0) {
    return `<div class="empty-line">${t('没有匹配的目录', 'No matching items')}</div>`
  }
  const maxB = Math.max(1, filtered[0]?.sizeBytes || 1)
  return filtered
    .slice(0, 35)
    .map((it, idx) => {
      const pct = Math.max(3, Math.round((it.sizeBytes / maxB) * 100))
      return `
        <div class="row-card">
          <div class="row-top">
            <div class="row-left">
              <span class="xxs mono muted">#${idx + 1}</span>
              <span class="row-title" title="${esc(it.path)}">${esc(it.name)}</span>
            </div>
            <strong class="mono xs">${esc(formatBytes(it.sizeBytes))}</strong>
          </div>
          <div class="thin-bar"><div style="width:${pct}%;background:${it.kind === 'gc_candidate' ? 'var(--ops-warn)' : 'var(--ops-violet)'}"></div></div>
          <div class="row-sub">
            <span class="truncate">${esc(itemSubtitle(it, true))}</span>
            <div class="row-actions">
              <button class="icon-btn" data-action="copy-path" data-path="${esc(it.path)}" title="${t('复制路径', 'Copy path')}">${icon('copy', 'ico-xs')}</button>
              ${it.workspaceId ? `<button class="icon-btn" data-action="open-ws" data-ws="${esc(it.workspaceId)}" title="${t('打开工作区', 'Open workspace')}">${icon('external', 'ico-xs')}</button>` : ''}
              ${it.canArchive ? `<button class="icon-btn" data-action="archive-wt" data-path="${esc(it.path)}" data-topic="${esc(it.name)}" title="${t('安全归档到 AI/Archive', 'Archive safely')}" ${busyArchive ? 'disabled' : ''}>${icon('archive', 'ico-xs')}</button>` : ''}
              ${it.canToggleKeep ? `<button class="icon-btn ${it.pinned ? 'pinned' : ''}" data-action="toggle-keep" data-path="${esc(it.path)}" data-keep="${it.pinned ? '0' : '1'}" title="${it.pinned ? t('取消 .keep 保护', 'Unpin .keep') : t('锁定 .keep 保护', 'Pin .keep')}" ${busyKeep ? 'disabled' : ''}>${icon(it.pinned ? 'lock' : 'unlock', 'ico-xs')}</button>` : ''}
            </div>
          </div>
        </div>`
    })
    .join('')
}

function updateStorageListInPlace() {
  if (!state.snapshot) return
  const listEl = document.getElementById('storage-list')
  if (listEl) listEl.innerHTML = renderStorageItemsHtml(state.snapshot)
  const clearBtn = document.getElementById('storage-search-clear')
  if (clearBtn) clearBtn.style.display = state.storageSearch ? 'inline-flex' : 'none'
  document.querySelectorAll('[data-action="storage-filter"]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.filter === state.storageFilter)
  })
}

function renderSpaceView(snap) {
  const busyStorage = hasBusy(snap, 'refresh:storage')
  const busyRestore = hasBusy(snap, 'restore')
  const busyOrca = hasBusy(snap, 'orca-cleanup')
  const busyArchive = hasBusy(snap, 'archive')
  const st = snap.storage
  const gov = snap.cleanup.governor
  const orcaC = snap.cleanup.orca

  let inner = ''

  if (state.spaceSub === 'offenders') {
    const filters = [
      ['all', t('全部', 'All')],
      ['legacy_work', t('历史', 'Legacy')],
      ['stranded_worktree', t('滞留', 'Stranded')],
      ['worktree', t('工作树', 'Worktree')],
      ['shared_data', 'AI/Data'],
      ['derived_data', 'Derived'],
      ['gc_candidate', t('可清', 'GC')]
    ]

    inner = `
      <div class="card">
        <div class="card-head">
          <span class="card-label">${icon('layers', 'ico-sm')}${t('空间构成', 'Breakdown')}</span>
          <button class="icon-btn" data-action="refresh" data-scope="storage" title="${t('深度重扫大户 (Triage/DU)', 'Deep rescan')}" ${busyStorage ? 'disabled' : ''}>
            ${icon('refresh', `ico-sm ${busyStorage ? 'spin' : ''}`)}
          </button>
        </div>
        ${st.categoryBreakdown.length ? renderSpectrum(st.categoryBreakdown) : `<div class="xs muted">${t('正在后台扫描空间分布…', 'Scanning storage distribution…')}</div>`}
      </div>

      <div class="card">
        <div class="search-box">
          ${icon('search', 'ico-sm search-ico')}
          <input id="storage-search" type="text" value="${esc(state.storageSearch)}" placeholder="${t('筛选项目或目录…', 'Filter projects or paths…')}" />
          <button id="storage-search-clear" class="icon-btn search-clear" data-action="clear-search" title="${t('清空筛选', 'Clear')}" style="display:${state.storageSearch ? 'inline-flex' : 'none'}">${icon('x', 'ico-xs')}</button>
        </div>
        <div class="filter-scroll">
          ${filters.map(([k, lbl]) => `<button class="chip ${state.storageFilter === k ? 'active' : ''}" data-action="storage-filter" data-filter="${k}">${lbl}</button>`).join('')}
        </div>
        <div id="storage-list" class="list">${renderStorageItemsHtml(snap)}</div>
      </div>`
  } else if (state.spaceSub === 'history') {
    const batches = gov.recentBatches || []
    const runs = gov.historyRuns || []

    inner = `
      <div class="card">
        <div class="card-head">
          <span class="card-label">${icon('trash', 'ico-sm')}${t('废纸篓收据（可一键撤销）', 'Trash Receipts & Undo')}</span>
          <span class="xxs muted num">${batches.length} ${t('批', 'batches')}</span>
        </div>
        ${batches.length === 0
          ? `<div class="empty-line">${t('暂无废纸篓批次；下方仍保留完整定时清理流水', 'No Trash batches yet; scheduled runs are below.')}</div>`
          : `<div class="list">
              ${batches
                .map((b) => {
                  const open = state.expandedBatchId === b.id
                  const arm = state.confirmRestorePath === b.manifestPath
                  return `
                    <div class="alert-item">
                      <div class="alert-item-head" data-action="toggle-batch" data-batch="${esc(b.id)}">
                        <div class="row-left">
                          ${icon('chevronRight', `ico-xs chev ${open ? 'rot' : ''}`)}
                          <span class="row-title num">${esc(formatClock(b.createdAt))}</span>
                          <span class="tag ${b.restoredAt ? '' : b.inTrash ? 'ok' : ''}">${b.restoredAt ? t('已还原', 'Restored') : b.inTrash ? t('可撤销', 'Trash') : t('已清空', 'Emptied')}</span>
                        </div>
                        <strong class="mono xs">${esc(formatBytes(b.totalBytes))}</strong>
                      </div>
                      <div class="accordion ${open ? 'open' : ''}" data-accordion="batch:${esc(b.id)}">
                        <div class="accordion-inner">
                          <div style="padding:0 8px 8px;display:flex;flex-direction:column;gap:6px">
                            ${b.largestItem ? `<div class="xxs muted truncate">${t('最大项', 'Largest')}: <strong style="color:var(--orca-foreground)">${esc(b.largestItem.originalPath.split('/').pop())}</strong> (${esc(formatBytes(b.largestItem.sizeBytes))})</div>` : ''}
                            ${b.inTrash && !b.restoredAt ? `
                              <div style="display:flex;gap:5px;justify-content:flex-end">
                                ${arm
                                  ? `<button class="pill-btn primary" data-action="restore-confirm" data-manifest="${esc(b.manifestPath)}" ${busyRestore ? 'disabled' : ''}>${t('确认还原', 'Confirm')}</button>
                                     <button class="pill-btn" data-action="restore-cancel">${t('取消', 'Cancel')}</button>`
                                  : `<button class="pill-btn warn-solid" data-action="restore-arm" data-manifest="${esc(b.manifestPath)}" ${busyRestore ? 'disabled' : ''}>${icon('undo', 'ico-xs')}${t('撤销还原', 'Undo')}</button>`}
                              </div>` : ''}
                            <div class="list">
                              ${b.items.slice(0, 12).map((it) => `
                                <div class="row-sub">
                                  <span class="truncate">${esc(it.originalPath.split('/').pop())}</span>
                                  <span class="mono">${esc(formatBytes(it.sizeBytes))}</span>
                                </div>`).join('')}
                            </div>
                          </div>
                        </div>
                      </div>
                    </div>`
                })
                .join('')}
            </div>`}
      </div>

      <div class="card">
        <div class="card-head">
          <span class="card-label">${icon('activity', 'ico-sm')}${t('定时清理流水', 'Scheduled GC Timeline')}</span>
          <span class="xxs muted">agent-hygiene</span>
        </div>
        ${runs.length === 0
          ? `<div class="empty-line">${t('暂无日志记录', 'No log entries')}</div>`
          : `<div class="timeline">
              ${runs.slice(0, 12).map((r) => `
                <div class="tl-row">
                  <span class="live-dot ${r.state === 'fail' ? 'danger' : r.state === 'warn' ? 'warn' : 'ok'}"></span>
                  <div style="min-width:0">
                    <div class="xs num" style="font-weight:560">${esc(formatClock(r.at))} <span class="muted">· ${r.reclaimedItems} ${t('项', 'items')}</span></div>
                    <div class="xxs muted truncate">${r.state === 'fail' && r.reclaimedItems === 0 ? esc(r.raw) : `${t('保护跳过', 'Protected')} ${r.protectedSkips}${r.ghostApps ? ` · ${r.ghostApps} ghost` : ''}`}</div>
                  </div>
                  <strong class="mono xs">${esc(formatBytes(r.reclaimedBytes))}</strong>
                </div>`).join('')}
            </div>`}
      </div>`
  } else {
    // Sub-view C: Worktree Archive & Orca Cleanup
    const mergeable = [...(orcaC.preview?.rows || []), ...(orcaC.lastRun?.rows || [])].filter(
      (r, i, arr) => r.path && (r.reason === 'merged' || r.outcome !== 'kept') && arr.findIndex((x) => x.path === r.path) === i
    )
    const archives = snap.archiveHistory || []

    inner = `
      <div class="card">
        <div class="card-head">
          <span class="card-label">${icon('archive', 'ico-sm')}${t('安全归档工作树', 'Safe Archive')}</span>
          <div class="row-actions">
            <button class="icon-btn" data-action="orca-cleanup" data-apply="0" title="${t('预览 Orca 清理规则', 'Preview rules')}" ${busyOrca ? 'disabled' : ''}>${icon('eye', 'ico-xs')}</button>
            <button class="pill-btn warn-solid" data-action="orca-cleanup" data-apply="1" ${busyOrca ? 'disabled' : ''}>${t('清理已合并', 'Cleanup')}</button>
          </div>
        </div>
        ${mergeable.length === 0
          ? `<div class="empty-line">${t('暂无已合并待退役工作树；也可在「大户排行」对任意任务工作树点归档图标', 'No merged worktrees flagged; archive any task worktree from Storage.')}</div>`
          : `<div class="list">
              ${mergeable.slice(0, 10).map((r) => `
                <div class="row-card">
                  <div class="row-top">
                    <div class="row-left">
                      <span class="tag accent">${esc(r.reason || r.outcome)}</span>
                      <span class="row-title">${esc(r.displayName || r.branch)}</span>
                    </div>
                    <button class="pill-btn primary" data-action="archive-wt" data-path="${esc(r.path)}" data-topic="${esc(r.branch || r.displayName)}" ${busyArchive ? 'disabled' : ''}>${icon('archive', 'ico-xs')}${t('归档', 'Archive')}</button>
                  </div>
                  <div class="row-sub">
                    <span class="truncate">${esc(r.repoName)} · ${esc(shortPath(r.path))}</span>
                    ${r.sizeBytes ? `<span class="mono">${esc(formatBytes(r.sizeBytes))}</span>` : ''}
                  </div>
                </div>`).join('')}
            </div>`}
        ${archives.length > 0 ? `
          <div class="divider"></div>
          <div class="list">
            ${archives.slice(0, 5).map((a) => `
              <div class="row-sub">
                <span class="truncate">${a.ok ? '✓' : '!'} ${esc(a.topic)} → ${esc(shortPath(a.archiveDir))}</span>
                <span class="mono">${esc(formatBytes(a.totalBytes))}</span>
              </div>`).join('')}
          </div>` : ''}
      </div>`
  }

  return `
    <div class="sub-seg">
      ${[
        ['offenders', t('大户排行', 'Top Space')],
        ['history', t('清理历史', 'History')],
        ['archive', t('工作树归档', 'Archive')]
      ]
        .map(([id, lbl]) => `<button class="sub-seg-btn ${state.spaceSub === id ? 'active' : ''}" data-action="space-sub" data-sub="${id}">${lbl}</button>`)
        .join('')}
    </div>
    ${inner}`
}

/* ────────────────── View 3: Live, Bots & Health ────────────────── */

function renderLiveView(snap) {
  const { bots, workspaces, memory, testDevices } = snap.live
  const { jobs, doctor, audit } = snap.health
  const busyBot = hasBusy(snap, 'bot-send')
  const busyMem = hasBusy(snap, 'refresh:memory')
  const busyHealth = hasBusy(snap, 'refresh:health')
  const bot = bots[0] || null
  const botName = snap.settings?.botName || bot?.name || 'Ram'
  const botMode = snap.settings?.botAlertMode || 'critical'
  const lastReply = bot?.chats?.[0]?.lastSaid || ''

  const visibleWs = state.wsFilter === 'active'
    ? workspaces.filter((w) => w.state !== 'inactive')
    : workspaces

  const wsDot = (w) => {
    if (w.state === 'waiting') return 'warn pulse'
    if (w.state === 'blocked') return 'danger pulse'
    if (w.state === 'working') return 'ok pulse'
    if (w.state === 'done' || w.state === 'active') return 'accent'
    return 'muted'
  }

  const wsLabel = (w) => {
    if (w.state === 'waiting') return t(`等你 ${w.waitingMinutes ?? 0}m`, `Wait ${w.waitingMinutes ?? 0}m`)
    if (w.state === 'blocked') return t('阻塞', 'Blocked')
    if (w.state === 'working') return t('运行中', 'Working')
    if (w.state === 'done') return t('完成', 'Done')
    if (w.state === 'active') return t('活跃', 'Active')
    return t('空闲', 'Idle')
  }

  const sortedJobs = [...jobs].sort((a, b) => {
    const rank = (s) => (s === 'fail' || s === 'stale' || s === 'absent' ? 0 : s === 'warn' ? 1 : 2)
    return rank(a.state) - rank(b.state)
  })
  const expandedJob = sortedJobs.find((j) => j.name === state.expandedJobName) || null
  const totalTok = (audit.globalTokens || 0) + (audit.skillsTokens || 0)

  return `
    <div class="card bot-card">
      <div class="card-head">
        <div class="row-left">
          <span class="live-dot ${bot?.paused ? 'warn' : 'ok pulse'}"></span>
          <strong style="font-size:12.5px">${esc(botName)}</strong>
          ${bot ? `<span class="tag accent">${esc(bot.agent)}</span>` : ''}
        </div>
        <div class="mode-pills" title="${t('异常自动交给 Bot 策略：紧急自动 / 全部自动 / 仅手动', 'Auto Bot handoff mode')}">
          ${[
            ['critical', t('紧急', 'Crit')],
            ['all', t('自动', 'All')],
            ['manual', t('手动', 'Manual')]
          ]
            .map(([m, lbl]) => `<button class="mode-pill ${botMode === m ? 'active' : ''}" data-action="bot-mode" data-mode="${m}">${lbl}</button>`)
            .join('')}
        </div>
      </div>

      ${lastReply ? `<div class="bot-quote">${esc(lastReply)}</div>` : ''}

      <div class="bot-compose">
        <input id="bot-note-input" type="text" value="${esc(state.botNote)}" placeholder="${t(`让 ${botName} 结合项目与清理现状反馈…`, `Ask ${botName} to inspect…`)}" />
        <button class="icon-btn" data-action="escalate-custom" title="${t(`发送给 ${botName}`, `Send to ${botName}`)}" ${busyBot ? 'disabled' : ''} style="color:var(--ops-accent)">${icon('send', 'ico-sm')}</button>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-label">${icon('activity', 'ico-sm')}${t(`实时工作区 (${snap.live.totalWorkspaceCount ?? workspaces.length})`, `Live (${snap.live.totalWorkspaceCount ?? workspaces.length})`)}</span>
        <div class="row-actions">
          <div class="mode-pills">
            <button class="mode-pill ${state.wsFilter === 'active' ? 'active' : ''}" data-action="ws-filter" data-filter="active">${t('活跃', 'Active')}</button>
            <button class="mode-pill ${state.wsFilter === 'all' ? 'active' : ''}" data-action="ws-filter" data-filter="all">${t('全部', 'All')}</button>
          </div>
          <button id="ops-mem-btn" class="icon-btn ${state.showMemory ? 'active-btn' : ''}" data-action="toggle-memory" title="${t('采样进程内存/CPU', 'Sample Memory/CPU')}" ${busyMem ? 'disabled' : ''}>${icon('cpu', 'ico-sm')}</button>
        </div>
      </div>

      <div class="accordion ${state.showMemory ? 'open' : ''}" data-accordion="memory">
        <div class="accordion-inner">
          <div style="padding-bottom:8px;display:flex;flex-direction:column;gap:6px">
            ${memory ? `
              <div class="row-sub num">
                <span>${t('总内存', 'Total')} <strong>${esc(formatBytes(memory.totalMemoryBytes))}</strong> · ${memory.totalCpuPct.toFixed(1)}% CPU</span>
                <span>Orca ${esc(formatBytes(memory.appMemoryBytes))}</span>
              </div>
              <div class="list">
                ${memory.worktrees.slice(0, 6).map((w) => {
                  const pct = Math.max(4, Math.round((w.memoryBytes / Math.max(1, memory.worktrees[0]?.memoryBytes || 1)) * 100))
                  return `
                    <div class="row-card interactive" data-action="open-ws" data-ws="${esc(w.workspaceId || w.worktreeId)}">
                      <div class="row-top">
                        <span class="row-title">${esc(w.worktreeName || w.repoName)}</span>
                        <span class="mono xxs">${esc(formatBytes(w.memoryBytes))} · ${w.cpuPct.toFixed(0)}%</span>
                      </div>
                      <div class="thin-bar"><div style="width:${pct}%;background:var(--ops-teal)"></div></div>
                    </div>`
                }).join('')}
              </div>` : `<div class="xs muted">${t('正在采样进程资源…', 'Sampling processes…')}</div>`}
          </div>
        </div>
      </div>

      ${visibleWs.length === 0
        ? `<div class="empty-line">${t('当前没有运行中或等待确认的工作区', 'No active workspaces right now')}</div>`
        : `<div class="list">
            ${visibleWs.slice(0, 20).map((ws) => `
              <div class="row-card interactive" data-action="open-ws" data-ws="${esc(ws.id)}">
                <div class="row-top">
                  <div class="row-left">
                    <span class="live-dot ${wsDot(ws)}"></span>
                    <span class="row-title" title="${esc(ws.name)}">${esc(ws.name)}</span>
                  </div>
                  <span class="tag ${ws.state === 'waiting' ? 'warn' : ws.state === 'blocked' ? 'danger' : ws.state === 'working' ? 'ok' : ''}">${esc(wsLabel(ws))}</span>
                </div>
                <div class="row-sub">
                  <span class="truncate">${esc(ws.project || ws.kind)}${ws.agentType ? ` · ${esc(ws.agentType)}` : ''}${ws.linkedPR ? ` · ${esc(ws.linkedPR)}` : ''}</span>
                  <span class="num">${ws.memoryBytes ? `${esc(formatBytes(ws.memoryBytes))} · ` : ''}${esc(formatRelative(ws.lastActivityAt))}</span>
                </div>
              </div>`).join('')}
          </div>`}
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-label">${icon('shield', 'ico-sm')}${t('系统守卫与上下文预算', 'Health & Context Budget')}</span>
        <div class="row-actions">
          <span class="tag ${doctor.ok === true ? 'ok' : doctor.ok === false ? 'danger' : ''}">Doctor ${doctor.ok === true ? 'OK' : doctor.ok === false ? `${doctor.attention.length}` : '…'}</span>
          <button class="icon-btn" data-action="refresh" data-scope="health" title="${t('重新体检', 'Re-run health check')}" ${busyHealth ? 'disabled' : ''}>${icon('refresh', `ico-xs ${busyHealth ? 'spin' : ''}`)}</button>
        </div>
      </div>

      <div class="job-matrix">
        ${sortedJobs.map((j) => {
          const cls = j.state === 'fail' || j.state === 'stale' || j.state === 'absent' ? 'danger' : j.state === 'warn' ? 'warn' : ''
          const sel = state.expandedJobName === j.name ? 'selected' : ''
          return `
            <div class="job-pill ${cls} ${sel}" data-action="toggle-job" data-job="${esc(j.name)}" title="${esc(j.message)}">
              <span class="job-pill-name">${esc(j.name)}</span>
              <span class="live-dot ${cls || 'ok'}"></span>
            </div>`
        }).join('')}
      </div>

      <div class="accordion ${expandedJob ? 'open' : ''}" data-accordion="job">
        <div class="accordion-inner" id="ops-job-detail">
          ${expandedJob ? renderJobDetailHtml(expandedJob) : ''}
        </div>
      </div>

      <div class="divider"></div>

      <div class="row-top" style="cursor:pointer" data-action="toggle-audit">
        <span class="xs muted">${t('启动 Token', 'Startup Tax')} <strong class="num" style="color:var(--orca-foreground)">~${totalTok.toLocaleString()}</strong> <span class="xxs">(${t('规则', 'Rules')} ${audit.globalTokens} · Skills ${audit.skillsTokens} · MCP ${audit.mcpClaude}/${audit.mcpCodex})</span></span>
        ${icon('chevronRight', `ico-xs chev ${state.showAuditDetails ? 'rot' : ''}`)}
      </div>
      <div class="xxs muted">
        ${t('临时设备', 'Devices')}: ${testDevices.activeLeases.length} ${t('租约', 'leases')} · ${testDevices.ephemeralSimulators.length} ${t('残留模拟器', 'sims')} · ${testDevices.runningQemu.length} QEMU
      </div>

      <div class="accordion ${state.showAuditDetails ? 'open' : ''}" data-accordion="audit">
        <div class="accordion-inner">
          <div class="list" style="padding-top:4px">
            ${audit.attention.map((x) => `<div class="xxs" style="color:var(--ops-warn)">• ${esc(x)}</div>`).join('')}
            ${audit.flaggedProjects.slice(0, 6).map((p) => `
              <div class="row-sub">
                <span>${esc(p.name)} (${p.agentsLines}L)</span>
                <span class="truncate">${esc([...p.issues, ...p.smells].join(', '))}</span>
              </div>`).join('')}
          </div>
        </div>
      </div>
    </div>`
}

function renderJobDetailHtml(job) {
  return `
    <div class="row-card" style="margin-top:2px">
      <div class="row-top">
        <strong class="xs">${esc(job.name)} · ${esc(job.state)}</strong>
        <span class="xxs mono muted">${esc(job.time)}</span>
      </div>
      <div class="xxs muted">${esc(job.message || '—')}</div>
    </div>`
}

/* ────────────────── Persistent Shell & Smooth Render ────────────────── */

function ensureShell() {
  if (shellMounted && document.getElementById('ops-view')) return
  appEl.innerHTML = `
    <div id="ops-header" class="topbar">
      <div class="brand">
        <span id="ops-brand-mark" class="brand-mark">${icon('radar', 'ico-sm')}</span>
        <span id="ops-brand-title" class="brand-title">${t('运维雷达', 'Ops Radar')}</span>
        <span id="ops-updated" class="xxs muted num"></span>
      </div>
      <button id="ops-refresh-btn" class="icon-btn" data-action="refresh" data-scope="all" title="${t('立即刷新', 'Refresh')}">
        ${icon('refresh', 'ico-sm')}
      </button>
    </div>
    <div id="ops-nav" class="seg-nav" role="tablist">
      <div id="ops-seg-thumb" class="seg-thumb"></div>
      <button class="seg-btn" data-action="tab" data-tab="overview" role="tab">
        <span id="ops-tab-lbl-overview">${t('概览', 'Now')}</span>
      </button>
      <button class="seg-btn" data-action="tab" data-tab="space" role="tab">
        <span id="ops-tab-lbl-space">${t('空间清理', 'Space')}</span>
        <span id="ops-badge-space" class="seg-badge accent" style="display:none"></span>
      </button>
      <button class="seg-btn" data-action="tab" data-tab="live" role="tab">
        <span id="ops-tab-lbl-live">${t('实时监控', 'Live')}</span>
        <span id="ops-badge-live" class="seg-badge" style="display:none"></span>
      </button>
    </div>
    <div id="ops-view" class="view-stack"></div>
    <div id="ops-toast-wrap" class="toast-wrap"></div>
  `
  shellMounted = true
}

function renderToast() {
  const wrap = document.getElementById('ops-toast-wrap')
  if (!wrap) return
  if (!state.toast) {
    wrap.innerHTML = ''
    return
  }
  const tIcon = state.toast.tone === 'success' ? 'check' : state.toast.tone === 'danger' ? 'alert' : 'sparkles'
  wrap.innerHTML = `
    <div class="toast ${esc(state.toast.tone)}">
      ${icon(tIcon, 'ico-xs')}
      <span class="truncate">${esc(state.toast.message)}</span>
      <button class="icon-btn" data-action="close-toast" style="width:18px;height:18px">${icon('x', 'ico-xs')}</button>
    </div>`
}

function animateOverviewRing() {
  const arc = document.querySelector('#ops-view .ring-arc')
  if (!arc) return
  const full = arc.getAttribute('data-dash-full')
  const target = arc.getAttribute('data-dash-target')
  if (!full || !target) return
  arc.style.transition = 'none'
  arc.setAttribute('stroke-dashoffset', full)
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        arc.style.transition = ''
        arc.setAttribute('stroke-dashoffset', target)
      })
    })
  } else {
    arc.style.transition = ''
    arc.setAttribute('stroke-dashoffset', target)
  }
}

function render() {
  const snap = state.snapshot
  if (!snap) {
    appEl.innerHTML = `
      <div class="boot-shell">
        <div class="boot-ring"></div>
        <div class="muted xs">${esc(state.localError || t('正在连接运维雷达…', 'Connecting Ops Radar…'))}</div>
      </div>`
    shellMounted = false
    return
  }

  ensureShell()
  const prevScrollY = typeof window.scrollY === 'number' ? window.scrollY : 0
  const busyRefresh = hasBusy(snap, 'refresh:', true)
  const alertCount = snap.counts.criticalAlerts + snap.counts.warnAlerts
  const markTone = snap.counts.criticalAlerts > 0 ? 'danger' : alertCount > 0 ? 'warn' : ''

  // 1. Update persistent header in-place (keeps refresh spin smooth across ticks)
  const markEl = document.getElementById('ops-brand-mark')
  if (markEl) markEl.className = `brand-mark ${markTone}`
  const titleEl = document.getElementById('ops-brand-title')
  if (titleEl) titleEl.textContent = t('运维雷达', 'Ops Radar')
  const updatedEl = document.getElementById('ops-updated')
  if (updatedEl) updatedEl.textContent = formatRelative(snap.generatedAt)
  const refreshBtn = document.getElementById('ops-refresh-btn')
  if (refreshBtn) {
    refreshBtn.disabled = busyRefresh
    const svg = refreshBtn.querySelector('svg')
    if (svg) svg.classList.toggle('spin', busyRefresh)
  }

  // 2. Update persistent gliding segmented nav in-place (enables 60fps transform glide)
  const tabIndex = state.tab === 'overview' ? 0 : state.tab === 'space' ? 1 : 2
  const thumbEl = document.getElementById('ops-seg-thumb')
  if (thumbEl) thumbEl.style.transform = `translateX(${tabIndex * 100}%)`

  const lblOverview = document.getElementById('ops-tab-lbl-overview')
  if (lblOverview) lblOverview.textContent = t('概览', 'Now')
  const lblSpace = document.getElementById('ops-tab-lbl-space')
  if (lblSpace) lblSpace.textContent = t('空间清理', 'Space')
  const lblLive = document.getElementById('ops-tab-lbl-live')
  if (lblLive) lblLive.textContent = t('实时监控', 'Live')

  document.querySelectorAll('#ops-nav .seg-btn').forEach((btn) => {
    const active = btn.dataset.tab === state.tab
    btn.classList.toggle('active', active)
    btn.setAttribute('aria-selected', active ? 'true' : 'false')
  })

  const badgeSpace = document.getElementById('ops-badge-space')
  if (badgeSpace) {
    badgeSpace.style.display = snap.counts.trashBatches > 0 ? 'inline-flex' : 'none'
    badgeSpace.textContent = String(snap.counts.trashBatches || '')
  }

  const liveBadge = snap.counts.waitingAgents + alertCount
  const badgeLive = document.getElementById('ops-badge-live')
  if (badgeLive) {
    badgeLive.style.display = liveBadge > 0 ? 'inline-flex' : 'none'
    badgeLive.className = `seg-badge ${snap.counts.criticalAlerts > 0 ? 'danger' : ''}`
    badgeLive.textContent = String(liveBadge || '')
  }

  // 3. Active View (animate ONLY when the user switched views, never on background data ticks)
  const viewKey = `${state.tab}:${state.tab === 'space' ? state.spaceSub : ''}`
  const viewChanged = viewKey !== state.lastRenderedView
  state.lastRenderedView = viewKey

  const viewEl = document.getElementById('ops-view')
  if (state.tab === 'overview') viewEl.innerHTML = renderOverviewView(snap)
  else if (state.tab === 'space') viewEl.innerHTML = renderSpaceView(snap)
  else viewEl.innerHTML = renderLiveView(snap)

  if (viewChanged) {
    viewEl.classList.remove('view-enter')
    void viewEl.offsetWidth
    viewEl.classList.add('view-enter')
    if (state.tab === 'overview') animateOverviewRing()
  }

  renderToast()

  if (!viewChanged && prevScrollY > 0 && typeof window.scrollTo === 'function') {
    window.scrollTo(0, prevScrollY)
  }
}

/* ────────────────── Interactions & Events ────────────────── */

appEl.addEventListener('click', (ev) => {
  const target = ev.target.closest('[data-action]')
  if (!target) return
  const action = target.dataset.action

  if (action === 'tab') {
    state.tab = target.dataset.tab
    render()
    return
  }
  if (action === 'goto-space') {
    state.tab = 'space'
    state.spaceSub = 'offenders'
    render()
    return
  }
  if (action === 'space-sub') {
    state.spaceSub = target.dataset.sub
    render()
    return
  }
  if (action === 'gc-mode') {
    state.gcMode = target.dataset.mode
    document.querySelectorAll('[data-action="gc-mode"]').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.mode === state.gcMode)
    })
    const hintEl = document.getElementById('gc-mode-hint')
    if (hintEl) hintEl.textContent = t(...(GC_MODE_HINTS[state.gcMode] || GC_MODE_HINTS.daily))
    return
  }
  if (action === 'gc') {
    void req('ops.runGc', { mode: state.gcMode, apply: target.dataset.apply === '1' }, 110_000)
    return
  }
  if (action === 'refresh') {
    void req('ops.refresh', { scope: target.dataset.scope || 'all' })
    return
  }
  if (action === 'toggle-alert') {
    const id = target.dataset.alert
    state.expandedAlertId = state.expandedAlertId === id ? null : id
    document.querySelectorAll('[data-accordion^="alert:"]').forEach((el) => {
      el.classList.toggle('open', el.dataset.accordion === `alert:${state.expandedAlertId}`)
    })
    return
  }
  if (action === 'toggle-batch') {
    const id = target.dataset.batch
    state.expandedBatchId = state.expandedBatchId === id ? null : id
    state.confirmRestorePath = null
    document.querySelectorAll('[data-accordion^="batch:"]').forEach((el) => {
      el.classList.toggle('open', el.dataset.accordion === `batch:${state.expandedBatchId}`)
    })
    document.querySelectorAll('[data-action="toggle-batch"]').forEach((head) => {
      const chev = head.querySelector('.chev')
      if (chev) chev.classList.toggle('rot', head.dataset.batch === state.expandedBatchId)
    })
    return
  }
  if (action === 'toggle-job') {
    const name = target.dataset.job
    state.expandedJobName = state.expandedJobName === name ? null : name
    document.querySelectorAll('.job-pill').forEach((pill) => {
      pill.classList.toggle('selected', pill.dataset.job === state.expandedJobName)
    })
    const acc = document.querySelector('[data-accordion="job"]')
    const detail = document.getElementById('ops-job-detail')
    const job = (state.snapshot?.health?.jobs || []).find((j) => j.name === state.expandedJobName)
    if (job && detail) detail.innerHTML = renderJobDetailHtml(job)
    if (acc) acc.classList.toggle('open', Boolean(job))
    return
  }
  if (action === 'toggle-audit') {
    state.showAuditDetails = !state.showAuditDetails
    const acc = document.querySelector('[data-accordion="audit"]')
    if (acc) acc.classList.toggle('open', state.showAuditDetails)
    const chev = target.querySelector('.chev')
    if (chev) chev.classList.toggle('rot', state.showAuditDetails)
    return
  }
  if (action === 'toggle-memory') {
    state.showMemory = !state.showMemory
    const acc = document.querySelector('[data-accordion="memory"]')
    if (acc) acc.classList.toggle('open', state.showMemory)
    target.classList.toggle('active-btn', state.showMemory)
    if (state.showMemory) void req('ops.refresh', { scope: 'memory' })
    return
  }
  if (action === 'storage-filter') {
    state.storageFilter = target.dataset.filter
    updateStorageListInPlace()
    return
  }
  if (action === 'clear-search') {
    state.storageSearch = ''
    const inp = document.getElementById('storage-search')
    if (inp) {
      inp.value = ''
      inp.focus()
    }
    updateStorageListInPlace()
    return
  }
  if (action === 'ws-filter') {
    state.wsFilter = target.dataset.filter
    render()
    return
  }
  if (action === 'copy-path') {
    ev.stopPropagation()
    const p = target.dataset.path || ''
    if (p) {
      void copyTextSafe(p).then((ok) => {
        showToast(ok ? 'success' : 'warning', ok ? t('已复制路径', 'Path copied') : t('复制失败', 'Copy failed'))
      })
    }
    return
  }
  if (action === 'toggle-keep') {
    ev.stopPropagation()
    void req('ops.toggleKeep', { path: target.dataset.path, keep: target.dataset.keep === '1' })
    return
  }
  if (action === 'archive-wt') {
    ev.stopPropagation()
    void req('ops.archiveWorktree', { path: target.dataset.path, topic: target.dataset.topic || undefined }, 120_000)
    return
  }
  if (action === 'open-ws') {
    ev.stopPropagation()
    void req('ops.openWorkspace', { workspaceId: target.dataset.ws })
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
  if (action === 'orca-cleanup') {
    void req('ops.runOrcaCleanup', { apply: target.dataset.apply === '1' })
    return
  }
  if (action === 'dismiss-alert') {
    ev.stopPropagation()
    void req('ops.dismissAlert', { fingerprint: target.dataset.fp })
    return
  }
  if (action === 'escalate-all') {
    void req('ops.escalateBot', { mode: 'handoff' })
    return
  }
  if (action === 'escalate-one') {
    ev.stopPropagation()
    void req('ops.escalateBot', { alertIds: [target.dataset.alert], mode: 'handoff' })
    return
  }
  if (action === 'escalate-custom') {
    const note = state.botNote
    state.botNote = ''
    const inp = document.getElementById('bot-note-input')
    if (inp) inp.value = ''
    void req('ops.escalateBot', { mode: 'handoff', customNote: note, newChat: state.botNewChat })
    return
  }
  if (action === 'bot-mode') {
    void req('ops.updateSetting', { key: 'botAlertMode', value: target.dataset.mode })
    return
  }
  if (action === 'close-toast') {
    dismissToast()
  }
})

appEl.addEventListener('keydown', (ev) => {
  if (ev.target.id === 'bot-note-input' && ev.key === 'Enter' && !state.composing) {
    ev.preventDefault()
    const note = state.botNote
    state.botNote = ''
    ev.target.value = ''
    void req('ops.escalateBot', { mode: 'handoff', customNote: note, newChat: state.botNewChat })
  }
})

appEl.addEventListener('compositionstart', () => {
  state.composing = true
})

appEl.addEventListener('compositionend', () => {
  state.composing = false
  if (document.activeElement?.id === 'storage-search') {
    state.storageSearch = document.activeElement.value
    updateStorageListInPlace()
  }
})

appEl.addEventListener('focusout', () => {
  setTimeout(flushPendingSnapshot, 60)
})

appEl.addEventListener('input', (ev) => {
  if (ev.target.id === 'storage-search') {
    state.storageSearch = ev.target.value
    if (state.composing) return
    updateStorageListInPlace()
  } else if (ev.target.id === 'bot-note-input') {
    state.botNote = ev.target.value
  }
})

if (window.orca) {
  window.orca.plugin.on('ops.changed', (next) => {
    if (isUserEditing()) {
      state.pendingSnapshot = next
      return
    }
    state.pendingSnapshot = null
    applySnapshot(next)
  })
  window.orca.on('theme', () => {
    if (!isUserEditing()) render()
  })
  setInterval(() => {
    if (state.snapshot && !isUserEditing()) render()
  }, 30_000)
  void req('ops.snapshot')
}
