#!/usr/bin/env python3
"""
agent-governor v1.5.0 — Multi-Agent Workspace Governance, Context Optimizer & Ephemeral GC
==========================================================================================
A zero-dependency governance, startup-token optimizer, and 5-gate TTL garbage collection
engine for multi-agent coding environments (Claude Code, OpenAI Codex, Google Gemini /
Antigravity, Cursor, Windsurf, Orca).

Core Capabilities:
  1. `audit`     : Audits startup context token budgets (AGENTS.md / CLAUDE.md / GEMINI.md /
                   SKILL.md frontmatter & 500-line body cap / eager MCP count), code-block-aware
                   link & placeholder checks, 4-zone directory norms (`docs`, `reports`,
                   `outputs`, `work`), `.git/info/exclude` & Spotlight shield safety, and
                   unauthorized project/worktree root clutter across nested worktrees.
  2. `adopt`     : Non-destructively initializes the 4-zone layout + 3-tier `work/` lifecycle
                   subdirectories (`_scratch`, `_tests`, `retained`), applies `.metadata_never_index`
                   Spotlight shields across `work/`, `AI/Data`, and `Xcode/DerivedData`, heals
                   `@AGENTS.md` pointers without dirtying `git status`, and relocates root clutter.
  3. `scratch`   : Allocates a collision-safe, lease-stamped per-agent scratch directory under
                   `<project>/work/_scratch/<YYYYMMDD>-<agent>-<topic>/` (default 3-day TTL).
  4. `test-dir`  : Allocates a collision-safe, lease-stamped per-agent test output directory
                   under `<project>/work/_tests/<YYYYMMDD>-<agent>-<topic>/` (default 7-day TTL).
  5. `gc`        : Enforces 5 hard safety gates (`fcntl.flock` mutex, lease owner (PID + start
                   time) & `lsof` open-FD lock, `.keep`/`.pinned`, active-lease & grandfathering
                   guard, batched `git ls-files` & symlink guard, same-volume Trash) to sweep
                   expired `work/_scratch`, `work/_tests`, stale `DerivedData` build caches
                   (custom paths 12h, ~/Library/Developer/Xcode/DerivedData 7d), transient XCTest
                   runner bundles (`*Tests-Runner.app`, `*.xctrunner`), multi-account caches,
                   idle simulators created by `agent-test-device`, and LaunchServices
                   (`lsregister`) registrations of deleted bundles (`🚫`) and XCTest runners,
                   with a reversible `manifest.json` receipt.
  6. `triage`    : Read-only inventory & family grouping of grandfathered `work/*` folders plus
                   stranded `work/` data inside active worktrees.
  7. `restore`   : Atomically previews (`--dry-run`) or restores (`--apply`) any `gc` or
                   `adopt` batch from its `manifest.json`.
"""

from __future__ import annotations

import argparse
import bisect
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Iterator

VERSION = "1.5.0"

LSREGISTER_DEFAULT_BIN = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)

XCODE_SHARED_DERIVED_DIRS = frozenset(
    {
        "ModuleCache.noindex",
        "SDKStatCaches.noindex",
        "CompilationCache.noindex",
        "SDKExplicitPrecompiledModules",
        "SymbolCache.noindex",
    }
)

RUNNER_BUNDLE_SUFFIXES = ("UITests-Runner.app", "Tests-Runner.app", ".xctrunner")
LS_BUNDLE_SUFFIXES = (
    ".app",
    ".appex",
    ".xctrunner",
    ".pluginkit",
    ".saved-application",
    ".saved-service",
    ".app.saved",
)

FOUR_ZONES = ("docs", "reports", "outputs", "work")
WORK_TIERS = ("_scratch", "_tests", "retained")
PIN_MARKERS = (".keep", ".pin", ".pinned", "HOLD", ".hold")

FORBIDDEN_ROOT_DIRS = {
    "tmp",
    "temp",
    ".tmp",
    ".temp",
    "scratch",
    ".scratch",
    ".worktrees",
    "test_tmp",
    "debug_tmp",
    ".agent-tmp",
}

BACKUP_FILE_PATTERNS = (
    re.compile(r"^(AGENTS|CLAUDE|GEMINI)\.md\.(bak|backup)(?:[-._].*)?$"),
    re.compile(
        r"^(config\.toml|settings\.json|hooks\.json|\.codex-global-state\.json|\.claude\.json)\.(bak|backup)(?:[-._].*)?$"
    ),
)

EPHEMERAL_SIM_RE = re.compile(
    r"^(?:agent[-_ ]?tmp\b|ephemeral[-_ ]|scratch[-_ ]sim\b|temp[-_ ]sim\b|\[agent-tmp\])"
    r"|(?:[-_ ](?:agent[-_]?tmp|ephemeral[-_]?sim|temp[-_]?sim))$",
    re.IGNORECASE,
)

# Exact naming used by `agent-test-device` (scripts/test_device.py): agent-tmp-job-<pid>-<uuid4 hex>.
# Only these are reclaimed by default; EPHEMERAL_SIM_RE is opt-in via `gc --broad-simulator-names`.
MANAGED_SIM_RE = re.compile(r"^agent-tmp-job-([0-9]+)-[a-f0-9]{32}$")

# `.git` entries below these directories are package-manager checkouts (SwiftPM), not user repositories.
DEPENDENCY_CHECKOUT_DIRS = frozenset({"SourcePackages", ".build"})

FENCED_CODE_RE = re.compile(r"(?ms)^[ \t]*(?:```|~~~).*?^[ \t]*(?:```|~~~)[^\n]*\n?")
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
MD_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
UNRESOLVED_TOKEN_RE = re.compile(r"(\{\{[A-Za-z0-9_.-]+\}\}|\$\{[A-Za-z0-9_]+\})")

# Phrases that only belong in the global rule file. They occur in both shipped templates
# (templates/AGENTS.md.tmpl and templates/AGENTS.zh-CN.md.tmpl); a project AGENTS.md repeating them
# duplicates global rules. Command literals are language-neutral; the last two are the section
# headings of the English and Chinese template respectively.
GLOBAL_DUPLICATE_PHRASES = (
    "git reset --hard",
    "git clean -fdx",
    "git worktree prune",
    "agent-ship --no-merge",
    "One writer per directory",
    "一写一树",
)

GC_MODE_DEFAULTS: dict[str, dict[str, float]] = {
    "daily": {
        "scratch_ttl_days": 3.0,
        "tests_ttl_days": 7.0,
        "cache_ttl_days": 7.0,
        "tmp_ttl_hours": 24.0,
        "sim_ttl_days": 3.0,
        "derived_data_ttl_hours": 12.0,
        "system_derived_data_ttl_hours": 7 * 24.0,
        "min_idle_hours": 2.0,
    },
    "deep": {
        "scratch_ttl_days": 1.0,
        "tests_ttl_days": 3.0,
        "cache_ttl_days": 3.0,
        "tmp_ttl_hours": 6.0,
        "sim_ttl_days": 1.0,
        "derived_data_ttl_hours": 4.0,
        "system_derived_data_ttl_hours": 24.0,
        "min_idle_hours": 0.5,
    },
}
GC_MODE_DEFAULTS["session-end"] = {**GC_MODE_DEFAULTS["daily"], "min_idle_hours": 0.05}

WORK_README_CONTENT = """# `work/` — Three-Tier Agent Workspace Lifecycle Contract

This directory is excluded via `.git/info/exclude` (never committed to Git, yet searchable by local agents) and shielded from macOS Spotlight via `.metadata_never_index`.

| Subdirectory | Purpose | Default Retention (TTL) |
| :--- | :--- | :--- |
| `_scratch/<YYYYMMDD>-<agent>-<topic>/` | Throwaway probes, debug logs, screenshots, API dumps | **Auto-reclaimed after 3 days** (into `~/.Trash` with receipt) |
| `_tests/<YYYYMMDD>-<agent>-<topic>/` | Ephemeral test outputs, beta install bundles, `.xcresult` | **Auto-reclaimed after 7 days** (into `~/.Trash` with receipt) |
| `retained/<topic>/` | Long-lived datasets, firmware baselines, reference media | **Protected forever** (never auto-cleaned; `.keep`/`.pinned` supported) |

> **Grandfathering Rule**: Legacy directories directly under `work/<name>` (outside `_scratch/` and `_tests/`) are treated as protected historical data and are **never** deleted automatically. Run `agent-governor triage` to inspect them.
"""


# ==============================================================================
# Environment, Traversal & Formatting Helpers
# ==============================================================================
def _parse_env_value(raw: str) -> str:
    """Shell-like value parsing: quoted values keep their content, unquoted values drop a trailing ` # comment`."""
    v = raw.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        return v[1:end] if end != -1 else v[1:]
    return re.split(r"\s#", v, maxsplit=1)[0].strip()


def _read_env_file(path: Path, home: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines() if path.is_file() else []
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = _parse_env_value(v).replace("${HOME}", str(home)).replace("$HOME", str(home))
        if v:
            out[k.strip()] = v
    return out


def load_orca_env(home: Path | None = None) -> dict[str, str]:
    real_home = Path.home()
    h = home or real_home
    cfg: dict[str, str] = {}
    if h != real_home:
        # Preserve host primary/secondary simulator UDID protection even when --home is overridden
        for rp in (real_home / ".orca/backup-config.env", real_home / ".orca/guard.env"):
            for rk, rv in _read_env_file(rp, real_home).items():
                if rk in ("PRIMARY_SIMULATOR_UDID", "SECONDARY_SIMULATOR_UDID"):
                    cfg[rk] = rv
    for p in (h / ".orca/backup-config.env", h / ".orca/guard.env"):
        cfg.update(_read_env_file(p, h))
    for k in (
        "STORAGE_MODE",
        "AI_VOLUME_PATH",
        "INTERNAL_WORKSPACE_PATH",
        "PRIMARY_SIMULATOR_UDID",
        "SECONDARY_SIMULATOR_UDID",
    ):
        if os.environ.get(k):
            cfg[k] = os.environ[k].strip()
    return cfg


def resolve_projects_root(explicit_root: str | None = None, home: Path | None = None) -> Path:
    h = home or Path.home()
    if explicit_root:
        p = Path(explicit_root).expanduser().resolve()
        if (p / "AI/Projects").is_dir():
            return p / "AI/Projects"
        if (p / "Projects").is_dir() and p.name == "AI":
            return p / "Projects"
        return p
    cfg = load_orca_env(h)
    env_vol = os.environ.get("AI_VOLUME_PATH", "").strip()
    storage_mode = cfg.get("STORAGE_MODE", "").strip().lower()
    if env_vol:
        vol = env_vol
    elif storage_mode == "internal":
        if cfg.get("INTERNAL_WORKSPACE_PATH"):
            vol = cfg["INTERNAL_WORKSPACE_PATH"]
        elif cfg.get("AI_VOLUME_PATH") and not cfg["AI_VOLUME_PATH"].startswith("/Volumes/"):
            vol = cfg["AI_VOLUME_PATH"]
        else:
            vol = str(h / "AI-Workspace")
    else:
        vol = cfg.get("AI_VOLUME_PATH") or cfg.get("INTERNAL_WORKSPACE_PATH") or str(h / "AI-Workspace")
    cand = Path(vol).expanduser() / "AI/Projects"
    if cand.is_dir():
        return cand
    fallback = h / "AI-Workspace/AI/Projects"
    if fallback.is_dir():
        return fallback
    return Path.cwd()


def resolve_ai_root(projects_root: Path) -> Path | None:
    if projects_root.name == "Projects" and projects_root.parent.name == "AI":
        return projects_root.parent
    return None


def discover_worktree_checkouts(ai_root: Path | None, max_depth: int = 4) -> list[Path]:
    """Discover real Git worktree checkouts across depth 1..max_depth under {Orca,Codex,Claude}/Worktrees."""
    if not ai_root or not ai_root.is_dir():
        return []
    found: list[Path] = []
    seen: set[str] = set()
    for agent_wt in (ai_root / "Orca/Worktrees", ai_root / "Codex/Worktrees", ai_root / "Claude/Worktrees"):
        if not agent_wt.is_dir() or agent_wt.is_symlink():
            continue
        stack: list[tuple[Path, int]] = [(agent_wt, 0)]
        while stack:
            cur, depth = stack.pop()
            if depth >= max_depth:
                continue
            try:
                with os.scandir(cur) as it:
                    children = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for entry in children:
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                cpath = Path(entry.path)
                if (cpath / ".git").exists():
                    key = str(cpath)
                    if key not in seen:
                        seen.add(key)
                        found.append(cpath)
                    # Do not descend inside a discovered Git checkout
                    continue
                stack.append((cpath, depth + 1))
    found.sort()
    return found


def estimate_tokens(text: str) -> int:
    """Fast deterministic token estimator for mixed English/Code/CJK markdown."""
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii = len(text) - ascii_chars
    return max(1, int(round(ascii_chars / 4.0 + non_ascii * 1.3)))


def strip_markdown_code(text: str) -> str:
    """Strip fenced code blocks and inline code spans before link/placeholder lint checks."""
    without_fenced = FENCED_CODE_RE.sub("\n", text)
    return INLINE_CODE_RE.sub("", without_fenced)


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value.strip(), flags=re.UNICODE)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-._")
    return cleaned[:64] or "task"


@dataclass
class TreeInspection:
    total_bytes: int
    latest_activity: float
    latest_mtime: float
    file_count: int
    non_lease_files: int
    had_error: bool
    has_mount_or_xdev: bool
    has_nested_pin: bool
    has_nested_git: bool


def first_activity_after(path: Path, cutoff: float) -> float:
    """The first mtime/ctime newer than `cutoff` found under `path`, or 0.0 when there is none.

    Stops at the first hit, so an entry that is still in use costs a few stat calls instead of a full
    walk. Unreadable folders, symlinks and other file systems are skipped here: 0.0 only means "nothing
    recent was seen", and the full `inspect_tree` that follows still fails closed on them."""
    try:
        st0 = path.lstat()
    except OSError:
        return 0.0
    act0 = max(float(st0.st_mtime), float(st0.st_ctime))
    if act0 > cutoff:
        return act0
    if not stat.S_ISDIR(st0.st_mode):
        return 0.0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        est = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    act = max(float(est.st_mtime), float(est.st_ctime))
                    if act > cutoff:
                        return act
                    if stat.S_ISDIR(est.st_mode) and est.st_dev == st0.st_dev:
                        stack.append(Path(entry.path))
        except OSError:
            continue
    return 0.0


def inspect_tree(path: Path) -> TreeInspection:
    """Single-pass non-symlink `os.scandir` inspection with fail-closed error, mountpoint, pin & nested-Git detection."""
    try:
        st0 = path.lstat()
    except OSError:
        return TreeInspection(0, 0.0, 0.0, 0, 0, True, False, False, False)

    act0 = max(float(st0.st_mtime), float(st0.st_ctime))
    if path.is_symlink() or not path.is_dir():
        non_lease = 0 if path.name in {".agent-lease.json", ".DS_Store"} else 1
        return TreeInspection(
            st0.st_size,
            act0,
            float(st0.st_mtime),
            1,
            non_lease,
            False,
            False,
            path.name in PIN_MARKERS,
            path.name == ".git",
        )

    root_dev = st0.st_dev
    total = 0
    latest_act = act0
    latest_mt = float(st0.st_mtime)
    files = 0
    non_lease = 0
    had_error = False
    has_mount_or_xdev = os.path.ismount(path)
    has_nested_pin = any((path / m).exists() for m in PIN_MARKERS)
    has_nested_git = (path / ".git").exists()

    # (directory, inside a package-manager checkout cache such as SourcePackages/ or .build/)
    stack: list[tuple[Path, bool]] = [(path, False)]
    while stack:
        cur, in_dep_cache = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    ename = entry.name
                    if ename in PIN_MARKERS:
                        has_nested_pin = True
                    if ename == ".git" and not in_dep_cache:
                        has_nested_git = True
                    try:
                        est = entry.stat(follow_symlinks=False)
                    except OSError:
                        had_error = True
                        continue
                    e_act = max(float(est.st_mtime), float(est.st_ctime))
                    if e_act > latest_act:
                        latest_act = e_act
                    if est.st_mtime > latest_mt:
                        latest_mt = float(est.st_mtime)
                    if est.st_dev != root_dev:
                        has_mount_or_xdev = True
                    if entry.is_symlink():
                        total += est.st_size
                        files += 1
                        if ename not in {".agent-lease.json", ".DS_Store"}:
                            non_lease += 1
                    elif entry.is_dir(follow_symlinks=False):
                        ep = Path(entry.path)
                        if os.path.ismount(ep):
                            has_mount_or_xdev = True
                        else:
                            stack.append((ep, in_dep_cache or ename in DEPENDENCY_CHECKOUT_DIRS))
                    else:
                        total += est.st_size
                        files += 1
                        if ename not in {".agent-lease.json", ".DS_Store"}:
                            non_lease += 1
        except OSError:
            had_error = True

    return TreeInspection(
        total_bytes=total,
        latest_activity=latest_act,
        latest_mtime=latest_mt,
        file_count=files,
        non_lease_files=non_lease,
        had_error=had_error,
        has_mount_or_xdev=has_mount_or_xdev,
        has_nested_pin=has_nested_pin,
        has_nested_git=has_nested_git,
    )


def dir_stats(path: Path) -> tuple[int, float, int]:
    """Iterative `os.scandir` traversal returning (total_bytes, latest_activity_ts, file_count) without following symlinks."""
    ti = inspect_tree(path)
    return ti.total_bytes, ti.latest_activity, ti.file_count


def format_bytes(num: int) -> str:
    val = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(val) < 1024.0 or unit == "TiB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} TiB"


# ==============================================================================
# Process Liveness, Open-FD (`lsof`), Concurrency Lock & Git Safety Helpers
# ==============================================================================
@contextmanager
def acquire_gc_lock(
    home: Path,
    non_blocking: bool = False,
    timeout_s: float = 15.0,
    error_out: list[str] | None = None,
) -> Iterator[bool]:
    """Serialize GC mutations across simultaneous SessionEnd hooks and scheduled runs (fail-closed on lock I/O error)."""
    lock_dir = home / ".orca/locks"
    lock_file = lock_dir / "agent-governor-gc.lock"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        fh = lock_file.open("w", encoding="utf-8")
    except OSError as exc:
        if error_out is not None:
            error_out.append(f"lock open failed ({lock_file}): {exc}")
        yield False
        return

    acquired = False
    deadline = time.monotonic() + max(0.0, timeout_s)
    contention_errnos = {errno.EACCES, errno.EAGAIN, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)}
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in contention_errnos:
                    if error_out is not None:
                        error_out.append(f"flock failed ({lock_file}): {exc}")
                    acquired = False
                    break
                if non_blocking or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def _ps_env() -> dict[str, str]:
    # `lstart` is locale-dependent; pin the C locale so recorded and observed start times compare exactly.
    return {**os.environ, "LC_ALL": "C", "LANG": "C"}


