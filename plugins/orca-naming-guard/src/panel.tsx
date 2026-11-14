// The Naming Guard sidebar panel. It only draws what src/worker.ts reports and sends the
// person's choices back with plugin.request; all renaming happens in the worker.
import {
  atom,
  Badge,
  Button,
  Callout,
  EmptyState,
  env,
  Heading,
  KeyValue,
  Markdown,
  plugin,
  read,
  Row,
  RelativeTime,
  set,
  Stack,
  Switch,
  Tab,
  Table,
  Tabs,
  Text,
  update,
  type NamingNotice,
  type NamingView,
  type PanelContext
} from 'orca'

const EN = {
  title: 'Naming Guard',
  status: { starting: 'Starting', active: 'On', paused: 'Paused', unsupported: 'Update Orca', error: 'Error' },
  auto: 'Rename new workspaces automatically',
  unsupported: 'This Orca cannot rename workspaces from plugins. Update to Orca 1.4.256 or later.',
  error: 'Could not read workspace names',
  actionFailed: 'That did not work',
  line: (n: number) => `line ${n}`,
  notice: {
    'rename-failed': (d?: string) => `A rename failed (${d}); it is tried again on the next check.`,
    'apply-failed': (d?: string) => `Some suggestions could not be applied (${d}).`,
    stale: () => 'Some suggestions changed since they were shown; check them again.',
    'already-undone': () => 'That rename was already undone.',
    gone: () => 'That workspace no longer exists.',
    'renamed-since': (d?: string) => `Not undone: the workspace was renamed to "${d}" since.`
  },
  aliasErrors: 'Some alias lines were skipped',
  aliasHint: 'Write each alias as "Repo name = Prefix" in Settings → Plugins → Workspace Naming Guard.',
  checked: 'Checked',
  waiting: (n: number) => `${n} waiting for a title`,
  renamedToday: 'Renamed (24 h)',
  suggestions: 'Suggestions',
  locked: 'Left as you named them',
  activity: 'Activity',
  how: 'How it works',
  colAfter: 'New name',
  colBefore: 'Was',
  colWhen: 'When',
  colCurrent: 'Now',
  colProposed: 'Suggested',
  noActivity: 'No renames yet. New workspaces are renamed once Orca gives them a title.',
  noSuggestions: 'Every existing workspace already follows Project｜Topic.',
  undo: 'Undo',
  apply: 'Apply',
  applyAll: 'Apply all',
  ignore: 'Ignore',
  check: 'Check now',
  settingsHint: 'Aliases and standby names: Settings → Plugins → Workspace Naming Guard.',
  undone: '(undone)',
  reasons: { 'agent-title': 'Agent title', branch: 'Branch', prefix: 'Prefix', standby: 'Standby', undo: 'Undo' },
  help: [
    '**New workspaces** are named `Project｜Topic` automatically:',
    '',
    "1. Orca names a workspace from your first message to its agent; Naming Guard adds the project prefix (`Fix auth token refresh` → `CloudNode｜Fix auth token refresh`).",
    '2. A worktree whose branch already says what it is for (`feature/login-page`) is named from the branch.',
    '3. Generic or wrong prefixes (`Agent｜…`, another project’s name, an alias) are replaced with the project’s own.',
    '',
    '**Older workspaces** (from before the plugin ran, or first seen more than 6 hours ago) are never renamed on their own: they show up under Suggestions.',
    '',
    '**Your renames are final.** After Naming Guard has named a workspace, rename it yourself (or press Undo or Ignore) and it is left alone from then on. A name typed when creating a workspace is treated like Orca’s title and gets the prefix once; while paused, nothing is renamed and new workspaces only get suggestions.',
    '',
    'It uses only Orca’s plugin API: no files, no programs, no network.'
  ].join('\n')
}

