import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createConnection } from 'node:net';
import { randomUUID } from 'node:crypto';
const run = promisify(execFile);
const CLI = process.env.ORCA_CLI_COMMAND || '/Applications/Orca.app/Contents/Resources/bin/orca';
const VOLUME = (process.env.AI_VOLUME_PATH || '/Volumes/AI_Workspace').replace(/\/+$/, '');
const EXPECTED_UUID = process.env.AI_VOLUME_UUID ?? 'AUTO';

export async function cli(args) {
  const { stdout } = await run(CLI, [...args, '--json'], { timeout: 20000, maxBuffer: 16 * 1024 * 1024, cwd: homedir() });
  const result = JSON.parse(stdout);
  if (!result.ok) throw new Error(result.error?.code ?? 'orca_command_failed');
  return result.result;
}
export async function verifyHost() {
  const { stdout } = await run('/usr/libexec/PlistBuddy', ['-c', 'Print :CFBundleShortVersionString', '/Applications/Orca.app/Contents/Info.plist']);
  if (!stdout.trim().startsWith('1.4.')) throw new Error('unsupported_orca_version');
  if (VOLUME.startsWith('/Volumes/') && EXPECTED_UUID && EXPECTED_UUID !== 'AUTO') {
    const { stdout: volume } = await run('/usr/sbin/diskutil', ['info', '-plist', VOLUME]);
    if (!volume.includes(`<string>${EXPECTED_UUID}</string>`)) throw new Error('wrong_or_missing_volume');
  }
}
// Internal read-only compatibility adapter. Never log or persist the runtime auth token.
export async function tabSnapshots() {
  const meta = JSON.parse(await readFile(join(homedir(), 'Library/Application Support/Orca/orca-runtime.json'), 'utf8'));
  const endpoint = (meta.transports ?? [meta.transport]).find(t => t?.kind === 'unix')?.endpoint;
  if (typeof endpoint !== 'string' || !endpoint.startsWith('/') || !meta.authToken) throw new Error('runtime_unavailable');
  const id = randomUUID();
  return new Promise((resolve, reject) => {
    const socket = createConnection(endpoint);
    let buffer = '';
    const fail = e => { socket.destroy(); reject(new Error(e?.message === 'incompatible_tab_schema' ? e.message : 'tab_snapshot_unavailable')); };
    socket.setTimeout(15000, () => fail());
    socket.on('error', fail);
    socket.on('end', () => fail());
    socket.on('connect', () => socket.write(JSON.stringify({ id, authToken: meta.authToken, method: 'session.tabs.listAll', params: {} }) + '\n'));
    socket.on('data', data => {
      buffer += data.toString();
      if (buffer.length > 16 * 1024 * 1024) return fail();
      let end;
      while ((end = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
        try {
          const d = JSON.parse(line);
          if (d.id !== id) continue;
          if (!d.ok || !Array.isArray(d.result?.snapshots)) throw new Error('incompatible_tab_schema');
          const snapshots = d.result.snapshots.map(s => {
            if (typeof s.worktree !== 'string' || !Array.isArray(s.tabs)) throw new Error('incompatible_tab_schema');
            return { worktree: s.worktree, tabs: s.tabs.filter(t => t.type === 'agent-session').map(t => ({ type: t.type, title: t.title })) };
          });
          socket.removeAllListeners('end'); socket.destroy(); resolve(snapshots);
        } catch (e) { fail(e); }
      }
    });
  });
}
export const adapter = {
  async snapshot() {
    await verifyHost();
    const [w, r, snapshots] = await Promise.all([cli(['worktree', 'list', '--limit', '10000']), cli(['repo', 'list']), tabSnapshots()]);
    if (!Array.isArray(w.worktrees) || !Array.isArray(r.repos)) throw new Error('incompatible_workspace_schema');
    return { worktrees: w.worktrees, repos: r.repos.map(r => ({ id: r.id, displayName: r.displayName })), snapshots };
  },
  async current(id) { return (await cli(['worktree', 'list', '--limit', '10000'])).worktrees.find(w => w.id === id); },
  async rename(id, name) { return cli(['worktree', 'set', '--worktree', `id:${id}`, '--display-name', name]); }
};
