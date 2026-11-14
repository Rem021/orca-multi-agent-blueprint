// `orca plugin test .` runs this in Node with the same runtime and checks Orca uses.
import { assert, mockHost, mount, test } from 'orca/testing'
import type { NamingView } from 'orca'
import * as panel from './panel.tsx'

const base: NamingView = {
  status: 'active',
  error: null,
  checkedAt: Date.now(),
  enabled: true,
  standbyNames: false,
  aliasErrors: [],
  counts: { managed: 1, locked: 0, waiting: 0, renamedToday: 1 },
  suggestions: [{ id: 'old', current: 'Fix auth token refresh', proposed: 'Mac｜Fix auth token refresh', reason: 'agent-title' }],
  history: [{ key: 'h1', before: 'blobfish', after: 'Mac｜Fix login bug', reason: 'agent-title', at: Date.now() - 60_000, undone: false }],
  notice: null
}

test('shows status, activity and suggestions, and sends Undo and Apply to the worker', async () => {
  const calls: string[] = []
  let current = base
  const host = mockHost({
    requests: {
      'naming.view': () => current,
      'naming.undo': ({ key }) => {
        calls.push(`undo:${key}`)
        current = { ...current, history: [{ ...current.history[0], undone: true }] }
        return current
      },
      'naming.apply': ({ ids }) => {
        calls.push(`apply:${ids.join(',')}`)
        current = { ...current, suggestions: [] }
        return current
      },
      'naming.setEnabled': ({ enabled }) => {
        calls.push(`enabled:${enabled}`)
        current = { ...current, enabled, status: enabled ? 'active' : 'paused' }
        return current
      }
    }
  })
  const view = await mount(panel, { host })
  assert.ok(view.find({ text: 'On' }))
  assert.deepEqual(view.rowsOf({ label: 'Activity' }), ['h1'])

  await view.selectRows({ label: 'Activity' }, ['h1'])
  await view.press({ text: 'Undo' })
  assert.deepEqual(calls, ['undo:h1'])

  await view.select({ label: 'Naming Guard' }, 'suggestions')
  assert.deepEqual(view.rowsOf({ label: 'Suggestions' }), ['old'])
  await view.press({ text: 'Apply all' })
  assert.deepEqual(calls, ['undo:h1', 'apply:old'])
  assert.ok(view.find({ text: 'Every existing workspace already follows Project｜Topic.' }))

  await view.toggle({ label: 'Rename new workspaces automatically' })
  assert.deepEqual(calls.at(-1), 'enabled:false')
  assert.ok(view.find({ text: 'Paused' }))

  await view.post('naming.changed', { ...current, status: 'error', error: 'unavailable' })
  assert.ok(view.find({ role: 'note', text: /unavailable/ }))
  assert.deepEqual(view.errors, [])
  assert.deepEqual(view.warnings, [])
  assert.deepEqual(view.refusals, [])
  await view.unmount()
})

test('speaks Chinese when Orca does', async () => {
  const host = mockHost({ requests: { 'naming.view': () => ({ ...base, status: 'unsupported' }) } })
  const view = await mount(panel, { host, locale: 'zh-CN' })
  assert.ok(view.find({ text: '需更新 Orca' }))
  assert.ok(view.find({ label: '自动命名新工作区' }))
  await view.unmount()
})
