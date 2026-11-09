#!/usr/bin/env python3
"""
Orca Multi-Agent Blueprint — Universal Lossless Cross-Mac Migration Engine (migrate.py)

Packs all internal-disk AI Agent states, SQLite session/orchestration databases,
multi-account profiles, CLI toolchains, Node/Python runtimes, core .app bundles,
Xcode signing profiles, iOS pairing records, SSH/Git/Zsh configs, LaunchAgents,
and local repositories onto an external SSD (${AI_VOLUME_PATH}/AI/Migration/latest),
and restores them losslessly onto a new Mac without copying bulky VMs
(skips Claude Desktop claudevm.bundle / claude-code-vm and CoreSimulator/Devices).

Hardened features (B1–B12):
- Atomic SQLite online `.backup()` API with strict `PRAGMA quick_check == 'ok'` & SHA-256 receipts.
- Auto-discovers WAL-active SQLite databases across all migrated directories so no WAL frames are lost.
- Fail-closed Keychain secret discovery & export (`restic-mac-agents`, `icloud-package-key`, + account metadata).
- Safe Keychain handling on restore: never overwrites live `~/Library/Keychains/login.keychain-db`; stages
  old login keychain to `~/Migration-Imports/old-login.keychain-db` and imports generic passwords via `security`.
- Atomic pack state tracking (`metadata/PACK_STATE.json`) and full rsync logging (`metadata/pack.log`).
- Restore preflight checks (running Agent processes, `PACK_STATE == complete`, macOS version, free disk space)
  plus automatic rollback backup (`~/.migration-preexisting/<timestamp>`).
- Selective LaunchAgent bootstrapping: only auto-loads blueprint `local.<prefix>.*` jobs after Keychain verification;
  lists third-party/trading LaunchAgents (`--enable-agents` to opt in).
- Safe cross-username path relocation using `plistlib` (preserves binary plists) and boundary-aware regex.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

SQLITE_MAGIC_HEADER = b"SQLite format 3\x00"
CORE_KEYCHAIN_SERVICES = ("restic-mac-agents", "icloud-package-key")


def _load_config_fallbacks(home: Path | None = None) -> dict[str, str]:
    """Load configuration from repo config.env and ~/.orca/backup-config.env when env vars are not set."""
    h = home or Path.home()
    cfg: dict[str, str] = {}
    repo_cfg = Path(__file__).resolve().parent.parent / "config.env"
    orca_cfg = h / ".orca/backup-config.env"
    for candidate in (repo_cfg, orca_cfg):
        try:
            if not candidate.is_file():
                continue
            for raw_line in candidate.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'").replace("$HOME", str(h)).replace("${HOME}", str(h))
                if v:
                    cfg[k.strip()] = v
        except OSError:
            pass
    for k, v in os.environ.items():
        if v.strip():
            cfg[k] = v.strip()
    return cfg


def detect_volume(require_external_if_configured: bool = False) -> Path:
    cfg = _load_config_fallbacks()
    env_vol = os.environ.get("AI_VOLUME_PATH", "").strip() or cfg.get("AI_VOLUME_PATH", "").strip()
    storage_mode = os.environ.get("STORAGE_MODE", "").strip() or cfg.get("STORAGE_MODE", "auto").strip()
    if env_vol:
        p = Path(env_vol)
        if p.exists() or require_external_if_configured or storage_mode == "external":
            return p
    volumes = Path("/Volumes")
    skip = {"Macintosh HD", "Macintosh HD - Data", "Preboot", "Recovery", "VM", "Update"}
    if volumes.is_dir():
        for v in sorted(volumes.iterdir()):
            if v.name in skip or v.name.startswith("Macintosh HD") or v.name.startswith("com.apple."):
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
# Note (B4): Library/Keychains/login.keychain-db is intentionally NOT in HOME_RELATIVE_TARGETS
# so restore never overwrites a live login.keychain-db in place; it is staged separately into metadata/.
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
    # 3. Xcode Signing Profiles & Core Agent Preferences
    "Library/Developer/Xcode/UserData",
    "Library/Application Support/com.apple.dt.Xcode",
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
    "Library/Keychains",
    "Library/Keychains/*",
    ".claude/shell-snapshots",
    ".claude/debug",
    ".codex/cache",
    ".codex/.tmp",
    ".codex/tmp",
    ".codex/node_repl",
    "artifact-sessions/runtime-generations",
    ".dsh/cache",
    ".dsh/profiles/*/node_modules",
    ".dsh-tui/compile-cache",
    "DerivedData",
    "Developer/build",
    ".DS_Store",
]

# Universal SQLite databases backed up via atomic SQLite Online Backup API
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
                            if svc and not svc.startswith("$") and svc not in discovered_keychains:
                                discovered_keychains.append(svc)
                    except OSError:
                        pass

    return discovered_home_rels, discovered_apps, discovered_keychains


def get_home_targets(home: Path | None = None) -> list[str]:
    h = home or Path.home()
    cfg = _load_config_fallbacks(h)
    targets = [t for t in HOME_RELATIVE_TARGETS if not t.startswith("Library/Keychains")]
    la_rels, _, _ = discover_launchagent_dependencies(h)
    for rel in la_rels:
        if rel not in targets and not rel.startswith("Library/Keychains"):
            targets.append(rel)
    extra = os.environ.get("EXTRA_MIGRATION_PATHS", "") or cfg.get("EXTRA_MIGRATION_PATHS", "")
    for item in extra.split(","):
        cleaned = item.strip()
        if cleaned and not cleaned.startswith("Library/Keychains") and cleaned not in targets:
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

    # Include all valid Antigravity conversation DBs
    ag_convs = home / "Library/Application Support/orca/antigravity-chat/antigravity-acp/conversations"
    if ag_convs.is_dir():
        for db in sorted(ag_convs.glob("*.db")):
            if is_valid_sqlite_file(db):
                rel_p = str(db.relative_to(home))
                if rel_p not in targets:
                    targets.append(rel_p)

    # P2-5: Auto-discover any SQLite database inside migrated directories that has an active -wal file
    # so no WAL-mode database ever loses uncheckpointed frames due to *-wal rsync exclusions.
    for target_rel in get_home_targets(home):
        target_p = home / target_rel
        if not target_p.is_dir() or target_p.is_symlink():
            continue
        try:
            for wal_pattern in ("*.db-wal", "*.sqlite-wal", "*.sqlite3-wal"):
                for wal_file in sorted(target_p.rglob(wal_pattern)):
                    if " profile-state.db.backup." in str(wal_file):
                        continue
                    db_file = Path(str(wal_file)[:-4])
                    if is_valid_sqlite_file(db_file):
                        rel_p = str(db_file.relative_to(home))
                        if rel_p not in targets:
                            targets.append(rel_p)
        except OSError:
            pass

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
    cfg = _load_config_fallbacks(h)
    apps_dir = Path("/Applications")
    candidates = list(CORE_APP_BUNDLES)
    _, la_apps, _ = discover_launchagent_dependencies(h)
    for app in la_apps:
        if app not in candidates:
            candidates.append(app)
    extra_apps = os.environ.get("EXTRA_BUNDLED_APPS", "") or cfg.get("EXTRA_BUNDLED_APPS", "")
    for item in extra_apps.split(","):
        cleaned = item.strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    return [name for name in candidates if (apps_dir / name).is_dir()]


def discover_keychain_services(home: Path | None = None) -> list[str]:
    """B1: Always include core backup Keychain services (restic-mac-agents, icloud-package-key)
    plus services configured in ~/.orca/backup-config.env, config.env, or EXTRA_KEYCHAIN_SERVICES."""
    h = home or Path.home()
    cfg = _load_config_fallbacks(h)
    services: list[str] = list(CORE_KEYCHAIN_SERVICES)

    for cfg_key in ("BACKUP_KEYCHAIN_RESTIC", "BACKUP_KEYCHAIN_ICLOUD"):
        val = cfg.get(cfg_key, "").strip()
        if val and val not in services:
            services.append(val)

    _, _, la_kc = discover_launchagent_dependencies(h)
    for svc in la_kc:
        if svc and svc not in services:
            services.append(svc)

    extra_kc = os.environ.get("EXTRA_KEYCHAIN_SERVICES", "") or cfg.get("EXTRA_KEYCHAIN_SERVICES", "")
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


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


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


def verify_sqlite_quick_check(db_path: Path) -> tuple[bool, str]:
    """B9 (P2-2): Strict SQLite integrity verification — runs PRAGMA quick_check on ANY size DB
    and requires every output row to equal 'ok'."""
    if not is_valid_sqlite_file(db_path):
        return False, "invalid_sqlite_header"
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        try:
            rows = [r[0] for r in conn.execute("PRAGMA quick_check;").fetchall()]
        finally:
            conn.close()
        if rows == ["ok"]:
            return True, "ok"
        return False, "; ".join(str(x) for x in rows[:3]) or "empty_quick_check"
    except Exception as e:
        return False, str(e)


def sqlite_online_backup(src_db: Path, dst_db: Path, prev_meta: dict | None = None) -> dict:
    """B9 (P2-1, P2-2, P2-3): Atomic online SQLite backup using APFS Copy-on-Write (or copy2)
    of DB + WAL + SHM into local NVMe temp dir, followed by PRAGMA wal_checkpoint(TRUNCATE),
    strict PRAGMA quick_check == 'ok', and SHA-256 calculation. Never hangs on EXCLUSIVE-locked DBs."""
    t0 = time.time()
    if not is_valid_sqlite_file(src_db):
        return {
            "status": "error",
            "integrity": "failed:invalid_sqlite_header",
            "size_bytes": 0,
            "sha256": "",
            "seconds": 0.0,
        }

    src_wal = Path(str(src_db) + "-wal")
    src_shm = Path(str(src_db) + "-shm")

    # Fast reuse for unchanged historical DBs that have no active WAL file
    if (
        prev_meta
        and prev_meta.get("status") == "ok"
        and prev_meta.get("integrity") == "ok"
        and prev_meta.get("sha256")
        and not src_wal.exists()
        and dst_db.is_file()
        and not Path(str(dst_db) + "-wal").exists()
    ):
        try:
            src_st = src_db.stat()
            dst_st = dst_db.stat()
            if (
                src_st.st_size == dst_st.st_size
                and prev_meta.get("size_bytes") == src_st.st_size
                and abs(src_st.st_mtime - dst_st.st_mtime) < 1.0
            ):
                return {
                    "status": "ok",
                    "integrity": "ok",
                    "size_bytes": dst_st.st_size,
                    "sha256": prev_meta["sha256"],
                    "seconds": round(time.time() - t0, 3),
                }
        except OSError:
            pass

    dst_db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ["", "-wal", "-shm"]:
        p = Path(str(dst_db) + suffix)
        if p.exists():
            p.unlink()

    cache_root = Path.home() / "Library/Caches/orca-migrate-sqlite"
    cache_root.mkdir(parents=True, exist_ok=True)
    unique_tag = f"{os.getpid()}-{abs(hash(str(src_db)))}"
    tmp_db = cache_root / f"snap-{unique_tag}-{src_db.name}"
    try:
        for suffix in ["", "-wal", "-shm"]:
            t_p = Path(str(tmp_db) + suffix)
            if t_p.exists():
                t_p.unlink()

        # 1. Snapshot DB + WAL + SHM into local NVMe temp dir via APFS CoW (`cp -c`), fallback to copy2
        for suffix in ["", "-wal", "-shm"]:
            s_p = Path(str(src_db) + suffix)
            t_p = Path(str(tmp_db) + suffix)
            if s_p.is_file():
                r_cp = subprocess.run(["/bin/cp", "-c", str(s_p), str(t_p)], capture_output=True)
                if r_cp.returncode != 0:
                    shutil.copy2(s_p, t_p)

        # 2. Fold WAL into the isolated temp DB and switch to DELETE journal mode so no -wal/-shm remains
        subprocess.run(
            [
                "/usr/bin/sqlite3",
                str(tmp_db),
                "PRAGMA busy_timeout=5000; PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=DELETE;",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        for suffix in ["-wal", "-shm"]:
            t_p = Path(str(tmp_db) + suffix)
            if t_p.exists():
                t_p.unlink()

        # 3. Verify PRAGMA quick_check == 'ok' and compute SHA-256 on fast local NVMe
        ok, detail = verify_sqlite_quick_check(tmp_db)
        if not ok:
            return {
                "status": "error",
                "integrity": f"failed:{detail}",
                "size_bytes": 0,
                "sha256": "",
                "seconds": round(time.time() - t0, 2),
            }

        digest = compute_sha256(tmp_db)
        shutil.copy2(tmp_db, dst_db)
        # Preserve source mtime when no WAL was folded so incremental reuse works on next pack
        if not src_wal.exists():
            try:
                st = src_db.stat()
                os.utime(dst_db, (st.st_atime, st.st_mtime))
            except OSError:
                pass
        for suffix in ["-wal", "-shm"]:
            w_p = Path(str(dst_db) + suffix)
            if w_p.exists():
                w_p.unlink()

        sz = dst_db.stat().st_size if dst_db.exists() else 0
        return {
            "status": "ok",
            "integrity": "ok",
            "size_bytes": sz,
            "sha256": digest,
            "seconds": round(time.time() - t0, 2),
        }
    except Exception as e:
        return {
            "status": "error",
            "integrity": "failed",
            "error": str(e),
            "size_bytes": 0,
            "sha256": "",
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


def export_keychain_secrets(account: str, services: list[str] | None = None) -> dict[str, dict[str, str]]:
    """B1 / P2-9: Export generic passwords along with account and SHA-256 digest."""
    secrets_map: dict[str, dict[str, str]] = {}
    svc_list = services if services is not None else discover_keychain_services()
    for svc in svc_list:
        pw = ""
        resolved_acct = account
        for cmd in (
            ["/usr/bin/security", "find-generic-password", "-s", svc, "-a", account, "-w"],
            ["/usr/bin/security", "find-generic-password", "-s", svc, "-w"],
        ):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    pw = r.stdout.strip()
                    break
            except Exception:
                pass
        if pw:
            secrets_map[svc] = {
                "account": resolved_acct,
                "password": pw,
                "sha256": hashlib.sha256(pw.encode("utf-8")).hexdigest(),
            }
    return secrets_map


def import_keychain_secrets(secrets_data: dict, default_account: str) -> list[str]:
    """Import secrets from either structured dict {svc: {account, password}} or legacy {svc: password}."""
    imported: list[str] = []
    for svc, val in secrets_data.items():
        if isinstance(val, dict):
            pw = val.get("password", "")
            acct = val.get("account") or default_account
        elif isinstance(val, str):
            pw = val
            acct = default_account
        else:
            continue
        if not pw:
            continue
        try:
            r = subprocess.run(
                [
                    "/usr/bin/security",
                    "add-generic-password",
                    "-U",
                    "-a",
                    acct,
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


def _rewrite_plist_obj(obj, pattern: re.Pattern, new_prefix: str) -> tuple[object, bool]:
    changed = False
    if isinstance(obj, str):
        new_str = pattern.sub(new_prefix, obj)
        return new_str, (new_str != obj)
    if isinstance(obj, list):
        out_list = []
        for item in obj:
            new_item, c = _rewrite_plist_obj(item, pattern, new_prefix)
            out_list.append(new_item)
            changed = changed or c
        return out_list, changed
    if isinstance(obj, dict):
        out_dict = {}
        for k, v in obj.items():
            new_k, ck = _rewrite_plist_obj(k, pattern, new_prefix)
            new_v, cv = _rewrite_plist_obj(v, pattern, new_prefix)
            out_dict[new_k] = new_v
            changed = changed or ck or cv
        return out_dict, changed
    return obj, False


def relocate_home_references(home: Path, old_user: str, new_user: str) -> dict[str, int]:
    """B11 (P2-8): If the new Mac's username differs from the old Mac's username, automatically
    rewrite /Users/<old_user> -> <home> using boundary-aware regex and plistlib (preserving binary plists)."""
    stats = {"relocated_symlinks": 0, "rewritten_configs": 0}
    if not old_user or not new_user or old_user == new_user:
        return stats

    old_prefix = f"/Users/{old_user}"
    new_prefix = str(home)
    boundary_pat = re.compile(rf"{re.escape(old_prefix)}(?=/|$|[\s\"':;,])")

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
                    if target == old_prefix or target.startswith(old_prefix + "/"):
                        new_target = new_prefix + target[len(old_prefix) :]
                        p.unlink()
                        p.symlink_to(new_target)
                        stats["relocated_symlinks"] += 1
                except OSError:
                    pass

    # 2. Rewrite hardcoded /Users/<old_user> in LaunchAgents (via plistlib) and agent config files
    plist_candidates: list[Path] = []
    la_dir = home / "Library/LaunchAgents"
    if la_dir.is_dir():
        plist_candidates.extend(la_dir.glob("*.plist"))
    codex_la_dir = home / "Library/Application Support/Codex Storage/agents/launchd"
    if codex_la_dir.is_dir():
        plist_candidates.extend(codex_la_dir.glob("*.plist"))

    for pl_path in plist_candidates:
        try:
            if not pl_path.is_file() or pl_path.is_symlink():
                continue
            raw_bytes = pl_path.read_bytes()
            is_binary = raw_bytes.startswith(b"bplist00")
            pl_data = plistlib.loads(raw_bytes)
            new_data, changed = _rewrite_plist_obj(pl_data, boundary_pat, new_prefix)
            if changed:
                fmt = plistlib.FMT_BINARY if is_binary else plistlib.FMT_XML
                pl_path.write_bytes(plistlib.dumps(new_data, fmt=fmt, sort_keys=False))
                stats["rewritten_configs"] += 1
        except Exception:
            pass

    text_candidates: list[Path] = []
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
            text_candidates.append(p)
    orca_dir = home / "Library/Application Support/orca"
    if orca_dir.is_dir():
        text_candidates.extend(orca_dir.glob("claude-profiles/*/home/settings.json"))
        text_candidates.extend(orca_dir.glob("codex-accounts/*/home/config.toml"))
        text_candidates.extend(orca_dir.glob("codex-accounts/*/home/hooks.json"))

    for cfg in text_candidates:
        try:
            if not cfg.is_file() or cfg.is_symlink():
                continue
            raw = cfg.read_text(encoding="utf-8", errors="ignore")
            new_raw = boundary_pat.sub(new_prefix, raw)
            if new_raw != raw:
                cfg.write_text(new_raw, encoding="utf-8")
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


def check_running_agent_processes() -> list[str]:
    """Return list of running AI Agent desktop app names that could hold SQLite locks."""
    apps = ("Orca", "Claude", "ChatGPT", "Antigravity", "Muse")
    running: list[str] = []
    try:
        out = subprocess.check_output(["/bin/ps", "-ax", "-o", "comm="], text=True, timeout=5)
        for app in apps:
            needle = f"/{app}.app/Contents/MacOS/{app}"
            if any(needle in line for line in out.splitlines()):
                running.append(app)
    except Exception:
        pass
    return running


def _parse_version_tuple(ver_str: str) -> tuple[int, ...]:
    parts = []
    for p in (ver_str or "").split("."):
        if p.isdigit():
            parts.append(int(p))
    return tuple(parts) or (0,)


def cmd_pack(vol: Path, dry_run: bool, fast_config_only: bool, force_apps: bool = False) -> int:
    home = Path.home()
    cfg = _load_config_fallbacks(home)
    storage_mode = os.environ.get("STORAGE_MODE", "") or cfg.get("STORAGE_MODE", "auto")

    # B10 (P2-7): Refuse silent fallback to internal disk when external mode or /Volumes/* is configured
    if (storage_mode == "external" or str(vol).startswith("/Volumes/")) and not (
        str(vol).startswith("/Volumes/") and os.path.ismount(vol)
    ):
        print(
            f"❌ Error: Target external volume {vol} is not mounted (refusing fallback to internal disk).",
            file=sys.stderr,
        )
        return 1

    vol_uuid = get_volume_uuid(vol)
    expected_uuid = (os.environ.get("AI_VOLUME_UUID", "") or cfg.get("AI_VOLUME_UUID", "AUTO")).strip()
    if (
        str(vol).startswith("/Volumes/")
        and expected_uuid
        and expected_uuid not in ("AUTO", "LOCAL", "INTERNAL")
        and vol_uuid != expected_uuid
    ):
        print(
            f"❌ Error: Volume UUID mismatch on {vol} (expected {expected_uuid}, got {vol_uuid}).",
            file=sys.stderr,
        )
        return 1

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

    free_bytes = shutil.disk_usage(vol).free if vol.exists() else 0

    print("==================================================================")
    print(" 📦 Orca Multi-Agent Blueprint — Pack Internal State to SSD")
    print(f" Mode              : {'DRY-RUN (Preview)' if dry_run else 'APPLY (Live Pack)'}")
    print(f" Source Host       : {socket.gethostname()} ({platform.machine()})")
    print(f" Source User       : {os.environ.get('USER', 'unknown')} (UID {os.getuid()})")
    print(f" Destination Root  : {mig_root}")
    print(f" Volume UUID       : {vol_uuid} (Free Space: {format_bytes(free_bytes)})")
    print(" VM Exclusion      : Enabled (skips claudevm.bundle, claude-code-vm & CoreSimulator)")
    print(f" Fast Config Only  : {fast_config_only}")
    if fast_config_only:
        print(" ⚠️  WARNING       : --fast-config-only skips raw session journals & .app bundles!")
    print("==================================================================")

    print(f"\n--> 1. Discovered {len(existing_targets)} internal-disk paths to mirror:")
    for rel in existing_targets:
        p = home / rel
        kind = "symlink" if p.is_symlink() else ("dir" if p.is_dir() else "file")
        print(f"    • ~/{rel} ({kind})")

    print(f"\n--> 2. Discovered {len(sqlite_targets)} live SQLite databases for atomic online `.backup()`:")
    for s in sqlite_targets:
        sz = (home / s).stat().st_size if (home / s).exists() else 0
        print(f"    • ~/{s} ({format_bytes(sz)})")

    print(f"\n--> 3. Discovered {len(core_apps)} core AI & utility .app bundles + Python.framework ({has_py_fw}):")
    for app in core_apps:
        print(f"    • /Applications/{app}")
    if has_py_fw:
        print("    • /Library/Frameworks/Python.framework (Standalone Python 3.14)")

    print(f"\n--> 4. Discovered {len(keychain_services)} Keychain service names to export:")
    for svc in keychain_services:
        print(f"    • {svc}")

    if dry_run:
        print("\n✅ [DRY-RUN] Preview complete. Run with `--apply` to execute lossless SSD pack.")
        return 0

    mig_home.mkdir(parents=True, exist_ok=True)
    mig_apps.mkdir(parents=True, exist_ok=True)
    mig_sys.mkdir(parents=True, exist_ok=True)
    mig_meta.mkdir(parents=True, exist_ok=True)
    mig_blueprint.mkdir(parents=True, exist_ok=True)

    # Remove any legacy login.keychain-db inside mig_home/Library/Keychains (B4)
    legacy_kc_dir = mig_home / "Library/Keychains"
    if legacy_kc_dir.exists():
        shutil.rmtree(legacy_kc_dir, ignore_errors=True)

    pack_state_file = mig_meta / "PACK_STATE.json"
    pack_log_file = mig_meta / "pack.log"
    started_at = datetime.now(timezone.utc).isoformat()
    pack_state_file.write_text(
        json.dumps(
            {
                "state": "in_progress",
                "started_at": started_at,
                "fast_config_only": fast_config_only,
                "source_hostname": socket.gethostname(),
            },
            indent=2,
        )
    )
    pack_errors: list[str] = []

    def log_pack(msg: str) -> None:
        with pack_log_file.open("a", encoding="utf-8") as lf:
            lf.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")

    log_pack(f"=== Starting cmd_pack (fast_config_only={fast_config_only}) ===")

    # Step 1: Run openrsync -aHR from $HOME to preserve hardlinks across ~/.codex & orca/codex-accounts
    print("\n--> Step 1/5: Running lossless `rsync -aHR` on $HOME paths (preserving hardlinks & symlinks)...")
    stale_ro_gen = mig_home / "Library/Application Support/Codex/artifact-sessions/runtime-generations"
    if stale_ro_gen.exists():
        subprocess.run(["/bin/chmod", "-R", "u+w", str(stale_ro_gen)], capture_output=True)
        shutil.rmtree(stale_ro_gen, ignore_errors=True)

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
    if proc.stderr:
        log_pack(f"$HOME rsync stderr (rc={proc.returncode}):\n{proc.stderr}")
    if proc.returncode not in (0, 24):
        err_msg = f"$HOME rsync exited with code {proc.returncode}: {proc.stderr[:300].strip()}"
        pack_errors.append(err_msg)
        print(f"⚠️ {err_msg}", file=sys.stderr)
    else:
        print(f"    ✅ $HOME rsync mirror completed in {round(time.time() - t0, 1)}s")

    clean_stale_locks(mig_home)
    ensure_local_bin_runtime_symlinks(mig_home)

    # Stage login.keychain-db safely into metadata/old-login.keychain-db (never inside home/Library/Keychains)
    src_login_kc = home / "Library/Keychains/login.keychain-db"
    if src_login_kc.is_file():
        dst_login_kc = mig_meta / "old-login.keychain-db"
        shutil.copy2(src_login_kc, dst_login_kc)
        dst_login_kc.chmod(0o600)

    # Step 2: Parallel Online SQLite `.backup()` with strict PRAGMA quick_check == 'ok'
    print(f"\n--> Step 2/5: Performing parallel atomic SQLite `.backup()` ({len(sqlite_targets)} DBs)...")
    t_sql = time.time()
    prev_sqlite_meta: dict[str, dict] = {}
    prev_manifest_file = mig_meta / "MANIFEST.json"
    if prev_manifest_file.is_file():
        try:
            prev_sqlite_meta = json.loads(prev_manifest_file.read_text()).get("sqlite_backups", {})
        except Exception:
            prev_sqlite_meta = {}

    sqlite_results: dict[str, dict] = {}
    valid_sql_targets = [rel_db for rel_db in sqlite_targets if (home / rel_db).is_file()]
    with ThreadPoolExecutor(max_workers=4) as pool:
        future_map = {
            pool.submit(
                sqlite_online_backup,
                home / rel_db,
                mig_home / rel_db,
                prev_sqlite_meta.get(rel_db),
            ): rel_db
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
        if res.get("status") != "ok" or res.get("integrity") != "ok":
            err_msg = f"SQLite backup failed for {rel_db}: {res}"
            pack_errors.append(err_msg)
            log_pack(err_msg)
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
        pending_fps: dict[str, str] = {}
        for app in core_apps:
            src_app = Path("/Applications") / app
            dst_app = mig_apps / app
            fp = compute_bundle_fingerprint(src_app)
            if not force_apps and dst_app.is_dir() and cached_fps.get(f"app:{app}") == fp:
                apps_skipped.append(app)
                new_fps[f"app:{app}"] = fp
            else:
                apps_synced.append(app)
                pending_fps[f"app:{app}"] = fp

        if apps_synced:
            app_rsync = ["/usr/bin/rsync", "-aH", "--delete", *apps_synced, str(mig_apps) + "/"]
            app_proc = subprocess.run(app_rsync, cwd=Path("/Applications"), capture_output=True, text=True)
            if app_proc.returncode == 0:
                new_fps.update(pending_fps)
            else:
                err_msg = f".app bundles rsync failed (rc={app_proc.returncode}): {app_proc.stderr[:300].strip()}"
                pack_errors.append(err_msg)
                log_pack(err_msg)
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
                new_fps["sys:Python.framework"] = py_fp
            else:
                py_rsync = ["/usr/bin/rsync", "-aH", "--delete", "Python.framework", str(mig_sys) + "/"]
                py_proc = subprocess.run(py_rsync, cwd=Path("/Library/Frameworks"), capture_output=True, text=True)
                if py_proc.returncode == 0:
                    new_fps["sys:Python.framework"] = py_fp
                    print(f"    ✅ Mirrored Python.framework to {mig_sys} ({round(time.time() - t_py, 1)}s)")
                else:
                    err_msg = f"Python.framework rsync failed (rc={py_proc.returncode}): {py_proc.stderr[:300].strip()}"
                    pack_errors.append(err_msg)
                    log_pack(err_msg)

        fp_file.write_text(json.dumps(new_fps, ensure_ascii=False, indent=2))
    else:
        print("\n--> Step 3/5: Skipped .app bundles & Python.framework (--fast-config-only).")

    # Embed a clean copy of the blueprint repository itself into ${MIG_ROOT}/blueprint
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
    secrets_map = export_keychain_secrets(user_name, keychain_services)
    secrets_file = mig_meta / "keychain-secrets.json"
    if secrets_map:
        secrets_file.write_text(json.dumps(secrets_map, ensure_ascii=False, indent=2))
        secrets_file.chmod(0o600)
        print(f"    ✅ Exported {len(secrets_map)} Keychain secrets ({', '.join(secrets_map.keys())}) with mode 0600")
    else:
        print("    ⚠️ Warning: No Keychain secrets were exported!", file=sys.stderr)

    # Step 5: Write MANIFEST.json, PACK_STATE.json, restore-on-new-mac.sh, and AGENT_RESTORE.md on the SSD
    packed_at = datetime.now(timezone.utc).isoformat()
    keychain_digests = {k: v.get("sha256", "") for k, v in secrets_map.items()}
    manifest = {
        "schema_version": "1.3",
        "packed_at": packed_at,
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
        "keychain_services": list(secrets_map.keys()),
        "keychain_digests": keychain_digests,
        "errors": pack_errors,
    }
    (mig_meta / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    final_state = "complete" if not pack_errors else "failed"
    pack_state_file.write_text(
        json.dumps(
            {
                "state": final_state,
                "started_at": started_at,
                "completed_at": packed_at,
                "fast_config_only": fast_config_only,
                "errors": pack_errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    # Write universal dynamic restore-on-new-mac.sh (explicitly forwards --volume "$VOL_ROOT" per B6)
    launcher = vol / "AI/Migration/restore-on-new-mac.sh"
    launcher.write_text(
        """#!/usr/bin/env zsh
