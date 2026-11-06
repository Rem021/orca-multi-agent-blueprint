import { test } from 'node:test';
import assert from 'node:assert/strict';
import { planNames, applyPlan } from './core.mjs';
const repo = [
  { id: 'r', displayName: 'Mac' },
  { id: 'c', displayName: 'ColorStudio' },
  { id: 'h', displayName: 'Infra_v2' }
];
const workspace = { id: 'r::/Volumes/AI_Workspace/AI/Projects/Mac::workspace:one', repoId: 'r', path: '/Volumes/AI_Workspace/AI/Projects/Mac', hostId: 'local', displayName: 'fish' };
const gitWorkspace = { id: 'r::/Volumes/AI_Workspace/AI/Orca/Worktrees/Mac/mojarra', repoId: 'r', path: '/Volumes/AI_Workspace/AI/Orca/Worktrees/Mac/mojarra', hostId: 'local', branch: 'refs/heads/codex/mojarra', displayName: 'codex/mojarra' };
const snapshots = (title, id = workspace.id, fallbackPrompt = undefined) => [
  { worktree: id, tabs: [{ type: 'agent-session', title, ...(fallbackPrompt ? { fallbackPrompt } : {}) }] }
];

test('new workspace is immediately named on creation, then upgrades to task title when a task is sent', () => {
  const p = planNames([workspace], repo, snapshots('Codex Chat'))[0];
  assert.match(p.after, /^Mac｜待命工作区 [0-9a-f]{8}$/);
  assert.equal(p.reason, 'pending-task');
  assert.equal(planNames([{ ...workspace, displayName: p.after }], repo, snapshots('Codex Chat'), { [workspace.id]: { after: p.after } }).length, 0);

  const g = planNames([gitWorkspace], repo, snapshots('Codex Chat', gitWorkspace.id))[0];
  assert.match(g.after, /^Mac｜待命工作区 [0-9a-f]{8}$/);
  assert.equal(g.reason, 'pending-task');

  const q = planNames([{ ...workspace, displayName: p.after }], repo, snapshots('Mac｜相机任务规划'), { [workspace.id]: { after: p.after } });
  assert.equal(q[0].after, 'Mac｜相机任务规划');
  assert.equal(planNames([{ ...workspace, displayName: q[0].after }], repo, snapshots('Mac｜别的主题'), { [workspace.id]: { after: q[0].after } }).length, 0);
});

test('recovers from Orca native branch auto-rename failure and enforces dedicated project prefix', () => {
  const seagrass = {
    id: 'h::/Volumes/AI_Workspace/AI/Orca/Worktrees/Infra_v2/seagrass',
    repoId: 'h',
    path: '/Volumes/AI_Workspace/AI/Orca/Worktrees/Infra_v2/seagrass',
    hostId: 'local',
    branch: 'refs/heads/codex/seagrass',
    displayName: 'seagrass',
    pendingFirstAgentMessageRename: true,
    firstAgentMessageRenameError: 'Codex CLI command failed with code 1: You have hit your usage limit'
  };
  const plan = planNames([seagrass], repo, snapshots('流织｜报错原因联合排查', seagrass.id));
  assert.equal(plan.length, 1);
  assert.equal(plan[0].after, 'CloudNode｜报错原因联合排查');
  assert.equal(plan[0].clearError, true);

  const fromJournal = planNames(
    [seagrass],
    repo,
    snapshots('Codex Chat', seagrass.id, '[Image #1] 为什么这边还是有这么多报错报错去结合流织那边，你们联合一起去调查一下问题所在。')
  );
  assert.equal(fromJournal.length, 1);
  assert.equal(fromJournal[0].after, 'CloudNode｜报错报错去结合流织那边 你们联合一起去调查一下问');
  assert.equal(fromJournal[0].provisional, true);
  assert.equal(fromJournal[0].clearError, true);
});

