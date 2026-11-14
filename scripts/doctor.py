#!/usr/bin/env python3
"""On-demand read-only audit. No prompts, restarts, mailbox reads or recovery.

The report ends with `summary`: `attention` lists what is broken or will silently stop protecting
the machine (backups that no longer succeed, an unreachable NAS, an inherited host tag, stale
deployed scripts...), `advice` lists hardening that is worth doing but not urgent.
Exit code: 0 healthy, 1 something needs attention, 2 a check could not run."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'scripts'))
from setup_backups import local_host_name, machine_id, resolve_host_tag  # noqa: E402


def _read_env_file(cfg):
    vals = {}
    if cfg.is_file():
        for line in cfg.read_text(encoding='utf-8', errors='ignore').splitlines():
            line = line.strip()
            if line.startswith('export '):
                line = line[7:].strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                vals.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return vals


def _load_configured_env():
    vals = _read_env_file(Path.home() / '.orca/backup-config.env')
    for k, v in _read_env_file(REPO_ROOT / 'config.env').items():
        vals.setdefault(k, v)
    return vals


_CFG_ENV = _load_configured_env()
_STORAGE_MODE = (os.environ.get('STORAGE_MODE') or _CFG_ENV.get('STORAGE_MODE') or 'auto').strip().lower()
if _STORAGE_MODE == 'internal':
    _raw_vol = (
        os.environ.get('INTERNAL_WORKSPACE_PATH')
        or _CFG_ENV.get('INTERNAL_WORKSPACE_PATH')
        or str(Path.home() / 'AI-Workspace')
    )
    VOLUME = Path(_raw_vol.replace('$HOME', str(Path.home())).replace('${HOME}', str(Path.home()))).expanduser()
    UUID = 'LOCAL'
else:
    VOLUME = Path(os.environ.get('AI_VOLUME_PATH') or _CFG_ENV.get('AI_VOLUME_PATH') or '/Volumes/AI_Workspace')
    UUID = os.environ.get('AI_VOLUME_UUID') or _CFG_ENV.get('AI_VOLUME_UUID') or 'AUTO'
ORCA = os.environ.get('ORCA_CLI_COMMAND', '/Applications/Orca.app/Contents/Resources/bin/orca')
EXTRA_PATH = f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}"
STORAGE_ROOT = Path.home() / 'Library/Application Support/Codex Storage'
# PATH of the backup LaunchAgents (scripts/codex-storage/lib.sh)
JOB_PATH = f"{Path.home() / '.local/bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# job -> hours it may go without a successful run before it is reported (None: event driven)
BACKUP_JOBS = {
    'volume-guard': None,
    'backup-hot': 24,
    'icloud-package': 48,
    'backup-projects': 48,
    'backup-media': 9 * 24,
    'maintenance': 9 * 24,
    'restore-drill': 9 * 24,
    'agent-hygiene': 48,
}
STATUS_TIME_FORMAT = '%Y-%m-%d %H:%M:%S'


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
    required_skills = {'agent-browser', 'agent-governor', 'mole', 'orca-cli', 'orca-computer-use', 'orca-emulator', 'orchestration'}

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


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def hours_since(stamp):
    try:
        return (time.time() - time.mktime(time.strptime(stamp, STATUS_TIME_FORMAT))) / 3600
    except (TypeError, ValueError, OverflowError):
        return None


def launchd_loaded(label):
    try:
        return subprocess.run(['/bin/launchctl', 'print', f'gui/{os.getuid()}/{label}'],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def check_backup_health():
    """Do the installed backup jobs still succeed? Reads the receipts the jobs write themselves."""
    la_dir = Path.home() / 'Library/LaunchAgents'
    jobs, attention = {}, []
    for job, limit in BACKUP_JOBS.items():
        plists = sorted(la_dir.glob(f'local.*.{job}.plist')) if la_dir.is_dir() else []
        if not plists:
            jobs[job] = {'installed': False}
            continue
        receipt = read_json(STORAGE_ROOT / 'status' / f'{job}.json')
        state = receipt.get('state') or 'never-run'
        message = str(receipt.get('message') or '')[:200]
        last_ok = receipt.get('last_ok') or (receipt.get('time') if state in ('ok', 'warn') else None)
        ok_hours = hours_since(last_ok)
        loaded = launchd_loaded(plists[0].stem)
        jobs[job] = {
            'installed': True, 'loaded': loaded, 'state': state, 'last_run': receipt.get('time'),
            'last_ok': last_ok, 'hours_since_ok': None if ok_hours is None else round(ok_hours, 1),
            'message': message,
        }
        detail = f'{state}: {message}' if message else state
        if not loaded:
            attention.append(f'Backup job {job} is installed but not loaded in launchd '
                             f'(run ./deploy.sh --setup-backups).')
        elif state in ('fail', 'stale', 'absent'):
            attention.append(f'Backup job {job} is {detail}')
        elif limit and ok_hours is not None and ok_hours > limit:
            attention.append(f'Backup job {job} has not succeeded for {int(ok_hours)} h (last run {detail})')
        elif limit and ok_hours is None and state not in ('never-run', 'running'):
            attention.append(f'Backup job {job} has no successful run on record (last run {detail})')
    return {'status': 'ok', 'jobs': jobs, 'attention': attention}


def check_nas():
    """Restic targets a NAS share in most setups; an unmounted share makes every run skip quietly."""
    cfg = _read_env_file(Path.home() / '.orca/backup-config.env')
    match = cfg.get('BACKUP_NAS_SMB_MATCH', '')
    if not match:
        return {'status': 'ok', 'configured': False}
    spec = match.split(' on ')[0].strip()
    host = spec.split('@')[-1].lstrip('/').split('/')[0]
    try:
        mounts = subprocess.run(['/sbin/mount'], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {'status': 'unknown', 'reason': 'mount_unavailable'}
    mounted = match in mounts
    try:
        saved = subprocess.run(['/usr/bin/security', 'find-internet-password', '-s', host],
                               capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        saved = False
    attention = []
    if not mounted:
        hint = ('macOS has a saved credential, so the jobs remount it once the NAS is reachable '
                '(same network or VPN)' if saved else
                'no SMB credential is saved, so the jobs cannot mount it themselves: connect to the '
                f'NAS network, open Finder > Go > Connect to Server, enter smb:{spec} and tick '
                '"Remember this password in my keychain"')
        attention.append(f'NAS share smb:{spec} is not mounted; Restic backups skip until it is. {hint}.')
    return {'status': 'ok', 'configured': True, 'share': f'smb:{spec}', 'mounted': mounted,
            'saved_credential': saved, 'attention': attention}


def check_host_tag():
    """A tag inherited from the Mac this one was migrated from mixes both machines' backups."""
    cfg = _read_env_file(Path.home() / '.orca/backup-config.env')
    if not cfg:
        return {'status': 'ok', 'configured': False}
    marker = read_json(Path.home() / '.orca/.migrated-from')
    configured = cfg.get('BACKUP_HOST_TAG', '')
    current = local_host_name()
    source = str(marker.get('source_local_hostname') or marker.get('source_hostname') or '')
    keep = cfg.get('BACKUP_HOST_TAG_KEEP', '').lower() in ('1', 'true', 'yes')
    expected, why = resolve_host_tag(configured, current, source, keep=keep,
                                     source_machine=str(marker.get('source_machine_id') or ''),
                                     this_machine=machine_id())
    effective = configured or current
    attention = []
    if effective != expected:
        attention.append(
            f"Backups are published as '{effective}', the name of the Mac this one was restored from "
            f"({source.split('.')[0]}); both Macs then share and prune one history. Run "
            f"./deploy.sh --setup-backups to switch to '{expected}', or set BACKUP_HOST_TAG_KEEP=true "
            'to keep the old name on purpose.')
    return {'status': 'ok', 'configured': True, 'host_tag': effective, 'this_mac': current,
            'migrated_from': source or None, 'expected': expected, 'reason': why, 'attention': attention}


