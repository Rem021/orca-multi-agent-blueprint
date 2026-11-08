import { adapter } from './adapter.mjs';
import { planNames, applyPlan } from './core.mjs';

let interval, delayed, subscription, stopped = false, busy = false, lastTabSig = null;

function tabSignature(snapshots) {
  return snapshots
    .map(s => {
      const titles = (s.tabs ?? [])
        .filter(t => t.type === 'agent-session')
        .map(t => `${t.title ?? ''}\x1e${t.fallbackPrompt ?? ''}`)
        .join('\x1f');
      return titles ? `${s.worktree}=${titles}` : '';
    })
    .filter(Boolean)
    .sort()
    .join('\n');
}

export default function activate(orca) {
  stopped = false;
  lastTabSig = null;
  const persist = value => orca.storage.set('state-v1', value);

  async function reconcile(preview = false, forceFull = true) {
    preview = preview || process.env.ORCA_NAMING_READ_ONLY === '1';
    if (busy || stopped) return { busy: true };
    busy = true;
    try {
      const state = (await orca.storage.get('state-v1')) ?? { records: {}, history: [], pending: null };
      if (!state.records || !Array.isArray(state.history)) throw new Error('incompatible_plugin_state');

      let preloadedSnapshots = null;
      if (!preview && !forceFull && !state.pending && typeof adapter.tabsOnly === 'function') {
        preloadedSnapshots = await adapter.tabsOnly();
        if (stopped) return { stopped: true };
        const sig = tabSignature(preloadedSnapshots);
        if (sig === lastTabSig) return { updated: 0, skipped: true };
      }

      const s = await adapter.snapshot(preloadedSnapshots);
      if (stopped) return { stopped: true };
      const hasRenameError = s.worktrees.some(w => Boolean(w.firstAgentMessageRenameError));
      lastTabSig = hasRenameError ? null : tabSignature(s.snapshots);

      if (state.pending) {
        const actual = s.worktrees.find(w => w.id === state.pending.id);
        if (actual?.displayName === state.pending.after) state.records[actual.id] = { after: state.pending.after };
        state.pending = null;
        if (!preview) await persist(state);
      }
      const plan = planNames(s.worktrees, s.repos, s.snapshots, state.records);
      if (preview) return { pending: plan, recent: state.history.slice(-10) };
      const count = await applyPlan(plan.slice(0, 25), adapter, state, persist, () => stopped);
      await orca.host.call('statusBar.update', {
        id: 'naming',
        text: 'Naming Guard',
        tone: 'success',
        tooltip: `Active · Auto-names new workspaces & syncs Project｜Topic titles (${count} updated this pass)`
      });
      if (count) orca.log(`Normalized ${count} workspace display names`);
      return { updated: count };
    } catch (e) {
      const code = /^[a-z_]+$/.test(e.message) ? e.message : 'naming_check_failed';
      orca.log(code);
      await orca.host.call('statusBar.update', { id: 'naming', text: 'Naming Check', tone: 'warning', tooltip: code });
      return { error: code };
    } finally {
      busy = false;
    }
  }

  orca.commands.register('naming.preview', () => reconcile(true, true));
  subscription = orca.events.on('workspace.created', () => {
    clearTimeout(delayed);
    delayed = setTimeout(() => void reconcile(false, true), 350);
  });
  interval = setInterval(() => void reconcile(false, false), 4000);
  void reconcile(false, true);
}

export function deactivate() {
  stopped = true;
  clearInterval(interval);
  clearTimeout(delayed);
  subscription?.dispose();
}
