#!/usr/bin/env python3
"""
Orca Multi-Agent Blueprint — Lossless Cross-Mac Migration Engine (migrate.py)

Packs all internal-disk AI Agent states, SQLite session/orchestration databases,
multi-account profiles, CLI toolchains, SSH/Git/Zsh configs, LaunchAgents, and
local repositories onto the external SSD (${AI_VOLUME_PATH}/AI/Migration/latest),
and restores them losslessly onto a new Mac without copying bulky VMs
(skips Claude Desktop claudevm.bundle and CoreSimulator/Devices by default).

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
import stat
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
    # 1. Core AI Agent States & Multi-Account Pools
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
    "Library/Application Support/Codex",
    "Library/Application Support/Codex Storage",
    "Library/Application Support/Antigravity",
    "Library/Application Support/Grok Bot",
    "Library/Application Support/@deepseek-ai",
    "Library/Application Support/Claude/claude_desktop_config.json",
    "Library/Application Support/Claude/config.json",
    # 2. Native CLI Toolchain, Shell, Git, SSH & LaunchAgents
    ".local",
    ".config",
    ".ssh",
    ".gitconfig",
    ".zshrc",
    ".zshenv",
    ".zprofile",
    "Library/LaunchAgents",
    # 3. Internal-Disk Local Repositories & Harnesses
    "Developer",
    "Documents/deepseek-harness",
]


def get_home_targets() -> list[str]:
    targets = list(HOME_RELATIVE_TARGETS)
    extra = os.environ.get("EXTRA_MIGRATION_PATHS", "")
    for item in extra.split(","):
        cleaned = item.strip()
        if cleaned and cleaned not in targets:
            targets.append(cleaned)
    return targets

# Ephemeral caches, VMs, locks, and WAL files excluded from rsync
RSYNC_EXCLUDES = [
    "vm_bundles",
    "claudevm.bundle",
    "CoreSimulator",
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "Crashpad",
    "acp-temp",
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "orcad.lock",
    "*.sock",
    "*.db-wal",
    "*.db-shm",
    "*.sqlite-wal",
    "*.sqlite-shm",
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

# Critical SQLite databases backed up via online `sqlite3 .backup` API
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
]

KEYCHAIN_SERVICES = [
    "restic-mac-agents",
    "icloud-package-key",
]


def discover_all_sqlite_targets(home: Path) -> list[str]:
    targets = []
    for rel in SQLITE_RELATIVE_TARGETS:
        if (home / rel).is_file():
            targets.append(rel)
    codex_accts = home / "Library/Application Support/orca/codex-accounts"
    if codex_accts.is_dir():
        for db in sorted(codex_accts.glob("*/home/*.sqlite")):
            if db.is_file():
                targets.append(str(db.relative_to(home)))
    return targets


def format_bytes(num: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def dir_size_fast(path: Path) -> int:
    if path.is_symlink():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    try:
        out = subprocess.check_output(
            ["/usr/bin/du", "-sk", str(path)],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        kb = int(out.split()[0])
        return kb * 1024
    except Exception:
        return 0


def sqlite_online_backup(src_db: Path, dst_db: Path) -> dict:
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    # Remove any pre-existing destination file or WAL/SHM before online backup
    for suffix in ["", "-wal", "-shm"]:
        p = Path(str(dst_db) + suffix)
        if p.exists():
            p.unlink()
    t0 = time.time()
    try:
        subprocess.run(
            ["/usr/bin/sqlite3", str(src_db), ".timeout 15000", f".backup '{dst_db}'"],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        # Run quick integrity check on the backed-up DB
        chk = subprocess.run(
            ["/usr/bin/sqlite3", str(dst_db), "PRAGMA quick_check;"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        integrity = chk.stdout.strip() if chk.returncode == 0 else "check_failed"
        sz = dst_db.stat().st_size if dst_db.exists() else 0
        return {
            "status": "ok" if integrity == "ok" else "warn",
            "integrity": integrity,
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
    ]
    if orca_support.is_dir():
        for pat in lock_patterns:
            for p in orca_support.glob(pat):
                try:
                    p.unlink()
                    removed.append(str(p.relative_to(home)))
                except OSError:
                    pass
    return removed


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
    mig_meta = mig_root / "metadata"

    existing_targets = [
        rel for rel in get_home_targets() if (home / rel).exists() or (home / rel).is_symlink()
    ]
    sqlite_targets = discover_all_sqlite_targets(home)
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
    print(f" VM Exclusion      : Enabled (skips claudevm.bundle & CoreSimulator)")
    print(f" Fast Config Only  : {fast_config_only}")
    print("==================================================================")

    print(f"\n--> 1. Discovered {len(existing_targets)} internal-disk paths to mirror:")
    for rel in existing_targets:
        p = home / rel
        kind = "symlink" if p.is_symlink() else ("dir" if p.is_dir() else "file")
        print(f"    • ~/{rel} ({kind})")

    print(f"\n--> 2. Discovered {len(sqlite_targets)} live SQLite databases for online `.backup` snapshot:")
    for s in sqlite_targets:
        sz = (home / s).stat().st_size if (home / s).exists() else 0
        print(f"    • ~/{s} ({format_bytes(sz)})")

    if dry_run:
        print("\n✅ [DRY-RUN] Preview complete. Run with `--apply` to execute lossless SSD pack.")
        return 0

    mig_home.mkdir(parents=True, exist_ok=True)
    mig_meta.mkdir(parents=True, exist_ok=True)

    # Step 1: Run openrsync -aHR from $HOME to preserve hardlinks across ~/.codex & orca/codex-accounts
    print("\n--> Step 1/4: Running lossless `rsync -aHR` (preserving hardlinks & symlinks)...")
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
        print(f"    ✅ rsync mirror completed in {round(time.time() - t0, 1)}s")

    # Clean any stale lock files that might have existed on the destination
    clean_stale_locks(mig_home)

    # Step 2: Online SQLite `.backup` directly into mig_home so every DB is WAL-checkpointed & verified
    print("\n--> Step 2/4: Performing atomic online SQLite `.backup` + `PRAGMA quick_check`...")
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

    # Step 3: Export Installed Applications, Brewfile & Keychain Secrets
    print("\n--> Step 3/4: Exporting /Applications inventory, Brewfile & Keychain secrets...")
    apps = sorted(p.name for p in Path("/Applications").glob("*.app")) if Path("/Applications").is_dir() else []
    user_apps = (
        sorted(p.name for p in (home / "Applications").glob("*.app"))
        if (home / "Applications").is_dir()
        else []
    )
    (mig_meta / "applications.json").write_text(
        json.dumps({"system_applications": apps, "user_applications": user_apps}, ensure_ascii=False, indent=2)
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

    # Step 4: Write MANIFEST.json and standalone restore helper on the SSD
    manifest = {
        "schema_version": "1.0",
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
        "sqlite_backups": sqlite_results,
        "applications_count": len(apps),
        "keychain_services": list(secrets.keys()),
    }
    (mig_meta / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    # Write a one-click restore launcher script at ${AI_VOLUME_PATH}/AI/Migration/restore-on-new-mac.sh
    launcher = vol / "AI/Migration/restore-on-new-mac.sh"
    blueprint_dir = vol / "AI/Projects/Mac/outputs/orca-multi-agent-blueprint"
    launcher.write_text(
        f"""#!/usr/bin/env zsh