def volume_encrypted(vol):
    try:
        info = plistlib.loads(subprocess.check_output(
            ['/usr/sbin/diskutil', 'info', '-plist', str(vol)], stderr=subprocess.DEVNULL, timeout=10))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if 'FileVault' not in info and 'Encryption' not in info:
        return None
    return bool(info.get('FileVault') or info.get('Encryption'))


def _migration_candidate_volumes():
    seen = []
    backup_cfg = _read_env_file(Path.home() / '.orca/backup-config.env')
    repo_cfg = _read_env_file(REPO_ROOT / 'config.env')
    for candidate in (
        VOLUME,
        Path(repo_cfg['AI_VOLUME_PATH']) if repo_cfg.get('AI_VOLUME_PATH') else None,
    ):
        if candidate and str(candidate) not in seen:
            seen.append(str(candidate))
    media = (
        os.environ.get('BACKUP_MEDIA_PATH')
        or backup_cfg.get('BACKUP_MEDIA_PATH')
        or _CFG_ENV.get('BACKUP_MEDIA_PATH')
        or ''
    )
    if media:
        mp = Path(media)
        parts = mp.parts
        if len(parts) >= 3 and parts[0] == '/' and parts[1] == 'Volumes':
            ext_root = f'/Volumes/{parts[2]}'
            if ext_root not in seen:
                seen.append(ext_root)
        if mp.name == 'Work' and str(mp.parent) not in seen:
            seen.append(str(mp.parent))
    return [Path(p) for p in seen]


