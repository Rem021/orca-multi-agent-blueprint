#!/usr/bin/env python3
"""
Orca Multi-Agent Blueprint — Universal Lossless Cross-Mac Migration Engine (migrate.py)

Packs all internal-disk AI Agent states, SQLite session/orchestration databases,
multi-account profiles, CLI toolchains, Node/Python runtimes, core .app bundles,
Xcode signing profiles, iOS pairing records, SSH/Git/Zsh configs, LaunchAgents,
and local repositories onto an external SSD (${AI_VOLUME_PATH}/AI/Migration/latest),
and restores them losslessly onto a new Mac without copying bulky VMs
(skips Claude Desktop claudevm.bundle / claude-code-vm and CoreSimulator/Devices).

Actions:
  pack     mirror this Mac's agent state into <volume>/AI/Migration/latest
  restore  restore that snapshot onto this Mac
  status   show the snapshot's manifest, origin and age
  agents   list / enable / hold the third-party LaunchAgents a restore staged
  guide    re-render the restore launcher and guides from the existing snapshot (no re-pack)

Safety properties:
- SQLite: every live database is copied through the online backup API (sqlite_snapshot.py), verified
  with `PRAGMA quick_check` and a SHA-256, and only then swapped into the snapshot, so a failed
  backup keeps the previous good copy.
- Pack refuses to replace a snapshot that was packed on a different Mac unless `--force` is given.
- Keychain: generic passwords are exported to a 0600 file and imported without ever appearing on a
  command line; the live `login.keychain-db` is never overwritten (the old one is only staged at
  `~/Migration-Imports/old-login.keychain-db`).
- Pack state tracking (`metadata/PACK_STATE.json`) plus rsync logging (`metadata/pack.log`); restore
  refuses an interrupted or failed snapshot.
- Restore preflight: running Agent apps, pack state, macOS version and free disk space; replaced
  files are kept under `~/.migration-preexisting/<timestamp>`.
- LaunchAgents: blueprint backup jobs are loaded only when their Keychain secrets are present.
  Third-party jobs from the snapshot are held with `launchctl disable`, so they do not start at the
  next login either (`migrate.py agents --enable <label>` opts in).
- Cross-username path relocation using `plistlib` (preserves binary plists) and boundary-aware regex.
"""

from __future__ import annotations

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

_SCRIPTS_DIR = Path(__file__).resolve().parent
for _extra in (_SCRIPTS_DIR, _SCRIPTS_DIR / "codex-storage"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from setup_backups import (  # noqa: E402
    keychain_add,
    load_launch_agent,
    local_host_name,
    machine_id,
    resolve_host_tag,
    shell_dq,
    write_private_file,
)
from sqlite_snapshot import SnapshotError, snapshot as sqlite_snapshot  # noqa: E402

SQLITE_MAGIC_HEADER = b"SQLite format 3\x00"
CORE_KEYCHAIN_SERVICES = ("restic-mac-agents", "icloud-package-key")
# Job suffixes of the LaunchAgents installed by setup_backups.py (local.<prefix>.<job>)
BLUEPRINT_JOBS = (
    "icloud-package",
    "backup-hot",
    "backup-projects",
    "backup-media",
    "maintenance",
    "restore-drill",
    "volume-guard",
    "agent-hygiene",
)
SYSTEM_AGENT_PREFIXES = ("com.apple.", "com.google.")
ROOT_COMMAND_NAME = "新Mac一键恢复.command"
ROOT_GUIDE_NAME = "新电脑恢复指南.md"
GUIDE_EXTRA_NAME = "guide-extra.md"


def read_json(path: Path) -> dict:
    """Parsed JSON object, or {} when the file is missing, unreadable or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_text_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    """Replace `path` in one rename so a reader never sees a half-written file."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_json_atomic(path: Path, payload: object) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


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
    extra = ",".join(
        filter(
            None,
            [
                os.environ.get("EXTRA_MIGRATION_PATHS", "") or cfg.get("EXTRA_MIGRATION_PATHS", ""),
                os.environ.get("EXTRA_RESTIC_PATHS", "") or cfg.get("EXTRA_RESTIC_PATHS", ""),
            ],
        )
    )
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
                    if "profile-state.db.backup." in str(wal_file):
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
        bundle_path / "Versions/Current/bin/python3",
        bundle_path / "Versions/Current/lib",
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
        # A database without a -wal file is complete on its own and is opened immutable: a plain
        # read-only open of a WAL-mode file still wants to create the -shm index next to it, which
        # litters the snapshot and fails outright on stricter SQLite builds (Apple's system Python).
        wal = Path(str(db_path) + "-wal")
        has_wal = wal.is_file() and wal.stat().st_size > 0
        uri = db_path.resolve().as_uri() + ("?mode=ro" if has_wal else "?mode=ro&immutable=1")
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
    """Consistent, verified copy of a live SQLite database into the snapshot.

    The copy is taken on the internal disk through the SQLite online backup API (sqlite_snapshot.py;
    it falls back to a stability-checked clone for databases that cannot be opened), verified with
    `PRAGMA quick_check` and hashed. It is then written beside `dst_db`, re-hashed, and renamed over
    it — a failure at any step leaves the previous good copy in the snapshot untouched."""
    t0 = time.time()

    def failed(reason: str) -> dict:
        return {
            "status": "error",
            "integrity": f"failed:{reason}",
            "size_bytes": 0,
            "sha256": "",
            "seconds": round(time.time() - t0, 2),
        }

    if not is_valid_sqlite_file(src_db):
        return failed("invalid_sqlite_header")

    src_wal = Path(str(src_db) + "-wal")

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
                    "method": "reused",
                    "consistent": True,
                    "seconds": round(time.time() - t0, 3),
                }
        except OSError:
            pass

    cache_root = Path.home() / "Library/Caches/orca-migrate-sqlite"
    tag = hashlib.sha256(str(src_db).encode("utf-8")).hexdigest()[:16]
    tmp_db = cache_root / f"snap-{os.getpid()}-{tag}-{src_db.name}"
    staged = dst_db.parent / f".{dst_db.name}.migrate-{os.getpid()}"

    def discard(path: Path) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                Path(str(path) + suffix).unlink()
            except OSError:
                pass

    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        dst_db.parent.mkdir(parents=True, exist_ok=True)
        had_wal = src_wal.exists()
        src_st = src_db.stat()

        # 1. Point-in-time copy on the fast internal disk
        info = sqlite_snapshot(src_db, tmp_db)

        # 2. Verify and hash it before anything in the snapshot changes
        ok, detail = verify_sqlite_quick_check(tmp_db)
        if not ok:
            return failed(detail)
        digest = compute_sha256(tmp_db)

        # 3. Copy to the snapshot volume, confirm the bytes arrived intact, then swap it in
        shutil.copyfile(tmp_db, staged)
        if compute_sha256(staged) != digest:
            return failed("copy_verification_mismatch")
        if not had_wal:
            # Keep the source mtime so the next pack can reuse this copy when nothing changed
            os.utime(staged, (src_st.st_atime, src_st.st_mtime))
        os.replace(staged, dst_db)
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                Path(str(dst_db) + suffix).unlink()
            except OSError:
                pass

        return {
            "status": "ok",
            "integrity": "ok",
            "size_bytes": dst_db.stat().st_size,
            "sha256": digest,
            "method": info["method"],
            "consistent": info["consistent"],
            "seconds": round(time.time() - t0, 2),
        }
    except (SnapshotError, OSError, sqlite3.Error) as e:
        return failed(str(e) or type(e).__name__)
    finally:
        discard(tmp_db)
        discard(staged)


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
    """Import secrets from either structured dict {svc: {account, password}} or legacy {svc: password}.

    The password goes to `security` on stdin (keychain_add), never on a command line."""
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
        if pw and keychain_add(svc, acct, pw):
            imported.append(svc)
    return imported


def keychain_has(service: str) -> bool:
    """True when a generic password for `service` exists (does not read the secret)."""
    try:
        r = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service], capture_output=True, timeout=5
        )
    except Exception:
        return False
    return r.returncode == 0


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

    py_link = link_persistent_python(home)
    if py_link:
        created.append(py_link)

    return created


