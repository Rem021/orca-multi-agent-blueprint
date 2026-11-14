#!/usr/bin/env python3
"""
agent-governor v1.2.0 — Multi-Agent Workspace Governance, Context Optimizer & Ephemeral GC
==========================================================================================
A zero-dependency governance, startup-token optimizer, and 5-gate TTL garbage collection
engine for multi-agent coding environments (Claude Code, OpenAI Codex, Google Gemini /
Antigravity, Cursor, Windsurf, Orca).

Core Capabilities:
  1. `audit`     : Audits startup context token budgets (AGENTS.md / CLAUDE.md / GEMINI.md /
                   SKILL.md frontmatter & 500-line body cap / eager MCP count), code-block-aware
                   link & placeholder checks, 4-zone directory norms (`docs`, `reports`,
                   `outputs`, `work`), `.git/info/exclude` indexing safety, and unauthorized
                   project/worktree root clutter across nested worktrees.
  2. `adopt`     : Non-destructively initializes the 4-zone layout + 3-tier `work/` lifecycle
                   subdirectories (`_scratch`, `_tests`, `retained`), heals `@AGENTS.md`
                   pointers without dirtying `git status`, and safely relocates root clutter.
  3. `scratch`   : Allocates a collision-safe, lease-stamped per-agent scratch directory under
                   `<project>/work/_scratch/<YYYYMMDD>-<agent>-<topic>/` (default 3-day TTL).
  4. `test-dir`  : Allocates a collision-safe, lease-stamped per-agent test output directory
                   under `<project>/work/_tests/<YYYYMMDD>-<agent>-<topic>/` (default 7-day TTL).
  5. `gc`        : Enforces 5 hard safety gates (`fcntl.flock` mutex, creator PID + `ps` +
                   `lsof` open-FD lock, `.keep`/`.pinned` & grandfathering guard, batched
                   `git ls-files` & symlink guard, multi-account harness isolation, primary
                   simulator protection) to sweep expired `work/_scratch`, `work/_tests`,
                   nested worktree ephemerals, multi-account caches, and idle test iOS
                   Simulators into Trash with a reversible `manifest.json` receipt.
  6. `triage`    : Read-only inventory & family grouping of grandfathered `work/*` folders plus
                   stranded `work/` data inside active worktrees.
  7. `restore`   : Atomically previews (`--dry-run`) or restores (`--apply`) any `gc` or
                   `adopt` batch from its `manifest.json`.
"""

from __future__ import annotations

import argparse
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
import subprocess
import sys
import time
from typing import Any, Iterator

VERSION = "1.2.0"

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
    r"^(?:agent[-_ ]?tmp\b|ephemeral[-_ ]|scratch[-_ ]sim\b|temp[-_ ]sim\b|clone[-_ ]of[-_ ]|\[agent-tmp\])"
    r"|(?:[-_ ](?:agent[-_]?tmp|ephemeral[-_]?sim|temp[-_]?sim))$",
    re.IGNORECASE,
)

FENCED_CODE_RE = re.compile(r"(?ms)^[ \t]*(?:```|~~~).*?^[ \t]*(?:```|~~~)[^\n]*\n?")
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
MD_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
UNRESOLVED_TOKEN_RE = re.compile(r"(\{\{[A-Za-z0-9_.-]+\}\}|\$\{[A-Za-z0-9_]+\})")

GLOBAL_DUPLICATE_PHRASES = (
    "git reset --hard",
    "git clean -fdx",
    "git worktree prune",
    "默认中文",
    "一写一树",
)

