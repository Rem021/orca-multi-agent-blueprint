import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, stat } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createConnection } from 'node:net';
import { randomUUID } from 'node:crypto';
import { isGenericChatTitle } from './core.mjs';

const run = promisify(execFile);
const VOLUME = (process.env.AI_VOLUME_PATH || join(homedir(), 'AI-Workspace')).replace(/\/+$/, '');
const EXPECTED_UUID = process.env.AI_VOLUME_UUID ?? 'AUTO';
const RUNTIME_META_PATHS = [
  join(homedir(), 'Library/Application Support/orca/orca-runtime.json'),
  join(homedir(), 'Library/Application Support/Orca/orca-runtime.json')
];
const JOURNAL_DB_PATH = join(homedir(), 'Library/Application Support/orca/agent-session-journal.db');
const HOST_VERIFY_TTL_MS = 60_000;
let lastHostVerifyAt = 0;

async function readRuntimeMeta() {
  for (const p of RUNTIME_META_PATHS) {
    try {
      return JSON.parse(await readFile(p, 'utf8'));
    } catch {}
  }
  throw new Error('runtime_unavailable');
}

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
  const ver = /^(\d+)\.(\d+)\.(\d+)/.exec(stdout.trim());
  if (!ver || Number(ver[1]) < 1 || (Number(ver[1]) === 1 && (Number(ver[2]) < 4 || (Number(ver[2]) === 4 && Number(ver[3]) < 250)))) {
    throw new Error('unsupported_orca_version');
  }
  if (VOLUME.startsWith('/Volumes/') && EXPECTED_UUID && !['AUTO', 'LOCAL', 'INTERNAL'].includes(EXPECTED_UUID)) {
    const { stdout: volume } = await run('/usr/sbin/diskutil', ['info', '-plist', VOLUME]);
    if (!volume.includes(`<string>${EXPECTED_UUID}</string>`)) throw new Error('wrong_or_missing_volume');
  }
  lastHostVerifyAt = now;
}

async function rpcBatch(calls, errorCode = 'orca_command_failed') {
  const meta = await readRuntimeMeta();
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

async function fetchJournalFallbackPrompts(sessionIds) {
  const validIds = [...new Set(sessionIds.filter(id => typeof id === 'string' && /^[A-Za-z0-9_-]+$/.test(id)))];
  if (validIds.length === 0) return new Map();
  try {
    const quoted = validIds.map(id => `'${id}'`).join(',');
    const sql = `
      SELECT g.session_id, o.value_json
      FROM journal_outcome_generations g
      JOIN journal_outcomes o ON o.generation = g.generation
      WHERE g.session_id IN (${quoted})
        AND o.field = 'items'
        AND o.deleted = 0
        AND o.value_json LIKE '%"role":"user"%'
      ORDER BY g.generation ASC, o.ordinal ASC;
    `;
    const { stdout } = await run('/usr/bin/sqlite3', ['-readonly', '-json', JOURNAL_DB_PATH, sql], { timeout: 3000, maxBuffer: 4 * 1024 * 1024 });
    const rows = stdout.trim() ? JSON.parse(stdout) : [];
    const map = new Map();
    for (const r of rows) {
      if (map.has(r.session_id)) continue;
      try {
        const item = JSON.parse(r.value_json);
        const text = (item?.body?.blocks ?? [])
          .filter(b => b?.type === 'text' && typeof b.text === 'string')
          .map(b => b.text.trim())
          .filter(t => t && !/^────\s*From:\s*STRUCTWORKER_/i.test(t))
          .join('\n');
        if (text && /[\u4e00-\u9fff]/.test(text)) {
          map.set(r.session_id, text);
        }
      } catch {}
    }
    return map;
  } catch {
    return new Map();
  }
}

async function parseTabSnapshots(raw) {
  if (!Array.isArray(raw?.snapshots)) throw new Error('incompatible_tab_schema');
  const genericSessionIds = [];
  for (const s of raw.snapshots) {
    if (typeof s.worktree !== 'string' || !Array.isArray(s.tabs)) throw new Error('incompatible_tab_schema');
    for (const t of s.tabs) {
      if (t.type === 'agent-session' && isGenericChatTitle(t.title) && typeof t.sessionId === 'string') {
        genericSessionIds.push(t.sessionId);
      }
    }
  }
  const fallbackMap = await fetchJournalFallbackPrompts(genericSessionIds);
  return raw.snapshots.map(s => ({
    worktree: s.worktree,
    tabs: s.tabs.filter(t => t.type === 'agent-session').map(t => ({
      type: t.type,
      title: t.title,
      ...(t.sessionId && fallbackMap.has(t.sessionId) ? { fallbackPrompt: fallbackMap.get(t.sessionId) } : {})
    }))
  }));
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
      snapshots: await parseTabSnapshots(rawTabs)
    };
  },
  async current(id) {
    const [res] = await rpcBatch([{ method: 'worktree.show', params: { worktree: `id:${id}` } }]);
    return res?.worktree ?? null;
  },
  async rename(id, name) {
    const [res] = await rpcBatch([
      { method: 'worktree.set', params: { worktree: `id:${id}`, displayName: name, firstAgentMessageRenameError: null } }
    ]);
    return res;
  }
};
