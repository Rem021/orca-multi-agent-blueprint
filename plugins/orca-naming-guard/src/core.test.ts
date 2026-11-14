import { assert, test } from 'orca/testing'
import {
  cleanTopic,
  isProjectRoot,
  normalize,
  parseAliases,
  planAll,
  projectPrefix,
  shortHash,
  topicFromBranch,
  type NameRecord,
  type Options,
  type Workspace
} from './core.ts'

const options: Options = { standbyNames: false, standbyLabel: '待命工作区', aliases: { 'Infra_v2': 'CloudNode' } }
const suffix = () => 'ff348d15'
const ws = (over: Partial<Workspace>): Workspace => ({
  id: 'w1',
  kind: 'folder',
  name: 'blobfish',
  project: 'Mac',
  path: '/Users/me/worktrees/w1',
  branch: null,
  hostId: 'local',
  ...over
})
/** Plans with every workspace auto-renameable except those in `baseline`. */
const plan = (list: Workspace[], opts: Partial<{ baseline: string[]; records: Record<string, NameRecord>; options: Options }> = {}) =>
  planAll(
    list,
    new Set(list.map((w) => w.id).filter((id) => !(opts.baseline ?? []).includes(id))),
    opts.records ?? {},
    opts.options ?? options,
    suffix
  )

test('normalize spaces Han and Latin text and tightens the separator', () => {
  assert.equal(normalize('  Mac | 相机Pro任务  '), 'Mac｜相机 Pro 任务')
  assert.equal(normalize('FCP调色'), 'FCP 调色')
})

test('aliases parse one per line and report bad lines', () => {
  const { aliases, errors } = parseAliases('# comment\nmy-api-v2 = API\n\nbroken line\nFCP调色 = FCP 调色\nx = a｜b')
  assert.deepEqual({ ...aliases }, { 'my-api-v2': 'API', 'FCP调色': 'FCP 调色' })
  assert.deepEqual(errors, [
    { line: 4, text: 'broken line' },
    { line: 6, text: 'x = a｜b' }
  ])
  const empty = parseAliases(undefined)
  assert.deepEqual([{ ...empty.aliases }, empty.errors], [{}, []])
})

test('project prefix uses the alias, else the head of the project name', () => {
  assert.equal(projectPrefix('Infra_v2', options.aliases), 'CloudNode')
  assert.equal(projectPrefix('FX3｜FX5 & FX6 系统研究', {}), 'FX3')
  assert.equal(projectPrefix('Orca', {}), 'Orca')
})

test('topics drop openers, attachments and trailing punctuation, and stay short', () => {
  assert.equal(cleanTopic('[Image #1] 请帮我修复登录页的报错！'), '修复登录页的报错')
  assert.equal(cleanTopic('Please help me fix the auth token refresh.'), 'fix the auth token refresh')
  assert.equal(cleanTopic('为什么这边还是有这么多报错，你们联合一起去调查一下问题所在并且给出修复方案'), '为什么这边还是有这么多报错 你们联合一起去调查一')
  assert.equal(cleanTopic('Refactor the entire workspace naming pipeline so it uses official APIs only'), 'Refactor the entire workspace naming')
  assert.equal(cleanTopic('blobfish'), '')
  assert.equal(cleanTopic('New chat'), '')
  assert.equal(cleanTopic('see https://example.com/x for details'), '')
})

test('branch topics come from the last segment, without a trailing date', () => {
  assert.equal(topicFromBranch('claude/ui-motion-redesign-20261113'), 'Ui motion redesign')
  assert.equal(topicFromBranch('refs/heads/feature/login-page'), 'Login page')
  assert.equal(topicFromBranch(null), '')
})

test('a new workspace waits on its slug, then takes Orca’s agent title with the project prefix', () => {
  const waiting = plan([ws({})])
  assert.deepEqual(waiting.auto, [])
  assert.deepEqual(waiting.waiting, ['w1'])

  const titled = plan([ws({ name: 'Fix auth token refresh', project: 'Infra_v2' })])
  assert.deepEqual(titled.auto, [
    { id: 'w1', before: 'Fix auth token refresh', after: 'CloudNode｜Fix auth token refresh', reason: 'agent-title', stage: 'final' }
  ])

  const chinese = plan([ws({ name: '多任务调度与缓存优化' })])
  assert.equal(chinese.auto[0].after, 'Mac｜多任务调度与缓存优化')
})

test('a worktree is named from a meaningful branch, never from a random one', () => {
  const login = plan([ws({ kind: 'worktree', name: 'feature/login-page', branch: 'feature/login-page' })])
  assert.equal(login.auto[0].after, 'Mac｜Login page')
  assert.equal(login.auto[0].reason, 'branch')
  const random = plan([ws({ kind: 'worktree', name: 'codex/mojarra', branch: 'codex/mojarra' })])
  assert.deepEqual(random.auto, [])
  assert.deepEqual(random.waiting, ['w1'])
})