def _join_lstart(tokens: list[str]) -> str:
    """`ps -o lstart` under LC_ALL=C is five tokens (`Mon Nov  3 12:56:40 2026`); normalise spacing."""
    return " ".join(tokens[:5])


def snapshot_running_processes() -> tuple[dict[int, str], list[str]]:
    """Return ({pid: lstart}, [command lines]) for every visible process except this one (empty on failure)."""
    pids: dict[int, str] = {}
    cmds: list[str] = []
    try:
        r = subprocess.run(
            ["/bin/ps", "-axo", "pid=,lstart=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            env=_ps_env(),
        )
        if r.returncode == 0:
            my_pid = os.getpid()
            for line in r.stdout.splitlines():
                parts = line.split(None, 6)
                if len(parts) < 6 or not parts[0].isdigit():
                    continue
                pid = int(parts[0])
                if pid == my_pid:
                    continue
                pids[pid] = _join_lstart(parts[1:6])
                if len(parts) > 6:
                    cmds.append(parts[6])
    except Exception:
        pass
    return pids, cmds


def process_table() -> dict[int, tuple[int, str, str]]:
    """{pid: (ppid, lstart, comm)} from a single `ps` call (empty on failure)."""
    table: dict[int, tuple[int, str, str]] = {}
    try:
        r = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,lstart=,comm="],
            capture_output=True,
            text=True,
            timeout=5,
            env=_ps_env(),
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                parts = line.split(None, 7)
                if len(parts) < 7 or not parts[0].isdigit() or not parts[1].isdigit():
                    continue
                table[int(parts[0])] = (int(parts[1]), _join_lstart(parts[2:7]), parts[7] if len(parts) > 7 else "")
    except Exception:
        pass
    return table


_GUI_HOST_COMMS = ("Orca", "Terminal", "iTerm2", "WindowServer", "launchd", "login", "tmux", "sshd")


def _is_gui_host(comm: str) -> bool:
    base = os.path.basename(comm)
    return any(base == h or base.startswith(h + ".") or f"/{h}.app/" in comm for h in _GUI_HOST_COMMS)


def collect_ancestor_pids(
    start_pid: int | None,
    max_hops: int = 8,
    table: dict[int, tuple[int, str, str]] | None = None,
) -> list[int]:
    """Walk parent PIDs above transient subshells up to the owning agent/shell session (stopping before GUI hosts)."""
    if not start_pid or start_pid <= 1:
        return []
    procs = table if table is not None else process_table()
    chain: list[int] = [int(start_pid)]
    cur = int(start_pid)
    for _ in range(max_hops):
        info = procs.get(cur)
        if info is None:
            break
        ppid = info[0]
        parent = procs.get(ppid)
        if ppid <= 1 or ppid in chain or parent is None or _is_gui_host(parent[2]):
            break
        chain.append(ppid)
        cur = ppid
    return chain


def pid_alive(pid: int | None, live_pids: set[int] | dict[int, str] | None = None) -> bool:
    if not pid or pid <= 1:
        return False
    if live_pids is not None:
        return pid in live_pids
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def process_lstart(pid: int) -> str | None:
    """Start time of a running `pid` (C locale); "" if alive but unreadable, None if not running."""
    if not pid_alive(pid):
        return None
    try:
        r = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            env=_ps_env(),
        )
        toks = r.stdout.split()
        if r.returncode == 0 and len(toks) >= 5:
            return _join_lstart(toks)
    except Exception:
        pass
    return ""


def lease_owners(lease: dict[str, Any]) -> list[tuple[int, str]]:
    """(pid, lstart) owner pairs; legacy leases without `owners` yield PIDs with an unknown ("") start time."""
    owners: list[tuple[int, str]] = []
    raw = lease.get("owners")
    if isinstance(raw, list):
        for o in raw:
            if isinstance(o, (list, tuple)) and len(o) == 2 and isinstance(o[0], int) and o[0] > 1:
                owners.append((o[0], str(o[1] or "")))
    if owners:
        return owners
    legacy: list[int] = []
    p = lease.get("pid")
    if isinstance(p, int) and p > 1:
        legacy.append(p)
    anc = lease.get("ancestor_pids")
    if isinstance(anc, list):
        legacy.extend(ap for ap in anc if isinstance(ap, int) and ap > 1 and ap not in legacy)
    return [(pid, "") for pid in legacy]


def lease_has_live_owner(
    lease: dict[str, Any],
    live_pids: set[int] | dict[int, str] | None = None,
    ignore_pids: frozenset[int] | set[int] = frozenset(),
) -> bool:
    """True if a recorded owner is still running as the *same* process (start time must match when recorded)."""
    for pid, lstart in lease_owners(lease):
        if pid in ignore_pids or not pid_alive(pid, live_pids):
            continue
        if not lstart:
            return True
        cur = live_pids.get(pid, "") if isinstance(live_pids, dict) else process_lstart(pid)
        if cur is not None and (not cur or cur == lstart):
            return True
    return False


def path_used_by_processes(path: Path, cmds: list[str]) -> bool:
    needle = str(path)
    return any(needle in c for c in cmds)


_VALID_LSOF_FIELD_TAGS = frozenset({"p", "f", "a", "t", "d", "i", "L", "u", "c", "g", "R", "n", "k", "m"})


def _parse_lsof_fn_output(r: subprocess.CompletedProcess[str]) -> list[str] | None:
    """Parse `lsof -n -P -Fn` output; return None on non-(0,1) exit, stderr output, or truncated/corrupt output."""
    if r.returncode not in (0, 1):
        return None
    if (r.stderr or "").strip():
        return None
    out = r.stdout or ""
    if not out:
        return [] if r.returncode == 1 else None
    if not out.endswith("\n"):
        return None
    names: list[str] = []
    saw_pid = False
    for line in out.splitlines():
        if not line:
            continue
        tag = line[0]
        if tag == "p" and line[1:].isdigit():
            saw_pid = True
        elif tag == "n" and len(line) > 1:
            names.append(line[1:])
        elif tag not in _VALID_LSOF_FIELD_TAGS:
            return None
    if not saw_pid or not names:
        return None
    return names


