// Runs src/worker.ts's activate(orca) over Orca's mock host, with workspace.list and
// workspace.rename backed by an in-memory sidebar.
import { assert, hostError, mockHost, mountWorker, test } from 'orca/testing'
import type { JsonValue } from 'orca'
import * as main from './worker.ts'
import type { Workspace } from './core.ts'

const ws = (id: string, name: string, over: Partial<Workspace> = {}): Workspace => ({
  id,
  kind: 'folder',
  name,
  project: 'Mac',
  path: `/Users/me/Mac/${id}`,
  branch: null,
  hostId: 'local',
  ...over
})

function sidebar(
  initial: Workspace[],
  options: { settings?: Record<string, JsonValue>; storage?: Record<string, JsonValue>; failRename?: string; failFor?: string; truncated?: () => boolean; hidden?: () => string[] } = {}
) {
  const workspaces = initial.map((w) => ({ ...w }))
  const host = mockHost({
    settings: options.settings,
    storage: options.storage,
    results: {
      'workspace.list': () => {
        const hidden = new Set(options.hidden?.() ?? [])
        const truncated = options.truncated?.() ?? false
        const listed = workspaces.filter((w) => !hidden.has(w.id))
        return { workspaces: (truncated ? listed.slice(0, 1) : listed).map((w) => ({ ...w })), truncated }
      },
      'workspace.rename': ({ workspaceId, name }: { workspaceId: string; name: string }) => {
        if (options.failRename && (!options.failFor || options.failFor === workspaceId)) throw hostError(options.failRename)
        const w = workspaces.find((x) => x.id === workspaceId)
        if (!w) throw hostError('invalid_params', 'no such workspace')
        w.name = name
        return { name }
      }
    }
  })
  const named = (id: string) => workspaces.find((w) => w.id === id)?.name
  return { host, workspaces, named }
}

test('first run only records what exists, and offers suggestions instead of renaming', async () => {
  const s = sidebar([ws('old', 'Fix auth token refresh'), ws('root', 'main', { kind: 'worktree', branch: 'main' })])
  const worker = await mountWorker(main, { host: s.host })
  try {
    const view = await worker.request('naming.check')
    assert.equal(view.status, 'active')
    assert.deepEqual(s.host.callsOf('workspace.rename'), [])
    assert.deepEqual(view.suggestions, [{ id: 'old', current: 'Fix auth token refresh', proposed: 'Mac｜Fix auth token refresh', reason: 'agent-title' }])
    const state = s.host.data().storage['state-v2'] as { baseline: string[] }
    assert.deepEqual(state.baseline, ['old', 'root'])
    assert.deepEqual(worker.refusals, [])
  } finally {
    await worker.deactivate()
  }
})

test('a new workspace waits for Orca’s title, gets the prefix, then is left alone once renamed by hand', async () => {
  const s = sidebar([ws('old', 'Mac｜Existing')])
  const worker = await mountWorker(main, { host: s.host })
  try {
    await worker.request('naming.check')

    s.workspaces.push(ws('new', 'blobfish'))
    await worker.emit('workspace.created', { workspaceId: 'new', kind: 'folder', path: '/Users/me/Mac/new', branch: '' })
    let view = await worker.request('naming.check')
    assert.equal(view.counts.waiting, 1)
    assert.equal(s.named('new'), 'blobfish')

    s.workspaces[1].name = 'Fix login bug' // Orca's own title after the first agent message
    view = await worker.request('naming.check')
    assert.equal(s.named('new'), 'Mac｜Fix login bug')
    assert.equal(view.counts.renamedToday, 1)
    assert.equal(view.history[0].after, 'Mac｜Fix login bug')
    assert.ok(s.host.callsOf('statusBar.update').length > 0)

    s.workspaces[1].name = 'My notes' // the person renames it
    view = await worker.request('naming.check')
    assert.equal(s.named('new'), 'My notes')
    assert.equal(view.counts.locked, 1)
    assert.deepEqual(worker.refusals, [])
  } finally {
    await worker.deactivate()
  }
})

