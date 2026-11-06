import { adapter } from './adapter.mjs';
import { planNames, applyPlan } from './core.mjs';
let interval, delayed, subscription, stopped = false, busy = false;
export default function activate(orca) {
  stopped = false;
  const persist = value => orca.storage.set('state-v1', value);
  async function reconcile(preview = false) {
    preview = preview || process.env.ORCA_NAMING_READ_ONLY === '1';
    if (busy || stopped) return { busy: true };
    busy = true;
    try {
      const state = (await orca.storage.get('state-v1')) ?? { records: {}, history: [], pending: null };
      if (!state.records || !Array.isArray(state.history)) throw new Error('incompatible_plugin_state');
      const s = await adapter.snapshot();
      if (stopped) return { stopped: true };
      if (state.pending) {
        const actual = s.worktrees.find(w => w.id === state.pending.id);
        if (actual?.displayName === state.pending.after) state.records[actual.id] = { after: state.pending.after };
        state.pending = null;
        if (!preview) await persist(state);
      }
      const plan = planNames(s.worktrees, s.repos, s.snapshots, state.records);
      if (preview) return { pending: plan, recent: state.history.slice(-10) };
      const count = await applyPlan(plan.slice(0, 25), adapter, state, persist, () => stopped);
      await orca.host.call('statusBar.update', { id: 'naming', text: '中文命名', tone: 'success', tooltip: `运行中；本轮更新 ${count} 项，60 秒补检。仅修改显示名。` });
      if (count) orca.log(`Normalized ${count} workspace display names`);
      return { updated: count };
    } catch (e) {
      // Avoid arbitrary CLI stderr/private content in logs.
      const code = /^[a-z_]+$/.test(e.message) ? e.message : 'naming_check_failed';
      orca.log(code);
      await orca.host.call('statusBar.update', { id: 'naming', text: '命名待检查', tone: 'warning', tooltip: code });
      return { error: code };
    } finally { busy = false; }
  }
  orca.commands.register('naming.preview', () => reconcile(true));
  subscription = orca.events.on('workspace.created', () => {
    clearTimeout(delayed); delayed = setTimeout(() => void reconcile(), 3000);
  });
  // A lightweight metadata pass, not an Agent/task scheduler; no model/API calls.
  interval = setInterval(() => void reconcile(), 60000);
  void reconcile();
}
export function deactivate() {
  stopped = true; clearInterval(interval); clearTimeout(delayed); subscription?.dispose();
}