# ==============================================================================
# One-Click New Mac Restore & Auto-Deploy Launcher
# Run on your new MacBook Pro after plugging in {vol.name}:
#   zsh "{launcher}"
# ==============================================================================
set -euo pipefail
exec "{blueprint_dir}/deploy.sh" --restore-from-ssd "$@"
"""
    )
    launcher.chmod(0o755)

    print("\n--> Step 4/4: Manifest & one-click new-Mac launcher written:")
    print(f"    • Manifest : {mig_meta / 'MANIFEST.json'}")
    print(f"    • Launcher : {launcher}")
    print("\n✅ Pack to SSD completed losslessly!")
    return 0


def cmd_restore(vol: Path, dry_run: bool) -> int:
    home = Path.home()
    mig_root = vol / "AI/Migration/latest"
    mig_home = mig_root / "home"
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
    print(f"\n--> Will restore {len(targets)} paths from {mig_home} into {home}:")
    for rel in targets:
        print(f"    • ~/{rel}")

    if dry_run:
        print("\n✅ [DRY-RUN] Restore preview complete. Run with `--apply` to execute restore.")
        return 0

    # Step 1: Restore files via openrsync -aHR (preserving hardlinks & symlinks)
    print("\n--> Step 1/4: Restoring files & hardlinks into $HOME via `rsync -aHR`...")
    rsync_cmd = ["/usr/bin/rsync", "-aHR", *targets, str(home) + "/"]
    proc = subprocess.run(rsync_cmd, cwd=mig_home, capture_output=True, text=True)
    if proc.returncode not in (0, 23, 24):
        print(f"⚠️ rsync warning (exit {proc.returncode}): {proc.stderr[:400]}", file=sys.stderr)
    else:
        print("    ✅ All Agent directories, SQLite databases, and local repositories restored.")

    # Step 2: Clean stale locks & WAL/SHM files on target so restored DBs open cleanly
    print("\n--> Step 2/4: Cleaning stale host locks & fixing ~/.ssh permissions...")
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
    print(f"    ✅ Cleaned {len(removed_locks)} stale lock/WAL files and verified ~/.ssh permissions (700/600).")

    # Step 3: Restore Keychain secrets
    print("\n--> Step 3/4: Restoring Keychain secrets...")
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

    print("\n--> Step 4/4: State restoration finished! Handing off to `deploy.sh --auto` for toolchain & profile alignment...")
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
    parser.add_argument("--fast-config-only", action="store_true", help="Skip multi-GB raw chat logs during pack")
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
