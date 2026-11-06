#!/usr/bin/env python3
"""On-demand read-only audit. No prompts, restarts, mailbox reads or recovery."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

VOLUME = Path(os.environ.get('AI_VOLUME_PATH', '/Volumes/AI_Workspace'))
UUID = os.environ.get('AI_VOLUME_UUID', 'AUTO')
ORCA = os.environ.get('ORCA_CLI_COMMAND', '/Applications/Orca.app/Contents/Resources/bin/orca')
EXTRA_PATH = f"{Path.home() / '.local/bin'}:/Library/Frameworks/Python.framework/Versions/3.14/bin:{os.environ.get('PATH', '')}"


def which_tool(name):
    return shutil.which(name, path=EXTRA_PATH)


def command(argv, timeout=20):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           cwd=Path.home())
        if r.returncode:
            return {'status': 'unknown', 'reason': 'command_failed', 'exit_code': r.returncode}
        return {'status': 'ok', 'stdout': r.stdout}
    except subprocess.TimeoutExpired:
        return {'status': 'unknown', 'reason': 'timeout'}
    except OSError:
        return {'status': 'unknown', 'reason': 'unavailable'}


def orca(args):
    r = command([ORCA, *args, '--json'])
    if r['status'] != 'ok':
        return r
    try:
        data = json.loads(r['stdout'])
        if data.get('ok') is False:
            return {'status': 'unknown', 'reason': 'orca_error'}
        return {'status': 'ok', 'data': data.get('result', data)}
    except (ValueError, AttributeError):
        return {'status': 'unknown', 'reason': 'invalid_json'}


def project_worktrees(data):
    rows = data['worktrees']
    if not isinstance(rows, list):
        raise ValueError('worktrees schema')
    agents = [a for w in rows for a in w.get('agents', [])]
    return {
        'workspaces': len(rows),
        'truncated': data.get('truncated', 'unknown'),
        'live_terminals': sum(w.get('liveTerminalCount', 0) for w in rows),
        'unverifiable_terminals': sum(w.get('unverifiableTerminalCount', 0) for w in rows),
        'agent_observations': len(agents),
        'agent_states': dict(Counter(a.get('state', 'unknown') for a in agents)),
        'agent_types': dict(Counter(a.get('agentType', 'unknown') for a in agents)),
        'note': 'Observations include retained sessions; done is not acceptance or proof of process exit.',
    }


def project(name, result):
    if result['status'] != 'ok':
        return result
    d = result['data']
    try:
        if name == 'runtime':
            r = {'running': d['app']['running'], 'state': d['runtime']['state'],
                 'version': d['runtime'].get('appVersion')}
        elif name == 'workspaces':
            r = project_worktrees(d)
        elif name == 'hooks':
            r = {'enabled': d['enabled'], 'agents': [
                {'agent': s['agent'], 'state': s['state']}
                for s in d['statuses'] if s['state'] != 'not_installed']}
        elif name == 'automations':
            r = {'count': len(d['automations'])}
        elif name == 'repositories':
            r = {'count': len(d['repos']), 'invalid_node_setups': [
                {'project': x.get('displayName'), 'reason': 'pnpm install without package.json'}
                for x in d['repos'] if
                x.get('hookSettings', {}).get('scripts', {}).get('setup', '').strip() == 'pnpm install'
                and Path(x['path']).is_dir() and not (Path(x['path']) / 'package.json').exists()]}
        else:
            raise ValueError('unrecognized projection')
        return {'status': 'ok', **r}
    except (KeyError, TypeError, ValueError):
        return {'status': 'unknown', 'reason': 'schema_changed'}


def check_profile_parity():
    home = Path.home()
    orca_dir = home / 'Library/Application Support/orca'
    required_skills = {'agent-browser', 'mole', 'orca-cli', 'orca-computer-use', 'orca-emulator', 'orchestration'}

    agy_missing = {}
    for label, p in [
        ('antigravity_chat_config', orca_dir / 'antigravity-chat/config/skills'),
        ('antigravity_chat_cli', orca_dir / 'antigravity-chat/antigravity-cli/skills'),
        ('gemini_config', home / '.gemini/config/skills'),
    ]:
        present = {x.name for x in p.iterdir()} if p.is_dir() else set()
        missing = sorted(required_skills - present)
        if missing:
            agy_missing[label] = missing

    claude_profiles = []
    for p in sorted((orca_dir / 'claude-profiles').glob('*/home')):
        pid = p.parent.name
        has_claude_md = (p / 'CLAUDE.md').exists()
        has_skills = (p / 'skills').is_dir()
        has_xcode_mcp = False
        cj = p / '.claude.json'
        if cj.is_file():
            try:
                has_xcode_mcp = 'xcode' in (json.loads(cj.read_text(encoding='utf-8')).get('mcpServers') or {})
            except Exception:
                has_xcode_mcp = False
        claude_profiles.append({
            'profile_id': pid,
            'has_claude_md': has_claude_md,
            'has_skills': has_skills,
            'has_xcode_mcp': has_xcode_mcp,
            'ok': has_claude_md and has_skills and has_xcode_mcp,
        })

    codex_accounts = []
    for p in sorted((orca_dir / 'codex-accounts').glob('*/home')):
        aid = p.parent.name
        has_agents_md = (p / 'AGENTS.md').exists()
        has_hooks_json = (p / 'hooks.json').is_file()
        cfg = p / 'config.toml'
        has_xcode_mcp = cfg.is_file() and ('[mcp_servers.xcode]' in cfg.read_text(encoding='utf-8'))
        skill_dir = p / 'skills'
        present_skills = {x.name for x in skill_dir.iterdir()} if skill_dir.is_dir() else set()
        missing_skills = sorted(required_skills - present_skills)
        codex_accounts.append({
            'account_id': aid,
            'has_agents_md': has_agents_md,
            'has_hooks_json': has_hooks_json,
            'has_xcode_mcp': has_xcode_mcp,
            'missing_shared_skills': missing_skills,
            'ok': has_agents_md and has_hooks_json and has_xcode_mcp and not missing_skills,
        })

    all_ok = (
        not agy_missing
        and all(x['ok'] for x in claude_profiles)
        and all(x['ok'] for x in codex_accounts)
        and (home / '.zshenv').is_file()
    )
    return {
        'status': 'ok',
        'all_aligned': all_ok,
        'zshenv_present': (home / '.zshenv').is_file(),
        'antigravity_missing_skills': agy_missing,
        'claude_profiles': claude_profiles,
        'codex_accounts': codex_accounts,
    }


def check_git_optimizations():
    keys = [
        'feature.manyfiles',
        'core.fsmonitor',
        'rerere.enabled',
        'worktree.userelativepaths',
        'branch.sort',
    ]
    r = command(['git', 'config', '--global', '-l'])
    if r['status'] != 'ok':
        return {'status': 'unknown', 'reason': r.get('reason', 'git_config_failed')}
    kv = {}
    for line in r['stdout'].splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            kv[k.strip().lower()] = v.strip()
    configured = {k: kv.get(k) for k in keys}
    return {
        'status': 'ok',
        'all_enabled': all(bool(configured.get(k)) for k in keys),
        'settings': configured,
    }


def check_simulator_coordination():
    coord_dir = VOLUME / 'AI/Data/SimulatorCoordination'
    if not coord_dir.is_dir():
        return {'status': 'ok', 'present': False}
    active_dirs = sorted(p.name for p in coord_dir.glob('active-*') if p.is_dir())
    zero_byte_locks = sorted(
        p.name for p in coord_dir.glob('*.lock') if p.is_file() and p.stat().st_size == 0
    )
    return {
        'status': 'ok',
        'present': True,
        'active_lease_dirs': active_dirs,
        'historical_zero_byte_locks_count': len(zero_byte_locks),
        'note': 'Read-only check; never auto-expires or deletes locks.',
    }


def collect():
    jobs = {'runtime': ['status'], 'workspaces': ['worktree', 'ps'],
            'hooks': ['agent', 'hooks', 'status'], 'repositories': ['repo', 'list'],
            'automations': ['automations', 'list']}
    result = {'schema_version': 2, 'observed_at': datetime.now(timezone.utc).isoformat(),
              'mode': 'read-only', 'actions_taken': [], 'orca_executable': ORCA}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {k: pool.submit(orca, v) for k, v in jobs.items()}
        result.update({k: project(k, f.result()) for k, f in futures.items()})
    for name, args in {
        'macos': ['sw_vers', '-productVersion'], 'xcode': ['xcodebuild', '-version'],
        'swap': ['sysctl', '-n', 'vm.swapusage'], 'memory_pressure': ['memory_pressure', '-Q'],
    }.items():
        result[name] = command(args)
    result['cli_paths'] = {
        k: which_tool(k)
        for k in ['claude', 'codex', 'dsh', 'gemini', 'opencode', 'gh']
    }
    result['cli_toolchain'] = {
        k: which_tool(k)
        for k in ['rg', 'fd', 'ast-grep', 'just', 'xcbeautify', 'scc', 'repomix', 'agent-guard']
    }
    result['git_optimizations'] = check_git_optimizations()
    result['profile_parity'] = check_profile_parity()
    result['simulator_coordination'] = check_simulator_coordination()
    result['interpretation'] = [
        'Unknown and timeout never authorize restart, retry, input or takeover.',
        'Swap allocation alone does not establish current memory pressure.',
        'No quota balances, credentials, prompts, terminal output or mailbox messages are collected.',
    ]
    return result


def save(path, payload):
    # Only a report under this project can be written, and only on the expected volume.
    if str(VOLUME).startswith('/Volumes/') and UUID and UUID != 'AUTO':
        info = subprocess.check_output(['diskutil', 'info', '-plist', str(VOLUME)], timeout=10)
        if plistlib.loads(info).get('VolumeUUID') != UUID:
            raise RuntimeError('Wrong or missing external volume')
    path = Path(path).resolve()
    root = (VOLUME / 'AI/Projects/Mac/reports').resolve()
    if root not in path.parents or not path.parent.is_dir():
        raise ValueError('Output must have an existing parent inside Mac/reports')
    # Exclusive creation preserves previous receipts, including after parallel runs.
    with path.open('x', encoding='utf-8') as f:
        f.write(payload)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', help='New JSON receipt under Mac/reports; never overwrite')
    args = p.parse_args()
    result = collect()
    payload = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        save(args.output, payload)
    print(payload, end='')
    return 2 if any(isinstance(v, dict) and v.get('status') == 'unknown' for v in result.values()) else 0


if __name__ == '__main__':
    sys.exit(main())
