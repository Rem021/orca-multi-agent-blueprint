#!/usr/bin/env python3
"""Deterministic safety guardrail for multi-agent commands and git pushes.

Canonical source: outputs/orca-multi-agent-blueprint/scripts/guard.py
Installed runtime path: ~/.local/bin/agent-guard

Enforces hard boundaries defined in AGENTS.md / CLAUDE.md / GEMINI.md:
1. Volume & UUID protection: if a command (or its working directory) touches the external volume
   `/Volumes/<Drive>` while it is not a real mount point, lacks `AI/`, or its VolumeUUID mismatches
   `AI_VOLUME_UUID`, block immediately so agents never write into a fake directory on the mountpoint.
2. No recursive `rm` on project/worktree/archive directories (archive to `${AI_VOLUME_PATH}/AI/Archive` first, then move to Trash).
3. No `git reset --hard` or `git clean -f` (preserve uncommitted work via git bundle / archive).
4. No unflagged `git worktree prune` without `--dry-run`.
5. No emptying Trash (`rm -rf ~/.Trash/*`, `.Trashes` deletion, Finder `empty trash`).
6. Optional, off by default: no creating or cloning iOS Simulators (`xcrun simctl create` / `clone`) when
   `GUARD_BLOCK_SIMULATOR_CREATE=1`. Parallel simulators are allowed; the user lifted that limit on 2026-11-13.
7. No pushing restricted plaintext-secret branches (`GUARD_RESTRICTED_BRANCHES`, default `develop`, `feat/C-*`).
8. No force-pushing or deleting shared branches (`GUARD_PROTECTED_BRANCHES`, default `main`, `master`).
9. No creating ad-hoc ephemeral directories (`tmp`, `temp`, `.tmp`, `scratch`, `.scratch`, `.worktrees`,
   `test_tmp`, `debug_tmp`, `.agent-tmp`) directly at a project root or worktree root (route to
   `work/_scratch/` [3d TTL], `work/_tests/` [7d TTL], or Orca worktrees instead).

Commands are analysed per shell segment (quote-aware split, wrapper stripping such as `sudo` / `env` /
`sh -c`, git global options such as `-C <dir>`), so quoted text in commit messages or `grep` patterns is
not a false positive while `git -C repo reset --hard` or `rm -r -f <project>` cannot slip through.

Settings (volume, simulator IDs, branch lists) are read from `config.env` next to a checkout, then
`~/.orca/backup-config.env`, then `~/.orca/guard.env` (written by `deploy.sh --apply`); environment
variables override all three. A branch list set to `none` is empty.

This is an accident guardrail, not a sandbox: a command that hides its intent inside an interpreter
(`python -c`, a script file, a remote shell) is not analysed. Layer it with the git `pre-push` hook
(`agent-guard --mode install-pre-push --repo <repo>`) and with backups.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys


_SHELL_DEFAULT = re.compile(r"\$\{(\w+)(?::-([^${}]*))?\}")


def _expand_defaults(value: str, cfg: dict[str, str]) -> str:
    """Resolve the `${NAME:-default}` form used throughout config.env without running a shell."""
    for _ in range(4):
        expanded = _SHELL_DEFAULT.sub(
            lambda m: os.environ.get(m.group(1), "").strip() or cfg.get(m.group(1), "") or (m.group(2) or ""), value
        )
        if expanded == value:
            break
        value = expanded
    return value


def _load_env_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    home = Path.home()
    # Later files win. The installed copy (~/.local/bin/agent-guard) has no checkout next to it and
    # relies on the two files that deploy.sh / setup_backups.py write under ~/.orca.
    candidates = [
        Path(__file__).resolve().parent.parent / "config.env",
        home / ".orca/backup-config.env",
        home / ".orca/guard.env",
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'").replace("$HOME", str(home)).replace("${HOME}", str(home))
                v = _expand_defaults(v, cfg)
                if v:
                    cfg[k.strip()] = v
        except OSError:
            pass
    for k in (
        "AI_VOLUME_PATH",
        "AI_VOLUME_UUID",
        "BACKUP_MEDIA_PATH",
        "EXTERNAL_VOLUME_PATH",
        "PRIMARY_SIMULATOR_UDID",
        "SECONDARY_SIMULATOR_UDID",
        "STORAGE_MODE",
        "GUARD_PROTECTED_BRANCHES",
        "GUARD_RESTRICTED_BRANCHES",
        "GUARD_BLOCK_SIMULATOR_CREATE",
    ):
        env_val = os.environ.get(k, "").strip()
        if env_val:
            cfg[k] = env_val
    return cfg


def _split_list(value: str) -> list[str]:
    """Space- or comma-separated list; the single word `none` means an explicitly empty list."""
    items = [item for item in re.split(r"[\s,]+", value.strip()) if item]
    return [] if [item.lower() for item in items] == ["none"] else items


def _external_volume_root(path: str) -> str:
    parts = Path(path).parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
        return f"/Volumes/{parts[2]}"
    return ""


_CFG = _load_env_config()
VOLUME_PATH = _CFG.get("AI_VOLUME_PATH", "/Volumes/AI_Workspace").rstrip("/")
VOLUME_UUID = _CFG.get("AI_VOLUME_UUID", "AUTO").strip()
MEDIA_PATH = _CFG.get("BACKUP_MEDIA_PATH", "").rstrip("/")
EXTERNAL_VOLUME_PATH = (
    _CFG.get("EXTERNAL_VOLUME_PATH", "").rstrip("/")
    or _external_volume_root(MEDIA_PATH)
    or (_external_volume_root(VOLUME_PATH))
)
VOLUME_AI_DIR = Path(f"{VOLUME_PATH}/AI")
BLOCK_SIM_CREATE = _CFG.get("GUARD_BLOCK_SIMULATOR_CREATE", "0").strip().lower() in ("1", "true", "yes", "on")
PRIMARY_SIM = _CFG.get("PRIMARY_SIMULATOR_UDID", "")
SECONDARY_SIM = _CFG.get("SECONDARY_SIMULATOR_UDID", "")
# Shared branches that must never be force-pushed or deleted on the remote.
PROTECTED_BRANCHES = _split_list(_CFG.get("GUARD_PROTECTED_BRANCHES", "main master"))
# Branches (fnmatch patterns) that must never be pushed at all, e.g. history with plaintext secrets.
RESTRICTED_BRANCHES = _split_list(_CFG.get("GUARD_RESTRICTED_BRANCHES", "develop feat/C-*"))

_SIM_HINT = (
    f"主模拟器 `{PRIMARY_SIM}` 或副机 `{SECONDARY_SIM}`"
    if PRIMARY_SIM and SECONDARY_SIM
    else "已分配的主/副模拟器"
)

REASON_RESET = f"严禁执行 `git reset --hard`（AGENTS.md 规则）：请先通过 `git bundle` 或压缩包归档未提交改动到 `{VOLUME_PATH}/AI/Archive/<日期>-<主题>/`。"
REASON_CLEAN = f"严禁执行 `git clean -f/-fdx`（AGENTS.md 规则）：未跟踪文件可能包含交付物或实验回执，请先核验并归档到 `{VOLUME_PATH}/AI/Archive/`（只读预览可用 `git clean -n`）。"
REASON_PRUNE = "严禁不带 `--dry-run` 直接执行 `git worktree prune`（AGENTS.md 规则）：请先运行 `git worktree prune --dry-run` 核对路径。"
REASON_SIM = f"已开启 GUARD_BLOCK_SIMULATOR_CREATE：不新建或克隆 iOS 模拟器，请复用{_SIM_HINT}。"
REASON_TRASH = "严禁清空废纸篓或直接删除 `.Trash` / `.Trashes` 内容（AGENTS.md 规则）。"
REASON_RM = f"严禁对项目、工作树、冷归档或外置素材盘直接执行 `rm -rf`（AGENTS.md 规则）：退役工作树请先归档至 `{VOLUME_PATH}/AI/Archive/`，移入同卷废纸篓后使用 `orca worktree rm` 或 `git worktree remove`。"
REASON_RESTRICTED = "严禁推送含历史明文密钥的受限分支 `{branch}`（AGENTS.md 规则，受限模式：{patterns}）。"
REASON_FORCE = "严禁对共享分支 `{branch}` 强制推送或远端删除（`--force` / `--force-with-lease` / `+refspec` / `--delete`，AGENTS.md 规则）：请改用普通推送或在任务分支上操作。"
REASON_FORCE_UNKNOWN = "强制推送未写明目标分支，无法确认不是共享分支（AGENTS.md 规则）：请显式写出 `git push --force-with-lease <remote> <任务分支>`。"
REASON_ROOT_WORKTREES = f"严禁在项目或工作树根目录创建 `.worktrees/`（AGENTS.md 规则）：并行工作树请统一建在 `{VOLUME_PATH}/AI/{{Orca,Codex,Claude}}/Worktrees/`（推荐使用 `orca worktree create`）。"
REASON_ROOT_CLUTTER = "严禁在项目或工作树根目录直接创建 `{name}/`（AGENTS.md 规则）：请改用 `work/_scratch/`（3 天自动回收，可运行 `agent-governor scratch --topic <主题>`）、`work/_tests/`（7 天自动回收，可运行 `agent-governor test-dir --topic <主题>`）或系统 `/tmp`。"

_MAX_DEPTH = 4
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_KEYWORDS = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}", "fi", "done"}
# Wrapper commands that run another command; value = options that consume the following token.
_WRAPPERS: dict[str, set[str]] = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-T", "-U"},
    "env": {"-u", "-C", "-S", "-P"},
    "command": set(),
    "exec": {"-a"},
    "builtin": set(),
    "noglob": set(),
    "nocorrect": set(),
    "nohup": set(),
    "time": set(),
    "nice": {"-n"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "gtimeout": {"-s", "-k", "--signal", "--kill-after"},
    "caffeinate": {"-t", "-w"},
    "arch": {"-e"},
    "xargs": {"-n", "-I", "-P", "-L", "-J", "-E", "-s", "-R", "-S", "-d"},
}
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC_RE = re.compile(r"<<(?P<dash>-?)[ \t]*(?P<q>['\"]?)(?P<delim>[A-Za-z_][A-Za-z0-9_.-]*)(?P=q)")
_SHELL_WORD_RE = re.compile(r"(?<![\w.-])(?:ba|z|da|k)?sh(?![\w.-])")
_REDIRECT_RE = re.compile(r"^\d*[<>]")
_PROTECTED_LITERAL_RE = re.compile(
    r"(?:/AI/(?:Projects|Archive|Migration|[^/\s]+/Worktrees)|/Volumes/[^/\s]+/(?:Work|Frozen-[^/\s]+))(?:/|$)"
)
_PROTECTED_ROOT_RE = re.compile(
    r"^(?:.*/AI/(?:Projects|Archive|Migration|[^/]+/Worktrees)(?:/[^/]+)?|/Volumes/[^/]+/(?:Work|Frozen-[^/]+))$"
)
_TRASH_PATH_RE = re.compile(r"(?:^|/)\.Trash(?:es)?(?:/|$)")
_EMPTY_TRASH_RE = re.compile(r"empty\s+(?:the\s+)?trash", re.IGNORECASE)
_SHORT_C_RE = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
_FORBIDDEN_ROOT_EPHEMERALS = {
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
_PROJECT_SUBPATH_RE = re.compile(r"^(.*/AI/Projects/[^/]+)/([^/]+)(?:/.*)?$")
_ORCA_WT_SUBPATH_RE = re.compile(r"^(.*/AI/Orca/Worktrees/([^/]+)/\2/[^/]+)/([^/]+)(?:/.*)?$")
_AGENT_WT_SUBPATH_RE = re.compile(r"^(.*/AI/(?!Orca/)[^/]+/Worktrees/[^/]+)/([^/]+)(?:/.*)?$")


# ------------------------------------------------------------------------------
# Volume identity
# ------------------------------------------------------------------------------
def verify_volume_mounted_and_uuid(vol_path: str, expected_uuid: str = "AUTO") -> tuple[bool, str]:
    """Verify external volume mount state and VolumeUUID (fail-closed on a fake mountpoint directory)."""
    vol_path = vol_path.rstrip("/") or "/"
    p = Path(vol_path)
    external = vol_path.startswith("/Volumes/")
    if external and not os.path.ismount(vol_path):
        return (
            False,
            f"外置盘 `{vol_path}` 未挂载（该路径不是真实挂载点，AGENTS.md 规则）：已阻止操作，严禁在挂载点自建目录。",
        )
    if not (p / "AI").is_dir():
        return (
            False,
            f"外置盘 `{vol_path}` 未挂载或 `AI/` 目录不可见（AGENTS.md 规则）：已阻止操作，严禁在挂载点自建目录。",
        )
    if external and expected_uuid and expected_uuid not in ("AUTO", "LOCAL", "INTERNAL"):
        try:
            out = subprocess.check_output(
                ["/usr/sbin/diskutil", "info", "-plist", vol_path],
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            info = plistlib.loads(out)
        except Exception:
            # Transient diskutil failure on a real mount point: do not lock every agent out.
            return True, ""
        actual_uuid = info.get("VolumeUUID", "")
        if actual_uuid and actual_uuid != expected_uuid:
            return (
                False,
                f"外置盘 `{vol_path}` 的硬件 UUID 不匹配（期望 `{expected_uuid}`，实际 `{actual_uuid}`）：已阻止操作。",
            )
        mount_point = info.get("MountPoint", "")
        if mount_point and mount_point.rstrip("/") != vol_path:
            return (
                False,
                f"外置盘 `{vol_path}` 的实际挂载点为 `{mount_point}`：已阻止操作。",
            )
    return True, ""


# ------------------------------------------------------------------------------
# Shell segment splitting (quote / substitution / heredoc aware)
# ------------------------------------------------------------------------------
def _read_substitution(text: str, i: int) -> tuple[str, int]:
    """Return (inner, index_after) for a `$(...)` or backtick substitution starting at text[i]."""
    n = len(text)
    if text[i] == "`":
        j = i + 1
        while j < n and text[j] != "`":
            j += 2 if text[j] == "\\" else 1
        return text[i + 1 : j], min(j + 1, n)
    depth = 0
    j = i + 1  # at "("
    while j < n:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch in "'\"":  # skip a quoted span so its parentheses are not counted
            k = j + 1
            while k < n and text[k] != ch:
                k += 2 if (ch == '"' and text[k] == "\\") else 1
            j = k + 1
            continue
        if text.startswith("<<", j) and not text.startswith("<<<", j):
            m = _HEREDOC_RE.match(text, j)
            eol = text.find("\n", j)
            if m and eol != -1:  # heredoc body is data: jump past its terminator
                _body, j = _read_heredoc(text, eol + 1, m.group("delim"), bool(m.group("dash")))
                continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[i + 2 : j], j + 1
        j += 1
    return text[i + 2 :], n


def _read_heredoc(text: str, i: int, delim: str, strip_tabs: bool) -> tuple[str, int]:
    """Return (body, index_after_terminator) for a heredoc body starting at text[i]."""
    body: list[str] = []
    n = len(text)
    while i < n:
        j = text.find("\n", i)
        end = n if j == -1 else j
        line = text[i:end]
        i = n if j == -1 else j + 1
        if (line.lstrip("\t") if strip_tabs else line) == delim:
            break
        body.append(line)
    return "\n".join(body), i


def split_segments(command: str, _depth: int = 0) -> list[str]:
    """Split a shell command line into simple-command segments.

    Separators outside quotes: newline, `;`, `|`, `&`, `(`, `)`, backtick. Command substitutions inside
    double quotes are extracted as their own segments. Heredoc bodies are data and are skipped unless
    the same line feeds a shell interpreter (`bash <<EOF`, `cat <<EOF | sh`).
    """
    segs: list[str] = []
    buf: list[str] = []
    pending: list[tuple[str, bool]] = []
    quote = ""
    line_start = 0
    i, n = 0, len(command)

    def flush() -> None:
        seg = "".join(buf).strip()
        buf.clear()
        if seg:
            segs.append(seg)

    while i < n:
        ch = command[i]
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = ""
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                buf.append(command[i : i + 2])
                i += 2
                continue
            if ch == "`" or command.startswith("$(", i):
                inner, i = _read_substitution(command, i)
                if _depth < _MAX_DEPTH:
                    segs.extend(split_segments(inner, _depth + 1))
                buf.append("$_")  # opaque placeholder: never resolved as a path
                continue
            if ch == '"':
                quote = ""
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            if command[i + 1] == "\n":
                i += 2  # line continuation
                continue
            buf.append(command[i : i + 2])
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command.startswith("<<<", i):
            buf.append("<<<")
            i += 3
            continue
        if command.startswith("<<", i):
            m = _HEREDOC_RE.match(command, i)
            if m:
                pending.append((m.group("delim"), bool(m.group("dash"))))
                buf.append(" ")
                i = m.end()
                continue
        if ch == "\n":
            flush()
            i += 1
            if pending:
                feeds_shell = bool(_SHELL_WORD_RE.search(command[line_start:i]))
                for delim, strip_tabs in pending:
                    body, i = _read_heredoc(command, i, delim, strip_tabs)
                    if feeds_shell and _depth < _MAX_DEPTH:
                        segs.extend(split_segments(body, _depth + 1))
                pending.clear()
            line_start = i
            continue
        if ch in ";|()`":
            flush()
            i += 1
            continue
        if ch == "&":
            prev = command[i - 1] if i else ""
            nxt = command[i + 1] if i + 1 < n else ""
            if prev in "<>" or nxt == ">":  # redirection such as 2>&1 or &>file
                buf.append(ch)
                i += 1
                continue
            flush()
            i += 1
            continue
        buf.append(ch)
        i += 1
    flush()
    return segs


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _strip_wrappers(tok: list[str]) -> list[str]:
    """Drop leading assignments, shell keywords and wrapper commands (`sudo -u x env A=1 git ...`)."""
    i, n = 0, len(tok)
    while i < n:
        t = tok[i]
        if _ASSIGN_RE.match(t) or t in _KEYWORDS:
            i += 1
            continue
        base = os.path.basename(t)
        if base not in _WRAPPERS:
            break
        takes_value = _WRAPPERS[base]
        i += 1
        while i < n and tok[i].startswith("-") and len(tok[i]) > 1:
            opt = tok[i]
            i += 1
            if opt == "--":
                break
            if opt in takes_value and i < n:
                i += 1
        if base == "env":
            while i < n and _ASSIGN_RE.match(tok[i]):
                i += 1
        elif base in ("timeout", "gtimeout") and i < n:
            i += 1  # duration
    return tok[i:]


# ------------------------------------------------------------------------------
# Path helpers
# ------------------------------------------------------------------------------
def _expand(path: str) -> str:
    home = str(Path.home())
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    for var, value in (("HOME", home), ("AI_VOLUME_PATH", VOLUME_PATH)):
        path = path.replace("${" + var + "}", value).replace("$" + var, value)
    return path


def _resolve(path: str, cwd: str | None) -> str | None:
    """Lexically resolve an expanded path against cwd (None when it cannot be determined)."""
    if "$" in path or "`" in path:
        return None
    if os.path.isabs(path):
        return os.path.normpath(path)
    if not cwd:
        return None
    return os.path.normpath(os.path.join(cwd, path))


def _is_protected_root(path: str) -> bool:
    """True for a protected root, a direct child of it, or an ancestor that contains protected roots."""
    path = path.rstrip("/") or "/"
    if _PROTECTED_ROOT_RE.match(path):
        return True
    if path in {p for p in (VOLUME_PATH, EXTERNAL_VOLUME_PATH, MEDIA_PATH) if p}:
        return True
    if re.match(r"^/Volumes/[^/]+$", path) and (
        os.path.isdir(os.path.join(path, "Work")) or os.path.isdir(os.path.join(path, "AI", "Migration"))
    ):
        return True
    if _is_ai_root(path):
        return True
    parent, name = os.path.split(path)
    if _is_ai_root(parent):
        return any(c in name for c in "*?[") or os.path.isdir(os.path.join(path, "Worktrees"))
    return False


def _is_ai_root(path: str) -> bool:
    if path == f"{VOLUME_PATH}/AI":
        return True
    if EXTERNAL_VOLUME_PATH and path == f"{EXTERNAL_VOLUME_PATH}/AI":
        return True
    return path.endswith("/AI") and (
        os.path.isdir(os.path.join(path, "Projects")) or os.path.isdir(os.path.join(path, "Migration"))
    )


# ------------------------------------------------------------------------------
# Per-command checks
# ------------------------------------------------------------------------------
class _Context:
    def __init__(self, cwd: str | None):
        self.cwd = cwd


def _root_ephemeral_violation(resolved: str | None) -> str | None:
    """Return a block reason if `resolved` creates a forbidden ephemeral dir directly under a project/worktree root."""
    if not resolved:
        return None
    norm = os.path.normpath(resolved)
    first_child: str | None = None

    m_proj = _PROJECT_SUBPATH_RE.match(norm)
    if m_proj:
        first_child = m_proj.group(2)
    else:
        m_orca = _ORCA_WT_SUBPATH_RE.match(norm)
        if m_orca:
            first_child = m_orca.group(3)
        else:
            m_wt = _AGENT_WT_SUBPATH_RE.match(norm)
            if m_wt:
                first_child = m_wt.group(2)
            elif "/AI/" in norm and "/Worktrees/" in norm:
                # Fallback for custom worktree layouts when the checkout already exists on disk
                cur = norm
                while len(cur) > 1:
                    parent, leaf = os.path.split(cur)
                    if not leaf or parent == cur:
                        break
                    if os.path.exists(os.path.join(parent, ".git")):
                        first_child = leaf
                        break
                    cur = parent

    if first_child in _FORBIDDEN_ROOT_EPHEMERALS:
        if first_child == ".worktrees":
            return REASON_ROOT_WORKTREES
        return REASON_ROOT_CLUTTER.format(name=first_child)
    return None


def _check_mkdir(args: list[str], ctx: _Context) -> str | None:
    opts_done = False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if not opts_done:
            if a == "--":
                opts_done = True
                continue
            if a in ("-m", "--mode"):
                i += 1
                continue
            if a.startswith("-m") and len(a) > 2:
                continue
            if a.startswith("--mode="):
                continue
            if a.startswith("-") and len(a) > 1:
                continue
        if _REDIRECT_RE.match(a):
            continue
        resolved = _resolve(_expand(a), ctx.cwd)
        violation = _root_ephemeral_violation(resolved)
        if violation:
            return violation
    return None


def _check_rm(args: list[str], ctx: _Context) -> str | None:
    recursive = False
    targets: list[str] = []
    opts_done = False
    for a in args:
        if not opts_done:
            if a == "--":
                opts_done = True
                continue
            if a.startswith("--"):
                recursive = recursive or a == "--recursive"
                continue
            if a.startswith("-") and len(a) > 1:
                recursive = recursive or "r" in a or "R" in a
                continue
        if not _REDIRECT_RE.match(a):
            targets.append(a)
    if not recursive:
        return None
    for target in targets:
        literal = _expand(target)
        resolved = _resolve(literal, ctx.cwd)
        for candidate in (literal, resolved):
            if candidate and _TRASH_PATH_RE.search(candidate):
                return REASON_TRASH
        if _PROTECTED_LITERAL_RE.search(literal):
            return REASON_RM
        if resolved and _is_protected_root(resolved):
            return REASON_RM
    return None


def _git_output(repo_dir: str | None, *args: str) -> str | None:
    if not repo_dir:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _branch_name(ref: str) -> str:
    return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref


def _is_restricted(branch: str) -> bool:
    return any(fnmatch.fnmatchcase(branch, pat) for pat in RESTRICTED_BRANCHES)


def _restricted_reason(branch: str) -> str:
    return REASON_RESTRICTED.format(branch=branch, patterns="、".join(f"`{p}`" for p in RESTRICTED_BRANCHES))


def _check_git_push(args: list[str], repo_dir: str | None) -> str | None:
    force = delete = all_branches = False
    positionals: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            positionals.extend(args[i:])
            break
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if name in ("--force", "--force-with-lease", "--mirror"):
                force = True
            if name in ("--all", "--mirror", "--branches"):
                all_branches = True
            if name == "--delete":
                delete = True
            if name in ("--repo", "--push-option", "--receive-pack", "--exec") and "=" not in a:
                i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            force = force or "f" in a[1:]
            delete = delete or "d" in a[1:]
            if a.endswith("o"):
                i += 1
            continue
        if not _REDIRECT_RE.match(a):
            positionals.append(a)

    refspecs = positionals[1:]
    current: list[str | None] = []  # lazily resolved

    def current_branch() -> str | None:
        if not current:
            current.append(_git_output(repo_dir, "symbolic-ref", "--short", "-q", "HEAD"))
        return current[0]

    def named(ref: str) -> str | None:
        return current_branch() if ref in ("HEAD", "@") else _branch_name(ref)

    if refspecs and refspecs[0] != "tag":
        for spec in refspecs:
            plus = spec.startswith("+")
            src, sep, dst = spec.lstrip("+").partition(":")
            dst_branch = named(dst if sep else src)
            src_branch = named(src) if src else None
            for branch in (src_branch, dst_branch):
                if branch and _is_restricted(branch):
                    return _restricted_reason(branch)
            destructive = force or plus or delete or (bool(sep) and not src)
            if destructive and dst_branch in PROTECTED_BRANCHES:
                return REASON_FORCE.format(branch=dst_branch)
            if destructive and dst_branch is None:
                return REASON_FORCE_UNKNOWN
        return None

    if all_branches:
        listing = _git_output(repo_dir, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        branches = listing.splitlines() if listing is not None else None
    else:
        head = current_branch()
        branches = [head] if head else None
    if branches is None:
        return REASON_FORCE_UNKNOWN if force else None
    for branch in branches:
        if _is_restricted(branch):
            return _restricted_reason(branch)
    if force:
        for branch in branches:
            if branch in PROTECTED_BRANCHES:
                return REASON_FORCE.format(branch=branch)
    return None


def _check_git(args: list[str], ctx: _Context) -> str | None:
    repo_dir = ctx.cwd
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            repo_dir = _resolve(_expand(args[i + 1]), repo_dir)
            i += 2
            continue
        if a in ("-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        break
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1 :]
    flags = [a for a in rest if a.startswith("-")]
    short = "".join(a[1:] for a in flags if not a.startswith("--"))
    dry_run = "--dry-run" in flags or "n" in short
    if sub == "reset" and "--hard" in flags:
        return REASON_RESET
    if sub == "clean" and ("--force" in flags or "f" in short) and not dry_run:
        return REASON_CLEAN
    if sub == "worktree":
        words: list[str] = []
        j = 0
        while j < len(rest):
            ra = rest[j]
            if ra in ("-b", "-B", "--reason") and j + 1 < len(rest):
                j += 2
                continue
            if ra.startswith("-"):
                j += 1
                continue
            if not _REDIRECT_RE.match(ra):
                words.append(ra)
            j += 1
        if words and words[0] == "prune" and not dry_run:
            return REASON_PRUNE
        if len(words) >= 2 and words[0] == "add":
            wt_dest = _resolve(_expand(words[1]), repo_dir)
            violation = _root_ephemeral_violation(wt_dest)
            if violation:
                return violation
    if sub == "push":
        return _check_git_push(rest, repo_dir)
    return None


def _check_simctl(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--set":
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return REASON_SIM if BLOCK_SIM_CREATE and a in ("create", "clone") else None
    return None


def _check_segment(segment: str, ctx: _Context, depth: int) -> str | None:
    if "EmptyTrash" in segment:
        return REASON_TRASH
    tok = _strip_wrappers(_tokens(segment))
    if not tok:
        return None
    prog, args = os.path.basename(tok[0]), tok[1:]
    if prog in ("cd", "pushd"):
        words = [a for a in args if not a.startswith("-") or a == "-"]
        if not words:
            ctx.cwd = str(Path.home())
        elif words[0] == "-":
            ctx.cwd = None
        else:
            ctx.cwd = _resolve(_expand(words[0]), ctx.cwd)
        return None
    if prog in _SHELLS and depth < _MAX_DEPTH:
        for idx, a in enumerate(args):
            if _SHORT_C_RE.match(a):
                if idx + 1 < len(args):
                    return _check_script(args[idx + 1], _Context(ctx.cwd), depth + 1)
                break
        return None
    if prog == "eval" and depth < _MAX_DEPTH:
        return _check_script(" ".join(args), ctx, depth + 1)
    if prog == "git":
        return _check_git(args, ctx)
    if prog == "rm":
        return _check_rm(args, ctx)
    if prog == "mkdir":
        return _check_mkdir(args, ctx)
    if prog == "xcrun":
        if "simctl" in args:
            return _check_simctl(args[args.index("simctl") + 1 :])
        return None
    if prog == "simctl":
        return _check_simctl(args)
    if prog == "osascript" and _EMPTY_TRASH_RE.search(segment):
        return REASON_TRASH
    return None


def _check_script(script: str, ctx: _Context, depth: int = 0) -> str | None:
    for segment in split_segments(script):
        reason = _check_segment(segment, ctx, depth)
        if reason:
            return reason
    return None


def _mentions_path(command: str, path: str) -> bool:
    return bool(re.search(re.escape(path) + r"(?![\w.-])", command))


def evaluate_command(
    command: str,
    check_volume: bool = True,
    vol_path: str | None = None,
    expected_uuid: str | None = None,
    cwd: str | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason). True means allowed."""
    if not command or not isinstance(command, str):
        return True, ""
    target_vol = (vol_path or VOLUME_PATH).rstrip("/")
    target_uuid = expected_uuid if expected_uuid is not None else VOLUME_UUID
    if check_volume and target_vol.startswith("/Volumes/"):
        cwd_on_volume = bool(cwd) and (cwd == target_vol or cwd.startswith(target_vol + "/"))
        if cwd_on_volume or _mentions_path(command, target_vol):
            ok, err = verify_volume_mounted_and_uuid(target_vol, target_uuid)
            if not ok:
                return False, err
    try:
        reason = _check_script(command, _Context(cwd))
    except RecursionError:
        reason = None
    if reason:
        return False, reason
    return True, ""


