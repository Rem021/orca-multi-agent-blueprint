#!/usr/bin/env python3
"""
Orca Multi-Agent Blueprint — Universal Lossless Cross-Mac Migration Engine (migrate.py)

Packs all internal-disk AI Agent states, SQLite session/orchestration databases,
multi-account profiles, CLI toolchains, Node/Python runtimes, core .app bundles,
Xcode signing profiles, iOS pairing records, SSH/Git/Zsh configs, LaunchAgents,
and local repositories onto an external SSD (${AI_VOLUME_PATH}/AI/Migration/latest),
and restores them losslessly onto a new Mac without copying bulky VMs
(skips Claude Desktop claudevm.bundle / claude-code-vm and CoreSimulator/Devices).

Universal features for any Mac developer:
- Auto-discovers custom LaunchAgents, their ~/Library/Application Support/* folders,
  live SQLite databases (verified by magic header), and Keychain service names.
- Parallel APFS Copy-on-Write (cp -c) SQLite snapshots + offline WAL truncation.
- Fingerprint-cached incremental mirroring for .app bundles & Python.framework.
- Automatic cross-Mac username path & symlink relocation (/Users/<old> -> /Users/<new>).
- Bundles the blueprint itself + AGENT_RESTORE.md onto the SSD for 100% offline restore.

Usage:
  python3 scripts/migrate.py pack --dry-run
  python3 scripts/migrate.py pack --apply
  python3 scripts/migrate.py restore --dry-run
  python3 scripts/migrate.py restore --apply
  python3 scripts/migrate.py status
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time

SQLITE_MAGIC_HEADER = b"SQLite format 3\x00"


def detect_volume() -> Path:
    env_vol = os.environ.get("AI_VOLUME_PATH")
    if env_vol and Path(env_vol).exists():
        return Path(env_vol)
    volumes = Path("/Volumes")
    skip = {"Macintosh HD", "Macintosh HD - Data", "Preboot", "Recovery", "VM", "Update"}
    if volumes.is_dir():
        for v in sorted(volumes.iterdir()):
            if v.name in skip or v.name.startswith("Macintosh HD"):
                continue
            if v.is_dir() and (v / "AI").is_dir():
                return v
    return Path.home() / "AI-Workspace"


def get_volume_uuid(vol: Path) -> str:
    if not str(vol).startswith("/Volumes/"):
        return "LOCAL"
    try:
        out = subprocess.check_output(
            ["/usr/sbin/diskutil", "info", "-plist", str(vol)],
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        info = plistlib.loads(out)
        return info.get("VolumeUUID", "UNKNOWN")
    except Exception:
        return "UNKNOWN"


# Universal relative paths under $HOME to migrate (Zero-VM: excludes claudevm.bundle & CoreSimulator)
HOME_RELATIVE_TARGETS = [
    # 1. Core AI Agent States, Sessions & Multi-Account Pools
    ".claude",
    ".claude.json",
    ".claude.json.backup",
    ".codex",
    ".gemini",
    ".agents",
    ".orca",
    ".dsh",
    ".dsh-tui",
    ".grokbot",
    ".hermes",
    ".agent-browser",
    "Library/Application Support/orca",
    "Library/Application Support/Claude",
    "Library/Application Support/Codex",
    "Library/Application Support/Codex Storage",
    "Library/Application Support/com.openai.chat",
    "Library/Application Support/com.openai.codex",
    "Library/Application Support/OpenAI",
    "Library/Application Support/Antigravity",
    "Library/Application Support/Grok Bot",
    "Library/Application Support/@deepseek-ai",
    "Library/Application Support/com.meta.endo",
    "Library/Application Support/com.meta.prototypes.endo.tokens",
    "Library/Application Support/cc-same",
    "Library/Application Support/com.tw93.MoleApp",
    # 2. Native CLI Toolchain, Node Runtime, Shell History, Git, SSH & LaunchAgents
    ".local",
    "opt",
    ".config",
    ".ssh",
    ".gitconfig",
    ".zshrc",
    ".zshenv",
    ".zprofile",
    ".zsh_history",
    ".zsh_sessions",
    ".python_history",
    ".viminfo",
    ".lldb",
    ".pymobiledevice3",
    "Library/LaunchAgents",
    # 3. Xcode Signing Profiles, Keychains Backup & Core Agent Preferences
    "Library/Developer/Xcode/UserData",
    "Library/Application Support/com.apple.dt.Xcode",
    "Library/Keychains/login.keychain-db",
    "Library/Preferences/com.stablyai.orca.plist",
    "Library/Preferences/com.openai.codex.plist",
    "Library/Preferences/com.anthropic.claudefordesktop.plist",
    "Library/Preferences/io.tailscale.ipn.macsys.plist",
    # 4. Internal-Disk Local Repositories, Harnesses & User Apps
    "Developer",
    "Documents/deepseek-harness",
    "Applications",
]

# Universal AI & developer desktop .app bundles in /Applications
CORE_APP_BUNDLES = [
    "Orca.app",
    "Muse.app",
    "Claude.app",
    "ChatGPT.app",
    "Antigravity.app",
    "DeepSeek Harness.app",
    "Grok Bot.app",
    "CC Same.app",
    "Cursor.app",
    "Ghostty.app",
    "Tailscale.app",
    "Mole.app",
]

# Ephemeral caches, VMs, locks, and WAL files excluded from rsync
RSYNC_EXCLUDES = [
    "vm_bundles",
    "claudevm.bundle",
    "claude-code-vm",
    "CoreSimulator",
    "Cache",
    "Code Cache",
    "GPUCache",
    "GPUPersistentCache",
    "GraphiteDawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "Crashpad",
    "acp-temp",
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "orcad.lock",
    "home.lock",
    ".mole-update.lock",
    "daemon/*.sock",
    "daemon/*.pid",
    "daemon/*.token",
    "*.sock",
    "*.ipc",
    "*.db-wal",
    "*.db-shm",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "*.sqlite3-wal",
    "*.sqlite3-shm",
    "profile-state.db.backup.*.db",
    ".claude/shell-snapshots",
    ".claude/debug",
    ".codex/cache",
    ".codex/.tmp",
    ".codex/tmp",
    ".codex/node_repl",
    ".dsh/cache",
    ".dsh/profiles/*/node_modules",
    ".dsh-tui/compile-cache",
    "DerivedData",
    "Developer/build",
    ".DS_Store",
]

# Universal SQLite databases backed up via APFS CoW clone + WAL checkpoint
SQLITE_RELATIVE_TARGETS = [
    "Library/Application Support/orca/agent-session-journal.db",
    "Library/Application Support/orca/orchestration.db",
    "Library/Application Support/orca/profiles/local-default/profile-state.db",
    "Library/Application Support/orca/ai-vault/session-search.sqlite",
    "Library/Application Support/orca/ai-vault/session-parse-cache.sqlite",
    ".codex/thread_history_1.sqlite",
    ".codex/logs_2.sqlite",
    ".codex/state_5.sqlite",
    ".codex/memories_1.sqlite",
    ".codex/memories_v2_1.sqlite",
    ".codex/sqlite/codex.db",
    ".codex/sqlite/codex-dev.db",
    ".gemini/antigravity/conversation_summaries.db",
    "Library/Application Support/com.meta.endo/analytics/unified_micro_batch/FBAnalyticsUnifiedEventStore.sqlite",
    "Library/Application Support/Claude/declarative_performance_observer.db",
]


def is_valid_sqlite_file(path: Path) -> bool:
    """Verify file has the standard 16-byte SQLite 3 format header (skips encrypted/raw files)."""
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size < 100:
            return False
        with path.open("rb") as f:
            return f.read(16) == SQLITE_MAGIC_HEADER
    except OSError:
        return False


def discover_launchagent_dependencies(home: Path) -> tuple[list[str], list[str], list[str]]:
    """Dynamically inspect ~/Library/LaunchAgents/*.plist to discover user-specific
    Application Support directories, /Applications/*.app bundles, and Keychain service names
    without hardcoding any personal project names in the repository."""
    discovered_home_rels: list[str] = []
    discovered_apps: list[str] = []
    discovered_keychains: list[str] = []

    la_dir = home / "Library/LaunchAgents"
    if not la_dir.is_dir():
        return discovered_home_rels, discovered_apps, discovered_keychains

    home_prefix = str(home) + "/"
    app_support_prefix = "Library/Application Support/"
    skip_app_support = {"Google", "Apple", "Caches", "CrashReporter", "CloudDocs", "FileProvider"}

    for plist_file in sorted(la_dir.glob("*.plist")):
        if plist_file.name.startswith("com.google.") or plist_file.name.startswith("com.apple."):
            continue
        try:
            with plist_file.open("rb") as f:
                pl = plistlib.load(f)
        except Exception:
            continue

        strings_to_scan: list[str] = []
        if isinstance(pl.get("Program"), str):
            strings_to_scan.append(pl["Program"])
        for arg in pl.get("ProgramArguments") or []:
            if isinstance(arg, str):
                strings_to_scan.append(arg)
        for key in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
            val = pl.get(key)
            if isinstance(val, str):
                strings_to_scan.append(val)
        env_vars = pl.get("EnvironmentVariables") or {}
        if isinstance(env_vars, dict):
            for v in env_vars.values():
                if isinstance(v, str):
                    strings_to_scan.append(v)

        for token in strings_to_scan:
            # 1. Check for /Applications/*.app references
            m_app = re.search(r"/Applications/([^/]+\.app)", token)
            if m_app:
                app_name = m_app.group(1)
                if app_name not in discovered_apps and (Path("/Applications") / app_name).is_dir():
                    discovered_apps.append(app_name)

            # 2. Check for $HOME/Library/Application Support/<ServiceDir> references
            if token.startswith(home_prefix):
                rel = token[len(home_prefix) :]
                if rel.startswith(app_support_prefix):
                    parts = Path(rel).parts
                    if len(parts) >= 3:
                        svc_dir = parts[2]
                        top_rel = f"Library/Application Support/{svc_dir}"
                        if (
                            svc_dir not in skip_app_support
                            and top_rel not in discovered_home_rels
                            and (home / top_rel).exists()
                        ):
                            discovered_home_rels.append(top_rel)

                # 3. If token is a shell/python script under $HOME, scan it for Keychain service lookups
                script_p = Path(token)
                if script_p.is_file() and script_p.suffix in {".sh", ".zsh", ".py"} and script_p.stat().st_size < 200_000:
                    try:
                        txt = script_p.read_text(encoding="utf-8", errors="ignore")
                        for m_kc in re.finditer(r"find-generic-password[^\n]*?-s\s+['\"]?([A-Za-z0-9_.-]+)['\"]?", txt):
                            svc = m_kc.group(1)
                            if svc and svc not in discovered_keychains:
                                discovered_keychains.append(svc)
                    except OSError:
                        pass

    return discovered_home_rels, discovered_apps, discovered_keychains


def get_home_targets(home: Path | None = None) -> list[str]:
    h = home or Path.home()
    targets = list(HOME_RELATIVE_TARGETS)
    la_rels, _, _ = discover_launchagent_dependencies(h)
    for rel in la_rels:
        if rel not in targets:
            targets.append(rel)
    extra = os.environ.get("EXTRA_MIGRATION_PATHS", "")
    for item in extra.split(","):
        cleaned = item.strip()
        if cleaned and cleaned not in targets:
            targets.append(cleaned)
    return targets


def discover_all_sqlite_targets(home: Path) -> list[str]:
    targets: list[str] = []
    for rel in SQLITE_RELATIVE_TARGETS:
        if is_valid_sqlite_file(home / rel):
            targets.append(rel)

    codex_accts = home / "Library/Application Support/orca/codex-accounts"
    if codex_accts.is_dir():
        for db in sorted(codex_accts.glob("*/home/*.sqlite")):
            if is_valid_sqlite_file(db):
                rel_p = str(db.relative_to(home))
                if rel_p not in targets:
                    targets.append(rel_p)

    # Include any active Antigravity conversation DB that currently has a live -wal file
    ag_convs = home / "Library/Application Support/orca/antigravity-chat/antigravity-acp/conversations"
    if ag_convs.is_dir():
        for wal in sorted(ag_convs.glob("*.db-wal")):
            db = ag_convs / wal.name[:-4]
            if is_valid_sqlite_file(db):
                rel_p = str(db.relative_to(home))
                if rel_p not in targets:
                    targets.append(rel_p)

    # Scan auto-discovered LaunchAgent service directories & EXTRA_MIGRATION_PATHS for live SQLite DBs
    base_set = set(HOME_RELATIVE_TARGETS)
    for extra_rel in get_home_targets(home):
        if extra_rel not in base_set:
            extra_p = home / extra_rel
            if extra_p.is_dir():
                for pattern in ("*.sqlite", "*.sqlite3", "*.db", "*/*.sqlite", "*/*.sqlite3", "*/*.db"):
                    for db in sorted(extra_p.glob(pattern)):
                        if is_valid_sqlite_file(db):
                            rel_p = str(db.relative_to(home))
                            if rel_p not in targets:
                                targets.append(rel_p)
    return targets


