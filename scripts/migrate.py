#!/usr/bin/env python3
"""
Orca Multi-Agent Blueprint — Universal Lossless Cross-Mac Migration Engine (migrate.py)

Packs all internal-disk AI Agent states, SQLite session/orchestration databases,
multi-account profiles, CLI toolchains, Python runtimes, core .app bundles,
Xcode signing profiles, SSH/Git/Zsh configs, LaunchAgents and local repositories (~/Developer)
onto an external SSD (${AI_VOLUME_PATH}/AI/Migration/latest), and restores them losslessly onto a
new Mac without copying bulky VMs or caches (skips Claude Desktop claudevm.bundle / claude-code-vm,
CoreSimulator devices and app caches).

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
- Restore preflight: running Agent apps and CLIs, pack state, macOS version and free disk space;
  replaced files (and the WAL of replaced databases) are kept under `~/.migration-preexisting/<timestamp>`.
  Existing Keychain items with a different value are never overwritten.
- LaunchAgents: blueprint backup jobs are loaded only when their Keychain secrets are present.
  Third-party jobs from the snapshot are held with `launchctl disable` right after the files are
  copied, so they do not start at the next login either, even when a later restore step fails
  (`migrate.py agents --enable <label>` opts in).
- Pack never writes through a symlink into live data: databases and lock files that sit behind a
  symlinked directory are reported and left alone.
- Cross-username path relocation using `plistlib` (preserves binary plists) and boundary-aware regex.

Trust model: restore copies shell startup files, LaunchAgents and tools from the snapshot into
$HOME, so the snapshot drive must be trusted like the old Mac itself. Restore still refuses paths
that escape $HOME (absolute, `..`, symlinked parents) and never lets a file name act as an option.
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
import shlex
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
ROOT_COMMAND_NAME = "Restore on New Mac.command"
ROOT_GUIDE_NAME = "RESTORE-GUIDE.md"
GUIDE_EXTRA_NAME = "guide-extra.md"
# Root files written by earlier versions; left in place (they are the user's), only reported
LEGACY_ROOT_FILES = ("新Mac一键恢复.command", "新电脑恢复指南.md")
# LaunchAgent-discovered Application Support folders larger than this are not packed automatically
DISCOVERED_DIR_MAX_BYTES = 5 * 1024**3


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


# Relative paths under $HOME to migrate. Anything else goes through EXTRA_MIGRATION_PATHS.
# Note (B4): Library/Keychains/login.keychain-db is intentionally NOT in HOME_RELATIVE_TARGETS
# so restore never overwrites a live login.keychain-db in place; it is staged separately into metadata/.
HOME_RELATIVE_TARGETS = [
    # 1. AI agent state, sessions and multi-account pools
    ".claude",
    ".claude.json",
    ".claude.json.backup",
    ".codex",
    ".gemini",
    ".agents",
    ".orca",
    ".agent-browser",
    "Library/Application Support/orca",
    "Library/Application Support/Claude",
    "Library/Application Support/Codex",
    "Library/Application Support/Codex Storage",
    "Library/Application Support/com.openai.chat",
    "Library/Application Support/com.openai.codex",
    "Library/Application Support/OpenAI",
    "Library/Application Support/Antigravity",
    # 2. CLI toolchain, shell history, Git, SSH and LaunchAgents
    ".local",
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
    "Library/LaunchAgents",
    # 3. Xcode signing profiles and agent app preferences
    "Library/Developer/Xcode/UserData",
    "Library/Application Support/com.apple.dt.Xcode",
    "Library/Preferences/com.stablyai.orca.plist",
    "Library/Preferences/com.openai.codex.plist",
    "Library/Preferences/com.anthropic.claudefordesktop.plist",
    # 4. Local repositories (Apple's default ~/Developer folder; unpushed work lives here)
    "Developer",
]

# AI desktop .app bundles in /Applications; more through EXTRA_BUNDLED_APPS
CORE_APP_BUNDLES = [
    "Orca.app",
    "Claude.app",
    "ChatGPT.app",
    "Antigravity.app",
    "Cursor.app",
]

# Desktop apps (bundle names) and CLI agents (executable names) that hold the migrated state open
AGENT_APPS = ("Orca", "Claude", "ChatGPT", "Codex", "Antigravity", "Cursor")
AGENT_CLIS = ("claude", "codex", "gemini", "cursor-agent")

# rsync filters. Plain names match anywhere: they are never user data. App caches and VM images are
# anchored to ~/Library/Application Support, so a repository folder named `Cache` is still packed.
_APP_SUPPORT_JUNK = (
    "vm_bundles",
    "claudevm.bundle",
    "claude-code-vm",
    "Cache",
    "Code Cache",
    "GPUCache",
    "GPUPersistentCache",
    "GraphiteDawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "Crashpad",
    "acp-temp",
)
RSYNC_EXCLUDES = [
    *(f"/Library/Application Support/**/{name}/" for name in _APP_SUPPORT_JUNK),
    "/Library/Developer/CoreSimulator/",
    "/Library/Keychains/",
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "orcad.lock",
    "/Library/Application Support/orca/claude-profiles/*/home.lock",
    "/Library/Application Support/orca/daemon/*.pid",
    "/Library/Application Support/orca/daemon/*.token",
    "/Library/Application Support/orca/**/profile-state.db.backup.*.db",
    "*.sock",
    "*.ipc",
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
    "artifact-sessions/runtime-generations",
    "DerivedData",
    ".DS_Store",
]
# --fast-config-only also leaves out the raw session logs
FAST_CONFIG_EXCLUDES = [
    "/.codex/sessions/",
    "/Library/Application Support/orca/codex-accounts/*/home/sessions/",
    "/Library/Application Support/orca/antigravity-chat/antigravity-acp/",
    "agent-session-journal.db",
    "thread_history_1.sqlite",
]

# SQLite databases backed up via the SQLite online backup API
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
    "Library/Application Support/Claude/declarative_performance_observer.db",
]

# Directories never searched for live SQLite databases (huge, and never hold one that is in use)
_WALK_PRUNE = {".git", "node_modules", "DerivedData"}
_WAL_SUFFIXES = (".db-wal", ".sqlite-wal", ".sqlite3-wal")


def real_child(root: Path, rel: str) -> bool:
    """True when no directory on the way from `root` to `root/rel` is a symlink.

    The last component may itself be a symlink (removing or replacing it touches only the link).
    Used before every write into the snapshot or the target home, so neither pack nor restore can
    write through a symlinked directory into data that lives somewhere else."""
    parent = Path(rel).parent
    if str(parent) in ("", "."):
        return True
    base = os.path.realpath(root)
    return os.path.realpath(os.path.join(base, str(parent))) == os.path.join(base, str(parent))


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


def _csv_config(cfg: dict[str, str], *keys: str) -> list[str]:
    items: list[str] = []
    for key in keys:
        for item in (os.environ.get(key, "") or cfg.get(key, "")).split(","):
            cleaned = item.strip()
            if cleaned and cleaned not in items:
                items.append(cleaned)
    return items


def extra_migration_paths(cfg: dict[str, str]) -> list[str]:
    """EXTRA_MIGRATION_PATHS / EXTRA_RESTIC_PATHS entries that are safe relative paths under $HOME."""
    return [
        rel
        for rel in _csv_config(cfg, "EXTRA_MIGRATION_PATHS", "EXTRA_RESTIC_PATHS")
        if is_safe_relative(rel) and not rel.startswith("Library/Keychains")
    ]


def get_home_targets(home: Path | None = None, la_deps: tuple | None = None) -> list[str]:
    h = home or Path.home()
    cfg = _load_config_fallbacks(h)
    targets = list(HOME_RELATIVE_TARGETS)
    la_rels = (la_deps or discover_launchagent_dependencies(h))[0]
    for rel in [*la_rels, *extra_migration_paths(cfg)]:
        if rel not in targets:
            targets.append(rel)
    return targets


def discover_all_sqlite_targets(home: Path, home_targets: list[str] | None = None) -> list[str]:
    targets: list[str] = []

    def add(db: Path) -> None:
        if is_valid_sqlite_file(db):
            rel_p = str(db.relative_to(home))
            if rel_p not in targets:
                targets.append(rel_p)

    for rel in SQLITE_RELATIVE_TARGETS:
        add(home / rel)
    codex_accts = home / "Library/Application Support/orca/codex-accounts"
    if codex_accts.is_dir():
        for db in sorted(codex_accts.glob("*/home/*.sqlite")):
            add(db)
    ag_convs = home / "Library/Application Support/orca/antigravity-chat/antigravity-acp/conversations"
    if ag_convs.is_dir():
        for db in sorted(ag_convs.glob("*.db")):
            add(db)

    # One walk per migrated directory (symlinks not followed, .git/node_modules pruned):
    # - every database with an active -wal file, so no WAL-mode database loses uncheckpointed frames
    #   to the *-wal rsync exclusions;
    # - in LaunchAgent-discovered and EXTRA_MIGRATION_PATHS folders, also databases up to two levels
    #   deep without a WAL, so they are copied consistently instead of as raw files.
    base_set = set(HOME_RELATIVE_TARGETS)
    for target_rel in home_targets if home_targets is not None else get_home_targets(home):
        target_p = home / target_rel
        if not target_p.is_dir() or target_p.is_symlink():
            continue
        scan_plain = target_rel not in base_set
        for dirpath, dirnames, filenames in os.walk(target_p):
            dirnames[:] = sorted(d for d in dirnames if d not in _WALK_PRUNE)
            depth = len(Path(dirpath).relative_to(target_p).parts)
            for name in sorted(filenames):
                if name.endswith(_WAL_SUFFIXES):
                    if "profile-state.db.backup." not in name:
                        add(Path(dirpath) / name[:-4])
                elif scan_plain and depth <= 1 and name.endswith((".sqlite", ".sqlite3", ".db")):
                    add(Path(dirpath) / name)
    return targets


def discover_core_apps(home: Path | None = None) -> list[str]:
    """Bundled apps: CORE_APP_BUNDLES plus EXTRA_BUNDLED_APPS, when installed.

    Apps that third-party LaunchAgents point at are not bundled automatically (they can be several
    GB and can be reinstalled); pack lists them so the user can add them to EXTRA_BUNDLED_APPS."""
    h = home or Path.home()
    cfg = _load_config_fallbacks(h)
    candidates = list(CORE_APP_BUNDLES)
    for app in _csv_config(cfg, "EXTRA_BUNDLED_APPS"):
        if app not in candidates:
            candidates.append(app)
    return [name for name in candidates if "/" not in name and name != ".." and (Path("/Applications") / name).is_dir()]


def discover_keychain_services(home: Path | None = None, la_deps: tuple | None = None) -> list[str]:
    """B1: Always include core backup Keychain services (restic-mac-agents, icloud-package-key)
    plus services configured in ~/.orca/backup-config.env, config.env, or EXTRA_KEYCHAIN_SERVICES."""
    h = home or Path.home()
    cfg = _load_config_fallbacks(h)
    services: list[str] = list(CORE_KEYCHAIN_SERVICES)
    for cfg_key in ("BACKUP_KEYCHAIN_RESTIC", "BACKUP_KEYCHAIN_ICLOUD"):
        val = cfg.get(cfg_key, "").strip()
        if val and val not in services:
            services.append(val)
    la_kc = (la_deps or discover_launchagent_dependencies(h))[2]
    for svc in [*la_kc, *_csv_config(cfg, "EXTRA_KEYCHAIN_SERVICES")]:
        if svc and svc not in services:
            services.append(svc)
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


def _source_signature(path: Path) -> dict:
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino}


def sqlite_online_backup(
    src_db: Path, dst_db: Path, prev_meta: dict | None = None, cache_root: Path | None = None
) -> dict:
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
    src_journal = Path(str(src_db) + "-journal")

    # Fast reuse for an unchanged database: same file (inode), size and nanosecond mtime as when the
    # previous copy was taken, and no WAL or hot journal that could hold newer pages
    if (
        prev_meta
        and prev_meta.get("status") == "ok"
        and prev_meta.get("integrity") == "ok"
        and prev_meta.get("sha256")
        and isinstance(prev_meta.get("source"), dict)
        and not src_wal.exists()
        and not src_journal.exists()
        and dst_db.is_file()
        and not Path(str(dst_db) + "-wal").exists()
    ):
        try:
            dst_size = dst_db.stat().st_size
            if _source_signature(src_db) == prev_meta["source"] and dst_size == prev_meta.get("size_bytes"):
                return {
                    "status": "ok",
                    "integrity": "ok",
                    "size_bytes": dst_size,
                    "sha256": prev_meta["sha256"],
                    "method": "reused",
                    "consistent": True,
                    "source": prev_meta["source"],
                    "seconds": round(time.time() - t0, 3),
                }
        except OSError:
            pass

    cache_root = cache_root or Path.home() / "Library/Caches/orca-migrate-sqlite"
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
        # Taken before the copy: a change during the copy makes the next pack copy again
        source = _source_signature(src_db)

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
            "source": source,
            "seconds": round(time.time() - t0, 2),
        }
    except (SnapshotError, OSError, sqlite3.Error) as e:
        return failed(str(e) or type(e).__name__)
    finally:
        discard(tmp_db)
        discard(staged)


def _security(args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["/usr/bin/security", *args], capture_output=True, text=True, timeout=5)
    except Exception:
        return None


def _keychain_account(service: str) -> str:
    """Account attribute of the first generic password for `service` (reads attributes only)."""
    r = _security(["find-generic-password", "-s", service])
    if r is None or r.returncode != 0:
        return ""
    m = re.search(r'"acct"<blob>="((?:[^"\\]|\\.)*)"', r.stdout)
    return m.group(1) if m else ""


def keychain_lookup(service: str, account: str = "") -> tuple[str, str] | None:
    """(account, password) of the generic password for `service`, preferring `account`; None if absent.

    The secret comes back on stdout only; it never appears on a command line."""
    for scoped in (True, False):
        if scoped and not account:
            continue
        r = _security(["find-generic-password", "-s", service, *(["-a", account] if scoped else []), "-w"])
        if r is None or r.returncode != 0:
            continue
        pw = r.stdout[:-1] if r.stdout.endswith("\n") else r.stdout
        if pw:
            return (account if scoped else (_keychain_account(service) or account)), pw
    return None


def export_keychain_secrets(account: str, services: list[str] | None = None) -> dict[str, dict[str, str]]:
    """B1 / P2-9: Export generic passwords together with the account they are stored under."""
    secrets_map: dict[str, dict[str, str]] = {}
    for svc in services if services is not None else discover_keychain_services():
        found = keychain_lookup(svc, account)
        if found:
            secrets_map[svc] = {"account": found[0], "password": found[1]}
    return secrets_map


def import_keychain_secrets(secrets_data: dict, default_account: str) -> dict[str, list[str]]:
    """Import secrets from either structured dict {svc: {account, password}} or legacy {svc: password}.

    An item that already exists on this Mac is never overwritten: the same value counts as present,
    a different value is reported as a conflict and left alone (it may protect this Mac's own
    backups). The password goes to `security` on stdin (keychain_add), never on a command line."""
    result: dict[str, list[str]] = {"imported": [], "present": [], "conflicts": [], "failed": []}
    for svc, val in secrets_data.items():
        if isinstance(val, dict):
            pw = val.get("password", "")
            acct = default_account if svc in CORE_KEYCHAIN_SERVICES else (val.get("account") or default_account)
        elif isinstance(val, str):  # snapshots packed before schema 1.4
            pw = val
            acct = default_account
        else:
            continue
        if not pw or not isinstance(pw, str):
            continue
        existing = keychain_lookup(svc, acct)
        if existing is not None:
            result["present" if existing[1] == pw else "conflicts"].append(svc)
        elif keychain_add(svc, acct, pw):
            result["imported"].append(svc)
        else:
            result["failed"].append(svc)
    return result


def keychain_has(service: str) -> bool:
    """True when a generic password for `service` exists (does not read the secret)."""
    r = _security(["find-generic-password", "-s", service])
    return r is not None and r.returncode == 0


def clean_stale_locks(home: Path) -> list[str]:
    """Remove host-bound lock, socket and pid files of Orca under `home`, never through a symlink."""
    removed: list[str] = []
    orca_rel = "Library/Application Support/orca"
    orca_support = home / orca_rel
    if not orca_support.is_dir() or orca_support.is_symlink() or not real_child(home, f"{orca_rel}/x"):
        return removed
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
    for pat in lock_patterns:
        for p in orca_support.glob(pat):
            rel = str(p.relative_to(home))
            if not real_child(home, rel):
                continue
            try:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                removed.append(rel)
            except OSError:
                pass
    return removed


def _version_key(name: str) -> tuple[int, ...]:
    """Numeric version in a name like `node-v22.11.0` or `cpython-3.12.1-macos`: (22, 11, 0)."""
    m = re.search(r"(\d+(?:\.\d+)*)", name)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _relink(link: Path, target: Path) -> bool:
    """Point a missing or dangling `link` at an existing `target`; never replaces a working file."""
    if link.is_symlink() and not link.exists():
        try:
            link.unlink()
        except OSError:
            return False
    if not target.exists() or link.exists() or link.is_symlink():
        return False
    try:
        link.symlink_to(target)
    except OSError:
        return False
    return True


def ensure_local_bin_runtime_symlinks(home: Path) -> list[str]:
    """Wire runtimes the restored jobs and hooks expect in ~/.local/bin on a brand-new Mac:
    node/npm/npx/corepack from the newest ~/opt/node-* (when that folder was migrated through
    EXTRA_MIGRATION_PATHS), the backup jobs' restic, and a working python3."""
    created = []
    local_bin = home / ".local/bin"
    local_bin.mkdir(parents=True, exist_ok=True)

    node_bins = sorted((home / "opt").glob("node-*/bin"), key=lambda p: _version_key(p.parent.name), reverse=True)
    if node_bins:
        for tool in ("node", "npm", "npx", "corepack"):
            if _relink(local_bin / tool, node_bins[0] / tool):
                created.append(f"~/.local/bin/{tool} -> {node_bins[0] / tool}")

    codex_restic = home / "Library/Application Support/Codex Storage/bin/restic"
    if _relink(local_bin / "restic", codex_restic):
        created.append(f"~/.local/bin/restic -> {codex_restic}")

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

    candidates = (home / ".local/share/uv/python").glob("cpython-3.*/bin/python3")
    for candidate in sorted(candidates, key=lambda p: _version_key(p.parent.parent.name), reverse=True):
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