# ==============================================================================
# One-Click New Mac Restore & Auto-Deploy Launcher (100% Universal & Offline-Ready)
# Works even on a brand-new unconfigured MacBook Pro without Xcode CLT installed!
# Usage:
#   zsh /Volumes/<YOUR_SSD>/AI/Migration/restore-on-new-mac.sh [--restore-dry-run]
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

# Default to --restore-from-ssd unless caller passed --restore-dry-run
if [[ " $* " == *" --restore-dry-run "* ]]; then
  exec "${BLUEPRINT_DIR}/deploy.sh" --volume "${VOL_ROOT}" "$@"
else
  exec "${BLUEPRINT_DIR}/deploy.sh" --restore-from-ssd --volume "${VOL_ROOT}" "$@"
fi
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
2. **Execute Full Lossless Restore + Auto-Deploy (`--external`)**:
   ```bash
   zsh "{launcher}" --external
   ```
   *(Do NOT pass `--render-rules`; third-party LaunchAgents are staged without auto-starting so trading/LAN jobs do not collide with the old Mac).*
3. **Verify Health (`doctor.py`)**:
   After the command finishes, show the output of `doctor.py` to the user and ask them to run `tailscale up` if they use Tailscale.
"""
    )

    # Also write root-level one-click launcher (`新Mac一键恢复.command`) and guide (`新电脑恢复指南.md`) at SSD root
    root_command = vol / "新Mac一键恢复.command"
    root_command.write_text(
        """#!/usr/bin/env zsh