GC_MODE_DEFAULTS: dict[str, dict[str, float]] = {
    "daily": {
        "scratch_ttl_days": 3.0,
        "tests_ttl_days": 7.0,
        "cache_ttl_days": 7.0,
        "tmp_ttl_hours": 24.0,
        "sim_ttl_days": 3.0,
        "min_idle_hours": 2.0,
    },
    "deep": {
        "scratch_ttl_days": 1.0,
        "tests_ttl_days": 3.0,
        "cache_ttl_days": 3.0,
        "tmp_ttl_hours": 6.0,
        "sim_ttl_days": 1.0,
        "min_idle_hours": 0.5,
    },
    "session-end": {
        "scratch_ttl_days": 3.0,
        "tests_ttl_days": 7.0,
        "cache_ttl_days": 7.0,
        "tmp_ttl_hours": 24.0,
        "sim_ttl_days": 3.0,
        "min_idle_hours": 0.05,
    },
}

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
def load_orca_env(home: Path | None = None) -> dict[str, str]:
    real_home = Path.home()
    h = home or real_home
    cfg: dict[str, str] = {}
    search_files: list[Path] = []
    if h != real_home:
        # Preserve host primary/secondary simulator UDID protection even when --home is overridden
        for rp in (real_home / ".orca/backup-config.env", real_home / ".orca/guard.env"):
            if rp.is_file():
                try:
                    for raw in rp.read_text(encoding="utf-8", errors="ignore").splitlines():
                        s = raw.strip()
                        if s.startswith("export "):
                            s = s[7:].strip()
                        if "=" in s and not s.startswith("#"):
                            rk, rv = s.split("=", 1)
                            rk = rk.strip()
                            if rk in ("PRIMARY_SIMULATOR_UDID", "SECONDARY_SIMULATOR_UDID"):
                                rv = rv.strip().strip('"').strip("'")
                                if rv:
                                    cfg[rk] = rv
                except OSError:
                    pass
    for p in (h / ".orca/backup-config.env", h / ".orca/guard.env"):
        if not p.is_file():
            continue
        try:
            for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'").replace("$HOME", str(h)).replace("${HOME}", str(h))
                if v:
                    cfg[k.strip()] = v
        except OSError:
            pass
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

    stack: list[Path] = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    ename = entry.name
                    if ename in PIN_MARKERS:
                        has_nested_pin = True
                    if ename == ".git":
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
                            stack.append(ep)
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
def acquire_gc_lock(home: Path, non_blocking: bool = False, timeout_s: float = 15.0) -> Iterator[bool]:
    """Serialize GC mutations across simultaneous SessionEnd hooks and scheduled runs."""
    lock_dir = home / ".orca/locks"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        fh = (lock_dir / "agent-governor-gc.lock").open("w", encoding="utf-8")
    except OSError:
        yield True
        return

    acquired = False
    deadline = time.monotonic() + max(0.0, timeout_s)
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    acquired = True
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