def _replace_bytes(path: Path, data: bytes) -> None:
    """Atomically replace an existing file's content, keeping its permissions."""
    mode = path.stat().st_mode & 0o7777
    tmp = path.with_name(f".{path.name}.relocate-{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def relocate_home_references(home: Path, old_user: str, new_user: str) -> dict[str, int]:
    """B11 (P2-8): If the new Mac's username differs from the old Mac's username, automatically
    rewrite /Users/<old_user> -> <home> using boundary-aware regex and plistlib (preserving binary plists).

    Covers symlinks in the agent/tool folders, LaunchAgents, shell/Git/SSH/agent config files and the
    Claude Code per-project folders (~/.claude/projects/-Users-<old>-...). Databases and other app
    state that embed the old path are not rewritten."""
    stats = {"relocated_symlinks": 0, "rewritten_configs": 0, "renamed_project_dirs": 0}
    if not old_user or not new_user or old_user == new_user:
        return stats

    old_prefix = f"/Users/{old_user}"
    new_prefix = str(home)
    # `/Users/alice` but not `/Users/alice2` or `/Users/alice.old`
    boundary_pat = re.compile(rf"{re.escape(old_prefix)}(?![A-Za-z0-9._-])")

    def writable(path: Path) -> bool:
        try:
            rel = str(path.relative_to(home))
        except ValueError:
            return False
        return path.is_file() and not path.is_symlink() and real_child(home, rel)

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
        if not root.is_dir() or root.is_symlink():
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
            if not writable(pl_path):
                continue
            raw_bytes = pl_path.read_bytes()
            is_binary = raw_bytes.startswith(b"bplist00")
            pl_data = plistlib.loads(raw_bytes)
            new_data, changed = _rewrite_plist_obj(pl_data, boundary_pat, new_prefix)
            if changed:
                fmt = plistlib.FMT_BINARY if is_binary else plistlib.FMT_XML
                _replace_bytes(pl_path, plistlib.dumps(new_data, fmt=fmt, sort_keys=False))
                stats["rewritten_configs"] += 1
        except Exception:
            pass

    text_candidates: list[Path] = [
        home / rel_cfg
        for rel_cfg in (
            ".zshenv",
            ".zshrc",
            ".zprofile",
            ".gitconfig",
            ".config/git/config",
            ".ssh/config",
            ".orca/backup-config.env",
            ".claude.json",
            ".claude/settings.json",
            ".codex/config.toml",
            ".codex/hooks.json",
        )
    ]
    orca_dir = home / "Library/Application Support/orca"
    if orca_dir.is_dir():
        text_candidates.extend(orca_dir.glob("claude-profiles/*/home/settings.json"))
        text_candidates.extend(orca_dir.glob("claude-profiles/*/home/.claude.json"))
        text_candidates.extend(orca_dir.glob("codex-accounts/*/home/config.toml"))
        text_candidates.extend(orca_dir.glob("codex-accounts/*/home/hooks.json"))

    for cfg in text_candidates:
        try:
            if not writable(cfg):
                continue
            raw = cfg.read_bytes().decode("utf-8", errors="surrogateescape")
            new_raw = boundary_pat.sub(new_prefix, raw)
            if new_raw != raw:
                _replace_bytes(cfg, new_raw.encode("utf-8", errors="surrogateescape"))
                stats["rewritten_configs"] += 1
        except OSError:
            pass

    # 3. Claude Code keys per-project history by the encoded project path
    def encode(path: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "-", path)

    old_enc, new_enc = encode(old_prefix), encode(new_prefix)
    config_dirs = [home / ".claude", *(orca_dir.glob("claude-profiles/*/home") if orca_dir.is_dir() else [])]
    for config_dir in config_dirs:
        projects = config_dir / "projects"
        if not projects.is_dir() or projects.is_symlink() or not real_child(home, str(projects.relative_to(home)) + "/x"):
            continue
        for entry in sorted(projects.iterdir()):
            name = entry.name
            if not (name == old_enc or name.startswith(old_enc + "-")) or entry.is_symlink():
                continue
            dest = projects / (new_enc + name[len(old_enc) :])
            if dest.exists() or dest.is_symlink():
                continue
            try:
                entry.rename(dest)
                stats["renamed_project_dirs"] += 1
            except OSError:
                pass

    return stats


def fix_ssh_permissions(home: Path) -> None:
    """~/.ssh and every folder below it 0700, private files 0600, public ones 0644 (symlinks untouched)."""
    ssh_dir = home / ".ssh"
    if not ssh_dir.is_dir() or ssh_dir.is_symlink():
        return
    public = {"known_hosts", "known_hosts.old", "config", "authorized_keys"}
    for dirpath, dirnames, filenames in os.walk(ssh_dir):
        try:
            os.chmod(dirpath, 0o700)
        except OSError:
            pass
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            try:
                path.chmod(0o644 if name.endswith(".pub") or name in public else 0o600)
            except OSError:
                pass


def running_agents_from_ps(comm_out: str, args_out: str, uid: int) -> list[str]:
    """Agent apps and CLIs of user `uid` in `ps -ax -o pid=,uid=,comm=` / `-o pid=,args=` output."""
    comm: dict[str, str] = {}
    for line in comm_out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[1] == str(uid):
            comm[parts[0]] = parts[2]
    args: dict[str, str] = {}
    for line in args_out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            args[parts[0]] = parts[1]

    running: list[str] = []
    for app in AGENT_APPS:
        # Any executable of the bundle counts: helpers keep the databases open as well
        needle = f"/{app}.app/Contents/"
        if any(needle in c for c in comm.values()):
            running.append(app)
    for pid, c in comm.items():
        if ".app/Contents/" in c:
            continue
        base = os.path.basename(c)
        name = base if base in AGENT_CLIS else ""
        if not name and base in ("node", "bun", "deno"):
            # Script-based CLIs run as `node /path/to/gemini ...`
            for token in args.get(pid, "").split()[1:4]:
                if os.path.basename(token) in AGENT_CLIS:
                    name = os.path.basename(token)
                    break
        if name and f"{name} (CLI)" not in running:
            running.append(f"{name} (CLI)")
    return running


def check_running_agent_processes() -> list[str]:
    """Running agent desktop apps and CLI agents (Claude Code, Codex CLI, ...) of this user.

    They keep the databases and config files that a restore replaces open, so a restore refuses to
    run underneath them; that includes an agent session that was asked to run the restore."""
    try:
        comm_out = subprocess.run(["/bin/ps", "-ax", "-o", "pid=,uid=,comm="], capture_output=True, text=True, timeout=5).stdout
        args_out = subprocess.run(["/bin/ps", "-ax", "-o", "pid=,args="], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    return running_agents_from_ps(comm_out, args_out, os.getuid())


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


def disk_usage_bytes(paths: list[str], cwd: Path | None = None, timeout: int = 300) -> int | None:
    """Allocated size of `paths` (hard links counted once, symlinks not followed); None if unknown."""
    if not paths:
        return 0
    try:
        r = subprocess.run(
            ["/usr/bin/du", "-skc", "--", *paths], cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        last = r.stdout.strip().splitlines()[-1].split()
        return int(last[0]) * 1024 if last and last[-1] == "total" else None
    except Exception:
        return None


def snapshot_size_bytes(path: Path, timeout: int = 300) -> int | None:
    return disk_usage_bytes([str(path)], timeout=timeout)


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
        suffix=this_machine[:6] if this_machine else "",
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
    encrypted = manifest["volume_encrypted"] if "volume_encrypted" in manifest else volume_is_encrypted(vol)
    return {
        "packed_local": _local_time(str(manifest.get("packed_at") or "")),
        "source_host": manifest_host(manifest) or "unknown",
        "source_user": manifest.get("source_user") or "unknown",
        "macos": manifest.get("macos_version") or "unknown",
        "arch": manifest.get("arch") or "unknown",
        "volume_uuid": manifest.get("volume_uuid") or get_volume_uuid(vol),
        "encrypted": encrypted,
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
    }


def _q(value: object) -> str:
    """Shell-quote a path for a generated script or a command the user copies."""
    return shlex.quote(str(value))


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
LIVE_BLUEPRINT_REL={_q(live_blueprint_rel)}
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
    return """#!/usr/bin/env zsh
# ==============================================================================
# New-Mac restore, one click. Generated by migrate.py — edits are overwritten.
# Double-click it in Finder, or run in Terminal:  zsh "<this drive>/Restore on New Mac.command"
# Preview without writing anything: add --restore-dry-run
# Only restore from a drive you trust: it installs shell files and background jobs from the snapshot.
# ==============================================================================
set -euo pipefail
VOL_ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /bin/zsh "${VOL_ROOT}/AI/Migration/restore-on-new-mac.sh" "$@"
"""


def render_agent_restore(vol: Path, manifest: dict, pack_state: str, facts: dict) -> str:
    launcher = vol / "AI/Migration/restore-on-new-mac.sh"
    blueprint = blueprint_dir_for(vol)
    migrate_py = blueprint / "scripts/migrate.py"
    held = ", ".join(f"`{x}`" for x in facts["third_party_agents"]) or "none"
    return f"""# New-Mac Restore — Instructions for AI Agents

> Generated by `migrate.py`; overwritten on every pack. Human-readable guide: `{vol / ROOT_GUIDE_NAME}`.

| | |
| :--- | :--- |
| Snapshot | `{vol / 'AI/Migration/latest'}` |
| Packed | {facts['packed_local']} on `{facts['source_host']}` (user `{facts['source_user']}`, macOS {facts['macos']}, {facts['arch']}) |
| State | `{pack_state}`{' · packed with `--fast-config-only` (no raw session journals or .app bundles)' if facts['fast_config_only'] else ''} |

## Rules

- **You may run the preview only.** The real restore replaces the databases and config files of
  Orca, Claude, ChatGPT, Codex, Antigravity and Cursor *and of CLI agents such as Claude Code and
  Codex CLI, including this session*, and it refuses to run while any of them is open. Ask the user
  to quit them, end this session and run step 2 in a plain Terminal window. Do not pass `--force`.
- Do not edit files under `{vol / 'AI/Migration/latest'}`; treat the snapshot as read-only.
- Never print the contents of `metadata/keychain-secrets.json`, `home/.ssh` or any token file.
- The snapshot is trusted input: restore copies shell startup files, LaunchAgents and tools from it
  into `$HOME`. If the user is not sure where the drive has been, stop and say so.

## Steps

1. **Preview** (writes nothing; you may run it and summarise the result):
   ```bash
   zsh {_q(launcher)} --restore-dry-run
   ```
2. **Restore and deploy** (the user runs it in Terminal after quitting every agent):
   ```bash
   zsh {_q(launcher)}
   ```
   It may ask once for the login password (installing apps or a Python runtime). Files that already
   exist on this Mac are kept under `~/.migration-preexisting/<timestamp>/`.
3. **Afterwards**, in a new session: run `zsh {_q(blueprint / 'deploy.sh')} --doctor`, show the user
   `summary.attention` and help them work through every entry.

## What restore deliberately leaves to the user

- **Third-party LaunchAgents** from the old Mac ({held}) are copied but held with
  `launchctl disable`, so the same job never runs on two Macs. List or enable them with:
  ```bash
  python3 {_q(migrate_py)} agents
  python3 {_q(migrate_py)} agents --enable <label>
  ```
  Ask before enabling anything; the old Mac may still be running it.
- **Keychain items that already exist on this Mac with a different value** are not overwritten;
  restore lists them. Resolve them with the user.
- **NAS / SMB credentials** are not migrated. If backups target a network share, the user has to
  connect to it once in Finder and save the password.
- **VPN and other network logins** need the user to sign in again.
"""


def render_guide(vol: Path, manifest: dict, pack_state: str, facts: dict, extra: str = "") -> str:
    """RESTORE-GUIDE.md — every number and name comes from the snapshot, nothing is hardcoded."""
    blueprint = blueprint_dir_for(vol)
    migrate_py = blueprint / "scripts/migrate.py"
    deploy_sh = blueprint / "deploy.sh"
    root_command = vol / ROOT_COMMAND_NAME

    def code_list(items: list[str]) -> str:
        return ", ".join(f"`{x}`" for x in items)

    if facts["encrypted"] is True:
        disk_note = "encrypted"
    elif facts["encrypted"] is False:
        disk_note = "**not encrypted**, see section 5"
    else:
        disk_note = "encryption unknown"

    accounts = []
    if facts["claude_profiles"]:
        accounts.append(f"{facts['claude_profiles']} Claude account(s)")
    if facts["codex_accounts"]:
        accounts.append(f"{facts['codex_accounts']} Codex account(s)")
    if facts["skills_count"]:
        accounts.append(f"{facts['skills_count']} global skill(s)")

    runtimes = []
    if facts["uv_pythons"]:
        runtimes.append(f"standalone CPython ({facts['uv_pythons'][-1]})")
    if facts["python_framework"]:
        runtimes.append("Python.framework (fallback; installing it asks for the login password once)")
    runtime_text = "; ".join(runtimes) or "not packed: the new Mac needs Python 3.9 or later"

    rows = [
        ("Packed", f"{facts['packed_local']} · state `{pack_state}`"),
        ("Source Mac", f"`{facts['source_host']}` (user `{facts['source_user']}`, macOS {facts['macos']}, {facts['arch']})"),
        ("Drive", f"`{vol}` (volume UUID `{facts['volume_uuid']}`, {disk_note})"),
        ("Folders and config", f"{facts['targets_count']} paths: agent state and sessions, CLI tools, SSH / Git / Zsh, Xcode signing, local repositories"),
        ("SQLite databases", f"{facts['sqlite_total']}, of which {facts['sqlite_ok']} passed `quick_check`"),
    ]
    if accounts:
        rows.append(("Accounts and skills", ", ".join(accounts)))
    if facts["apps"]:
        rows.append(("Desktop apps", f"{len(facts['apps'])}: {', '.join(facts['apps'])}"))
    rows.append(("Python runtime", runtime_text))
    if facts["keychain_services"]:
        rows.append(("Keychain secrets", code_list(facts["keychain_services"])))
    rows.append(("Not included", "VM images (Claude VM, iOS simulator devices), caches, Xcode itself, NAS / network passwords"))
    overview = "\n".join(f"| {k} | {v} |" for k, v in rows)

    fast_warning = (
        "\n> ⚠️ This is a `--fast-config-only` snapshot: no raw session logs and no `.app` bundles. It syncs "
        "configuration only and is not a complete migration.\n"
        if facts["fast_config_only"]
        else ""
    )
    state_warning = (
        f"\n> ⚠️ The snapshot state is `{pack_state}`, not `complete`. Restore will refuse it: run "
        "`deploy.sh --pack-to-ssd` on the source Mac again.\n"
        if pack_state != "complete"
        else ""
    )

    checks = [
        "`summary.attention` in the health check output is empty. Every line in it is something to fix.",
        "Open Orca: projects, worktrees, past conversations and every account's login are there.",
        f"Check the backups: `zsh {_q(deploy_sh)} --backup-status` shows each job as `ok` (right after a restore "
        "a job may simply not have run yet).",
    ]
    if facts["nas_smb_url"]:
        checks.append(
            f"NAS backups need the share password saved once on this Mac: Finder → Go → Connect to Server → "
            f"`{facts['nas_smb_url']}`, tick “Remember this password in my keychain”. Until then the Restic jobs "
            "report `skipped`."
        )
    checks.append("VPN clients and other network logins need you to sign in again.")
    checks.append("Projects that need Xcode: install Xcode from the App Store; the snapshot only holds its settings and signing profiles.")
    checklist = "\n".join(f"- [ ] {c}" for c in checks)

    job_rows = []
    if facts["blueprint_jobs"]:
        job_rows.append(
            f"| Blueprint backup jobs ({len(facts['blueprint_jobs'])}) | {code_list(facts['blueprint_jobs'])} | "
            "enabled once their Keychain secrets are in place |"
        )
    if facts["third_party_agents"]:
        job_rows.append(
            f"| Third-party jobs ({len(facts['third_party_agents'])}) | {code_list(facts['third_party_agents'])} | "
            "**held**: they do not start, not even after a reboot |"
        )
    jobs_table = (
        "| Kind | Jobs | State after the restore |\n| :--- | :--- | :--- |\n" + "\n".join(job_rows)
        if job_rows
        else "The snapshot has no background jobs."
    )

    if facts["encrypted"] is False:
        encryption_advice = (
            "- This drive is **not encrypted**: anyone holding it can read all of the above. Encrypt it in Finder "
            "(right-click the drive → Encrypt), or delete `keychain-secrets.json` and `old-login.keychain-db` from "
            "`AI/Migration/latest/metadata/` once the migration is done."
        )
    elif facts["encrypted"] is True:
        encryption_advice = "- This drive is encrypted and needs its password after it is unplugged. Keep that password safe."
    else:
        encryption_advice = "- Whether this drive is encrypted could not be determined. If it is not, keep it as safe as a password book."

    extra_block = f"\n---\n\n{extra.strip()}\n" if extra.strip() else ""

    return f"""# Restore on a New Mac

> Generated by `migrate.py` from the snapshot; rewritten on every pack and by `deploy.sh --refresh-guide`.
> Notes you want to keep belong in `AI/Migration/{GUIDE_EXTRA_NAME}`; they are appended at the end unchanged.
{state_warning}{fast_warning}
## Snapshot overview

| Item | Details |
| :--- | :--- |
{overview}

## 1. Before you start

1. Finish the macOS setup assistant on the new Mac. It must run macOS {facts['macos']} or later (the source Mac's version).
2. Connect this drive and check that it shows up in Finder.
3. Quit every AI app **and CLI agent**: Orca, Claude, ChatGPT, Codex, Antigravity, Cursor, Claude Code, Codex CLI,
   Gemini CLI. Restore replaces their databases and config files and refuses to run while any of them is open.
4. The source Mac can stay on: each Mac keeps its backups under its own host name, and third-party background
   jobs stay paused on the new Mac (section 4).
5. **Only restore from a drive you trust.** Restore copies shell startup files, LaunchAgents and tools from the
   snapshot into your home folder; a tampered snapshot could run code as you.

## 2. Run the restore

Open Terminal, paste this line and press Return:

```bash
zsh {_q(root_command)}
```

- Or double-click `{ROOT_COMMAND_NAME}` in Finder.
- To preview without writing anything, add ` --restore-dry-run` at the end.
- You may be asked once for your login password, to install apps or a Python runtime.
- Nothing on this Mac is lost: files the restore replaces are kept in `~/.migration-preexisting/<time>/`.
- Keychain items that already exist on this Mac with a different value are left alone and listed at the end.

An AI agent can help: ask a terminal agent (Claude Code, Codex CLI, ...) to read
`{vol / 'AI/Migration/AGENT_RESTORE.md'}` and run the **preview** for you. The restore itself runs in a plain
Terminal window after you have quit the agent.

## 3. After the restore

A health check (`doctor.py`) runs at the end. Confirm each item:

{checklist}

## 4. Background jobs

{jobs_table}

Third-party jobs are paused so the same job never runs on two Macs at once (duplicate syncs, two machines writing
the same data). Once a job no longer runs on the source Mac, enable it here:

```bash
python3 {_q(migrate_py)} agents                    # state of every job
python3 {_q(migrate_py)} agents --enable <Label>   # enable and start it
python3 {_q(migrate_py)} agents --hold <Label>     # pause it again
```

## 5. Security

- The snapshot holds plaintext secrets: `AI/Migration/latest/metadata/keychain-secrets.json` (backup encryption keys),
  `home/.ssh/` (SSH private keys) and the agents' login tokens.
{encryption_advice}
- Treat this drive like the old Mac itself: do not restore from it if it has been out of your hands.
- After the restore, a copy of the source Mac's login keychain is at `~/Migration-Imports/old-login.keychain-db`, only
  for looking up old passwords. Delete it once you no longer need it.

## 6. Troubleshooting

| Symptom | What to do |
| :--- | :--- |
| An AI app or agent is still running | Quit it (CLI agents too) and run the command again. Do not skip the check with `--force`: databases in use get corrupted. |
| The snapshot state is not `complete` | The last pack was interrupted or failed. Run `deploy.sh --pack-to-ssd` on the source Mac again. |
| Not enough free disk space | Free up the amount shown on the internal disk and try again. |
| “The xcode-select command requires the command line developer tools” | Click Cancel; the restore does not need them. Install them later for `git` or Xcode. |
| A Keychain conflict is reported | This Mac already has that item with another value. Keep the one this Mac's backups use; the snapshot's value is in `keychain-secrets.json`. |
| Backup jobs show `skipped` | The backup target is unavailable, usually a NAS that is not connected or has no saved password (section 3). |
| Undo the restore | Replaced files are in `~/.migration-preexisting/<time>/`; move them back to the same paths. |

## 7. Advanced: move the workspace to the internal disk

After a restore the workspace stays on the external drive: projects and worktrees remain in `{vol}/AI`. If the new
Mac's internal disk is large enough and you want to stop depending on the drive:

```bash
zsh {_q(deploy_sh)} --auto --internal
```

This only creates the folder layout under `~/AI-Workspace`; it does **not** copy the projects. Copy `{vol}/AI/Projects`
over yourself.

## 简体中文摘要

- 在「终端」执行 `zsh {_q(root_command)}`；只预览不写入时在末尾加 ` --restore-dry-run`。
- 恢复前退出所有 AI 应用和命令行 Agent（Claude Code、Codex CLI 等），否则恢复会拒绝执行。
- 只从你信任的硬盘恢复：快照里的 shell 配置和后台任务会在你的账户下运行。
- 被替换的文件保存在 `~/.migration-preexisting/<时间>/`；本机已有且值不同的钥匙串条目不会被覆盖。
- 第三方后台任务默认暂停，确认旧电脑不再运行后用 `python3 {_q(migrate_py)} agents --enable <Label>` 启用。
{extra_block}"""


def sync_blueprint_copy(mig_blueprint: Path) -> str:
    """Mirror this checkout into the snapshot so a restore never depends on network access.

    Returns "" on success, otherwise the rsync error."""
    repo_root = Path(__file__).resolve().parent.parent
    target = mig_blueprint.resolve()
    if repo_root == target:
        return ""  # running from the embedded copy itself
    if target in repo_root.parents or repo_root in target.parents:
        return f"blueprint copy skipped: {mig_blueprint} and the checkout {repo_root} contain each other"
    mig_blueprint.mkdir(parents=True, exist_ok=True)
    cmd = ["/usr/bin/rsync", "-a", "--delete"]
    for pattern in (".git", "__pycache__", "*.pyc", ".DS_Store", "*.bak*"):
        cmd.extend(["--exclude", pattern])
    cmd.extend(["--", f"{repo_root}/", f"{mig_blueprint}/"])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return f"blueprint rsync failed (rc={r.returncode}): {r.stderr[:300].strip()}"
    return ""


def write_restore_artifacts(vol: Path, manifest: dict, pack_state: str) -> dict[str, Path]:
    """(Re)write the launcher, the agent instructions and the root-level guide for a snapshot.

    Launchers and guides written under other names by earlier versions are left in place (they sit on
    the user's drive) and returned as `legacy:<name>` so the caller can mention them."""
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
    for name in LEGACY_ROOT_FILES:
        if (vol / name).exists():
            paths[f"legacy:{name}"] = vol / name
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
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with src.open("rb") as fin, os.fdopen(os.open(tmp, flags, 0o600), "wb") as fout:
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
        if not is_safe_relative(rel) or rel in current or not real_child(mig_home, rel):
            continue
        # Never touch a path that contains, or is contained in, something this pack mirrors
        if any(cur.startswith(rel + "/") or rel.startswith(cur + "/") for cur in current):
            continue
        stale = mig_home / rel
        try:
            if stale.is_symlink() or stale.is_file():
                stale.unlink()
            elif stale.is_dir():
                subprocess.run(["/bin/chmod", "-R", "u+w", "--", str(stale)], capture_output=True)
                shutil.rmtree(stale)
            else:
                continue
        except OSError:
            continue
        removed.append(rel)
    return removed


def _remove_snapshot_dir(mig_home: Path, rel: str) -> None:
    """Delete a directory inside the snapshot, unless it is (or sits behind) a symlink."""
    path = mig_home / rel
    if path.is_dir() and not path.is_symlink() and real_child(mig_home, rel):
        subprocess.run(["/bin/chmod", "-R", "u+w", "--", str(path)], capture_output=True)
        shutil.rmtree(path, ignore_errors=True)


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
    pack_warnings: list[str] = []

    # Targets: built-in list + EXTRA_MIGRATION_PATHS + folders that third-party LaunchAgents use.
    # The LaunchAgent ones are only taken when they are small; the user opts into big ones explicitly.
    la_deps = discover_launchagent_dependencies(home)
    explicit = set(HOME_RELATIVE_TARGETS) | set(extra_migration_paths(cfg))
    existing_targets: list[str] = []
    for rel in get_home_targets(home, la_deps):
        path = home / rel
        if not (path.exists() or path.is_symlink()):
            continue
        if rel not in explicit:
            size = snapshot_size_bytes(path, timeout=60)
            if size is None or size > DISCOVERED_DIR_MAX_BYTES:
                pack_warnings.append(
                    f"~/{rel} (used by a LaunchAgent) is {'too large to measure' if size is None else format_bytes(size)} "
                    "and was not packed; add it to EXTRA_MIGRATION_PATHS to include it"
                )
                continue
        existing_targets.append(rel)
    for rel in existing_targets:
        if (home / rel).is_symlink():
            pack_warnings.append(
                f"~/{rel} is a symlink to {os.readlink(home / rel)}: only the link is packed, not what it points to"
            )

    sqlite_targets = discover_all_sqlite_targets(home, existing_targets)
    # A database behind a symlinked folder: the snapshot holds only the link, so writing the copy to
    # its snapshot path would write through that link into the live folder
    for rel_db in [s for s in sqlite_targets if not real_child(home, s)]:
        sqlite_targets.remove(rel_db)
        pack_warnings.append(f"~/{rel_db} sits behind a symlinked folder and was not snapshotted")
    if fast_config_only:
        sqlite_targets = [
            s
            for s in sqlite_targets
            if not s.endswith("agent-session-journal.db") and not s.endswith("thread_history_1.sqlite")
        ]

    core_apps = discover_core_apps(home) if live else []
    unbundled_la_apps = [a for a in la_deps[1] if a not in core_apps] if live else []
    keychain_services = discover_keychain_services(home, la_deps) if live else []
    py_fw = Path("/Library/Frameworks/Python.framework")
    has_py_fw = live and py_fw.is_dir()

    fp_file = mig_meta / "bundle_fingerprints.json"
    cached_fps: dict[str, str] = {} if (force_apps or foreign) else read_json(fp_file)
    bundles: list[tuple[str, Path, Path]] = []  # (fingerprint key, source, snapshot copy)
    if live and not fast_config_only:
        bundles = [(f"app:{app}", Path("/Applications") / app, mig_apps / app) for app in core_apps]
        if has_py_fw:
            bundles.append(("sys:Python.framework", py_fw, mig_sys / "Python.framework"))
    fingerprints = {key: compute_bundle_fingerprint(src) for key, src, _ in bundles}
    bundles_to_sync = [b for b in bundles if not (b[2].is_dir() and cached_fps.get(b[0]) == fingerprints[b[0]])]

    free_bytes = shutil.disk_usage(vol).free if vol.exists() else 0
    encrypted = volume_is_encrypted(vol) if str(vol).startswith("/Volumes/") else None
    running_apps = check_running_agent_processes() if live else []

    # Space this pack still needs on the drive: what is not in the snapshot yet, plus headroom
    src_bytes = disk_usage_bytes(existing_targets, cwd=home)
    snap_bytes = disk_usage_bytes([str(mig_home)]) if mig_home.is_dir() else 0
    need_bytes: int | None = None
    if src_bytes is not None and snap_bytes is not None:
        need_bytes = max(0, src_bytes - snap_bytes)
        for _, src, dst in bundles_to_sync:
            src_size = snapshot_size_bytes(src)
            dst_size = snapshot_size_bytes(dst) if dst.exists() else 0
            if src_size is None or dst_size is None:
                need_bytes = None
                break
            need_bytes += max(0, src_size - dst_size)
    headroom = 1024**3

    print("==================================================================")
    print(" 📦 Orca Multi-Agent Blueprint — Pack Internal State to SSD")
    print(f" Mode              : {'DRY-RUN (Preview)' if dry_run else 'APPLY (Live Pack)'}")
    print(f" Source Host       : {this_host} ({platform.machine()})")
    print(f" Source User       : {os.environ.get('USER', 'unknown')} (UID {os.getuid()})")
    print(f" Destination Root  : {mig_root}")
    print(f" Volume UUID       : {vol_uuid} (Free Space: {format_bytes(free_bytes)})")
    print(f" Space Needed      : {format_bytes(need_bytes) + ' + 1 GB headroom' if need_bytes is not None else 'unknown'}")
    print(" VM Exclusion      : Enabled (skips claudevm.bundle, claude-code-vm & CoreSimulator)")
    print(f" Fast Config Only  : {fast_config_only}")
    if fast_config_only:
        print(" ⚠️  WARNING       : --fast-config-only skips raw session journals & .app bundles!")
    print("==================================================================")

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
    if need_bytes is None:
        pack_warnings.append("the space this pack needs could not be measured; free space was not checked")
    elif free_bytes < need_bytes + headroom:
        message = (
            f"{vol} has {format_bytes(free_bytes)} free, this pack needs about {format_bytes(need_bytes)} "
            f"(+{format_bytes(headroom)} headroom)"
        )
        if not dry_run and not force:
            print(f"❌ Error: {message}. Free up space on the drive (or pass --force).", file=sys.stderr)
            return 1
        pack_warnings.append(message)
    if not str(vol).startswith("/Volumes/"):
        pack_warnings.append(f"the snapshot is written to {vol} on this Mac's own disk, not to an external drive")
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
    for app in unbundled_la_apps:
        print(f"    ◦ /Applications/{app} is used by a LaunchAgent but not bundled (add it to EXTRA_BUNDLED_APPS)")

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

    # Snapshots packed before schema 1.4 kept login.keychain-db inside home/Library/Keychains (B4)
    _remove_snapshot_dir(mig_home, "Library/Keychains")

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

    # Step 1: rsync -aHR from $HOME to preserve hardlinks across ~/.codex & orca/codex-accounts
    print("\n--> Step 1/5: Running lossless `rsync -aHR` on $HOME paths (preserving hardlinks & symlinks)...")
    # Read-only runtime generations copied by versions that did not exclude them block `--delete`
    _remove_snapshot_dir(mig_home, "Library/Application Support/Codex/artifact-sessions/runtime-generations")

    excludes = list(RSYNC_EXCLUDES)
    for rel_db in sqlite_targets:
        excludes.append(f"/{rel_db}")
    if fast_config_only:
        excludes.extend(FAST_CONFIG_EXCLUDES)

    rsync_cmd = ["/usr/bin/rsync", "-aHR", "--delete"]
    for exc in excludes:
        rsync_cmd.extend(["--exclude", exc])
    rsync_cmd.extend(["--", *existing_targets, str(mig_home) + "/"])

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

    if home_rsync_ok and existing_targets:
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
    cache_root = home / "Library/Caches/orca-migrate-sqlite"

    sqlite_results: dict[str, dict] = {}
    valid_sql_targets = [rel_db for rel_db in sqlite_targets if (home / rel_db).is_file()]
    for rel_db in [r for r in valid_sql_targets if not real_child(mig_home, r)]:
        # The snapshot has a symlink where this Mac has a folder: never write through it
        valid_sql_targets.remove(rel_db)
        pack_error(f"SQLite backup skipped for {rel_db}: its snapshot folder is a symlink")
    with ThreadPoolExecutor(max_workers=4) as pool:
        future_map = {
            pool.submit(
                sqlite_online_backup,
                home / rel_db,
                mig_home / rel_db,
                prev_sqlite_meta.get(rel_db),
                cache_root,
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
    new_fps = dict(cached_fps)
    if fast_config_only or not live:
        print("\n--> Step 3/5: Skipped .app bundles & Python.framework.")
    else:
        print("\n--> Step 3/5: Mirroring core AI .app bundles & Python.framework (with fingerprint cache)...")
        t_app = time.time()
        for key, _, _ in bundles:
            if not any(key == b[0] for b in bundles_to_sync):
                new_fps[key] = fingerprints[key]
        apps_synced = [src.name for key, src, _ in bundles_to_sync if key.startswith("app:")]
        if apps_synced:
            app_rsync = ["/usr/bin/rsync", "-aH", "--delete", "--", *apps_synced, str(mig_apps) + "/"]
            app_proc = subprocess.run(app_rsync, cwd=Path("/Applications"), capture_output=True, text=True)
            if app_proc.returncode == 0:
                new_fps.update({f"app:{app}": fingerprints[f"app:{app}"] for app in apps_synced})
            else:
                pack_error(f".app bundles rsync failed (rc={app_proc.returncode}): {app_proc.stderr[:300].strip()}")
        print(
            f"    ✅ Core .app bundles ready in {round(time.time() - t_app, 1)}s "
            f"({len(apps_synced)} synced, {len(core_apps) - len(apps_synced)} unchanged skipped)"
        )

        if has_py_fw:
            t_py = time.time()
            if not any(b[0] == "sys:Python.framework" for b in bundles_to_sync):
                print("    ⚡ Python.framework unchanged (fingerprint match, 0.0s)")
            else:
                py_rsync = ["/usr/bin/rsync", "-aH", "--delete", "--", "Python.framework", str(mig_sys) + "/"]
                py_proc = subprocess.run(py_rsync, cwd=Path("/Library/Frameworks"), capture_output=True, text=True)
                if py_proc.returncode == 0:
                    new_fps["sys:Python.framework"] = fingerprints["sys:Python.framework"]
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
        [svc for svc in (cfg.get("BACKUP_KEYCHAIN_RESTIC") or "restic-mac-agents", cfg.get("BACKUP_KEYCHAIN_ICLOUD") or "icloud-package-key") if svc not in secrets_map]
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
    manifest = {
        "schema_version": "1.5",
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
    print_legacy_artifacts(artifacts)

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


def print_legacy_artifacts(artifacts: dict[str, Path]) -> None:
    legacy = [path for name, path in artifacts.items() if name.startswith("legacy:")]
    if legacy:
        print("    • Older launcher/guide names from a previous version are still on the drive (left untouched, safe to delete):")
        for path in legacy:
            print(f"        {path}")


def restorable_targets(manifest: dict, mig_home: Path) -> tuple[list[str], list[str]]:
    """(targets to restore, rejected targets) from a manifest that lives on removable media.

    Only plain relative paths are taken; a path nested in another target is covered by its parent and
    dropped, and a path whose parent folder in the snapshot is a symlink is rejected, so no file can be
    written through a link to somewhere outside $HOME."""
    candidates = [
        rel
        for rel in manifest.get("targets") or []
        if is_safe_relative(rel) and not rel.startswith("Library/Keychains")
    ]
    targets: list[str] = []
    rejected: list[str] = []
    for rel in candidates:
        if any(rel.startswith(other + "/") for other in candidates if other != rel):
            continue
        if not real_child(mig_home, rel):
            rejected.append(rel)
            continue
        if (mig_home / rel).exists() or (mig_home / rel).is_symlink():
            targets.append(rel)
    return targets, rejected


def _file_signature(path: Path) -> tuple[int, int] | None:
    """Identity of the file at `path`: rsync replaces a file by renaming a new one over it (new inode),
    while a skipped file keeps its inode (rsync may still touch its mtime)."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


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

    # B7 (P2-4): Verify PACK_STATE.json is complete before restoring (a missing file is not "complete")
    if not force:
        pack_state = read_json(pack_state_file).get("state", "unreadable") if pack_state_file.is_file() else "missing"
        if pack_state != "complete":
            print(
                f"❌ Error: Snapshot PACK_STATE is '{pack_state}' (not 'complete'). "
                f"Refusing to restore an interrupted or failed snapshot (pack again, or pass --force).",
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

    # B7 (P1-6): Check running agent apps and CLI agents before a live restore
    running_apps = check_running_agent_processes() if live else []
    if running_apps and not dry_run and not force:
        print(
            f"❌ Error: AI agent app(s) or CLI agent(s) still running: {', '.join(running_apps)}.\n"
            f"   Quit them (an agent session that started this restore too) and run restore again from a plain\n"
            f"   Terminal window. Their live databases and config files would be replaced underneath them\n"
            f"   (--force skips this check at the risk of corrupting those databases).",
            file=sys.stderr,
        )
        return 1

    # The manifest lives on removable media: only plain relative paths inside $HOME are accepted from it.
    # B4 (P1-1): Library/Keychains is never restored, so login.keychain-db is never overwritten.
    targets, rejected_targets = restorable_targets(manifest, mig_home)
    sqlite_rels = [rel for rel in (manifest.get("sqlite_backups") or {}) if is_safe_relative(rel)]
    bundled_apps = [
        app
        for app in manifest.get("bundled_core_apps") or []
        if is_safe_relative(app) and "/" not in app and (mig_apps / app).is_dir()
    ]
    missing_apps = [app for app in bundled_apps if not (Path("/Applications") / app).is_dir()] if live else []

    # Free space: everything in home/ (and every app still to install) lands on the internal disk;
    # replaced files are moved aside, not copied
    free_home_bytes = shutil.disk_usage(home).free if home.exists() else 0
    need_bytes = snapshot_size_bytes(mig_home)
    if need_bytes is not None and missing_apps:
        app_bytes = disk_usage_bytes([str(mig_apps / app) for app in missing_apps])
        need_bytes = None if app_bytes is None else need_bytes + app_bytes
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

    if need_bytes is None:
        print("⚠️  The snapshot size could not be measured: free disk space was NOT checked.")
    if manifest.get("fast_config_only"):
        print(
            "⚠️  WARNING: This snapshot was packed with `--fast-config-only` (raw session journals & .app bundles excluded)."
        )
    if running_apps and dry_run:
        print(f"⚠️  Running now: {', '.join(running_apps)} — quit them before the real restore.")
    if rejected_targets:
        print(f"⚠️  Not restored (a parent folder in the snapshot is a symlink): {', '.join(rejected_targets)}")

    if current_user != src_user or current_uid != src_uid:
        print(
            f"ℹ️  Note: Target user/UID ({current_user}/{current_uid}) differs from source ({src_user}/{src_uid}).\n"
            f"    Restore will relocate /Users/{src_user} -> {home} in symlinks, LaunchAgents, shell/Git/SSH/agent\n"
            f"    configs and Claude Code project folders (not inside databases)."
        )

    print(f"\n--> Will restore {len(targets)} paths from {mig_home} into {home} (Keychains safely excluded):")
    for rel in targets:
        print(f"    • ~/{rel}")
    if bundled_apps:
        print(f"\n--> Will install any missing core .app bundles ({len(bundled_apps)} bundled) into /Applications:")
        for app in bundled_apps:
            status = "already installed" if (Path("/Applications") / app).is_dir() else "will install"
            print(f"    • /Applications/{app} ({status})")

    snapshot_la = mig_home / "Library/LaunchAgents"
    la_dir = home / "Library/LaunchAgents"
    enabled_set = set(enable_agents or [])
    if dry_run:
        staged = [label for label, _ in third_party_agents(snapshot_la) if label not in enabled_set]
        if staged:
            print(f"\n--> Third-party LaunchAgents that will be restored but held (not started): {', '.join(staged)}")
        print("\n✅ [DRY-RUN] Restore preview complete. Run with `--apply` to execute restore.")
        return 0

    follow_up: list[str] = []

    def hold_third_party_agents() -> list[str]:
        """Hold the snapshot's third-party LaunchAgents as soon as their plists are in place.

        Runs right after the copy, before any step that can fail, because launchd loads every plist
        in ~/Library/LaunchAgents at the next login."""
        held_now: list[str] = []
        for label, _ in plan_restored_agents(la_dir, snapshot_la, enabled_set)["hold"]:
            if not live:
                held_now.append(label)
            elif launchd_loaded(current_uid, label):
                continue  # already running on this Mac (enabled earlier): not ours to stop
            elif launchd_set_enabled(current_uid, label, False):
                held_now.append(label)
            else:
                follow_up.append(f"Could not hold LaunchAgent {label}; it will start at the next login.")
        return held_now

    def print_follow_up() -> None:
        if follow_up:
            print("\n--> Needs your attention after this restore:")
            for item in follow_up:
                print(f"    • {item}")

    # Step 1: Restore files via rsync -aHR with --backup-dir rollback protection (B7)
    ts_str = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = home / f".migration-preexisting/{ts_str}"
    print(f"\n--> Step 1/5: Restoring $HOME files & hardlinks via `rsync -aHR` (pre-existing backup -> {backup_dir})...")
    db_before = {rel: _file_signature(home / rel) for rel in sqlite_rels}
    rsync_cmd = [
        "/usr/bin/rsync",
        "-aHR",
        "--backup",
        f"--backup-dir={backup_dir}",
        "--exclude",
        "/Library/Keychains/",
        "--",
        *targets,
        str(home) + "/",
    ]
    rsync_failed = ""
    if targets:
        proc = subprocess.run(rsync_cmd, cwd=mig_home, capture_output=True, text=True)
        if proc.returncode not in (0, 24):
            rsync_failed = f"rsync error (exit {proc.returncode}): {proc.stderr[:400]}"
    held = hold_third_party_agents()
    if held:
        print(
            f"    ⏸  Held {len(held)} third-party LaunchAgent(s) so they do not run on two Macs at once "
            f"(they stay off across logins): {', '.join(held)}"
        )
        follow_up.append(
            f"Held LaunchAgents: {', '.join(held)}. When the source Mac no longer runs them: "
            f"python3 {_q(Path(__file__).resolve())} agents --enable <label>"
        )
    if rsync_failed:
        print(f"❌ {rsync_failed}", file=sys.stderr)
        print_follow_up()
        return 1
    print(f"    ✅ Restored {len(targets)} paths (agent folders, SQLite databases, configs and local repositories).")

    # Stage old login.keychain-db into ~/Migration-Imports/old-login.keychain-db (B4)
    old_kc_src = mig_meta / "old-login.keychain-db"
    if not old_kc_src.is_file():  # snapshots packed before schema 1.4
        old_kc_src = mig_home / "Library/Keychains/login.keychain-db"
    if old_kc_src.is_file() and real_child(home, "Migration-Imports/old-login.keychain-db"):
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
        for app in missing_apps:
            r = subprocess.run(
                ["/usr/bin/rsync", "-aH", "--", str(mig_apps / app), "/Applications/"],
                capture_output=True,
                text=True,
            )
            (installed_apps if r.returncode == 0 else failed_apps).append(app)
        print(
            f"    ✅ Restored {len(installed_apps)} missing .app bundles into /Applications "
            f"({len(bundled_apps) - len(missing_apps)} already present)."
        )
        if failed_apps:
            print(f"    ⚠️ Could not install: {', '.join(failed_apps)}")
            follow_up.append(f"Apps not installed: {', '.join(failed_apps)}. Copy them from {mig_apps} into /Applications by hand.")
    else:
        print("\n--> Step 2/5: Skipped /Applications install (custom --target-home test mode).")

    # Step 3: Clean stale locks, relocate cross-user paths if needed, fix ~/.ssh permissions, wire symlinks & verify DBs
    print("\n--> Step 3/5: Cleaning stale host locks, relocating paths, verifying SQLite DBs & wiring symlinks...")
    removed_locks = clean_stale_locks(home)
    failed_dbs = []
    for rel_db in sqlite_rels:
        if not real_child(home, rel_db):
            continue
        restored_db = home / rel_db
        if _file_signature(restored_db) != db_before[rel_db]:
            # The database was replaced (the old one went to backup_dir): its WAL / journal belong to
            # the old file, so they go next to it; leaving them would graft them onto the restored one
            for suffix in ("-wal", "-journal", "-shm"):
                side = home / (rel_db + suffix)
                if not (side.exists() or side.is_symlink()):
                    continue
                try:
                    if suffix == "-shm":  # derived index, rebuilt on open
                        side.unlink()
                    else:
                        kept = backup_dir / (rel_db + suffix)
                        kept.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(side, kept)
                    removed_locks.append(rel_db + suffix)
                except OSError as e:
                    failed_dbs.append(f"{rel_db} (could not move {suffix} aside: {e})")
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
    if real_child(home, ".orca/.migrated-from") and not orca_dir.is_symlink():
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
    else:
        follow_up.append("~/.orca is a symlink; the migration marker ~/.orca/.migrated-from was not written.")

    # Flush cfprefsd cache so restored Library/Preferences/*.plist take effect immediately (P2-10)
    if live:
        subprocess.run(["/usr/bin/killall", "cfprefsd"], capture_output=True)

    print(
        f"    ✅ Moved aside {len(removed_locks)} stale lock/WAL files, relocated {reloc_stats['relocated_symlinks']} symlinks, "
        f"{reloc_stats['rewritten_configs']} configs & {reloc_stats['renamed_project_dirs']} Claude project folders, "
        f"verified ~/.ssh (700/600), and wired {len(created_links)} runtime symlinks."
    )
    if new_host_tag:
        print(f"    🏷  Backup host tag set to '{new_host_tag}' (the snapshot carried the source Mac's tag).")
    if failed_dbs:
        print(f"    ❌ SQLite integrity check failed after restore: {failed_dbs}", file=sys.stderr)
        print_follow_up()
        return 1

    # Step 4: Restore Keychain secrets FIRST, then register LaunchAgents (B5)
    print("\n--> Step 4/5: Restoring Keychain secrets & registering LaunchAgents...")
    secrets_file = mig_meta / "keychain-secrets.json"
    if secrets_file.is_file() and live:
        kc = import_keychain_secrets(read_json(secrets_file), current_user)
        print(
            f"    ✅ Imported Keychain services: {', '.join(kc['imported']) or 'none'}"
            + (f" (already present: {', '.join(kc['present'])})" if kc["present"] else "")
        )
        if kc["conflicts"]:
            follow_up.append(
                f"Keychain item(s) already on this Mac with a different value were NOT overwritten: "
                f"{', '.join(kc['conflicts'])}. Keep the value this Mac's backups use; the snapshot's value is in {secrets_file}."
            )
        if kc["failed"]:
            follow_up.append(f"Keychain import failed for: {', '.join(kc['failed'])} (is the login keychain unlocked?)")
    else:
        print("    ℹ️ Keychain import skipped (no keychain-secrets.json or custom target-home).")

    plan = plan_restored_agents(la_dir, snapshot_la, enabled_set)
    unknown_enable = enabled_set - {x for label, p in plan["enable"] for x in (label, p.name, p.stem)}
    if unknown_enable:
        follow_up.append(
            f"--enable-agents named LaunchAgents that are not in ~/Library/LaunchAgents: {', '.join(sorted(unknown_enable))}"
        )

    if live:
        restore_cfg = _load_config_fallbacks(home)
        core_svcs = (
            restore_cfg.get("BACKUP_KEYCHAIN_RESTIC") or "restic-mac-agents",
            restore_cfg.get("BACKUP_KEYCHAIN_ICLOUD") or "icloud-package-key",
        )
        missing_secrets = [svc for svc in core_svcs if not keychain_has(svc)]
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
    else:
        print(
            f"    ℹ️ launchd untouched (custom --target-home): {len(plan['blueprint'])} blueprint, "
            f"{len(plan['enable'])} enabled, {len(held)} held."
        )

    # NAS credentials are per-Mac (internet passwords are not migrated): say so now, not after a week of skipped backups
    smb_match = _env_file_value(home / ".orca/backup-config.env", "BACKUP_NAS_SMB_MATCH")
    m_smb = re.match(r"//(?:([^@/\s]+)@)?([^/\s]+)/(\S+?)(?:\s+on\s|$)", smb_match)
    if m_smb and live:
        r_cred = _security(["find-internet-password", "-s", m_smb.group(2)])
        if r_cred is None or r_cred.returncode != 0:
            user_part = f"{m_smb.group(1)}@" if m_smb.group(1) else ""
            follow_up.append(
                f"NAS backups need the share password saved on this Mac: Finder → Go → Connect to Server → "
                f"smb://{user_part}{m_smb.group(2)}/{m_smb.group(3)} and tick “Remember this password in my keychain”. "
                "Until then the Restic jobs report `skipped`."
            )

    print_follow_up()
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
        if not name.startswith("legacy:"):
            print(f"    • {name:12s}: {path}")
    print_legacy_artifacts(artifacts)
    print("✅ Restore guides refreshed from the existing snapshot (snapshot data untouched).")
    return 1 if blueprint_error else 0


def cmd_status(vol: Path) -> int:
    mig_root = vol / "AI/Migration/latest"
    manifest_file = mig_root / "metadata/MANIFEST.json"
    if not manifest_file.is_file():
        print(json.dumps({"status": "not_packed", "migration_root": str(mig_root)}, indent=2))
        return 0
    manifest = read_json(manifest_file)
    manifest.pop("keychain_digests", None)  # written by schema 1.4; secret hashes are not shown
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
    parser.add_argument("--volume", type=str, default=None, help="Snapshot volume path (pack: required when no external drive is found)")
    args = parser.parse_args()

    def csv(value: str) -> list[str]:
        return [x.strip() for x in value.split(",") if x.strip()]

    if args.action == "agents":
        return cmd_agents(csv(args.enable), csv(args.hold))

    vol = Path(args.volume) if args.volume else detect_volume(require_external_if_configured=(args.action in ("pack", "restore", "guide")))
    dry_run = not args.apply
    if args.action == "pack" and not args.volume and not str(vol).startswith("/Volumes/"):
        # "Pack to SSD" without an SSD must not quietly fill the internal disk with a second copy
        print(
            f"❌ Error: No external drive found (would pack to {vol} on this Mac's own disk).\n"
            "   Connect the SSD (it needs an `AI` folder, or set AI_VOLUME_PATH), or pass --volume <path>\n"
            "   to pack to another location on purpose.",
            file=sys.stderr,
        )
        return 1
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