test('generic, alias and other-project prefixes are replaced; deliberate ones are kept word for word', () => {
  const list = [
    ws({ id: 'a', name: 'Agent｜Fix Auth Token Refresh', project: 'Infra_v2' }),
    ws({ id: 'b', name: 'Infra_v2｜报错原因联合排查', project: 'Infra_v2' }),
    ws({ id: 'c', name: 'Mac｜项目缓存清理', project: 'Infra_v2' }),
    ws({ id: 'd', name: '随手记｜一些想法', project: 'Infra_v2' }),
    ws({ id: 'e', name: 'CloudNode｜A very long topic that should never be truncated by the plugin at all', project: 'Infra_v2' }),
    ws({ id: 'f', name: 'Mac｜kept', project: 'Mac' })
  ]
  const result = plan(list)
  assert.deepEqual(
    result.auto.map((c) => [c.id, c.after]),
    [
      ['a', 'CloudNode｜Fix Auth Token Refresh'],
      ['b', 'CloudNode｜报错原因联合排查'],
      ['c', 'CloudNode｜项目缓存清理']
    ]
  )
})

test('standby names are opt-in and only for new workspaces', () => {
  const on = { ...options, standbyNames: true }
  assert.equal(plan([ws({})], { options: on }).auto[0].after, 'Mac｜待命工作区 ff348d15')
  assert.deepEqual(plan([ws({})], { options: on, baseline: ['w1'] }).suggestions, [])
  const standby = plan([ws({ name: 'Mac｜待命工作区 ff348d15' })], {
    options: on,
    records: { w1: { name: 'Mac｜待命工作区 ff348d15', stage: 'standby', at: 1 } }
  })
  assert.deepEqual(standby.auto, [])
  // A standby name replaced Orca's title, so nothing is waited for.
  assert.deepEqual(standby.waiting, [])
})

test('workspaces from before the plugin started only get suggestions', () => {
  const result = plan([ws({ name: 'Fix auth token refresh' })], { baseline: ['w1'] })
  assert.deepEqual(result.auto, [])
  assert.equal(result.suggestions[0].after, 'Mac｜Fix auth token refresh')
})

test('a rename by a person locks the workspace, and locked or final ones are left alone', () => {
  const renamed = plan([ws({ name: 'My own name' })], { records: { w1: { name: 'Mac｜Fix auth', stage: 'final', at: 1 } } })
  assert.deepEqual(renamed.locks, ['w1'])
  assert.deepEqual(renamed.auto, [])
  const locked = plan([ws({ name: 'Agent｜Something else' })], {
    records: { w1: { name: 'Agent｜Something else', stage: 'final', at: 1, locked: true } }
  })
  assert.deepEqual([locked.auto, locked.locks, locked.suggestions], [[], [], []])
})

test('project checkouts and root folders are never renamed', () => {
  assert.equal(isProjectRoot(ws({ kind: 'worktree', name: 'main', branch: 'main' })), true)
  assert.equal(isProjectRoot(ws({ name: 'mac', project: 'Mac' })), true)
  assert.equal(isProjectRoot(ws({ kind: 'worktree', name: 'x', branch: 'codex/x' })), false)
  const result = plan([ws({ kind: 'worktree', name: 'Fix auth token refresh', branch: 'main' }), ws({ id: 'g', project: null })])
  assert.deepEqual([result.auto, result.waiting], [[], []])
})

test('standby ids are stable 8-digit hex', () => {
  assert.equal(shortHash('w1'), shortHash('w1'))
  assert.match(shortHash('identity:wt2:local:5768dc78'), /^[0-9a-f]{8}$/)
  assert.notEqual(shortHash('a'), shortHash('b'))
})

test('a half-width bar reads like the full-width one', () => {
  const result = plan([ws({ id: 'a', name: 'Agent | Fix flaky tests' }), ws({ id: 'b', name: 'API | Token refresh' })])
  assert.deepEqual(result.auto.map((c) => [c.id, c.after]), [['a', 'Mac｜Fix flaky tests']])
})

test('inherited object keys are never read as aliases or records', () => {
  assert.equal(projectPrefix('constructor', {}), 'constructor')
  assert.equal(projectPrefix('toString', parseAliases('').aliases), 'toString')
  const result = plan([ws({ name: 'constructor｜Some topic here', project: 'Mac' }), ws({ id: 'toString', name: 'blobfish' })])
  assert.deepEqual([result.auto, result.locks], [[], []])
})

test('titles in other scripts are named too', () => {
  assert.equal(plan([ws({ name: 'Исправить обновление токена' })]).auto[0].after, 'Mac｜Исправить обновление токена')
  assert.equal(plan([ws({ name: '토큰갱신수정' })]).auto[0].after, 'Mac｜토큰갱신수정')
  assert.deepEqual(plan([ws({ name: 'Исправить' })]).auto, [])
})

test('a checkout in the project’s own folder is never renamed, whatever its branch', () => {
  const checkout = ws({ kind: 'worktree', name: 'feature/login-page', branch: 'feature/login-page', path: '/Users/me/Projects/Mac' })
  assert.equal(isProjectRoot(checkout), true)
  assert.deepEqual(plan([checkout]).auto, [])
})