def snapshot_running_processes() -> tuple[set[int], list[str]]:
    pids: set[int] = set()
    cmds: list[str] = []
    try:
        r = subprocess.run(
            ["/bin/ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            my_pid = os.getpid()
            for line in r.stdout.splitlines():
                s = line.strip()
                if not s:
                    continue
                parts = s.split(None, 1)
                if parts and parts[0].isdigit():
                    pid = int(parts[0])
                    if pid != my_pid:
                        pids.add(pid)
                        if len(parts) > 1:
                            cmds.append(parts[1])
    except Exception:
        pass
    return pids, cmds


_GUI_HOST_COMMS = ("Orca", "Terminal", "iTerm2", "WindowServer", "launchd", "login", "tmux", "sshd")


def collect_ancestor_pids(start_pid: int | None, max_hops: int = 8) -> list[int]:
    """Walk parent PIDs above transient subshells up to the owning agent/shell session (stopping before GUI hosts)."""
    if not start_pid or start_pid <= 1:
        return []
    chain: list[int] = [int(start_pid)]
    cur = int(start_pid)
    for _ in range(max_hops):
        try:
            r = subprocess.run(
                ["/bin/ps", "-o", "ppid=,comm=", "-p", str(cur)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if r.returncode != 0 or not r.stdout.strip():
                break
            parts = r.stdout.strip().split(None, 1)
            if not parts or not parts[0].isdigit():
                break
            ppid = int(parts[0])
            if ppid <= 1 or ppid in chain:
                break
            # Check parent's comm so we stop before desktop multiplexer apps like Orca.app / Terminal.app
            rp = subprocess.run(
                ["/bin/ps", "-o", "comm=", "-p", str(ppid)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            pcomm = (rp.stdout or "").strip()
            pbase = os.path.basename(pcomm)
            if any(pbase == h or pbase.startswith(h + ".") or f"/{h}.app/" in pcomm for h in _GUI_HOST_COMMS):
                break
            chain.append(ppid)
            cur = ppid
        except Exception:
            break
    return chain


def pid_alive(pid: int | None, live_pids: set[int] | None = None) -> bool:
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


def lease_has_live_owner(lease: dict[str, Any], live_pids: set[int] | None = None) -> bool:
    raw_pids: list[int] = []
    p = lease.get("pid")
    if isinstance(p, int) and p > 1:
        raw_pids.append(p)
    anc = lease.get("ancestor_pids")
    if isinstance(anc, list):
        for ap in anc:
            if isinstance(ap, int) and ap > 1 and ap not in raw_pids:
                raw_pids.append(ap)
    return any(pid_alive(cand_pid, live_pids) for cand_pid in raw_pids)


def path_used_by_processes(path: Path, cmds: list[str]) -> bool:
    needle = str(path)
    return any(needle in c for c in cmds)


def find_lsof_busy_paths(paths: list[Path]) -> set[Path]:
    """Verify via `lsof -Fn` that no process holds an open file descriptor or cwd inside `paths` (fail-closed on timeout/error)."""
    if not paths:
        return set()
    lsof_bin = "/usr/sbin/lsof" if Path("/usr/sbin/lsof").is_file() else shutil.which("lsof")
    if not lsof_bin:
        return set(paths)

    existing = [p for p in paths if p.exists()]
    if not existing:
        return set()

    cand_pairs: list[tuple[Path, str, str]] = []
    for p in existing:
        raw_s = str(p)
        try:
            res_s = str(p.resolve())
        except OSError:
            res_s = raw_s
        cand_pairs.append((p, raw_s, res_s))

    open_names: list[str] = []
    busy: set[Path] = set()
    chunk_size = 64
    for idx in range(0, len(existing), chunk_size):
        chunk_paths = existing[idx : idx + chunk_size]
        chunk = [str(p) for p in chunk_paths]
        try:
            r = subprocess.run(
                [lsof_bin, "-Fn", "-w", "--", *chunk],
                capture_output=True,
                text=True,
                timeout=8,
            )
            # Note: lsof returns 1 when no open files match; negative returncode (signal) or exception is a failure
            if r.returncode < 0:
                busy.update(chunk_paths)
                continue
            for line in r.stdout.splitlines():
                if line.startswith("n") and len(line) > 1:
                    open_names.append(line[1:])
        except Exception:
            # Fail-closed: if lsof times out or errors on a chunk, treat all candidates in that chunk as busy
            busy.update(chunk_paths)

    if not open_names:
        return busy

    for orig, raw_s, res_s in cand_pairs:
        raw_prefix = raw_s.rstrip("/") + "/"
        res_prefix = res_s.rstrip("/") + "/"
        for oname in open_names:
            if oname == raw_s or oname == res_s or oname.startswith(raw_prefix) or oname.startswith(res_prefix):
                busy.add(orig)
                break
    return busy


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
            if rel == "":
                return bool(tracked)
            if rel in tracked:
                return True
            prefix = rel.rstrip("/") + "/"
            return any(f.startswith(prefix) for f in tracked)
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


class TrashStager:
    def __init__(self, trash_root: Path | None = None, run_label: str = "gc", default_home: Path | None = None):
        self._explicit_trash_root = trash_root is not None
        self._base = trash_root or ((default_home or Path.home()) / ".Trash")
        self._day = datetime.now().strftime("%Y-%m-%d")
        stamp = datetime.now().strftime("%H%M%S") + f"-{os.getpid()}"
        self._run_id = f"{run_label}-{stamp}"
        self.batch_dir = self._base / f"agent-governor-{self._day}" / self._run_id
        self.items_dir = self.batch_dir / "items"
        self.manifest_path = self.batch_dir / "manifest.json"
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

    def stage(self, path: Path, reason: str, category: str, size_bytes: int = 0) -> Path:
        if path.is_dir() and not path.is_symlink():
            ti = inspect_tree(path)
            if ti.has_mount_or_xdev:
                raise OSError(errno.EXDEV, f"Refusing to stage directory containing mountpoint/subvolume: {path}")
            if ti.had_error:
                raise OSError(errno.EACCES, f"Refusing to stage unreadable subtree: {path}")

        items_dir = self._target_items_dir(path)
        if items_dir is None:
            raise OSError(
                errno.EXDEV,
                f"Refusing cross-device Trash copy+rmtree for {path}; no writable same-volume Trash available",
            )
        items_dir.mkdir(parents=True, exist_ok=True)
        if path.lstat().st_dev != items_dir.stat().st_dev:
            raise OSError(errno.EXDEV, f"Cross-device mismatch staging {path} -> {items_dir}")

        idx = len(self.records) + 1
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name)[:80] or "item"
        dest = items_dir / f"{idx:04d}_{safe_name}"
        seq = 2
        while dest.exists():
            dest = items_dir / f"{idx:04d}_{seq}_{safe_name}"
            seq += 1

        # Atomic same-filesystem rename: never traverses, copies, or unlinks read-only/mounted children
        os.rename(str(path), str(dest))
        rec = {
            "index": idx,
            "category": category,
            "reason": reason,
            "original_path": str(path),
            "staged_path": str(dest),
            "size_bytes": size_bytes,
            "staged_at": datetime.now(timezone.utc).isoformat(),
        }
        self.records.append(rec)
        return dest

    def record_relocation(self, original: Path, relocated_to: Path, reason: str, size_bytes: int = 0) -> None:
        self.records.append(
            {
                "index": len(self.records) + 1,
                "category": "relocated_root_clutter",
                "reason": reason,
                "original_path": str(original),
                "staged_path": str(relocated_to),
                "size_bytes": size_bytes,
                "staged_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def record_external_action(self, action_type: str, target: str, reason: str, metadata: dict[str, Any]) -> None:
        self.batch_dir.mkdir(parents=True, exist_ok=True)
        self.records.append(
            {
                "index": len(self.records) + 1,
                "category": action_type,
                "reason": reason,
                "original_path": target,
                "staged_path": None,
                "size_bytes": 0,
                "metadata": metadata,
                "staged_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def write_manifest(self, extra: dict[str, Any] | None = None) -> Path | None:
        if not self.records:
            return None
        self.batch_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "agent-governor-manifest/v1",
            "version": VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "total_items": len(self.records),
            "total_bytes": sum(int(r.get("size_bytes", 0)) for r in self.records),
            **(extra or {}),
            "items": self.records,
        }
        self.manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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
            txt = ct.read_text(encoding="utf-8", errors="ignore")
            counts["codex_global"] = len(re.findall(r"^\[mcp_servers\.[^\]]+\]", txt, flags=re.MULTILINE))
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
        dups = [p for p in GLOBAL_DUPLICATE_PHRASES if p in prose_txt]
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
        ("orca_agy_chat", "Library/Application Support/orca/antigravity-chat/GEMINI.md"),
        ("orca_agy_acp", "Library/Application Support/orca/antigravity-chat/antigravity-acp/GEMINI.md"),
    ):
        ep = home / extra_rel
        if ep.is_file():
            global_files[extra_label] = ep

    global_report: dict[str, Any] = {}
    hashes: set[str] = set()
    for agent, gf in global_files.items():
        if gf.is_file():
            txt = gf.read_text(encoding="utf-8", errors="ignore")
            sha = hashlib.sha256(txt.encode("utf-8")).hexdigest()[:12]
            hashes.add(sha)
            global_report[agent] = {
                "path": str(gf),
                "present": True,
                "lines": len(txt.splitlines()),
                "bytes": len(txt.encode("utf-8")),
                "estimated_tokens": estimate_tokens(txt),
                "sha256_12": sha,
            }
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

    # 3. MCP count
    mcp_counts = count_mcp_servers(home)

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
def run_adopt(args: argparse.Namespace) -> dict[str, Any]:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else Path.home()
    projects_root = resolve_projects_root(args.root, home=home)
    apply = bool(args.apply)
    trash_root = Path(args.trash_dir).expanduser().resolve() if getattr(args, "trash_dir", None) else None
    stager = TrashStager(trash_root=trash_root, run_label="adopt", default_home=home)
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
                        reloc_base = proj / "work/retained" / f"from-root-{cname.lstrip('.') or cname}-{today}"
                        if cname.startswith("."):
                            reloc_base = proj / "work/retained" / f"from-root-{cname}-{today}"
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
                            reloc_dest.parent.mkdir(parents=True, exist_ok=True)
                            os.rename(str(child), str(reloc_dest))
                            stager.record_relocation(
                                child,
                                reloc_dest,
                                reason=f"relocated non-empty root `{cname}/` into work/retained/",
                                size_bytes=ti.total_bytes,
                            )

    manifest = stager.write_manifest({"mode": "adopt", "projects_root": str(projects_root)}) if apply else None
    return {
        "mode": "apply" if apply else "dry-run",
        "projects_root": str(projects_root),
        "action_count": len(actions),
        "manifest": str(manifest) if manifest else None,
        "actions": actions,
    }


# ==============================================================================
# 3. SCRATCH & TEST-DIR ALLOCATOR: Collision-Safe Lease-Stamped Directories
# ==============================================================================
def infer_project_dir(explicit_project: str | None, cwd: Path | None = None) -> Path:
    start = Path(explicit_project).expanduser().resolve() if explicit_project else (cwd or Path.cwd()).resolve()
    if start.is_file():
        start = start.parent
    for cand in [start, *start.parents]:
        if (cand / "work").is_dir() or (cand / ".git").exists() or (cand / "AGENTS.md").is_file():
            return cand
    return start


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
    explicit_pid = getattr(args, "pid", None)
    raw_ppid = int(explicit_pid) if explicit_pid is not None else os.getppid()
    ancestor_pids = [raw_ppid] if explicit_pid is not None else collect_ancestor_pids(raw_ppid)
    # Prefer the highest non-GUI session ancestor (e.g. persistent claude/codex/shell) over transient `$(...)` subshell
    creator_pid = ancestor_pids[-1] if ancestor_pids else raw_ppid
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
    target = tier_dir / base_name
    if target.exists():
        need_disambiguate = bool(args.unique)
        if not need_disambiguate:
            existing_lease = read_lease(target)
            existing_pid = existing_lease.get("pid") if isinstance(existing_lease.get("pid"), int) else None
            existing_anc = existing_lease.get("ancestor_pids") if isinstance(existing_lease.get("ancestor_pids"), list) else []
            existing_sid = str(existing_lease.get("session_id") or "")
            same_lineage = bool(set(ancestor_pids) & ({existing_pid} | {x for x in existing_anc if isinstance(x, int)}))
            if existing_sid and session_id and existing_sid != session_id:
                need_disambiguate = True
            elif not same_lineage and lease_has_live_owner(existing_lease):
                need_disambiguate = True
        if need_disambiguate:
            stamp = datetime.now().strftime("%H%M%S")
            cand = tier_dir / f"{base_name}-{stamp}-{creator_pid}"
            seq = 2
            while cand.exists():
                cand = tier_dir / f"{base_name}-{stamp}-{creator_pid}-{seq}"
                seq += 1
            target = cand

    target.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    lease = {
        "schema": "agent-governor-lease/v1.2",
        "zone": zone,
        "project": str(proj),
        "agent": agent,
        "topic": topic,
        "pid": creator_pid,
        "ancestor_pids": ancestor_pids,
        "session_id": session_id,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(),
        "ttl_hours": ttl_hours,
    }
    (target / ".agent-lease.json").write_text(json.dumps(lease, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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


def has_pin_marker(directory: Path) -> bool:
    return any((directory / marker).exists() for marker in PIN_MARKERS)


def read_lease(directory: Path) -> dict[str, Any]:
    lf = directory / ".agent-lease.json"
    if not lf.is_file():
        return {}
    try:
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
) -> tuple[list[GCCandidate], list[dict[str, str]]]:
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

                is_dir_entry = False
                try:
                    is_dir_entry = entry.is_dir()
                except OSError:
                    protected_skips.append({"path": str(entry), "gate": "Gate03_UnreadableFailClosed"})
                    continue

                if is_dir_entry and has_pin_marker(entry):
                    protected_skips.append({"path": str(entry), "gate": "Gate02_PinMarker"})
                    continue

                lease = read_lease(entry) if is_dir_entry else {}
                if lease_has_live_owner(lease, live_pids) or path_used_by_processes(entry, proc_cmds):
                    protected_skips.append({"path": str(entry), "gate": "Gate01_ActiveProcessLock"})
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

    codex_homes = [home / ".codex"]
    if (orca_dir / "codex-accounts").is_dir():
        codex_homes.extend(sorted((orca_dir / "codex-accounts").glob("*/home")))

    claude_homes = [home / ".claude"]
    if (orca_dir / "claude-profiles").is_dir():
        claude_homes.extend(sorted((orca_dir / "claude-profiles").glob("*/home")))

    def _safe_children(d: Path) -> list[Path]:
        try:
            if not d.is_dir() or d.is_symlink():
                return []
            return sorted(d.iterdir())
        except OSError:
            return []

    def _safe_stats(item: Path) -> tuple[int, float, int] | None:
        ti = inspect_tree(item)
        if ti.had_error or ti.has_mount_or_xdev or ti.has_nested_pin or ti.has_nested_git:
            return None
        return ti.total_bytes, ti.latest_activity, ti.file_count

    # 1. Codex internal .tmp, tmp, shell_snapshots, stale runtime log files
    for ch in codex_homes:
        if not ch.is_dir() or ch.is_symlink():
            continue
        for tdir_name in (".tmp", "tmp"):
            tdir = ch / tdir_name
            for item in _safe_children(tdir):
                # Never reap Codex live argv0 dispatch helper shims (`~/.codex/tmp/arg0*`)
                if item.name.startswith("arg0") or item.is_symlink() or path_used_by_processes(item, proc_cmds):
                    continue
                st_res = _safe_stats(item)
                if st_res is None:
                    continue
                sz, act_ts, _ = st_res
                age_h = max(0.0, (now_ts - act_ts) / 3600.0)
                if age_h >= tmp_ttl_h:
                    candidates.append(
                        GCCandidate(
                            category="codex_tmp",
                            path=item,
                            size_bytes=sz,
                            age_hours=round(age_h, 1),
                            reason=f"Codex {tdir_name} artifact idle {age_h:.1f}h >= {tmp_ttl_h:.0f}h",
                        )
                    )
        for item in _safe_children(ch / "shell_snapshots"):
            if item.is_symlink() or path_used_by_processes(item, proc_cmds):
                continue
            st_res = _safe_stats(item)
            if st_res is None:
                continue
            sz, act_ts, _ = st_res
            age_h = max(0.0, (now_ts - act_ts) / 3600.0)
            if age_h >= cache_ttl_h:
                candidates.append(
                    GCCandidate(
                        category="codex_shell_snapshot",
                        path=item,
                        size_bytes=sz,
                        age_hours=round(age_h, 1),
                        reason=f"Codex shell snapshot idle {age_h / 24.0:.1f}d",
                    )
                )
        for item in _safe_children(ch / "log"):
            if item.is_symlink() or path_used_by_processes(item, proc_cmds):
                continue
            st_res = _safe_stats(item)
            if st_res is None:
                continue
            sz, act_ts, _ = st_res
            age_h = max(0.0, (now_ts - act_ts) / 3600.0)
            if age_h >= cache_ttl_h * 2:
                candidates.append(
                    GCCandidate(
                        category="codex_log",
                        path=item,
                        size_bytes=sz,
                        age_hours=round(age_h, 1),
                        reason=f"Codex runtime log idle {age_h / 24.0:.1f}d",
                    )
                )

    # 2. Claude internal shell-snapshots, paste-cache, telemetry, empty session-env
    for clh in claude_homes:
        if not clh.is_dir() or clh.is_symlink():
            continue
        for sub_name, cat_name in (
            ("shell-snapshots", "claude_shell_snapshot"),
            ("paste-cache", "claude_paste_cache"),
            ("telemetry", "claude_telemetry"),
        ):
            for item in _safe_children(clh / sub_name):
                if item.is_symlink() or path_used_by_processes(item, proc_cmds):
                    continue
                st_res = _safe_stats(item)
                if st_res is None:
                    continue
                sz, act_ts, _ = st_res
                age_h = max(0.0, (now_ts - act_ts) / 3600.0)
                if age_h >= cache_ttl_h:
                    candidates.append(
                        GCCandidate(
                            category=cat_name,
                            path=item,
                            size_bytes=sz,
                            age_hours=round(age_h, 1),
                            reason=f"Claude {sub_name} idle {age_h / 24.0:.1f}d",
                        )
                    )
        for item in _safe_children(clh / "session-env"):
            if not item.is_dir() or item.is_symlink() or path_used_by_processes(item, proc_cmds):
                continue
            st_res = _safe_stats(item)
            if st_res is None:
                continue
            sz, act_ts, cnt = st_res
            age_h = max(0.0, (now_ts - act_ts) / 3600.0)
            if (cnt == 0 and age_h >= tmp_ttl_h) or age_h >= cache_ttl_h * 2:
                candidates.append(
                    GCCandidate(
                        category="claude_session_env",
                        path=item,
                        size_bytes=sz,
                        age_hours=round(age_h, 1),
                        reason=f"stale Claude session-env ({cnt} files, idle {age_h / 24.0:.1f}d)",
                    )
                )

    # 3. Orca chat-scratch & all Antigravity brain/*/scratch + log roots
    for item in _safe_children(orca_dir / "chat-scratch"):
        if item.is_symlink() or path_used_by_processes(item, proc_cmds):
            continue
        st_res = _safe_stats(item)
        if st_res is None:
            continue
        sz, act_ts, _ = st_res
        age_h = max(0.0, (now_ts - act_ts) / 3600.0)
        if age_h >= cache_ttl_h * 2:
            candidates.append(
                GCCandidate(
                    category="orca_chat_scratch",
                    path=item,
                    size_bytes=sz,
                    age_hours=round(age_h, 1),
                    reason=f"Orca chat-scratch idle {age_h / 24.0:.1f}d",
                )
            )

    agy_brains = (
        orca_dir / "antigravity-chat/antigravity-acp/brain",
        orca_dir / "antigravity-chat/antigravity-cli/brain",
        home / ".gemini/antigravity/brain",
        home / ".gemini/antigravity-cli/brain",
    )
    for agy_brain in agy_brains:
        for conv_dir in _safe_children(agy_brain):
            if not conv_dir.is_dir() or conv_dir.is_symlink():
                continue
            for item in _safe_children(conv_dir / "scratch"):
                if item.is_symlink() or path_used_by_processes(item, proc_cmds):
                    continue
                st_res = _safe_stats(item)
                if st_res is None:
                    continue
                sz, act_ts, _ = st_res
                age_h = max(0.0, (now_ts - act_ts) / 3600.0)
                if age_h >= cache_ttl_h:
                    candidates.append(
                        GCCandidate(
                            category="antigravity_scratch",
                            path=item,
                            size_bytes=sz,
                            age_hours=round(age_h, 1),
                            reason=f"Antigravity scratch artifact idle {age_h / 24.0:.1f}d",
                        )
                    )

    for agy_log in (
        orca_dir / "antigravity-chat/antigravity-cli/log",
        home / ".gemini/antigravity-cli/log",
    ):
        for item in _safe_children(agy_log):
            if item.is_symlink() or path_used_by_processes(item, proc_cmds):
                continue
            st_res = _safe_stats(item)
            if st_res is None:
                continue
            sz, act_ts, _ = st_res
            age_h = max(0.0, (now_ts - act_ts) / 3600.0)
            if age_h >= cache_ttl_h * 2:
                candidates.append(
                    GCCandidate(
                        category="antigravity_log",
                        path=item,
                        size_bytes=sz,
                        age_hours=round(age_h, 1),
                        reason=f"Antigravity log idle {age_h / 24.0:.1f}d",
                    )
                )

    # 4. Config backup file retention (`*.bak*`, `*.backup*`) in agent config dirs
    if keep_backups >= 0:
        for cfg_dir in [*codex_homes, *claude_homes, home / ".gemini"]:
            grouped: dict[str, list[tuple[float, int, Path]]] = defaultdict(list)
            for item in _safe_children(cfg_dir):
                if not item.is_file() or item.is_symlink():
                    continue
                for pat in BACKUP_FILE_PATTERNS:
                    m = pat.match(item.name)
                    if m:
                        st = item.lstat()
                        grouped[m.group(1)].append((st.st_mtime, st.st_size, item))
                        break
            for base_name, entries in grouped.items():
                entries.sort(key=lambda x: x[0], reverse=True)
                for mtime, sz, old_bak in entries[keep_backups:]:
                    age_h = max(0.0, (now_ts - mtime) / 3600.0)
                    candidates.append(
                        GCCandidate(
                            category="excess_config_backup",
                            path=old_bak,
                            size_bytes=sz,
                            age_hours=round(age_h, 1),
                            reason=f"superseded backup of {base_name} (retaining newest {keep_backups})",
                        )
                    )

    return candidates


def scan_ephemeral_simulators(
    now_ts: float,
    sim_ttl_h: float,
    protected_udids: set[str],
    proc_cmds: list[str],
    home: Path | None = None,
) -> list[dict[str, Any]]:
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
            if not udid or udid in protected_udids:
                continue
            if state != "Shutdown":
                continue
            if not EPHEMERAL_SIM_RE.search(name):
                continue
            if any(udid in c for c in proc_cmds):
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


def _resolve_gc_ttls(args: argparse.Namespace) -> tuple[float, float, float, float, float, float]:
    defaults = GC_MODE_DEFAULTS.get(args.mode, GC_MODE_DEFAULTS["daily"])
    scratch_d = args.scratch_ttl_days if args.scratch_ttl_days is not None else defaults["scratch_ttl_days"]
    tests_d = args.tests_ttl_days if args.tests_ttl_days is not None else defaults["tests_ttl_days"]
    cache_d = args.cache_ttl_days if args.cache_ttl_days is not None else defaults["cache_ttl_days"]
    tmp_h = args.tmp_ttl_hours if args.tmp_ttl_hours is not None else defaults["tmp_ttl_hours"]
    sim_d = args.sim_ttl_days if args.sim_ttl_days is not None else defaults["sim_ttl_days"]
    idle_h = args.min_idle_hours if args.min_idle_hours is not None else defaults["min_idle_hours"]
    return (
        float(scratch_d) * 24.0,
        float(tests_d) * 24.0,
        float(cache_d) * 24.0,
        float(tmp_h),
        float(sim_d) * 24.0,
        float(idle_h),
    )


def run_gc(args: argparse.Namespace) -> dict[str, Any]:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else Path.home()
    projects_root = resolve_projects_root(args.root, home=home)
    apply = bool(args.apply)
    mode = args.mode

    with acquire_gc_lock(home, non_blocking=(mode == "session-end")) as locked:
        if apply and not locked:
            return {
                "schema": "agent-governor-gc/v1",
                "version": VERSION,
                "execution": "skipped-locked",
                "mode": mode,
                "projects_root": str(projects_root),
                "total_candidates": 0,
                "reclaimable_bytes": 0,
                "reclaimable_human": "0 B",
                "categories": {},
                "protected_skips_count": 0,
                "manifest": None,
                "candidates": [],
                "ephemeral_simulators": [],
                "errors": [],
            }

        ref_now = getattr(args, "reference_now", None)
        now_ts = float(ref_now) if ref_now is not None else time.time()
        live_pids, proc_cmds = snapshot_running_processes()
        if not live_pids:
            # Fail-closed: if /bin/ps failed or returned zero processes, never delete anything!
            return {
                "schema": "agent-governor-gc/v1",
                "version": VERSION,
                "execution": "aborted-process-check-failed",
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
                "errors": [{"path": "/bin/ps", "error": "Process table snapshot failed; aborting GC fail-closed"}],
            }
        targets = collect_project_targets(projects_root)
        tracked_index = GitTrackedIndex()

        scratch_ttl_h, tests_ttl_h, cache_ttl_h, tmp_ttl_h, sim_ttl_h, min_idle_h = _resolve_gc_ttls(args)

        work_cands, skips = scan_work_ephemeral_candidates(
            projects_dirs=targets,
            now_ts=now_ts,
            scratch_ttl_h=scratch_ttl_h,
            tests_ttl_h=tests_ttl_h,
            min_idle_h=min_idle_h,
            live_pids=live_pids,
            proc_cmds=proc_cmds,
            tracked_index=tracked_index,
            session_end_only=(mode == "session-end"),
        )

        home_cands: list[GCCandidate] = []
        sim_cands: list[dict[str, Any]] = []

        if mode != "session-end" and not args.workspace_only:
            home_cands = scan_agent_home_ephemerals(
                home=home,
                now_ts=now_ts,
                tmp_ttl_h=tmp_ttl_h,
                cache_ttl_h=cache_ttl_h,
                keep_backups=args.keep_backups,
                proc_cmds=proc_cmds,
            )
            if not args.skip_simulators:
                cfg = load_orca_env(home)
                protected_udids = {
                    u
                    for u in (
                        cfg.get("PRIMARY_SIMULATOR_UDID", ""),
                        cfg.get("SECONDARY_SIMULATOR_UDID", ""),
                    )
                    if u
                }
                sim_cands = scan_ephemeral_simulators(
                    now_ts=now_ts,
                    sim_ttl_h=sim_ttl_h,
                    protected_udids=protected_udids,
                    proc_cmds=proc_cmds,
                    home=home,
                )

        trash_root = Path(args.trash_dir).expanduser().resolve() if getattr(args, "trash_dir", None) else None
        stager = TrashStager(trash_root=trash_root, run_label=f"gc-{mode}", default_home=home)

        # Gate 01 Open-FD (`lsof`) + Gate 05 Same-Volume Atomic Trash verification
        raw_fs_cands = work_cands + home_cands
        busy_paths = find_lsof_busy_paths([c.path for c in raw_fs_cands])
        all_fs_cands: list[GCCandidate] = []
        for cand in raw_fs_cands:
            if cand.path in busy_paths:
                skips.append({"path": str(cand.path), "gate": "Gate01_OpenFileDescriptor"})
            elif not stager.can_stage_same_device(cand.path):
                skips.append({"path": str(cand.path), "gate": "Gate05_CrossDeviceTrashGuard"})
            else:
                all_fs_cands.append(cand)

        errors: list[dict[str, str]] = []

        if apply:
            for cand in all_fs_cands:
                if not cand.path.exists():
                    continue
                try:
                    stager.stage(cand.path, reason=cand.reason, category=cand.category, size_bytes=cand.size_bytes)
                except OSError as exc:
                    errors.append({"path": str(cand.path), "error": str(exc)})

            for sim in sim_cands:
                try:
                    r = subprocess.run(
                        ["/usr/bin/xcrun", "simctl", "delete", sim["udid"]],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if r.returncode == 0:
                        stager.record_external_action(
                            action_type="ephemeral_simulator_delete",
                            target=f"{sim['name']} ({sim['udid']})",
                            reason=f"idle Shutdown ephemeral simulator ({sim['idle_hours']}h >= {sim_ttl_h:.0f}h)",
                            metadata=sim,
                        )
                    else:
                        errors.append({"path": sim["udid"], "error": (r.stderr or r.stdout).strip()})
                except Exception as exc:
                    errors.append({"path": sim["udid"], "error": str(exc)})

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

        return {
            "schema": "agent-governor-gc/v1",
            "version": VERSION,
            "execution": "applied" if apply else "dry-run",
            "mode": mode,
            "projects_root": str(projects_root),
            "total_candidates": len(all_fs_cands) + len(sim_cands),
            "reclaimable_bytes": fs_bytes + sim_bytes,
            "reclaimable_human": format_bytes(fs_bytes + sim_bytes),
            "categories": dict(by_cat),
            "protected_skips_count": len(skips),
            "protected_skips": skips,
            "manifest": str(manifest) if manifest else None,
            "candidates": [
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
            "ephemeral_simulators": sim_cands,
            "errors": errors,
        }


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
def find_latest_manifest(trash_root: Path | None = None, include_restored: bool = False) -> Path:
    base = trash_root or (Path.home() / ".Trash")
    manifests = sorted(base.glob("agent-governor-*/*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not manifests:
        raise FileNotFoundError(f"No agent-governor manifest.json found under {base}")
    if not include_restored:
        for mp in manifests:
            try:
                payload = json.loads(mp.read_text(encoding="utf-8"))
                if not payload.get("restored_at"):
                    return mp
            except Exception:
                continue
    return manifests[0]


def run_restore(args: argparse.Namespace) -> dict[str, Any]:
    trash_root = Path(args.trash_dir).expanduser().resolve() if getattr(args, "trash_dir", None) else None
    batch_or_manifest = getattr(args, "batch", None) or getattr(args, "manifest", None)
    if getattr(args, "latest", False):
        mpath = find_latest_manifest(trash_root)
    elif batch_or_manifest:
        cand = Path(batch_or_manifest).expanduser().resolve()
        mpath = (cand / "manifest.json") if cand.is_dir() else cand
    else:
        raise ValueError("Specify a manifest/batch path or pass --latest")
    if not mpath.is_file():
        raise FileNotFoundError(f"Manifest not found: {mpath}")

    dry_run = not bool(getattr(args, "apply", False))
    data = json.loads(mpath.read_text(encoding="utf-8"))
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
        if not sp.exists():
            skipped.append({"target": orig, "reason": "staged item missing"})
            continue
        if op.exists():
            skipped.append({"target": orig, "reason": "destination already exists"})
            continue
        if not dry_run:
            op.parent.mkdir(parents=True, exist_ok=True)
            try:
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
        data["restored_at"] = datetime.now(timezone.utc).isoformat()
        data["restored_count"] = len(restored)
        mpath.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        "mode": "dry-run" if dry_run else "applied",
        "manifest": str(mpath),
        "restored_count": len(restored),
        "restored": restored,
        "skipped": skipped,
    }


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
    pg.add_argument("--trash-dir", default=None, help="Custom Trash staging root")
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
        return 0

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
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