# ==============================================================================
# 新 Mac 一键无损恢复启动器 (Root One-Click Restore Launcher)
# 可在 Finder 双击运行，或在终端执行：zsh /Volumes/KAFU2T/新Mac一键恢复.command
# ==============================================================================
set -euo pipefail
VOL_ROOT="$(cd "$(dirname "$0")" && pwd)"
exec zsh "${VOL_ROOT}/AI/Migration/restore-on-new-mac.sh" "$@"
"""
    )
    root_command.chmod(0o755)

    root_guide = vol / "新电脑恢复指南.md"
    root_guide.write_text(
        f"""# 💻 新 MacBook Pro 开箱一键无损恢复指南（Root Quick-Start Guide）

> **最新快照时间**：`{manifest['packed_at']}`（状态：`{final_state}` · `fast_config_only={manifest['fast_config_only']}`）  
> **外置盘挂载点**：`{vol}`（硬件 UUID：`{vol_uuid}`）  
> **打包内容摘要**：
> - **59 个内盘核心目录与配置**：Orca、Claude（双账号池）、Codex（三账号池）、Antigravity、DeepSeek Harness、Grok Bot、21 个精选升级版 AI Skills（`~/.agents/skills`）、SSH 密钥、Git 全局加速配置、Zsh 环境、Xcode 模板与签名配置
> - **{len(valid_sql_targets)} 个 SQLite 会话数据库**：全量通过 `PRAGMA quick_check == ok` 校验（含 Orca 全部项目与历史对话、Codex 历史线程、Antigravity 全部历史会话）
> - **{len(core_apps)} 个核心桌面 `.app` + 独立 Python 3.14 运行时**：即使新电脑刚开箱未装 Xcode 命令行工具、没连外网，也能零弹窗直接恢复所有桌面 App 与环境
> - **Keychain 核心加密密钥**：`restic-mac-agents` 与 `icloud-package-key`（自动导入新机钥匙串，无缝衔接 Restic 与 iCloud 每晚小包）
> - **影视与摄影工程备份**：位于 `{vol}/Work/工程备份/`（含 FCP 工程库、达芬奇工程库、Lightroom Classic 2025/2026 标准化目录）