test('undo puts the old name back and locks the workspace', async () => {
  const s = sidebar([])
  const worker = await mountWorker(main, { host: s.host })
  try {
    await worker.request('naming.check')
    s.workspaces.push(ws('w', 'Agent｜Fix flaky tests'))
    let view = await worker.request('naming.check')
    assert.equal(s.named('w'), 'Mac｜Fix flaky tests')

    view = await worker.request('naming.undo', { key: view.history[0].key })
    assert.equal(s.named('w'), 'Agent｜Fix flaky tests')
    assert.equal(view.history[1].undone, true)
    assert.equal(view.counts.locked, 1)

    view = await worker.request('naming.check')
    assert.equal(s.named('w'), 'Agent｜Fix flaky tests')
  } finally {
    await worker.deactivate()
  }
})

test('suggestions are applied or ignored only on request', async () => {
  const s = sidebar([ws('a', 'Fix auth token refresh'), ws('b', 'Chat｜Release notes draft')])
  const worker = await mountWorker(main, { host: s.host })
  try {
    let view = await worker.request('naming.check')
    assert.equal(view.suggestions.length, 2)

    view = await worker.request('naming.apply', { ids: ['a'] })
    assert.equal(s.named('a'), 'Mac｜Fix auth token refresh')
    view = await worker.request('naming.ignore', { ids: ['b'] })
    assert.equal(s.named('b'), 'Chat｜Release notes draft')
    assert.deepEqual(view.suggestions, [])
    assert.equal(view.counts.locked, 1)
  } finally {
    await worker.deactivate()
  }
})

test('paused: nothing is renamed, and the toggle command resumes with a suggestion', async () => {
  const s = sidebar([], { settings: { enabled: false } })
  const worker = await mountWorker(main, { host: s.host })
  try {
    await worker.request('naming.check')
    s.workspaces.push(ws('w', 'Write the changelog'))
    const view = await worker.request('naming.check')
    assert.equal(view.status, 'paused')
    assert.equal(s.named('w'), 'Write the changelog')

    const line = await worker.command('naming.toggle')
    assert.match(String(line), /is on/)
    // It appeared while paused, so it is only suggested, never renamed on its own.
    assert.equal(s.named('w'), 'Write the changelog')
    assert.deepEqual((await worker.request('naming.view')).suggestions.map((x) => x.id), ['w'])
  } finally {
    await worker.deactivate()
  }
})

test('aliases and standby names come from settings', async () => {
  const s = sidebar([], { settings: { aliases: 'Mac = Desk\nbad line', standbyNames: true } })
  const worker = await mountWorker(main, { host: s.host })
  try {
    await worker.request('naming.check')
    s.workspaces.push(ws('w', 'seagrass'))
    const view = await worker.request('naming.check')
    assert.match(s.named('w') ?? '', /^Desk｜Standby [0-9a-f]{8}$/)
    assert.deepEqual(view.aliasErrors, [{ line: 2, text: 'bad line' }])
    assert.equal(view.counts.waiting, 0)
  } finally {
    await worker.deactivate()
  }
})

test('a rename interrupted by a crash is finished from the write-ahead record', async () => {
  const s = sidebar([ws('w', 'Mac｜Half done')], {
    storage: {
      'state-v2': {
        version: 2,
        baseline: [],
        seen: { w: 1 },
        records: {},
        history: [],
        pending: { id: 'w', before: 'Half done', after: 'Mac｜Half done', reason: 'agent-title', stage: 'final', at: 1 }
      }
    }
  })
  const worker = await mountWorker(main, { host: s.host })
  try {
    const view = await worker.request('naming.check')
    const state = s.host.data().storage['state-v2'] as { pending: unknown; records: Record<string, { name: string }> }
    assert.equal(state.pending, null)
    assert.equal(state.records.w.name, 'Mac｜Half done')
    assert.equal(view.counts.managed, 1)
  } finally {
    await worker.deactivate()
  }
})

