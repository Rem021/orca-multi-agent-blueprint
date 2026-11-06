import { createHash } from 'node:crypto';

const VOLUME_AI_PREFIX = (process.env.AI_VOLUME_PATH || '/Volumes/AI_Workspace').replace(/\/+$/, '') + '/AI/';
const DEFAULT_ALIASES = { NovaRAW: 'NovaCam', 'NovaCam Pro': 'NovaCam', FlowEngine: '流织', FlowWeave: '流织', Infra_v2: 'CloudNode', Game: '星际前线', ORCA: 'Mac', Orca: 'Mac' };
const aliases = (() => {
  try {
    return process.env.PROJECT_ALIASES_JSON ? { ...DEFAULT_ALIASES, ...JSON.parse(process.env.PROJECT_ALIASES_JSON) } : DEFAULT_ALIASES;
  } catch {
    return DEFAULT_ALIASES;
  }
})();

export function normalize(value) {
  return String(value ?? '').trim().replace(/\s*｜\s*/g, '｜')
    .replace(/([\u4e00-\u9fff])([A-Za-z0-9])/g, '$1 $2')
    .replace(/([A-Za-z0-9])([\u4e00-\u9fff])/g, '$1 $2');
}
export function projectName(repo) {
  const name = normalize(repo.displayName).split('｜')[0];
  return aliases[name] ?? name;
}
export function validTitle(name, prefixes) {
  return typeof name === 'string' && name.length <= 60 && !/[\n\r\x00-\x1f]/.test(name)
    && name.split('｜').length === 2 && prefixes.has(name.split('｜')[0])
    && /[\u4e00-\u9fff]/.test(name.split('｜')[1]);
}
export function planNames(worktrees, repos, snapshots, records = {}) {
  const repoMap = new Map(repos.map(r => [r.id, projectName(r)]));
  const prefixes = new Set([...repoMap.values(), 'ChromaStudio']);
  const byId = new Map(snapshots.map(s => [s.worktree, s]));
  const plan = [];
  for (const w of worktrees) {
    if (w.hostId !== 'local' || w.isArchived || w.isMainWorktree) continue;
    // Setting a label clears first-work naming. Let Orca finish its Git branch
    // naming first, otherwise a waiting label could freeze a random fish branch.
    if (w.branch && w.pendingFirstAgentMessageRename) continue;
    const prefix = repoMap.get(w.repoId);
    if (!prefix || !w.path?.startsWith(VOLUME_AI_PREFIX)) continue;
    const before = w.displayName ?? '';
    const record = records[w.id];
    // Respect a human edit after a managed write, including a nonconforming name.
    if (record && before !== record.after) continue;
    const isWaiting = new RegExp('^' + prefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '｜待命工作区(?: [A-Za-z0-9]+)?$').test(before);
    if (validTitle(normalize(before), prefixes) && !isWaiting) continue;
    const candidate = (byId.get(w.id)?.tabs ?? [])
      .filter(t => t.type === 'agent-session')
      .map(t => normalize(t.title))
      .find(t => validTitle(t, prefixes) && !t.includes('｜待命工作区'));
    const suffix = createHash('sha256').update(w.id).digest('hex').slice(0, 8);
    const after = candidate ?? (isWaiting ? before : `${prefix}｜待命工作区 ${suffix}`);
    if (after !== before) plan.push({ id: w.id, path: w.path, before, after, reason: candidate ? 'chat-title' : 'pending-task' });
  }
  return plan;
}

export async function applyPlan(plan, adapter, state, persist, cancelled = () => false) {
  let applied = 0;
  for (const p of plan) {
    if (cancelled()) break;
    const current = await adapter.current(p.id);
    if (cancelled() || !current || current.displayName !== p.before || current.path !== p.path) continue;
    // Record intent before effect; a crash cannot cause an unrelated rename on restart.
    state.pending = p;
    await persist(state);
    if (cancelled()) break;
    await adapter.rename(p.id, p.after);
    const actual = await adapter.current(p.id);
    if (!actual || actual.displayName !== p.after) throw new Error('rename_readback_failed');
    state.records[p.id] = { after: p.after };
    state.history.push({ ...p, at: new Date().toISOString() });
    state.history = state.history.slice(-1000);
    state.pending = null;
    await persist(state);
    applied++;
  }
  return applied;
}
