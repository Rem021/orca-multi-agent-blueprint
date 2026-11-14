// `orca plugin check` renders the panel once per scenario, each with the worker on a fresh host.
import { defineCheck } from 'orca/testing'

const workspaces = [
  { id: 'a', kind: 'folder', name: 'Fix auth token refresh', project: 'CloudNode', path: '/w/a', branch: null, hostId: 'local' },
  { id: 'b', kind: 'worktree', name: 'Agent｜Release notes draft', project: 'NovaCam', path: '/w/b', branch: 'codex/mojarra', hostId: 'local' },
  { id: 'c', kind: 'worktree', name: 'main', project: 'NovaCam', path: '/w/c', branch: 'main', hostId: 'local' }
]
const list = { 'workspace.list': { workspaces, truncated: false } }

export default defineCheck({
  empty: { results: { 'workspace.list': { workspaces: [], truncated: false } } },
  suggestions: { results: list },
  activity: {
    results: list,
    storage: {
      'state-v2': {
        version: 2,
        baseline: ['a', 'b', 'c'],
        seen: {},
        records: { a: { name: 'Fix auth token refresh', stage: 'final', at: 1, locked: true } },
        history: [
          { key: 'k1', id: 'x', before: 'codex/mojarra', after: 'NovaCam｜RAW color pipeline', reason: 'branch', at: 1794600000000 },
          { key: 'k2', id: 'y', before: 'Fix login bug', after: 'CloudNode｜Fix login bug', reason: 'agent-title', at: 1794640000000 }
        ],
        pending: null
      }
    }
  },
  paused: { results: list, settings: { enabled: false } },
  aliasErrors: { results: list, settings: { aliases: 'NovaCam = Nova\nnot an alias' } }
})
