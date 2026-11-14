// The panel <-> worker contract, declared once: src/panel.tsx and src/worker.ts both read it.
export {}

declare module 'orca' {
  type NamingReason = 'agent-title' | 'branch' | 'prefix' | 'standby' | 'undo'

  /** Feedback on the last pass or action; the panel words it in Orca's language. */
  type NamingNotice = {
    code: 'rename-failed' | 'apply-failed' | 'stale' | 'already-undone' | 'gone' | 'renamed-since'
    /** An error code, a count, or the workspace's current name. */
    detail?: string
  }

  /** Everything the panel draws; the worker sends a fresh one after each change. */
  type NamingView = {
    /** `unsupported`: this Orca has no workspace.rename (needs 1.4.256 or later). */
    status: 'starting' | 'active' | 'paused' | 'unsupported' | 'error'
    /** A host error code when `status` is `error`. */
    error: string | null
    checkedAt: number | null
    enabled: boolean
    standbyNames: boolean
    /** Lines of the aliases setting that were skipped. */
    aliasErrors: { line: number; text: string }[]
    counts: { managed: number; locked: number; waiting: number; renamedToday: number }
    suggestions: { id: string; current: string; proposed: string; reason: NamingReason }[]
    history: {
      key: string
      before: string
      after: string
      reason: NamingReason
      at: number
      undone: boolean
    }[]
    notice: NamingNotice | null
  }

  interface PluginRequests {
    'naming.view': { output: NamingView }
    /** Runs a pass now. */
    'naming.check': { output: NamingView }
    /** Applies suggestions by workspace id. */
    'naming.apply': { input: { ids: string[] }; output: NamingView }
    /** Never touch these workspaces again. */
    'naming.ignore': { input: { ids: string[] }; output: NamingView }
    /** Puts back the name a history entry replaced, and locks that workspace. */
    'naming.undo': { input: { key: string }; output: NamingView }
    'naming.setEnabled': { input: { enabled: boolean }; output: NamingView }
  }

  interface PluginPanelEvents {
    'naming.changed': NamingView
  }
}
