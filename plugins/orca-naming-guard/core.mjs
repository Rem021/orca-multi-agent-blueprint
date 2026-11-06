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

export function validTitle(name, prefixes) {
  return typeof name === 'string' && name.length <= 60 && !/[\n\r\x00-\x1f]/.test(name)
    && name.split('｜').length === 2 && prefixes.has(name.split('｜')[0])
    && /[\u4e00-\u9fff]/.test(name.split('｜')[1])
    && !isWaitingName(name);
}

export function extractCandidate(rawTitle, prefix, prefixes) {
  if (typeof rawTitle !== 'string' || !/[\u4e00-\u9fff]/.test(rawTitle)) return null;
  if (/[\n\r\x00-\x1f]/.test(rawTitle) || /https?:\/\//i.test(rawTitle)) return null;
  const cleaned = normalize(rawTitle);
  if (!cleaned || isWaitingName(cleaned) || /^(?:New|Untitled|\S+)\s+Chat$/i.test(cleaned)) return null;
  if (cleaned.includes('｜')) {
    const parts = cleaned.split('｜');
    if (parts.length !== 2) return null;
    let [head, body] = parts.map(s => s.trim());
    if (!body || !/[\u4e00-\u9fff]/.test(body)) return null;
    if (genericPrefixes.has(head)) head = prefix;
    else if (aliases[head]) head = aliases[head];
    if (prefix !== 'Mac' && head === 'Mac') head = prefix;
    const title = normalize(`${head}｜${body}`);
    return validTitle(title, prefixes) ? { title, reason: 'chat-title', provisional: false } : null;
  }
  if (cleaned.length > 48) return null;
  let topic = cleaned
    .replace(/^(?:请问|请帮我|帮我|麻烦你|麻烦|去全面优化一下|全面优化一下|去优化一下|先帮我|我想|我要的是|我们需要)\s*/u, '')
    .replace(/[？?！!。；;：:…~～]+$/u, '')
    .replace(/[，,、？?！!。；;：:]+/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (topic.length > 24) topic = topic.slice(0, 24).trim();
  if (!topic || !/[\u4e00-\u9fff]/.test(topic)) return null;
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
    if (w.branch && w.pendingFirstAgentMessageRename) continue;
    const prefix = repoMap.get(w.repoId);
    if (!prefix || !isManagedPath(w.path)) continue;
    const before = w.displayName ?? '';
    const branchShort = (w.branch ?? '').replace(/^refs\/heads\//, '');
    const isWaiting = isWaitingName(before);
    const isBlank = before === BLANK_NAME || (Boolean(branchShort) && before === branchShort);
    const record = records[w.id];
    const recordIsWaiting = isWaitingName(record?.after);
    if (record && before !== record.after && !(recordIsWaiting && (isBlank || isWaiting))) continue;

    const tabs = (byId.get(w.id)?.tabs ?? []).filter(t => t.type === 'agent-session');
    const candidates = tabs.map(t => extractCandidate(t.title, prefix, prefixes)).filter(Boolean);
    const candidate = candidates.find(c => !c.provisional) ?? candidates[0] ?? null;

    if (validTitle(normalize(before), prefixes) && !isWaiting) {
      if (record?.provisional && before === record.after && candidate && !candidate.provisional && candidate.title !== before) {
        plan.push({ id: w.id, path: w.path, before, after: candidate.title, expect: candidate.title, reason: 'chat-title', provisional: false });
      }
      continue;
    }

    if (candidate) {
      if (candidate.title !== before) {
        plan.push({
          id: w.id,
          path: w.path,
          before,
          after: candidate.title,
          expect: candidate.title,
          reason: candidate.reason,
          provisional: candidate.provisional
        });
      }
      continue;
    }

    // No task has been sent to the agent yet: leave blank instead of generating "待命工作区".
    if (isBlank) continue;
    if (w.branch) {
      if (isWaiting) {
        plan.push({ id: w.id, path: w.path, before, after: ' ', expect: branchShort, reason: 'clear-waiting', blank: true });
      }
    } else if (isWaiting || (/^[a-z0-9_-]+$/i.test(before) && !record)) {
      plan.push({ id: w.id, path: w.path, before, after: BLANK_NAME, expect: BLANK_NAME, reason: 'blank-new', blank: true });
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
    state.records[p.id] = {
      after: expect,
      ...(p.blank ? { blank: true } : {}),
      ...(p.provisional ? { provisional: true } : {})
    };
    state.history.push({ ...p, after: expect, at: new Date().toISOString() });
    state.history = state.history.slice(-1000);
    state.pending = null;
    await persist(state);
    applied++;
  }
  return applied;
}