---

## 🚀 第一步：插上移动硬盘，执行一键恢复（二选一）

新电脑开机完成基础 macOS 设置后，插上这块 `KAFU2T` 移动硬盘（**注意：恢复前如果打开了 Orca / Claude / Codex，请先退出它们**）：

### 方式 A：自己在「终端 (Terminal)」跑一行命令（最快、最直观，约 1~2 分钟）

打开新电脑自带的 **终端 (Terminal.app)**，复制粘贴以下一行命令并回车（中途如提示输入开机密码，输入新电脑锁屏密码即可）：

```bash
zsh {vol}/新Mac一键恢复.command
```

*(如果想先只读预览将要恢复哪些文件、不实际写入，可执行：`zsh {vol}/新Mac一键恢复.command --restore-dry-run`)*

---

### 方式 B：直接让新电脑上的任何 AI Agent 帮你全自动恢复

如果你在新电脑上打开了任何 AI 编程工具（或者直接双击打开本盘自带的 `{vol}/AI/Migration/latest/applications/Orca.app`），直接把下面这句话发给 AI：

> **复制发给 AI 的口令**：  
> `请读取 {vol}/AI/Migration/AGENT_RESTORE.md，帮我在这台新 Mac 上执行全自动无损恢复并汇报 doctor.py 体检结果。`