# ------------------------------------------------------------------------------
# Entry points
# ------------------------------------------------------------------------------
def run_hook() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0

    is_antigravity = "toolCall" in payload or "conversationId" in payload
    tool_call = payload.get("toolCall") or {}
    tool_args = tool_call.get("args") or {}
    tool_input = payload.get("tool_input") or {}
    cmd = (
        tool_input.get("command")
        or tool_input.get("cmd")
        or tool_args.get("CommandLine")
        or tool_args.get("command")
        or payload.get("command")
        or ""
    )
    cwd = (
        tool_input.get("cwd")
        or tool_input.get("workdir")
        or tool_args.get("Cwd")
        or tool_args.get("cwd")
        or payload.get("cwd")
        or None
    )
    if isinstance(cmd, list):
        cmd = " ".join(shlex.quote(str(part)) for part in cmd)
    allowed, reason = evaluate_command(cmd, cwd=cwd if isinstance(cwd, str) else None)
    if not allowed:
        if is_antigravity:
            print(json.dumps({"decision": "deny", "reason": reason}, ensure_ascii=False))
            print(f"[agent-guard] BLOCKED: {reason}", file=sys.stderr)
            return 0
        response = {
            "decision": "block",
            "reason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }
        print(json.dumps(response, ensure_ascii=False))
        print(f"[agent-guard] BLOCKED: {reason}", file=sys.stderr)
        return 2

    if is_antigravity:
        print('{"decision":"ask"}')
    else:
        print("{}")
    return 0


def _is_null_sha(sha: str) -> bool:
    return bool(sha) and set(sha) == {"0"}


def run_git_pre_push(remote: str, url: str) -> int:
    """git pre-push hook: stdin lines are `<local ref> <local sha> <remote ref> <remote sha>`."""
    for line in sys.stdin:
        parts = line.strip().split()
        if len(parts) < 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts[:4]
        for ref in (local_ref, remote_ref):
            branch = _branch_name(ref)
            if _is_restricted(branch):
                print(
                    f"[agent-guard] 阻止推送：受限分支 `{branch}` 含历史敏感配置，严禁推送到远端（AGENTS.md 规则）。",
                    file=sys.stderr,
                )
                return 1
        if not remote_ref.startswith("refs/heads/"):
            continue
        target = _branch_name(remote_ref)
        if target not in PROTECTED_BRANCHES or _is_null_sha(remote_sha):
            continue
        if _is_null_sha(local_sha):
            print(f"[agent-guard] 阻止推送：严禁删除远端共享分支 `{target}`（AGENTS.md 规则）。", file=sys.stderr)
            return 1
        try:
            ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", remote_sha, local_sha],
                capture_output=True,
                timeout=20,
            ).returncode
        except Exception:
            ancestor = 0  # cannot verify; git itself still rejects non-fast-forward without --force
        if ancestor != 0:
            print(
                f"[agent-guard] 阻止推送：对共享分支 `{target}` 的推送不是快进（会改写远端历史，AGENTS.md 规则）。"
                "请先 `git pull --rebase` 或改推任务分支。",
                file=sys.stderr,
            )
            return 1
    return 0