def link_persistent_python(home: Path) -> str:
    """Point ~/.local/bin/python3 at the newest restored uv-managed CPython when the Mac has none.

    The backup jobs and agent hooks call a bare `python3`. A brand-new Mac only ships the
    /usr/bin/python3 stub, which opens the Command Line Tools installer instead of running, so the
    jobs loaded at the end of a restore would fail (and pop up that dialog) every time they fire."""
    local_bin = home / ".local/bin"
    link = local_bin / "python3"
    if link.is_symlink() and not link.exists():
        try:
            link.unlink()
        except OSError:
            return ""
    if link.exists() or any(Path(p).exists() for p in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3")):
        return ""

    def version(path: Path) -> tuple[int, ...]:
        m = re.match(r"cpython-(\d+(?:\.\d+)*)", path.parent.parent.name)
        return tuple(int(x) for x in m.group(1).split(".")) if m else ()

    for candidate in sorted((home / ".local/share/uv/python").glob("cpython-3.*/bin/python3"), key=version, reverse=True):
        try:
            ok = (
                subprocess.run(
                    [str(candidate), "-c", "import ssl, sqlite3, plistlib"], capture_output=True, timeout=30
                ).returncode
                == 0
            )
        except Exception:
            ok = False
        if not ok:
            continue
        try:
            local_bin.mkdir(parents=True, exist_ok=True)
            link.symlink_to(candidate)
        except OSError:
            return ""
        return f"~/.local/bin/python3 -> {candidate}"
    return ""


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
    apps = (
        "Orca",
        "Claude",
        "ChatGPT",
        "Codex",
        "Antigravity",
        "Muse",
        "Cursor",
        "DeepSeek Harness",
        "Grok Bot",
    )
    running: list[str] = []
    try:
        out = subprocess.check_output(["/bin/ps", "-ax", "-o", "comm="], text=True, timeout=5)
        lines = out.splitlines()
        for app in apps:
            # Any executable of the bundle counts: helpers keep the databases open as well
            needle = f"/{app}.app/Contents/"
            if any(needle in line for line in lines):
                running.append(app)
    except Exception:
        pass
    return running


def volume_is_encrypted(vol: Path) -> bool | None:
    """True/False for a mounted volume, None when it cannot be determined."""
    try:
        info = plistlib.loads(
            subprocess.check_output(
                ["/usr/sbin/diskutil", "info", "-plist", str(vol)], stderr=subprocess.DEVNULL, timeout=10
            )
        )
    except Exception:
        return None
    if "FileVault" not in info and "Encryption" not in info:
        return None
    return bool(info.get("FileVault") or info.get("Encryption"))


def manifest_host(manifest: dict) -> str:
    """Stable (network-independent) name of the Mac a snapshot was packed on."""
    return str(manifest.get("source_local_hostname") or str(manifest.get("source_hostname") or "").split(".")[0]).strip()


def snapshot_is_foreign(manifest: dict, host: str, machine: str) -> bool:
    """True when the snapshot described by `manifest` was packed on a different Mac."""
    if not manifest:
        return False
    src_machine = str(manifest.get("source_machine_id") or "")
    if src_machine and machine:
        return src_machine != machine
    src_host = manifest_host(manifest)
    return bool(src_host) and src_host != host


def check_volume_identity(vol: Path, cfg: dict[str, str]) -> str:
    """"" when `vol` may be written to, otherwise the reason it may not."""
    storage_mode = os.environ.get("STORAGE_MODE", "") or cfg.get("STORAGE_MODE", "auto")
    is_volume = str(vol).startswith("/Volumes/")
    # B10 (P2-7): Refuse silent fallback to internal disk when external mode or /Volumes/* is configured
    if (storage_mode == "external" or is_volume) and not (is_volume and os.path.ismount(vol)):
        return f"Target external volume {vol} is not mounted (refusing fallback to internal disk)."
    expected_uuid = (os.environ.get("AI_VOLUME_UUID", "") or cfg.get("AI_VOLUME_UUID", "AUTO")).strip()
    if is_volume and expected_uuid and expected_uuid not in ("AUTO", "LOCAL", "INTERNAL"):
        actual = get_volume_uuid(vol)
        if actual != expected_uuid:
            return f"Volume UUID mismatch on {vol} (expected {expected_uuid}, got {actual})."
    return ""


def is_blueprint_agent(name: str) -> bool:
    """True for the backup LaunchAgents installed by setup_backups.py (local.<prefix>.<job>)."""
    parts = name[: -len(".plist")].split(".") if name.endswith(".plist") else name.split(".")
    return len(parts) >= 3 and parts[0] == "local" and parts[-1] in BLUEPRINT_JOBS


def launch_agent_label(plist_path: Path) -> str:
    try:
        label = plistlib.loads(plist_path.read_bytes()).get("Label")
    except Exception:
        label = None
    return label if isinstance(label, str) and label else plist_path.stem


def third_party_agents(la_dir: Path) -> list[tuple[str, Path]]:
    """(label, plist) of every LaunchAgent that is neither a system nor a blueprint job."""
    found: list[tuple[str, Path]] = []
    if la_dir.is_dir():
        for plist_path in sorted(la_dir.glob("*.plist")):
            if plist_path.name.startswith(SYSTEM_AGENT_PREFIXES) or is_blueprint_agent(plist_path.name):
                continue
            found.append((launch_agent_label(plist_path), plist_path))
    return found


def launchd_loaded(uid: int, label: str) -> bool:
    try:
        return (
            subprocess.run(["/bin/launchctl", "print", f"gui/{uid}/{label}"], capture_output=True, timeout=10).returncode
            == 0
        )
    except Exception:
        return False


def launchd_disabled_labels(uid: int) -> set[str]:
    """Labels with a persistent `launchctl disable` override in the user's GUI domain."""
    try:
        out = subprocess.run(
            ["/bin/launchctl", "print-disabled", f"gui/{uid}"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return set()
    return {m.group(1) for m in re.finditer(r'"([^"]+)"\s*=>\s*(?:disabled|true)\b', out)}


def launchd_set_enabled(uid: int, label: str, enabled: bool) -> bool:
    try:
        r = subprocess.run(
            ["/bin/launchctl", "enable" if enabled else "disable", f"gui/{uid}/{label}"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return False
    return r.returncode == 0


def plan_restored_agents(la_dir: Path, snapshot_la_dir: Path, enabled: set[str]) -> dict[str, list[tuple[str, Path]]]:
    """Sort the LaunchAgents present after a restore into what restore does with them.

    blueprint: backup jobs of this blueprint -> loaded once their Keychain secrets are verified
    enable   : third-party jobs the caller opted into (--enable-agents)
    hold     : third-party jobs that came from the snapshot -> `launchctl disable`, because launchd
               loads every plist in ~/Library/LaunchAgents at the next login whether or not restore
               bootstrapped it
    Jobs that were already on this Mac and are not part of the snapshot are left alone."""
    plan: dict[str, list[tuple[str, Path]]] = {"blueprint": [], "enable": [], "hold": []}
    if not la_dir.is_dir():
        return plan
    for plist_path in sorted(la_dir.glob("*.plist")):
        name = plist_path.name
        if name.startswith(SYSTEM_AGENT_PREFIXES):
            continue
        label = launch_agent_label(plist_path)
        if is_blueprint_agent(name):
            plan["blueprint"].append((label, plist_path))
        elif enabled & {label, name, plist_path.stem}:
            plan["enable"].append((label, plist_path))
        elif (snapshot_la_dir / name).exists():
            plan["hold"].append((label, plist_path))
    return plan


def snapshot_size_bytes(path: Path, timeout: int = 300) -> int | None:
    try:
        r = subprocess.run(["/usr/bin/du", "-sk", str(path)], capture_output=True, text=True, timeout=timeout)
        return int(r.stdout.split()[0]) * 1024
    except Exception:
        return None


def rewrite_inherited_host_tag(home: Path, manifest: dict, current: str, this_machine: str, keep: bool = False) -> str:
    """Give a restored ~/.orca/backup-config.env this Mac's own BACKUP_HOST_TAG.

    Returns the new tag, or "" when nothing had to change. Without this, the backup jobs loaded right
    after a restore publish under the source Mac's name and prune that Mac's packages."""
    env_file = home / ".orca/backup-config.env"
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'^(export[ \t]+)?BACKUP_HOST_TAG=(["\']?)([^"\'\n]*)\2[ \t]*$', text, flags=re.M)
    if not m:
        return ""
    tag, _ = resolve_host_tag(
        m.group(3),
        current,
        manifest_host(manifest),
        keep=keep,
        source_machine=str(manifest.get("source_machine_id") or ""),
        this_machine=this_machine,
    )
    if tag == m.group(3):
        return ""
    new_line = f'{m.group(1) or ""}BACKUP_HOST_TAG="{shell_dq(tag)}"'
    write_private_file(env_file, text[: m.start()] + new_line + text[m.end() :])
    return tag


def _parse_version_tuple(ver_str: str) -> tuple[int, ...]:
    parts = []
    for p in (ver_str or "").split("."):
        if p.isdigit():
            parts.append(int(p))
    return tuple(parts) or (0,)


def blueprint_dir_for(vol: Path) -> Path:
    """Where the restore instructions should point: the live checkout when this script runs from the
    snapshot volume, otherwise the copy embedded in the snapshot."""
    repo_root = Path(__file__).resolve().parent.parent
    embedded = vol / "AI/Migration/latest/blueprint"
    try:
        repo_root.relative_to(vol.resolve())
    except ValueError:
        return embedded
    return repo_root


def _env_file_value(path: Path, key: str) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = re.search(rf'^(?:export[ \t]+)?{re.escape(key)}=(["\']?)(.*?)\1[ \t]*$', text, flags=re.M)
    return m.group(2) if m else ""


def _local_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()
    except (TypeError, ValueError):
        return iso or "unknown"


def snapshot_facts(vol: Path, manifest: dict) -> dict:
    """Everything the generated guides state about a snapshot, read from the snapshot itself."""
    mig_root = vol / "AI/Migration/latest"
    mig_home = mig_root / "home"
    sqlite_meta = manifest.get("sqlite_backups") or {}
    orca_dir = mig_home / "Library/Application Support/orca"

    def count_dirs(path: Path) -> int:
        try:
            return sum(1 for p in path.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return 0

    def count_account_homes(path: Path) -> int:
        try:
            return sum(1 for p in path.iterdir() if (p / "home").is_dir())
        except OSError:
            return 0

    snapshot_la = mig_home / "Library/LaunchAgents"
    blueprint_jobs = (
        sorted(p.stem for p in snapshot_la.glob("*.plist") if is_blueprint_agent(p.name)) if snapshot_la.is_dir() else []
    )
    uv_pythons = sorted(p.parent.parent.name for p in (mig_home / ".local/share/uv/python").glob("cpython-3.*/bin/python3"))
    smb_match = _env_file_value(mig_home / ".orca/backup-config.env", "BACKUP_NAS_SMB_MATCH")
    m_smb = re.match(r"//(\S+?)(?:\s+on\s|$)", smb_match)
    return {
        "packed_local": _local_time(str(manifest.get("packed_at") or "")),
        "source_host": manifest_host(manifest) or "unknown",
        "source_user": manifest.get("source_user") or "unknown",
        "macos": manifest.get("macos_version") or "unknown",
        "arch": manifest.get("arch") or "unknown",
        "volume_uuid": manifest.get("volume_uuid") or get_volume_uuid(vol),
        "encrypted": manifest.get("volume_encrypted", volume_is_encrypted(vol)),
        "fast_config_only": bool(manifest.get("fast_config_only")),
        "targets_count": len(manifest.get("targets") or []),
        "sqlite_total": len(sqlite_meta),
        "sqlite_ok": sum(1 for v in sqlite_meta.values() if isinstance(v, dict) and v.get("integrity") == "ok"),
        "apps": [a[:-4] if a.endswith(".app") else a for a in manifest.get("bundled_core_apps") or []],
        "uv_pythons": uv_pythons,
        "python_framework": (mig_root / "system/Python.framework").is_dir(),
        "keychain_services": list(manifest.get("keychain_services") or []),
        "blueprint_jobs": blueprint_jobs,
        "third_party_agents": [label for label, _ in third_party_agents(snapshot_la)],
        "skills_count": count_dirs(mig_home / ".agents/skills"),
        "claude_profiles": count_account_homes(orca_dir / "claude-profiles"),
        "codex_accounts": count_account_homes(orca_dir / "codex-accounts"),
        "nas_smb_url": f"smb://{m_smb.group(1)}" if m_smb else "",
        "has_tailscale": "Tailscale.app" in (manifest.get("bundled_core_apps") or []),
    }


def render_launcher(live_blueprint_rel: str) -> str:
    """restore-on-new-mac.sh. It needs nothing but zsh: deploy.sh locates a working Python itself."""
    return f"""#!/usr/bin/env zsh
# ==============================================================================
# New-Mac restore launcher. Generated by migrate.py — edits are overwritten.
# Works on a freshly set up Mac without Xcode Command Line Tools or network access.
# Usage:
#   zsh <volume>/AI/Migration/restore-on-new-mac.sh [--restore-dry-run] [deploy.sh flags]
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VOL_ROOT="$(cd "${{SCRIPT_DIR}}/../.." && pwd)"
export AI_VOLUME_PATH="${{VOL_ROOT}}"
MIG_ROOT="${{VOL_ROOT}}/AI/Migration/latest"

if [[ ! -f "${{MIG_ROOT}}/metadata/MANIFEST.json" || ! -d "${{MIG_ROOT}}/home" ]]; then
  echo "❌ Error: Migration snapshot not found at ${{MIG_ROOT}}" >&2
  exit 1
fi

# Prefer the live blueprint checkout on this volume, fall back to the copy inside the snapshot
LIVE_BLUEPRINT_REL="{live_blueprint_rel}"
if [[ -n "$LIVE_BLUEPRINT_REL" && -f "${{VOL_ROOT}}/${{LIVE_BLUEPRINT_REL}}/deploy.sh" ]]; then
  BLUEPRINT_DIR="${{VOL_ROOT}}/${{LIVE_BLUEPRINT_REL}}"
else
  BLUEPRINT_DIR="${{MIG_ROOT}}/blueprint"
fi
if [[ ! -f "${{BLUEPRINT_DIR}}/deploy.sh" ]]; then
  echo "❌ Error: deploy.sh not found in ${{BLUEPRINT_DIR}}" >&2
  exit 1
fi

# Default to --restore-from-ssd unless the caller asked for a preview
if [[ " $* " == *" --restore-dry-run "* ]]; then
  exec /bin/zsh "${{BLUEPRINT_DIR}}/deploy.sh" --volume "${{VOL_ROOT}}" "$@"
else
  exec /bin/zsh "${{BLUEPRINT_DIR}}/deploy.sh" --restore-from-ssd --volume "${{VOL_ROOT}}" "$@"
fi
"""


def render_root_command() -> str:
    return f"""#!/usr/bin/env zsh
# ==============================================================================
# 新 Mac 一键恢复启动器（由 migrate.py 生成，手动修改会被覆盖）
# 在访达中双击运行，或在终端执行：zsh "<本磁盘>/{ROOT_COMMAND_NAME}"
# 只预览不写入：在命令末尾加 --restore-dry-run
# ==============================================================================
set -euo pipefail
VOL_ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /bin/zsh "${{VOL_ROOT}}/AI/Migration/restore-on-new-mac.sh" "$@"
"""


def render_agent_restore(vol: Path, manifest: dict, pack_state: str, facts: dict) -> str:
    launcher = vol / "AI/Migration/restore-on-new-mac.sh"
    migrate_py = blueprint_dir_for(vol) / "scripts/migrate.py"
    held = ", ".join(f"`{x}`" for x in facts["third_party_agents"]) or "none"
    return f"""# New-Mac Restore — Instructions for AI Agents

> Generated by `migrate.py`; overwritten on every pack. Human-readable guide: `{vol / ROOT_GUIDE_NAME}`.

| | |
| :--- | :--- |
| Snapshot | `{vol / 'AI/Migration/latest'}` |
| Packed | {facts['packed_local']} on `{facts['source_host']}` (user `{facts['source_user']}`, macOS {facts['macos']}, {facts['arch']}) |
| State | `{pack_state}`{' · packed with `--fast-config-only` (no raw session journals or .app bundles)' if facts['fast_config_only'] else ''} |

## Before you start

- Restore replaces the databases of Orca, Claude, ChatGPT/Codex, Antigravity and similar desktop apps,
  and refuses to run while one of them is open. **If you are running inside one of those apps, stop:**
  ask the user to quit them and run step 2 from Terminal. Do not pass `--force` to get around it.
- Do not edit files under `{vol / 'AI/Migration/latest'}`; treat the snapshot as read-only.
- Never print the contents of `metadata/keychain-secrets.json`, `home/.ssh` or any token file.

## Steps

1. **Preview** (writes nothing):
   ```bash
   zsh "{launcher}" --restore-dry-run
   ```
2. **Restore and deploy**:
   ```bash
   zsh "{launcher}"
   ```
   It may ask once for the login password (installing apps or a Python runtime). Files that already
   exist on this Mac are kept under `~/.migration-preexisting/<timestamp>/`.
3. **Report** the health check that runs at the end (`doctor.py`): show the user `summary.attention`
   and help them work through every entry.

## What restore deliberately leaves to the user

- **Third-party LaunchAgents** from the old Mac ({held}) are copied but held with
  `launchctl disable`, so the same job never runs on two Macs. List or enable them with:
  ```bash
  python3 "{migrate_py}" agents
  python3 "{migrate_py}" agents --enable <label>
  ```
  Ask before enabling anything; the old Mac may still be running it.
- **NAS / SMB credentials** are not migrated. If backups target a network share, the user has to
  connect to it once in Finder and save the password.
- **Tailscale and other network logins** need the user to sign in again.
"""


def render_guide(vol: Path, manifest: dict, pack_state: str, facts: dict, extra: str = "") -> str:
    """新电脑恢复指南.md — every number and name comes from the snapshot, nothing is hardcoded."""
    blueprint = blueprint_dir_for(vol)
    migrate_py = blueprint / "scripts/migrate.py"
    deploy_sh = blueprint / "deploy.sh"
    root_command = vol / ROOT_COMMAND_NAME

    def code_list(items: list[str]) -> str:
        return "、".join(f"`{x}`" for x in items)

    if facts["encrypted"] is True:
        disk_note = "已加密"
    elif facts["encrypted"] is False:
        disk_note = "**未加密**，见第 5 节"
    else:
        disk_note = "加密状态未知"

    accounts = []
    if facts["claude_profiles"]:
        accounts.append(f"Claude 账号 {facts['claude_profiles']} 个")
    if facts["codex_accounts"]:
        accounts.append(f"Codex 账号 {facts['codex_accounts']} 个")
    if facts["skills_count"]:
        accounts.append(f"全局 Skills {facts['skills_count']} 个")

    runtimes = []
    if facts["uv_pythons"]:
        runtimes.append(f"免安装 CPython（{facts['uv_pythons'][-1]}）")
    if facts["python_framework"]:
        runtimes.append("Python.framework（备用，安装时需输入一次开机密码）")
    runtime_text = "；".join(runtimes) or "未打包，新电脑需自带 Python 3.9 以上"

    rows = [
        ("打包时间", f"{facts['packed_local']} · 状态 `{pack_state}`"),
        ("来源电脑", f"`{facts['source_host']}`（用户 `{facts['source_user']}`，macOS {facts['macos']}，{facts['arch']}）"),
        ("所在磁盘", f"`{vol}`（卷 UUID `{facts['volume_uuid']}`，{disk_note}）"),
        ("目录与配置", f"{facts['targets_count']} 项：Agent 状态与会话、CLI 工具链、SSH / Git / Zsh、Xcode 签名配置、本机仓库"),
        ("SQLite 数据库", f"{facts['sqlite_total']} 个，其中 {facts['sqlite_ok']} 个通过 `quick_check` 校验"),
    ]
    if accounts:
        rows.append(("账号与技能", "，".join(accounts)))
    if facts["apps"]:
        rows.append(("桌面 App", f"{len(facts['apps'])} 个：{'、'.join(facts['apps'])}"))
    rows.append(("Python 运行时", runtime_text))
    if facts["keychain_services"]:
        rows.append(("钥匙串密钥", code_list(facts["keychain_services"])))
    rows.append(("不包含", "虚拟机镜像（Claude VM、iOS 模拟器设备）、各类缓存、Xcode 本体、NAS / 网络登录密码"))
    overview = "\n".join(f"| {k} | {v} |" for k, v in rows)

    fast_warning = (
        "\n> ⚠️ 这是一份 `--fast-config-only` 快照：不含原始会话日志和 `.app`，只适合同步配置，不能当作完整迁移。\n"
        if facts["fast_config_only"]
        else ""
    )
    state_warning = (
        f"\n> ⚠️ 快照状态是 `{pack_state}`，不是 `complete`。恢复会被拒绝，请回来源电脑重新执行 `deploy.sh --pack-to-ssd`。\n"
        if pack_state != "complete"
        else ""
    )

    checks = [
        "体检输出里的 `summary.attention` 为空。里面每一行都是一件需要处理的事。",
        "打开 Orca：项目、工作树、历史对话和各账号登录状态都在。",
        f"查看备份面板：`zsh \"{deploy_sh}\" --backup-status`，各任务应为 `ok`（刚恢复时还没到运行时间属正常）。",
    ]
    if facts["nas_smb_url"]:
        checks.append(
            f"NAS 备份需要在新电脑上保存一次共享密码：访达 →「前往」→「连接服务器」→ `{facts['nas_smb_url']}`，"
            "勾选「在我的钥匙串中记住此密码」。没做这一步，Restic 备份会一直显示 `skipped`。"
        )
    if facts["has_tailscale"]:
        checks.append("需要访问家里或公司内网时，打开 Tailscale 重新登录。")
    checks.append("要用 Xcode 的项目：从 App Store 安装 Xcode，快照里只有它的配置和签名描述文件。")
    checklist = "\n".join(f"- [ ] {c}" for c in checks)

    job_rows = []
    if facts["blueprint_jobs"]:
        job_rows.append(
            f"| 蓝图备份任务（{len(facts['blueprint_jobs'])} 个） | {code_list(facts['blueprint_jobs'])} | 钥匙串密钥就绪后自动启用 |"
        )
    if facts["third_party_agents"]:
        job_rows.append(
            f"| 第三方任务（{len(facts['third_party_agents'])} 个） | {code_list(facts['third_party_agents'])} | **暂停**，重启后也不会自己运行 |"
        )
    jobs_table = (
        "| 类型 | 任务 | 恢复后的状态 |\n| :--- | :--- | :--- |\n" + "\n".join(job_rows)
        if job_rows
        else "快照里没有后台任务。"
    )

    if facts["encrypted"] is False:
        encryption_advice = (
            "- 本磁盘**没有加密**：拿到磁盘的人可以直接读取上面这些内容。建议在访达中右键磁盘 →「加密」，"
            "或在迁移完成后删除 `AI/Migration/latest/metadata/` 下的 `keychain-secrets.json` 和 `old-login.keychain-db`。"
        )
    elif facts["encrypted"] is True:
        encryption_advice = "- 本磁盘已加密，拔出后需要密码才能读取。请保管好磁盘密码。"
    else:
        encryption_advice = "- 无法判断本磁盘是否加密。如果没有加密，请像保管密码本一样保管它。"

    extra_block = f"\n---\n\n{extra.strip()}\n" if extra.strip() else ""

    return f"""# 新 Mac 恢复指南

> 本文件由 `migrate.py` 根据快照内容自动生成，每次打包或执行 `deploy.sh --refresh-guide` 时都会重写。
> 需要长期保留的个人备注请写在 `AI/Migration/{GUIDE_EXTRA_NAME}`，它会原样附在文末。
{state_warning}{fast_warning}
## 快照概览

| 项目 | 内容 |
| :--- | :--- |
{overview}

## 1. 开始之前

1. 新电脑完成 macOS 初始设置，系统版本不低于来源电脑（macOS {facts['macos']}）。
2. 接上本磁盘，确认它出现在访达里。
3. 退出 Orca、Claude、ChatGPT、Codex、Antigravity 等 AI 桌面应用。恢复会替换它们的数据库，应用开着时会被拒绝。
4. 来源电脑不需要关机：两台电脑的备份按各自主机名分开存放，第三方后台任务在新电脑上默认暂停（见第 4 节）。

## 2. 执行恢复

### 方式 A：终端一行命令（推荐）

打开「终端」，粘贴后回车：

```bash
zsh "{root_command}"
```

- 也可以在访达里双击 `{ROOT_COMMAND_NAME}`。
- 只想预览、不写入任何文件：在命令末尾加 ` --restore-dry-run`。
- 过程中可能要求输入一次开机密码，用于安装 App 或 Python 运行时。
- 新电脑上已有的同名文件不会丢，被替换的版本保存在 `~/.migration-preexisting/<时间>/`。

### 方式 B：交给 AI Agent

在**终端里运行**的 AI Agent（Claude Code、Codex CLI 等）中发送：

> 请读取 `{vol / 'AI/Migration/AGENT_RESTORE.md'}`，按其中步骤在这台 Mac 上执行恢复，并汇报体检结果。

不要在 Orca 等桌面应用里发起恢复：恢复要求它们处于退出状态。

## 3. 恢复后核对

恢复结束时会自动运行一次体检（`doctor.py`）。逐项确认：

{checklist}

## 4. 后台任务

{jobs_table}

第三方任务被暂停，是为了避免同一个任务在两台电脑上同时运行（重复下单、重复同步、互相覆盖数据）。确认来源电脑上已经停掉后，再在新电脑启用：

```bash
python3 "{migrate_py}" agents                    # 查看每个任务的状态
python3 "{migrate_py}" agents --enable <Label>   # 启用并立即加载
python3 "{migrate_py}" agents --hold <Label>     # 重新暂停
```

## 5. 安全须知

- 快照里有明文敏感数据：`AI/Migration/latest/metadata/keychain-secrets.json`（备份加密密钥）、`home/.ssh/`（SSH 私钥）以及各 Agent 的登录令牌。
{encryption_advice}
- 恢复后，来源电脑的登录钥匙串副本位于 `~/Migration-Imports/old-login.keychain-db`，仅供查找旧密码，确认用不到后可以删除。

## 6. 常见问题

| 现象 | 处理 |
| :--- | :--- |
| 提示有 AI 应用正在运行 | 退出这些应用后重新执行。不要用 `--force` 跳过，运行中的数据库会被写坏。 |
| 提示快照状态不是 `complete` | 上次打包被中断或失败。回来源电脑重新执行 `deploy.sh --pack-to-ssd`。 |
| 提示磁盘空间不足 | 按提示的大小清理内置硬盘后重试。 |
| 弹出「需要安装命令行开发者工具」 | 可以点「取消」，恢复不依赖它。之后需要 `git` 或 Xcode 时再安装。 |
| 备份任务显示 `skipped` | 备份目标不可用，常见原因是 NAS 没连上或没保存共享密码，见第 3 节。 |
| 想撤销这次恢复 | 被替换的文件都在 `~/.migration-preexisting/<时间>/`，按原路径移回即可。 |

## 7. 进阶：把工作区迁入内置硬盘

恢复后默认是外置盘模式：项目和工作树仍在 `{vol}/AI`。如果新电脑内置硬盘足够大，想脱离外置盘：

```bash
zsh "{deploy_sh}" --auto --internal
```

这条命令只在 `~/AI-Workspace` 下建立目录结构，**不会**复制外置盘上的项目，需要自行把 `{vol}/AI/Projects` 拷贝过去。
{extra_block}"""


def sync_blueprint_copy(mig_blueprint: Path) -> str:
    """Mirror this checkout into the snapshot so a restore never depends on network access.

    Returns "" on success, otherwise the rsync error."""
    repo_root = Path(__file__).resolve().parent.parent
    if repo_root == mig_blueprint.resolve():
        return ""  # running from the embedded copy itself
    mig_blueprint.mkdir(parents=True, exist_ok=True)
    cmd = ["/usr/bin/rsync", "-a", "--delete"]
    for pattern in (".git", "__pycache__", "*.pyc", ".DS_Store", "*.bak*"):
        cmd.extend(["--exclude", pattern])
    cmd.extend([f"{repo_root}/", f"{mig_blueprint}/"])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return f"blueprint rsync failed (rc={r.returncode}): {r.stderr[:300].strip()}"
    return ""


def write_restore_artifacts(vol: Path, manifest: dict, pack_state: str) -> dict[str, Path]:
    """(Re)write the launcher, the agent instructions and the root-level guide for a snapshot."""
    mig_dir = vol / "AI/Migration"
    mig_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "launcher": mig_dir / "restore-on-new-mac.sh",
        "agent_doc": mig_dir / "AGENT_RESTORE.md",
        "root_command": vol / ROOT_COMMAND_NAME,
        "root_guide": vol / ROOT_GUIDE_NAME,
    }
    facts = snapshot_facts(vol, manifest)
    try:
        extra = (mig_dir / GUIDE_EXTRA_NAME).read_text(encoding="utf-8")
    except OSError:
        extra = ""
    blueprint = blueprint_dir_for(vol)
    embedded = vol / "AI/Migration/latest/blueprint"
    live_rel = "" if blueprint == embedded else str(blueprint.relative_to(vol.resolve()))

    write_text_atomic(paths["launcher"], render_launcher(live_rel), 0o755)
    write_text_atomic(paths["agent_doc"], render_agent_restore(vol, manifest, pack_state, facts))
    write_text_atomic(paths["root_command"], render_root_command(), 0o755)
    write_text_atomic(paths["root_guide"], render_guide(vol, manifest, pack_state, facts, extra))

    # Root-level shortcut to the bundled Orca.app
    root_orca_link = vol / "Orca.app"
    if (vol / "AI/Migration/latest/applications/Orca.app").is_dir():
        try:
            if root_orca_link.is_symlink() or root_orca_link.is_file():
                root_orca_link.unlink()
            root_orca_link.symlink_to("AI/Migration/latest/applications/Orca.app")
        except OSError:
            pass
    return paths


def dump_brewfile(dst: Path) -> None:
    brew_bin = shutil.which("brew") or ("/opt/homebrew/bin/brew" if Path("/opt/homebrew/bin/brew").exists() else None)
    if not brew_bin:
        return
    try:
        subprocess.run(
            [brew_bin, "bundle", "dump", "--file", str(dst), "--force"], capture_output=True, text=True, timeout=180
        )
    except Exception:
        pass


def copy_private(src: Path, dst: Path) -> None:
    """Copy `src` to `dst` so that the copy is 0600 from the moment it is created."""
    tmp = dst.with_name(f".{dst.name}.tmp-{os.getpid()}")
    try:
        with src.open("rb") as fin, os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fout:
            shutil.copyfileobj(fin, fout)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def is_safe_relative(rel: object) -> bool:
    """True for a plain relative path that cannot escape the directory it is joined to."""
    if not isinstance(rel, str) or not rel or rel.startswith("/"):
        return False
    return ".." not in Path(rel).parts


def prune_stale_targets(mig_home: Path, previous: list, current: list[str]) -> list[str]:
    """Drop top-level targets an earlier pack mirrored that this pack no longer covers.

    `rsync --delete` only cleans inside the targets it is given, so a directory that was removed from
    the source Mac (or from the target list) would otherwise sit in the snapshot forever."""
    removed: list[str] = []
    for rel in previous:
        if not is_safe_relative(rel) or rel in current:
            continue
        # Never touch a path that contains, or is contained in, something this pack mirrors
        if any(cur.startswith(rel + "/") or rel.startswith(cur + "/") for cur in current):
            continue
        stale = mig_home / rel
        try:
            if stale.is_symlink() or stale.is_file():
                stale.unlink()
            elif stale.is_dir():
                subprocess.run(["/bin/chmod", "-R", "u+w", str(stale)], capture_output=True)
                shutil.rmtree(stale)
            else:
                continue
        except OSError:
            continue
        removed.append(rel)
    return removed


def cmd_pack(
    vol: Path,
    dry_run: bool,
    fast_config_only: bool,
    force_apps: bool = False,
    force: bool = False,
    home: Path | None = None,
) -> int:
    live = home is None  # a custom `home` is the isolated test mode: no /Applications, Keychain or brew
    home = home or Path.home()
    cfg = _load_config_fallbacks(home)

    problem = check_volume_identity(vol, cfg)
    if problem:
        print(f"❌ Error: {problem}", file=sys.stderr)
        return 1
    vol_uuid = get_volume_uuid(vol)

    mig_root = vol / "AI/Migration/latest"
    mig_home = mig_root / "home"
    mig_apps = mig_root / "applications"
    mig_sys = mig_root / "system"
    mig_meta = mig_root / "metadata"
    mig_blueprint = mig_root / "blueprint"

    this_host = local_host_name()
    this_machine = machine_id()
    prev_manifest = read_json(mig_meta / "MANIFEST.json")
    foreign = snapshot_is_foreign(prev_manifest, this_host, this_machine)

    existing_targets = [
        rel for rel in get_home_targets(home) if (home / rel).exists() or (home / rel).is_symlink()
    ]
    sqlite_targets = discover_all_sqlite_targets(home)
    core_apps = discover_core_apps(home) if live else []
    keychain_services = discover_keychain_services(home) if live else []
    py_fw = Path("/Library/Frameworks/Python.framework")
    has_py_fw = live and py_fw.is_dir()

    if fast_config_only:
        sqlite_targets = [
            s
            for s in sqlite_targets
            if not s.endswith("agent-session-journal.db") and not s.endswith("thread_history_1.sqlite")
        ]

    free_bytes = shutil.disk_usage(vol).free if vol.exists() else 0
    encrypted = volume_is_encrypted(vol) if str(vol).startswith("/Volumes/") else None
    running_apps = check_running_agent_processes() if live else []

    print("==================================================================")
    print(" 📦 Orca Multi-Agent Blueprint — Pack Internal State to SSD")
    print(f" Mode              : {'DRY-RUN (Preview)' if dry_run else 'APPLY (Live Pack)'}")
    print(f" Source Host       : {this_host} ({platform.machine()})")
    print(f" Source User       : {os.environ.get('USER', 'unknown')} (UID {os.getuid()})")
    print(f" Destination Root  : {mig_root}")
    print(f" Volume UUID       : {vol_uuid} (Free Space: {format_bytes(free_bytes)})")
    print(" VM Exclusion      : Enabled (skips claudevm.bundle, claude-code-vm & CoreSimulator)")
    print(f" Fast Config Only  : {fast_config_only}")
    if fast_config_only:
        print(" ⚠️  WARNING       : --fast-config-only skips raw session journals & .app bundles!")
    print("==================================================================")

    pack_warnings: list[str] = []
    if foreign:
        origin = f"{manifest_host(prev_manifest) or 'another Mac'} at {prev_manifest.get('packed_at', 'unknown time')}"
        if not dry_run and not force:
            print(
                f"❌ Error: {mig_root} holds a snapshot packed on a different Mac ({origin}).\n"
                f"   Packing from this Mac ({this_host}) would replace it. If that is what you want, re-run with --force.",
                file=sys.stderr,
            )
            return 1
        pack_warnings.append(f"replaces a snapshot packed on a different Mac ({origin})")
    if encrypted is False:
        pack_warnings.append(
            f"{vol} is not encrypted: the snapshot holds plaintext secrets (keychain-secrets.json, ~/.ssh, agent tokens)"
        )
    if running_apps:
        pack_warnings.append(
            f"running while packing: {', '.join(running_apps)} — their databases are snapshotted consistently, "
            "other files they are writing may be captured mid-change"
        )
    for w in pack_warnings:
        print(f"⚠️  {w}")

    print(f"\n--> 1. Discovered {len(existing_targets)} internal-disk paths to mirror:")
    for rel in existing_targets:
        p = home / rel
        kind = "symlink" if p.is_symlink() else ("dir" if p.is_dir() else "file")
        print(f"    • ~/{rel} ({kind})")

    print(f"\n--> 2. Discovered {len(sqlite_targets)} live SQLite databases for online backup:")
    for s in sqlite_targets:
        sz = (home / s).stat().st_size if (home / s).exists() else 0
        print(f"    • ~/{s} ({format_bytes(sz)})")

    print(f"\n--> 3. Discovered {len(core_apps)} core AI & utility .app bundles + Python.framework ({has_py_fw}):")
    for app in core_apps:
        print(f"    • /Applications/{app}")
    if has_py_fw:
        print("    • /Library/Frameworks/Python.framework")

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

    # Remove any legacy login.keychain-db inside mig_home/Library/Keychains (B4)
    legacy_kc_dir = mig_home / "Library/Keychains"
    if legacy_kc_dir.exists():
        shutil.rmtree(legacy_kc_dir, ignore_errors=True)

    pack_state_file = mig_meta / "PACK_STATE.json"
    pack_log_file = mig_meta / "pack.log"
    started_at = datetime.now(timezone.utc).isoformat()
    write_json_atomic(
        pack_state_file,
        {
            "state": "in_progress",
            "started_at": started_at,
            "fast_config_only": fast_config_only,
            "source_hostname": socket.gethostname(),
            "source_local_hostname": this_host,
        },
    )
    pack_errors: list[str] = []

    def log_pack(msg: str) -> None:
        with pack_log_file.open("a", encoding="utf-8") as lf:
            lf.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")

    def pack_error(msg: str) -> None:
        pack_errors.append(msg)
        log_pack(msg)
        print(f"⚠️ {msg}", file=sys.stderr)

    try:
        if pack_log_file.stat().st_size > 5 * 1024 * 1024:  # the log is append-only; keep one generation
            os.replace(pack_log_file, pack_log_file.with_name("pack.log.1"))
    except OSError:
        pass
    log_pack(f"=== Starting cmd_pack (fast_config_only={fast_config_only}, foreign={foreign}) ===")

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
    home_rsync_ok = True
    if existing_targets:
        proc = subprocess.run(rsync_cmd, cwd=home, capture_output=True, text=True)
        if proc.stderr:
            log_pack(f"$HOME rsync stderr (rc={proc.returncode}):\n{proc.stderr}")
        if proc.returncode not in (0, 24):
            home_rsync_ok = False
            pack_error(f"$HOME rsync exited with code {proc.returncode}: {proc.stderr[:300].strip()}")
        else:
            print(f"    ✅ $HOME rsync mirror completed in {round(time.time() - t0, 1)}s")

    if home_rsync_ok:
        pruned = prune_stale_targets(mig_home, prev_manifest.get("targets") or [], existing_targets)
        if pruned:
            log_pack(f"Pruned stale snapshot targets: {pruned}")
            print(f"    🧹 Removed {len(pruned)} stale path(s) no longer on this Mac: {', '.join(pruned)}")

    clean_stale_locks(mig_home)

    # Stage login.keychain-db safely into metadata/old-login.keychain-db (never inside home/Library/Keychains)
    src_login_kc = home / "Library/Keychains/login.keychain-db"
    if src_login_kc.is_file():
        try:
            copy_private(src_login_kc, mig_meta / "old-login.keychain-db")
        except OSError as e:
            pack_warnings.append(f"login keychain reference copy failed: {e}")

    # Step 2: Parallel online SQLite backup with strict PRAGMA quick_check == 'ok'
    print(f"\n--> Step 2/5: Snapshotting {len(sqlite_targets)} live SQLite databases (online backup + quick_check)...")
    t_sql = time.time()
    # Checksums recorded for another Mac's databases must not short-circuit this Mac's backups
    prev_sqlite_meta = {} if foreign else (prev_manifest.get("sqlite_backups") or {})

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
            try:
                sqlite_results[rel_db] = fut.result()
            except Exception as e:  # one broken database must not abort the whole pack
                sqlite_results[rel_db] = {
                    "status": "error",
                    "integrity": f"failed:{type(e).__name__}: {e}",
                    "size_bytes": 0,
                    "sha256": "",
                    "seconds": 0,
                }

    sqlite_failed = 0
    for rel_db in valid_sql_targets:
        res = sqlite_results[rel_db]
        print(
            f"    • {rel_db}: {res['status']} (integrity={res.get('integrity', 'n/a')}, "
            f"{format_bytes(res.get('size_bytes', 0))}, {res['seconds']}s)"
        )
        if res.get("status") != "ok" or res.get("integrity") != "ok":
            sqlite_failed += 1
            pack_errors.append(f"SQLite backup failed for {rel_db}: {res}")
            log_pack(pack_errors[-1])
        elif res.get("consistent") is False:
            pack_warnings.append(f"{rel_db} was copied as a file (not through the SQLite backup API)")
    print(
        f"    ⚡ {len(valid_sql_targets) - sqlite_failed}/{len(valid_sql_targets)} SQLite databases snapshotted & "
        f"verified in {round(time.time() - t_sql, 1)}s"
    )

    # Step 3: Incremental Fingerprint-Cached Mirror for Core .app Bundles & Python.framework
    fp_file = mig_meta / "bundle_fingerprints.json"
    cached_fps: dict[str, str] = {} if (force_apps or foreign) else read_json(fp_file)
    new_fps = dict(cached_fps)

    if fast_config_only or not live:
        print("\n--> Step 3/5: Skipped .app bundles & Python.framework.")
    else:
        print("\n--> Step 3/5: Mirroring core AI .app bundles & Python.framework (with fingerprint cache)...")
        t_app = time.time()
        apps_synced = []
        apps_skipped = []
        pending_fps: dict[str, str] = {}
        for app in core_apps:
            src_app = Path("/Applications") / app
            dst_app = mig_apps / app
            fp = compute_bundle_fingerprint(src_app)
            if dst_app.is_dir() and cached_fps.get(f"app:{app}") == fp:
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
                pack_error(f".app bundles rsync failed (rc={app_proc.returncode}): {app_proc.stderr[:300].strip()}")
        print(
            f"    ✅ Core .app bundles ready in {round(time.time() - t_app, 1)}s "
            f"({len(apps_synced)} synced, {len(apps_skipped)} unchanged skipped)"
        )

        if has_py_fw:
            t_py = time.time()
            py_fp = compute_bundle_fingerprint(py_fw)
            dst_py = mig_sys / "Python.framework"
            if dst_py.is_dir() and cached_fps.get("sys:Python.framework") == py_fp:
                print("    ⚡ Python.framework unchanged (fingerprint match, 0.0s)")
                new_fps["sys:Python.framework"] = py_fp
            else:
                py_rsync = ["/usr/bin/rsync", "-aH", "--delete", "Python.framework", str(mig_sys) + "/"]
                py_proc = subprocess.run(py_rsync, cwd=Path("/Library/Frameworks"), capture_output=True, text=True)
                if py_proc.returncode == 0:
                    new_fps["sys:Python.framework"] = py_fp
                    print(f"    ✅ Mirrored Python.framework to {mig_sys} ({round(time.time() - t_py, 1)}s)")
                else:
                    pack_error(f"Python.framework rsync failed (rc={py_proc.returncode}): {py_proc.stderr[:300].strip()}")

        write_json_atomic(fp_file, new_fps)

    # Embed a clean copy of the blueprint repository so a restore never needs network access
    blueprint_error = sync_blueprint_copy(mig_blueprint)
    if blueprint_error:
        pack_error(blueprint_error)

    # Step 4: Export Installed Applications, Brewfile & Keychain Secrets
    print("\n--> Step 4/5: Exporting /Applications inventory, Brewfile & Keychain secrets...")
    apps = sorted(p.name for p in Path("/Applications").glob("*.app")) if live and Path("/Applications").is_dir() else []
    user_apps = (
        sorted(p.name for p in (home / "Applications").glob("*.app"))
        if (home / "Applications").is_dir()
        else []
    )
    write_json_atomic(
        mig_meta / "applications.json",
        {
            "system_applications": apps,
            "user_applications": user_apps,
            "bundled_core_apps": core_apps,
        },
    )
    if live:
        dump_brewfile(mig_meta / "Brewfile")

    user_name = os.environ.get("USER", "developer")
    secrets_map = export_keychain_secrets(user_name, keychain_services) if live else {}
    secrets_file = mig_meta / "keychain-secrets.json"
    missing_core = (
        [svc for svc in CORE_KEYCHAIN_SERVICES if svc not in secrets_map]
        if live and (home / ".orca/backup-config.env").is_file()
        else []
    )
    if missing_core:
        # Backups are configured on this Mac, so these secrets exist. Not being able to read them
        # (locked keychain, denied prompt, non-GUI session) must not produce a "complete" snapshot
        # whose restore would lock the new Mac out of its own backups. An earlier export is kept.
        pack_error(
            f"Keychain secret(s) could not be exported: {', '.join(missing_core)}. "
            "Unlock the login keychain and allow access, then pack again."
        )
    elif secrets_map:
        write_private_file(secrets_file, json.dumps(secrets_map, ensure_ascii=False, indent=2) + "\n")
        print(f"    ✅ Exported {len(secrets_map)} Keychain secrets ({', '.join(secrets_map.keys())}) with mode 0600")
    elif live:
        print("    ℹ️ No Keychain secrets to export (automated backups are not configured on this Mac).")
        if secrets_file.is_file():
            pack_warnings.append("keychain-secrets.json from an earlier pack was kept; this pack exported none")

    # Step 5: Write MANIFEST.json, PACK_STATE.json, restore-on-new-mac.sh, and AGENT_RESTORE.md on the SSD
    packed_at = datetime.now(timezone.utc).isoformat()
    keychain_digests = {k: v.get("sha256", "") for k, v in secrets_map.items()}
    manifest = {
        "schema_version": "1.4",
        "packed_at": packed_at,
        "source_hostname": socket.gethostname(),
        "source_local_hostname": this_host,
        "source_machine_id": this_machine,
        "source_user": user_name if live else home.name,
        "source_uid": os.getuid(),
        "macos_version": platform.mac_ver()[0],
        "arch": platform.machine(),
        "volume_path": str(vol),
        "volume_uuid": vol_uuid,
        "volume_encrypted": encrypted,
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
        "warnings": pack_warnings,
        "errors": pack_errors,
    }
    write_json_atomic(mig_meta / "MANIFEST.json", manifest)

    final_state = "complete" if not pack_errors else "failed"
    write_json_atomic(
        pack_state_file,
        {
            "state": final_state,
            "started_at": started_at,
            "completed_at": packed_at,
            "fast_config_only": fast_config_only,
            "source_local_hostname": this_host,
            "errors": pack_errors,
        },
    )

    artifacts = write_restore_artifacts(vol, manifest, final_state)
    log_pack(f"=== Finished cmd_pack: {final_state} ({len(pack_errors)} error(s), {len(pack_warnings)} warning(s)) ===")

    print("\n--> Step 5/5: Manifest, self-contained blueprint & new-Mac launcher written:")
    print(f"    • Manifest   : {mig_meta / 'MANIFEST.json'}")
    print(f"    • Pack State : {pack_state_file} ({final_state})")
    print(f"    • Launcher   : {artifacts['launcher']}")
    print(f"    • Root Cmd   : {artifacts['root_command']}")
    print(f"    • Root Guide : {artifacts['root_guide']}")
    print(f"    • Agent Doc  : {artifacts['agent_doc']}")

    if pack_warnings:
        print(f"\n⚠️  {len(pack_warnings)} warning(s):")
        for w in pack_warnings:
            print(f"    • {w}")
    if pack_errors:
        print(f"\n❌ Pack finished with {len(pack_errors)} error(s) — the snapshot is marked 'failed':", file=sys.stderr)
        for e in pack_errors:
            print(f"    • {e}", file=sys.stderr)
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
    live = target_home is None
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

    manifest = read_json(manifest_file)
    if not manifest:
        print(f"❌ Error: {manifest_file} is unreadable or not valid JSON. Pack again on the source Mac.", file=sys.stderr)
        return 1

    # B7 (P2-4): Verify PACK_STATE.json is complete before restoring
    if pack_state_file.is_file() and not force:
        pack_state = read_json(pack_state_file).get("state", "unreadable")
        if pack_state != "complete":
            print(
                f"❌ Error: Snapshot PACK_STATE is '{pack_state}' (not 'complete'). "
                f"Refusing to restore an interrupted or failed snapshot.",
                file=sys.stderr,
            )
            return 1

    current_user = os.environ.get("USER", "unknown") if live else home.name
    current_uid = os.getuid()
    current_host = local_host_name()
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
    running_apps = check_running_agent_processes() if live else []
    if running_apps and not dry_run and not force:
        print(
            f"❌ Error: Active AI Agent app(s) currently running: {', '.join(running_apps)}.\n"
            f"   Quit them and run restore again from Terminal. Their live SQLite databases would be replaced\n"
            f"   underneath them (--force skips this check at the risk of corrupting those databases).",
            file=sys.stderr,
        )
        return 1

    # Free space: everything in home/ lands on the internal disk (replaced files are moved, not copied)
    free_home_bytes = shutil.disk_usage(home).free if home.exists() else 0
    need_bytes = snapshot_size_bytes(mig_home)
    headroom = 2 * 1024**3
    if need_bytes is not None and free_home_bytes < need_bytes + headroom and not force:
        print(
            f"❌ Error: Not enough free space in {home}: the snapshot needs {format_bytes(need_bytes)} "
            f"(+{format_bytes(headroom)} headroom), only {format_bytes(free_home_bytes)} is free.\n"
            f"   Free up space and run restore again (or pass --force).",
            file=sys.stderr,
        )
        return 1

    print("==================================================================")
    print(" 🚀 Orca Multi-Agent Blueprint — Restore State onto New Mac")
    print(f" Mode              : {'DRY-RUN (Preview)' if dry_run else 'APPLY (Live Restore)'}")
    print(f" Snapshot Packed At: {manifest.get('packed_at')} (fast_config_only={manifest.get('fast_config_only')})")
    print(f" Source Host / User: {manifest_host(manifest) or 'unknown'} ({src_user}, UID {src_uid}, macOS {src_macos})")
    print(f" Target Host / User: {current_host} ({current_user}, UID {current_uid}, macOS {cur_macos})")
    print(
        f" Target Free Space : {format_bytes(free_home_bytes)} "
        f"(snapshot needs {format_bytes(need_bytes) if need_bytes is not None else 'unknown'})"
    )
    print(f" Snapshot Root     : {mig_root}")
    print("==================================================================")

    if manifest.get("fast_config_only"):
        print(
            "⚠️  WARNING: This snapshot was packed with `--fast-config-only` (raw session journals & .app bundles excluded)."
        )
    if running_apps and dry_run:
        print(f"⚠️  Running now: {', '.join(running_apps)} — quit them before the real restore.")

    if current_user != src_user or current_uid != src_uid:
        print(
            f"ℹ️  Note: Target user/UID ({current_user}/{current_uid}) differs from source ({src_user}/{src_uid}).\n"
            f"    Restore will automatically relocate /Users/{src_user} -> {home} across symlinks, LaunchAgents & configs."
        )

    # B4 (P1-1): Explicitly exclude Library/Keychains from $HOME targets so login.keychain-db is never overwritten.
    # The manifest lives on removable media: only plain relative paths are accepted from it.
    targets = [
        rel
        for rel in manifest.get("targets") or []
        if is_safe_relative(rel)
        and not rel.startswith("Library/Keychains")
        and ((mig_home / rel).exists() or (mig_home / rel).is_symlink())
    ]
    sqlite_rels = [rel for rel in (manifest.get("sqlite_backups") or {}) if is_safe_relative(rel)]
    bundled_apps = [
        app
        for app in manifest.get("bundled_core_apps") or []
        if is_safe_relative(app) and "/" not in app and (mig_apps / app).is_dir()
    ]
    print(f"\n--> Will restore {len(targets)} paths from {mig_home} into {home} (Keychains safely excluded):")
    for rel in targets:
        print(f"    • ~/{rel}")
    if bundled_apps:
        print(f"\n--> Will install any missing core .app bundles ({len(bundled_apps)} bundled) into /Applications:")
        for app in bundled_apps:
            status = "already installed" if (Path("/Applications") / app).is_dir() else "will install"
            print(f"    • /Applications/{app} ({status})")

    snapshot_la = mig_home / "Library/LaunchAgents"
    enabled_set = set(enable_agents or [])
    if dry_run:
        staged = [label for label, _ in third_party_agents(snapshot_la) if label not in enabled_set]
        if staged:
            print(f"\n--> Third-party LaunchAgents that will be restored but held (not started): {', '.join(staged)}")
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
    if targets:
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
        try:
            copy_private(old_kc_src, staged_kc)
            print(f"    🔐 Staged old Mac login keychain (read-only reference) at {staged_kc}")
        except OSError as e:
            print(f"    ⚠️ Could not stage the old login keychain: {e}")

    # Step 2: Restore missing core .app bundles into /Applications (only when restoring to real HOME)
    if live:
        print("\n--> Step 2/5: Checking & restoring core AI .app bundles in /Applications...")
        installed_apps = []
        failed_apps = []
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
                else:
                    failed_apps.append(app)
        print(
            f"    ✅ Restored {len(installed_apps)} missing .app bundles into /Applications "
            f"({len(bundled_apps) - len(installed_apps) - len(failed_apps)} already present)."
        )
        if failed_apps:
            print(f"    ⚠️ Could not install: {', '.join(failed_apps)} — copy them from {mig_apps} by hand.")
    else:
        print("\n--> Step 2/5: Skipped /Applications install (custom --target-home test mode).")

    # Step 3: Clean stale locks, relocate cross-user paths if needed, fix ~/.ssh permissions, wire symlinks & verify DBs
    print("\n--> Step 3/5: Cleaning stale host locks, relocating paths, verifying SQLite DBs & wiring symlinks...")
    removed_locks = clean_stale_locks(home)
    failed_dbs = []
    for rel_db in sqlite_rels:
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

    # The restored backup config still carries the source Mac's host tag: give this Mac its own
    # before any backup job can run, so the two Macs never publish (and prune) under one name
    keep_tag = _env_file_value(home / ".orca/backup-config.env", "BACKUP_HOST_TAG_KEEP").lower() in ("1", "true", "yes")
    new_host_tag = rewrite_inherited_host_tag(home, manifest, current_host, machine_id(), keep=keep_tag)

    # Record migration receipt marker for fail-closed backup secret checks (B2)
    orca_dir = home / ".orca"
    orca_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        orca_dir / ".migrated-from",
        {
            "source_hostname": manifest.get("source_hostname"),
            "source_local_hostname": manifest_host(manifest),
            "source_machine_id": manifest.get("source_machine_id", ""),
            "packed_at": manifest.get("packed_at"),
            "restored_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    # Flush cfprefsd cache so restored Library/Preferences/*.plist take effect immediately (P2-10)
    if live:
        subprocess.run(["/usr/bin/killall", "cfprefsd"], capture_output=True)

    print(
        f"    ✅ Cleaned {len(removed_locks)} stale lock/WAL files, relocated {reloc_stats['relocated_symlinks']} symlinks "
        f"& {reloc_stats['rewritten_configs']} configs, verified ~/.ssh (700/600), and wired {len(created_links)} runtime symlinks."
    )
    if new_host_tag:
        print(f"    🏷  Backup host tag set to '{new_host_tag}' (the snapshot carried the source Mac's tag).")
    if failed_dbs:
        print(f"    ❌ SQLite integrity check failed after restore: {failed_dbs}", file=sys.stderr)
        return 1

    # Step 4: Restore Keychain secrets FIRST, then register LaunchAgents (B5)
    print("\n--> Step 4/5: Restoring Keychain secrets & registering LaunchAgents...")
    follow_up: list[str] = []
    secrets_file = mig_meta / "keychain-secrets.json"
    if secrets_file.is_file() and live:
        secrets_data = read_json(secrets_file)
        imported_services = import_keychain_secrets(secrets_data, current_user)
        print(f"    ✅ Imported Keychain services: {', '.join(imported_services) if imported_services else 'none'}")
        not_imported = [svc for svc in secrets_data if svc not in imported_services]
        if not_imported:
            follow_up.append(f"Keychain import failed for: {', '.join(not_imported)} (is the login keychain unlocked?)")
    else:
        print("    ℹ️ Keychain import skipped (no keychain-secrets.json or custom target-home).")

    plan = plan_restored_agents(home / "Library/LaunchAgents", snapshot_la, enabled_set)
    unknown_enable = enabled_set - {x for label, p in plan["enable"] for x in (label, p.name, p.stem)}
    if unknown_enable:
        follow_up.append(f"--enable-agents named LaunchAgents that are not in the snapshot: {', '.join(sorted(unknown_enable))}")

    held: list[str] = []
    if live:
        missing_secrets = [svc for svc in CORE_KEYCHAIN_SERVICES if not keychain_has(svc)]
        if plan["blueprint"] and missing_secrets:
            print(
                f"    ⚠️ Backup LaunchAgents NOT loaded: Keychain secret(s) missing ({', '.join(missing_secrets)})."
            )
            follow_up.append(
                f"Backup jobs are not running: Keychain secret(s) {', '.join(missing_secrets)} are missing. "
                "Restore them, then run `deploy.sh --setup-backups`."
            )
        else:
            load_errors = []
            for label, plist_path in plan["blueprint"]:
                err = load_launch_agent(current_uid, label, plist_path, changed=False)
                if err:
                    load_errors.append(f"{label}: {err}")
            print(
                f"    ✅ Loaded {len(plan['blueprint']) - len(load_errors)}/{len(plan['blueprint'])} "
                f"blueprint backup LaunchAgents (gui/{current_uid})."
            )
            if load_errors:
                follow_up.append(f"LaunchAgents failed to load: {'; '.join(load_errors)}")

        for label, plist_path in plan["enable"]:
            launchd_set_enabled(current_uid, label, True)
            err = load_launch_agent(current_uid, label, plist_path, changed=False)
            if err:
                follow_up.append(f"LaunchAgent {label} could not be started: {err}")
            else:
                print(f"    ✅ Enabled third-party LaunchAgent {label} (--enable-agents).")

        for label, _ in plan["hold"]:
            if launchd_loaded(current_uid, label):
                continue  # already running on this Mac (enabled earlier): not ours to stop
            if launchd_set_enabled(current_uid, label, False):
                held.append(label)
            else:
                follow_up.append(f"Could not hold LaunchAgent {label}; it will start at the next login.")
    else:
        held = [label for label, _ in plan["hold"]]
        print(
            f"    ℹ️ launchd untouched (custom --target-home): {len(plan['blueprint'])} blueprint, "
            f"{len(plan['enable'])} enabled, {len(held)} held."
        )

    if held:
        print(
            f"    ⏸  Held {len(held)} third-party LaunchAgent(s) so they do not run on two Macs at once "
            f"(they stay off across logins): {', '.join(held)}"
        )
        follow_up.append(
            f"Held LaunchAgents: {', '.join(held)}. When the source Mac no longer runs them: "
            f"python3 \"{Path(__file__).resolve()}\" agents --enable <label>"
        )

    # NAS credentials are per-Mac (internet passwords are not migrated): say so now, not after a week of skipped backups
    smb_match = _env_file_value(home / ".orca/backup-config.env", "BACKUP_NAS_SMB_MATCH")
    m_smb = re.match(r"//(?:([^@/\s]+)@)?([^/\s]+)/(\S+?)(?:\s+on\s|$)", smb_match)
    if m_smb and live:
        try:
            has_cred = (
                subprocess.run(
                    ["/usr/bin/security", "find-internet-password", "-s", m_smb.group(2)], capture_output=True, timeout=5
                ).returncode
                == 0
            )
        except Exception:
            has_cred = False
        if not has_cred:
            user_part = f"{m_smb.group(1)}@" if m_smb.group(1) else ""
            follow_up.append(
                f"NAS backups need the share password saved on this Mac: Finder → Go → Connect to Server → "
                f"smb://{user_part}{m_smb.group(2)}/{m_smb.group(3)} and tick “Remember this password in my keychain”. "
                "Until then the Restic jobs report `skipped`."
            )

    if follow_up:
        print("\n--> Needs your attention after this restore:")
        for item in follow_up:
            print(f"    • {item}")

    print("\n--> Step 5/5: State restoration finished! Handing off to `deploy.sh` for toolchain & profile alignment...")
    return 0


def cmd_agents(enable: list[str], hold: list[str]) -> int:
    """List the third-party LaunchAgents in ~/Library/LaunchAgents; optionally enable or hold some."""
    uid = os.getuid()
    agents = third_party_agents(Path.home() / "Library/LaunchAgents")
    by_name: dict[str, tuple[str, Path]] = {}
    for label, plist_path in agents:
        for key in (label, plist_path.name, plist_path.stem):
            by_name[key] = (label, plist_path)

    rc = 0
    actions: list[dict] = []
    for name in enable:
        if name not in by_name:
            actions.append({"label": name, "action": "enable", "error": "no such third-party LaunchAgent"})
            rc = 1
            continue
        label, plist_path = by_name[name]
        launchd_set_enabled(uid, label, True)
        err = load_launch_agent(uid, label, plist_path, changed=False)
        actions.append({"label": label, "action": "enable", **({"error": err} if err else {})})
        rc = rc or (1 if err else 0)
    for name in hold:
        if name not in by_name:
            actions.append({"label": name, "action": "hold", "error": "no such third-party LaunchAgent"})
            rc = 1
            continue
        label, _ = by_name[name]
        ok = launchd_set_enabled(uid, label, False)
        if launchd_loaded(uid, label):
            subprocess.run(["/bin/launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True, timeout=20)
        actions.append({"label": label, "action": "hold", **({} if ok else {"error": "launchctl disable failed"})})
        rc = rc or (0 if ok else 1)

    disabled = launchd_disabled_labels(uid)
    listing = []
    for label, plist_path in agents:
        loaded = launchd_loaded(uid, label)
        is_disabled = label in disabled
        if loaded:
            state = "running"
        elif is_disabled:
            state = "held"
        else:
            state = "starts-at-next-login"
        listing.append(
            {"label": label, "state": state, "loaded": loaded, "disabled": is_disabled, "plist": str(plist_path)}
        )
    out: dict = {"agents": listing}
    if actions:
        out["actions"] = actions
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return rc


def cmd_guide(vol: Path, dry_run: bool) -> int:
    """Re-render the launcher, AGENT_RESTORE.md and the root guide from the existing snapshot."""
    problem = check_volume_identity(vol, _load_config_fallbacks())
    if problem:
        print(f"❌ Error: {problem}", file=sys.stderr)
        return 1
    mig_root = vol / "AI/Migration/latest"
    manifest = read_json(mig_root / "metadata/MANIFEST.json")
    if not manifest:
        print(f"❌ Error: No readable snapshot manifest under {mig_root}. Run `deploy.sh --pack-to-ssd` first.", file=sys.stderr)
        return 1
    pack_state = str(read_json(mig_root / "metadata/PACK_STATE.json").get("state", "unknown"))
    if dry_run:
        facts = snapshot_facts(vol, manifest)
        print(render_guide(vol, manifest, pack_state, facts))
        print("\n[DRY-RUN] Nothing written. Run with --apply to refresh the guides and the embedded blueprint copy.")
        return 0
    blueprint_error = sync_blueprint_copy(mig_root / "blueprint")
    if blueprint_error:
        print(f"⚠️ {blueprint_error}", file=sys.stderr)
    artifacts = write_restore_artifacts(vol, manifest, pack_state)
    for name, path in artifacts.items():
        print(f"    • {name:12s}: {path}")
    print("✅ Restore guides refreshed from the existing snapshot (snapshot data untouched).")
    return 1 if blueprint_error else 0


def cmd_status(vol: Path) -> int:
    mig_root = vol / "AI/Migration/latest"
    manifest_file = mig_root / "metadata/MANIFEST.json"
    if not manifest_file.is_file():
        print(json.dumps({"status": "not_packed", "migration_root": str(mig_root)}, indent=2))
        return 0
    manifest = read_json(manifest_file)
    pack_state = read_json(mig_root / "metadata/PACK_STATE.json") or {"state": "unknown"}
    age_hours = None
    try:
        packed = datetime.fromisoformat(str(manifest.get("packed_at")))
        age_hours = round((datetime.now(timezone.utc) - packed).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        pass
    if not manifest:
        status = "unreadable"
    else:
        status = "ready" if pack_state.get("state") == "complete" else pack_state.get("state", "unknown")
    print(
        json.dumps(
            {
                "status": status,
                "migration_root": str(mig_root),
                "source_host": manifest_host(manifest),
                "packed_on_this_mac": not snapshot_is_foreign(manifest, local_host_name(), machine_id()),
                "age_hours": age_hours,
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
    parser.add_argument("action", choices=["pack", "restore", "status", "agents", "guide"], help="Migration action")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    parser.add_argument("--apply", action="store_true", help="Execute pack, restore or guide")
    parser.add_argument("--fast-config-only", action="store_true", help="Skip multi-GB raw chat logs & .app bundles during pack")
    parser.add_argument("--force-apps", action="store_true", help="Force full resync of .app bundles & Python.framework ignoring fingerprint cache")
    parser.add_argument(
        "--force",
        action="store_true",
        help="pack: replace a snapshot that was packed on a different Mac. "
        "restore: bypass preflight blocks (running apps, macOS version, pack state, free space)",
    )
    parser.add_argument("--enable-agents", type=str, default="", help="restore: comma-separated third-party LaunchAgent labels to start instead of holding")
    parser.add_argument("--enable", type=str, default="", help="agents: comma-separated labels to enable and load")
    parser.add_argument("--hold", type=str, default="", help="agents: comma-separated labels to disable and unload")
    parser.add_argument("--target-home", type=str, default=None, help="Override target $HOME directory for isolated restore testing")
    parser.add_argument("--volume", type=str, default=None, help="Override external SSD volume path")
    args = parser.parse_args()

    def csv(value: str) -> list[str]:
        return [x.strip() for x in value.split(",") if x.strip()]

    if args.action == "agents":
        return cmd_agents(csv(args.enable), csv(args.hold))

    vol = Path(args.volume) if args.volume else detect_volume(require_external_if_configured=(args.action in ("pack", "restore", "guide")))
    dry_run = not args.apply
    target_home = Path(args.target_home) if args.target_home else None

    if args.action == "status":
        return cmd_status(vol)
    if args.action == "guide":
        return cmd_guide(vol, dry_run=dry_run)
    if args.action == "pack":
        return cmd_pack(
            vol, dry_run=dry_run, fast_config_only=args.fast_config_only, force_apps=args.force_apps, force=args.force
        )
    return cmd_restore(
        vol, dry_run=dry_run, force=args.force, enable_agents=csv(args.enable_agents), target_home=target_home
    )


if __name__ == "__main__":
    sys.exit(main())