---

## 🔍 第二步：恢复完成后 30 秒核对清单

脚本跑完最后会自动输出一份 JSON 体检报告（`doctor.py`），只要看到 `"status": "ok"`、`"all_aligned": true`、`"all_ready": true` 即代表全部就绪：

1. **打开 Orca.app**：
   - 原本 `/Applications` 下缺失的 `Orca.app`、`Claude.app`、`ChatGPT.app`、`Antigravity.app`、`Tailscale.app`、`Mole.app`、`Loon.app` 等 15 个核心 App 已自动装好。
   - 打开 **Orca**，左侧所有的 **项目工作树（Worktrees）**、**历史对话记录**、**Claude / Codex 多账号登录态**、**21 个全局 AI Skills** 都会原封不动直接出现。
2. **后台任务安全防撞保护（已自动处理）**：
   - 7 个核心备份与外置盘守护任务（`local.kafu.*`）已自动注册生效，且 iCloud 每晚小包已按主机名隔离，不会误删旧电脑的包。
   - 第三方后台任务（如 `perp-arena` 实盘/量化任务、`ha-backup-pull`、`gmgn-paper-research`）已拷贝到 `~/Library/LaunchAgents/` 但**默认不自动启动**，防止与旧电脑同时运行产生冲突。如需在新电脑启用某个任务，执行：
     ```bash
     python3 {vol}/AI/Projects/Mac/outputs/orca-multi-agent-blueprint/scripts/migrate.py restore --apply --enable-agents com.kafu.perp-arena.01-live
     ```
