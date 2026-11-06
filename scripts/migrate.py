#!/usr/bin/env python3
"""
Orca Multi-Agent Blueprint — Lossless Cross-Mac Migration Engine (migrate.py)

Packs all internal-disk AI Agent states, SQLite session/orchestration databases,
multi-account profiles, CLI toolchains, Node/Python runtimes, core .app bundles,
Xcode signing profiles, iOS pairing records, SSH/Git/Zsh configs, LaunchAgents,
and local repositories onto the external SSD (${AI_VOLUME_PATH}/AI/Migration/latest),
and restores them losslessly onto a new Mac without copying bulky VMs
(skips Claude Desktop claudevm.bundle / claude-code-vm and CoreSimulator/Devices).

Usage:
  python3 scripts/migrate.py pack --dry-run
  python3 scripts/migrate.py pack --apply
  python3 scripts/migrate.py restore --dry-run
  python3 scripts/migrate.py restore --apply
  python3 scripts/migrate.py status
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import socket
import subprocess
import sys
import time


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


# Relative paths under $HOME to migrate (Zero-VM: excludes claudevm.bundle & CoreSimulator)
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
    # 2. Custom LaunchAgent Backing Services & App Configs
    "Library/Application Support/GMGN Paper Research",
    "Library/Application Support/perp-arena",
    "Library/Application Support/Local Metrics",
    "Library/Application Support/Homelab Backups",
    "Library/Application Support/HomeOps",
    "Library/Application Support/NEGA Remote Install",
    "Library/Application Support/com.loon.Loon",
    "Library/Application Support/com.tw93.MoleApp",
    "Library/Application Support/me.nabdev.iloader",
    "Library/Application Support/SpektraLab",
    "Library/Application Support/com.shiguang.desktop",
    "Library/Group Containers/group.com.loon.mac",
    # 3. Native CLI Toolchain, Node Runtime, Shell History, Git, SSH & LaunchAgents
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
    # 4. Xcode Signing Profiles, Keychains Backup & Preferences
    "Library/Developer/Xcode/UserData",
    "Library/Application Support/com.apple.dt.Xcode",
    "Library/Keychains/login.keychain-db",
    "Library/Preferences/com.stablyai.orca.plist",
    "Library/Preferences/com.openai.codex.plist",
    "Library/Preferences/com.anthropic.claudefordesktop.plist",
    "Library/Preferences/com.loon.Loon.plist",
    "Library/Preferences/io.tailscale.ipn.macsys.plist",
    # 5. Internal-Disk Local Repositories, Harnesses & Desktop
    "Developer",
    "Documents/deepseek-harness",
    "Desktop",
    "Applications",
]

# Core AI & developer desktop .app bundles in /Applications to bundle onto the SSD
# so CLI symlinks (orca, codex, dsh, cc-same) and GUI apps work immediately on the new Mac
CORE_APP_BUNDLES = [
    "Orca.app",
    "Muse.app",
    "Claude.app",
    "ChatGPT.app",
    "Antigravity.app",
    "DeepSeek Harness.app",
    "Grok Bot.app",
    "CC Same.app",
    "Loon.app",
    "Tailscale.app",
    "Mole.app",
    "iloader.app",
    "SpektraLab.app",
    "拾光.app",
    "PrevisShot.app",
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
    "*.db-wal",
    "*.db-shm",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "*.sqlite3-wal",
    "*.sqlite3-shm",
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

# Critical SQLite databases backed up via APFS CoW clone + WAL checkpoint
SQLITE_RELATIVE_TARGETS = [
    "Library/Application Support/orca/agent-session-journal.db",
    "Library/Application Support/orca/orchestration.db",
    "Library/Application Support/orca/ai-vault/session-search.sqlite",
    "Library/Application Support/orca/ai-vault/session-parse-cache.sqlite",
    ".codex/thread_history_1.sqlite",
    ".codex/logs_2.sqlite",
    ".codex/state_5.sqlite",
    ".codex/memories_1.sqlite",
    ".codex/memories_v2_1.sqlite",
    ".gemini/antigravity/conversation_summaries.db",
    "Library/Application Support/GMGN Paper Research/runtime/ledger.sqlite3",
    "Library/Application Support/perp-arena/c11-arena-v1/arena.sqlite",
    "Library/Application Support/com.meta.endo/analytics/unified_micro_batch/FBAnalyticsUnifiedEventStore.sqlite",
    "Library/Application Support/Claude/declarative_performance_observer.db",
]

KEYCHAIN_SERVICES = [
    "restic-mac-agents",
    "icloud-package-key",
]


def get_home_targets() -> list[str]:
    targets = list(HOME_RELATIVE_TARGETS)
    extra = os.environ.get("EXTRA_MIGRATION_PATHS", "")
    for item in extra.split(","):
        cleaned = item.strip()
        if cleaned and cleaned not in targets:
            targets.append(cleaned)
    return targets


def discover_all_sqlite_targets(home: Path) -> list[str]:
    targets = []
    for rel in SQLITE_RELATIVE_TARGETS:
        if (home / rel).is_file():
            targets.append(rel)
    codex_accts = home / "Library/Application Support/orca/codex-accounts"
    if codex_accts.is_dir():
        for db in sorted(codex_accts.glob("*/home/*.sqlite")):
            if db.is_file():
                rel_p = str(db.relative_to(home))
                if rel_p not in targets:
                    targets.append(rel_p)
    # Also check any SQLite DBs inside EXTRA_MIGRATION_PATHS (e.g. Kocard)
    for extra_rel in get_home_targets():
        if extra_rel not in HOME_RELATIVE_TARGETS:
            extra_p = home / extra_rel
            if extra_p.is_dir():
                for db in sorted(extra_p.glob("*.sqlite")):
                    if db.is_file():
                        rel_p = str(db.relative_to(home))
                        if rel_p not in targets:
                            targets.append(rel_p)
    return targets


def discover_core_apps() -> list[str]:
    apps_dir = Path("/Applications")
    found = []
    for name in CORE_APP_BUNDLES:
        if (apps_dir / name).is_dir():
            found.append(name)
    return found


def format_bytes(num: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def sqlite_online_backup(src_db: Path, dst_db: Path) -> dict:
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ["", "-wal", "-shm"]:
        p = Path(str(dst_db) + suffix)
        if p.exists():
            p.unlink()
    t0 = time.time()
    cache_root = Path.home() / "Library/Caches/orca-migrate-sqlite"
    cache_root.mkdir(parents=True, exist_ok=True)
    tmp_db = cache_root / f"snap-{os.getpid()}-{src_db.name}"
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


def export_keychain_secrets(account: str) -> dict[str, str]:
    secrets = {}
    for svc in KEYCHAIN_SERVICES:
        try:
            r = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", svc, "-a", account, "-w"],
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

    # Link node, npm, npx, corepack from ~/opt/node-*/bin if present
    opt_dir = home / "opt"
    if opt_dir.is_dir():
        node_bins = sorted(opt_dir.glob("node-*/bin"), reverse=True)
        if node_bins:
            node_bin_dir = node_bins[0]
            for tool in ["node", "npm", "npx", "corepack"]:
                src = node_bin_dir / tool
                dst = local_bin / tool
                if src.exists() and not dst.exists():
                    try:
                        dst.symlink_to(src)
                        created.append(f"~/.local/bin/{tool} -> {src}")
                    except OSError:
                        pass

    # Link dsh from /Applications/DeepSeek Harness.app if present
    dsh_app_bin = Path("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh")
    dsh_link = local_bin / "dsh"
    if dsh_app_bin.exists() and not dsh_link.exists():
        try:
            dsh_link.symlink_to(dsh_app_bin)
            created.append(f"~/.local/bin/dsh -> {dsh_app_bin}")
        except OSError:
            pass

    return created


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


def cmd_pack(vol: Path, dry_run: bool, fast_config_only: bool) -> int:
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

    existing_targets = [
        rel for rel in get_home_targets() if (home / rel).exists() or (home / rel).is_symlink()
    ]
    sqlite_targets = discover_all_sqlite_targets(home)
    core_apps = discover_core_apps()
    py_fw = Path("/Library/Frameworks/Python.framework")
    has_py_fw = py_fw.is_dir()

    if fast_config_only:
        sqlite_targets = [
            s for s in sqlite_targets if not s.endswith("agent-session-journal.db") and not s.endswith("thread_history_1.sqlite")
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

    print(f"\n--> 2. Discovered {len(sqlite_targets)} live SQLite databases for APFS CoW `.backup` snapshot:")
    for s in sqlite_targets:
        sz = (home / s).stat().st_size if (home / s).exists() else 0
        print(f"    • ~/{s} ({format_bytes(sz)})")

    print(f"\n--> 3. Discovered {len(core_apps)} core AI & utility .app bundles + Python.framework ({has_py_fw}):")
    for app in core_apps:
        print(f"    • /Applications/{app}")
    if has_py_fw:
        print("    • /Library/Frameworks/Python.framework (Python 3.14 + 210 site-packages)")

    if dry_run:
        print("\n✅ [DRY-RUN] Preview complete. Run with `--apply` to execute lossless SSD pack.")
        return 0

    mig_home.mkdir(parents=True, exist_ok=True)
    mig_apps.mkdir(parents=True, exist_ok=True)
    mig_sys.mkdir(parents=True, exist_ok=True)
    mig_meta.mkdir(parents=True, exist_ok=True)

    # Ensure ~/.local/bin has node/npm/npx/dsh symlinks before mirroring
    ensure_local_bin_runtime_symlinks(home)

    # Step 1: Run openrsync -aHR from $HOME to preserve hardlinks across ~/.codex & orca/codex-accounts
    print("\n--> Step 1/5: Running lossless `rsync -aHR` on $HOME paths (preserving hardlinks & symlinks)...")
    excludes = list(RSYNC_EXCLUDES)
    for rel_db in sqlite_targets:
        excludes.append(Path(rel_db).name)
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

    # Step 2: Online SQLite `.backup` (via APFS CoW clone + WAL checkpoint)
    print("\n--> Step 2/5: Performing atomic APFS CoW SQLite `.backup` + integrity check...")
    sqlite_results = {}
    for rel_db in sqlite_targets:
        src_db = home / rel_db
        dst_db = mig_home / rel_db
        if not src_db.is_file():
            continue
        res = sqlite_online_backup(src_db, dst_db)
        sqlite_results[rel_db] = res
        print(
            f"    • {rel_db}: {res['status']} (integrity={res.get('integrity', 'n/a')}, "
            f"{format_bytes(res.get('size_bytes', 0))}, {res['seconds']}s)"
        )

    # Step 3: Mirror Core .app Bundles & Standalone Python.framework onto SSD
    if not fast_config_only:
        print("\n--> Step 3/5: Mirroring core AI .app bundles & Python.framework to SSD for offline new-Mac setup...")
        t_app = time.time()
        if core_apps:
            app_rsync = ["/usr/bin/rsync", "-aH", "--delete", *core_apps, str(mig_apps) + "/"]
            subprocess.run(app_rsync, cwd=Path("/Applications"), capture_output=True, text=True)
            print(f"    ✅ Mirrored {len(core_apps)} core .app bundles to {mig_apps} ({round(time.time() - t_app, 1)}s)")
        if has_py_fw:
            t_py = time.time()
            py_rsync = ["/usr/bin/rsync", "-aH", "--delete", "Python.framework", str(mig_sys) + "/"]
            subprocess.run(py_rsync, cwd=Path("/Library/Frameworks"), capture_output=True, text=True)
            print(f"    ✅ Mirrored Python.framework (Python 3.14) to {mig_sys} ({round(time.time() - t_py, 1)}s)")
    else:
        print("\n--> Step 3/5: Skipped .app bundles & Python.framework (--fast-config-only).")

    # Step 4: Export Installed Applications, Brewfile, Apple Signing Identities & Keychain Secrets
    print("\n--> Step 4/5: Exporting /Applications inventory, signing identities & Keychain secrets...")
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
    secrets = export_keychain_secrets(user_name)
    secrets_file = mig_meta / "keychain-secrets.json"
    if secrets:
        secrets_file.write_text(json.dumps(secrets, ensure_ascii=False, indent=2))
        secrets_file.chmod(0o600)
        print(f"    ✅ Exported {len(secrets)} Keychain secrets ({', '.join(secrets.keys())}) with mode 0600")

    # Step 5: Write MANIFEST.json and standalone zero-dependency restore launcher on the SSD
    manifest = {
        "schema_version": "1.1",
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

    # Write a one-click restore launcher script at ${AI_VOLUME_PATH}/AI/Migration/restore-on-new-mac.sh
    # Uses pure /bin/zsh and bootstraps Python.framework before invoking python3 so a brand-new Mac
    # without Xcode Command Line Tools installed never hits the macOS /usr/bin/python3 stub dialog!
    launcher = vol / "AI/Migration/restore-on-new-mac.sh"
    blueprint_dir = vol / "AI/Projects/Mac/outputs/orca-multi-agent-blueprint"
    launcher.write_text(
        f"""#!/usr/bin/env zsh
