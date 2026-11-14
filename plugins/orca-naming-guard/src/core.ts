// Pure naming rules: no Orca calls and no I/O, so every rule is unit-tested in src/core.test.ts.
// src/worker.ts feeds them what `workspace.list` returns and applies the plan with `workspace.rename`.

export const SEP = '｜'

/** One row of `workspace.list` (host API 1.5). */
export type Workspace = {
  id: string
  kind: 'worktree' | 'folder'
  name: string
  project?: string | null
  path: string
  branch: string | null
  hostId: string | null
}

/** What the plugin remembers about a workspace it named. */
export type NameRecord = {
  /** The name the plugin last wrote (or the name it was holding when it locked). */
  name: string
  stage: 'standby' | 'final'
  at: number
  /** A person renamed it after the plugin did, or chose Ignore or Undo: never touch it again. */
  locked?: boolean
}

/** `undo` only appears in history: the plugin put a name back on request. */
export type Reason = 'agent-title' | 'branch' | 'prefix' | 'standby' | 'undo'

export type Change = {
  id: string
  before: string
  after: string
  reason: Reason
  stage: NameRecord['stage']
}

export type Options = {
  standbyNames: boolean
  standbyLabel: string
  aliases: Record<string, string>
}

export type Plan = {
  /** Renames for workspaces created while the plugin runs. */
  auto: Change[]
  /** Proposals for workspaces that existed before the plugin started; applied only on request. */
  suggestions: Change[]
  /** Workspaces renamed by someone after the plugin named them: lock them. */
  locks: string[]
  /** New workspaces still on their creation slug, waiting for Orca's agent title. */
  waiting: string[]
}