class OpenFileSnapshot:
    """One system-wide `lsof -Fn` listing of open files and cwds, matched by path prefix (fail-closed)."""

    def __init__(self, names: list[str] | None) -> None:
        self.names = sorted(set(names)) if names is not None else None
        self.taken_at = time.monotonic()

    @classmethod
    def capture(cls) -> "OpenFileSnapshot":
        lsof_bin = "/usr/sbin/lsof" if Path("/usr/sbin/lsof").is_file() else shutil.which("lsof")
        names: list[str] | None = None
        if lsof_bin:
            try:
                r = subprocess.run([lsof_bin, "-n", "-P", "-w", "-Fn"], capture_output=True, text=True, timeout=30)
                names = _parse_lsof_fn_output(r)
            except Exception:
                names = None
        return cls(names)

    def _has_prefix(self, prefix: str) -> bool:
        names = self.names or []
        i = bisect.bisect_left(names, prefix)
        if i < len(names) and names[i] == prefix:
            return True
        sub = prefix.rstrip("/") + "/"
        j = bisect.bisect_left(names, sub)
        return j < len(names) and names[j].startswith(sub)

    def is_busy(self, path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if self.names is None:
            return True
        raw_s = str(path)
        try:
            res_s = str(path.resolve())
        except OSError:
            res_s = raw_s
        return self._has_prefix(raw_s) or (res_s != raw_s and self._has_prefix(res_s))


def find_lsof_busy_paths(paths: list[Path]) -> set[Path]:
    """Paths that are, or contain, a file/cwd held open by any process (all paths if `lsof` fails)."""
    if not paths:
        return set()
    snap = OpenFileSnapshot.capture()
    return {p for p in paths if snap.is_busy(p)}


def is_git_root(project_dir: Path) -> bool:
    return (project_dir / ".git").exists()


def resolve_git_common_exclude(project_dir: Path) -> Path | None:
    dot_git = project_dir / ".git"
    if dot_git.is_dir():
        return dot_git / "info/exclude"
    if dot_git.is_file():
        try:
            raw = dot_git.read_text(encoding="utf-8", errors="ignore").strip()
            if raw.startswith("gitdir:"):
                gitdir = Path(raw.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = (project_dir / gitdir).resolve()
                commondir_file = gitdir / "commondir"
                if commondir_file.is_file():
                    rel = commondir_file.read_text(encoding="utf-8", errors="ignore").strip()
                    common = (gitdir / rel).resolve()
                    return common / "info/exclude"
                return gitdir / "info/exclude"
        except OSError:
            return None
    return None


def ensure_git_info_exclude(project_dir: Path, entry_pattern: str, apply: bool = True) -> bool:
    """Ensure `entry_pattern` (e.g. `/work/`, `/CLAUDE.md`) is present in `.git/info/exclude`."""
    if not is_git_root(project_dir):
        return False
    ex = resolve_git_common_exclude(project_dir)
    if not ex:
        return False
    existing = ex.read_text(encoding="utf-8", errors="ignore") if ex.is_file() else ""
    bare = entry_pattern.strip("/")
    regex = re.compile(rf"(?m)^/?{re.escape(bare)}/?$")
    if regex.search(existing):
        return False
    if apply:
        ex.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        with ex.open("a", encoding="utf-8") as fh:
            fh.write(f"{prefix}{entry_pattern}\n")
    return True


_GIT_BASE_CMD = ("git", "-c", "core.fsmonitor=false")


def is_git_tracked_or_ignored(project_dir: Path, rel_name: str) -> bool:
    if not is_git_root(project_dir):
        return True
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        tr = subprocess.run(
            [*_GIT_BASE_CMD, "-C", str(project_dir), "ls-files", "--error-unmatch", "--", rel_name],
            capture_output=True,
            text=True,
            timeout=3,
            env=env,
        )
        if tr.returncode == 0:
            return True
        ig = subprocess.run(
            [*_GIT_BASE_CMD, "-C", str(project_dir), "check-ignore", "-q", "--", rel_name],
            capture_output=True,
            text=True,
            timeout=3,
            env=env,
        )
        return ig.returncode == 0
    except Exception:
        return False


class GitTrackedIndex:
    """Per-repository batched `git ls-files` cache (fail-closed if Git errors inside a repository)."""

    def __init__(self) -> None:
        self._cache: dict[str, set[str] | None] = {}
        self._dirs: dict[str, set[str]] = {}

    def _find_repo_root(self, target: Path) -> Path | None:
        cur = target if target.is_dir() else target.parent
        for cand in (cur, *cur.parents):
            if (cand / ".git").exists():
                return cand
        return None

    def _load_repo_files(self, repo_root: Path) -> set[str] | None:
        key = str(repo_root)
        if key in self._cache:
            return self._cache[key]
        try:
            r = subprocess.run(
                [*_GIT_BASE_CMD, "-C", key, "ls-files", "-z"],
                capture_output=True,
                timeout=6,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            )
            if r.returncode != 0:
                self._cache[key] = None
                return None
            files = {p.decode("utf-8", errors="ignore") for p in r.stdout.split(b"\0") if p}
            self._cache[key] = files
            return files
        except Exception:
            self._cache[key] = None
            return None

    def has_tracked(self, target: Path) -> bool:
        repo = self._find_repo_root(target)
        if not repo:
            return False
        tracked = self._load_repo_files(repo)
        if tracked is not None:
            try:
                rel = target.relative_to(repo).as_posix()
            except ValueError:
                rel = ""
            if rel in ("", "."):
                return bool(tracked)
            if rel in tracked:
                return True
            key = str(repo)
            dirs = self._dirs.get(key)
            if dirs is None:
                # Every ancestor directory of every tracked path, so lookups are O(1) instead of a full scan
                dirs = set()
                for f in tracked:
                    cut = f.rfind("/")
                    while cut > 0 and f[:cut] not in dirs:
                        dirs.add(f[:cut])
                        cut = f.rfind("/", 0, cut)
                self._dirs[key] = dirs
            return rel.rstrip("/") in dirs
        # Fallback direct check; fail-closed (return True) if Git fails inside an existing repo
        return has_git_tracked_files(target, fail_closed=True)


def has_git_tracked_files(target: Path, fail_closed: bool = True) -> bool:
    parent = target if target.is_dir() else target.parent
    in_repo = any((c / ".git").exists() for c in (parent, *parent.parents))
    try:
        r = subprocess.run(
            [*_GIT_BASE_CMD, "-C", str(parent), "ls-files", "--", str(target)],
            capture_output=True,
            text=True,
            timeout=4,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        if r.returncode != 0:
            return bool(fail_closed and in_repo)
        return bool(r.stdout.strip())
    except Exception:
        return bool(fail_closed and in_repo)


# ==============================================================================
# Reversible Trash Staging Engine (Zero `rm -rf`, Same-Volume Aware)
# ==============================================================================
def _find_mount_root(path: Path) -> Path:
    cur = path.parent if path.is_symlink() else path
    for anc in (cur, *cur.parents):
        if os.path.ismount(anc):
            return anc
    return Path("/")


def _append_wal_record(journal_path: Path, record: dict[str, Any]) -> None:
    """Append one fsynced JSONL record to the Write-Ahead Log before/after filesystem rename."""
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(str(journal_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(target_path: Path, payload: dict[str, Any]) -> None:
    """Atomically write fsynced JSON via sibling temp file + `os.replace`."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.parent / f".{target_path.name}.tmp-{os.getpid()}-{time.time_ns()}"
    raw = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp_path), str(target_path))


class TrashStager:
    def __init__(
        self,
        trash_root: Path | None = None,
        run_label: str = "gc",
        default_home: Path | None = None,
        manifest_extra: dict[str, Any] | None = None,
    ):
        self._explicit_trash_root = trash_root is not None
        self._base = trash_root or ((default_home or Path.home()) / ".Trash")
        self._day = datetime.now().strftime("%Y-%m-%d")
        stamp = datetime.now().strftime("%H%M%S") + f"-{os.getpid()}-{time.time_ns() % 100000:05d}"
        self._run_id = f"{run_label}-{stamp}"
        self.batch_dir = self._base / f"agent-governor-{self._day}" / self._run_id
        self.items_dir = self.batch_dir / "items"
        self.journal_path = self.batch_dir / "journal.jsonl"
        self.manifest_path = self.batch_dir / "manifest.json"
        self._manifest_extra: dict[str, Any] = dict(manifest_extra or {})
        self._seq = 0
        self.records: list[dict[str, Any]] = []

    def _base_dev(self) -> int | None:
        for anc in (self._base, *self._base.parents):
            try:
                if anc.exists():
                    return anc.stat().st_dev
            except OSError:
                continue
        return None

    def _target_items_dir(self, path: Path) -> Path | None:
        try:
            path_dev = path.lstat().st_dev
        except OSError:
            return None

        base_dev = self._base_dev()
        if base_dev is not None and base_dev == path_dev:
            return self.items_dir

        if not self._explicit_trash_root:
            mount_root = _find_mount_root(path)
            vol_trash = mount_root / ".Trashes" / str(os.getuid())
            try:
                if vol_trash.is_dir() and os.access(vol_trash, os.W_OK) and vol_trash.stat().st_dev == path_dev:
                    return vol_trash / f"agent-governor-{self._day}" / self._run_id / "items"
            except OSError:
                pass
        return None

    def can_stage_same_device(self, path: Path) -> bool:
        return self._target_items_dir(path) is not None

    def stage(
        self,
        path: Path,
        reason: str,
        category: str,
        size_bytes: int = 0,
        forbid_pin_or_git: bool = False,
        inspection: TreeInspection | None = None,
    ) -> Path:
        """Move `path` into this batch's Trash items dir; `inspection` may reuse a walk taken just before."""
        st0 = path.lstat()
        if path.is_dir() and not path.is_symlink():
            ti = inspection or inspect_tree(path)
            if ti.has_mount_or_xdev:
                raise OSError(errno.EXDEV, f"Refusing to stage directory containing mountpoint/subvolume: {path}")
            if ti.had_error:
                raise OSError(errno.EACCES, f"Refusing to stage unreadable subtree: {path}")
            if forbid_pin_or_git and (ti.has_nested_pin or has_pin_marker(path)):
                raise OSError(errno.EPERM, f"Refusing to stage pinned directory: {path}")
            if forbid_pin_or_git and ti.has_nested_git:
                raise OSError(errno.EPERM, f"Refusing to stage directory containing nested Git repo: {path}")

        items_dir = self._target_items_dir(path)
        if items_dir is None:
            raise OSError(
                errno.EXDEV,
                f"Refusing cross-device Trash copy+rmtree for {path}; no writable same-volume Trash available",
            )
        items_dir.mkdir(parents=True, exist_ok=True)
        st_check = path.lstat()
        if st_check.st_dev != items_dir.stat().st_dev:
            raise OSError(errno.EXDEV, f"Cross-device mismatch staging {path} -> {items_dir}")
        if st_check.st_ino != st0.st_ino or st_check.st_dev != st0.st_dev:
            raise OSError(errno.EBUSY, f"Source inode changed concurrently before staging: {path}")

        idx = self._seq + 1
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name)[:80] or "item"
        dest = items_dir / f"{idx:04d}_{safe_name}"
        seq = 2
        while dest.exists() or dest.is_symlink():
            dest = items_dir / f"{idx:04d}_{seq}_{safe_name}"
            seq += 1
        self._wal_rename(path, dest, st0, category, reason, size_bytes)
        return dest

    def relocate(self, original: Path, relocated_to: Path, reason: str, size_bytes: int = 0) -> None:
        """Atomically relocate root clutter into `work/retained/` with WAL durability before & after rename."""
        st0 = original.lstat()
        relocated_to.parent.mkdir(parents=True, exist_ok=True)
        self._wal_rename(original, relocated_to, st0, "relocated_root_clutter", reason, size_bytes)

    def _wal_rename(
        self, src: Path, dest: Path, st0: os.stat_result, category: str, reason: str, size_bytes: int
    ) -> None:
        """Fsynced `planned` WAL record, atomic same-filesystem rename, then `staged` record + manifest flush."""
        self._seq += 1
        idx = self._seq
        base = {
            "seq": idx,
            "category": category,
            "reason": reason,
            "original_path": str(src),
            "staged_path": str(dest),
            "size_bytes": size_bytes,
            "src_dev": int(st0.st_dev),
            "src_ino": int(st0.st_ino),
        }
        _append_wal_record(
            self.journal_path, {**base, "phase": "planned", "planned_at": datetime.now(timezone.utc).isoformat()}
        )
        try:
            os.rename(str(src), str(dest))
        except OSError as exc:
            _append_wal_record(
                self.journal_path,
                {
                    "seq": idx,
                    "phase": "aborted",
                    "original_path": str(src),
                    "staged_path": str(dest),
                    "error": str(exc),
                    "aborted_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            raise
        rec = {"index": idx, **base, "staged_at": datetime.now(timezone.utc).isoformat()}
        self.records.append(rec)
        _append_wal_record(self.journal_path, {"phase": "staged", **rec})
        self.write_manifest()

    def record_external_action(self, action_type: str, target: str, reason: str, metadata: dict[str, Any]) -> None:
        self.batch_dir.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        idx = self._seq
        rec = {
            "index": idx,
            "seq": idx,
            "category": action_type,
            "reason": reason,
            "original_path": target,
            "staged_path": None,
            "size_bytes": 0,
            "metadata": metadata,
            "staged_at": datetime.now(timezone.utc).isoformat(),
        }
        self.records.append(rec)
        _append_wal_record(self.journal_path, {"phase": "staged", **rec})
        self.write_manifest()

    def write_manifest(self, extra: dict[str, Any] | None = None) -> Path | None:
        if extra:
            self._manifest_extra.update(extra)
        if not self.records:
            return None
        payload = {
            "schema": "agent-governor-manifest/v1.3",
            "version": VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "total_items": len(self.records),
            "total_bytes": sum(int(r.get("size_bytes", 0)) for r in self.records),
            **self._manifest_extra,
            "items": self.records,
        }
        _atomic_write_json(self.manifest_path, payload)
        return self.manifest_path


# ==============================================================================
# 1. AUDIT: Context Budget, Config Smells & 4-Zone File Governance
# ==============================================================================
def parse_skill_frontmatter(skill_md: Path) -> dict[str, Any]:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {"valid": False, "frontmatter_bytes": 0, "description_chars": 0, "body_lines": 0}
    total_lines = len(text.splitlines())
    if not text.startswith("---\n"):
        return {"valid": False, "frontmatter_bytes": 0, "description_chars": 0, "body_lines": total_lines}
    end = text.find("\n---", 4)
    if end == -1:
        return {"valid": False, "frontmatter_bytes": 0, "description_chars": 0, "body_lines": total_lines}
    fm = text[4:end]
    lines = fm.splitlines()
    desc_parts: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("description:"):
            val = line.split(":", 1)[1].strip()
            block_style = val in (">", ">-", ">+", "|", "|-", "|+")
            if val and not block_style:
                desc_parts.append(val.strip('"').strip("'"))
            i += 1
            while i < len(lines) and (lines[i].startswith("  ") or lines[i].startswith("\t") or not lines[i].strip()):
                s = lines[i].strip()
                if s:
                    desc_parts.append(s)
                elif block_style and lines[i] == "":
                    desc_parts.append("")
                i += 1
            break
        i += 1
    joiner = "\n" if ("|" in fm[:60]) else " "
    desc = joiner.join(p for p in desc_parts if p != "").strip()
    fm_bytes = len(fm.encode("utf-8"))
    return {
        "valid": True,
        "frontmatter_bytes": fm_bytes,
        "estimated_tokens": estimate_tokens(fm),
        "description_chars": len(desc),
        "body_lines": total_lines,
    }


_MCP_SECTION_RE = re.compile(r'^\[\s*mcp_servers\.(?:"([^"]+)"|([A-Za-z0-9_-]+))\s*\]\s*(?:#.*)?$')


def parse_codex_mcp_servers(raw_cfg: str) -> dict[str, dict[str, Any]]:
    """`[mcp_servers.<name>]` tables of a Codex config.toml (tomllib on 3.11+, equivalent line parser on 3.9/3.10)."""
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        tomllib = None  # type: ignore[assignment]
    if tomllib is not None:
        try:
            servers = tomllib.loads(raw_cfg).get("mcp_servers")
            return {k: v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}
        except Exception:
            pass
    parsed: dict[str, dict[str, Any]] = {}
    cur: str | None = None
    for rline in raw_cfg.splitlines():
        sline = rline.strip()
        m_sec = _MCP_SECTION_RE.match(sline)
        if m_sec:
            cur = m_sec.group(1) or m_sec.group(2)
            parsed.setdefault(cur, {})
        elif sline.startswith("["):
            cur = None  # includes sub-tables such as [mcp_servers.<name>.env]
        elif cur:
            m_cmd = re.match(r'^command\s*=\s*["\']([^"\']+)["\']', sline)
            if m_cmd:
                parsed[cur]["command"] = m_cmd.group(1)
            m_en = re.match(r"^enabled\s*=\s*(true|false)\b", sline)
            if m_en:
                parsed[cur]["enabled"] = m_en.group(1) == "true"
    return parsed


def count_mcp_servers(home: Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    cj = home / ".claude.json"
    if cj.is_file():
        try:
            d = json.loads(cj.read_text(encoding="utf-8", errors="ignore"))
            mcp = d.get("mcpServers") or {}
            if isinstance(mcp, dict):
                counts["claude_global"] = len(mcp)
        except Exception:
            pass
    ct = home / ".codex/config.toml"
    if ct.is_file():
        try:
            counts["codex_global"] = len(parse_codex_mcp_servers(ct.read_text(encoding="utf-8", errors="ignore")))
        except OSError:
            pass
    return counts


def scan_root_clutter(root_dir: Path) -> list[dict[str, str]]:
    clutter: list[dict[str, str]] = []
    try:
        for child in sorted(root_dir.iterdir()):
            cname = child.name
            if cname in FORBIDDEN_ROOT_DIRS:
                clutter.append({"name": cname, "kind": "forbidden_ephemeral_dir"})
            elif cname.startswith("tmpclaude-"):
                clutter.append({"name": cname, "kind": "leaked_claude_cwd_file"})
            elif cname == ".DS_Store":
                clutter.append({"name": cname, "kind": "ds_store"})
            elif any(pat.match(cname) for pat in BACKUP_FILE_PATTERNS):
                clutter.append({"name": cname, "kind": "backup_file"})
    except OSError:
        pass
    return clutter


def audit_project_rules(
    proj: Path,
    max_lines: int = 40,
    max_bytes: int = 3000,
) -> dict[str, Any]:
    agents_md = proj / "AGENTS.md"
    claude_md = proj / "CLAUDE.md"
    gemini_md = proj / "GEMINI.md"
    issues: list[str] = []
    smells: list[str] = []

    info: dict[str, Any] = {
        "name": proj.name,
        "path": str(proj),
        "is_git": is_git_root(proj),
        "has_agents_md": agents_md.is_file(),
        "claude_pointer_ok": False,
        "gemini_pointer_ok": False,
        "agents_lines": 0,
        "agents_bytes": 0,
        "agents_tokens": 0,
        "missing_zones": [],
        "missing_work_tiers": [],
        "work_excluded_in_git": None,
        "work_in_gitignore_trap": False,
        "work_spotlight_shielded": (proj / "work/.metadata_never_index").is_file(),
        "root_clutter": [],
        "issues": issues,
        "smells": smells,
    }

    if agents_md.is_file():
        txt = agents_md.read_text(encoding="utf-8", errors="ignore")
        prose_txt = strip_markdown_code(txt)
        b_len = len(txt.encode("utf-8"))
        l_cnt = len(txt.splitlines())
        info["agents_lines"] = l_cnt
        info["agents_bytes"] = b_len
        info["agents_tokens"] = estimate_tokens(txt)
        if l_cnt > max_lines or b_len > max_bytes:
            smells.append(
                f"oversized_agents_md ({l_cnt} lines / {b_len} B > budget {max_lines}L/{max_bytes}B; "
                "move runbook details to docs/ for progressive disclosure)"
            )
        # Raw text: global rules quote their commands in `code`, which prose_txt strips
        dups = [p for p in GLOBAL_DUPLICATE_PHRASES if p in txt]
        if dups:
            smells.append(f"duplicates_global_rules ({', '.join(dups)})")
        unresolved = UNRESOLVED_TOKEN_RE.findall(prose_txt)
        if unresolved:
            issues.append(f"unresolved_placeholders ({', '.join(sorted(set(unresolved))[:3])})")
        for m in MD_LINK_RE.finditer(prose_txt):
            raw_target = m.group(1).strip().split("#", 1)[0].strip()
            if not raw_target or "://" in raw_target or raw_target.startswith("mailto:"):
                continue
            target_path = (proj / raw_target).resolve()
            if not target_path.exists():
                issues.append(f"broken_doc_link ({raw_target})")
    else:
        issues.append("missing_AGENTS.md")

    for label, ptr_file in (("claude", claude_md), ("gemini", gemini_md)):
        if not ptr_file.is_file():
            if agents_md.is_file():
                issues.append(f"missing_{ptr_file.name}")
        else:
            ptxt = ptr_file.read_text(encoding="utf-8", errors="ignore").strip()
            if ptxt == "@AGENTS.md":
                info[f"{label}_pointer_ok"] = True
            elif agents_md.is_file() and ptxt != agents_md.read_text(encoding="utf-8", errors="ignore").strip():
                smells.append(f"{ptr_file.name}_diverges_from_AGENTS.md (recommend `@AGENTS.md` pointer)")
            else:
                info[f"{label}_pointer_ok"] = True

    for z in FOUR_ZONES:
        if not (proj / z).is_dir():
            info["missing_zones"].append(z)
    if info["missing_zones"]:
        issues.append(f"missing_zones ({', '.join(info['missing_zones'])})")

    if (proj / "work").is_dir():
        for tier in WORK_TIERS:
            if not (proj / "work" / tier).is_dir():
                info["missing_work_tiers"].append(tier)
        if not info["work_spotlight_shielded"]:
            smells.append("missing_work_spotlight_shield (.metadata_never_index)")

    if info["is_git"]:
        ex = resolve_git_common_exclude(proj)
        excluded = False
        if ex and ex.is_file():
            ex_txt = ex.read_text(encoding="utf-8", errors="ignore")
            excluded = bool(re.search(r"(?m)^/?work/?$", ex_txt))
        info["work_excluded_in_git"] = excluded
        if not excluded:
            issues.append("work_not_in_git_info_exclude")
        gi = proj / ".gitignore"
        if gi.is_file():
            gi_txt = gi.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"(?m)^/?work/?$", gi_txt):
                info["work_in_gitignore_trap"] = True
                smells.append("work_in_gitignore (may block IDE/agent search indexers; prefer .git/info/exclude)")

    clutter = scan_root_clutter(proj)
    info["root_clutter"] = clutter
    for item in clutter:
        cname, kind = item["name"], item["kind"]
        if kind == "forbidden_ephemeral_dir":
            issues.append(f"root_clutter_dir ({cname}/)")
        elif kind == "leaked_claude_cwd_file":
            issues.append(f"leaked_temp_file ({cname})")
        elif kind == "backup_file":
            smells.append(f"root_backup_file ({cname})")

    return info


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else Path.home()
    projects_root = resolve_projects_root(args.root, home=home)

    # 1. Global rules parity & budget (including Orca Antigravity GEMINI.md when installed)
    global_files: dict[str, Path] = {
        "codex": home / ".codex/AGENTS.md",
        "claude": home / ".claude/CLAUDE.md",
        "gemini": home / ".gemini/GEMINI.md",
    }
    for extra_label, extra_rel in (
        ("gemini_agy", ".gemini/antigravity/GEMINI.md"),
        ("gemini_agy_cli", ".gemini/antigravity-cli/GEMINI.md"),
        ("orca_agy_chat", "Library/Application Support/orca/antigravity-chat/GEMINI.md"),
        ("orca_agy_acp", "Library/Application Support/orca/antigravity-chat/antigravity-acp/GEMINI.md"),
        ("orca_codex_rt", "Library/Application Support/orca/codex-runtime-home/home/AGENTS.md"),
    ):
        ep = home / extra_rel
        if ep.is_file():
            global_files[extra_label] = ep
    for idx, ca_md in enumerate(sorted((home / "Library/Application Support/orca/codex-accounts").glob("*/home/AGENTS.md")), 1):
        if ca_md.is_file():
            global_files[f"orca_codex_a{idx}"] = ca_md
    for idx, cp_md in enumerate(sorted((home / "Library/Application Support/orca/claude-profiles").glob("*/home/CLAUDE.md")), 1):
        if cp_md.is_file():
            global_files[f"orca_claude_p{idx}"] = cp_md

    global_report: dict[str, Any] = {}
    hashes: set[str] = set()
    for agent, gf in global_files.items():
        if gf.is_file():
            txt = gf.read_text(encoding="utf-8", errors="ignore")
            effective_txt = txt
            pointer_to: str | None = None
            stripped = txt.strip()
            if stripped.startswith("@") and "\n" not in stripped and len(stripped) > 1:
                raw_ref = stripped[1:].strip()
                if raw_ref.startswith("~/"):
                    ptr_target = (home / raw_ref[2:]).resolve()
                elif raw_ref.startswith("/"):
                    ptr_target = Path(raw_ref).resolve()
                else:
                    ptr_target = (gf.parent / raw_ref).resolve()
                if ptr_target.is_file():
                    effective_txt = ptr_target.read_text(encoding="utf-8", errors="ignore")
                    pointer_to = str(ptr_target)
            sha = hashlib.sha256(effective_txt.encode("utf-8")).hexdigest()[:12]
            hashes.add(sha)
            entry: dict[str, Any] = {
                "path": str(gf),
                "present": True,
                "lines": len(txt.splitlines()),
                "bytes": len(txt.encode("utf-8")),
                "estimated_tokens": estimate_tokens(txt),
                "sha256_12": sha,
            }
            if pointer_to is not None:
                entry["pointer_to"] = pointer_to
            global_report[agent] = entry
        else:
            global_report[agent] = {"path": str(gf), "present": False}

    # 2. Skills frontmatter & 500-line body budget
    skills_dir = home / ".agents/skills"
    skill_items: list[dict[str, Any]] = []
    if skills_dir.is_dir():
        for sd in sorted(skills_dir.iterdir()):
            if sd.name.startswith(".") or not (sd.is_dir() or sd.is_symlink()):
                continue
            smd = sd / "SKILL.md"
            if smd.is_file():
                meta = parse_skill_frontmatter(smd)
                skill_items.append({"skill": sd.name, **meta})
    total_skill_tokens = sum(int(s.get("estimated_tokens", 0)) for s in skill_items)
    heavy_skills = [s["skill"] for s in skill_items if int(s.get("description_chars", 0)) > 1200]
    oversized_skill_files = [
        f"{s['skill']} ({s['body_lines']}L)" for s in skill_items if int(s.get("body_lines", 0)) > 500
    ]

    # 3. MCP count & broken local MCP binary check + Claude memory bloat check
    mcp_counts = count_mcp_servers(home)
    broken_mcps: list[str] = []
    for cfg_p in [home / ".codex/config.toml"] + sorted(
        (home / "Library/Application Support/orca/codex-accounts").glob("*/home/config.toml")
    ):
        if not cfg_p.is_file():
            continue
        parsed_mcps = parse_codex_mcp_servers(cfg_p.read_text(encoding="utf-8", errors="ignore"))
        for mname, mspec in parsed_mcps.items():
            if isinstance(mspec, dict) and mspec.get("enabled", True) is not False:
                cmd = str(mspec.get("command") or "")
                if cmd.startswith(("/", "~/")) and not Path(cmd).expanduser().exists():
                    broken_mcps.append(f"{mname} ({cmd})")
    oversized_memories: list[str] = []
    for mem_md in sorted((home / ".claude/projects").glob("*/memory/*.md")) if (home / ".claude/projects").is_dir() else []:
        try:
            msz = mem_md.stat().st_size
            if msz > 15000:
                oversized_memories.append(f"{mem_md.parent.parent.name}/{mem_md.name} ({msz}B)")
        except OSError:
            pass

    # 4. Projects audit
    project_reports: list[dict[str, Any]] = []
    if projects_root.is_dir():
        candidates = sorted(
            p for p in projects_root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
        )
        if any((projects_root / z).is_dir() for z in FOUR_ZONES) and (projects_root / "AGENTS.md").is_file():
            candidates = [projects_root]
        for p in candidates:
            project_reports.append(
                audit_project_rules(p, max_lines=args.max_project_lines, max_bytes=args.max_project_bytes)
            )

    # 5. Active Worktrees root-clutter audit (across depth 1..4)
    ai_root = resolve_ai_root(projects_root)
    worktree_clutter_reports: list[dict[str, Any]] = []
    for wt in discover_worktree_checkouts(ai_root):
        wt_clutter = [
            c
            for c in scan_root_clutter(wt)
            if c["kind"] in ("forbidden_ephemeral_dir", "leaked_claude_cwd_file")
        ]
        if wt_clutter:
            rel_label = str(wt.relative_to(ai_root)) if ai_root and wt.is_relative_to(ai_root) else str(wt)
            worktree_clutter_reports.append({"worktree": rel_label, "path": str(wt), "clutter": wt_clutter})

    # 6. Summary aggregation
    attention: list[str] = []
    advice: list[str] = []

    if len(hashes) > 1:
        attention.append(
            "Global rule files (~/.codex/AGENTS.md, ~/.claude/CLAUDE.md, ~/.gemini/GEMINI.md, Orca Antigravity) have drifted out of sync."
        )
    seen_budget_shas: set[str] = set()
    for agent, ginfo in global_report.items():
        if ginfo.get("present") and (
            ginfo["lines"] > args.max_global_lines or ginfo["bytes"] > args.max_global_bytes
        ):
            sha = ginfo.get("sha256_12", agent)
            if sha not in seen_budget_shas:
                seen_budget_shas.add(sha)
                advice.append(
                    f"Global rule file ({ginfo['lines']}L / {ginfo['bytes']}B) exceeds lean startup budget "
                    f"({args.max_global_lines}L / {args.max_global_bytes}B)."
                )

    if broken_mcps:
        attention.append(f"Broken local MCP executable(s) missing on disk: {', '.join(sorted(set(broken_mcps)))}")
    if oversized_memories:
        advice.append(
            f"Oversized Claude memory file(s) >15KB ({', '.join(oversized_memories[:4])}); archive raw PR changelogs to reports/."
        )
    for mcp_label, cnt in mcp_counts.items():
        if cnt > 6:
            advice.append(
                f"{mcp_label} has {cnt} eager MCP servers (>6 increases startup token tax; scope per-project where possible)."
            )
    if heavy_skills:
        advice.append(
            f"{len(heavy_skills)} skill(s) have frontmatter descriptions >1200 chars ({', '.join(heavy_skills[:5])}); "
            "trim YAML descriptions so detailed docs load only on trigger."
        )
    if oversized_skill_files:
        advice.append(
            f"{len(oversized_skill_files)} skill(s) exceed 500-line SKILL.md progressive-disclosure cap "
            f"({', '.join(oversized_skill_files[:5])}); split deep references into references/."
        )

    for pr in project_reports:
        for iss in pr["issues"]:
            attention.append(f"[{pr['name']}] {iss}")
        if pr["missing_work_tiers"]:
            advice.append(f"[{pr['name']}] work/ missing lifecycle tiers: {', '.join(pr['missing_work_tiers'])}")
        for sm in pr["smells"]:
            advice.append(f"[{pr['name']}] {sm}")

    for wtr in worktree_clutter_reports:
        names = ", ".join(c["name"] for c in wtr["clutter"])
        attention.append(f"[worktree:{wtr['worktree']}] root clutter ({names})")

    unshielded_build_roots = ensure_build_spotlight_shields(home=home, projects_root=projects_root, apply=False)
    if unshielded_build_roots:
        labels = ", ".join(Path(x["path"]).parent.name for x in unshielded_build_roots[:4])
        advice.append(
            f"Missing `.metadata_never_index` Spotlight shield on {len(unshielded_build_roots)} build/cache root(s) "
            f"({labels}); run `agent-governor adopt --apply` or `gc --apply` to prevent `🚫` runner app indexing."
        )

    return {
        "schema": "agent-governor-audit/v1",
        "version": VERSION,
        "projects_root": str(projects_root),
        "global_rules": {
            "in_sync": len(hashes) <= 1,
            "files": global_report,
        },
        "skills_budget": {
            "installed_count": len(skill_items),
            "startup_frontmatter_tokens_est": total_skill_tokens,
            "oversized_descriptions": heavy_skills,
            "oversized_skill_files": oversized_skill_files,
        },
        "mcp_servers": mcp_counts,
        "projects_audited": len(project_reports),
        "projects": project_reports,
        "worktrees_clutter": worktree_clutter_reports,
        "summary": {
            "healthy": len(attention) == 0,
            "attention_count": len(attention),
            "advice_count": len(advice),
            "attention": attention,
            "advice": advice,
        },
    }


# ==============================================================================
# 2. ADOPT / FIX: Non-Destructive 4-Zone + 3-Tier `work/` & Pointer Healer
# ==============================================================================
def _effective_apply(args: argparse.Namespace) -> tuple[bool, bool]:
    """Resolve `--apply` vs `--dry-run`; `--dry-run` always overrides `--apply` in any argument order."""
    want_apply = bool(getattr(args, "apply", False))
    want_dry = bool(getattr(args, "dry_run", False))
    if want_apply and want_dry:
        return False, True
    return want_apply, False


def ensure_build_spotlight_shields(home: Path, projects_root: Path, apply: bool) -> list[dict[str, str]]:
    """Ensure `.metadata_never_index` exists on Xcode build/simulator caches and workspace Data/Archive/Worktrees."""
    shield_targets: list[Path] = [
        home / "Library/Developer/Xcode/DerivedData",
        home / "Library/Developer/CoreSimulator",
        home / "Library/Developer/XCTestDevices",
    ]
    ai_root = resolve_ai_root(projects_root)
    if ai_root and ai_root.is_dir():
        shield_targets.extend(
            [
                ai_root / "Data",
                ai_root / "Archive",
                ai_root / "Orca/Worktrees",
                ai_root / "Codex/Worktrees",
                ai_root / "Claude/Worktrees",
            ]
        )
    actions: list[dict[str, str]] = []
    seen_targets: set[str] = set()
    for td in shield_targets:
        key = str(td)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        try:
            if not td.is_dir() or td.is_symlink():
                continue
            marker = td / ".metadata_never_index"
            if not marker.exists():
                actions.append({"project": "_system", "action": "touch_spotlight_shield", "path": str(marker)})
                if apply:
                    marker.write_text("", encoding="utf-8")
        except OSError:
            continue
    return actions


def run_adopt(args: argparse.Namespace) -> dict[str, Any]:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else Path.home()
    projects_root = resolve_projects_root(args.root, home=home)
    apply, had_conflict = _effective_apply(args)
    trash_root = Path(args.trash_dir).expanduser().resolve() if getattr(args, "trash_dir", None) else None
    stager = TrashStager(
        trash_root=trash_root,
        run_label="adopt",
        default_home=home,
        manifest_extra={"mode": "adopt", "projects_root": str(projects_root)},
    )
    tracked_index = GitTrackedIndex()
    actions: list[dict[str, Any]] = []

    candidates: list[Path] = []
    if projects_root.is_dir():
        if any((projects_root / z).is_dir() for z in FOUR_ZONES) and (projects_root / "AGENTS.md").is_file():
            candidates = [projects_root]
        else:
            candidates = sorted(
                p for p in projects_root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
            )

    today = datetime.now().strftime("%Y%m%d")

    for proj in candidates:
        # 1. 4 zones + 3 work tiers
        for z in FOUR_ZONES:
            zp = proj / z
            if not zp.exists():
                actions.append({"project": proj.name, "action": "mkdir_zone", "path": str(zp)})
                if apply:
                    zp.mkdir(parents=True, exist_ok=True)

        work_dir = proj / "work"
        for tier in WORK_TIERS:
            tp = work_dir / tier
            if not tp.exists():
                actions.append({"project": proj.name, "action": "mkdir_work_tier", "path": str(tp)})
                if apply:
                    tp.mkdir(parents=True, exist_ok=True)

        shield = work_dir / ".metadata_never_index"
        if not shield.exists():
            actions.append({"project": proj.name, "action": "touch_spotlight_shield", "path": str(shield)})
            if apply and work_dir.is_dir():
                shield.write_text("", encoding="utf-8")

        wreadme = work_dir / "README.md"
        if not wreadme.exists():
            actions.append({"project": proj.name, "action": "write_work_contract", "path": str(wreadme)})
            if apply and work_dir.is_dir():
                wreadme.write_text(WORK_README_CONTENT, encoding="utf-8")

        # 2. .git/info/exclude for `/work/`
        if is_git_root(proj):
            ex = resolve_git_common_exclude(proj)
            if ex and ensure_git_info_exclude(proj, "/work/", apply=apply):
                actions.append({"project": proj.name, "action": "git_info_exclude_work", "path": str(ex)})

        # 3. @AGENTS.md pointer healing (with clean `git status` protection)
        if (proj / "AGENTS.md").is_file():
            for ptr_name in ("CLAUDE.md", "GEMINI.md"):
                ptr = proj / ptr_name
                if not ptr.exists():
                    actions.append({"project": proj.name, "action": f"create_pointer_{ptr_name}", "path": str(ptr)})
                    if is_git_root(proj) and not is_git_tracked_or_ignored(proj, ptr_name):
                        ex = resolve_git_common_exclude(proj)
                        if ex and ensure_git_info_exclude(proj, f"/{ptr_name}", apply=apply):
                            actions.append(
                                {"project": proj.name, "action": f"git_info_exclude_{ptr_name}", "path": str(ex)}
                            )
                    if apply:
                        ptr.write_text("@AGENTS.md\n", encoding="utf-8")

        # 4. Optional root clutter safe relocation / staging
        if args.relocate_clutter:
            try:
                proj_children = sorted(proj.iterdir())
            except OSError:
                proj_children = []
            for child in proj_children:
                cname = child.name
                if cname == ".DS_Store" and child.is_file() and not child.is_symlink():
                    if not stager.can_stage_same_device(child):
                        continue
                    sz = child.stat().st_size
                    actions.append({"project": proj.name, "action": "trash_ds_store", "path": str(child)})
                    if apply:
                        stager.stage(child, reason="root .DS_Store cleanup", category="root_ds_store", size_bytes=sz)
                elif cname.startswith("tmpclaude-") and child.is_file() and not child.is_symlink():
                    if not tracked_index.has_tracked(child) and stager.can_stage_same_device(child):
                        sz = child.stat().st_size
                        actions.append({"project": proj.name, "action": "trash_leaked_tmpclaude", "path": str(child)})
                        if apply:
                            stager.stage(
                                child, reason="leaked tmpclaude root file", category="tmpclaude", size_bytes=sz
                            )
                elif cname in FORBIDDEN_ROOT_DIRS and child.is_dir() and not child.is_symlink():
                    if tracked_index.has_tracked(child):
                        continue
                    ti = inspect_tree(child)
                    if ti.had_error or ti.has_mount_or_xdev or ti.has_nested_git:
                        actions.append(
                            {
                                "project": proj.name,
                                "action": "skip_unsafe_root_clutter",
                                "path": str(child),
                                "reason": "contains nested .git/worktree, mountpoint, or unreadable entries",
                            }
                        )
                        continue
                    try:
                        entries = [x for x in child.iterdir() if x.name != ".DS_Store"]
                    except OSError:
                        continue
                    if not entries:
                        if not stager.can_stage_same_device(child):
                            continue
                        actions.append({"project": proj.name, "action": "trash_empty_root_clutter", "path": str(child)})
                        if apply:
                            # Remove stray .DS_Store inside empty clutter dir if present, or stage whole dir atomically
                            stager.stage(
                                child,
                                reason=f"empty unauthorized root dir `{cname}/`",
                                category="empty_root_clutter",
                                size_bytes=ti.total_bytes,
                            )
                    else:
                        reloc_base = proj / "work/retained" / f"from-root-{cname}-{today}"
                        reloc_dest = reloc_base
                        seq = 2
                        while reloc_dest.exists():
                            reloc_dest = Path(f"{reloc_base}-{seq}")
                            seq += 1
                        actions.append(
                            {
                                "project": proj.name,
                                "action": "relocate_nonempty_root_clutter",
                                "from": str(child),
                                "to": str(reloc_dest),
                            }
                        )
                        if apply:
                            stager.relocate(
                                child,
                                reloc_dest,
                                reason=f"relocated non-empty root `{cname}/` into work/retained/",
                                size_bytes=ti.total_bytes,
                            )

    actions.extend(ensure_build_spotlight_shields(home=home, projects_root=projects_root, apply=apply))
    manifest = stager.write_manifest({"mode": "adopt", "projects_root": str(projects_root)}) if apply else None
    res_adopt: dict[str, Any] = {
        "schema": "agent-governor-adopt/v1",
        "version": VERSION,
        "mode": "apply" if apply else "dry-run",
        "projects_root": str(projects_root),
        "action_count": len(actions),
        "manifest": str(manifest) if manifest else None,
        "actions": actions,
    }
    if had_conflict:
        res_adopt["flag_conflict"] = "dry-run overrides --apply; no changes made"
    return res_adopt


# ==============================================================================
# 3. SCRATCH & TEST-DIR ALLOCATOR: Collision-Safe Lease-Stamped Directories
# ==============================================================================
class GovernorError(Exception):
    """User-facing error: printed without a traceback and exits with status 2."""


def infer_project_dir(explicit_project: str | None, cwd: Path | None = None, home: Path | None = None) -> Path:
    """Nearest ancestor that is a Git checkout or has AGENTS.md, else the top-level project inside the projects root."""
    start = Path(explicit_project).expanduser().resolve() if explicit_project else (cwd or Path.cwd()).resolve()
    if start.is_file():
        start = start.parent
    for cand in [start, *start.parents]:
        if (cand / ".git").exists() or (cand / "AGENTS.md").is_file():
            return cand
    projects_root = resolve_projects_root(home=home)
    if resolve_ai_root(projects_root) is not None:
        try:
            rel = start.relative_to(projects_root.resolve())
        except ValueError:
            rel = None
        if rel is not None and rel.parts:
            return projects_root.resolve() / rel.parts[0]
    raise GovernorError(
        f"{start} is not inside a project (no Git checkout or AGENTS.md above it, and not under {projects_root}); "
        "run from a project directory or pass --project <dir>"
    )


def allocate_ephemeral_dir(zone: str, args: argparse.Namespace) -> Path:
    proj = infer_project_dir(args.project)
    agent = slugify(
        args.agent
        or os.environ.get("ORCA_AGENT_TYPE")
        or ("claude" if os.environ.get("CLAUDECODE") else "")
        or ("codex" if os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_CI") else "")
        or "agent"
    )
    topic = slugify(args.topic)
    ttl_hours = float(args.ttl_days) * 24.0 if getattr(args, "ttl_days", None) is not None else float(args.ttl_hours)
    sub = "_scratch" if zone == "scratch" else "_tests"
    day = datetime.now().strftime("%Y%m%d")
    procs = process_table()
    raw_ppid = os.getppid()
    ancestor_pids = collect_ancestor_pids(raw_ppid, table=procs) or [raw_ppid]
    # Prefer the highest non-GUI session ancestor (e.g. persistent claude/codex/shell) over transient `$(...)` subshell
    creator_pid = ancestor_pids[-1]
    # (pid, start time) pairs let GC tell a live owner from an unrelated process that reused the PID
    owners = [[pid, procs[pid][1] if pid in procs else ""] for pid in ancestor_pids]
    proc_start_sig = procs[creator_pid][1] if creator_pid in procs else ""
    session_id = os.environ.get("ORCA_PANE_KEY") or os.environ.get("CLAUDE_SESSION_ID") or ""

    work_dir = proj / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    shield = work_dir / ".metadata_never_index"
    if not shield.exists():
        shield.write_text("", encoding="utf-8")
    if is_git_root(proj):
        ensure_git_info_exclude(proj, "/work/", apply=True)

    tier_dir = work_dir / sub
    tier_dir.mkdir(parents=True, exist_ok=True)

    base_name = f"{day}-{agent}-{topic}"
    target: Path | None = None

    def _can_reuse_existing(cand_dir: Path) -> bool:
        if args.unique:
            return False
        existing_lease = read_lease(cand_dir)
        if not existing_lease:
            # Another concurrent allocator may be writing `.agent-lease.json` right now; wait briefly once
            time.sleep(0.02)
            existing_lease = read_lease(cand_dir)
            if not existing_lease:
                return False
        existing_sid = str(existing_lease.get("session_id") or "")
        if session_id and existing_sid:
            return existing_sid == session_id
        if bool(session_id) != bool(existing_sid):
            return False
        existing_sig = str(existing_lease.get("pid_lstart") or "")
        if proc_start_sig and existing_sig and proc_start_sig != existing_sig:
            return False
        existing_pid = existing_lease.get("pid") if isinstance(existing_lease.get("pid"), int) else None
        existing_anc = existing_lease.get("ancestor_pids") if isinstance(existing_lease.get("ancestor_pids"), list) else []
        same_lineage = bool(set(ancestor_pids) & ({existing_pid} | {x for x in existing_anc if isinstance(x, int)}))
        return same_lineage

    first_cand = tier_dir / base_name
    if not args.unique:
        try:
            os.mkdir(first_cand)
            target = first_cand
        except FileExistsError:
            if _can_reuse_existing(first_cand):
                target = first_cand

    if target is None:
        stamp = datetime.now().strftime("%H%M%S")
        alloc_pid = os.getpid()
        for seq in range(1, 512):
            suffix = f"{stamp}-{alloc_pid}" if seq == 1 else f"{stamp}-{alloc_pid}-{seq}"
            cand = tier_dir / f"{base_name}-{suffix}"
            try:
                os.mkdir(cand)
                target = cand
                break
            except FileExistsError:
                continue
        if target is None:
            raise OSError(errno.EEXIST, f"Unable to allocate unique directory under {tier_dir}")

    now = datetime.now(timezone.utc)
    lease = {
        "schema": "agent-governor-lease/v1.5",
        "zone": zone,
        "project": str(proj),
        "agent": agent,
        "topic": topic,
        "pid": creator_pid,
        "pid_lstart": proc_start_sig,
        "ancestor_pids": ancestor_pids,
        "owners": owners,
        "session_id": session_id,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(),
        "ttl_hours": ttl_hours,
    }
    _atomic_write_json(target / ".agent-lease.json", lease)
    return target


# ==============================================================================
# 4. FIVE-GATE GARBAGE COLLECTOR (`gc`)
# ==============================================================================
@dataclass
class GCCandidate:
    category: str
    path: Path
    size_bytes: int
    age_hours: float
    reason: str
    scan_dev: int | None = None
    scan_ino: int | None = None
    scan_latest_activity: float | None = None
    scan_lease_expires_at: str = ""
    session_end_candidate: bool = False


def has_pin_marker(directory: Path) -> bool:
    try:
        return any((directory / marker).exists() for marker in PIN_MARKERS)
    except OSError:
        return False


def read_lease(directory: Path) -> dict[str, Any]:
    lf = directory / ".agent-lease.json"
    try:
        if not lf.is_file():
            return {}
        d = json.loads(lf.read_text(encoding="utf-8", errors="ignore"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def scan_work_ephemeral_candidates(
    projects_dirs: list[Path],
    now_ts: float,
    scratch_ttl_h: float,
    tests_ttl_h: float,
    min_idle_h: float,
    live_pids: set[int],
    proc_cmds: list[str],
    tracked_index: GitTrackedIndex | None = None,
    session_end_only: bool = False,
    exiting_pids: frozenset[int] = frozenset(),
) -> tuple[list[GCCandidate], list[dict[str, str]]]:
    """`exiting_pids` (session-end mode only): the exiting session's own process chain, not counted as live owners."""
    candidates: list[GCCandidate] = []
    protected_skips: list[dict[str, str]] = []
    git_idx = tracked_index or GitTrackedIndex()
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    cur_session_id = os.environ.get("ORCA_PANE_KEY") or os.environ.get("CLAUDE_SESSION_ID") or ""

    zones = [("_scratch", scratch_ttl_h)]
    if not session_end_only:
        zones.append(("_tests", tests_ttl_h))

    for proj in projects_dirs:
        try:
            for item in proj.iterdir():
                if item.name.startswith("tmpclaude-") and item.is_file() and not item.is_symlink():
                    try:
                        st = item.lstat()
                    except OSError:
                        continue
                    act_ts = max(float(st.st_mtime), float(st.st_ctime))
                    age_h = max(0.0, (now_ts - act_ts) / 3600.0)
                    if age_h >= max(0.25, min_idle_h) and not path_used_by_processes(item, proc_cmds):
                        if not git_idx.has_tracked(item):
                            candidates.append(
                                GCCandidate(
                                    category="leaked_tmpclaude",
                                    path=item,
                                    size_bytes=st.st_size,
                                    age_hours=round(age_h, 2),
                                    reason=f"leaked root {item.name} idle {age_h:.1f}h",
                                    scan_dev=int(st.st_dev),
                                    scan_ino=int(st.st_ino),
                                    scan_latest_activity=act_ts,
                                )
                            )
        except OSError:
            pass

        work_dir = proj / "work"
        try:
            if not work_dir.is_dir() or work_dir.is_symlink():
                continue
        except OSError:
            continue

        for sub_name, ttl_h in zones:
            tier_dir = work_dir / sub_name
            try:
                if not tier_dir.is_dir() or tier_dir.is_symlink():
                    continue
                entries = sorted(tier_dir.iterdir())
            except OSError:
                protected_skips.append({"path": str(tier_dir), "gate": "Gate03_UnreadableFailClosed"})
                continue

            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_symlink():
                    protected_skips.append({"path": str(entry), "gate": "Gate03_SymlinkGuard"})
                    continue

                try:
                    entry_st = entry.lstat()
                    is_dir_entry = stat.S_ISDIR(entry_st.st_mode)
                except OSError:
                    protected_skips.append({"path": str(entry), "gate": "Gate03_UnreadableFailClosed"})
                    continue

                if is_dir_entry and has_pin_marker(entry):
                    protected_skips.append({"path": str(entry), "gate": "Gate02_PinMarker"})
                    continue

                lease = read_lease(entry) if is_dir_entry else {}
                ignore = exiting_pids if session_end_only else frozenset()
                if lease_has_live_owner(lease, live_pids, ignore) or path_used_by_processes(entry, proc_cmds):
                    protected_skips.append({"path": str(entry), "gate": "Gate01_ActiveProcessLock"})
                    continue

                if not session_end_only:
                    # Cheap gates before the full inspection: an unexpired lease, or any activity in the tree
                    # newer than the TTL, means the entry is not due. Only real candidates pay for the walk
                    # that totals sizes and looks for mounts, pins and nested repositories.
                    lease_ttl_h = ttl_h
                    if lease.get("expires_at"):
                        try:
                            exp_dt = datetime.fromisoformat(str(lease["expires_at"]))
                            if exp_dt.tzinfo is None:
                                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                            if now_dt < exp_dt:
                                protected_skips.append({"path": str(entry), "gate": "Gate02_ActiveLeaseTTL"})
                                continue
                            lease_ttl_h = min(ttl_h, float(lease.get("ttl_hours") or ttl_h))
                        except (ValueError, TypeError):
                            pass
                    recent = first_activity_after(entry, now_ts - max(lease_ttl_h, min_idle_h) * 3600.0)
                    if recent:
                        if (now_ts - recent) / 3600.0 < min_idle_h:
                            protected_skips.append({"path": str(entry), "gate": "Gate01_MinIdleGuard"})
                        continue

                ti = inspect_tree(entry)
                if ti.had_error:
                    protected_skips.append({"path": str(entry), "gate": "Gate03_UnreadableFailClosed"})
                    continue
                if ti.has_mount_or_xdev:
                    protected_skips.append({"path": str(entry), "gate": "Gate03_MountpointBoundaryGuard"})
                    continue
                if ti.has_nested_pin:
                    protected_skips.append({"path": str(entry), "gate": "Gate02_PinMarker"})
                    continue
                if ti.has_nested_git:
                    protected_skips.append({"path": str(entry), "gate": "Gate03_NestedGitRepoGuard"})
                    continue

                sz = ti.total_bytes
                idle_h = max(0.0, (now_ts - ti.latest_activity) / 3600.0)

                if session_end_only:
                    if is_dir_entry and ti.non_lease_files == 0:
                        lease_sid = str(lease.get("session_id") or "")
                        same_exiting_session = bool(cur_session_id and lease_sid and lease_sid == cur_session_id)
                        # Only reap in session-end if owned by the exiting session, or abandoned empty for >= 2h
                        if same_exiting_session or (not cur_session_id and not lease_sid and idle_h >= 2.0) or (
                            lease_sid != cur_session_id and idle_h >= 2.0
                        ):
                            if not git_idx.has_tracked(entry):
                                candidates.append(
                                    GCCandidate(
                                        category=f"empty_{sub_name.lstrip('_')}",
                                        path=entry,
                                        size_bytes=sz,
                                        age_hours=round(idle_h, 2),
                                        reason="empty agent scratch directory after session exit",
                                        scan_dev=int(entry_st.st_dev),
                                        scan_ino=int(entry_st.st_ino),
                                        scan_latest_activity=ti.latest_activity,
                                        scan_lease_expires_at=str(lease.get("expires_at") or ""),
                                        session_end_candidate=True,
                                    )
                                )
                    continue

                if idle_h < min_idle_h:
                    protected_skips.append({"path": str(entry), "gate": "Gate01_MinIdleGuard"})
                    continue

                expired = False
                if lease.get("expires_at"):
                    try:
                        exp_dt = datetime.fromisoformat(str(lease["expires_at"]))
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                        if now_dt < exp_dt:
                            # Explicit lease TTL still active (e.g. --ttl-days 30): honour extended lease!
                            protected_skips.append({"path": str(entry), "gate": "Gate02_ActiveLeaseTTL"})
                            continue
                        lease_ttl_h = float(lease.get("ttl_hours") or ttl_h)
                        effective_ttl_h = min(ttl_h, lease_ttl_h)
                        expired = idle_h >= effective_ttl_h and idle_h >= min_idle_h
                    except (ValueError, TypeError):
                        expired = idle_h >= ttl_h
                else:
                    expired = idle_h >= ttl_h

                if not expired:
                    continue

                if git_idx.has_tracked(entry):
                    protected_skips.append({"path": str(entry), "gate": "Gate03_GitTrackedGuard"})
                    continue

                candidates.append(
                    GCCandidate(
                        category=f"work{sub_name}",
                        path=entry,
                        size_bytes=sz,
                        age_hours=round(idle_h, 1),
                        reason=f"{sub_name} expired (idle {idle_h / 24.0:.1f}d >= TTL {ttl_h / 24.0:.1f}d)",
                        scan_dev=int(entry_st.st_dev),
                        scan_ino=int(entry_st.st_ino),
                        scan_latest_activity=ti.latest_activity,
                        scan_lease_expires_at=str(lease.get("expires_at") or ""),
                    )
                )

    return candidates, protected_skips


def scan_agent_home_ephemerals(
    home: Path,
    now_ts: float,
    tmp_ttl_h: float,
    cache_ttl_h: float,
    keep_backups: int,
    proc_cmds: list[str],
) -> list[GCCandidate]:
    candidates: list[GCCandidate] = []
    orca_dir = home / "Library/Application Support/orca"

    def _safe_children(d: Path) -> list[Path]:
        try:
            if not d.is_dir() or d.is_symlink():
                return []
            return sorted(d.iterdir())
        except OSError:
            return []

    def _account_homes(d: Path) -> list[Path]:
        return [p / "home" for p in _safe_children(d) if not p.name.startswith(".")]

    codex_homes = [home / ".codex", *_account_homes(orca_dir / "codex-accounts")]
    claude_homes = [home / ".claude", *_account_homes(orca_dir / "claude-profiles")]

    # (parent dir, category, TTL hours, reason label, extra) — children of `parent` idle >= TTL are candidates.
    # extra: "skip_arg0" keeps Codex argv0 dispatch shims; "session_env" = dirs only, empty ones expire at tmp TTL.
    rules: list[tuple[Path, str, float, str, str]] = []
    for ch in codex_homes:
        if not ch.is_dir() or ch.is_symlink():
            continue
        for tdir_name in (".tmp", "tmp"):
            rules.append((ch / tdir_name, "codex_tmp", tmp_ttl_h, f"Codex {tdir_name} artifact", "skip_arg0"))
        rules.append((ch / "shell_snapshots", "codex_shell_snapshot", cache_ttl_h, "Codex shell snapshot", ""))
        rules.append((ch / "log", "codex_log", cache_ttl_h * 2, "Codex runtime log", ""))
    for clh in claude_homes:
        if not clh.is_dir() or clh.is_symlink():
            continue
        for sub_name, cat_name in (
            ("shell-snapshots", "claude_shell_snapshot"),
            ("paste-cache", "claude_paste_cache"),
            ("telemetry", "claude_telemetry"),
        ):
            rules.append((clh / sub_name, cat_name, cache_ttl_h, f"Claude {sub_name}", ""))
        rules.append((clh / "session-env", "claude_session_env", cache_ttl_h * 2, "stale Claude session-env", "session_env"))
    rules.append((orca_dir / "chat-scratch", "orca_chat_scratch", cache_ttl_h * 2, "Orca chat-scratch", ""))
    for agy_brain in (
        orca_dir / "antigravity-chat/antigravity-acp/brain",
        orca_dir / "antigravity-chat/antigravity-cli/brain",
        home / ".gemini/antigravity/brain",
        home / ".gemini/antigravity-cli/brain",
    ):
        for conv_dir in _safe_children(agy_brain):
            if conv_dir.is_dir() and not conv_dir.is_symlink():
                rules.append((conv_dir / "scratch", "antigravity_scratch", cache_ttl_h, "Antigravity scratch artifact", ""))
    for agy_log in (orca_dir / "antigravity-chat/antigravity-cli/log", home / ".gemini/antigravity-cli/log"):
        rules.append((agy_log, "antigravity_log", cache_ttl_h * 2, "Antigravity log", ""))

    for parent, category, ttl_h, label, extra in rules:
        for item in _safe_children(parent):
            if item.is_symlink() or path_used_by_processes(item, proc_cmds):
                continue
            if extra == "skip_arg0" and item.name.startswith("arg0"):
                continue
            if extra == "session_env" and not item.is_dir():
                continue
            try:
                ist = item.lstat()
            except OSError:
                continue
            ti = inspect_tree(item)
            if ti.had_error or ti.has_mount_or_xdev or ti.has_nested_pin or ti.has_nested_git:
                continue
            age_h = max(0.0, (now_ts - ti.latest_activity) / 3600.0)
            expired = age_h >= ttl_h or (extra == "session_env" and ti.file_count == 0 and age_h >= tmp_ttl_h)
            if not expired:
                continue
            what = f"{label} ({ti.file_count} files)" if extra == "session_env" else label
            idle_txt = f"{age_h:.1f}h >= {ttl_h:.0f}h" if ttl_h < 48 else f"{age_h / 24.0:.1f}d"
            candidates.append(
                GCCandidate(
                    category=category,
                    path=item,
                    size_bytes=ti.total_bytes,
                    age_hours=round(age_h, 1),
                    reason=f"{what} idle {idle_txt}",
                    scan_dev=int(ist.st_dev),
                    scan_ino=int(ist.st_ino),
                    scan_latest_activity=ti.latest_activity,
                )
            )

    # Config backup file retention (`*.bak*`, `*.backup*`) in agent config dirs
    if keep_backups >= 0:
        for cfg_dir in [*codex_homes, *claude_homes, home / ".gemini"]:
            grouped: dict[str, list[tuple[os.stat_result, Path]]] = defaultdict(list)
            for item in _safe_children(cfg_dir):
                if not item.is_file() or item.is_symlink():
                    continue
                for pat in BACKUP_FILE_PATTERNS:
                    m = pat.match(item.name)
                    if m:
                        try:
                            grouped[m.group(1)].append((item.lstat(), item))
                        except OSError:
                            pass
                        break
            for base_name, entries in grouped.items():
                entries.sort(key=lambda x: x[0].st_mtime, reverse=True)
                for st, old_bak in entries[keep_backups:]:
                    candidates.append(
                        GCCandidate(
                            category="excess_config_backup",
                            path=old_bak,
                            size_bytes=st.st_size,
                            age_hours=round(max(0.0, (now_ts - st.st_mtime) / 3600.0), 1),
                            reason=f"superseded backup of {base_name} (retaining newest {keep_backups})",
                            scan_dev=int(st.st_dev),
                            scan_ino=int(st.st_ino),
                            scan_latest_activity=max(float(st.st_mtime), float(st.st_ctime)),
                        )
                    )
    return candidates


def scan_ephemeral_simulators(
    now_ts: float,
    sim_ttl_h: float,
    protected_udids: set[str],
    proc_cmds: list[str],
    home: Path | None = None,
    coordination: Path | None = None,
    only_udid: str | None = None,
    broad_names: bool = False,
) -> list[dict[str, Any]]:
    """Idle Shutdown simulators created by `agent-test-device` (or, with `broad_names`, any ephemeral-looking name)."""
    if not Path("/usr/bin/xcrun").is_file():
        return []
    try:
        r = subprocess.run(
            ["/usr/bin/xcrun", "simctl", "list", "devices", "-j"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode != 0 or not r.stdout:
            return []
        data = json.loads(r.stdout)
    except Exception:
        return []

    reapable: list[dict[str, Any]] = []
    for runtime, devices in (data.get("devices") or {}).items():
        if not isinstance(devices, list):
            continue
        for dev in devices:
            udid = str(dev.get("udid") or "")
            name = str(dev.get("name") or "")
            state = str(dev.get("state") or "")
            if only_udid and udid != only_udid:
                continue
            if not udid or udid.upper() in protected_udids:
                continue
            if coordination and os.path.lexists(coordination / f"active-{udid.upper()}"):
                continue
            if state != "Shutdown":
                continue
            owner = MANAGED_SIM_RE.fullmatch(name)
            if not owner and not (broad_names and EPHEMERAL_SIM_RE.search(name)):
                continue
            if owner and pid_alive(int(owner[1])):
                continue
            if any(udid.lower() in c.lower() for c in proc_cmds):
                continue
            data_path_raw = str(dev.get("dataPath") or "")
            if not data_path_raw:
                continue
            data_path = Path(data_path_raw)
            if not data_path.is_dir():
                continue
            if home is not None and not str(data_path.resolve()).startswith(str(home.resolve()).rstrip("/") + "/"):
                continue
            sz, mtime, _ = dir_stats(data_path)
            idle_h = max(0.0, (now_ts - mtime) / 3600.0)
            if idle_h >= sim_ttl_h:
                reapable.append(
                    {
                        "udid": udid,
                        "name": name,
                        "runtime": runtime,
                        "data_path": str(data_path),
                        "size_bytes": sz,
                        "idle_hours": round(idle_h, 1),
                    }
                )
    return reapable


@contextmanager
def reserve_simulator_for_gc(coordination: Path | None, udid: str) -> Iterator[bool]:
    if not coordination or not coordination.is_dir():
        yield True
        return
    lease = coordination / f"active-{udid.upper()}"
    try:
        lease.mkdir()
    except FileExistsError:
        yield False
        return
    try:
        yield True
    finally:
        lease.rmdir()


def collect_project_targets(projects_root: Path) -> list[Path]:
    targets: list[Path] = []
    seen: set[str] = set()

    def _add(cand: Path) -> None:
        key = str(cand)
        if key not in seen:
            seen.add(key)
            targets.append(cand)

    if not projects_root.is_dir():
        return targets
    try:
        top_Single = any((projects_root / z).is_dir() for z in FOUR_ZONES) and (projects_root / "AGENTS.md").is_file()
    except OSError:
        top_Single = False

    top_projects: list[Path] = []
    if top_Single:
        top_projects.append(projects_root)
    else:
        try:
            for p in sorted(projects_root.iterdir()):
                if p.is_dir() and not p.is_symlink() and not p.name.startswith("."):
                    top_projects.append(p)
        except OSError:
            pass

    for p in top_projects:
        _add(p)
        # Also include nested standalone Git sub-repos (e.g. <Project>/outputs/<repo>, <Project>/<subrepo>) for GC
        for container in (p, p / "outputs"):
            try:
                if not container.is_dir() or container.is_symlink():
                    continue
                for child in sorted(container.iterdir()):
                    if child.name.startswith(".") or child.is_symlink():
                        continue
                    if container == p and child.name in FOUR_ZONES:
                        continue
                    if child.is_dir() and (
                        (child / ".git").exists()
                        or (child / "work/_scratch").is_dir()
                        or (child / "work/_tests").is_dir()
                    ):
                        _add(child)
            except OSError:
                continue

    ai_root = resolve_ai_root(projects_root)
    if ai_root:
        for wt in discover_worktree_checkouts(ai_root):
            _add(wt)
    return targets


def _resolve_gc_ttls(args: argparse.Namespace) -> dict[str, float]:
    """Effective TTLs in hours for the selected `--mode`, with explicit CLI overrides winning."""
    defaults = GC_MODE_DEFAULTS.get(args.mode, GC_MODE_DEFAULTS["daily"])

    def _pick(arg_name: str, key: str, scale: float) -> float:
        val = getattr(args, arg_name, None)
        return float(val if val is not None else defaults[key]) * scale

    return {
        "scratch": _pick("scratch_ttl_days", "scratch_ttl_days", 24.0),
        "tests": _pick("tests_ttl_days", "tests_ttl_days", 24.0),
        "cache": _pick("cache_ttl_days", "cache_ttl_days", 24.0),
        "tmp": _pick("tmp_ttl_hours", "tmp_ttl_hours", 1.0),
        "sim": _pick("sim_ttl_days", "sim_ttl_days", 24.0),
        "derived_data": _pick("derived_data_ttl_hours", "derived_data_ttl_hours", 1.0),
        "system_derived_data": _pick("system_derived_data_ttl_hours", "system_derived_data_ttl_hours", 1.0),
        "min_idle": _pick("min_idle_hours", "min_idle_hours", 1.0),
    }


def _work_tier_entries(path: Path) -> list[Path]:
    """Every `<proj>/work/{_scratch,_tests}/<entry>` strictly enclosing `path`."""
    parts = path.parts
    return [
        Path(*parts[: i + 2])
        for i in range(1, len(parts) - 2)
        if parts[i - 1] == "work" and parts[i] in ("_scratch", "_tests")
    ]


def enclosing_entry_gate(path: Path, now_ts: float, live_pids: set[int] | dict[int, str] | None = None) -> str | None:
    """Gate name if `path` sits inside a work-tier entry that is pinned, owned by a live process or still leased."""
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    for entry in _work_tier_entries(path):
        if entry.is_symlink():
            continue
        if has_pin_marker(entry):
            return "Gate02_EnclosingPinMarker"
        lease = read_lease(entry)
        if not lease:
            continue
        if lease_has_live_owner(lease, live_pids):
            return "Gate01_EnclosingActiveProcessLock"
        try:
            exp_dt = datetime.fromisoformat(str(lease.get("expires_at") or ""))
        except (ValueError, TypeError):
            continue
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        if now_dt < exp_dt:
            return "Gate02_EnclosingActiveLeaseTTL"
    return None


def scan_xcode_build_ephemerals(
    home: Path,
    projects_root: Path,
    targets: list[Path],
    already_queued: set[Path],
    now_ts: float,
    derived_data_ttl_h: float,
    min_idle_h: float,
    proc_cmds: list[str],
    tracked_index: GitTrackedIndex,
    workspace_only: bool = False,
    system_derived_data_ttl_h: float | None = None,
    live_pids: set[int] | dict[int, str] | None = None,
) -> tuple[list[GCCandidate], list[dict[str, str]]]:
    """Sweep stale Xcode `DerivedData*` build trees and transient XCTest runner bundles (`*UITests-Runner.app`,
    `*Tests-Runner.app`, `*.xctrunner`) across `Xcode/DerivedData`, `AI/Data`, and `work/{_scratch,_tests}`.

    `derived_data_ttl_h` applies to custom `-derivedDataPath` trees; `system_derived_data_ttl_h` (default: the same)
    to per-project folders in ~/Library/Developer/Xcode/DerivedData. Nothing inside a pinned, live-owned or
    still-leased `work/{_scratch,_tests}/<entry>` is touched."""
    sys_dd_ttl_h = derived_data_ttl_h if system_derived_data_ttl_h is None else system_derived_data_ttl_h
    cands: list[GCCandidate] = []
    skips: list[dict[str, str]] = []
    queued: set[Path] = set(already_queued)

    active_xcode_cmds = [c.lower() for c in proc_cmds if ("xcodebuild" in c or "xctest" in c)]

    def _path_or_ancestor_busy(p: Path, stop_root: Path) -> bool:
        cur = p
        while True:
            if path_used_by_processes(cur, proc_cmds):
                return True
            if cur == stop_root or cur == cur.parent:
                break
            cur = cur.parent
        return False

    def _stage_candidate(p: Path, category: str, ttl_h: float, reason_tpl: str) -> GCCandidate | None:
        if p in queued or any(anc in queued for anc in p.parents):
            return None
        try:
            st = p.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            skips.append({"path": str(p), "gate": "Gate03_SymlinkGuard"})
            return None
        if not stat.S_ISDIR(st.st_mode):
            return None
        if has_pin_marker(p):
            skips.append({"path": str(p), "gate": "Gate02_PinMarker"})
            return None
        enclosing = enclosing_entry_gate(p, now_ts, live_pids)
        if enclosing:
            skips.append({"path": str(p), "gate": enclosing})
            return None
        ti = inspect_tree(p)
        if ti.had_error:
            skips.append({"path": str(p), "gate": "Gate03_UnreadableFailClosed"})
            return None
        if ti.has_mount_or_xdev:
            skips.append({"path": str(p), "gate": "Gate03_MountpointBoundaryGuard"})
            return None
        if ti.has_nested_pin:
            skips.append({"path": str(p), "gate": "Gate02_PinMarker"})
            return None
        if ti.has_nested_git:
            skips.append({"path": str(p), "gate": "Gate03_NestedGitRepoGuard"})
            return None
        if tracked_index.has_tracked(p):
            skips.append({"path": str(p), "gate": "Gate03_GitTrackedGuard"})
            return None

        idle_h = max(0.0, (now_ts - ti.latest_activity) / 3600.0)
        eff_ttl_h = max(ttl_h, min_idle_h)
        if idle_h < eff_ttl_h:
            if idle_h < min_idle_h:
                skips.append({"path": str(p), "gate": "Gate01_MinIdleGuard"})
            return None
        return GCCandidate(
            category=category,
            path=p,
            size_bytes=ti.total_bytes,
            age_hours=round(idle_h, 1),
            reason=reason_tpl.format(idle_h=idle_h, ttl_h=eff_ttl_h),
            scan_dev=int(st.st_dev),
            scan_ino=int(st.st_ino),
            scan_latest_activity=ti.latest_activity,
        )

    def _on_walk_error(err: OSError) -> None:
        err_path = getattr(err, "filename", None)
        if err_path:
            skips.append({"path": str(err_path), "gate": "Gate03_UnreadableFailClosed"})

    def _safe_is_real_dir(p: Path) -> bool:
        try:
            return p.is_dir() and not p.is_symlink()
        except OSError:
            skips.append({"path": str(p), "gate": "Gate03_UnreadableFailClosed"})
            return False

    def _safe_is_symlink(p: Path) -> bool:
        try:
            return p.is_symlink()
        except OSError:
            return True

    pin_set = frozenset(PIN_MARKERS)
    skip_walk_dirs = frozenset({
        "node_modules", ".venv", "venv", "__pycache__", "target",
        "Intermediates.noindex", "Index.noindex", "SourcePackages",
        "ModuleCache.noindex", "CompilationCache.noindex", "Logs",
        "Objects-normal", "SDKStatCaches.noindex", "SDKExplicitPrecompiledModules",
        ".git", ".build", "Pods",
    })
    skip_bundle_suffixes = (".app", ".xcresult", ".dSYM", ".saved-application", ".app.saved", ".framework", ".xcframework", ".bundle", ".pkg", ".dmg")

    def _sweep_runners_under(build_dir: Path, stop_root: Path) -> None:
        if not _safe_is_real_dir(build_dir):
            return
        for dp, dnames, fnames in os.walk(build_dir, topdown=True, followlinks=False, onerror=_on_walk_error):
            cur = Path(dp)
            if queued and (cur in queued or any(anc in queued for anc in cur.parents)):
                dnames[:] = []
                continue
            if pin_set.intersection(fnames) or pin_set.intersection(dnames):
                skips.append({"path": str(cur), "gate": "Gate02_PinMarker"})
                dnames[:] = []
                continue
            next_d: list[str] = []
            for d in dnames:
                if d.startswith(".") or d in skip_walk_dirs:
                    continue
                sub = cur / d
                if _safe_is_symlink(sub) or sub in queued:
                    continue
                if d.endswith(RUNNER_BUNDLE_SUFFIXES):
                    if _path_or_ancestor_busy(sub, stop_root):
                        skips.append({"path": str(sub), "gate": "Gate01_ActiveProcessLock"})
                        continue
                    rc = _stage_candidate(
                        sub,
                        "xcode_test_runner",
                        min_idle_h,
                        "transient XCTest runner bundle idle {idle_h:.1f}h >= {ttl_h:.1f}h",
                    )
                    if rc is not None:
                        cands.append(rc)
                        queued.add(sub)
                    continue
                if not d.endswith(skip_bundle_suffixes):
                    next_d.append(d)
            dnames[:] = next_d

    # 1. Per-project build dirs in ~/Library/Developer/Xcode/DerivedData (preserves shared ModuleCache/SDK caches)
    sys_dd = home / "Library/Developer/Xcode/DerivedData"
    if not workspace_only and _safe_is_real_dir(sys_dd) and not has_pin_marker(sys_dd):
        try:
            dd_children = sorted(sys_dd.iterdir())
        except OSError:
            skips.append({"path": str(sys_dd), "gate": "Gate03_UnreadableFailClosed"})
            dd_children = []
        for child in dd_children:
            if child.name.startswith(".") or child.name in XCODE_SHARED_DERIVED_DIRS or _safe_is_symlink(child):
                continue
            if not _safe_is_real_dir(child):
                continue
            proj_stem = child.name.rsplit("-", 1)[0].lower()
            if _path_or_ancestor_busy(child, sys_dd) or (
                len(proj_stem) >= 3 and any(proj_stem in ac for ac in active_xcode_cmds)
            ):
                skips.append({"path": str(child), "gate": "Gate01_ActiveProcessLock"})
                continue
            dc = _stage_candidate(
                child,
                "stale_derived_data",
                sys_dd_ttl_h,
                "Xcode DerivedData project build cache idle {idle_h:.1f}h >= {ttl_h:.1f}h",
            )
            if dc is not None:
                cands.append(dc)
                queued.add(child)
            else:
                _sweep_runners_under(child / "Build/Products", sys_dd)

    # 2. Custom DerivedData* & transient *UITests-Runner.app bundles inside AI/Data and work/{_scratch,_tests}
    scan_roots: list[Path] = []
    ai_root = resolve_ai_root(projects_root)
    if ai_root and _safe_is_real_dir(ai_root / "Data"):
        scan_roots.append(ai_root / "Data")
    for tgt in targets:
        for sub_tier in ("work/_scratch", "work/_tests"):
            tp = tgt / sub_tier
            if _safe_is_real_dir(tp):
                scan_roots.append(tp)

    seen_sroots: set[str] = set()
    for sroot in scan_roots:
        skey = str(sroot)
        if skey in seen_sroots:
            continue
        seen_sroots.add(skey)
        try:
            os.listdir(sroot)
        except OSError:
            skips.append({"path": str(sroot), "gate": "Gate03_UnreadableFailClosed"})
            continue
        if has_pin_marker(sroot):
            skips.append({"path": str(sroot), "gate": "Gate02_PinMarker"})
            continue

        for dp, dnames, fnames in os.walk(sroot, topdown=True, followlinks=False, onerror=_on_walk_error):
            cur = Path(dp)
            if queued and (cur in queued or any(anc in queued for anc in cur.parents)):
                dnames[:] = []
                continue
            if cur != sroot and (pin_set.intersection(fnames) or pin_set.intersection(dnames)):
                skips.append({"path": str(cur), "gate": "Gate02_PinMarker"})
                dnames[:] = []
                continue

            next_d: list[str] = []
            for d in dnames:
                if d.startswith(".") or d in skip_walk_dirs:
                    continue
                sub = cur / d
                if _safe_is_symlink(sub) or sub in queued:
                    continue
                is_runner = d.endswith(RUNNER_BUNDLE_SUFFIXES)
                is_dd = d == "DerivedData" or d.startswith(("DerivedData", "derived-simulator"))
                if is_runner:
                    if _path_or_ancestor_busy(sub, sroot):
                        skips.append({"path": str(sub), "gate": "Gate01_ActiveProcessLock"})
                        continue
                    rc = _stage_candidate(
                        sub,
                        "xcode_test_runner",
                        min_idle_h,
                        "transient XCTest runner bundle idle {idle_h:.1f}h >= {ttl_h:.1f}h",
                    )
                    if rc is not None:
                        cands.append(rc)
                        queued.add(sub)
                    continue
                if is_dd:
                    if _path_or_ancestor_busy(sub, sroot):
                        skips.append({"path": str(sub), "gate": "Gate01_ActiveProcessLock"})
                        continue
                    dc = _stage_candidate(
                        sub,
                        "stale_derived_data",
                        derived_data_ttl_h,
                        "custom DerivedData build cache idle {idle_h:.1f}h >= {ttl_h:.1f}h",
                    )
                    if dc is not None:
                        cands.append(dc)
                        queued.add(sub)
                        continue
                    # Fresh DerivedData: only sweep Build/Products for transient runners instead of walking 40k+ Intermediates/Index dirs
                    _sweep_runners_under(sub / "Build/Products", sroot)
                    continue
                if not d.endswith(skip_bundle_suffixes):
                    next_d.append(d)
            dnames[:] = next_d

    return cands, skips


def scan_launchservices_ghost_apps(
    home: Path,
    proc_cmds: list[str],
    staged_paths: list[Path] | None = None,
) -> list[str]:
    """LaunchServices (`lsregister -dump`) registrations to drop: bundles that no longer exist (`🚫` ghosts) and
    XCTest runner bundles (`*Tests-Runner.app`, `*.xctrunner`) that are not running. Existing apps are kept, and a
    path whose existence cannot be determined (TCC, permissions) is never treated as gone."""
    override_bin = os.environ.get("AGENT_GOVERNOR_LSREGISTER", "").strip()
    if not override_bin:
        if sys.platform != "darwin":
            return []
        try:
            import pwd

            real_user_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
            if home.resolve() != real_user_home:
                # Hermetic isolation: LaunchServices is per-UID, not per-$HOME; never query or mutate
                # the host Mac's LaunchServices daemon when running against a synthetic $HOME / --home.
                return []
        except Exception:
            return []
    lsreg = override_bin or LSREGISTER_DEFAULT_BIN
    if not os.path.exists(lsreg):
        return []

    staged_strs = [str(p).rstrip("/") for p in (staged_paths or [])]
    to_unregister: set[str] = set()

    try:
        r = subprocess.run([lsreg, "-dump"], capture_output=True, text=True, timeout=20, errors="ignore")
        dump_out = r.stdout or ""
    except Exception:
        return []

    in_container = False
    for line in dump_out.splitlines():
        sline = line.lstrip()
        if sline.startswith("container id:"):
            in_container = True
            continue
        if sline.startswith("bundle id:") or sline.startswith("-----"):
            in_container = False
            continue
        if in_container or not sline.startswith("path:"):
            continue
        m = re.match(r"^path:\s+(.*?)\s+\(0x[0-9a-fA-F]+\)\s*$", sline)
        if not m:
            continue
        p_str = m.group(1).strip()
        p_norm = p_str.rstrip("/")
        if not p_norm or p_norm.startswith(("/System/", "/Library/")):
            continue
        if not p_norm.lower().endswith(LS_BUNDLE_SUFFIXES):
            continue
        if "/Library/Daemon Containers/" in p_str and "Runner.app" not in p_str and ".xctrunner" not in p_str:
            continue
        if p_norm.startswith("/Volumes/"):
            vol_parts = p_norm.split("/", 3)
            if len(vol_parts) >= 3 and not os.path.ismount(f"/Volumes/{vol_parts[2]}"):
                continue
        if any(p_str == s or p_str.startswith(s + "/") for s in staged_strs):
            to_unregister.add(p_str)  # just moved to the Trash by this run
            continue
        try:
            os.lstat(p_str)
        except (FileNotFoundError, NotADirectoryError):
            to_unregister.add(p_str)  # ghost: the bundle is gone
            continue
        except OSError:
            continue  # unreadable (TCC, permissions, offline volume): never treat as gone
        # Present on disk: only transient XCTest runners are unregistered, never real apps
        if p_norm.endswith(RUNNER_BUNDLE_SUFFIXES) and not path_used_by_processes(Path(p_str), proc_cmds):
            to_unregister.add(p_str)

    return sorted(to_unregister)


def unregister_launchservices_ghost_apps(paths: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    """Unregister ghost/transient `.app` bundles via `lsregister -u` + `lsregister -gc`."""
    if not paths:
        return [], []
    lsreg = os.environ.get("AGENT_GOVERNOR_LSREGISTER", "").strip() or LSREGISTER_DEFAULT_BIN
    if not os.path.exists(lsreg):
        return [], []
    done: list[str] = []
    errs: list[dict[str, str]] = []
    for p_str in paths:
        try:
            r = subprocess.run([lsreg, "-u", p_str], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                done.append(p_str)
            else:
                err_msg = (r.stderr or r.stdout or f"lsregister -u exited {r.returncode}").strip()
                errs.append({"path": p_str, "error": err_msg})
        except Exception as exc:
            errs.append({"path": p_str, "error": str(exc)})
    if done:
        try:
            subprocess.run([lsreg, "-gc"], capture_output=True, text=True, timeout=15)
        except Exception:
            pass
    return done, errs


def verify_candidate_before_stage(
    cand: GCCandidate,
    now_ts: float,
    tracked_index: GitTrackedIndex,
    exiting_pids: frozenset[int] = frozenset(),
) -> tuple[str | None, TreeInspection | None]:
    """Re-verify pin, lease, git, inode identity, and subtree activity right before staging (closes scan->stage TOCTOU).

    Returns (blocking gate or None, the fresh tree inspection so staging does not walk the tree again)."""
    try:
        st_now = cand.path.lstat()
    except FileNotFoundError:
        return "AlreadyGone", None
    except OSError:
        return "Gate03_UnreadableFailClosed", None

    if stat.S_ISLNK(st_now.st_mode):
        return "Gate03_SymlinkGuard", None
    if cand.scan_dev is not None and int(st_now.st_dev) != cand.scan_dev:
        return "Gate01_ConcurrentIdentityChanged", None
    if cand.scan_ino is not None and int(st_now.st_ino) != cand.scan_ino:
        return "Gate01_ConcurrentIdentityChanged", None

    is_dir = stat.S_ISDIR(st_now.st_mode)
    if is_dir and has_pin_marker(cand.path):
        return "Gate02_PinMarker", None
    enclosing = enclosing_entry_gate(cand.path, now_ts)
    if enclosing:
        return enclosing, None

    ti = inspect_tree(cand.path)
    if ti.had_error:
        return "Gate03_UnreadableFailClosed", ti
    if ti.has_mount_or_xdev:
        return "Gate03_MountpointBoundaryGuard", ti
    if ti.has_nested_pin:
        return "Gate02_PinMarker", ti
    if ti.has_nested_git:
        return "Gate03_NestedGitRepoGuard", ti

    if is_dir:
        lease = read_lease(cand.path)
        if lease:
            ignore = exiting_pids if cand.session_end_candidate else frozenset()
            if lease_has_live_owner(lease, ignore_pids=ignore):
                return "Gate01_ActiveProcessLock", ti
            cur_exp = str(lease.get("expires_at") or "")
            if cur_exp and cur_exp != cand.scan_lease_expires_at:
                return "Gate02_ActiveLeaseTTL", ti
            if cur_exp and not cand.session_end_candidate:
                try:
                    exp_dt = datetime.fromisoformat(cur_exp)
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    if datetime.fromtimestamp(now_ts, tz=timezone.utc) < exp_dt:
                        return "Gate02_ActiveLeaseTTL", ti
                except (ValueError, TypeError):
                    pass

    if cand.session_end_candidate and ti.non_lease_files > 0:
        return "Gate01_ConcurrentActivitySinceScan", ti

    if cand.scan_latest_activity is not None and ti.latest_activity > cand.scan_latest_activity + 1e-3:
        return "Gate01_ConcurrentActivitySinceScan", ti

    if tracked_index.has_tracked(cand.path):
        return "Gate03_GitTrackedGuard", ti

    return None, ti


def _gc_report(
    execution: str,
    mode: str,
    projects_root: Path,
    errors: list[dict[str, str]],
    **fields: Any,
) -> dict[str, Any]:
    """GC report with every key always present (early aborts included)."""
    rep: dict[str, Any] = {
        "schema": "agent-governor-gc/v1",
        "version": VERSION,
        "execution": execution,
        "mode": mode,
        "projects_root": str(projects_root),
        "total_candidates": 0,
        "reclaimable_bytes": 0,
        "reclaimable_human": "0 B",
        "categories": {},
        "protected_skips_count": 0,
        "protected_skips": [],
        "manifest": None,
        "candidates": [],
        "ephemeral_simulators": [],
        "launchservices_ghosts": [],
        "spotlight_shields": [],
        "errors": errors,
    }
    rep.update(fields)
    return rep


# A pre-stage `lsof` recheck reuses a system-wide snapshot that is at most this old.
LSOF_RECHECK_MAX_AGE_S = 1.0


def run_gc(args: argparse.Namespace) -> dict[str, Any]:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else Path.home()
    projects_root = resolve_projects_root(args.root, home=home)
    apply, had_conflict = _effective_apply(args)
    mode = args.mode
    lock_errors: list[str] = []

    with acquire_gc_lock(home, non_blocking=(mode == "session-end"), error_out=lock_errors) as locked:
        if apply and not locked:
            lock_path = str(home / ".orca/locks/agent-governor-gc.lock")
            return _gc_report(
                "aborted-lock-error" if lock_errors else "skipped-locked",
                mode,
                projects_root,
                [{"path": lock_path, "error": lock_errors[0]}] if lock_errors else [],
            )

        ref_now = getattr(args, "reference_now", None)
        now_ts = float(ref_now) if ref_now is not None else time.time()
        live_pids, proc_cmds = snapshot_running_processes()
        if not live_pids:
            # Fail-closed: if /bin/ps failed or returned zero processes, never delete anything!
            return _gc_report(
                "aborted-process-check-failed",
                mode,
                projects_root,
                [{"path": "/bin/ps", "error": "Process table snapshot failed; aborting GC fail-closed"}],
            )
        targets = collect_project_targets(projects_root)
        tracked_index = GitTrackedIndex()
        ttl = _resolve_gc_ttls(args)
        # SessionEnd hooks run as children of the exiting agent: its own process chain is not a "live owner"
        exiting_pids = (
            frozenset(collect_ancestor_pids(os.getppid())) if mode == "session-end" else frozenset()
        )

        work_cands, skips = scan_work_ephemeral_candidates(
            projects_dirs=targets,
            now_ts=now_ts,
            scratch_ttl_h=ttl["scratch"],
            tests_ttl_h=ttl["tests"],
            min_idle_h=ttl["min_idle"],
            live_pids=live_pids,
            proc_cmds=proc_cmds,
            tracked_index=tracked_index,
            session_end_only=(mode == "session-end"),
            exiting_pids=exiting_pids,
        )

        xcode_cands: list[GCCandidate] = []
        home_cands: list[GCCandidate] = []
        sim_cands: list[dict[str, Any]] = []
        shield_actions: list[dict[str, str]] = []
        coordination: Path | None = None
        protected_udids: set[str] = set()
        broad_sims = bool(getattr(args, "broad_simulator_names", False))

        if mode != "session-end":
            if not getattr(args, "skip_xcode", False):
                xcode_cands, xcode_skips = scan_xcode_build_ephemerals(
                    home=home,
                    projects_root=projects_root,
                    targets=targets,
                    already_queued={c.path for c in work_cands},
                    now_ts=now_ts,
                    derived_data_ttl_h=ttl["derived_data"],
                    system_derived_data_ttl_h=ttl["system_derived_data"],
                    min_idle_h=ttl["min_idle"],
                    proc_cmds=proc_cmds,
                    tracked_index=tracked_index,
                    workspace_only=bool(args.workspace_only),
                    live_pids=live_pids,
                )
                skips.extend(xcode_skips)

            if not args.workspace_only:
                shield_actions = ensure_build_spotlight_shields(home=home, projects_root=projects_root, apply=apply)
                home_cands = scan_agent_home_ephemerals(
                    home=home,
                    now_ts=now_ts,
                    tmp_ttl_h=ttl["tmp"],
                    cache_ttl_h=ttl["cache"],
                    keep_backups=args.keep_backups,
                    proc_cmds=proc_cmds,
                )
                if not args.skip_simulators:
                    cfg = load_orca_env(home)
                    ai_root = resolve_ai_root(projects_root) or resolve_ai_root(resolve_projects_root(home=home))
                    coordination = ai_root / "Data/SimulatorCoordination" if ai_root else None
                    protected_udids = {
                        u.upper()
                        for u in (cfg.get("PRIMARY_SIMULATOR_UDID", ""), cfg.get("SECONDARY_SIMULATOR_UDID", ""))
                        if u
                    }
                    sim_cands = scan_ephemeral_simulators(
                        now_ts=now_ts,
                        sim_ttl_h=ttl["sim"],
                        coordination=coordination,
                        protected_udids=protected_udids,
                        proc_cmds=proc_cmds,
                        home=home,
                        broad_names=broad_sims,
                    )

        trash_root = Path(args.trash_dir).expanduser().resolve() if getattr(args, "trash_dir", None) else None
        stager = TrashStager(
            trash_root=trash_root,
            run_label=f"gc-{mode}",
            default_home=home,
            manifest_extra={"mode": mode, "projects_root": str(projects_root)},
        )

        # Gate 01 Open-FD (`lsof`) + Gate 05 Same-Volume Atomic Trash
        raw_fs_cands = work_cands + xcode_cands + home_cands
        busy_paths = find_lsof_busy_paths([c.path for c in raw_fs_cands])
        pending: list[GCCandidate] = []
        for cand in raw_fs_cands:
            if cand.path in busy_paths:
                skips.append({"path": str(cand.path), "gate": "Gate01_OpenFileDescriptor"})
            elif not stager.can_stage_same_device(cand.path):
                skips.append({"path": str(cand.path), "gate": "Gate05_CrossDeviceTrashGuard"})
            else:
                pending.append(cand)

        errors: list[dict[str, str]] = []
        all_fs_cands: list[GCCandidate] = []
        ls_ghosts: list[str] = []
        want_ls = mode != "session-end" and not args.workspace_only and not getattr(args, "skip_launchservices", False)
        open_files: OpenFileSnapshot | None = None

        # Pre-stage TOCTOU verification (dry-run reports exactly what --apply would stage)
        for cand in pending:
            if apply:
                # Fresh open-file check right before the rename (snapshot at most LSOF_RECHECK_MAX_AGE_S old)
                if open_files is None or time.monotonic() - open_files.taken_at > LSOF_RECHECK_MAX_AGE_S:
                    open_files = OpenFileSnapshot.capture()
                if open_files.is_busy(cand.path):
                    skips.append({"path": str(cand.path), "gate": "Gate01_OpenFileDescriptor"})
                    continue
            gate, inspection = verify_candidate_before_stage(cand, now_ts, tracked_index, exiting_pids)
            if gate == "AlreadyGone":
                continue
            if gate is not None:
                skips.append({"path": str(cand.path), "gate": gate})
                continue
            if not apply:
                all_fs_cands.append(cand)
                continue
            try:
                stager.stage(
                    cand.path,
                    reason=cand.reason,
                    category=cand.category,
                    size_bytes=cand.size_bytes,
                    forbid_pin_or_git=True,
                    inspection=inspection,
                )
                all_fs_cands.append(cand)
            except OSError as exc:
                errors.append({"path": str(cand.path), "error": str(exc)})

        if apply:
            deleted_simulators = []
            for sim in sim_cands:
                try:
                    # Filesystem staging can take minutes. Recheck ownership, state and idle age.
                    live, commands = snapshot_running_processes()
                    current = (
                        scan_ephemeral_simulators(
                            now_ts,
                            ttl["sim"],
                            protected_udids,
                            commands,
                            home,
                            coordination=coordination,
                            only_udid=sim["udid"],
                            broad_names=broad_sims,
                        )
                        if live
                        else []
                    )
                    if not any(s["udid"] == sim["udid"] and s["name"] == sim["name"] for s in current):
                        skips.append({"path": sim["udid"], "gate": "SimulatorIneligibleAtDelete"})
                        continue
                    with reserve_simulator_for_gc(coordination, sim["udid"]) as reserved:
                        if not reserved:
                            skips.append({"path": sim["udid"], "gate": "SimulatorLeasedAtDelete"})
                            continue
                        r = subprocess.run(
                            ["/usr/bin/xcrun", "simctl", "delete", sim["udid"]],
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                    if r.returncode == 0:
                        deleted_simulators.append(sim)
                        stager.record_external_action(
                            action_type="ephemeral_simulator_delete",
                            target=f"{sim['name']} ({sim['udid']})",
                            reason=f"idle Shutdown ephemeral simulator ({sim['idle_hours']}h >= {ttl['sim']:.0f}h)",
                            metadata=sim,
                        )
                    else:
                        errors.append({"path": sim["udid"], "error": (r.stderr or r.stdout).strip()})
                except Exception as exc:
                    errors.append({"path": sim["udid"], "error": str(exc)})
            sim_cands = deleted_simulators

        if want_ls:
            ls_targets = scan_launchservices_ghost_apps(
                home=home, proc_cmds=proc_cmds, staged_paths=[c.path for c in all_fs_cands]
            )
            if not apply:
                ls_ghosts = ls_targets
            elif ls_targets:
                ls_ghosts, ls_errs = unregister_launchservices_ghost_apps(ls_targets)
                errors.extend(ls_errs)
                if ls_ghosts:
                    stager.record_external_action(
                        action_type="launchservices_unregister",
                        target=f"{len(ls_ghosts)} LaunchServices ghost/build app registration(s)",
                        reason="unregistered ghost/transient build `.app` bundles (`lsregister -u` & `-gc`)",
                        metadata={"paths": ls_ghosts},
                    )

        manifest = stager.write_manifest({"mode": mode, "projects_root": str(projects_root)}) if apply else None
        fs_bytes = sum(c.size_bytes for c in all_fs_cands)
        sim_bytes = sum(int(s["size_bytes"]) for s in sim_cands)

        by_cat: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "bytes": 0})
        for c in all_fs_cands:
            by_cat[c.category]["count"] += 1
            by_cat[c.category]["bytes"] += c.size_bytes
        if sim_cands:
            by_cat["ephemeral_simulator"]["count"] = len(sim_cands)
            by_cat["ephemeral_simulator"]["bytes"] = sim_bytes
        if ls_ghosts:
            by_cat["launchservices_ghost"]["count"] = len(ls_ghosts)
            by_cat["launchservices_ghost"]["bytes"] = 0

        gc_rep = _gc_report(
            "applied" if apply else "dry-run",
            mode,
            projects_root,
            errors,
            total_candidates=len(all_fs_cands) + len(sim_cands) + len(ls_ghosts),
            reclaimable_bytes=fs_bytes + sim_bytes,
            reclaimable_human=format_bytes(fs_bytes + sim_bytes),
            categories=dict(by_cat),
            protected_skips_count=len(skips),
            protected_skips=skips,
            manifest=str(manifest) if manifest else None,
            candidates=[
                {
                    "category": c.category,
                    "path": str(c.path),
                    "size_bytes": c.size_bytes,
                    "size_human": format_bytes(c.size_bytes),
                    "age_hours": c.age_hours,
                    "reason": c.reason,
                }
                for c in all_fs_cands
            ],
            ephemeral_simulators=sim_cands,
            launchservices_ghosts=ls_ghosts,
            spotlight_shields=[x["path"] for x in shield_actions],
        )
        if had_conflict:
            gc_rep["flag_conflict"] = "dry-run overrides --apply; no changes made"
        return gc_rep


# ==============================================================================
# 5. TRIAGE: Read-Only Legacy `work/` & Worktree Stranded Data Analyzer
# ==============================================================================
_ITER_STRIP_RE = re.compile(
    r"(?:[-_](?:20\d{6}|20\d{2}-\d{2}-\d{2}|beta\d+|rc\d+|v\d+(?:\.\d+)*|r\d+|\d{3}-\d{2,3}))+$",
    re.IGNORECASE,
)


def normalize_family_stem(name: str) -> str:
    s = re.sub(r"^20\d{6}[-_]", "", name)
    s = re.sub(r"^20\d{2}-\d{2}-\d{2}[-_]", "", s)
    s = _ITER_STRIP_RE.sub("", s)
    return s or name


def run_triage(args: argparse.Namespace) -> dict[str, Any]:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else Path.home()
    projects_root = resolve_projects_root(args.root, home=home)
    now_ts = time.time()

    project_summaries: list[dict[str, Any]] = []
    grand_legacy_bytes = 0
    grand_legacy_count = 0

    candidates = (
        sorted(p for p in projects_root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
        if projects_root.is_dir()
        else []
    )
    if any((projects_root / z).is_dir() for z in FOUR_ZONES) and (projects_root / "work").is_dir():
        candidates = [projects_root]

    for proj in candidates:
        work_dir = proj / "work"
        if not work_dir.is_dir() or work_dir.is_symlink():
            continue
        legacy_items: list[dict[str, Any]] = []
        families: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for entry in sorted(work_dir.iterdir()):
            if entry.name.startswith(".") or entry.name in WORK_TIERS or entry.name == "README.md":
                continue
            if entry.is_symlink():
                continue
            sz, mtime, fcnt = dir_stats(entry)
            age_d = round(max(0.0, (now_ts - mtime) / 86400.0), 1)
            fam = normalize_family_stem(entry.name)
            rec = {
                "name": entry.name,
                "path": str(entry),
                "family": fam,
                "size_bytes": sz,
                "size_human": format_bytes(sz),
                "files": fcnt,
                "age_days": age_d,
                "pinned": entry.is_dir() and has_pin_marker(entry),
            }
            legacy_items.append(rec)
            families[fam].append(rec)

        if not legacy_items:
            continue

        proj_bytes = sum(x["size_bytes"] for x in legacy_items)
        grand_legacy_bytes += proj_bytes
        grand_legacy_count += len(legacy_items)

        multi_series = []
        for fam, members in sorted(families.items(), key=lambda kv: sum(m["size_bytes"] for m in kv[1]), reverse=True):
            if len(members) >= 2:
                members_sorted = sorted(members, key=lambda m: m["age_days"])
                older_bytes = sum(m["size_bytes"] for m in members_sorted[1:])
                total_fam_bytes = sum(m["size_bytes"] for m in members)
                multi_series.append(
                    {
                        "family": fam,
                        "count": len(members),
                        "total_bytes": total_fam_bytes,
                        "total_human": format_bytes(total_fam_bytes),
                        "newest": members_sorted[0]["name"],
                        "older_candidates_count": len(members_sorted) - 1,
                        "older_candidates_bytes": older_bytes,
                        "older_candidates_human": format_bytes(older_bytes),
                    }
                )

        legacy_items.sort(key=lambda x: x["size_bytes"], reverse=True)
        project_summaries.append(
            {
                "project": proj.name,
                "work_path": str(work_dir),
                "legacy_count": len(legacy_items),
                "legacy_bytes": proj_bytes,
                "legacy_human": format_bytes(proj_bytes),
                "iterative_families": multi_series[:15],
                "largest_items": legacy_items[:15],
            }
        )

    # Also inventory stranded `work/` data inside discovered Git worktrees (hold-data risk visibility)
    ai_root = resolve_ai_root(projects_root)
    worktree_stranded: list[dict[str, Any]] = []
    for wt in discover_worktree_checkouts(ai_root):
        wt_work = wt / "work"
        if not wt_work.is_dir() or wt_work.is_symlink():
            continue
        non_meta = [
            e for e in wt_work.iterdir() if e.name not in (".metadata_never_index", "README.md", ".DS_Store")
        ]
        sz, mtime, fcnt = dir_stats(wt_work)
        if fcnt == 0 or not non_meta:
            continue
        rel_label = str(wt.relative_to(ai_root)) if ai_root and wt.is_relative_to(ai_root) else str(wt)
        worktree_stranded.append(
            {
                "worktree": rel_label,
                "work_path": str(wt_work),
                "size_bytes": sz,
                "size_human": format_bytes(sz),
                "files": fcnt,
                "age_days": round(max(0.0, (now_ts - mtime) / 86400.0), 1),
            }
        )
    worktree_stranded.sort(key=lambda x: x["size_bytes"], reverse=True)

    project_summaries.sort(key=lambda x: x["legacy_bytes"], reverse=True)
    report = {
        "schema": "agent-governor-triage/v1",
        "version": VERSION,
        "note": "Read-only report. Pre-existing untagged `work/<dir>` folders are protected by Grandfathering and never auto-deleted.",
        "projects_root": str(projects_root),
        "total_legacy_items": grand_legacy_count,
        "total_legacy_bytes": grand_legacy_bytes,
        "total_legacy_human": format_bytes(grand_legacy_bytes),
        "projects": project_summaries,
        "worktree_stranded_work": worktree_stranded,
    }

    if getattr(args, "markdown", None):
        md_path = Path(args.markdown).expanduser().resolve()
        md_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Legacy `work/` Triage Report (Grandfathered & Protected)",
            "",
            f"- **Scanned Root**: `{projects_root}`",
            f"- **Protected Legacy Folders**: **{grand_legacy_count}** items (**{format_bytes(grand_legacy_bytes)}**)",
            f"- **Worktrees Holding Local `work/` Data**: **{len(worktree_stranded)}**",
            "- **Policy**: Zero automatic deletion. Promote long-term assets into `work/retained/` or archive/trash obsolete iterative series after review.",
            "",
            "| Project | Legacy Folders | Total Size | Iterative Series Detected |",
            "| :--- | ---: | ---: | ---: |",
        ]
        for ps in project_summaries:
            lines.append(
                f"| `{ps['project']}` | {ps['legacy_count']} | {ps['legacy_human']} | {len(ps['iterative_families'])} |"
            )
        if worktree_stranded:
            lines.extend(
                [
                    "",
                    "## Active Worktrees Holding Local `work/` Data (`hold-data` Pre-Retirement Check)",
                    "| Worktree | `work/` Size | Files | Idle Days |",
                    "| :--- | ---: | ---: | ---: |",
                ]
            )
            for ws in worktree_stranded[:20]:
                lines.append(
                    f"| `{ws['worktree']}` | {ws['size_human']} | {ws['files']} | {ws['age_days']}d |"
                )
        for ps in project_summaries:
            if not ps["iterative_families"] and ps["legacy_bytes"] < 50 * 1024 * 1024:
                continue
            lines.extend(["", f"## `{ps['project']}` ({ps['legacy_human']}, {ps['legacy_count']} folders)"])
            if ps["iterative_families"]:
                lines.append("")
                lines.append("### Multi-Run Iterative Series (Newest vs Older Runs)")
                lines.append("| Series Family | Runs | Total | Newest Run | Older Runs Size |")
                lines.append("| :--- | ---: | ---: | :--- | ---: |")
                for fam in ps["iterative_families"][:10]:
                    lines.append(
                        f"| `{fam['family']}` | {fam['count']} | {fam['total_human']} | `{fam['newest']}` | {fam['older_candidates_human']} |"
                    )
            lines.append("")
            lines.append("### Largest Legacy Folders")
            for item in ps["largest_items"][:8]:
                lines.append(
                    f"- `{item['name']}` — **{item['size_human']}** ({item['files']} files, idle {item['age_days']}d)"
                )
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return report


# ==============================================================================
# 6. RESTORE: Atomic Manifest Preview & Restoration
# ==============================================================================
def reconcile_batch_ledger(batch_or_file: Path) -> tuple[Path, dict[str, Any]]:
    """Load `manifest.json` and/or reconcile interrupted `journal.jsonl` WAL entries after mid-batch crash."""
    if batch_or_file.is_dir():
        batch_dir = batch_or_file
        mpath = batch_dir / "manifest.json"
        jpath = batch_dir / "journal.jsonl"
    elif batch_or_file.name == "journal.jsonl":
        batch_dir = batch_or_file.parent
        mpath = batch_dir / "manifest.json"
        jpath = batch_or_file
    else:
        mpath = batch_or_file
        batch_dir = mpath.parent
        jpath = batch_dir / "journal.jsonl"

    data: dict[str, Any] = {}
    if mpath.is_file():
        try:
            loaded = json.loads(mpath.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    existing_items: list[dict[str, Any]] = list(data.get("items", [])) if isinstance(data.get("items"), list) else []
    known_staged_paths = {str(it.get("staged_path")) for it in existing_items if it.get("staged_path")}
    known_seqs = {int(it["seq"]) for it in existing_items if isinstance(it.get("seq"), int)}

    if jpath.is_file():
        by_seq: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
        try:
            for raw_ln in jpath.read_text(encoding="utf-8", errors="ignore").splitlines():
                ln = raw_ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except json.JSONDecodeError:
                    # Tolerate partial trailing line from mid-write power loss
                    continue
                if not isinstance(rec, dict):
                    continue
                seq = rec.get("seq") or rec.get("index")
                phase = str(rec.get("phase") or "")
                if isinstance(seq, int) and phase:
                    by_seq[seq][phase] = rec
        except OSError:
            pass

        for seq in sorted(by_seq):
            phases = by_seq[seq]
            if seq in known_seqs or "aborted" in phases:
                continue
            if "staged" in phases:
                st_rec = dict(phases["staged"])
                st_rec.pop("phase", None)
                if str(st_rec.get("staged_path")) not in known_staged_paths:
                    existing_items.append(st_rec)
                    if st_rec.get("staged_path"):
                        known_staged_paths.add(str(st_rec["staged_path"]))
                    known_seqs.add(seq)
            elif "planned" in phases:
                pl_rec = phases["planned"]
                sp_raw = pl_rec.get("staged_path")
                op_raw = pl_rec.get("original_path")
                if not sp_raw or not op_raw or str(sp_raw) in known_staged_paths:
                    continue
                sp = Path(str(sp_raw))
                try:
                    sp_st = sp.lstat()
                except OSError:
                    # `os.rename` never completed before crash; source was not moved to `staged_path`
                    continue
                exp_ino = pl_rec.get("src_ino")
                if isinstance(exp_ino, int) and int(sp_st.st_ino) != exp_ino:
                    continue
                recovered_item = {
                    "index": seq,
                    "seq": seq,
                    "category": pl_rec.get("category", "recovered_wal"),
                    "reason": pl_rec.get("reason", "recovered from write-ahead journal"),
                    "original_path": str(op_raw),
                    "staged_path": str(sp_raw),
                    "size_bytes": int(pl_rec.get("size_bytes", 0)),
                    "src_dev": pl_rec.get("src_dev"),
                    "src_ino": exp_ino,
                    "recovered_from_wal": True,
                    "staged_at": pl_rec.get("planned_at"),
                }
                existing_items.append(recovered_item)
                known_staged_paths.add(str(sp_raw))
                known_seqs.add(seq)

    if not mpath.is_file() and not jpath.is_file():
        raise FileNotFoundError(f"Manifest or journal not found: {batch_or_file}")

    if not data:
        data = {
            "schema": "agent-governor-manifest/v1.3",
            "version": VERSION,
            "recovered_from_journal": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    data["items"] = existing_items
    data["total_items"] = len(existing_items)
    data["total_bytes"] = sum(int(r.get("size_bytes", 0)) for r in existing_items)
    ledger_ref = mpath if mpath.is_file() else jpath
    return ledger_ref, data


def find_latest_manifest(trash_root: Path | None = None, include_restored: bool = False) -> Path:
    base = trash_root or (Path.home() / ".Trash")
    batch_dirs: dict[str, tuple[float, Path]] = {}
    for pat in ("agent-governor-*/*/manifest.json", "agent-governor-*/*/journal.jsonl"):
        for fp in base.glob(pat):
            try:
                mt = fp.stat().st_mtime
            except OSError:
                continue
            bdir = fp.parent
            key = str(bdir)
            if key not in batch_dirs or mt > batch_dirs[key][0]:
                batch_dirs[key] = (mt, bdir)

    if not batch_dirs:
        raise FileNotFoundError(f"No agent-governor manifest.json or journal.jsonl found under {base}")

    ordered = [bdir for _, bdir in sorted(batch_dirs.values(), key=lambda x: x[0], reverse=True)]
    if not include_restored:
        for bdir in ordered:
            try:
                ledger_path, payload = reconcile_batch_ledger(bdir)
                if not payload.get("restored_at") and payload.get("items"):
                    return ledger_path
            except Exception:
                continue
    ledger_path, _ = reconcile_batch_ledger(ordered[0])
    return ledger_path


def run_restore(args: argparse.Namespace) -> dict[str, Any]:
    trash_root = Path(args.trash_dir).expanduser().resolve() if getattr(args, "trash_dir", None) else None
    batch_or_manifest = getattr(args, "batch", None) or getattr(args, "manifest", None)
    if getattr(args, "latest", False):
        target_ref = find_latest_manifest(trash_root)
    elif batch_or_manifest:
        target_ref = Path(batch_or_manifest).expanduser().resolve()
    else:
        raise ValueError("Specify a manifest/batch path or pass --latest")

    ledger_path, data = reconcile_batch_ledger(target_ref)
    mpath = (ledger_path.parent / "manifest.json") if ledger_path.name == "journal.jsonl" else ledger_path

    apply, had_conflict = _effective_apply(args)
    dry_run = not apply
    restored: list[str] = []
    skipped: list[dict[str, str]] = []

    for item in data.get("items", []):
        staged = item.get("staged_path")
        orig = item.get("original_path")
        if not staged or not orig:
            skipped.append({"target": str(orig), "reason": "non-filesystem action (e.g. simulator)"})
            continue
        sp = Path(staged)
        op = Path(orig)
        if not (sp.exists() or sp.is_symlink()):
            skipped.append({"target": orig, "reason": "staged item missing"})
            continue
        identity = sp.lstat()
        if (item.get("src_ino", identity.st_ino) != identity.st_ino
                or item.get("src_dev", identity.st_dev) != identity.st_dev):
            skipped.append({"target": orig, "reason": "staged item identity changed"})
            continue
        if op.exists() or op.is_symlink():
            skipped.append({"target": orig, "reason": "destination already exists"})
            continue
        if not dry_run:
            try:
                op.parent.mkdir(parents=True, exist_ok=True)
                os.rename(str(sp), str(op))
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    skipped.append({"target": orig, "reason": f"restore failed: {exc}"})
                    continue
                # Cross-device restore: stage into temporary sibling on target filesystem first, then atomic rename
                ti = inspect_tree(sp)
                if ti.has_mount_or_xdev or ti.had_error:
                    skipped.append({"target": orig, "reason": "cross-device restore blocked (mountpoint or unreadable)"})
                    continue
                tmp_op = op.parent / f".agent-governor-restore-{os.getpid()}-{len(restored)}"
                try:
                    if sp.is_symlink():
                        os.symlink(os.readlink(sp), tmp_op)
                    elif sp.is_dir():
                        shutil.copytree(sp, tmp_op, symlinks=True)
                    else:
                        shutil.copy2(sp, tmp_op, follow_symlinks=False)
                    os.rename(str(tmp_op), str(op))
                    if sp.is_dir() and not sp.is_symlink():
                        shutil.rmtree(sp)
                    else:
                        sp.unlink()
                except OSError as cexc:
                    try:
                        if tmp_op.is_symlink() or tmp_op.is_file():
                            tmp_op.unlink()
                        elif tmp_op.is_dir():
                            shutil.rmtree(tmp_op, ignore_errors=True)
                    except OSError:
                        pass
                    skipped.append({"target": orig, "reason": f"cross-device restore failed cleanly: {cexc}"})
                    continue
        restored.append(orig)

    if not dry_run and restored:
        if not any(it.get("staged_path") and os.path.lexists(it["staged_path"]) for it in data["items"]):
            data["restored_at"] = datetime.now(timezone.utc).isoformat()
        data["restored_count"] = int(data.get("restored_count", 0)) + len(restored)
        _atomic_write_json(mpath, data)

    # Reasons that are not failures: non-filesystem actions and items already restored by an earlier run
    benign = ("non-filesystem action", "staged item missing")
    failed = [s for s in skipped if not str(s.get("reason", "")).startswith(benign)]
    rep_restore: dict[str, Any] = {
        "schema": "agent-governor-restore/v1",
        "version": VERSION,
        "mode": "dry-run" if dry_run else "applied",
        "manifest": str(mpath if mpath.is_file() else ledger_path),
        "restored_count": len(restored),
        "failed_count": len(failed),
        "restored": restored,
        "skipped": skipped,
    }
    if had_conflict:
        rep_restore["flag_conflict"] = "dry-run overrides --apply; no changes made"
    return rep_restore


# ==============================================================================
# Human-Readable Printers & CLI Entrypoint
# ==============================================================================
def print_audit_human(rep: dict[str, Any]) -> None:
    print("=" * 78)
    print(f"agent-governor v{VERSION} — Governance, Context Budget & File Norm Audit")
    print("=" * 78)
    g = rep["global_rules"]
    sync_badge = "IN SYNC" if g["in_sync"] else "DRIFT DETECTED"
    print(f"Global Rules Parity : {sync_badge}")
    for agent, info in g["files"].items():
        if info.get("present"):
            print(
                f"  - {agent:<14}: {info['lines']:>3} lines | {info['bytes']:>5} B | "
                f"~{info['estimated_tokens']:>4} tokens | sha256:{info['sha256_12']}"
            )
    sb = rep["skills_budget"]
    print(
        f"Skills Startup Tax  : {sb['installed_count']} skills (~{sb['startup_frontmatter_tokens_est']} frontmatter tokens)"
    )
    if rep["mcp_servers"]:
        mcp_str = ", ".join(f"{k}={v}" for k, v in rep["mcp_servers"].items())
        print(f"Configured MCPs     : {mcp_str}")
    print(f"Projects Audited    : {rep['projects_audited']} ({rep['projects_root']})")
    print("-" * 78)
    s = rep["summary"]
    if s["attention"]:
        print(f"Attention Required ({s['attention_count']}):")
        for idx, msg in enumerate(s["attention"], 1):
            print(f"  {idx}. {msg}")
    else:
        print("Attention Required  : 0 (All projects & worktrees comply with 4-zone & pointer rules)")
    if s["advice"]:
        print(f"\nOptimization Advice ({s['advice_count']}):")
        for msg in s["advice"][:20]:
            print(f"  - {msg}")
        if len(s["advice"]) > 20:
            print(f"  ... and {len(s['advice']) - 20} more (use --json for full list)")


def print_gc_human(rep: dict[str, Any]) -> None:
    print("=" * 78)
    print(f"agent-governor v{VERSION} — 5-Gate TTL Garbage Collector ({rep['execution'].upper()} / {rep['mode']})")
    print("=" * 78)
    print(f"Total Candidates    : {rep['total_candidates']} ({rep['reclaimable_human']})")
    print(f"Protected by Gates  : {rep['protected_skips_count']} active/pinned items skipped")
    if rep["categories"]:
        print("-" * 78)
        for cat, stats in sorted(rep["categories"].items(), key=lambda kv: kv[1]["bytes"], reverse=True):
            print(f"  {cat:<28} : {stats['count']:>5} items | {format_bytes(stats['bytes']):>10}")
    if rep["manifest"]:
        print("-" * 78)
        print(f"Reversible Trash Manifest : {rep['manifest']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-governor",
        description="Multi-Agent File Norm Governance, Startup Context Optimizer & 5-Gate Ephemeral GC",
    )
    p.add_argument("--version", action="version", version=f"agent-governor {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    # audit
    pa = sub.add_parser("audit", help="Read-only audit of context budgets, 4-zone norms, pointers & root clutter")
    pa.add_argument("--root", default=None, help="Projects root or single project path")
    pa.add_argument("--home", default=None, help="Override HOME directory (for testing)")
    pa.add_argument("--max-global-lines", type=int, default=50)
    pa.add_argument("--max-global-bytes", type=int, default=4500)
    pa.add_argument("--max-project-lines", type=int, default=40)
    pa.add_argument("--max-project-bytes", type=int, default=3000)
    pa.add_argument("--strict", action="store_true", help="Exit 1 on attention items")
    pa.add_argument("--json", action="store_true", help="Output JSON report")

    # adopt
    pf = sub.add_parser("adopt", help="Initialize 4-zone + 3-tier work/ layout, heal pointers & relocate clutter")
    pf.add_argument("--root", default=None, help="Projects root or single project path")
    pf.add_argument("--home", default=None, help="Override HOME directory")
    pf.add_argument(
        "--relocate-clutter",
        action="store_true",
        help="Trash empty root clutter & move non-empty root tmp/ into work/retained/",
    )
    pf.add_argument("--trash-dir", default=None, help="Custom Trash staging root")
    pf.add_argument("--dry-run", action="store_true", help="Preview changes without mutating disk (overrides --apply)")
    pf.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    pf.add_argument("--json", action="store_true", help="Output JSON report")

    # scratch & test-dir
    for zone_cmd, default_ttl, help_txt in (
        ("scratch", 72.0, "Allocate a lease-stamped per-agent directory in work/_scratch/ (default 72h TTL)"),
        ("test-dir", 168.0, "Allocate a lease-stamped per-agent directory in work/_tests/ (default 168h TTL)"),
    ):
        ps = sub.add_parser(zone_cmd, help=help_txt)
        ps.add_argument("--project", default=None, help="Project directory (auto-inferred from cwd if omitted)")
        ps.add_argument("--agent", default=None, help="Agent identifier (e.g. claude, codex, gemini)")
        ps.add_argument("--topic", "--slug", dest="topic", required=True, help="Short task/topic slug")
        ps.add_argument(
            "--ttl-hours", type=float, default=default_ttl, help=f"Retention TTL in hours (default {default_ttl:g})"
        )
        ps.add_argument("--ttl-days", type=float, default=None, help="Optional TTL override in days")
        ps.add_argument("--unique", action="store_true", help="Force unique HHMMSS-pid suffix even for same session")

    # gc (None defaults allow `--mode deep` to tighten TTLs unless explicitly overridden on CLI)
    pg = sub.add_parser("gc", help="Run 5-gate TTL garbage collection into ~/.Trash with reversible manifest")
    pg.add_argument("--root", default=None, help="Projects root or single project path")
    pg.add_argument("--home", default=None, help="Override HOME directory")
    pg.add_argument("--mode", choices=("daily", "session-end", "deep"), default="daily")
    pg.add_argument("--scratch-ttl-days", type=float, default=None, help="TTL days for work/_scratch/* (daily:3, deep:1)")
    pg.add_argument("--tests-ttl-days", type=float, default=None, help="TTL days for work/_tests/* (daily:7, deep:3)")
    pg.add_argument("--cache-ttl-days", type=float, default=None, help="TTL days for agent caches (daily:7, deep:3)")
    pg.add_argument("--tmp-ttl-hours", type=float, default=None, help="TTL hours for agent .tmp/* (daily:24, deep:6)")
    pg.add_argument("--sim-ttl-days", type=float, default=None, help="TTL days for test simulators (daily:3, deep:1)")
    pg.add_argument(
        "--derived-data-ttl-hours",
        type=float,
        default=None,
        help="TTL hours for custom -derivedDataPath trees in work/_scratch, work/_tests and AI/Data (daily:12, deep:4)",
    )
    pg.add_argument(
        "--system-derived-data-ttl-hours",
        type=float,
        default=None,
        help="TTL hours for per-project folders in ~/Library/Developer/Xcode/DerivedData (daily:168, deep:24)",
    )
    pg.add_argument(
        "--keep-backups",
        type=int,
        default=2,
        help="Newest config .bak files to keep per config (default 2, -1 to disable)",
    )
    pg.add_argument("--min-idle-hours", type=float, default=None, help="Minimum idle hours before GC (daily:2, deep:0.5)")
    pg.add_argument("--reference-now", type=float, default=None, help=argparse.SUPPRESS)
    pg.add_argument(
        "--workspace-only",
        action="store_true",
        help="Only scan project/worktree work/_scratch & work/_tests, skip ~/.claude & ~/.codex",
    )
    pg.add_argument("--skip-simulators", action="store_true", help="Skip iOS Simulator checks")
    pg.add_argument(
        "--broad-simulator-names",
        action="store_true",
        help="Also delete idle Shutdown simulators whose names merely look ephemeral (agent-tmp*, ephemeral-*, "
        "temp-sim, ...); default: only simulators created by agent-test-device (agent-tmp-job-<pid>-<id>)",
    )
    pg.add_argument("--skip-xcode", action="store_true", help="Skip Xcode DerivedData & XCTest runner bundle sweep")
    pg.add_argument("--skip-launchservices", action="store_true", help="Skip macOS LaunchServices ghost app unregistration")
    pg.add_argument("--trash-dir", default=None, help="Custom Trash staging root")
    pg.add_argument("--dry-run", action="store_true", help="Preview candidates without mutating disk (overrides --apply)")
    pg.add_argument("--apply", action="store_true", help="Move expired candidates to Trash (default is dry-run)")
    pg.add_argument("--json", action="store_true", help="Output JSON report")

    # triage
    pt = sub.add_parser("triage", help="Read-only inventory & family grouping of grandfathered work/* directories")
    pt.add_argument("--root", default=None, help="Projects root or single project path")
    pt.add_argument("--home", default=None, help="Override HOME directory")
    pt.add_argument(
        "--markdown",
        "--output",
        "-o",
        dest="markdown",
        default=None,
        help="Optional path to write Markdown triage report",
    )
    pt.add_argument("--json", action="store_true", help="Output JSON report")

    # restore
    pr = sub.add_parser("restore", help="Atomically restore files from a GC or adopt Trash manifest.json")
    pr.add_argument("manifest", nargs="?", default=None, help="Path to manifest.json or Trash batch directory")
    pr.add_argument("--batch", default=None, help="Path to Trash batch directory or manifest.json")
    pr.add_argument("--latest", action="store_true", help="Restore the most recent un-restored agent-governor batch")
    pr.add_argument("--trash-dir", default=None, help="Custom Trash root when searching with --latest")
    pr.add_argument("--dry-run", action="store_true", help="Preview items that would be restored (default)")
    pr.add_argument("--apply", action="store_true", help="Apply restoration (moves staged items back to original paths)")
    pr.add_argument("--json", action="store_true", help="Output JSON report")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    try:
        return _dispatch(args)
    except (GovernorError, FileNotFoundError, ValueError) as exc:
        print(f"agent-governor {args.cmd}: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:

    if args.cmd == "audit":
        rep = run_audit(args)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print_audit_human(rep)
        return 1 if (args.strict and not rep["summary"]["healthy"]) else 0

    if args.cmd == "adopt":
        rep = run_adopt(args)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print(f"[agent-governor adopt ({rep['mode']})] {rep['action_count']} action(s) across {rep['projects_root']}")
            for act in rep["actions"][:40]:
                target = act.get("path") or f"{act.get('from')} -> {act.get('to')}"
                print(f"  - [{act['project']}] {act['action']}: {target}")
            if len(rep["actions"]) > 40:
                print(f"  ... and {len(rep['actions']) - 40} more")
            if rep["manifest"]:
                print(f"Trash receipt: {rep['manifest']}")
        return 0

    if args.cmd in ("scratch", "test-dir"):
        zone = "scratch" if args.cmd == "scratch" else "tests"
        target = allocate_ephemeral_dir(zone, args)
        print(str(target))
        return 0

    if args.cmd == "gc":
        rep = run_gc(args)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print_gc_human(rep)
        return 1 if rep.get("errors") else 0

    if args.cmd == "triage":
        rep = run_triage(args)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print("=" * 78)
            print(f"agent-governor v{VERSION} — Grandfathered Legacy `work/` Triage (Read-Only)")
            print("=" * 78)
            print(f"Protected Legacy Items : {rep['total_legacy_items']} ({rep['total_legacy_human']})")
            if rep.get("worktree_stranded_work"):
                print(f"Worktrees w/ `work/`   : {len(rep['worktree_stranded_work'])} active worktree(s)")
            for ps in rep["projects"][:15]:
                print(
                    f"  - {ps['project']:<26}: {ps['legacy_count']:>4} folders | {ps['legacy_human']:>10} | "
                    f"{len(ps['iterative_families'])} multi-run series"
                )
            if args.markdown:
                print(f"\nMarkdown report written to: {args.markdown}")
        return 0

    if args.cmd == "restore":
        rep = run_restore(args)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            verb = "Would restore" if rep["mode"] == "dry-run" else "Restored"
            print(f"[agent-governor restore ({rep['mode']})] {verb} {rep['restored_count']} item(s) from {rep['manifest']}")
            for s in rep["skipped"]:
                print(f"  - skipped {s['target']}: {s['reason']}")
        return 1 if (rep["mode"] == "applied" and rep["failed_count"]) else 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