_PRE_PUSH_MARKER = "# agent-guard pre-push"


def install_pre_push(repo: str) -> int:
    """Install a git pre-push hook in one repository (never overwrites a foreign hook)."""
    hook_rel = _git_output(repo, "rev-parse", "--git-path", "hooks/pre-push")
    if not hook_rel:
        print(f"[agent-guard] `{repo}` 不是 Git 仓库，未安装 pre-push 钩子。", file=sys.stderr)
        return 1
    hook = Path(hook_rel) if os.path.isabs(hook_rel) else Path(repo) / hook_rel
    if hook.exists() and _PRE_PUSH_MARKER not in hook.read_text(encoding="utf-8", errors="ignore"):
        print(f"[agent-guard] `{hook}` 已存在其他 pre-push 钩子，未覆盖；请手动在其中调用 agent-guard --mode pre-push。", file=sys.stderr)
        return 1
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n"
        f"{_PRE_PUSH_MARKER}\n"
        'GUARD="$HOME/.local/bin/agent-guard"\n'
        '[ -x "$GUARD" ] || exit 0\n'
        'exec "$GUARD" --mode pre-push "$@"\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)
    print(f"[agent-guard] 已安装 pre-push 钩子：{hook}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-Agent Deterministic Safety Guardrail")
    parser.add_argument("--mode", choices=["hook", "check", "pre-push", "install-pre-push"], default="hook")
    parser.add_argument("--command", default="", help="Command string when --mode=check")
    parser.add_argument("--repo", default=".", help="Repository path when --mode=install-pre-push")
    parser.add_argument("extra", nargs="*", help="Extra git hook args")
    args = parser.parse_args()

    if args.mode == "hook":
        return run_hook()
    if args.mode == "pre-push":
        remote = args.extra[0] if len(args.extra) > 0 else "origin"
        url = args.extra[1] if len(args.extra) > 1 else ""
        return run_git_pre_push(remote, url)
    if args.mode == "install-pre-push":
        return install_pre_push(args.repo)
    if args.mode == "check":
        allowed, reason = evaluate_command(args.command, cwd=os.getcwd())
        if not allowed:
            print(reason, file=sys.stderr)
            return 2
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