/** Scripts written without spaces between words: one run is already a readable topic. */
const WIDE = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u
const GENERIC_HEADS = new Set(['agent', 'ai', 'assistant', 'chat', 'workspace', 'task', 'orca', '工作区', '项目', '任务'])
const DEFAULT_BRANCHES = new Set(['main', 'master', 'trunk', 'develop', 'dev'])
const POLITE_LEAD = /^(?:请问|请帮我|请你|帮我|麻烦你|麻烦|我想要|我想|我要|我们需要|能不能|可以帮我|please\s+help\s+me\s+(?:to\s+)?|please\s+|can\s+you\s+|could\s+you\s+|help\s+me\s+(?:to\s+)?|let'?s\s+)/iu

/** Trims, joins `｜` (or `|`) tightly and puts one space between Han characters and Latin letters or digits. */
export function normalize(value: string): string {
  return value
    .trim()
    .replace(/\s*[｜|]\s*/g, SEP)
    .replace(/([\u3400-\u9fff\uf900-\ufaff])([A-Za-z0-9])/g, '$1 $2')
    .replace(/([A-Za-z0-9])([\u3400-\u9fff\uf900-\ufaff])/g, '$1 $2')
    .replace(/\s+/g, ' ')
}

/**
 * Parses the `aliases` setting: one `Raw name = Prefix` per line; blank lines and `#` comments are
 * skipped. Bad lines come back in `errors` (1-based line numbers) so the panel can show them.
 */
export type AliasError = { line: number; text: string }

export function parseAliases(text: unknown): { aliases: Record<string, string>; errors: AliasError[] } {
  const aliases: Record<string, string> = Object.create(null)
  const errors: AliasError[] = []
  if (typeof text !== 'string') return { aliases, errors }
  text.split(/\r?\n/).forEach((raw, index) => {
    const line = raw.trim()
    if (!line || line.startsWith('#')) return
    const at = line.indexOf('=')
    const from = at > 0 ? line.slice(0, at).trim() : ''
    const to = at > 0 ? normalize(line.slice(at + 1)) : ''
    if (!from || !to || to.includes(SEP)) {
      errors.push({ line: index + 1, text: line.slice(0, 60) })
      return
    }
    aliases[from] = to
  })
  return { aliases, errors }
}

/** The alias for `name`, ignoring inherited keys such as `constructor`. */
function aliasOf(aliases: Record<string, string>, name: string): string | undefined {
  return Object.hasOwn(aliases, name) ? aliases[name] : undefined
}

/** The prefix for a project: its alias when one is set, else its own name, normalized. */
export function projectPrefix(project: string, aliases: Record<string, string>): string {
  const raw = normalize(project)
  const head = raw.includes(SEP) ? raw.slice(0, raw.indexOf(SEP)).trim() : raw
  return aliasOf(aliases, project.trim()) ?? aliasOf(aliases, raw) ?? aliasOf(aliases, head) ?? head
}

/** A slug such as `blobfish`, `codex/mojarra` or `main`: one token, nothing a person would read as a topic. */
export function isSlug(name: string): boolean {
  const s = name.trim()
  return !s || (!WIDE.test(s) && !/\s/.test(s))
}

/** Text worth using as a topic: Chinese, Japanese or Korean text, or at least two words in any script. */
export function isMeaningfulTopic(text: string): boolean {
  const s = text.trim()
  if (!s || /^(?:new|untitled)(?:\s+(?:chat|session|workspace))?$/i.test(s)) return false
  if (WIDE.test(s)) return true
  return /[\p{L}\p{N}]{2,}[^\p{L}\p{N}]+[\p{L}\p{N}]{2,}/u.test(s)
}

/** Orca's own `humanizeBranchSlug`: `fix-auth-token` -> `Fix auth token`. */
export function humanizeSlug(slug: string): string {
  const joined = slug.split('-').filter(Boolean).join(' ')
  return joined ? joined.charAt(0).toUpperCase() + joined.slice(1) : ''
}

/** A readable topic from a branch such as `codex/fix-auth-token-refresh-20261113`, or ''. */
export function topicFromBranch(branch: string | null): string {
  if (!branch) return ''
  const last = branch.replace(/^refs\/heads\//, '').split('/').pop() ?? ''
  const words = last.replace(/[_.]+/g, '-').replace(/-?\d{6,}$/, '')
  return humanizeSlug(words)
}

/**
 * Turns a title (an agent's workspace title, a chat title, a first prompt) into a short topic:
 * drops attachment markers, polite openers and trailing punctuation, folds inner punctuation to
 * spaces and keeps at most 24 Han-text or 40 Latin characters. '' when nothing meaningful is left.
 */
export function cleanTopic(title: string): string {
  const firstLine = title
    .split(/\r?\n/)
    .map((line) => line.replace(/^(?:>\s*|\[(?:Image|Attachment|File)[^\]]*\]\s*)+/gi, '').trim())
    .find((line) => line && isMeaningfulTopic(line))
  if (!firstLine || /https?:\/\//i.test(firstLine) || /[\x00-\x1f]/.test(firstLine)) return ''
  let topic = normalize(firstLine)
    .replace(POLITE_LEAD, '')
    .replace(/[？?！!。；;：:…~～.,，、]+$/u, '')
    .replace(/[，,、？?！!。；;：:｜|]+/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  const max = WIDE.test(topic) ? 24 : 40
  if (topic.length > max) {
    topic = topic.slice(0, max)
    // Cut spaced text at a word boundary instead of mid-word.
    if (!WIDE.test(topic) && topic.includes(' ')) topic = topic.slice(0, topic.lastIndexOf(' '))
    topic = topic.trim()
  }
  return isMeaningfulTopic(topic) ? normalize(topic) : ''
}

export function isStandbyName(name: string, label: string): boolean {
  const parts = name.split(SEP)
  return parts.length === 2 && new RegExp(`^${escapeRegExp(label)}(?: [0-9a-f]{4,})?$`).test(parts[1])
}

export function standbyName(prefix: string, label: string, suffix: string): string {
  return `${prefix}${SEP}${label} ${suffix}`
}

/**
 * The project's own checkout or root folder: never renamed. That is a worktree on a default
 * branch, a worktree in a folder named after the project, or a workspace named after it.
 */
export function isProjectRoot(ws: Workspace): boolean {
  const project = normalize(ws.project ?? '').toLowerCase()
  if (ws.kind === 'worktree') {
    if (ws.branch && DEFAULT_BRANCHES.has(ws.branch.replace(/^refs\/heads\//, ''))) return true
    const folder = ws.path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() ?? ''
    if (project && normalize(folder).toLowerCase() === project) return true
  }
  return Boolean(project) && normalize(ws.name).toLowerCase() === project
}

/**
 * The name a workspace should have, or null to leave it as it is.
 * `allowStandby` is false for workspaces that existed before the plugin started.
 */
export function proposeName(
  ws: Workspace,
  prefix: string,
  prefixes: ReadonlySet<string>,
  options: Options,
  standbySuffix: string,
  allowStandby: boolean
): Omit<Change, 'id' | 'before'> | null {
  // Normalized first, so `Agent | Fix tests` is read like `Agent｜Fix tests`.
  const name = normalize(ws.name)

  if (name.includes(SEP)) {
    if (isStandbyName(name, options.standbyLabel)) return null
    const [head, ...rest] = name.split(SEP)
    const cleanHead = normalize(head)
    // Fix only heads that are clearly not a deliberate choice: generic words, an alias or
    // another project's prefix. Any other head is someone's own naming, and the topic after
    // it is kept word for word.
    const replaceable =
      GENERIC_HEADS.has(cleanHead.toLowerCase()) ||
      aliasOf(options.aliases, cleanHead) !== undefined ||
      (prefixes.has(cleanHead) && cleanHead !== prefix)
    if (!replaceable) return null
    const topic = normalize(rest.join(' '))
    if (!isMeaningfulTopic(topic)) return null
    return { after: `${prefix}${SEP}${topic}`, reason: 'prefix', stage: 'final' }
  }

  if (!isSlug(name)) {
    const topic = cleanTopic(name)
    if (topic) return { after: `${prefix}${SEP}${topic}`, reason: 'agent-title', stage: 'final' }
  }

  const fromBranch = ws.kind === 'worktree' ? cleanTopic(topicFromBranch(ws.branch)) : ''
  if (fromBranch && isSlug(name)) {
    return { after: `${prefix}${SEP}${fromBranch}`, reason: 'branch', stage: 'final' }
  }

  if (allowStandby && options.standbyNames && isSlug(name)) {
    return { after: standbyName(prefix, options.standbyLabel, standbySuffix), reason: 'standby', stage: 'standby' }
  }
  return null
}

/**
 * Plans one pass over every workspace.
 * - `autoIds`: workspaces the plugin may rename on its own (new ones, first seen recently while
 *   it was on); every other workspace only gets suggestions.
 * - `records`: what the plugin named; a record whose name no longer matches means a person
 *   renamed it, so the workspace is locked and left alone from then on.
 * - `suffixOf`: a short stable id for standby names (the worker passes a hash of the id).
 */
export function planAll(
  workspaces: readonly Workspace[],
  autoIds: ReadonlySet<string>,
  records: Readonly<Record<string, NameRecord>>,
  options: Options,
  suffixOf: (id: string) => string
): Plan {
  const plan: Plan = { auto: [], suggestions: [], locks: [], waiting: [] }
  const prefixes = new Set<string>(Object.keys(options.aliases).map((k) => options.aliases[k]))
  for (const ws of workspaces) if (ws.project) prefixes.add(projectPrefix(ws.project, options.aliases))

  for (const ws of workspaces) {
    if (!ws.project || isProjectRoot(ws)) continue
    const record = Object.hasOwn(records, ws.id) ? records[ws.id] : undefined
    if (record?.locked) continue
    if (record && record.name !== ws.name) {
      plan.locks.push(ws.id)
      continue
    }
    // Already named by the plugin and still final: nothing to do.
    if (record?.stage === 'final') continue

    const prefix = projectPrefix(ws.project, options.aliases)
    const auto = autoIds.has(ws.id)
    const proposal = proposeName(ws, prefix, prefixes, options, suffixOf(ws.id), auto)
    if (proposal && proposal.after !== ws.name) {
      const change: Change = { id: ws.id, before: ws.name, ...proposal }
      if (auto) plan.auto.push(change)
      else if (proposal.reason !== 'standby') plan.suggestions.push(change)
    } else if (auto && isSlug(ws.name)) {
      // A standby name never waits: it replaced Orca's title, so none is coming.
      plan.waiting.push(ws.id)
    }
  }
  return plan
}

/** A stable 8-hex-digit id for standby names (FNV-1a); not for security. */
export function shortHash(text: string): string {
  let hash = 0x811c9dc5
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193)
  }
  return (hash >>> 0).toString(16).padStart(8, '0')
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}