const ZH: typeof EN = {
  title: '命名守卫',
  status: { starting: '启动中', active: '运行中', paused: '已暂停', unsupported: '需更新 Orca', error: '出错' },
  auto: '自动命名新工作区',
  unsupported: '当前 Orca 版本不支持插件重命名工作区，请更新到 1.4.256 或更高版本。',
  error: '无法读取工作区名称',
  actionFailed: '操作未完成',
  line: (n: number) => `第 ${n} 行`,
  notice: {
    'rename-failed': (d?: string) => `有一次重命名失败（${d}），下次检查时会重试。`,
    'apply-failed': (d?: string) => `部分建议未能应用（${d}）。`,
    stale: () => '部分建议在显示后已变化，请重新检查。',
    'already-undone': () => '这次重命名已经撤销过了。',
    gone: () => '该工作区已不存在。',
    'renamed-since': (d?: string) => `未撤销：该工作区之后已被改名为“${d}”。`
  },
  aliasErrors: '部分别名行已跳过',
  aliasHint: '别名格式为“仓库名 = 前缀”，在 设置 → 插件 → Workspace Naming Guard 中填写。',
  checked: '上次检查',
  waiting: (n: number) => `${n} 个等待标题`,
  renamedToday: '24 小时内重命名',
  suggestions: '建议',
  locked: '保留你的命名',
  activity: '记录',
  how: '工作方式',
  colAfter: '新名称',
  colBefore: '原名称',
  colWhen: '时间',
  colCurrent: '当前',
  colProposed: '建议',
  noActivity: '还没有重命名。新工作区在 Orca 生成标题后会自动命名。',
  noSuggestions: '所有已有工作区都已是“项目｜主题”格式。',
  undo: '撤销',
  apply: '应用',
  applyAll: '全部应用',
  ignore: '忽略',
  check: '立即检查',
  settingsHint: '别名与待命名称：设置 → 插件 → Workspace Naming Guard。',
  undone: '（已撤销）',
  reasons: { 'agent-title': '智能体标题', branch: '分支', prefix: '前缀', standby: '待命', undo: '撤销' },
  help: [
    '**新工作区**会自动命名为 `项目｜主题`：',
    '',
    '1. Orca 根据你发给智能体的第一条消息为工作区起名，命名守卫补上项目前缀（`Fix auth token refresh` → `CloudNode｜Fix auth token refresh`）。',
    '2. 分支名本身已说明用途的工作树（如 `feature/login-page`）直接按分支命名。',
    '3. 通用或错误的前缀（`Agent｜…`、其他项目名、别名）会换成本项目前缀。',
    '',
    '**较早的工作区**（插件运行前已有，或首次出现已超过 6 小时）不会被自动改名，只出现在“建议”里。',
    '',
    '**你的改名是最终的。**命名守卫命名过的工作区，你手动改名（或点“撤销”“忽略”）后就不再改动。创建时手填的名字会像 Orca 标题一样补一次前缀；暂停期间不改任何名字，新工作区只出现在“建议”里。',
    '',
    '只使用 Orca 插件 API：不读文件、不运行程序、不联网。'
  ].join('\n')
}

/** Orca's UI language, read on each render so a language switch shows at once. */
const strings = () => (env.locale.toLowerCase().startsWith('zh') ? ZH : EN)

/** What the worker last reported; null until it answers. */
const view = atom<NamingView | null>(null)
/** A failed request's error code, shown until the next answer. */
const failure = atom<string | null>(null)
/** Requests still in flight; controls are disabled while any is. */
const busy = atom(0)
const pickedSuggestions = atom<string[]>([])
const pickedHistory = atom<string[]>([])
const tab = atom('tab', 'activity', { persist: 'plugin' })

async function send(task: () => Promise<NamingView>): Promise<void> {
  update(busy, (n) => n + 1)
  try {
    set(view, await task())
    set(failure, null)
  } catch (e) {
    set(failure, (e as { code?: string }).code ?? 'action_failed')
  } finally {
    update(busy, (n) => n - 1)
  }
}

const check = () => send(() => plugin.request('naming.check'))
const setEnabled = (enabled: boolean) => send(() => plugin.request('naming.setEnabled', { enabled }))
const apply = (ids: string[]) => {
  set(pickedSuggestions, [])
  // A long list of renames can outlast the default 30 s.
  return send(() => plugin.request('naming.apply', { ids }, { timeoutMs: 120_000 }))
}
const ignore = (ids: string[]) => {
  set(pickedSuggestions, [])
  return send(() => plugin.request('naming.ignore', { ids }))
}
const undo = (key: string) => {
  set(pickedHistory, [])
  return send(() => plugin.request('naming.undo', { key }))
}

export function setup(ctx: PanelContext): void {
  ctx.subscriptions.push(
    plugin.on('naming.changed', (next) => {
      set(view, next)
      set(failure, null)
    })
  )
  void send(() => plugin.request('naming.view'))
}

function noticeText(t: typeof EN, notice: NamingNotice): string {
  const word = t.notice[notice.code]
  return word ? word(notice.detail) : notice.code
}

const STATUS_TONE = { starting: 'neutral', active: 'success', paused: 'neutral', unsupported: 'warning', error: 'danger' } as const