def discover_core_apps(home: Path | None = None) -> list[str]:
    h = home or Path.home()
    apps_dir = Path("/Applications")
    candidates = list(CORE_APP_BUNDLES)
    _, la_apps, _ = discover_launchagent_dependencies(h)
    for app in la_apps:
        if app not in candidates:
            candidates.append(app)
    extra_apps = os.environ.get("EXTRA_BUNDLED_APPS", "")
    for item in extra_apps.split(","):
        cleaned = item.strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    return [name for name in candidates if (apps_dir / name).is_dir()]


def discover_keychain_services(home: Path | None = None) -> list[str]:
    h = home or Path.home()
    _, _, la_kc = discover_launchagent_dependencies(h)
    services = list(la_kc)
    extra_kc = os.environ.get("EXTRA_KEYCHAIN_SERVICES", "")
    for item in extra_kc.split(","):
        cleaned = item.strip()
        if cleaned and cleaned not in services:
            services.append(cleaned)
    return services


def format_bytes(num: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def compute_bundle_fingerprint(bundle_path: Path) -> str:
    """Fast O(1) metadata fingerprint for an .app bundle or Python.framework
    so unchanged multi-GB bundles can skip full rsync file-tree traversal."""
    parts: list[str] = []
    candidates = [
        bundle_path / "Contents/Info.plist",
        bundle_path / "Contents/MacOS",
        bundle_path / "Contents/Resources",
        bundle_path / "Versions/Current",
        bundle_path / "Versions/3.14/bin/python3",
        bundle_path / "Versions/3.14/lib/python3.14/site-packages",
    ]
    for p in candidates:
        if p.exists():
            try:
                st = p.stat()
                parts.append(f"{p.name}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                pass
    return "|".join(parts) or "unknown"


def sqlite_online_backup(src_db: Path, dst_db: Path) -> dict:
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ["", "-wal", "-shm"]:
        p = Path(str(dst_db) + suffix)
        if p.exists():
            p.unlink()
    t0 = time.time()
    cache_root = Path.home() / "Library/Caches/orca-migrate-sqlite"
    cache_root.mkdir(parents=True, exist_ok=True)
    unique_tag = f"{os.getpid()}-{abs(hash(str(src_db)))}"
    tmp_db = cache_root / f"snap-{unique_tag}-{src_db.name}"
    try:
        # Instantaneous APFS Copy-on-Write clone (cp -c) on the internal SSD (< 5ms even for 4GB DBs)
        # prevents sqlite3 .backup from restarting when Orca/Codex writes to the live DB during USB transfer.
        for suffix in ["", "-wal", "-shm"]:
            t_p = Path(str(tmp_db) + suffix)
            if t_p.exists():
                t_p.unlink()
        clone_rc = subprocess.run(
            ["/bin/cp", "-c", str(src_db), str(tmp_db)],
            capture_output=True,
            timeout=15,
        ).returncode
        if clone_rc == 0:
            src_wal = Path(str(src_db) + "-wal")
            if src_wal.is_file():
                subprocess.run(
                    ["/bin/cp", "-c", str(src_wal), str(tmp_db) + "-wal"],
                    capture_output=True,
                    timeout=15,
                )
            subprocess.run(
                ["/usr/bin/sqlite3", str(tmp_db), "PRAGMA wal_checkpoint(TRUNCATE);"],
                capture_output=True,
                timeout=60,
            )
            shutil.copy2(tmp_db, dst_db)
        else:
            subprocess.run(
                ["/usr/bin/sqlite3", str(src_db), ".timeout 15000", f".backup '{dst_db}'"],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )

        sz = dst_db.stat().st_size if dst_db.exists() else 0
        pragma_cmd = "PRAGMA schema_version;" if sz > 100 * 1024 * 1024 else "PRAGMA quick_check;"
        chk = subprocess.run(
            ["/usr/bin/sqlite3", str(dst_db), pragma_cmd],
            capture_output=True,
            text=True,
            timeout=30,
        )
        ok = chk.returncode == 0 and bool(chk.stdout.strip())
        return {
            "status": "ok" if ok else "warn",
            "integrity": "ok" if ok else "check_failed",
            "size_bytes": sz,
            "seconds": round(time.time() - t0, 2),
        }
    except Exception as e:
        shutil.copy2(src_db, dst_db)
        sz = dst_db.stat().st_size if dst_db.exists() else 0
        return {
            "status": "fallback_copy",
            "error": str(e),
            "size_bytes": sz,
            "seconds": round(time.time() - t0, 2),
        }
    finally:
        for suffix in ["", "-wal", "-shm"]:
            t_p = Path(str(tmp_db) + suffix)
            if t_p.exists():
                try:
                    t_p.unlink()
                except OSError:
                    pass


def export_keychain_secrets(account: str, services: list[str] | None = None) -> dict[str, str]:
    secrets = {}
    svc_list = services if services is not None else discover_keychain_services()
    for svc in svc_list:
        try:
            r = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", svc, "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                secrets[svc] = r.stdout.strip()
        except Exception:
            pass
    return secrets


def import_keychain_secrets(secrets: dict[str, str], account: str) -> list[str]:
    imported = []
    for svc, pw in secrets.items():
        try:
            r = subprocess.run(
                [
                    "/usr/bin/security",
                    "add-generic-password",
                    "-U",
                    "-a",
                    account,
                    "-s",
                    svc,
                    "-w",
                    pw,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                imported.append(svc)
        except Exception:
            pass
    return imported


def clean_stale_locks(home: Path) -> list[str]:
    removed = []
    orca_support = home / "Library/Application Support/orca"
    lock_patterns = [
        "SingletonLock",
        "SingletonSocket",
        "SingletonCookie",
        "orcad.lock",
        "*.sock",
        "daemon/*.sock",
        "daemon/*.pid",
        "daemon/*.token",
        "claude-profiles/*/home.lock",
        "codex-accounts/*/.sqlite-maintenance.lock",
    ]
    if orca_support.is_dir():
        for pat in lock_patterns:
            for p in orca_support.glob(pat):
                try:
                    if p.is_dir() and not p.is_symlink():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
                    removed.append(str(p.relative_to(home)))
                except OSError:
                    pass
    mole_lock = home / ".local/bin/.mole-update.lock"
    if mole_lock.exists():
        try:
            shutil.rmtree(mole_lock) if mole_lock.is_dir() else mole_lock.unlink()
            removed.append(".local/bin/.mole-update.lock")
        except OSError:
            pass
    return removed


def ensure_local_bin_runtime_symlinks(home: Path) -> list[str]:
    """Ensure node/npm/npx/corepack (from ~/opt/node-*) and dsh exist in ~/.local/bin
    so a brand-new Mac works immediately without needing /usr/local/bin populated first."""
    created = []
    local_bin = home / ".local/bin"
    local_bin.mkdir(parents=True, exist_ok=True)

    opt_dir = home / "opt"
    if opt_dir.is_dir():
        node_bins = sorted(opt_dir.glob("node-*/bin"), reverse=True)
        if node_bins:
            node_bin_dir = node_bins[0]
            for tool in ["node", "npm", "npx", "corepack"]:
                src = node_bin_dir / tool
                dst = local_bin / tool
                if dst.is_symlink() and not dst.exists():
                    try:
                        dst.unlink()
                    except OSError:
                        pass
                if src.exists() and not dst.exists():
                    try:
                        dst.symlink_to(src)
                        created.append(f"~/.local/bin/{tool} -> {src}")
                    except OSError:
                        pass

    dsh_app_bin = Path("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh")
    dsh_link = local_bin / "dsh"
    if dsh_link.is_symlink() and not dsh_link.exists():
        try:
            dsh_link.unlink()
        except OSError:
            pass
    if dsh_app_bin.exists() and not dsh_link.exists():
        try:
            dsh_link.symlink_to(dsh_app_bin)
            created.append(f"~/.local/bin/dsh -> {dsh_app_bin}")
        except OSError:
            pass

    codex_restic = home / "Library/Application Support/Codex Storage/bin/restic"
    restic_link = local_bin / "restic"
    if restic_link.is_symlink() and not restic_link.exists():
        try:
            restic_link.unlink()
        except OSError:
            pass
    if codex_restic.exists() and not restic_link.exists():
        try:
            restic_link.symlink_to(codex_restic)
            created.append(f"~/.local/bin/restic -> {codex_restic}")
        except OSError:
            pass

    return created


def relocate_home_references(home: Path, old_user: str, new_user: str) -> dict[str, int]:
    """If the new Mac's username differs from the old Mac's username, automatically
    rewrite /Users/<old_user> -> /Users/<new_user> in symlinks, LaunchAgents, and agent configs."""
    stats = {"relocated_symlinks": 0, "rewritten_configs": 0}
    if not old_user or not new_user or old_user == new_user:
        return stats

    old_prefix = f"/Users/{old_user}"
    new_prefix = str(home)

    # 1. Relocate symlinks inside key agent & tool directories
    symlink_roots = [
        home / ".local",
        home / ".agents",
        home / ".claude",
        home / ".codex",
        home / ".gemini",
        home / "Library/Application Support/orca",
        home / "Library/Application Support/Codex Storage",
    ]
    for root in symlink_roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.is_symlink():
                try:
                    target = str(os.readlink(p))
                    if target.startswith(old_prefix + "/"):
                        new_target = new_prefix + target[len(old_prefix) :]
                        p.unlink()
                        p.symlink_to(new_target)
                        stats["relocated_symlinks"] += 1
                except OSError:
                    pass

    # 2. Rewrite hardcoded /Users/<old_user> in LaunchAgents and agent config files
    config_candidates: list[Path] = []
    la_dir = home / "Library/LaunchAgents"
    if la_dir.is_dir():
        config_candidates.extend(la_dir.glob("*.plist"))
    codex_la_dir = home / "Library/Application Support/Codex Storage/agents/launchd"
    if codex_la_dir.is_dir():
        config_candidates.extend(codex_la_dir.glob("*.plist"))
    for rel_cfg in [
        ".zshenv",
        ".zshrc",
        ".zprofile",
        ".orca/backup-config.env",
        ".claude/settings.json",
        ".codex/config.toml",
        ".codex/hooks.json",
    ]:
        p = home / rel_cfg
        if p.is_file():
            config_candidates.append(p)
    orca_dir = home / "Library/Application Support/orca"
    if orca_dir.is_dir():
        config_candidates.extend(orca_dir.glob("claude-profiles/*/home/settings.json"))
        config_candidates.extend(orca_dir.glob("codex-accounts/*/home/config.toml"))
        config_candidates.extend(orca_dir.glob("codex-accounts/*/home/hooks.json"))

    for cfg in config_candidates:
        try:
            if not cfg.is_file() or cfg.is_symlink():
                continue
            raw = cfg.read_text(encoding="utf-8", errors="ignore")
            if old_prefix in raw:
                cfg.write_text(raw.replace(old_prefix, new_prefix), encoding="utf-8")
                stats["rewritten_configs"] += 1
        except OSError:
            pass

    return stats


def fix_ssh_permissions(home: Path) -> None:
    ssh_dir = home / ".ssh"
    if not ssh_dir.is_dir():
        return
    try:
        ssh_dir.chmod(0o700)
        for item in ssh_dir.iterdir():
            if item.is_file() and not item.is_symlink():
                if item.name.endswith(".pub") or item.name in {"known_hosts", "config", "authorized_keys"}:
                    item.chmod(0o644)
                else:
                    item.chmod(0o600)
    except OSError:
        pass


def cmd_pack(vol: Path, dry_run: bool, fast_config_only: bool, force_apps: bool = False) -> int:
    home = Path.home()
    if str(vol).startswith("/Volumes/") and not vol.is_dir():
        print(f"❌ Error: Target volume {vol} is not mounted.", file=sys.stderr)
        return 1

    vol_uuid = get_volume_uuid(vol)
    mig_root = vol / "AI/Migration/latest"
    mig_home = mig_root / "home"
    mig_apps = mig_root / "applications"
    mig_sys = mig_root / "system"
    mig_meta = mig_root / "metadata"
    mig_blueprint = mig_root / "blueprint"

    existing_targets = [
        rel for rel in get_home_targets(home) if (home / rel).exists() or (home / rel).is_symlink()
    ]
    sqlite_targets = discover_all_sqlite_targets(home)
    core_apps = discover_core_apps(home)
    keychain_services = discover_keychain_services(home)
    py_fw = Path("/Library/Frameworks/Python.framework")
    has_py_fw = py_fw.is_dir()

    if fast_config_only:
        sqlite_targets = [
            s
            for s in sqlite_targets
            if not s.endswith("agent-session-journal.db") and not s.endswith("thread_history_1.sqlite")
        ]

    print("==================================================================")
    print(" 📦 Orca Multi-Agent Blueprint — Pack Internal State to SSD")
    print(f" Mode              : {'DRY-RUN (Preview)' if dry_run else 'APPLY (Live Pack)'}")
    print(f" Source Host       : {socket.gethostname()} ({platform.machine()})")
    print(f" Source User       : {os.environ.get('USER', 'unknown')} (UID {os.getuid()})")
    print(f" Destination Root  : {mig_root}")
    print(f" Volume UUID       : {vol_uuid}")
    print(f" VM Exclusion      : Enabled (skips claudevm.bundle, claude-code-vm & CoreSimulator)")
    print(f" Fast Config Only  : {fast_config_only}")
    print("==================================================================")

    print(f"\n--> 1. Discovered {len(existing_targets)} internal-disk paths to mirror:")
    for rel in existing_targets:
        p = home / rel
        kind = "symlink" if p.is_symlink() else ("dir" if p.is_dir() else "file")
        print(f"    • ~/{rel} ({kind})")

    print(f"\n--> 2. Discovered {len(sqlite_targets)} live SQLite databases for parallel APFS CoW `.backup`:")
    for s in sqlite_targets:
        sz = (home / s).stat().st_size if (home / s).exists() else 0
        print(f"    • ~/{s} ({format_bytes(sz)})")

    print(f"\n--> 3. Discovered {len(core_apps)} core AI & utility .app bundles + Python.framework ({has_py_fw}):")
    for app in core_apps:
        print(f"    • /Applications/{app}")
    if has_py_fw:
        print("    • /Library/Frameworks/Python.framework (Standalone Python 3.14)")

    if dry_run:
        print("\n✅ [DRY-RUN] Preview complete. Run with `--apply` to execute lossless SSD pack.")
        return 0

    mig_home.mkdir(parents=True, exist_ok=True)
    mig_apps.mkdir(parents=True, exist_ok=True)
    mig_sys.mkdir(parents=True, exist_ok=True)
    mig_meta.mkdir(parents=True, exist_ok=True)
    mig_blueprint.mkdir(parents=True, exist_ok=True)

    # Ensure ~/.local/bin has node/npm/npx/dsh symlinks before mirroring
    ensure_local_bin_runtime_symlinks(home)

    # Step 1: Run openrsync -aHR from $HOME to preserve hardlinks across ~/.codex & orca/codex-accounts
    print("\n--> Step 1/5: Running lossless `rsync -aHR` on $HOME paths (preserving hardlinks & symlinks)...")
    excludes = list(RSYNC_EXCLUDES)
    for rel_db in sqlite_targets:
        excludes.append(f"/{rel_db}")
    if fast_config_only:
        excludes.extend(["sessions", "agent-session-journal.db", "thread_history_1.sqlite", "antigravity-acp"])

    rsync_cmd = ["/usr/bin/rsync", "-aHR", "--delete"]
    for exc in excludes:
        rsync_cmd.extend(["--exclude", exc])
    rsync_cmd.extend(existing_targets)
    rsync_cmd.append(str(mig_home) + "/")

    t0 = time.time()
    proc = subprocess.run(rsync_cmd, cwd=home, capture_output=True, text=True)
    if proc.returncode not in (0, 23, 24):
        print(f"⚠️ rsync exited with code {proc.returncode}: {proc.stderr[:400]}", file=sys.stderr)
    else:
        print(f"    ✅ $HOME rsync mirror completed in {round(time.time() - t0, 1)}s")

    clean_stale_locks(mig_home)

    # Step 2: Parallel Online SQLite `.backup` (via APFS CoW clone + WAL checkpoint)
    print(f"\n--> Step 2/5: Performing parallel atomic APFS CoW SQLite `.backup` ({len(sqlite_targets)} DBs)...")
    t_sql = time.time()
    sqlite_results: dict[str, dict] = {}
    valid_sql_targets = [rel_db for rel_db in sqlite_targets if (home / rel_db).is_file()]
    with ThreadPoolExecutor(max_workers=4) as pool:
        future_map = {
            pool.submit(sqlite_online_backup, home / rel_db, mig_home / rel_db): rel_db
            for rel_db in valid_sql_targets
        }
        for fut in as_completed(future_map):
            rel_db = future_map[fut]
            sqlite_results[rel_db] = fut.result()

    for rel_db in valid_sql_targets:
        res = sqlite_results[rel_db]
        print(
            f"    • {rel_db}: {res['status']} (integrity={res.get('integrity', 'n/a')}, "
            f"{format_bytes(res.get('size_bytes', 0))}, {res['seconds']}s)"
        )
    print(f"    ⚡ All {len(valid_sql_targets)} SQLite databases snapshotted & verified in {round(time.time() - t_sql, 1)}s")

    # Step 3: Incremental Fingerprint-Cached Mirror for Core .app Bundles & Python.framework
    fp_file = mig_meta / "bundle_fingerprints.json"
    cached_fps: dict[str, str] = {}
    if fp_file.is_file() and not force_apps:
        try:
            cached_fps = json.loads(fp_file.read_text())
        except Exception:
            cached_fps = {}
    new_fps = dict(cached_fps)

    if not fast_config_only:
        print("\n--> Step 3/5: Mirroring core AI .app bundles & Python.framework (with fingerprint cache)...")
        t_app = time.time()
        apps_synced = []
        apps_skipped = []
        for app in core_apps:
            src_app = Path("/Applications") / app
            dst_app = mig_apps / app
            fp = compute_bundle_fingerprint(src_app)
            if not force_apps and dst_app.is_dir() and cached_fps.get(f"app:{app}") == fp:
                apps_skipped.append(app)
            else:
                apps_synced.append(app)
            new_fps[f"app:{app}"] = fp

        if apps_synced:
            app_rsync = ["/usr/bin/rsync", "-aH", "--delete", *apps_synced, str(mig_apps) + "/"]
            subprocess.run(app_rsync, cwd=Path("/Applications"), capture_output=True, text=True)
        print(
            f"    ✅ Core .app bundles ready in {round(time.time() - t_app, 1)}s "
            f"({len(apps_synced)} synced, {len(apps_skipped)} unchanged skipped)"
        )

        if has_py_fw:
            t_py = time.time()
            py_fp = compute_bundle_fingerprint(py_fw)
            dst_py = mig_sys / "Python.framework"
            if not force_apps and dst_py.is_dir() and cached_fps.get("sys:Python.framework") == py_fp:
                print("    ⚡ Python.framework unchanged (fingerprint match, 0.0s)")
            else:
                py_rsync = ["/usr/bin/rsync", "-aH", "--delete", "Python.framework", str(mig_sys) + "/"]
                subprocess.run(py_rsync, cwd=Path("/Library/Frameworks"), capture_output=True, text=True)
                print(f"    ✅ Mirrored Python.framework to {mig_sys} ({round(time.time() - t_py, 1)}s)")
            new_fps["sys:Python.framework"] = py_fp

        fp_file.write_text(json.dumps(new_fps, ensure_ascii=False, indent=2))
    else:
        print("\n--> Step 3/5: Skipped .app bundles & Python.framework (--fast-config-only).")

    # Also embed a clean copy of the blueprint repository itself into ${MIG_ROOT}/blueprint
    # so any user's external SSD is 100% self-contained on the new Mac regardless of where they cloned the repo!
    repo_root = Path(__file__).resolve().parent.parent
    subprocess.run(
        ["/usr/bin/rsync", "-a", "--delete", "--exclude", ".git", f"{repo_root}/", f"{mig_blueprint}/"],
        capture_output=True,
        text=True,
    )

    # Step 4: Export Installed Applications, Brewfile & Keychain Secrets
    print("\n--> Step 4/5: Exporting /Applications inventory, Brewfile & Keychain secrets...")
    apps = sorted(p.name for p in Path("/Applications").glob("*.app")) if Path("/Applications").is_dir() else []
    user_apps = (
        sorted(p.name for p in (home / "Applications").glob("*.app"))
        if (home / "Applications").is_dir()
        else []
    )
    (mig_meta / "applications.json").write_text(
        json.dumps(
            {
                "system_applications": apps,
                "user_applications": user_apps,
                "bundled_core_apps": core_apps,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    brew_bin = shutil.which("brew") or ("/opt/homebrew/bin/brew" if Path("/opt/homebrew/bin/brew").exists() else None)
    if brew_bin:
        subprocess.run(
            [brew_bin, "bundle", "dump", "--file", str(mig_meta / "Brewfile"), "--force"],
            capture_output=True,
            text=True,
        )

    user_name = os.environ.get("USER", "developer")
    secrets = export_keychain_secrets(user_name, keychain_services)
    secrets_file = mig_meta / "keychain-secrets.json"
    if secrets:
        secrets_file.write_text(json.dumps(secrets, ensure_ascii=False, indent=2))
        secrets_file.chmod(0o600)
        print(f"    ✅ Exported {len(secrets)} Keychain secrets ({', '.join(secrets.keys())}) with mode 0600")

    # Step 5: Write MANIFEST.json, restore-on-new-mac.sh, and AGENT_RESTORE.md on the SSD
    manifest = {
        "schema_version": "1.2",
        "packed_at": datetime.now(timezone.utc).isoformat(),
        "source_hostname": socket.gethostname(),
        "source_user": user_name,
        "source_uid": os.getuid(),
        "macos_version": platform.mac_ver()[0],
        "arch": platform.machine(),
        "volume_path": str(vol),
        "volume_uuid": vol_uuid,
        "vm_excluded": True,
        "fast_config_only": fast_config_only,
        "targets_count": len(existing_targets),
        "targets": existing_targets,
        "bundled_core_apps": core_apps,
        "bundled_python_framework": has_py_fw,
        "sqlite_backups": sqlite_results,
        "applications_count": len(apps),
        "keychain_services": list(secrets.keys()),
    }
    (mig_meta / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    # Write universal dynamic restore-on-new-mac.sh (resolves VOL_ROOT from its own location)
    launcher = vol / "AI/Migration/restore-on-new-mac.sh"
    launcher.write_text(
        """#!/usr/bin/env zsh
# ==============================================================================
# One-Click New Mac Restore & Auto-Deploy Launcher (100% Universal & Offline-Ready)
# Works even on a brand-new unconfigured MacBook Pro without Xcode CLT installed!
# Usage:
#   zsh /Volumes/<YOUR_SSD>/AI/Migration/restore-on-new-mac.sh
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VOL_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export AI_VOLUME_PATH="${VOL_ROOT}"
MIG_ROOT="${VOL_ROOT}/AI/Migration/latest"

if [[ ! -d "$MIG_ROOT" ]]; then
  echo "❌ Error: Migration snapshot not found at $MIG_ROOT" >&2
  exit 1
fi

# 1. Bootstrap bundled Python.framework if not yet installed on this new Mac
#    so /usr/bin/python3 never triggers a blocking Xcode CLT popup dialog!
SSD_PY_FW="${MIG_ROOT}/system/Python.framework"
if [[ ! -x "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3" && -d "$SSD_PY_FW" ]]; then
  echo "--> Installing bundled Python 3.14 Framework to /Library/Frameworks..."
  if sudo -n true 2>/dev/null || sudo -v; then
    sudo /usr/bin/rsync -aH "$SSD_PY_FW" /Library/Frameworks/
    export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:$PATH"
    echo "    ✅ Installed /Library/Frameworks/Python.framework"
  else
    echo "    ℹ️ Running Python 3.14 directly from external SSD..."
    export DYLD_FRAMEWORK_PATH="${MIG_ROOT}/system"
    export PYTHONHOME="${SSD_PY_FW}/Versions/3.14"
    export PATH="${SSD_PY_FW}/Versions/3.14/bin:$PATH"
  fi
fi

# 2. Locate blueprint checkout on SSD (prefer AI/Projects/Mac/outputs/..., fallback to bundled snapshot copy)
if [[ -x "${VOL_ROOT}/AI/Projects/Mac/outputs/orca-multi-agent-blueprint/deploy.sh" ]]; then
  BLUEPRINT_DIR="${VOL_ROOT}/AI/Projects/Mac/outputs/orca-multi-agent-blueprint"
else
  BLUEPRINT_DIR="${MIG_ROOT}/blueprint"
fi

exec "${BLUEPRINT_DIR}/deploy.sh" --restore-from-ssd "$@"
"""
    )
    launcher.chmod(0o755)

    # Also write AGENT_RESTORE.md on the SSD so any AI Agent on the new Mac can read it directly
    agent_restore_doc = vol / "AI/Migration/AGENT_RESTORE.md"
    agent_restore_doc.write_text(
        f"""# AI Agent New-Mac Lossless Restore Guide (`AGENT_RESTORE.md`)

> **For AI Coding Agents (Claude, Codex, Antigravity, Cursor, Muse)**:
> This external SSD (`{vol}`) contains a complete lossless migration snapshot packed at `{manifest['packed_at']}` from `{manifest['source_hostname']}`.

## Step-by-Step Restore Instructions

1. **Preview Restore (`--restore-dry-run`)**:
   ```bash
   zsh "{launcher}" --restore-dry-run
   ```
2. **Execute Full Lossless Restore + Auto-Deploy**:
   ```bash
   zsh "{launcher}"
   ```
3. **Verify Health (`doctor.py`)**:
   After the command finishes, show the output of `doctor.py` to the user and ask them to run `tailscale up` if they use Tailscale.
"""
    )

    print("\n--> Step 5/5: Manifest, self-contained blueprint & new-Mac launcher written:")
    print(f"    • Manifest  : {mig_meta / 'MANIFEST.json'}")
    print(f"    • Launcher  : {launcher}")
    print(f"    • Agent Doc : {agent_restore_doc}")
    print("\n✅ Pack to SSD completed losslessly!")
    return 0


def cmd_restore(vol: Path, dry_run: bool) -> int:
    home = Path.home()
    mig_root = vol / "AI/Migration/latest"
    mig_home = mig_root / "home"
    mig_apps = mig_root / "applications"
    mig_meta = mig_root / "metadata"
    manifest_file = mig_meta / "MANIFEST.json"

    if not manifest_file.is_file() or not mig_home.is_dir():
        print(
            f"❌ Error: No migration snapshot found at {mig_root}.\n"
            f"   Run `./deploy.sh --pack-to-ssd` on your old Mac first.",
            file=sys.stderr,
        )
        return 1

    manifest = json.loads(manifest_file.read_text())
    current_user = os.environ.get("USER", "unknown")
    current_uid = os.getuid()
    src_user = manifest.get("source_user", "developer")
    src_uid = manifest.get("source_uid", 501)

    print("==================================================================")
    print(" 🚀 Orca Multi-Agent Blueprint — Restore State onto New Mac")
    print(f" Mode              : {'DRY-RUN (Preview)' if dry_run else 'APPLY (Live Restore)'}")
    print(f" Snapshot Packed At: {manifest.get('packed_at')}")
    print(f" Source Host / User: {manifest.get('source_hostname')} ({src_user}, UID {src_uid})")
    print(f" Target Host / User: {socket.gethostname()} ({current_user}, UID {current_uid})")
    print(f" Snapshot Root     : {mig_root}")
    print("==================================================================")

    if current_user != src_user or current_uid != src_uid:
        print(
            f"ℹ️  Note: Target user/UID ({current_user}/{current_uid}) differs from source ({src_user}/{src_uid}).\n"
            f"    Restore will automatically relocate /Users/{src_user} -> {home} across symlinks, LaunchAgents & configs."
        )

    targets = [rel for rel in manifest.get("targets", []) if (mig_home / rel).exists() or (mig_home / rel).is_symlink()]
    bundled_apps = [app for app in manifest.get("bundled_core_apps", []) if (mig_apps / app).is_dir()]
    print(f"\n--> Will restore {len(targets)} paths from {mig_home} into {home}:")
    for rel in targets:
        print(f"    • ~/{rel}")
    if bundled_apps:
        print(f"\n--> Will install any missing core .app bundles ({len(bundled_apps)} bundled) into /Applications:")
        for app in bundled_apps:
            status = "already installed" if (Path("/Applications") / app).is_dir() else "will install"
            print(f"    • /Applications/{app} ({status})")

    if dry_run:
        print("\n✅ [DRY-RUN] Restore preview complete. Run with `--apply` to execute restore.")
        return 0

    # Step 1: Restore files via openrsync -aHR (preserving hardlinks & symlinks)
    print("\n--> Step 1/5: Restoring $HOME files & hardlinks via `rsync -aHR`...")
    rsync_cmd = ["/usr/bin/rsync", "-aHR", *targets, str(home) + "/"]
    proc = subprocess.run(rsync_cmd, cwd=mig_home, capture_output=True, text=True)
    if proc.returncode not in (0, 23, 24):
        print(f"⚠️ rsync warning (exit {proc.returncode}): {proc.stderr[:400]}", file=sys.stderr)
    else:
        print("    ✅ All Agent directories, SQLite databases, and local repositories restored.")

    # Step 2: Restore missing core .app bundles into /Applications
    print("\n--> Step 2/5: Checking & restoring core AI .app bundles in /Applications...")
    installed_apps = []
    for app in bundled_apps:
        dst_app = Path("/Applications") / app
        src_app = mig_apps / app
        if not dst_app.is_dir() and src_app.is_dir():
            r = subprocess.run(
                ["/usr/bin/rsync", "-aH", str(src_app), "/Applications/"],
                capture_output=True,
                text=True,
            )
            if r.returncode == 0:
                installed_apps.append(app)
    print(
        f"    ✅ Restored {len(installed_apps)} missing .app bundles into /Applications "
        f"({len(bundled_apps) - len(installed_apps)} already present)."
    )

    # Step 3: Clean stale locks, relocate cross-user paths if needed, fix ~/.ssh permissions, and wire symlinks
    print("\n--> Step 3/5: Cleaning stale host locks, relocating paths, fixing ~/.ssh & wiring symlinks...")
    removed_locks = clean_stale_locks(home)
    for rel_db in manifest.get("sqlite_backups", {}).keys():
        for suffix in ["-wal", "-shm"]:
            wal_file = home / (rel_db + suffix)
            if wal_file.exists():
                try:
                    wal_file.unlink()
                    removed_locks.append(rel_db + suffix)
                except OSError:
                    pass
    reloc_stats = relocate_home_references(home, src_user, current_user)
    fix_ssh_permissions(home)
    created_links = ensure_local_bin_runtime_symlinks(home)
    print(
        f"    ✅ Cleaned {len(removed_locks)} stale lock/WAL files, relocated {reloc_stats['relocated_symlinks']} symlinks "
        f"& {reloc_stats['rewritten_configs']} configs, verified ~/.ssh (700/600), and wired {len(created_links)} runtime symlinks."
    )

    # Step 4: Restore Keychain secrets & reload LaunchAgents
    print("\n--> Step 4/5: Restoring Keychain secrets & registering LaunchAgents...")
    secrets_file = mig_meta / "keychain-secrets.json"
    if secrets_file.is_file():
        try:
            secrets = json.loads(secrets_file.read_text())
            imported = import_keychain_secrets(secrets, current_user)
            print(f"    ✅ Imported Keychain services: {', '.join(imported) if imported else 'none'}")
        except Exception as e:
            print(f"    ⚠️ Keychain import skipped: {e}")
    else:
        print("    ℹ️ No keychain-secrets.json present in snapshot.")

    launch_agents_dir = home / "Library/LaunchAgents"
    loaded_agents = 0
    if launch_agents_dir.is_dir():
        for plist_path in sorted(launch_agents_dir.glob("*.plist")):
            if plist_path.name.startswith("com.google.") or plist_path.name.startswith("com.apple."):
                continue
            subprocess.run(
                ["/bin/launchctl", "bootstrap", f"gui/{current_uid}", str(plist_path)],
                capture_output=True,
                timeout=5,
            )
            loaded_agents += 1
    print(f"    ✅ Registered {loaded_agents} LaunchAgents with launchd (gui/{current_uid}).")

    print("\n--> Step 5/5: State restoration finished! Handing off to `deploy.sh --auto` for toolchain & profile alignment...")
    return 0


def cmd_status(vol: Path) -> int:
    mig_root = vol / "AI/Migration/latest"
    manifest_file = mig_root / "metadata/MANIFEST.json"
    if not manifest_file.is_file():
        print(json.dumps({"status": "not_packed", "migration_root": str(mig_root)}, indent=2))
        return 0
    manifest = json.loads(manifest_file.read_text())
    print(json.dumps({"status": "ready", "migration_root": str(mig_root), "manifest": manifest}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Universal Lossless Cross-Mac AI Agent Migration Engine")
    parser.add_argument("action", choices=["pack", "restore", "status"], help="Migration action")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    parser.add_argument("--apply", action="store_true", help="Execute pack or restore")
    parser.add_argument("--fast-config-only", action="store_true", help="Skip multi-GB raw chat logs & .app bundles during pack")
    parser.add_argument("--force-apps", action="store_true", help="Force full resync of .app bundles & Python.framework ignoring fingerprint cache")
    parser.add_argument("--volume", type=str, default=None, help="Override external SSD volume path")
    args = parser.parse_args()

    vol = Path(args.volume) if args.volume else detect_volume()
    dry_run = not args.apply

    if args.action == "status":
        return cmd_status(vol)
    elif args.action == "pack":
        return cmd_pack(vol, dry_run=dry_run, fast_config_only=args.fast_config_only, force_apps=args.force_apps)
    elif args.action == "restore":
        return cmd_restore(vol, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
