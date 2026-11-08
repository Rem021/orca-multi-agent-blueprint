import { createHash } from 'node:crypto';
import { homedir } from 'node:os';

export const BLANK_NAME = '\u200b';

const VOLUME_ROOT = (process.env.AI_VOLUME_PATH || '/Volumes/AI_Workspace').replace(/\/+$/, '') + '/';
const HOME_ROOT = homedir().replace(/\/+$/, '') + '/';

const DEFAULT_ALIASES = {
  NovaRAW: 'NovaCam',
  'NovaCam Pro': 'NovaCam',
  FlowEngine: '流织',
  FlowWeave: '流织',
  Infra_v2: 'CloudNode',
  Game: '星际前线',
  ORCA: 'Mac',
  Orca: 'Mac',
  ColorStudio: 'ChromaStudio'
};

const aliases = (() => {
  try {
    return process.env.PROJECT_ALIASES_JSON ? { ...DEFAULT_ALIASES, ...JSON.parse(process.env.PROJECT_ALIASES_JSON) } : DEFAULT_ALIASES;
  } catch {
    return DEFAULT_ALIASES;
  }
})();

const genericPrefixes = new Set(['Agent', 'AI', 'Assistant', 'Chat', 'Workspace', 'Orca', 'ORCA', '工作区', '项目']);

export function normalize(value) {
  return String(value ?? '').trim().replace(/\s*｜\s*/g, '｜')
    .replace(/([\u4e00-\u9fff])([A-Za-z0-9])/g, '$1 $2')
    .replace(/([A-Za-z0-9])([\u4e00-\u9fff])/g, '$1 $2');
}

export function projectName(repo) {
  const raw = String(repo.displayName ?? '').trim().split('｜')[0].trim();
  if (aliases[raw]) return aliases[raw];
  const name = normalize(raw);
  return aliases[name] ?? name;
}

export function isWaitingName(name) {
  return typeof name === 'string' && /^.+｜待命工作区(?: [A-Za-z0-9]+)?$/.test(name.trim());
}

export function isGenericChatTitle(title) {
  return typeof title !== 'string' || !title.trim() || /^(?:New|Untitled|\S+)\s+Chat$/i.test(title.trim());
}

export function isMeaningfulTopic(text) {
  if (typeof text !== 'string') return false;
  const s = text.trim();
  if (!s || isGenericChatTitle(s)) return false;
  if (/[\u4e00-\u9fff]/.test(s)) return true;
  return /[A-Za-z0-9]{2,}\s+[A-Za-z0-9]{2,}/.test(s);
}

export function validTitle(name, prefixes) {
  return typeof name === 'string' && name.length <= 60 && !/[\n\r\x00-\x1f]/.test(name)
    && name.split('｜').length === 2 && prefixes.has(name.split('｜')[0])
    && isMeaningfulTopic(name.split('｜')[1])
    && !isWaitingName(name);
}