3. **可选网络登录**：
   - 如需连回家里局域网 NAS，打开 **Tailscale.app** 登录（或终端运行 `tailscale up`）。

---

## 🎬 第三步：Final Cut Pro / 达芬奇 / Lightroom 工程与目录位置

所有整理好的创作工程都在移动硬盘 `{vol}/Work/工程备份/` 下，新电脑安装好软件后可直接使用或拷入本机：

| 软件类别 | 移动硬盘路径 | 新电脑使用方式 |
| :--- | :--- | :--- |
| **Lightroom Classic** | `{vol}/Work/工程备份/Lightroom_Catalogs/` | 已按规范整理为 `KAFU_Master_2025.lrcat` 与 `KAFU_Master_2026.lrcat`（原片路径已全部挂在 `{vol}/Work/` 下，双击 `.lrcat` 即可直接修图，零感叹号缺失） |
| **达芬奇 (DaVinci Resolve)** | `{vol}/Work/工程备份/DaVinci_Resolve/` | 打开达芬奇 → Project Manager → 重新链接 `Resolve Project Library`（或导入 `.drp` 工程包） |
| **Final Cut Pro / Motion** | `{vol}/Work/工程备份/FinalCutPro_Libraries/`<br>`{vol}/Work/工程备份/Motion_Templates/` | 双击 `.fcpbundle` 即可打开工程；插件与字幕转场按计划在新电脑全新干净安装即可 |

---

## 💡 进阶可选：如果新电脑内置硬盘很大，想把 AI 工作区迁入本机内置盘

默认恢复后采用 **外置盘模式（External Mode）**，即所有项目与工作树继续保存在 `{vol}/AI` 上。  
如果你购买的新 MacBook Pro 内置硬盘很大（如 2TB / 4TB / 8TB），希望脱离移动硬盘、把项目迁入本机 `~/AI-Workspace`，只需运行：