def check_migration_snapshot():
    snap_vol = VOLUME
    meta = snap_vol / 'AI/Migration/latest/metadata'
    manifest = read_json(meta / 'MANIFEST.json')
    if not manifest:
        for cand in _migration_candidate_volumes():
            cand_meta = cand / 'AI/Migration/latest/metadata'
            cand_manifest = read_json(cand_meta / 'MANIFEST.json')
            if cand_manifest:
                snap_vol, meta, manifest = cand, cand_meta, cand_manifest
                break
    if not manifest:
        return {'status': 'ok', 'present': False}
    state = read_json(meta / 'PACK_STATE.json').get('state', 'unknown')
    src_host = str(manifest.get('source_local_hostname')
                   or str(manifest.get('source_hostname') or '').split('.')[0])
    src_machine = str(manifest.get('source_machine_id') or '')
    packed_here = (src_machine == machine_id()) if src_machine else (src_host == local_host_name())
    try:
        packed = datetime.fromisoformat(str(manifest.get('packed_at')))
        age_days = round((datetime.now(timezone.utc) - packed).total_seconds() / 86400, 1)
    except (TypeError, ValueError):
        age_days = None
    holds_secrets = (meta / 'keychain-secrets.json').is_file()
    encrypted = volume_encrypted(snap_vol) if str(snap_vol).startswith('/Volumes/') else None
    attention, advice = [], []
    if state != 'complete':
        attention.append(f"Migration snapshot is '{state}', not complete: a restore would be refused. "
                         'Run ./deploy.sh --pack-to-ssd on the source Mac again.')
    if holds_secrets and encrypted is False:
        advice.append(f'The migration snapshot on {snap_vol} keeps Keychain secrets and SSH keys as plain '
                      'files and the volume is not encrypted. Encrypt the volume (Finder > right-click '
                      '> Encrypt), or delete AI/Migration/latest once the new Mac is verified.')
    if not packed_here:
        advice.append(f'The migration snapshot still belongs to {src_host or "another Mac"}. Packing from '
                      'this Mac replaces it and needs ./deploy.sh --pack-to-ssd --force once.')
    return {'status': 'ok', 'present': True, 'volume': str(snap_vol), 'pack_state': state, 'source_host': src_host or None,
            'packed_on_this_mac': packed_here, 'age_days': age_days, 'holds_plaintext_secrets': holds_secrets,
            'volume_encrypted': encrypted, 'attention': attention, 'advice': advice}


def check_deployment_drift():
    """Installed copies that differ from this checkout keep running the old behaviour."""
    src = REPO_ROOT / 'scripts/codex-storage'
    dst = STORAGE_ROOT / 'agents'
    stale_scripts = []
    if dst.is_dir() and src.is_dir():
        for f in sorted(src.iterdir()):
            if not f.is_file() or f.name.endswith('.swift') or f.name.startswith('.'):
                continue
            installed = dst / f.name
            if not installed.is_file() or file_sha256(installed) != file_sha256(f):
                stale_scripts.append(f.name)
    guard_src = REPO_ROOT / 'scripts/guard.py'
    guard_dst = Path.home() / '.local/bin/agent-guard'
    guard_current = guard_dst.is_file() and guard_src.is_file() and file_sha256(guard_dst) == file_sha256(guard_src)
    gov_src = REPO_ROOT / 'scripts/agent_governor.py'
    gov_dst = Path.home() / '.local/bin/agent-governor'
    gov_current = gov_dst.is_file() and gov_src.is_file() and file_sha256(gov_dst) == file_sha256(gov_src)
    attention = []
    if stale_scripts:
        attention.append(f'{len(stale_scripts)} installed backup script(s) differ from this checkout '
                         f'({", ".join(stale_scripts)}); run ./deploy.sh --setup-backups.')
    if guard_dst.exists() and not guard_current:
        attention.append('~/.local/bin/agent-guard differs from scripts/guard.py; run ./deploy.sh --apply.')
    if gov_dst.exists() and not gov_current:
        attention.append('~/.local/bin/agent-governor differs from scripts/agent_governor.py; run ./deploy.sh --apply.')
    guard_env = (Path.home() / '.orca/guard.env').is_file()
    if guard_current and not guard_env:
        attention.append('~/.orca/guard.env is missing, so the branch rules and simulator IDs of config.env do not '
                         'reach the installed agent-guard; run ./deploy.sh --apply.')
    return {'status': 'ok', 'backups_installed': dst.is_dir(), 'stale_backup_scripts': stale_scripts,
            'agent_guard_installed': guard_dst.is_file(), 'agent_guard_current': guard_current,
            'agent_governor_installed': gov_dst.is_file(), 'agent_governor_current': gov_current,
            'agent_guard_config': guard_env, 'attention': attention}


