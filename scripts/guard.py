#!/usr/bin/env python3
"""Deterministic safety guardrail for multi-agent commands, file writes, and git pushes.

Canonical source: outputs/orca-multi-agent-blueprint/scripts/guard.py
Installed runtime path: ~/.local/bin/agent-guard

Enforces hard boundaries defined in AGENTS.md / CLAUDE.md / GEMINI.md:
1. Volume & UUID protection: if a command/file tool (or its working directory) touches the external
   volume `/Volumes/<Drive>` while it is not a real mount point, lacks `AI/`, or its VolumeUUID
   mismatches `AI_VOLUME_UUID`, block immediately so agents never write into a fake mountpoint stub.
2. No destructive removal (`rm -rf`, `find -delete`, `xargs rm -rf`, `rsync --delete`, inline
   `python -c shutil.rmtree`) on `$HOME`, ancestors, project/worktree roots, `.git`, `AI/{Archive,Migration,Data}`,
   or `/Volumes/*/{Work,Frozen-*}` (archive to `${AI_VOLUME_PATH}/AI/Archive` first, then move to Trash).
3. No `git reset --hard`, `git clean -f` (including `-c clean.requireForce=false` and abbreviated long
   flags like `--har` / `--forc`), `git checkout -- .` / `git checkout -f`, `git restore .`,
   `git stash clear`, or `git worktree remove --force`.
4. No unflagged `git worktree prune` without `--dry-run`.
5. No emptying Trash (`rm -rf ~/.Trash/*`, `find ~/.Trash -delete`, `.Trashes` deletion, Finder `empty trash`).
6. Optional, off by default: no creating or cloning iOS Simulators (`xcrun simctl create` / `clone`) when
   `GUARD_BLOCK_SIMULATOR_CREATE=1`.
7. No pushing restricted plaintext-secret branches (`GUARD_RESTRICTED_BRANCHES`, default `develop`, `feat/C-*`),
   including wildcard (`refs/heads/*`) or matching (`:`) refspecs.
8. No force-pushing or deleting shared branches (`GUARD_PROTECTED_BRANCHES`, default `main`, `master`),
   including `gh api -X DELETE repos/.../git/refs/heads/<branch>` (while allowing `--dry-run` / `-n`).
9. No creating or writing ad-hoc ephemeral directories (`tmp`, `temp`, `.tmp`, `scratch`, `.scratch`,
   `.worktrees`, `test_tmp`, `debug_tmp`, `.agent-tmp`) directly at a project root or worktree root
   via shell (`mkdir`, `ln`, `cp`, `mv`, `git worktree add`) OR file tools (`Write`, `Edit`, `MultiEdit`).
10. No quitting or force-killing the host `Orca.app` (`osascript ... tell application "Orca" to quit`,
    `killall Orca`, `pkill Orca` / `pkill -f Orca.app`, unless `GUARD_ALLOW_HOST_ORCA_QUIT=1`), and no
    submitting unbounded `KeepAlive=true` jobs via deprecated `launchctl submit`.
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
        "GUARD_ALLOW_HOST_ORCA_QUIT",
    ):
        env_val = os.environ.get(k, "").strip()
        if env_val:
            cfg[k] = env_val
    return cfg


def _split_list(value: str) -> list[str]:
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
ALLOW_HOST_ORCA_QUIT = _CFG.get("GUARD_ALLOW_HOST_ORCA_QUIT", "0").strip().lower() in ("1", "true", "yes", "on")
PRIMARY_SIM = _CFG.get("PRIMARY_SIMULATOR_UDID", "")
SECONDARY_SIM = _CFG.get("SECONDARY_SIMULATOR_UDID", "")
PROTECTED_BRANCHES = _split_list(_CFG.get("GUARD_PROTECTED_BRANCHES", "main master"))
RESTRICTED_BRANCHES = _split_list(_CFG.get("GUARD_RESTRICTED_BRANCHES", "develop feat/C-*"))

_SIM_HINT = (
    f"主模拟器 `{PRIMARY_SIM}` 或副机 `{SECONDARY_SIM}`"
    if PRIMARY_SIM and SECONDARY_SIM
    else "已分配的主/副模拟器"
)

REASON_RESET = f"严禁执行 `git reset --hard` 或全仓丢弃未提交改动（AGENTS.md 规则）：请先通过 `git bundle` 或压缩包归档未提交改动到 `{VOLUME_PATH}/AI/Archive/<日期>-<主题>/`。"
REASON_CLEAN = f"严禁执行 `git clean -f/-fdx`（AGENTS.md 规则）：未跟踪文件可能包含交付物或实验回执，请先核验并归档到 `{VOLUME_PATH}/AI/Archive/`（只读预览可用 `git clean -n`）。"
REASON_PRUNE = "严禁不带 `--dry-run` 直接执行 `git worktree prune` 或 `git worktree remove --force`（AGENTS.md 规则）：请先核对未提交改动并归档后再移除。"
REASON_SIM = f"已开启 GUARD_BLOCK_SIMULATOR_CREATE：不新建或克隆 iOS 模拟器，请复用{_SIM_HINT}。"
REASON_TRASH = "严禁清空废纸篓或直接删除 `.Trash` / `.Trashes` 内容（AGENTS.md 规则）。"
REASON_RM = f"严禁对主目录、项目、工作树、冷归档、数据目录或外置素材盘直接执行破坏性删除（`rm -rf` / `find -delete` / `rsync --delete`，AGENTS.md 规则）：退役工作树请先归档至 `{VOLUME_PATH}/AI/Archive/`，移入同卷废纸篓后使用 `orca worktree rm` 或 `git worktree remove`。"
REASON_RESTRICTED = "严禁推送含历史明文密钥的受限分支 `{branch}`（AGENTS.md 规则，受限模式：{patterns}）。"
REASON_FORCE = "严禁对共享分支 `{branch}` 强制推送或远端删除（`--force` / `--force-with-lease` / `+refspec` / `--delete` / `gh api DELETE`，AGENTS.md 规则）：请改用普通推送或在任务分支上操作。"
REASON_FORCE_UNKNOWN = "强制推送或通配推送未写明安全的单分支目标，无法确认不触及共享/受限分支（AGENTS.md 规则）：请显式写出 `git push --force-with-lease <remote> <任务分支>`。"
REASON_ROOT_WORKTREES = f"严禁在项目或工作树根目录创建 `.worktrees/`（AGENTS.md 规则）：并行工作树请统一建在 `{VOLUME_PATH}/AI/{{Orca,Codex,Claude}}/Worktrees/`（推荐使用 `orca worktree create`）。"
REASON_ROOT_CLUTTER = "严禁在项目或工作树根目录直接创建或写入 `{name}/`（AGENTS.md 规则）：请改用 `work/_scratch/`（3 天自动回收，可运行 `agent-governor scratch --topic <主题>`）、`work/_tests/`（7 天自动回收，可运行 `agent-governor test-dir --topic <主题>`）或系统 `/tmp`。"
REASON_HOST_ORCA_QUIT = "严禁在 Agent 会话中退出或强杀宿主 Orca.app（会导致所有运行中会话中断甚至触发无限闪退循环）：如需重启 Orca 请提示用户手动操作。"
REASON_LAUNCHCTL_SUBMIT = "严禁使用已废弃的 `launchctl submit` 提交无节流 `KeepAlive=true` 常驻任务（极易引发 10 秒死循环自杀风暴）：请改用受控前台进程或带节流配置的 LaunchAgent plist。"

_MAX_DEPTH = 4
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_KEYWORDS = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}", "fi", "done"}
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
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_HEREDOC_RE = re.compile(r"<<(?P<dash>-?)[ \t]*(?P<q>['\"]?)(?P<delim>[A-Za-z_][A-Za-z0-9_.-]*)(?P=q)")
_SHELL_WORD_RE = re.compile(r"(?<![\w.-])(?:ba|z|da|k)?sh(?![\w.-])")
_REDIRECT_RE = re.compile(r"^\d*[<>]")
_PROTECTED_LITERAL_RE = re.compile(
    r"(?:/AI/(?:Projects|Archive|Migration|Data|[^/\s]+/Worktrees)|/AI[-_]Workspace/Data|/Volumes/[^/\s]+/(?:Work|Archive|Data|Backups|Migration|Frozen-[^/\s]+))(?:/|$)"
)
_PROTECTED_SUBTREE_RE = re.compile(
    r"^(?:.*/AI/(?:Archive|Migration|Data)(?:/.*)?|.*/AI[-_]Workspace/Data(?:/.*)?|/Volumes/[^/]+/(?:Work|Archive|Data|Backups|Migration|Frozen-[^/]+)(?:/.*)?)$"
)
_PROTECTED_PROJECT_RE = re.compile(
    r"^.*/AI/Projects(?:/[^/]+(?:/(?:docs|reports|outputs|work)|/outputs/[^/]+)?)?$"
)
_WORKTREE_PATH_RE = re.compile(r"^(.*/AI/[^/]+/Worktrees)(?:/(.*))?$")
_TRASH_PATH_RE = re.compile(r"(?:^|/)\.Trash(?:es)?(?:/|$)")
_EMPTY_TRASH_RE = re.compile(r"empty\s+(?:the\s+)?trash|EmptyTrash", re.IGNORECASE)
_OSASCRIPT_WORD_RE = re.compile(r"(?<![\w.-])osascript(?![\w.-])")
_OSASCRIPT_ORCA_TARGET_RE = re.compile(
    r"""(?:application\s+(?:id\s+)?|Application\s*\(\s*)(?:\\?["'])?(?:com\.stablyai\.orca|(?:/Applications/)?Orca(?:\.app)?)(?:\\?["'])?""",
    re.IGNORECASE,
)
_OSASCRIPT_QUIT_RE = re.compile(r"\bquit\b", re.IGNORECASE)
_PYTHON_LAUNCHCTL_SUBMIT_RE = re.compile(
    r"""['"]launchctl['"]\s*,\s*['"]submit['"]|\blaunchctl\s+submit\b"""
)
_SHORT_C_RE = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
_PYTHON_PROG_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
_PYTHON_DESTRUCTIVE_RE = re.compile(
    r"\b(?:shutil\.rmtree|os\.(?:remove|unlink|rmdir|removedirs)|Path\([^)]*\)\.(?:unlink|rmdir))\b"
)
_VAR_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_BRACE_RE = re.compile(r"^(.*?)\{([^{}$]+,[^{}$]+)\}(.*)$")

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
_SAFE_BUILD_OR_CACHE_DIRS = {
    "node_modules",
    "DerivedData",
    ".build",
    "build",
    "target",
    "dist",
    "Pods",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".turbo",
    ".cache",
}
_PROJECT_SUBPATH_RE = re.compile(r"^(.*/AI/Projects/[^/]+)/([^/]+)(?:/.*)?$")
_WORKTREE_AFTER_RE = re.compile(r"^(.*/AI/[^/]+/Worktrees)/(.+)$")
_NON_ROOT_CONTAINERS = {
    "work",
    "src",
    "tests",
    "test",
    "docs",
    "reports",
    "outputs",
    "packages",
    "apps",
    "lib",
    "scripts",
    "build",
    ".build",
    "node_modules",
    "target",
    "vendor",
    "assets",
    "resources",
    "Sources",
    "Tests",
}


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
# Shell segment splitting (comment / quote / substitution / heredoc aware)
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
        if ch in "'\"":
            k = j + 1
            while k < n and text[k] != ch:
                k += 2 if (ch == '"' and text[k] == "\\") else 1
            j = k + 1
            continue
        if text.startswith("<<", j) and not text.startswith("<<<", j):
            m = _HEREDOC_RE.match(text, j)
            eol = text.find("\n", j)
            if m and eol != -1:
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


def _substitution_placeholder(inner: str) -> str:
    s = inner.strip()
    if s == "pwd":
        return "${PWD}"
    if s.startswith("git ") and "rev-parse" in s and "--show-toplevel" in s:
        return "${_GIT_TOPLEVEL}"
    return "${_DYNAMIC_SUB}"


def split_segments(command: str, _depth: int = 0) -> list[str]:
    """Split a shell command line into simple-command segments in O(N) time."""
    segs: list[str] = []
    buf: list[str] = []
    header_chars: list[str] = []
    pending: list[tuple[str, bool]] = []
    quote = ""
    i, n = 0, len(command)

    def flush() -> None:
        if not buf:
            return
        seg = "".join(buf).strip()
        buf.clear()
        if seg:
            segs.append(seg)

    while i < n:
        ch = command[i]
        if quote == "'":
            buf.append(ch)
            header_chars.append(ch)
            if ch == "'":
                quote = ""
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                span = command[i : i + 2]
                buf.append(span)
                header_chars.append(span)
                i += 2
                continue
            if ch == "`" or command.startswith("$(", i):
                inner, i = _read_substitution(command, i)
                if _depth < _MAX_DEPTH:
                    segs.extend(split_segments(inner, _depth + 1))
                ph = _substitution_placeholder(inner)
                buf.append(ph)
                header_chars.append(ph)
                continue
            if ch == '"':
                quote = ""
            buf.append(ch)
            header_chars.append(ch)
            i += 1
            continue

        # Outside quotes: `#` at token boundary starts a shell comment up to `\n`
        if ch == "#" and (i == 0 or command[i - 1] in " \t\r\n;|&("):
            eol = command.find("\n", i)
            i = n if eol == -1 else eol
            continue

        if ch == "\\" and i + 1 < n:
            if command[i + 1] == "\n":
                i += 2
                continue
            span = command[i : i + 2]
            buf.append(span)
            header_chars.append(span)
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            header_chars.append(ch)
            i += 1
            continue
        if ch == "`" or command.startswith("$(", i):
            inner, i = _read_substitution(command, i)
            if _depth < _MAX_DEPTH:
                segs.extend(split_segments(inner, _depth + 1))
            ph = _substitution_placeholder(inner)
            buf.append(ph)
            header_chars.append(ph)
            continue
        if command.startswith("<<<", i):
            buf.append("<<<")
            header_chars.append("<<<")
            i += 3
            continue
        if command.startswith("<<", i):
            m = _HEREDOC_RE.match(command, i)
            if m:
                pending.append((m.group("delim"), bool(m.group("dash"))))
                buf.append(" ")
                header_chars.append(" ")
                i = m.end()
                continue
        if ch == "\n":
            flush()
            i += 1
            header_line = "".join(header_chars)
            header_chars.clear()
            if pending:
                feeds_shell = bool(_SHELL_WORD_RE.search(header_line))
                feeds_osascript = bool(_OSASCRIPT_WORD_RE.search(header_line))
                for delim, strip_tabs in pending:
                    body, i = _read_heredoc(command, i, delim, strip_tabs)
                    if feeds_shell and _depth < _MAX_DEPTH:
                        segs.extend(split_segments(body, _depth + 1))
                    elif feeds_osascript:
                        segs.append(f"osascript -e {shlex.quote(body)}")
                pending.clear()
            continue
        if ch in ";|()":
            flush()
            header_chars.append(ch)
            i += 1
            continue
        if ch == "&":
            prev = command[i - 1] if i else ""
            nxt = command[i + 1] if i + 1 < n else ""
            if prev in "<>" or nxt == ">":
                buf.append(ch)
                header_chars.append(ch)
                i += 1
                continue
            flush()
            header_chars.append(ch)
            i += 1
            continue
        buf.append(ch)
        header_chars.append(ch)
        i += 1
    flush()
    return segs


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _expand_braces(token: str) -> list[str]:
    m = _BRACE_RE.match(token)
    if not m:
        return [token]
    prefix, inner, suffix = m.group(1), m.group(2), m.group(3)
    parts = inner.split(",")[:16]
    out: list[str] = []
    for part in parts:
        out.extend(_expand_braces(prefix + part + suffix))
    return out[:16]


class _Context:
    def __init__(self, cwd: str | None, vars_map: dict[str, str] | None = None):
        self.cwd = cwd
        self.vars: dict[str, str] = dict(vars_map) if vars_map else {}


def _find_git_toplevel(start_dir: str | None) -> str | None:
    if not start_dir:
        return None
    cur = Path(start_dir)
    for cand in (cur, *cur.parents):
        if (cand / ".git").exists():
            return str(cand)
    return start_dir


# ------------------------------------------------------------------------------
# Path helpers
# ------------------------------------------------------------------------------
def _expand(path: str, ctx: _Context | None = None) -> str:
    home = str(Path.home())
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    base_vars: dict[str, str] = {
        "HOME": home,
        "AI_VOLUME_PATH": VOLUME_PATH,
    }
    if ctx is not None:
        if ctx.cwd:
            base_vars["PWD"] = ctx.cwd
        if "${_GIT_TOPLEVEL}" in path and ctx.cwd:
            gt = _find_git_toplevel(ctx.cwd)
            if gt:
                base_vars["_GIT_TOPLEVEL"] = gt
        for k, v in ctx.vars.items():
            base_vars[k] = v

    def _repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2) or ""
        return base_vars.get(name, m.group(0))

    return _VAR_REF_RE.sub(_repl, path)


def _resolve(path: str, cwd: str | None) -> str | None:
    """Lexically resolve an expanded path against cwd (None when dynamic variables remain)."""
    if "$" in path or "`" in path:
        return None
    if os.path.isabs(path):
        return os.path.normpath(path)
    if not cwd:
        return None
    return os.path.normpath(os.path.join(cwd, path))


def _is_ai_root(path: str) -> bool:
    if path == f"{VOLUME_PATH}/AI":
        return True
    if EXTERNAL_VOLUME_PATH and path == f"{EXTERNAL_VOLUME_PATH}/AI":
        return True
    return path.endswith("/AI") and (
        os.path.isdir(os.path.join(path, "Projects")) or os.path.isdir(os.path.join(path, "Migration"))
    )


def _is_ancestor_of_protected(path: str) -> bool:
    norm = path.rstrip("/") or "/"
    home = str(Path.home()).rstrip("/") or "/"
    if norm in ("/", "/Users", "/Volumes", home):
        return True
    anchors = [p.rstrip("/") for p in (VOLUME_PATH, f"{VOLUME_PATH}/AI", EXTERNAL_VOLUME_PATH, MEDIA_PATH) if p]
    prefix = norm + "/"
    return any((a == norm or (a + "/").startswith(prefix)) for a in anchors)


def _is_worktree_protected_target(path: str) -> bool:
    m = _WORKTREE_PATH_RE.match(path)
    if not m:
        return False
    wt_base = m.group(1)
    rest = m.group(2) or ""
    segs = [s for s in rest.split("/") if s]
    if len(segs) <= 1:
        return True
    # If a strictly higher segment already has `.git` on disk, `path` is inside a worktree (e.g. DerivedData / node_modules)
    for k in range(1, len(segs)):
        anc = os.path.join(wt_base, *segs[:k])
        if os.path.exists(os.path.join(anc, ".git")):
            return False
    if os.path.exists(os.path.join(path, ".git")):
        return True
    # Lexical 2-level (`Orca/Worktrees/<repo>/<wt>`) and 3-level (`Orca/Worktrees/<org>/<repo>/<wt>`) protection
    if len(segs) == 2:
        if segs[1] in _SAFE_BUILD_OR_CACHE_DIRS or segs[1] == "work":
            return False
        return True
    if len(segs) == 3 and "/Orca/Worktrees" in wt_base:
        if segs[1] in _SAFE_BUILD_OR_CACHE_DIRS or segs[2] in _SAFE_BUILD_OR_CACHE_DIRS or "work" in segs[1:]:
            return False
        return True
    return False


def _is_protected_root(path: str) -> bool:
    """True for a protected root, protected storage subtree, worktree checkout, or ancestor."""
    path = path.rstrip("/") or "/"
    if _is_ancestor_of_protected(path):
        return True
    if VOLUME_PATH and (path == f"{VOLUME_PATH}/Data" or path.startswith(f"{VOLUME_PATH}/Data/")):
        return True
    if MEDIA_PATH and (path == MEDIA_PATH or path.startswith(f"{MEDIA_PATH}/")):
        return True
    if EXTERNAL_VOLUME_PATH and EXTERNAL_VOLUME_PATH != VOLUME_PATH and (
        path == EXTERNAL_VOLUME_PATH or path.startswith(f"{EXTERNAL_VOLUME_PATH}/")
    ):
        return True
    if _PROTECTED_SUBTREE_RE.match(path) or _PROTECTED_PROJECT_RE.match(path):
        return True
    if _is_worktree_protected_target(path):
        return True
    if re.match(r"^/Volumes/[^/]+$", path) and (
        os.path.isdir(os.path.join(path, "Work")) or os.path.isdir(os.path.join(path, "AI"))
    ):
        return True
    if _is_ai_root(path):
        return True
    parent, name = os.path.split(path)
    if _is_ai_root(parent):
        return any(c in name for c in "*?[") or os.path.isdir(os.path.join(path, "Worktrees")) or name in (
            "Projects",
            "Archive",
            "Migration",
            "Data",
            "Orca",
            "Codex",
            "Claude",
        )
    return False


# ------------------------------------------------------------------------------
# Per-command checks
# ------------------------------------------------------------------------------
def _root_ephemeral_violation(resolved: str | None) -> str | None:
    """Return a block reason if `resolved` creates or writes inside a forbidden ephemeral dir at a project/worktree root."""
    if not resolved:
        return None
    norm = os.path.normpath(resolved)
    first_child: str | None = None

    m_proj = _PROJECT_SUBPATH_RE.match(norm)
    if m_proj:
        first_child = m_proj.group(2)
    else:
        m_wt = _WORKTREE_AFTER_RE.match(norm)
        if m_wt:
            wt_base = m_wt.group(1)
            segments = [s for s in m_wt.group(2).split("/") if s]
            found_git_root = False
            for k in range(1, min(len(segments), 5)):
                anc = os.path.join(wt_base, *segments[:k])
                if os.path.exists(os.path.join(anc, ".git")):
                    if k < len(segments):
                        first_child = segments[k]
                    found_git_root = True
                    break
            if not found_git_root and len(segments) >= 2:
                for idx in range(1, min(len(segments), 4)):
                    if any(prev in _NON_ROOT_CONTAINERS for prev in segments[1:idx]):
                        break
                    if segments[idx] in _FORBIDDEN_ROOT_EPHEMERALS:
                        first_child = segments[idx]
                        break

    if first_child in _FORBIDDEN_ROOT_EPHEMERALS:
        if first_child == ".worktrees":
            return REASON_ROOT_WORKTREES
        return REASON_ROOT_CLUTTER.format(name=first_child)
    return None


def _check_ln(args: list[str], ctx: _Context) -> str | None:
    target_dir: str | None = None
    positionals: list[str] = []
    opts_done = False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if not opts_done:
            if a == "--":
                opts_done = True
                continue
            if a in ("-t", "--target-directory") and i < len(args):
                target_dir = args[i]
                i += 1
                continue
            if a.startswith("--target-directory="):
                target_dir = a.split("=", 1)[1]
                continue
            if a in ("-S", "--suffix") and i < len(args):
                i += 1
                continue
            if a.startswith("-") and len(a) > 1:
                continue
        if not _REDIRECT_RE.match(a):
            positionals.extend(_expand_braces(a))
    if not positionals:
        return None
    if target_dir is not None:
        base = _resolve(_expand(target_dir, ctx), ctx.cwd)
        if not base:
            return None
        for src in positionals:
            leaf = os.path.basename(_expand(src, ctx).rstrip("/"))
            if leaf:
                violation = _root_ephemeral_violation(os.path.join(base, leaf))
                if violation:
                    return violation
        return None
    if len(positionals) == 1:
        leaf = os.path.basename(_expand(positionals[0], ctx).rstrip("/"))
        dest = _resolve(leaf, ctx.cwd) if leaf else None
        return _root_ephemeral_violation(dest)
    dest = _resolve(_expand(positionals[-1], ctx), ctx.cwd)
    return _root_ephemeral_violation(dest)


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
        for item in _expand_braces(a):
            resolved = _resolve(_expand(item, ctx), ctx.cwd)
            violation = _root_ephemeral_violation(resolved)
            if violation:
                return violation
    return None


def _check_cp_mv(args: list[str], ctx: _Context) -> str | None:
    target_dir: str | None = None
    positionals: list[str] = []
    opts_done = False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if not opts_done:
            if a == "--":
                opts_done = True
                continue
            if a in ("-t", "--target-directory") and i < len(args):
                target_dir = args[i]
                i += 1
                continue
            if a.startswith("--target-directory="):
                target_dir = a.split("=", 1)[1]
                continue
            if a in ("-S", "--suffix") and i < len(args):
                i += 1
                continue
            if a.startswith("-") and len(a) > 1:
                continue
        if not _REDIRECT_RE.match(a):
            positionals.extend(_expand_braces(a))
    if target_dir is not None:
        dest = _resolve(_expand(target_dir, ctx), ctx.cwd)
        return _root_ephemeral_violation(dest)
    if len(positionals) >= 2:
        dest = _resolve(_expand(positionals[-1], ctx), ctx.cwd)
        return _root_ephemeral_violation(dest)
    return None


def _is_ephemeral_tmp(path: str) -> bool:
    return path.startswith(("/tmp/", "/private/tmp/")) and "/AI/" not in path


def _is_dangerous_rm_resolved(resolved: str) -> str | None:
    if _TRASH_PATH_RE.search(resolved):
        return REASON_TRASH
    base = os.path.basename(resolved.rstrip("/"))
    if base == ".git" or "/.git/" in (resolved + "/"):
        if not _is_ephemeral_tmp(resolved) and "/work/_scratch/" not in resolved and "/work/_tests/" not in resolved:
            return REASON_RM
    if any(c in base for c in "*?["):
        glob_parent = os.path.dirname(resolved.rstrip("/")) or "/"
        if _TRASH_PATH_RE.search(glob_parent):
            return REASON_TRASH
        if (
            _is_protected_root(glob_parent)
            or re.match(r"^/Volumes/[^/]+$", glob_parent)
            or (os.path.exists(os.path.join(glob_parent, ".git")) and not _is_ephemeral_tmp(glob_parent))
        ):
            return REASON_RM
        return None
    if _is_protected_root(resolved):
        return REASON_RM
    if os.path.exists(os.path.join(resolved, ".git")):
        if not _is_ephemeral_tmp(resolved) and "/work/_scratch/" not in resolved and "/work/_tests/" not in resolved:
            return REASON_RM
    return None


def _check_rm(args: list[str], ctx: _Context, via_xargs: bool = False) -> str | None:
    recursive = False
    targets: list[str] = []
    opts_done = False
    for a in args:
        if not opts_done:
            if a == "--":
                opts_done = True
                continue
            if a.startswith("--"):
                recursive = recursive or _has_long_prefix(a.split("=", 1)[0], "--recursive", 5)
                continue
            if a.startswith("-") and len(a) > 1:
                recursive = recursive or "r" in a[1:] or "R" in a[1:]
                continue
        if not _REDIRECT_RE.match(a):
            targets.extend(_expand_braces(a))
    if not recursive:
        return None
    if via_xargs and not targets:
        return REASON_RM
    for target in targets:
        literal = _expand(target, ctx)
        resolved = _resolve(literal, ctx.cwd)
        if _TRASH_PATH_RE.search(literal):
            return REASON_TRASH
        if _PROTECTED_LITERAL_RE.search(literal) and resolved is None:
            return REASON_RM
        if resolved is None:
            # Unresolvable dynamic target (`rm -rf "$UNKNOWN"` / `rm -rf "$(cmd)"`): fail-closed unless scoped to /tmp or $TMPDIR/<subpath>
            if not literal.startswith(("/tmp/", "/private/tmp/", "$TMPDIR/", "${TMPDIR}/")):
                return REASON_RM
            continue
        err = _is_dangerous_rm_resolved(resolved)
        if err:
            return err
    return None


def _check_find(args: list[str], ctx: _Context) -> str | None:
    paths: list[str] = []
    i = 0
    while i < len(args) and args[i] in ("-H", "-L", "-P", "-E", "-X", "-s", "-x"):
        i += 1
    while i < len(args):
        a = args[i]
        if a == "-f" and i + 1 < len(args):
            paths.append(args[i + 1])
            i += 2
            continue
        if a.startswith("-") or a in ("!", "(", ")"):
            break
        paths.append(a)
        i += 1
    if not paths:
        paths = ["."]

    destructive = False
    j = i
    while j < len(args):
        a = args[j]
        if a == "-delete":
            destructive = True
            break
        if a in ("-exec", "-execdir") and j + 1 < len(args):
            sub_prog = os.path.basename(args[j + 1])
            if sub_prog in ("rm", "rmdir", "unlink"):
                destructive = True
                break
        j += 1
    if not destructive:
        return None

    for p in paths:
        lit = _expand(p, ctx)
        res = _resolve(lit, ctx.cwd)
        if _TRASH_PATH_RE.search(lit) or (res and _TRASH_PATH_RE.search(res)):
            return REASON_TRASH
        if res is None:
            if not lit.startswith(("/tmp/", "/private/tmp/", "$TMPDIR/", "${TMPDIR}/")):
                return REASON_RM
            continue
        # Allow find -delete only inside safe build/cache/scratch subtrees or non-AI /tmp
        if _is_ephemeral_tmp(res):
            continue
        if any(f"/{safe}/" in (res + "/") for safe in (*_SAFE_BUILD_OR_CACHE_DIRS, "work/_scratch", "work/_tests")):
            continue
        if _is_protected_root(res) or os.path.exists(os.path.join(res, ".git")) or "/AI/" in res:
            return REASON_RM
    return None


def _check_rsync(args: list[str], ctx: _Context) -> str | None:
    has_delete = False
    dry_run = False
    positionals: list[str] = []
    opts_done = False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if not opts_done:
            if a == "--":
                opts_done = True
                continue
            if a.startswith("--"):
                name = a.split("=", 1)[0]
                if name.startswith("--delete"):
                    has_delete = True
                if name == "--dry-run":
                    dry_run = True
                if name in ("-e", "--rsh", "--exclude", "--include", "--filter", "--files-from", "--log-file") and "=" not in a:
                    i += 1
                continue
            if a.startswith("-") and len(a) > 1:
                if "n" in a[1:]:
                    dry_run = True
                if a in ("-e", "-f") and i < len(args):
                    i += 1
                continue
        if not _REDIRECT_RE.match(a):
            positionals.append(a)
    if not has_delete or dry_run or len(positionals) < 2:
        return None
    dest_lit = _expand(positionals[-1], ctx)
    dest_res = _resolve(dest_lit, ctx.cwd)
    if _TRASH_PATH_RE.search(dest_lit) or (dest_res and _TRASH_PATH_RE.search(dest_res)):
        return REASON_TRASH
    if dest_res and (_is_protected_root(dest_res) or os.path.exists(os.path.join(dest_res, ".git"))):
        return REASON_RM
    if dest_res is None and _PROTECTED_LITERAL_RE.search(dest_lit):
        return REASON_RM
    return None


def _check_python(args: list[str], ctx: _Context) -> str | None:
    for idx, a in enumerate(args):
        if a == "-c" and idx + 1 < len(args):
            code = args[idx + 1]
            if _PYTHON_DESTRUCTIVE_RE.search(code):
                if _TRASH_PATH_RE.search(code):
                    return REASON_TRASH
                if _PROTECTED_LITERAL_RE.search(code):
                    return REASON_RM
            if _PYTHON_LAUNCHCTL_SUBMIT_RE.search(code):
                return REASON_LAUNCHCTL_SUBMIT
            if not ALLOW_HOST_ORCA_QUIT and _OSASCRIPT_ORCA_TARGET_RE.search(code) and _OSASCRIPT_QUIT_RE.search(code):
                return REASON_HOST_ORCA_QUIT
            break
        if not a.startswith("-"):
            break
    return None


def _check_osascript(args: list[str], segment: str) -> str | None:
    if _EMPTY_TRASH_RE.search(segment):
        return REASON_TRASH
    if ALLOW_HOST_ORCA_QUIT:
        return None
    exprs: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "-e" and i < len(args):
            exprs.append(args[i])
            i += 1
            continue
        if a.startswith("-e") and len(a) > 2:
            exprs.append(a[2:])
            continue
        if a in ("-l", "-s") and i < len(args):
            i += 1
            continue
    joined = "\n".join(exprs)
    if joined and _OSASCRIPT_ORCA_TARGET_RE.search(joined) and _OSASCRIPT_QUIT_RE.search(joined):
        return REASON_HOST_ORCA_QUIT
    return None


def _check_killall_pkill(prog: str, args: list[str]) -> str | None:
    if ALLOW_HOST_ORCA_QUIT:
        return None
    takes_val = {"-s", "-u", "-t", "-c", "-P", "-g", "-G", "-F"}
    full_match = False
    operands: list[str] = []
    opts_done = False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if not opts_done:
            if a == "--":
                opts_done = True
                continue
            if a in takes_val and i < len(args):
                i += 1
                continue
            if a.startswith("-") and len(a) > 1:
                if prog == "pkill" and "f" in a[1:] and not a[1:].isdigit():
                    full_match = True
                continue
        if not _REDIRECT_RE.match(a):
            operands.append(a)
    for op in operands:
        low = op.strip().lower()
        if "crashpad" in low:
            continue
        bare = low.lstrip("^").rstrip("$")
        if bare in ("orca", "orca.app", "orca helper"):
            return REASON_HOST_ORCA_QUIT
        if prog == "pkill" and full_match:
            if re.search(r"(?:^|/)orca\.app(?:/|$)|/contents/macos/orca(?:\b|$)", low):
                return REASON_HOST_ORCA_QUIT
    return None


def _check_launchctl(args: list[str]) -> str | None:
    for a in args:
        if a == "--":
            continue
        if a.startswith("-"):
            continue
        return REASON_LAUNCHCTL_SUBMIT if a == "submit" else None
    return None


def _check_gh(args: list[str]) -> str | None:
    if not args or args[0] != "api":
        return None
    is_delete = False
    endpoint = ""
    i = 1
    while i < len(args):
        a = args[i]
        i += 1
        if a in ("-X", "--method") and i < len(args):
            if args[i].upper() == "DELETE":
                is_delete = True
            i += 1
            continue
        if a.startswith("-X") and a[2:].upper() == "DELETE":
            is_delete = True
            continue
        if a.startswith("--method=") and a.split("=", 1)[1].upper() == "DELETE":
            is_delete = True
            continue
        if a in ("-H", "--header", "-f", "-F", "--field", "--raw-field", "--input", "-q", "--jq") and i < len(args):
            i += 1
            continue
        if a.startswith("-"):
            continue
        if not endpoint:
            endpoint = a.lstrip("/")
    if is_delete and endpoint:
        m = re.search(r"(?:^|/)git/refs/heads/(.+)$", endpoint)
        if m:
            branch = m.group(1)
            if branch in PROTECTED_BRANCHES or any(fnmatch.fnmatchcase(p, branch) for p in PROTECTED_BRANCHES):
                return REASON_FORCE.format(branch=branch)
    return None


def _has_long_prefix(flag_name: str, target: str, min_len: int) -> bool:
    return flag_name.startswith("--") and len(flag_name) >= min_len and target.startswith(flag_name)


def _git_output(repo_dir: str | None, *args: str) -> str | None:
    if not repo_dir:
        return None
    try:
        r = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", repo_dir, *args],
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
    force = delete = all_branches = dry_run = False
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
            if _has_long_prefix(name, "--dry-run", 5):
                dry_run = True
            if (
                _has_long_prefix(name, "--force", 6)
                or _has_long_prefix(name, "--force-with-lease", 8)
                or _has_long_prefix(name, "--mirror", 5)
            ):
                force = True
            if (
                _has_long_prefix(name, "--all", 5)
                or _has_long_prefix(name, "--mirror", 5)
                or _has_long_prefix(name, "--branches", 6)
            ):
                all_branches = True
            if _has_long_prefix(name, "--delete", 5):
                delete = True
            if name in ("--repo", "--push-option", "--receive-pack", "--exec") and "=" not in a:
                i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            short_flags = a[1:]
            dry_run = dry_run or "n" in short_flags
            force = force or "f" in short_flags
            delete = delete or "d" in short_flags
            if a.endswith("o"):
                i += 1
            continue
        if not _REDIRECT_RE.match(a):
            positionals.append(a)

    if dry_run:
        return None

    refspecs = positionals[1:]
    current: list[str | None] = []

    def current_branch() -> str | None:
        if not current:
            current.append(_git_output(repo_dir, "symbolic-ref", "--short", "-q", "HEAD"))
        return current[0]

    def named(ref: str) -> str | None:
        return current_branch() if ref in ("HEAD", "@") else _branch_name(ref)

    if refspecs:
        k = 0
        saw_non_tag = False
        while k < len(refspecs):
            spec = refspecs[k]
            if spec == "tag" and k + 1 < len(refspecs):
                # `tag <name>` pushes `refs/tags/<name>`; still inspect any trailing refspecs after `<name>`!
                k += 2
                continue
            saw_non_tag = True
            k += 1
            plus = spec.startswith("+")
            body = spec.lstrip("+")
            if body == ":" or any(c in body for c in "*?["):
                # Matching (`:`) or wildcard (`refs/heads/*`) refspecs push arbitrary branch sets; require explicit branch
                return REASON_FORCE_UNKNOWN
            src, sep, dst = body.partition(":")
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
        if not all_branches:
            if not saw_non_tag:
                return None
            return None

    if all_branches:
        listing = _git_output(repo_dir, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        branches = listing.splitlines() if listing is not None else None
        if branches is None:
            return REASON_FORCE_UNKNOWN
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
    clean_require_force_false = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            repo_dir = _resolve(_expand(args[i + 1], ctx), repo_dir)
            i += 2
            continue
        if a == "-c" and i + 1 < len(args):
            cfg_kv = args[i + 1].strip().lower()
            if cfg_kv.startswith("clean.requireforce=") and cfg_kv.split("=", 1)[1] in ("false", "0", "no", "off"):
                clean_require_force_false = True
            i += 2
            continue
        if a.startswith("-c") and len(a) > 2:
            cfg_kv = a[2:].strip().lower()
            if cfg_kv.startswith("clean.requireforce=") and cfg_kv.split("=", 1)[1] in ("false", "0", "no", "off"):
                clean_require_force_false = True
            i += 1
            continue
        if a in ("--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env") and i + 1 < len(args):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        break
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1 :]
    flags = [a for a in rest if a.startswith("-") and a != "--"]
    long_names = [a.split("=", 1)[0] for a in flags if a.startswith("--")]
    short = "".join(a[1:] for a in flags if not a.startswith("--"))
    dry_run = any(_has_long_prefix(n, "--dry-run", 5) for n in long_names) or "n" in short

    if sub == "reset" and any(_has_long_prefix(n, "--hard", 5) for n in long_names):
        return REASON_RESET
    if sub == "clean" and not dry_run:
        has_force = any(_has_long_prefix(n, "--force", 6) for n in long_names) or "f" in short or clean_require_force_false
        if has_force:
            return REASON_CLEAN
    if sub == "checkout":
        has_force = any(_has_long_prefix(n, "--force", 6) for n in long_names) or "f" in short
        if has_force:
            return REASON_RESET
        if "--" in rest:
            after_dd = rest[rest.index("--") + 1 :]
            if any(p in (".", "./", "*") for p in after_dd):
                return REASON_RESET
    if sub == "restore":
        staged_only = (
            any(_has_long_prefix(n, "--staged", 6) for n in long_names) or "S" in short
        ) and not (any(_has_long_prefix(n, "--worktree", 6) for n in long_names) or "W" in short)
        if not staged_only:
            pos = [a for a in rest if not a.startswith("-") and a != "--"]
            if any(p in (".", "./", "*") for p in pos):
                return REASON_RESET
    if sub == "stash":
        pos = [a for a in rest if not a.startswith("-")]
        if pos and pos[0] == "clear":
            return REASON_RESET
    if sub == "worktree":
        words: list[str] = []
        wt_force = False
        j = 0
        while j < len(rest):
            ra = rest[j]
            if ra in ("-b", "-B", "--reason") and j + 1 < len(rest):
                j += 2
                continue
            if ra.startswith("--"):
                if _has_long_prefix(ra.split("=", 1)[0], "--force", 6):
                    wt_force = True
                j += 1
                continue
            if ra.startswith("-") and len(ra) > 1:
                if "f" in ra[1:]:
                    wt_force = True
                j += 1
                continue
            if not _REDIRECT_RE.match(ra):
                words.append(ra)
            j += 1
        if words and words[0] == "prune" and not dry_run:
            return REASON_PRUNE
        if words and words[0] == "remove" and wt_force:
            return REASON_PRUNE
        if len(words) >= 2 and words[0] == "add":
            wt_dest = _resolve(_expand(words[1], ctx), repo_dir)
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


def _extract_shell_c_script(args: list[str]) -> str | None:
    """POSIX sh/bash/zsh `-c`: `-c` sets flag; command_string is the first non-option operand (even after `--`)."""
    saw_c = False
    opts_done = False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if not opts_done:
            if a == "--":
                opts_done = True
                continue
            if a in ("-o", "+o", "--rcfile", "--init-file") and i < len(args):
                i += 1
                continue
            if _SHORT_C_RE.match(a):
                saw_c = True
                continue
            if a == "--command" and i < len(args):
                return args[i]
            if a.startswith("-") and len(a) > 1:
                continue
            opts_done = True
        if saw_c:
            return a
        break
    return None


def _strip_wrappers_with_meta(tok: list[str], ctx: _Context) -> tuple[list[str], bool, _Context]:
    """Drop leading assignments, keywords, and wrappers; return (remaining_tokens, via_xargs, effective_ctx)."""
    i, n = 0, len(tok)
    via_xargs = False
    temp_vars: dict[str, str] = {}
    while i < n:
        t = tok[i]
        m_asgn = _ASSIGN_RE.match(t)
        if m_asgn:
            temp_vars[m_asgn.group(1)] = _expand(m_asgn.group(2), ctx)
            i += 1
            continue
        if t in _KEYWORDS:
            i += 1
            continue
        base = os.path.basename(t)
        if base not in _WRAPPERS:
            break
        if base == "xargs":
            via_xargs = True
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
            while i < n:
                m_env = _ASSIGN_RE.match(tok[i])
                if not m_env:
                    break
                temp_vars[m_env.group(1)] = _expand(m_env.group(2), ctx)
                i += 1
        elif base in ("timeout", "gtimeout") and i < n:
            i += 1
    rem = tok[i:]
    if not rem and temp_vars:
        # Pure assignment statement (`D=/path/to/Foo`): persist into ctx.vars for subsequent segments!
        ctx.vars.update(temp_vars)
        return [], via_xargs, ctx
    eff_ctx = _Context(ctx.cwd, {**ctx.vars, **temp_vars}) if temp_vars else ctx
    return rem, via_xargs, eff_ctx


def _check_segment(segment: str, ctx: _Context, depth: int) -> str | None:
    # Ultra-fast O(1) skip for plain `echo` / `printf` segments (substitutions were already split out)
    s_lstrip = segment.lstrip()
    if s_lstrip == "echo" or s_lstrip.startswith(("echo ", "printf ")):
        return None

    raw_tok = _tokens(segment)
    if not raw_tok:
        return None
    tok, via_xargs, eff_ctx = _strip_wrappers_with_meta(raw_tok, ctx)
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
            ctx.cwd = _resolve(_expand(words[0], eff_ctx), ctx.cwd)
        return None
    if prog in _SHELLS and depth < _MAX_DEPTH:
        sub_script = _extract_shell_c_script(args)
        if sub_script is not None:
            return _check_script(sub_script, _Context(eff_ctx.cwd, eff_ctx.vars), depth + 1)
        return None
    if prog == "eval" and depth < _MAX_DEPTH:
        return _check_script(" ".join(args), eff_ctx, depth + 1)
    if prog == "xcrun":
        if "simctl" in args:
            return _check_simctl(args[args.index("simctl") + 1 :])
        # Unwrap `xcrun [--sdk ...] <tool> ...` (e.g. `xcrun git reset --hard`)
        k = 0
        while k < len(args):
            xa = args[k]
            if xa in ("-sdk", "--sdk", "-toolchain", "--toolchain") and k + 1 < len(args):
                k += 2
                continue
            if xa.startswith("-"):
                k += 1
                continue
            break
        if k < len(args) and depth < _MAX_DEPTH:
            return _check_segment(" ".join(shlex.quote(x) for x in args[k:]), eff_ctx, depth + 1)
        return None
    if prog == "git":
        return _check_git(args, eff_ctx)
    if prog == "gh":
        return _check_gh(args)
    if prog == "rm":
        return _check_rm(args, eff_ctx, via_xargs=via_xargs)
    if prog == "find":
        return _check_find(args, eff_ctx)
    if prog == "rsync":
        return _check_rsync(args, eff_ctx)
    if _PYTHON_PROG_RE.match(prog):
        return _check_python(args, eff_ctx)
    if prog == "mkdir":
        return _check_mkdir(args, eff_ctx)
    if prog == "ln":
        return _check_ln(args, eff_ctx)
    if prog in ("cp", "mv"):
        return _check_cp_mv(args, eff_ctx)
    if prog == "simctl":
        return _check_simctl(args)
    if prog == "osascript":
        return _check_osascript(args, segment)
    if prog in ("automator", "shortcuts") and _EMPTY_TRASH_RE.search(segment):
        return REASON_TRASH
    if prog in ("killall", "pkill"):
        return _check_killall_pkill(prog, args)
    if prog == "launchctl":
        return _check_launchctl(args)
    return None


def _check_script(script: str, ctx: _Context, depth: int = 0) -> str | None:
    for segment in split_segments(script):
        reason = _check_segment(segment, ctx, depth)
        if reason:
            return reason
    return None


def _mentions_path(command: str, path: str) -> bool:
    return bool(re.search(re.escape(path) + r"(?![\w.-])", command))


def evaluate_file_target(
    file_path: str,
    check_volume: bool = True,
    vol_path: str | None = None,
    expected_uuid: str | None = None,
    cwd: str | None = None,
) -> tuple[bool, str]:
    """Validate direct file-writing tool paths (`Write`, `Edit`, `MultiEdit`, `write_to_file`)."""
    if not file_path or not isinstance(file_path, str):
        return True, ""
    ctx = _Context(cwd)
    expanded = _expand(file_path.strip(), ctx)
    resolved = _resolve(expanded, cwd)
    target_vol = (vol_path or VOLUME_PATH).rstrip("/")
    target_uuid = expected_uuid if expected_uuid is not None else VOLUME_UUID
    if check_volume and target_vol.startswith("/Volumes/"):
        for cand in (expanded, resolved or "", cwd or ""):
            if cand == target_vol or cand.startswith(target_vol + "/"):
                ok, err = verify_volume_mounted_and_uuid(target_vol, target_uuid)
                if not ok:
                    return False, err
                break
    if _TRASH_PATH_RE.search(expanded) or (resolved and _TRASH_PATH_RE.search(resolved)):
        return False, REASON_TRASH
    violation = _root_ephemeral_violation(resolved)
    if violation:
        return False, violation
    return True, ""


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
def _emit_hook_deny(reason: str, is_antigravity: bool) -> int:
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
    raw_tc = payload.get("toolCall")
    tool_call = raw_tc if isinstance(raw_tc, dict) else {}
    raw_ta = tool_call.get("args")
    tool_args = raw_ta if isinstance(raw_ta, dict) else {}
    raw_ti = payload.get("tool_input")
    tool_input = raw_ti if isinstance(raw_ti, dict) else {}

    cwd_raw = (
        tool_input.get("cwd")
        or tool_input.get("workdir")
        or tool_args.get("Cwd")
        or tool_args.get("cwd")
        or payload.get("cwd")
        or None
    )
    cwd = cwd_raw if isinstance(cwd_raw, str) else os.getcwd()

    cmd = (
        tool_input.get("command")
        or tool_input.get("cmd")
        or tool_args.get("CommandLine")
        or tool_args.get("command")
        or payload.get("command")
        or ""
    )
    if isinstance(cmd, list):
        cmd = " ".join(shlex.quote(str(part)) for part in cmd)
    elif not isinstance(cmd, str):
        cmd = ""

    if cmd:
        allowed, reason = evaluate_command(cmd, cwd=cwd)
        if not allowed:
            return _emit_hook_deny(reason, is_antigravity)

    file_target = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_args.get("TargetFile")
        or tool_args.get("AbsolutePath")
        or tool_args.get("absolute_path")
        or ""
    )
    if isinstance(file_target, str) and file_target.strip():
        f_ok, f_reason = evaluate_file_target(file_target, cwd=cwd)
        if not f_ok:
            return _emit_hook_deny(f_reason, is_antigravity)

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
                ["git", "-c", "core.fsmonitor=false", "merge-base", "--is-ancestor", remote_sha, local_sha],
                capture_output=True,
                timeout=20,
            ).returncode
        except Exception:
            ancestor = 0
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
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Multi-Agent PreToolUse & Git Pre-Push Safety Guardrail")
    p.add_argument("--mode", choices=["hook", "check", "verify-volume", "pre-push", "install-pre-push"], default="hook")
    p.add_argument("--command", default="", help="Shell command to evaluate in check mode")
    p.add_argument("--cwd", default=None, help="Working directory for relative paths and git checks")
    p.add_argument("--repo", default=".", help="Repository path for install-pre-push")
    args, extra = p.parse_known_args()

    if args.mode == "verify-volume":
        if not VOLUME_PATH.startswith("/Volumes/"):
            return 0
        ok, err = verify_volume_mounted_and_uuid(VOLUME_PATH, VOLUME_UUID)
        if not ok:
            print(err, file=sys.stderr)
            return 2
        return 0

    if args.mode == "check":
        allowed, reason = evaluate_command(args.command, cwd=args.cwd or os.getcwd())
        if not allowed:
            print(reason, file=sys.stderr)
            return 2
        return 0

    if args.mode == "pre-push":
        remote = extra[0] if len(extra) > 0 else "origin"
        url = extra[1] if len(extra) > 1 else ""
        return run_git_pre_push(remote, url)

    if args.mode == "install-pre-push":
        return install_pre_push(args.repo)

    return run_hook()


if __name__ == "__main__":
    sys.exit(main())