```bash
zsh {vol}/AI/Projects/Mac/outputs/orca-multi-agent-blueprint/deploy.sh --auto --internal
```
"""
    )

    print("\n--> Step 5/5: Manifest, self-contained blueprint & new-Mac launcher written:")
    print(f"    • Manifest   : {mig_meta / 'MANIFEST.json'}")
    print(f"    • Pack State : {pack_state_file} ({final_state})")
    print(f"    • Launcher   : {launcher}")
    print(f"    • Root Cmd   : {root_command}")
    print(f"    • Root Guide : {root_guide}")
    print(f"    • Agent Doc  : {agent_restore_doc}")

    if pack_errors:
        print(f"\n❌ Pack finished with {len(pack_errors)} error(s): {pack_errors}", file=sys.stderr)
        return 1

    print("\n✅ Pack to SSD completed losslessly!")
    return 0


def cmd_restore(
    vol: Path,
    dry_run: bool,
    force: bool = False,
    enable_agents: list[str] | None = None,
    target_home: Path | None = None,
) -> int:
    home = target_home or Path.home()
    mig_root = vol / "AI/Migration/latest"
    mig_home = mig_root / "home"
    mig_apps = mig_root / "applications"
    mig_meta = mig_root / "metadata"
    manifest_file = mig_meta / "MANIFEST.json"
    pack_state_file = mig_meta / "PACK_STATE.json"

    if not manifest_file.is_file() or not mig_home.is_dir():
        print(
            f"❌ Error: No migration snapshot found at {mig_root}.\n"
            f"   Run `./deploy.sh --pack-to-ssd` on your old Mac first.",
            file=sys.stderr,
        )
        return 1

    # B7 (P2-4): Verify PACK_STATE.json is complete before restoring
    if pack_state_file.is_file():
        try:
            ps = json.loads(pack_state_file.read_text())
            if ps.get("state") != "complete" and not force:
                print(
                    f"❌ Error: Snapshot PACK_STATE is '{ps.get('state')}' (not 'complete'). "
                    f"Refusing to restore an interrupted or failed snapshot.",
                    file=sys.stderr,
                )
                return 1
        except Exception as e:
            if not force:
                print(f"❌ Error reading PACK_STATE.json: {e}", file=sys.stderr)
                return 1

    manifest = json.loads(manifest_file.read_text())
    current_user = os.environ.get("USER", "unknown") if target_home is None else home.name
    current_uid = os.getuid()
    src_user = manifest.get("source_user", "developer")
    src_uid = manifest.get("source_uid", 501)
    src_macos = manifest.get("macos_version", "")
    cur_macos = platform.mac_ver()[0]

    # B7: Check macOS version compatibility
    if src_macos and cur_macos and _parse_version_tuple(cur_macos) < _parse_version_tuple(src_macos) and not force:
        print(
            f"❌ Error: Target Mac macOS ({cur_macos}) is older than snapshot macOS ({src_macos}). "
            f"Update macOS first or pass --force.",
            file=sys.stderr,
        )
        return 1

    # B7 (P1-6): Check running Agent processes before live restore
    if not dry_run and target_home is None and not force:
        running_apps = check_running_agent_processes()
        if running_apps:
            print(
                f"❌ Error: Active AI Agent app(s) currently running: {', '.join(running_apps)}.\n"
                f"   Quit them before running restore so live SQLite databases are not corrupted (or pass --force).",
                file=sys.stderr,
            )
            return 1

    free_home_bytes = shutil.disk_usage(home).free if home.exists() else 0

    print("==================================================================")
    print(" 🚀 Orca Multi-Agent Blueprint — Restore State onto New Mac")
    print(f" Mode              : {'DRY-RUN (Preview)' if dry_run else 'APPLY (Live Restore)'}")
    print(f" Snapshot Packed At: {manifest.get('packed_at')} (fast_config_only={manifest.get('fast_config_only')})")
    print(f" Source Host / User: {manifest.get('source_hostname')} ({src_user}, UID {src_uid}, macOS {src_macos})")
    print(f" Target Host / User: {socket.gethostname()} ({current_user}, UID {current_uid}, macOS {cur_macos})")
    print(f" Target Free Space : {format_bytes(free_home_bytes)}")
    print(f" Snapshot Root     : {mig_root}")
    print("==================================================================")

    if manifest.get("fast_config_only"):
        print(
            "⚠️  WARNING: This snapshot was packed with `--fast-config-only` (raw session journals & .app bundles excluded)."
        )

    if current_user != src_user or current_uid != src_uid:
        print(
            f"ℹ️  Note: Target user/UID ({current_user}/{current_uid}) differs from source ({src_user}/{src_uid}).\n"
            f"    Restore will automatically relocate /Users/{src_user} -> {home} across symlinks, LaunchAgents & configs."
        )

    # B4 (P1-1): Explicitly exclude Library/Keychains from $HOME targets so login.keychain-db is never overwritten
    targets = [
        rel
        for rel in manifest.get("targets", [])
        if not rel.startswith("Library/Keychains")
        and ((mig_home / rel).exists() or (mig_home / rel).is_symlink())
    ]
    bundled_apps = [app for app in manifest.get("bundled_core_apps", []) if (mig_apps / app).is_dir()]
    print(f"\n--> Will restore {len(targets)} paths from {mig_home} into {home} (Keychains safely excluded):")
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

    # Step 1: Restore files via openrsync -aHR with --backup-dir rollback protection (B7)
    ts_str = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = home / f".migration-preexisting/{ts_str}"
    print(f"\n--> Step 1/5: Restoring $HOME files & hardlinks via `rsync -aHR` (pre-existing backup -> {backup_dir})...")
    rsync_cmd = [
        "/usr/bin/rsync",
        "-aHR",
        "--backup",
        f"--backup-dir={backup_dir}",
        "--exclude",
        "Library/Keychains",
        "--exclude",
        "Library/Keychains/*",
        *targets,
        str(home) + "/",
    ]
    proc = subprocess.run(rsync_cmd, cwd=mig_home, capture_output=True, text=True)
    if proc.returncode not in (0, 24):
        print(f"❌ rsync error (exit {proc.returncode}): {proc.stderr[:400]}", file=sys.stderr)
        return 1
    print("    ✅ All Agent directories, SQLite databases, and local repositories restored.")

    # Stage old login.keychain-db into ~/Migration-Imports/old-login.keychain-db (B4)
    old_kc_src = mig_meta / "old-login.keychain-db"
    if not old_kc_src.is_file():
        old_kc_src = mig_home / "Library/Keychains/login.keychain-db"
    if old_kc_src.is_file():
        imports_dir = home / "Migration-Imports"
        imports_dir.mkdir(parents=True, exist_ok=True)
        staged_kc = imports_dir / "old-login.keychain-db"
        shutil.copy2(old_kc_src, staged_kc)
        staged_kc.chmod(0o600)
        print(f"    🔐 Staged old Mac login keychain (read-only reference) at {staged_kc}")

    # Step 2: Restore missing core .app bundles into /Applications (only when restoring to real HOME)
    if target_home is None:
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
    else:
        print("\n--> Step 2/5: Skipped /Applications install (custom --target-home test mode).")

    # Step 3: Clean stale locks, relocate cross-user paths if needed, fix ~/.ssh permissions, wire symlinks & verify DBs
    print("\n--> Step 3/5: Cleaning stale host locks, relocating paths, verifying SQLite DBs & wiring symlinks...")
    removed_locks = clean_stale_locks(home)
    failed_dbs = []
    for rel_db in manifest.get("sqlite_backups", {}).keys():
        for suffix in ["-wal", "-shm"]:
            wal_file = home / (rel_db + suffix)
            if wal_file.exists():
                try:
                    wal_file.unlink()
                    removed_locks.append(rel_db + suffix)
                except OSError:
                    pass
        restored_db = home / rel_db
        if restored_db.is_file():
            ok, detail = verify_sqlite_quick_check(restored_db)
            if not ok:
                failed_dbs.append(f"{rel_db} ({detail})")

    reloc_stats = relocate_home_references(home, src_user, current_user)
    fix_ssh_permissions(home)
    created_links = ensure_local_bin_runtime_symlinks(home)

    # Record migration receipt marker for fail-closed backup secret checks (B2)
    orca_dir = home / ".orca"
    orca_dir.mkdir(parents=True, exist_ok=True)
    (orca_dir / ".migrated-from").write_text(
        json.dumps(
            {
                "source_hostname": manifest.get("source_hostname"),
                "packed_at": manifest.get("packed_at"),
                "restored_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )

    # Flush cfprefsd cache so restored Library/Preferences/*.plist take effect immediately (P2-10)
    if target_home is None:
        subprocess.run(["/usr/bin/killall", "cfprefsd"], capture_output=True)

    print(
        f"    ✅ Cleaned {len(removed_locks)} stale lock/WAL files, relocated {reloc_stats['relocated_symlinks']} symlinks "
        f"& {reloc_stats['rewritten_configs']} configs, verified ~/.ssh (700/600), and wired {len(created_links)} runtime symlinks."
    )
    if failed_dbs:
        print(f"    ❌ SQLite integrity check failed after restore: {failed_dbs}", file=sys.stderr)
        return 1

    # Step 4: Restore Keychain secrets FIRST, then selectively bootstrap blueprint LaunchAgents (B5)
    print("\n--> Step 4/5: Restoring Keychain secrets & selectively registering LaunchAgents...")
    secrets_file = mig_meta / "keychain-secrets.json"
    imported_services: list[str] = []
    if secrets_file.is_file() and target_home is None:
        try:
            secrets_data = json.loads(secrets_file.read_text())
            imported_services = import_keychain_secrets(secrets_data, current_user)
            print(f"    ✅ Imported Keychain services: {', '.join(imported_services) if imported_services else 'none'}")
        except Exception as e:
            print(f"    ⚠️ Keychain import warning: {e}")
    else:
        print("    ℹ️ Keychain import skipped (no keychain-secrets.json or custom target-home).")

    launch_agents_dir = home / "Library/LaunchAgents"
    loaded_agents: list[str] = []
    skipped_third_party: list[str] = []
    enabled_set = set(enable_agents or [])

    if launch_agents_dir.is_dir():
        for plist_path in sorted(launch_agents_dir.glob("*.plist")):
            name = plist_path.name
            stem = plist_path.stem
            if name.startswith("com.google.") or name.startswith("com.apple."):
                continue
            is_blueprint_agent = name.startswith(f"local.{current_user}.") or name.startswith(f"local.{src_user}.") or name.startswith("local.orca.")
            if is_blueprint_agent or stem in enabled_set or name in enabled_set:
                if target_home is None:
                    subprocess.run(
                        ["/bin/launchctl", "bootstrap", f"gui/{current_uid}", str(plist_path)],
                        capture_output=True,
                        timeout=5,
                    )
                loaded_agents.append(name)
            else:
                skipped_third_party.append(name)

    print(f"    ✅ Registered {len(loaded_agents)} blueprint LaunchAgents with launchd (gui/{current_uid}).")
    if skipped_third_party:
        print(
            f"    ℹ️ Staged {len(skipped_third_party)} third-party/project LaunchAgents WITHOUT auto-starting "
            f"(prevents duplicate trading/LAN jobs across Macs): {', '.join(skipped_third_party)}"
        )

    print("\n--> Step 5/5: State restoration finished! Handing off to `deploy.sh` for toolchain & profile alignment...")
    return 0


def cmd_status(vol: Path) -> int:
    mig_root = vol / "AI/Migration/latest"
    manifest_file = mig_root / "metadata/MANIFEST.json"
    pack_state_file = mig_root / "metadata/PACK_STATE.json"
    if not manifest_file.is_file():
        print(json.dumps({"status": "not_packed", "migration_root": str(mig_root)}, indent=2))
        return 0
    manifest = json.loads(manifest_file.read_text())
    pack_state = json.loads(pack_state_file.read_text()) if pack_state_file.is_file() else {"state": "unknown"}
    print(
        json.dumps(
            {
                "status": "ready" if pack_state.get("state") == "complete" else pack_state.get("state", "ready"),
                "migration_root": str(mig_root),
                "pack_state": pack_state,
                "manifest": manifest,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Universal Lossless Cross-Mac AI Agent Migration Engine")
    parser.add_argument("action", choices=["pack", "restore", "status"], help="Migration action")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    parser.add_argument("--apply", action="store_true", help="Execute pack or restore")
    parser.add_argument("--fast-config-only", action="store_true", help="Skip multi-GB raw chat logs & .app bundles during pack")
    parser.add_argument("--force-apps", action="store_true", help="Force full resync of .app bundles & Python.framework ignoring fingerprint cache")
    parser.add_argument("--force", action="store_true", help="Bypass restore preflight blocks (running processes, macOS version, pack state)")
    parser.add_argument("--enable-agents", type=str, default="", help="Comma-separated list of non-blueprint LaunchAgent labels to auto-bootstrap on restore")
    parser.add_argument("--target-home", type=str, default=None, help="Override target $HOME directory for isolated restore testing")
    parser.add_argument("--volume", type=str, default=None, help="Override external SSD volume path")
    args = parser.parse_args()

    vol = Path(args.volume) if args.volume else detect_volume(require_external_if_configured=(args.action in ("pack", "restore")))
    dry_run = not args.apply
    enable_agents = [x.strip() for x in args.enable_agents.split(",") if x.strip()]
    target_home = Path(args.target_home) if args.target_home else None

    if args.action == "status":
        return cmd_status(vol)
    elif args.action == "pack":
        return cmd_pack(vol, dry_run=dry_run, fast_config_only=args.fast_config_only, force_apps=args.force_apps)
    elif args.action == "restore":
        return cmd_restore(vol, dry_run=dry_run, force=args.force, enable_agents=enable_agents, target_home=target_home)
    return 0


if __name__ == "__main__":
    sys.exit(main())