export default function Panel() {
  const t = strings()
  const v = read(view)
  const isBusy = read(busy) > 0
  const error = read(failure)
  if (!v) {
    return (
      <Stack gap="md">
        <Heading>{t.title}</Heading>
        {error ? <Callout tone="danger" title={t.error}>{error}</Callout> : <Text muted>{t.status.starting}…</Text>}
      </Stack>
    )
  }

  const suggestionIds = v.suggestions.map((s) => s.id)
  const picked = read(pickedSuggestions).filter((id) => suggestionIds.includes(id))
  const pickedKey = read(pickedHistory)[0]
  const pickedEntry = v.history.find((h) => h.key === pickedKey && !h.undone && h.reason !== 'undo')

  return (
    <Stack gap="md">
      <Row justify="between">
        <Heading>{t.title}</Heading>
        <Badge tone={STATUS_TONE[v.status]}>{t.status[v.status]}</Badge>
      </Row>

      <Switch
        label={t.auto}
        checked={v.enabled}
        disabled={isBusy || v.status === 'unsupported'}
        onChange={(checked) => void setEnabled(checked)}
      />

      {v.status === 'unsupported' ? <Callout tone="warning">{t.unsupported}</Callout> : null}
      {error ? (
        <Callout tone="danger" title={t.actionFailed}>
          {error}
        </Callout>
      ) : v.status === 'error' ? (
        <Callout tone="danger" title={t.error}>
          {v.error}
        </Callout>
      ) : null}
      {v.aliasErrors.length ? (
        <Callout tone="warning" title={t.aliasErrors}>
          {v.aliasErrors.map((e) => `${t.line(e.line)}: ${e.text}`).join('; ')}. {t.aliasHint}
        </Callout>
      ) : null}
      {v.notice ? <Callout tone="accent">{noticeText(t, v.notice)}</Callout> : null}

      <Row justify="between">
        <Text muted size="xs">
          {t.checked} {v.checkedAt ? <RelativeTime at={v.checkedAt} format="short" /> : '—'}
          {v.counts.waiting ? ` · ${t.waiting(v.counts.waiting)}` : ''}
        </Text>
        <Button size="xs" variant="ghost" icon="refresh" disabled={isBusy} onPress={check}>
          {t.check}
        </Button>
      </Row>

      <KeyValue
        items={[
          { label: t.renamedToday, value: v.counts.renamedToday },
          { label: t.suggestions, value: v.suggestions.length, tone: v.suggestions.length ? 'accent' : undefined },
          { label: t.locked, value: v.counts.locked }
        ]}
      />

      <Tabs label={t.title} value={read(tab)} onChange={(value) => set(tab, value)}>
        <Tab value="activity" label={t.activity} icon="activity">
          <Stack gap="sm">
            <Table
              label={t.activity}
              columns={[
                { key: 'after', label: t.colAfter },
                { key: 'before', label: t.colBefore, hideBelow: 'md' },
                { key: 'when', label: t.colWhen, width: 'xs', align: 'end' }
              ]}
              rows={v.history.map((h) => ({
                key: h.key,
                disabled: h.undone || h.reason === 'undo',
                cells: {
                  after: h.undone ? `${h.after} ${t.undone}` : h.after,
                  before: `${h.before} · ${t.reasons[h.reason]}`,
                  when: { type: 'relative', at: h.at, format: 'short' }
                }
              }))}
              selectionMode="single"
              selected={pickedEntry ? [pickedEntry.key] : []}
              onSelectionChange={({ keys }) => set(pickedHistory, keys)}
              emptyText={t.noActivity}
              maxHeight="md"
            />
            <Row justify="end">
              <Button size="sm" variant="outline" disabled={!pickedEntry || isBusy} onPress={() => pickedEntry && undo(pickedEntry.key)}>
                {t.undo}
              </Button>
            </Row>
          </Stack>
        </Tab>

        <Tab value="suggestions" label={v.suggestions.length ? `${t.suggestions} (${v.suggestions.length})` : t.suggestions} icon="lightbulb">
          {v.suggestions.length === 0 ? (
            <EmptyState icon="check" title={t.noSuggestions} />
          ) : (
            <Stack gap="sm">
              <Table
                label={t.suggestions}
                columns={[
                  { key: 'proposed', label: t.colProposed },
                  { key: 'current', label: t.colCurrent, hideBelow: 'md' }
                ]}
                rows={v.suggestions.map((s) => ({
                  key: s.id,
                  cells: { proposed: s.proposed, current: `${s.current} · ${t.reasons[s.reason]}` }
                }))}
                selectionMode="multiple"
                selected={picked}
                onSelectionChange={({ keys }) => set(pickedSuggestions, keys)}
                onRowAction={({ key }) => void apply([key])}
                maxHeight="md"
              />
              <Row justify="end" gap="xs">
                <Button size="sm" variant="ghost" disabled={!picked.length || isBusy} onPress={() => ignore(picked)}>
                  {t.ignore}
                </Button>
                <Button
                  size="sm"
                  tone="accent"
                  icon="check"
                  disabled={isBusy}
                  onPress={() => apply(picked.length ? picked : suggestionIds)}
                >
                  {picked.length ? `${t.apply} (${picked.length})` : t.applyAll}
                </Button>
              </Row>
            </Stack>
          )}
        </Tab>

        <Tab value="how" label={t.how} icon="info">
          <Markdown content={t.help} />
        </Tab>
      </Tabs>

      <Text muted size="xs">
        {t.settingsHint}
      </Text>
    </Stack>
  )
}