test('normalizes generic Agent prefix, repo-specific prefix, and raw Chinese prompt seeds', () => {
  const waiting = 'Mac｜待命工作区 3b258f0d';
  const a = planNames([{ ...workspace, displayName: waiting }], repo, snapshots('Agent｜多任务调度与工具链效率优化'), { [workspace.id]: { after: waiting } });
  assert.equal(a[0].after, 'Mac｜多任务调度与工具链效率优化');

  const seed = planNames([{ ...workspace, displayName: waiting }], repo, snapshots('这些都有什么用，推荐我连接吗？'), { [workspace.id]: { after: waiting } })[0];
  assert.equal(seed.after, 'Mac｜这些都有什么用 推荐我连接吗');
  assert.equal(seed.provisional, true);
  const refined = planNames([{ ...workspace, displayName: seed.after }], repo, snapshots('Mac｜外部工具连接评估'), { [workspace.id]: { after: seed.after, provisional: true } })[0];
  assert.equal(refined.after, 'Mac｜外部工具连接评估');
  assert.equal(refined.provisional, false);

  const chroma = { id: 'c::/Volumes/AI_Workspace/Work/ColorStudio::workspace:two', repoId: 'c', path: '/Volumes/AI_Workspace/Work/ColorStudio', hostId: 'local', displayName: 'blobfish' };
  const chromaPlan = planNames([chroma], repo, snapshots('Mac｜项目无用文件与缓存清理', chroma.id))[0];
  assert.equal(chromaPlan.after, 'ChromaStudio｜项目无用文件与缓存清理');
});

test('never touches remote, archived, main, outside-volume or manually changed names', () => {
  for (const override of [{ hostId: 'ssh:test' }, { isArchived: true }, { isMainWorktree: true }, { path: '/tmp/a' }]) {
    assert.deepEqual(planNames([{ ...workspace, ...override }], repo, snapshots('Mac｜主题')), []);
  }
  assert.deepEqual(planNames([workspace], repo, snapshots('Mac｜主题'), { [workspace.id]: { after: 'Mac｜手工指定' } }), []);
});

test('immediately names pending Git branch worktrees on creation or when task title is ready', () => {
  const unnamedBranch = planNames([{ ...workspace, displayName: 'codex/fish', branch: 'refs/heads/codex/fish', pendingFirstAgentMessageRename: true }], repo, snapshots('Codex Chat'));
  assert.match(unnamedBranch[0].after, /^Mac｜待命工作区 [0-9a-f]{8}$/);
  assert.equal(planNames([{ ...workspace, branch: 'refs/heads/codex/task-topic', pendingFirstAgentMessageRename: false }], repo, snapshots('Mac｜任务主题'))[0].after, 'Mac｜任务主题');
});

test('urls, terminal titles, unknown project prefixes and control chars fall back to waiting name instead of bad task titles', () => {
  const waiting = 'Mac｜待命工作区 3b258f0d';
  for (const value of ['https://example.test/task', 'Unknown｜说明', 'Mac｜A\nB', 'Mac｜纯中文'.repeat(30), 'Mac｜ascii']) {
    const res = planNames([{ ...workspace, displayName: waiting }], repo, snapshots(value));
    assert.deepEqual(res, []);
  }
  assert.deepEqual(planNames([{ ...workspace, displayName: waiting }], repo, [{ worktree: workspace.id, tabs: [{ type: 'terminal', title: 'Mac｜意外终端标题' }] }]), []);
});

test('repeated plans are idempotent and compare before mutation', async () => {
  let current = { ...workspace }, writes = 0;
  const adapter = { current: async () => current, rename: async (_id, name) => { writes++; current.displayName = name; return { worktree: current }; } };
  const state = { records: {}, history: [], pending: null };
  const plan = planNames([workspace], repo, snapshots('Mac｜测试任务'));
  assert.equal(await applyPlan(plan, adapter, state, async () => {}), 1);
  assert.equal(await applyPlan(plan, adapter, state, async () => {}), 0);
  assert.equal(writes, 1); assert.equal(state.history.length, 1); assert.equal(state.pending, null);
});

test('failed writes retain recovery intent without claiming success', async () => {
  const state = { records: {}, history: [], pending: null };
  await assert.rejects(applyPlan(planNames([workspace], repo, snapshots('Mac｜测试任务')), {
    current: async () => workspace, rename: async () => { throw new Error('failed'); }
  }, state, async () => {}));
  assert.equal(state.history.length, 0); assert.ok(state.pending);
});