function cleanRawInput(rawTitle) {
  if (typeof rawTitle !== 'string') return '';
  if (/[\n\r]/.test(rawTitle)) {
    const lines = rawTitle
      .split(/\r?\n/)
      .map(l => l.replace(/^(?:>\s*|\[Image\s*#?\d*\]\s*|\[Attachment[^\]]*\]\s*|<\/?[A-Z_]+>\s*)+/gi, '').trim())
      .filter(l => l && !/^────/.test(l) && isMeaningfulTopic(l));
    return lines[0] ?? '';
  }
  if (/[\x00-\x1f]/.test(rawTitle)) return '';
  return rawTitle.replace(/^(?:\[Image\s*#?\d*\]\s*|\[Attachment[^\]]*\]\s*)+/gi, '').trim();
}

export function extractCandidate(rawTitle, prefix, prefixes) {
  if (typeof rawTitle !== 'string') return null;
  if (!rawTitle.includes('\n') && /[\r\x00-\x1f]/.test(rawTitle)) return null;
  if (rawTitle.includes('｜') && /[\n\r\x00-\x1f]/.test(rawTitle)) return null;

  const line = cleanRawInput(rawTitle);
  if (!line || !isMeaningfulTopic(line) || /https?:\/\//i.test(line)) return null;

  const cleaned = normalize(line);
  if (!cleaned || isWaitingName(cleaned) || isGenericChatTitle(cleaned)) return null;

  if (cleaned.includes('｜')) {
    const parts = cleaned.split('｜');
    if (parts.length !== 2) return null;
    let [head, body] = parts.map(s => s.trim());
    if (!isMeaningfulTopic(body)) return null;
    if (genericPrefixes.has(head)) head = prefix;
    else if (aliases[head]) head = aliases[head];
    const isKnownOrChineseHead = prefixes.has(head) || Object.values(aliases).includes(head) || /[\u4e00-\u9fff]/.test(head);
    if (isKnownOrChineseHead && (prefix !== 'Mac' || !prefixes.has(head))) {
      head = prefix;
    }
    const title = normalize(`${head}｜${body}`);
    return validTitle(title, prefixes) ? { title, reason: 'chat-title', provisional: false } : null;
  }

  let topic = cleaned
    .replace(/^(?:请问|请帮我|帮我|麻烦你|麻烦|去全面优化一下|全面优化一下|去优化一下|全面查找修复一下|先帮我|我想|我要的是|我们需要|为什么这边还是有这么多|为什么这边|为什么|please\s+help\s+me|please|can\s+you|could\s+you|help\s+me|let'?s)\s*/iu, '')
    .replace(/[？?！!。；;：:…~～]+$/u, '')
    .replace(/[，,、？?！!。；;：:]+/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const maxLen = /[\u4e00-\u9fff]/.test(topic) ? 24 : 36;
  if (topic.length > maxLen) topic = topic.slice(0, maxLen).trim();
  if (!isMeaningfulTopic(topic)) return null;
  const title = normalize(`${prefix}｜${topic}`);
  return validTitle(title, prefixes) ? { title, reason: 'prompt-seed', provisional: true } : null;
}

function isManagedPath(path) {
  return typeof path === 'string' && (path.startsWith(VOLUME_ROOT) || path.startsWith(HOME_ROOT));
}

export function planNames(worktrees, repos, snapshots, records = {}) {
  const repoMap = new Map(repos.map(r => [r.id, projectName(r)]));
  const prefixes = new Set([...repoMap.values(), 'ChromaStudio']);
  const byId = new Map(snapshots.map(s => [s.worktree, s]));
  const plan = [];
  for (const w of worktrees) {
    if (w.hostId !== 'local' || w.isArchived || w.isMainWorktree) continue;
    const prefix = repoMap.get(w.repoId);
    if (!prefix || !isManagedPath(w.path)) continue;
    const before = w.displayName ?? '';
    const hasError = Boolean(w.firstAgentMessageRenameError);
    const isWaiting = isWaitingName(before);
    const record = records[w.id];
    const recordIsTemporary = Boolean(record?.blank) || isWaitingName(record?.after);
    if (record && before !== record.after && !recordIsTemporary) {
      if (hasError) {
        plan.push({ id: w.id, path: w.path, before, after: before, expect: before, reason: 'clear-error', clearError: true });
      }
      continue;
    }

    const tabs = (byId.get(w.id)?.tabs ?? []).filter(t => t.type === 'agent-session');
    const candidates = [];
    for (const t of tabs) {
      const c = extractCandidate(t.title, prefix, prefixes)
        ?? (t.fallbackPrompt ? extractCandidate(t.fallbackPrompt, prefix, prefixes) : null);
      if (c) candidates.push(c);
    }
    const candidate = candidates.find(c => !c.provisional) ?? candidates[0] ?? null;

    if (validTitle(normalize(before), prefixes) && !isWaiting) {
      if (record?.provisional && before === record.after && candidate && !candidate.provisional && candidate.title !== before) {
        plan.push({ id: w.id, path: w.path, before, after: candidate.title, expect: candidate.title, reason: 'chat-title', provisional: false, clearError: hasError });
      } else if (hasError) {
        plan.push({ id: w.id, path: w.path, before, after: before, expect: before, reason: 'clear-error', clearError: true });
      }
      continue;
    }

    if (candidate) {
      if (candidate.title !== before || hasError) {
        plan.push({
          id: w.id,
          path: w.path,
          before,
          after: candidate.title,
          expect: candidate.title,
          reason: candidate.reason,
          provisional: candidate.provisional,
          clearError: hasError
        });
      }
      continue;
    }

    // Immediate creation naming: assign "${prefix}｜待命工作区 <8-char hash>" right on creation,
    // which also clears pendingFirstAgentMessageRename so Orca never fails on background branch rename.
    const suffix = createHash('sha256').update(w.id).digest('hex').slice(0, 8);
    const waitingName = isWaiting ? before : `${prefix}｜待命工作区 ${suffix}`;
    if (waitingName !== before || hasError) {
      plan.push({
        id: w.id,
        path: w.path,
        before,
        after: waitingName,
        expect: waitingName,
        reason: waitingName === before ? 'clear-error' : 'pending-task',
        clearError: hasError
      });
    }
  }
  return plan;
}

export async function applyPlan(plan, adapter, state, persist, cancelled = () => false) {
  let applied = 0;
  for (const p of plan) {
    if (cancelled()) break;
    const current = await adapter.current(p.id);
    if (cancelled() || !current || current.displayName !== p.before || current.path !== p.path) continue;
    const expect = p.expect ?? p.after;
    state.pending = { ...p, after: expect };
    await persist(state);
    if (cancelled()) break;
    const renamed = await adapter.rename(p.id, p.after);
    const actual = renamed?.worktree?.id === p.id ? renamed.worktree : await adapter.current(p.id);
    if (!actual || actual.displayName !== expect) throw new Error('rename_readback_failed');
    if (!p.clearError || state.records[p.id]) {
      state.records[p.id] = {
        after: expect,
        ...(p.blank ? { blank: true } : {}),
        ...(p.provisional ? { provisional: true } : {})
      };
    }
    state.history.push({ ...p, after: expect, at: new Date().toISOString() });
    state.history = state.history.slice(-1000);
    state.pending = null;
    await persist(state);
    applied++;
  }
  return applied;
}
