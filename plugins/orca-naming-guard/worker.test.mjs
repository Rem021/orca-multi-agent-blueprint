import { test } from 'node:test';
import assert from 'node:assert/strict';
import { adapter } from './adapter.mjs';
import activate, { deactivate } from './worker.mjs';
test('worker prevents overlapping passes and stops cleanly without invoking a real host', async () => {
  const original = adapter.snapshot;
  let release, count = 0, disposed = false;
  adapter.snapshot = async () => { count++; return new Promise(resolve => { release = resolve; }); };
  const commands = new Map();
  const store = new Map();
  const calls = [];
  const host = {
    storage: { get: async key => store.get(key), set: async (key, value) => store.set(key, value) },
    commands: { register: (id, fn) => commands.set(id, fn) },
    events: { on: () => ({ dispose: () => { disposed = true; } }) },
    host: { call: async (name, args) => { calls.push([name, args]); return {}; } },
    log: () => {}
  };
  try {
    activate(host);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(count, 1);
    assert.deepEqual(await commands.get('naming.preview')(), { busy: true });
    deactivate();
    release({ worktrees: [], repos: [], snapshots: [] });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(disposed, true);
    assert.equal(store.size, 0);
    assert.equal(calls.length, 0);
  } finally { deactivate(); adapter.snapshot = original; }
});