def check_guard_hooks():
    """Is the PreToolUse guard wired into each agent that has a hook file?"""
    home = Path.home()
    files = {
        'claude': home / '.claude/settings.json',
        'codex': home / '.codex/hooks.json',
        'antigravity': home / '.gemini/config/hooks.json',
    }
    wired, attention = {}, []
    enabled = (os.environ.get('ENABLE_PRETOOLUSE_GUARDRAILS')
               or _CFG_ENV.get('ENABLE_PRETOOLUSE_GUARDRAILS') or 'true').lower() == 'true'
    for agent, path in files.items():
        if not path.is_file():
            continue
        try:
            wired[agent] = 'agent-guard' in path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            wired[agent] = False
        if enabled and not wired[agent]:
            attention.append(f'The agent-guard hook is not wired into {agent} ({path}); '
                             'run ./deploy.sh --sync-profiles.')
    return {'status': 'ok', 'enabled': enabled, 'wired': wired, 'attention': attention,
            'note': 'A guardrail against known destructive commands, not a sandbox.'}


def check_job_python():
    """The backup jobs call a bare python3; on a Mac without one they fail on every run."""
    py = shutil.which('python3', path=JOB_PATH)
    usable = False
    if py:
        stub = os.path.realpath(py).startswith('/usr/bin/') and subprocess.run(
            ['/usr/bin/xcode-select', '-p'], capture_output=True).returncode != 0
        if not stub:
            try:
                usable = subprocess.run([py, '-c', 'import ssl, sqlite3, plistlib'],
                                        capture_output=True, timeout=30).returncode == 0
            except (OSError, subprocess.SubprocessError):
                usable = False
    attention = [] if usable else [
        'No working python3 on the PATH of the background jobs (~/.local/bin, /opt/homebrew/bin, '
        '/usr/local/bin, /usr/bin). Install one: xcode-select --install, python.org, or uv.']
    return {'status': 'ok', 'python3': py, 'usable': usable, 'attention': attention}


def check_third_party_agents():
    """LaunchAgents that are not part of this blueprint and what launchd will do with them."""
    try:
        from migrate import launchd_disabled_labels, third_party_agents
    except Exception:
        return {'status': 'unknown', 'reason': 'migrate_module_unavailable'}
    uid = os.getuid()
    disabled = launchd_disabled_labels(uid)
    groups = {'running': [], 'held': [], 'starts_at_next_login': []}
    for label, _ in third_party_agents(Path.home() / 'Library/LaunchAgents'):
        if label in disabled:
            groups['held'].append(label)
        elif launchd_loaded(label):
            groups['running'].append(label)
        else:
            groups['starts_at_next_login'].append(label)
    advice = []
    if groups['starts_at_next_login']:
        advice.append('Not running now but launchd starts them at the next login: '
                      f'{", ".join(groups["starts_at_next_login"])}. Hold the ones you do not want with '
                      './deploy.sh --hold-agents <label,label>.')
    return {'status': 'ok', **groups, 'advice': advice}