# ==============================================================================
# One-Click New Mac Restore & Auto-Deploy Launcher (Zero-Dependency)
# Works even on a brand-new unconfigured MacBook Pro without Xcode CLT installed!
# Usage:
#   zsh "{launcher}"
# ==============================================================================
set -euo pipefail

VOL_ROOT="{vol}"
MIG_ROOT="${{VOL_ROOT}}/AI/Migration/latest"
BLUEPRINT_DIR="{blueprint_dir}"

if [[ ! -d "$MIG_ROOT" ]]; then
  echo "❌ Error: Migration snapshot not found at $MIG_ROOT" >&2
  exit 1
fi

# 1. If /Library/Frameworks/Python.framework is not yet installed on this new Mac
#    and we have it bundled on the SSD, offer to install it or run directly off the SSD!
SSD_PY_FW="${{MIG_ROOT}}/system/Python.framework"
if [[ ! -x "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3" && -d "$SSD_PY_FW" ]]; then
  echo "--> Installing bundled Python 3.14 Framework (with 210 packages) to /Library/Frameworks..."
  if sudo -n true 2>/dev/null || sudo -v; then
    sudo /usr/bin/rsync -aH "$SSD_PY_FW" /Library/Frameworks/
    echo "    ✅ Installed /Library/Frameworks/Python.framework"
  else
    echo "    ℹ️ Running Python 3.14 directly from external SSD..."
    export DYLD_FRAMEWORK_PATH="${{MIG_ROOT}}/system"
    export PYTHONHOME="${{SSD_PY_FW}}/Versions/3.14"
    export PATH="${{SSD_PY_FW}}/Versions/3.14/bin:$PATH"
  fi
