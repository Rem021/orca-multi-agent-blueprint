import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, stat } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createConnection } from 'node:net';
import { randomUUID } from 'node:crypto';

const run = promisify(execFile);
const VOLUME = (process.env.AI_VOLUME_PATH || '/Volumes/AI_Workspace').replace(/\/+$/, '');
const EXPECTED_UUID = process.env.AI_VOLUME_UUID ?? 'AUTO';
const HOST_VERIFY_TTL_MS = 60_000;
let lastHostVerifyAt = 0;

export async function verifyHost() {
  const now = Date.now();
  if (now - lastHostVerifyAt < HOST_VERIFY_TTL_MS) {
    if (VOLUME.startsWith('/Volumes/')) {
      await stat(VOLUME).catch(() => {
        lastHostVerifyAt = 0;
        throw new Error('wrong_or_missing_volume');
      });
    }
    return;
  }
  const { stdout } = await run('/usr/libexec/PlistBuddy', ['-c', 'Print :CFBundleShortVersionString', '/Applications/Orca.app/Contents/Info.plist']);
  if (!stdout.trim().startsWith('1.4.')) throw new Error('unsupported_orca_version');
  if (VOLUME.startsWith('/Volumes/') && EXPECTED_UUID && EXPECTED_UUID !== 'AUTO') {
    const { stdout: volume } = await run('/usr/sbin/diskutil', ['info', '-plist', VOLUME]);
    if (!volume.includes(`<string>${EXPECTED_UUID}</string>`)) throw new Error('wrong_or_missing_volume');
  }
  lastHostVerifyAt = now;
}

async function rpcBatch(calls, errorCode = 'orca_command_failed') {
  const meta = JSON.parse(await readFile(join(homedir(), 'Library/Application Support/Orca/orca-runtime.json'), 'utf8'));
  const endpoint = (meta.transports ?? [meta.transport]).find(t => t?.kind === 'unix')?.endpoint;
  if (typeof endpoint !== 'string' || !endpoint.startsWith('/') || !meta.authToken) throw new Error('runtime_unavailable');

  const pending = new Map();
  const results = new Array(calls.length);
  const payloads = calls.map((c, index) => {
    const id = randomUUID();
    pending.set(id, { index, method: c.method });
    return JSON.stringify({ id, authToken: meta.authToken, method: c.method, params: c.params ?? {} }) + '\n';
  });

  return new Promise((resolve, reject) => {
    const socket = createConnection(endpoint);
    let buffer = '';
    const fail = e => {
      socket.destroy();
      const msg = e?.message;
      reject(new Error(msg && /^[a-z_]+$/.test(msg) ? msg : errorCode));
    };
    socket.setTimeout(15000, () => fail());
    socket.on('error', fail);
    socket.on('end', () => {
      if (pending.size > 0) fail();
    });
    socket.on('connect', () => {
      socket.write(payloads.join(''));
    });
    socket.on('data', data => {
      buffer += data.toString();
      if (buffer.length > 16 * 1024 * 1024) return fail();
      let end;
      while ((end = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, end);
        buffer = buffer.slice(end + 1);
        try {
          const d = JSON.parse(line);
          const item = pending.get(d.id);
          if (!item) continue;
          pending.delete(d.id);
          if (!d.ok) throw new Error(d.error?.code ?? errorCode);
          results[item.index] = d.result;
          if (pending.size === 0) {
            socket.removeAllListeners('end');
            socket.destroy();
            resolve(results);
            return;
          }
        } catch (e) {
          return fail(e);
        }
      }
    });
  });
}

function parseTabSnapshots(raw) {
  if (!Array.isArray(raw?.snapshots)) throw new Error('incompatible_tab_schema');
  return raw.snapshots.map(s => {
    if (typeof s.worktree !== 'string' || !Array.isArray(s.tabs)) throw new Error('incompatible_tab_schema');
    return {
      worktree: s.worktree,
      tabs: s.tabs.filter(t => t.type === 'agent-session').map(t => ({ type: t.type, title: t.title }))
    };
  });
}

export async function tabSnapshots() {
  const [res] = await rpcBatch([{ method: 'session.tabs.listAll', params: {} }], 'tab_snapshot_unavailable');
  return parseTabSnapshots(res);
}

export const adapter = {
  async tabsOnly() {
    await verifyHost();
    return tabSnapshots();
  },
  async snapshot(preloadedSnapshots = null) {
    await verifyHost();
    if (preloadedSnapshots) {
      const [w, r] = await rpcBatch([
        { method: 'worktree.list', params: { limit: 10000 } },
        { method: 'repo.list', params: {} }
      ], 'orca_command_failed');
      if (!Array.isArray(w?.worktrees) || !Array.isArray(r?.repos)) throw new Error('incompatible_workspace_schema');
      return {
        worktrees: w.worktrees,
        repos: r.repos.map(x => ({ id: x.id, displayName: x.displayName })),
        snapshots: preloadedSnapshots
      };
    }
    const [w, r, rawTabs] = await rpcBatch([
      { method: 'worktree.list', params: { limit: 10000 } },
      { method: 'repo.list', params: {} },
      { method: 'session.tabs.listAll', params: {} }
    ], 'orca_command_failed');
    if (!Array.isArray(w?.worktrees) || !Array.isArray(r?.repos)) throw new Error('incompatible_workspace_schema');
    return {
      worktrees: w.worktrees,
      repos: r.repos.map(x => ({ id: x.id, displayName: x.displayName })),
      snapshots: parseTabSnapshots(rawTabs)
    };
  },
  async current(id) {
    const [res] = await rpcBatch([{ method: 'worktree.show', params: { worktree: `id:${id}` } }]);
    return res?.worktree ?? null;
  },
  async rename(id, name) {
    const [res] = await rpcBatch([{ method: 'worktree.set', params: { worktree: `id:${id}`, displayName: name } }]);
    return res;
  }
};