test('a failed rename is reported and retried, without stopping the worker', async () => {
  const s = sidebar([], { failRename: 'rate_limited' })
  const worker = await mountWorker(main, { host: s.host })
  try {
    await worker.request('naming.check')
    s.workspaces.push(ws('w', 'Ship the release'))
    const view = await worker.request('naming.check')
    assert.equal(view.status, 'active')
    assert.deepEqual(view.notice, { code: 'rename-failed', detail: 'rate_limited' })
    assert.equal(s.named('w'), 'Ship the release')
    assert.equal(view.suggestions.length, 0)
    const status = await worker.command('naming.status')
    assert.match(String(status), /Workspace Naming Guard is on/)
  } finally {
    await worker.deactivate()
  }
})

test('a lock survives the workspace missing from one list', async () => {
  let hidden: string[] = []
  const s = sidebar([], { hidden: () => hidden })
  const worker = await mountWorker(main, { host: s.host })
  try {
    await worker.request('naming.check')
    s.workspaces.push(ws('w', 'Fix login bug'))
    await worker.request('naming.check')
    s.workspaces[0].name = 'Scratch notes for Alice'
    let view = await worker.request('naming.check')
    assert.equal(view.counts.locked, 1)

    hidden = ['w'] // an SSH host offline, or Orca still starting
    await worker.request('naming.check')
    hidden = []
    view = await worker.request('naming.check')
    assert.equal(s.named('w'), 'Scratch notes for Alice')
    assert.equal(view.counts.locked, 1)
  } finally {
    await worker.deactivate()
  }
})

test('a workspace that appears or is renamed while paused only gets a suggestion', async () => {
  const s = sidebar([], { settings: { enabled: false } })
  const worker = await mountWorker(main, { host: s.host })
  try {
    await worker.request('naming.check')
    s.workspaces.push(ws('w', 'My own design notes'))
    await worker.request('naming.check')
    const view = await worker.request('naming.setEnabled', { enabled: true })
    assert.equal(s.named('w'), 'My own design notes')
    assert.deepEqual(view.suggestions.map((x) => x.id), ['w'])
  } finally {
    await worker.deactivate()
  }
})

test('a truncated first list does not make old workspaces look new', async () => {
  let truncated = true
  const s = sidebar([ws('a', 'Old task one'), ws('b', 'Old task two')], { truncated: () => truncated })
  const worker = await mountWorker(main, { host: s.host })
  try {
    await worker.request('naming.check')
    truncated = false
    const view = await worker.request('naming.check')
    assert.deepEqual(s.host.callsOf('workspace.rename'), [])
    assert.deepEqual(view.suggestions.map((x) => x.id).sort(), ['a', 'b'])
  } finally {
    await worker.deactivate()
  }
})

test('Apply carries on past a failed rename and says how many failed', async () => {
  const s = sidebar([ws('a', 'First old task'), ws('b', 'Second old task'), ws('c', 'Third old task')], { failRename: 'rate_limited', failFor: 'b' })
  const worker = await mountWorker(main, { host: s.host })
  try {
    await worker.request('naming.check')
    const view = await worker.request('naming.apply', { ids: ['a', 'b', 'c'] })
    assert.equal(s.named('a'), 'Mac｜First old task')
    assert.equal(s.named('b'), 'Second old task')
    assert.equal(s.named('c'), 'Mac｜Third old task')
    assert.deepEqual(view.notice, { code: 'apply-failed', detail: '1/3 · rate_limited' })
  } finally {
    await worker.deactivate()
  }
})

test('an undo cannot itself be undone', async () => {
  const s = sidebar([])
  const worker = await mountWorker(main, { host: s.host })
  try {
    await worker.request('naming.check')
    s.workspaces.push(ws('w', 'Agent｜Fix flaky tests'))
    let view = await worker.request('naming.check')
    view = await worker.request('naming.undo', { key: view.history[0].key })
    const undoEntry = view.history.find((h) => h.reason === 'undo')
    view = await worker.request('naming.undo', { key: undoEntry?.key ?? '' })
    assert.deepEqual(view.notice, { code: 'already-undone' })
    assert.equal(s.named('w'), 'Agent｜Fix flaky tests')
  } finally {
    await worker.deactivate()
  }
})