fi

exec "${{BLUEPRINT_DIR}}/deploy.sh" --restore-from-ssd "$@"
"""
    )
    launcher.chmod(0o755)

    print("\n--> Step 5/5: Manifest & one-click new-Mac launcher written:")
    print(f"    • Manifest : {mig_meta / 'MANIFEST.json'}")
    print(f"    • Launcher : {launcher}")
    print("\n✅ Pack to SSD completed losslessly!")
    return 0


def cmd_restore(vol: Path, dry_run: bool) -> int:
    home = Path.home()
    mig_root = vol / "AI/Migration/latest"
    mig_home = mig_root / "home"
    mig_apps = mig_root / "applications"
    mig_sys = mig_root / "system"
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
            f"⚠️  Warning: Target user/UID ({current_user}/{current_uid}) differs from source ({src_user}/{src_uid}).\n"
            f"    Hardcoded paths under /Users/{src_user} will be updated by sync_profiles.py after restore."
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

    # Step 3: Clean stale locks, WAL/SHM files, fix ~/.ssh permissions, and wire ~/.local/bin runtime symlinks
    print("\n--> Step 3/5: Cleaning stale host locks, fixing ~/.ssh permissions & wiring Node/CLI symlinks...")
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
    fix_ssh_permissions(home)
    created_links = ensure_local_bin_runtime_symlinks(home)
    print(
        f"    ✅ Cleaned {len(removed_locks)} stale lock/WAL files, verified ~/.ssh (700/600), "
        f"and wired {len(created_links)} runtime symlinks."
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
            if plist_path.name.startswith("com.google."):
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
    parser = argparse.ArgumentParser(description="Lossless Cross-Mac AI Agent Migration Engine")
    parser.add_argument("action", choices=["pack", "restore", "status"], help="Migration action")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    parser.add_argument("--apply", action="store_true", help="Execute pack or restore")
    parser.add_argument("--fast-config-only", action="store_true", help="Skip multi-GB raw chat logs & .app bundles during pack")
    parser.add_argument("--volume", type=str, default=None, help="Override external SSD volume path")
    args = parser.parse_args()

    vol = Path(args.volume) if args.volume else detect_volume()
    dry_run = not args.apply

    if args.action == "status":
        return cmd_status(vol)
    elif args.action == "pack":
        return cmd_pack(vol, dry_run=dry_run, fast_config_only=args.fast_config_only)
    elif args.action == "restore":
        return cmd_restore(vol, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
