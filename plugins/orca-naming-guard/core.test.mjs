import { test } from 'node:test';
import assert from 'node:assert/strict';
import { planNames, applyPlan } from './core.mjs';
const volumeBase = (process.env.AI_VOLUME_PATH || '/Volumes/AI_Workspace').replace(/\/+$/, '');
const repo = [{ id: 'r', displayName: 'Mac' }];
const workspace = { id: 'r::' + volumeBase + '/AI/Projects/Mac::workspace:one', repoId: 'r', path: volumeBase + '/AI/Projects/Mac', hostId: 'local', displayName: 'fish' };
const snapshots = title => [{ worktree: workspace.id, tabs: [{ type: 'agent-session', title }] }];
test('new blank workspace receives a stable title, then adopts a real task once', () => {
  const p = planNames([workspace], repo, snapshots('Codex Chat'))[0];
  assert.match(p.after, /^Mac｜待命工作区 [a-f0-9]{8}$/);
  assert.equal(planNames([{ ...workspace, displayName: p.after }], repo, snapshots('Codex Chat')).length, 0);
  const q = planNames([{ ...workspace, displayName: p.after }], repo, snapshots('Mac｜相机任务规划'), { [workspace.id]: { after: p.after } });
  assert.equal(q[0].after, 'Mac｜相机任务规划');
  assert.equal(planNames([{ ...workspace, displayName: q[0].after }], repo, snapshots('Mac｜别的主题')).length, 0);
});
test('never touches remote, archived, main, outside-volume or manually changed names', () => {
  for (const override of [{ hostId: 'ssh:test' }, { isArchived: true }, { isMainWorktree: true }, { path: '/tmp/a' }]) {
    assert.deepEqual(planNames([{ ...workspace, ...override }], repo, snapshots('Mac｜主题')), []);
  }
  assert.deepEqual(planNames([workspace], repo, snapshots('Mac｜主题'), { [workspace.id]: { after: 'Mac｜手工指定' } }), []);
});
test('waits for native Git branch naming before setting a display name', () => {
  assert.deepEqual(planNames([{ ...workspace, branch: 'refs/heads/codex/fish', pendingFirstAgentMessageRename: true }], repo, snapshots('Mac｜任务主题')), []);
  assert.equal(planNames([{ ...workspace, branch: 'refs/heads/codex/task-topic', pendingFirstAgentMessageRename: false }], repo, snapshots('Mac｜任务主题'))[0].after, 'Mac｜任务主题');
});
test('prompt fragments, terminal titles, unknown project and control chars are not task names', () => {
  for (const value of ['https://example.test/task', 'Unknown｜说明', 'Mac｜A\nB', 'Mac｜纯中文'.repeat(30), 'Mac｜ascii']) {
    assert.equal(planNames([workspace], repo, snapshots(value))[0].reason, 'pending-task');
  }
  assert.equal(planNames([workspace], repo, [{ worktree: workspace.id, tabs: [{ type: 'terminal', title: 'Mac｜意外终端标题' }] }])[0].reason, 'pending-task');
});
test('repeated plans are idempotent and compare before mutation', async () => {
  let current = { ...workspace }, writes = 0;
  const adapter = { current: async () => current, rename: async (_id, name) => { writes++; current.displayName = name; } };
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