def summarize(result):
    attention, advice = [], []
    unknown = sorted(k for k, v in result.items() if isinstance(v, dict) and v.get('status') == 'unknown')
    if unknown:
        attention.append(f'Could not be checked: {", ".join(unknown)} (is Orca running?).')
    for value in result.values():
        if isinstance(value, dict):
            attention.extend(value.get('attention') or [])
            advice.extend(value.get('advice') or [])
    parity = result.get('profile_parity') or {}
    if parity.get('status') == 'ok' and not parity.get('all_aligned'):
        attention.append('Agent profiles are not aligned (skills, rules or MCP differ); '
                         'run ./deploy.sh --sync-profiles.')
    missing = sorted(k for k, v in (result.get('cli_toolchain') or {}).items() if not v)
    if missing:
        advice.append(f'CLI tools not installed: {", ".join(missing)} (./deploy.sh --apply installs them).')
    bad_setups = (result.get('repositories') or {}).get('invalid_node_setups') or []
    if bad_setups:
        advice.append('Orca setup script runs `pnpm install` in a repository without package.json: '
                      + ', '.join(str(x.get('project')) for x in bad_setups) + '.')
    return {'ok': not attention, 'attention': attention, 'advice': advice}


def render_summary(summary):
    lines = []
    if summary['attention']:
        lines.append(f"Health check: {len(summary['attention'])} item(s) need attention")
        lines += [f'  {i}. {text}' for i, text in enumerate(summary['attention'], 1)]
    else:
        lines.append('Health check: OK, nothing needs attention')
    if summary['advice']:
        lines.append('Advice:')
        lines += [f'  - {text}' for text in summary['advice']]
    return '\n'.join(lines) + '\n'


def check_automated_backups():
    home = Path.home()
    agents_dir = STORAGE_ROOT / 'agents'
    la_dir = home / 'Library/LaunchAgents'
    backup_cfg = home / '.orca/backup-config.env'
    expected_scripts = [
        'lib.sh',
        'icloud-package.sh',
        'backup-restic.sh',
        'maintenance.sh',
        'restore-drill.sh',
        'agent-hygiene.sh',
        'volume-guard.sh',
        'volume-observer.py',
        'sqlite_snapshot.py',
        'status.sh',
    ]
    present_scripts = [s for s in expected_scripts if (agents_dir / s).is_file()]
    job_suffixes = list(BACKUP_JOBS)
    present_plists = []
    if la_dir.is_dir():
        for suffix in job_suffixes:
            matches = list(la_dir.glob(f'local.*.{suffix}.plist'))
            if matches:
                present_plists.append(matches[0].name)
    storage_mode = 'external' if str(VOLUME).startswith('/Volumes/') else 'internal'
    return {
        'status': 'ok',
        'storage_mode': storage_mode,
        'workspace_root': str(VOLUME),
        'backup_config_present': backup_cfg.is_file(),
        'installed_scripts_count': len(present_scripts),
        'installed_launchagents_count': len(present_plists),
        'launchagents': present_plists,
        'all_ready': len(present_scripts) == len(expected_scripts) and len(present_plists) == len(job_suffixes),
        'note': 'all_ready only says the jobs are installed; backup_health says whether they succeed.',
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
        for k in ['rg', 'fd', 'ast-grep', 'just', 'xcbeautify', 'scc', 'repomix', 'restic', 'agent-guard']
    }
    result['git_optimizations'] = check_git_optimizations()
    result['profile_parity'] = check_profile_parity()
    result['simulator_coordination'] = check_simulator_coordination()
    result['automated_backups'] = check_automated_backups()
    result['backup_health'] = check_backup_health()
    result['nas'] = check_nas()
    result['backup_host_tag'] = check_host_tag()
    result['migration_snapshot'] = check_migration_snapshot()
    result['deployment_drift'] = check_deployment_drift()
    result['guard_hooks'] = check_guard_hooks()
    result['job_python'] = check_job_python()
    result['third_party_launch_agents'] = check_third_party_agents()
    result['interpretation'] = [
        'Unknown and timeout never authorize restart, retry, input or takeover.',
        'Swap allocation alone does not establish current memory pressure.',
        'No quota balances, credentials, prompts, terminal output or mailbox messages are collected.',
    ]
    result['summary'] = summarize(result)
    return result


def save(path, payload):
    # Only a report under this project can be written, and only on the expected volume.
    if str(VOLUME).startswith('/Volumes/') and UUID and UUID not in ('AUTO', 'LOCAL', 'INTERNAL'):
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
    p.add_argument('--summary', action='store_true', help='Print only the attention/advice lists as text')
    args = p.parse_args()
    result = collect()
    payload = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        save(args.output, payload)
    print(render_summary(result['summary']) if args.summary else payload, end='')
    if any(isinstance(v, dict) and v.get('status') == 'unknown' for v in result.values()):
        return 2
    return 0 if result['summary']['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
