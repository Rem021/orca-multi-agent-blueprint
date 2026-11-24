#!/usr/bin/env python3
"""Deterministic safety guardrail for agent shell commands, file tools and git pushes.

Installed as ~/.local/bin/agent-guard and registered as the pre-tool hook of Claude Code, Codex, Gemini CLI
and Antigravity (`--mode hook`) and as a git pre-push hook (`--mode pre-push`). Settings come from
~/.orca/guard.env (written by deploy.sh), ~/.orca/backup-config.env and the environment.

Refuses:
1. Touching the configured `/Volumes/<Drive>` workspace while it is not a real mount point, lacks `AI/`,
   or its VolumeUUID differs from `AI_VOLUME_UUID`.
2. Destructive removal (`rm -r`, `find -delete` / `-exec rm`, `xargs rm -r`, `rsync --delete`) of `$HOME`,
   its ancestors, project and worktree roots, `.git`, `AI/{Projects,Archive,Migration,Data}` or
   `/Volumes/*/{Work,Frozen-*}`. Paths are compared case-insensitively and through symlinks (APFS).
3. `git reset --hard`, `git clean -f` (also via abbreviations, `-c`/`GIT_CONFIG_*` `clean.requireForce`
   and `-c alias.*`), `git checkout -f` / `-- .`, `git switch -f`, `git restore .`, `git stash clear`,
   `git worktree remove --force`.
4. `git worktree prune` without `--dry-run`.
5. Emptying the Trash (`.Trash` / `.Trashes` deletion, Finder `empty`).
6. Optional: `xcrun simctl create` / `clone` when `GUARD_BLOCK_SIMULATOR_CREATE=1`.
7. Pushing branches listed in `GUARD_RESTRICTED_BRANCHES` (empty by default).
8. Force-pushing, pruning or deleting `GUARD_PROTECTED_BRANCHES` (default `main master`), including
   `gh api` DELETE / forced PATCH of `git/refs/heads/<branch>`.
9. Creating ad-hoc `tmp`/`scratch`/`.worktrees`-style directories at a project or worktree root.
10. Quitting or killing the host Orca.app (unless `GUARD_ALLOW_HOST_ORCA_QUIT=1`) and `launchctl submit`.
11. Reading private keys and plaintext credential files into the model context.

Out of scope (a guardrail against accidents, not a sandbox): code run by `python -c`, script files,
remote shells, downloaded installers (`curl ... | sh`), and commands whose program name comes from a
variable or command substitution (`R=rm; $R -rf ...`).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shlex
import subprocess
import tempfile
import sys
import time
import urllib.parse


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
    # ~/.orca/guard.env (written by deploy.sh) is the canonical source; the checkout's config.env is never read so
    # that tests and ad-hoc runs from a checkout behave exactly like the installed copy.
    candidates = [
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
        "GUARD_PROTECTED_BRANCHES",
        "GUARD_RESTRICTED_BRANCHES",
        "GUARD_BLOCK_SIMULATOR_CREATE",
        "GUARD_ALLOW_HOST_ORCA_QUIT",
        "GUARD_ALLOW_SECRET_READ",
        "GUARD_ALLOW_WORKTREE_CONFLICT",
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
BLOCK_SIM_CREATE = _CFG.get("GUARD_BLOCK_SIMULATOR_CREATE", "0").strip().lower() in ("1", "true", "yes", "on")
ALLOW_HOST_ORCA_QUIT = _CFG.get("GUARD_ALLOW_HOST_ORCA_QUIT", "0").strip().lower() in ("1", "true", "yes", "on")
PRIMARY_SIM = _CFG.get("PRIMARY_SIMULATOR_UDID", "")
SECONDARY_SIM = _CFG.get("SECONDARY_SIMULATOR_UDID", "")
PROTECTED_BRANCHES = _split_list(_CFG.get("GUARD_PROTECTED_BRANCHES", "main master"))
RESTRICTED_BRANCHES = _split_list(_CFG.get("GUARD_RESTRICTED_BRANCHES", ""))

_SIM_HINT = (
    f"the primary `{PRIMARY_SIM}` or secondary `{SECONDARY_SIM}` simulator"
    if PRIMARY_SIM and SECONDARY_SIM
    else "the assigned simulators"
)

REASON_RESET = "Blocked: this discards uncommitted work (`git reset --hard` and friends). Commit, stash or `git bundle` it first."
REASON_CLEAN = "Blocked `git clean -f`: untracked files may be deliverables. Preview with `git clean -n` and archive what you need first."
REASON_PRUNE = "Blocked `git worktree prune` without `--dry-run` / `git worktree remove --force`. Check the worktrees for uncommitted work first."
REASON_SIM = f"Blocked simulator create/clone (GUARD_BLOCK_SIMULATOR_CREATE=1). Reuse {_SIM_HINT}."
REASON_TRASH = "Blocked: emptying the Trash or deleting from `.Trash` / `.Trashes`."
REASON_RM = f"Blocked destructive delete of a home, project, worktree, archive, data or external-volume directory. Archive it under `{VOLUME_PATH}/AI/Archive/` and move it to the Trash, or use `git worktree remove`."
REASON_RESTRICTED = "Blocked push of restricted branch `{branch}` (GUARD_RESTRICTED_BRANCHES: {patterns})."
REASON_FORCE = "Blocked force-push, prune or deletion of shared branch `{branch}`. Push a normal commit or use a task branch."
REASON_FORCE_UNKNOWN = "Blocked: this force/wildcard/prune push does not name one safe branch. Spell it out, e.g. `git push --force-with-lease origin <task-branch>`."
REASON_ROOT_WORKTREES = f"Blocked `.worktrees/` at a project or worktree root. Create parallel worktrees under `{VOLUME_PATH}/AI/{{Orca,Codex,Claude}}/Worktrees/`."
REASON_ROOT_CLUTTER = "Blocked `{name}/` at a project or worktree root. Use `work/_scratch/` (`agent-governor scratch --topic <topic>`), `work/_tests/` (`agent-governor test-dir --topic <topic>`) or the system temp dir."
REASON_HOST_ORCA_QUIT = "Blocked quitting or killing the host Orca.app: it would end every running agent session. Ask the user to restart Orca."
REASON_LAUNCHCTL_SUBMIT = "Blocked deprecated `launchctl submit` (unthrottled KeepAlive jobs respawn in a loop). Use a throttled LaunchAgent plist."
REASON_TOO_COMPLEX = "Blocked: the command is too complex for agent-guard to analyse safely. Split it into simpler commands."
REASON_INTERNAL = "Blocked: agent-guard failed ({error}) and fails closed for shell and write tools."

_MAX_DEPTH = 4
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_KEYWORDS = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}", "fi", "done"}
# Wrapper programs that run their operands as a command, with the options that consume a separate value.
_WRAPPERS: dict[str, set[str]] = {
    "sudo": {
        "-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-T", "-U", "-R",
        "--user", "--group", "--host", "--prompt", "--close-from", "--chdir", "--role", "--type",
        "--command-timeout", "--other-user", "--chroot",
    },
    "env": {"-u", "-C", "-S", "-P", "-a", "--unset", "--chdir", "--split-string", "--argv0"},
    "command": set(),
    "exec": {"-a"},
    "builtin": set(),
    "noglob": set(),
    "nocorrect": set(),
    "nohup": set(),
    "time": set(),
    "nice": {"-n", "--adjustment"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "gtimeout": {"-s", "-k", "--signal", "--kill-after"},
    "caffeinate": {"-t", "-w"},
    "arch": {"-e"},
    "xargs": {
        "-n", "-I", "-P", "-L", "-J", "-E", "-s", "-R", "-S", "-d", "-a",
        "--max-args", "--max-procs", "--delimiter", "--arg-file", "--max-chars", "--process-slot-var",
    },
}
_CHDIR_OPTS = {"env": {"-C", "--chdir"}, "sudo": {"-D", "--chdir"}}
_ENV_SPLIT_OPTS = {"-S", "--split-string"}
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_HEREDOC_RE = re.compile(r"<<(?P<dash>-?)[ \t]*(?P<q>['\"]?)(?P<delim>[A-Za-z_][A-Za-z0-9_.-]*)(?P=q)")
_SHELL_WORD_RE = re.compile(r"(?<![\w.-])(?:ba|z|da|k)?sh(?![\w.-])")
_REDIRECT_RE = re.compile(r"^\d*[<>]")
# APFS is case-insensitive by default, so every protected-path pattern ignores case.
_PROTECTED_LITERAL_RE = re.compile(
    r"(?:/AI/(?:Projects|Archive|Migration|Data|[^/\s]+/Worktrees)|/AI[-_]Workspace/Data|/Volumes/[^/\s]+/(?:Work|Archive|Data|Backups|Migration|Frozen-[^/\s]+))(?:/|$)",
    re.IGNORECASE,
)
_PROTECTED_SUBTREE_RE = re.compile(
    r"^(?:.*/AI/(?:Archive|Migration|Data)(?:/.*)?|.*/AI[-_]Workspace/Data(?:/.*)?|/Volumes/[^/]+/(?:Work|Archive|Data|Backups|Migration|Frozen-[^/]+)(?:/.*)?)$",
    re.IGNORECASE,
)
_PROTECTED_PROJECT_RE = re.compile(
    r"^.*/AI/Projects(?:/[^/]+(?:/(?:docs|reports|outputs|work)|/outputs/[^/]+)?)?$", re.IGNORECASE
)
_WORKTREE_PATH_RE = re.compile(r"^(.*/AI/[^/]+/Worktrees)(?:/(.*))?$", re.IGNORECASE)
_VOLUME_ROOT_RE = re.compile(r"^/Volumes/[^/]+$", re.IGNORECASE)
_TRASH_PATH_RE = re.compile(r"(?:^|/)\.Trash(?:es)?(?:/|$)", re.IGNORECASE)
_EMPTY_TRASH_RE = re.compile(r"empty\s+(?:the\s+)?trash|EmptyTrash", re.IGNORECASE)
_FINDER_RE = re.compile(r"\bFinder\b", re.IGNORECASE)
_EMPTY_WORD_RE = re.compile(r"\bempty\b", re.IGNORECASE)
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
_VAR_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^{}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_BRACE_RE = re.compile(r"^(.*?)\{([^{}$]+,[^{}$]+)\}(.*)$")
_BRACE_GROUP_RE = re.compile(r"\{[^{}$]*,[^{}$]*\}")
_BRACE_LIMIT = 64
_MKTEMP_PLACEHOLDER = "/tmp/agent-guard-mktemp"

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
_WORKSPACE_ANCHOR_RE = re.compile(r"/AI/(?:Projects/[^/]+|[^/]+/Worktrees)(?=/|$)", re.IGNORECASE)
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
_VOL_CACHE_TTL_SEC = 10.0
_MAX_SCANNED_WORKTREES = 32


def _vol_cache_path() -> Path:
    return Path(tempfile.gettempdir()) / f"agent-guard-volcache-{os.getuid()}.json"


def verify_volume_mounted_and_uuid(vol_path: str, expected_uuid: str = "AUTO") -> tuple[bool, str]:
    """Verify external volume mount state and VolumeUUID (fail-closed on fake mountpoints or diskutil failure)."""
    vol_path = vol_path.rstrip("/") or "/"
    p = Path(vol_path)
    external = vol_path.startswith("/Volumes/")
    if external and not os.path.ismount(vol_path):
        return (
            False,
            f"Blocked: volume `{vol_path}` is not mounted (not a real mount point). Never create folders at a mount point.",
        )
    if not (p / "AI").is_dir():
        return (
            False,
            f"Blocked: `{vol_path}/AI` is not visible, so the volume is probably not mounted.",
        )
    if external and expected_uuid and expected_uuid not in ("AUTO", "LOCAL", "INTERNAL"):
        try:
            vol_dev: int | None = int(os.stat(vol_path).st_dev)
        except OSError:
            vol_dev = None
        now = time.time()
        cache_file = _vol_cache_path()
        cache_key = f"{vol_path}|{expected_uuid}|{vol_dev}" if vol_dev is not None else ""
        if cache_key:
            try:
                if cache_file.is_file():
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    ts = float(cached.get(cache_key, 0.0))
                    if 0.0 <= (now - ts) < _VOL_CACHE_TTL_SEC:
                        return True, ""
            except Exception:
                pass
        try:
            out = subprocess.check_output(
                ["/usr/sbin/diskutil", "info", "-plist", vol_path],
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            info = plistlib.loads(out)
        except subprocess.TimeoutExpired:
            return (
                False,
                f"Blocked: the VolumeUUID check of `{vol_path}` timed out (fail-closed).",
            )
        except Exception:
            info = None
        if not isinstance(info, dict) or not info.get("VolumeUUID"):
            return (
                False,
                f"Blocked: could not read the VolumeUUID of `{vol_path}` with diskutil (fail-closed).",
            )
        actual_uuid = str(info.get("VolumeUUID", ""))
        if actual_uuid.upper() != expected_uuid.upper():
            return (
                False,
                f"Blocked: `{vol_path}` has VolumeUUID `{actual_uuid}`, expected `{expected_uuid}`.",
            )
        mount_point = str(info.get("MountPoint", ""))
        if mount_point and mount_point.rstrip("/").casefold() != vol_path.casefold():
            return (
                False,
                f"Blocked: the volume for `{vol_path}` is mounted at `{mount_point}`.",
            )
        if cache_key:
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.is_file() else {}
                if not isinstance(data, dict):
                    data = {}
                data = {k: v for k, v in data.items() if isinstance(v, (int, float)) and (now - v) < 60.0}
                data[cache_key] = now
                tmp_c = cache_file.with_name(f".{cache_file.name}.{os.getpid()}.tmp")
                tmp_c.write_text(json.dumps(data), encoding="utf-8")
                os.replace(tmp_c, cache_file)
            except Exception:
                pass
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
        if text.startswith("$'", j):
            j = _ansi_c_end(text, j)
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
    if re.match(r"^mktemp(?:\s+[^;&|<>`$()]*)?$", s):
        # `mktemp` always creates a brand-new path, so deleting what it returned cannot hit existing work.
        return _MKTEMP_PLACEHOLDER
    return "${_DYNAMIC_SUB}"


def _read_subshell(text: str, i: int) -> tuple[str, int]:
    """Return `(inner_script, index_after_closing_paren)` for a `(...)` subshell starting at `text[i] == '('`."""
    n = len(text)
    depth = 0
    j = i
    quote = ""
    while j < n:
        ch = text[j]
        if quote == "'":
            if ch == "'":
                quote = ""
            j += 1
            continue
        if quote == '"':
            if ch == "\\" and j + 1 < n:
                j += 2
                continue
            if ch == "`" or text.startswith("$(", j):
                _sub, j = _read_substitution(text, j)
                continue
            if ch == '"':
                quote = ""
            j += 1
            continue
        if ch == "#" and (j == i or text[j - 1] in " \t\r\n;|&("):
            eol = text.find("\n", j)
            j = n if eol == -1 else eol
            continue
        if ch == "\\" and j + 1 < n:
            j += 2
            continue
        if text.startswith("$'", j):
            j = _ansi_c_end(text, j)
            continue
        if ch in "'\"":
            quote = ch
            j += 1
            continue
        if ch == "`" or text.startswith("$(", j):
            _sub, j = _read_substitution(text, j)
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
                return text[i + 1 : j], j + 1
        j += 1
    return text[i + 1 :], n


class _ParsedNode:
    __slots__ = ("kind", "text", "subs", "next_op")

    def __init__(self, kind: str, text: str, subs: list[str] | None = None, next_op: str = ";"):
        self.kind = kind  # "cmd" | "subshell"
        self.text = text
        self.subs: list[str] = subs or []
        self.next_op = next_op  # "&&" | "||" | "|" | "&" | ";"


def _parse_shell_nodes(command: str, _depth: int = 0) -> list[_ParsedNode]:
    """Parse a shell script into scope-preserving command and subshell nodes with exact control operators."""
    nodes: list[_ParsedNode] = []
    buf: list[str] = []
    cur_subs: list[str] = []
    header_chars: list[str] = []
    pending: list[tuple[str, bool]] = []
    quote = ""
    i, n = 0, len(command)

    def flush(op: str) -> None:
        seg = "".join(buf).strip()
        buf.clear()
        subs_copy = list(cur_subs)
        cur_subs.clear()
        if seg or subs_copy:
            nodes.append(_ParsedNode("cmd", seg, subs=subs_copy, next_op=op))
        elif nodes and op in ("&&", "||", "|", "&", ";"):
            # Preserve trailing operator attached to a preceding `(subshell)` node (e.g. `(cd /tmp) && rm -rf .`)
            if nodes[-1].next_op == ";":
                nodes[-1].next_op = op

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
                cur_subs.append(inner)
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
        if command.startswith("$'", i):
            end = _ansi_c_end(command, i)
            span = command[i:end]
            buf.append(span)
            header_chars.append(span)
            i = end
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            header_chars.append(ch)
            i += 1
            continue
        if ch == "`" or command.startswith("$(", i):
            inner, i = _read_substitution(command, i)
            cur_subs.append(inner)
            ph = _substitution_placeholder(inner)
            buf.append(ph)
            header_chars.append(ph)
            continue
        if ch == "(" and not "".join(buf).strip():
            # Subshell `( ... )` at command start: keep isolated from parent's cwd/vars!
            inner_sub, i = _read_subshell(command, i)
            sub_list = list(cur_subs)
            cur_subs.clear()
            nodes.append(_ParsedNode("subshell", inner_sub, subs=sub_list, next_op=";"))
            header_chars.append("()")
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
            flush(";")
            i += 1
            header_line = "".join(header_chars)
            header_chars.clear()
            if pending:
                feeds_shell = bool(_SHELL_WORD_RE.search(header_line))
                feeds_osascript = bool(_OSASCRIPT_WORD_RE.search(header_line))
                for delim, strip_tabs in pending:
                    body, i = _read_heredoc(command, i, delim, strip_tabs)
                    if feeds_shell and _depth < _MAX_DEPTH:
                        nodes.append(_ParsedNode("subshell", body, next_op=";"))
                    elif feeds_osascript:
                        nodes.append(_ParsedNode("cmd", f"osascript -e {shlex.quote(body)}", next_op=";"))
                pending.clear()
            continue
        if command.startswith("&&", i):
            flush("&&")
            header_chars.append("&&")
            i += 2
            continue
        if command.startswith("||", i):
            flush("||")
            header_chars.append("||")
            i += 2
            continue
        if ch == "|":
            flush("|")
            header_chars.append("|")
            i += 1
            continue
        if ch in ";()":
            flush(";")
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
            flush("&")
            header_chars.append("&")
            i += 1
            continue
        buf.append(ch)
        header_chars.append(ch)
        i += 1
    flush(";")
    return nodes


_ANSI_SIMPLE = {
    "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b",
    "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}


def _decode_ansi_c(body: str) -> str:
    """Decode the escapes of a bash/zsh `$'...'` string."""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in _ANSI_SIMPLE:
            out.append(_ANSI_SIMPLE[nxt])
            i += 2
            continue
        m = None
        if nxt == "x":
            m = re.match(r"[0-9A-Fa-f]{1,2}", body[i + 2 :])
        elif nxt == "u":
            m = re.match(r"[0-9A-Fa-f]{1,4}", body[i + 2 :])
        elif nxt == "U":
            m = re.match(r"[0-9A-Fa-f]{1,8}", body[i + 2 :])
        if m:
            try:
                out.append(chr(int(m.group(0), 16)))
            except (ValueError, OverflowError):
                pass
            i += 2 + len(m.group(0))
            continue
        if nxt in "01234567":
            m = re.match(r"[0-7]{1,3}", body[i + 1 :])
            out.append(chr(int(m.group(0), 8)))  # type: ignore[union-attr]
            i += 1 + len(m.group(0))  # type: ignore[union-attr]
            continue
        out.append(ch + nxt)
        i += 2
    return "".join(out)


def _ansi_c_end(text: str, i: int) -> int:
    """Index just after the closing quote of a `$'...'` string whose `$` is at text[i]."""
    j, n = i + 2, len(text)
    while j < n and text[j] != "'":
        j += 2 if text[j] == "\\" else 1
    return min(j + 1, n)


def _normalize_ansi_c(segment: str) -> str:
    """Rewrite `$'...'` (ANSI-C) and `$"..."` (locale) quoting into plain quoting that shlex understands."""
    if "$'" not in segment and '$"' not in segment:
        return segment
    out: list[str] = []
    i, n = 0, len(segment)
    quote = ""
    while i < n:
        ch = segment[i]
        if quote == "'":
            out.append(ch)
            if ch == "'":
                quote = ""
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                out.append(segment[i : i + 2])
                i += 2
                continue
            if ch == '"':
                quote = ""
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(segment[i : i + 2])
            i += 2
            continue
        if segment.startswith("$'", i):
            end = _ansi_c_end(segment, i)
            out.append(shlex.quote(_decode_ansi_c(segment[i + 2 : end - 1])))
            i = end
            continue
        if segment.startswith('$"', i):
            i += 1
            continue
        if ch in "'\"":
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


def _tokens(segment: str) -> list[str]:
    segment = _normalize_ansi_c(segment)
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _collapse_braces(token: str) -> str:
    prev = None
    while prev != token:
        prev, token = token, _BRACE_GROUP_RE.sub("*", token)
    return token


def _expand_braces(token: str) -> list[str]:
    """Expand `{a,b}` groups like the shell; past `_BRACE_LIMIT` words fall back to the equivalent glob."""
    if "{" not in token:
        return [token]
    done: list[str] = []
    todo = [token]
    while todo:
        t = todo.pop()
        m = _BRACE_RE.match(t)
        if not m:
            done.append(t)
            continue
        prefix, inner, suffix = m.group(1), m.group(2), m.group(3)
        parts = inner.split(",")
        if len(done) + len(todo) + len(parts) > _BRACE_LIMIT:
            return [_collapse_braces(token)]
        todo.extend(prefix + part + suffix for part in reversed(parts))
    return done


class _Context:
    def __init__(
        self,
        cwd: str | None,
        vars_map: dict[str, str] | None = None,
        possible_cwds: set[str | None] | None = None,
    ):
        self.cwd = cwd
        self.vars: dict[str, str] = dict(vars_map) if vars_map else {}
        self.possible_cwds: set[str | None] = set(possible_cwds) if possible_cwds is not None else {cwd}

    def clone(self) -> "_Context":
        return _Context(self.cwd, dict(self.vars), set(self.possible_cwds))


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
    elif path.startswith("~"):
        head, sep, rest = path[1:].partition("/")
        if head == "+":
            path = "${PWD}" + sep + rest
        elif head == "-":
            path = "${OLDPWD}" + sep + rest
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", head):
            try:
                path = pwd.getpwnam(head).pw_dir + sep + rest
            except KeyError:
                pass
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
        name = m.group(1) or m.group(3) or ""
        if name in base_vars:
            return base_vars[name]
        if name == "TMPDIR" and m.group(2) is not None:
            return "$TMPDIR"  # `${TMPDIR:-/tmp}` is the per-user temp dir either way
        return m.group(0)

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


def _physical(resolved: str, follow_last: bool = False) -> str:
    """Resolve symlinks the way `rm` meets them: parents always, the last component only when it ends in `/`."""
    try:
        if follow_last:
            return os.path.realpath(resolved)
        parent, name = os.path.split(resolved)
        if not name:
            return os.path.realpath(resolved)
        return os.path.join(os.path.realpath(parent or "/"), name)
    except (OSError, ValueError):
        return resolved


def _same_or_under(path: str, root: str) -> bool:
    """Case-insensitive `path == root or path inside root` (APFS default)."""
    if not root:
        return False
    a, b = path.casefold(), root.rstrip("/").casefold()
    return a == b or a.startswith(b + "/")


_SAFE_BUILD_OR_CACHE_CF = frozenset(d.casefold() for d in _SAFE_BUILD_OR_CACHE_DIRS)
_FORBIDDEN_ROOT_EPHEMERALS_CF = frozenset(d.casefold() for d in _FORBIDDEN_ROOT_EPHEMERALS)
_NON_ROOT_CONTAINERS_CF = frozenset(d.casefold() for d in _NON_ROOT_CONTAINERS)
_AI_ROOT_CHILDREN_CF = frozenset(n.casefold() for n in ("Projects", "Archive", "Migration", "Data", "Orca", "Codex", "Claude"))


def _is_ai_root(path: str) -> bool:
    low = path.casefold()
    if low == f"{VOLUME_PATH}/AI".casefold():
        return True
    if EXTERNAL_VOLUME_PATH and low == f"{EXTERNAL_VOLUME_PATH}/AI".casefold():
        return True
    return low.endswith("/ai") and (
        os.path.isdir(os.path.join(path, "Projects")) or os.path.isdir(os.path.join(path, "Migration"))
    )


def _is_ancestor_of_protected(path: str) -> bool:
    norm = (path.rstrip("/") or "/").casefold()
    home = (str(Path.home()).rstrip("/") or "/").casefold()
    if norm in ("/", "/users", "/volumes", "/tmp", "/private", "/private/tmp", "/var", "/private/var", home):
        return True
    if re.match(r"^/users/[^/]+$", norm):  # any user's home directory (`rm -rf ~alice`)
        return True
    anchors = [p.rstrip("/").casefold() for p in (VOLUME_PATH, f"{VOLUME_PATH}/AI", EXTERNAL_VOLUME_PATH, MEDIA_PATH) if p]
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
    folded = [s.casefold() for s in segs]
    # Lexical 2-level (`Orca/Worktrees/<repo>/<wt>`) and 3-level (`Orca/Worktrees/<org>/<repo>/<wt>`) protection
    if len(segs) == 2:
        return not (folded[1] in _SAFE_BUILD_OR_CACHE_CF or folded[1] == "work")
    if len(segs) == 3 and "/orca/worktrees" in wt_base.casefold():
        return not (folded[1] in _SAFE_BUILD_OR_CACHE_CF or folded[2] in _SAFE_BUILD_OR_CACHE_CF or "work" in folded[1:])
    return False


def _is_protected_root(path: str) -> bool:
    """True for a protected root, protected storage subtree, worktree checkout, or ancestor."""
    path = path.rstrip("/") or "/"
    if _is_ancestor_of_protected(path):
        return True
    if VOLUME_PATH and _same_or_under(path, f"{VOLUME_PATH}/Data"):
        return True
    if MEDIA_PATH and _same_or_under(path, MEDIA_PATH):
        return True
    if EXTERNAL_VOLUME_PATH and EXTERNAL_VOLUME_PATH != VOLUME_PATH and _same_or_under(path, EXTERNAL_VOLUME_PATH):
        return True
    if _PROTECTED_SUBTREE_RE.match(path) or _PROTECTED_PROJECT_RE.match(path):
        return True
    if _is_worktree_protected_target(path):
        return True
    if _VOLUME_ROOT_RE.match(path) and (
        os.path.isdir(os.path.join(path, "Work")) or os.path.isdir(os.path.join(path, "AI"))
    ):
        return True
    if _is_ai_root(path):
        return True
    parent, name = os.path.split(path)
    if _is_ai_root(parent):
        return (
            any(c in name for c in "*?[")
            or os.path.isdir(os.path.join(path, "Worktrees"))
            or name.casefold() in _AI_ROOT_CHILDREN_CF
        )
    return False


# ------------------------------------------------------------------------------
# Per-command checks
# ------------------------------------------------------------------------------
def _root_ephemeral_violation(resolved: str | None) -> str | None:
    """Return a block reason if `resolved` creates or writes inside a forbidden ephemeral dir at the innermost project/worktree root."""
    if not resolved:
        return None
    norm = os.path.normpath(resolved)
    matches = list(_WORKSPACE_ANCHOR_RE.finditer(norm))
    if not matches:
        return None

    last_m = matches[-1]
    anchor_path = norm[: last_m.end()]
    tail = norm[last_m.end() :].lstrip("/")
    segments = [s for s in tail.split("/") if s]
    first_child: str | None = None

    if last_m.group(0).casefold().startswith("/ai/projects/"):
        if segments:
            first_child = segments[0]
    else:
        found_git_root = False
        for k in range(1, min(len(segments), 5)):
            anc = os.path.join(anchor_path, *segments[:k])
            if os.path.exists(os.path.join(anc, ".git")):
                if k < len(segments):
                    first_child = segments[k]
                found_git_root = True
                break
        if not found_git_root and len(segments) >= 2:
            for idx in range(1, min(len(segments), 4)):
                if any(prev.casefold() in _NON_ROOT_CONTAINERS_CF for prev in segments[1:idx]):
                    break
                if segments[idx].casefold() in _FORBIDDEN_ROOT_EPHEMERALS_CF:
                    first_child = segments[idx]
                    break

    if first_child is not None and first_child.casefold() in _FORBIDDEN_ROOT_EPHEMERALS_CF:
        if first_child.casefold() == ".worktrees":
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
    return path.startswith(("/tmp/", "/private/tmp/")) and "/ai/" not in path.casefold()


def _is_unanchored_scratch_or_test_path(path: str) -> bool:
    """True only when `path` is inside `/work/_scratch/` or `/work/_tests/` AND no nested `/AI/` workspace hierarchy follows."""
    norm = (path.rstrip("/") + "/").casefold()
    idx = max(norm.rfind("/work/_scratch/"), norm.rfind("/work/_tests/"))
    if idx == -1:
        return False
    after = norm[idx + 1 :]
    return "/ai/" not in ("/" + after)


# Transient git state that git itself tells users to delete when a rebase/cherry-pick is stuck.
_GIT_TRANSIENT_DIRS = frozenset({"rebase-merge", "rebase-apply", "sequencer"})


def _touches_git_dir(resolved: str) -> bool:
    parts = [p.casefold() for p in resolved.rstrip("/").split("/")]
    if ".git" not in parts:
        return False
    after = parts[parts.index(".git") + 1 :]
    return not any(p in _GIT_TRANSIENT_DIRS for p in after)


def _is_dangerous_rm_resolved(resolved: str) -> str | None:
    if _TRASH_PATH_RE.search(resolved):
        return REASON_TRASH
    base = os.path.basename(resolved.rstrip("/"))
    if _touches_git_dir(resolved):
        if not _is_ephemeral_tmp(resolved) and not _is_unanchored_scratch_or_test_path(resolved):
            return REASON_RM
    if any(c in base for c in "*?["):
        glob_parent = os.path.dirname(resolved.rstrip("/")) or "/"
        if _TRASH_PATH_RE.search(glob_parent):
            return REASON_TRASH
        if (
            _is_protected_root(glob_parent)
            or _VOLUME_ROOT_RE.match(glob_parent)
            or (
                os.path.exists(os.path.join(glob_parent, ".git"))
                and not _is_ephemeral_tmp(glob_parent)
                and not _is_unanchored_scratch_or_test_path(glob_parent)
            )
        ):
            return REASON_RM
        return None
    if _is_protected_root(resolved):
        return REASON_RM
    if os.path.exists(os.path.join(resolved, ".git")):
        if not _is_ephemeral_tmp(resolved) and not _is_unanchored_scratch_or_test_path(resolved):
            return REASON_RM
    return None


def _is_dangerous_delete_target(literal: str, resolved: str) -> str | None:
    """Check both the lexical path and the physical path (symlinks resolved like `rm` would)."""
    err = _is_dangerous_rm_resolved(resolved)
    if err:
        return err
    follow_last = literal.endswith("/") or literal.endswith("/.") or literal in (".", "..")
    physical = _physical(resolved, follow_last=follow_last)
    if physical != resolved:
        return _is_dangerous_rm_resolved(physical)
    return None


_TMP_LITERAL_PREFIXES = ("/tmp/", "/private/tmp/", "$TMPDIR/", "${TMPDIR}/")
_DOTDOT_RE = re.compile(r"(?:^|/)\.\.(?:/|$)")


def _program_name(token: str) -> str:
    """Basename of a command word; zsh `=cmd` expands to the path of `cmd`."""
    if token.startswith("=") and len(token) > 1:
        token = token[1:]
    return os.path.basename(token)


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
                recursive = recursive or _has_long_prefix(a.split("=", 1)[0], "--recursive", 3)
                continue
            if a.startswith("-") and len(a) > 1:
                recursive = recursive or "r" in a[1:] or "R" in a[1:]
                continue
        if not _REDIRECT_RE.match(a):
            targets.extend(_expand_braces(a))
    for target in targets:
        literal = _expand(target, ctx)
        if _TRASH_PATH_RE.search(literal):
            return REASON_TRASH
        resolved = _resolve(literal, ctx.cwd)
        if resolved is not None and (_TRASH_PATH_RE.search(resolved) or _TRASH_PATH_RE.search(_physical(resolved))):
            return REASON_TRASH
    if not recursive:
        return None
    if via_xargs:
        # xargs appends unknown stdin items to whatever targets are spelled out.
        return REASON_RM
    for target in targets:
        literal = _expand(target, ctx)
        resolved = _resolve(literal, ctx.cwd)
        if resolved is None:
            # Unresolvable dynamic target (`rm -rf "$UNKNOWN"`): fail closed unless scoped to a temp dir without `..`
            if _PROTECTED_LITERAL_RE.search(literal) or not literal.startswith(_TMP_LITERAL_PREFIXES) or _DOTDOT_RE.search(literal):
                return REASON_RM
            continue
        err = _is_dangerous_delete_target(literal, resolved)
        if err:
            return err
    return None


_FIND_EXEC_ACTIONS = ("-exec", "-execdir", "-ok", "-okdir")
_FIND_DELETERS = frozenset({"rm", "rmdir", "unlink"})
_FIND_LOGIC_OPS = frozenset({"-o", "-or", "!", "-not", ","})
_DELETE_WORD_RE = re.compile(r"(?<![\w./-])(?:rm|rmdir|unlink|find)(?![\w.-])")
_PROJECT_ROOT_RE = re.compile(r"^.*/AI/Projects/[^/]+(?:/.*)?$", re.IGNORECASE)
_PROJECT_PROTECTED_CHILDREN = ("docs", "reports", "outputs", "work", ".git")


def _find_exec_is_destructive(cmd: list[str], ctx: _Context) -> bool:
    rem, _via_xargs, _eff = _strip_wrappers_with_meta(cmd, ctx.clone(), persist_assignments=False)
    if not rem:
        return False
    prog = _program_name(rem[0])
    if prog in _FIND_DELETERS:
        return True
    if prog in _SHELLS:
        script = _extract_shell_c_script(rem[1:])
        return script is None or bool(_DELETE_WORD_RE.search(script))
    return prog == "xargs"


def _inside_checkout(path: str) -> bool:
    """True when `path` is one project/worktree checkout or lies inside one (not a container of several)."""
    if _is_ancestor_of_protected(path) or _is_ai_root(path) or _is_ai_root(os.path.dirname(path)):
        return False
    if _PROJECT_ROOT_RE.match(path):
        return True
    cur = path
    for _ in range(64):
        if os.path.exists(os.path.join(cur, ".git")):
            return not _is_ancestor_of_protected(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return False


def _protected_names_below(root: str) -> set[str]:
    """Names of protected directories one or two levels below `root` (bounded scan)."""
    names = {n for n in _PROJECT_PROTECTED_CHILDREN}
    try:
        with os.scandir(root) as it:
            children = [e for _, e in zip(range(2000), it)]
    except OSError:
        return names
    for e in children:
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if os.path.exists(os.path.join(e.path, ".git")):
            names.add(e.name)
        if e.name.casefold() == "outputs":
            try:
                with os.scandir(e.path) as it2:
                    for _, sub in zip(range(2000), it2):
                        if os.path.exists(os.path.join(sub.path, ".git")):
                            names.add(sub.name)
            except OSError:
                pass
    return names


def _find_filters_are_contained(root: str, name_filters: list[tuple[str, bool]], empty_only: bool) -> bool:
    """A `-name`/`-iname`/`-empty` filtered delete inside one checkout cannot remove the checkout or its `.git`."""
    if not (name_filters or empty_only) or not _inside_checkout(root):
        return False
    if empty_only and not name_filters:
        return True
    protected = _protected_names_below(root) | {os.path.basename(root.rstrip("/")) or "/"}
    for pattern, icase in name_filters:
        pat = pattern.casefold() if icase else pattern
        for name in protected:
            if fnmatch.fnmatchcase(name.casefold() if icase else name, pat):
                return False
    return True


def _check_find(args: list[str], ctx: _Context) -> str | None:
    paths: list[str] = []
    i = 0
    while i < len(args) and args[i] in ("-H", "-L", "-P", "-E", "-X", "-s", "-x", "-d"):
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
    name_filters: list[tuple[str, bool]] = []
    empty_only = False
    other_logic = False
    j = i
    while j < len(args):
        a = args[j]
        if a == "-delete":
            destructive = True
        elif a in _FIND_EXEC_ACTIONS:
            k = j + 1
            cmd: list[str] = []
            while k < len(args) and args[k] not in (";", "+"):
                cmd.append(args[k])
                k += 1
            destructive = destructive or _find_exec_is_destructive(cmd, ctx)
            j = k
        elif a in ("-name", "-iname") and j + 1 < len(args):
            name_filters.append((args[j + 1], a == "-iname"))
            j += 1
        elif a == "-empty":
            empty_only = True
        elif a in _FIND_LOGIC_OPS:
            other_logic = True
        j += 1
    if not destructive:
        return None

    for p in paths:
        lit = _expand(p, ctx)
        res = _resolve(lit, ctx.cwd)
        if _TRASH_PATH_RE.search(lit) or (res and (_TRASH_PATH_RE.search(res) or _TRASH_PATH_RE.search(_physical(res, True)))):
            return REASON_TRASH
        if res is None:
            if not lit.startswith(_TMP_LITERAL_PREFIXES) or _DOTDOT_RE.search(lit):
                return REASON_RM
            continue
        for cand in dict.fromkeys((res, _physical(res, follow_last=True))):
            # Allow find -delete inside safe build/cache/scratch subtrees, non-workspace /tmp, or a filtered delete
            if _is_ephemeral_tmp(cand) or _is_unanchored_scratch_or_test_path(cand):
                continue
            if any(f"/{safe}/" in (cand + "/").casefold() for safe in _SAFE_BUILD_OR_CACHE_CF):
                continue
            if not other_logic and _find_filters_are_contained(cand, name_filters, empty_only):
                continue
            if _is_protected_root(cand) or os.path.exists(os.path.join(cand, ".git")) or "/ai/" in cand.casefold():
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
                if name.startswith("--del"):  # --del, --delete, --delete-after, ...
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
    if dest_res is None:
        return REASON_RM if _PROTECTED_LITERAL_RE.search(dest_lit) else None
    for cand in dict.fromkeys((dest_res, _physical(dest_res, follow_last=True))):
        if _is_protected_root(cand) or os.path.exists(os.path.join(cand, ".git")):
            return REASON_RM
    return None


def _check_python(args: list[str]) -> str | None:
    for idx, a in enumerate(args):
        code: str | None = None
        if a == "-c" and idx + 1 < len(args):
            code = args[idx + 1]
        elif a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
            c_pos = a.index("c", 1)
            # Only short flags before `c` (e.g., -uc, -Bc, -c"...")
            if a[c_pos + 1 :]:
                code = a[c_pos + 1 :]
            elif idx + 1 < len(args):
                code = args[idx + 1]
        if code is not None:
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
    joined = "\n".join(exprs) if exprs else segment
    if _EMPTY_TRASH_RE.search(segment) or (_FINDER_RE.search(joined) and _EMPTY_WORD_RE.search(joined)):
        return REASON_TRASH
    if ALLOW_HOST_ORCA_QUIT:
        return None
    if joined and _OSASCRIPT_ORCA_TARGET_RE.search(joined) and _OSASCRIPT_QUIT_RE.search(joined):
        return REASON_HOST_ORCA_QUIT
    if exprs and _OSASCRIPT_ORCA_TARGET_RE.search(segment) and _OSASCRIPT_QUIT_RE.search(segment):
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
    method = ""
    endpoint = ""
    fields: list[str] = []
    has_input = False
    i = 1
    while i < len(args):
        a = args[i]
        i += 1
        if a in ("-X", "--method") and i < len(args):
            method = args[i].upper()
            i += 1
            continue
        if a.startswith("-X") and len(a) > 2:
            method = a[2:].upper()
            continue
        if a.startswith("--method="):
            method = a.split("=", 1)[1].upper()
            continue
        if a in ("-f", "-F", "--field", "--raw-field") and i < len(args):
            fields.append(args[i])
            i += 1
            continue
        if a.startswith(("--field=", "--raw-field=")):
            fields.append(a.split("=", 1)[1])
            continue
        if a.startswith(("-f", "-F")) and len(a) > 2:
            fields.append(a[2:])
            continue
        if a == "--input" or a.startswith("--input="):
            has_input = True
            i += 1 if a == "--input" else 0
            continue
        if a in ("-H", "--header", "-q", "--jq", "-t", "--template", "--hostname", "--cache", "-p", "--preview") and i < len(args):
            i += 1
            continue
        if a.startswith("-"):
            continue
        if not endpoint:
            endpoint = a.lstrip("/")
    m = re.search(r"(?:^|/)git/refs/heads/([^?#]+)", urllib.parse.unquote(endpoint))
    if not m or not _is_protected(m.group(1)):
        return None
    branch = m.group(1)
    if method == "DELETE":
        return REASON_FORCE.format(branch=branch)
    forced = any(
        f.split("=", 1)[0].strip().lower() == "force" and f.split("=", 1)[-1].strip().lower() in ("true", "1")
        for f in fields
        if "=" in f
    )
    if method == "PATCH" and (forced or has_input):
        return REASON_FORCE.format(branch=branch)
    return None


def _has_long_prefix(flag_name: str, target: str, min_len: int) -> bool:
    """True when `flag_name` is `target` or an abbreviation git/GNU getopt would accept (>= `min_len` chars)."""
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


def _is_protected(branch: str | None) -> bool:
    if not branch:
        return False
    return branch in PROTECTED_BRANCHES or any(fnmatch.fnmatchcase(branch, pat) for pat in PROTECTED_BRANCHES)


def _is_restricted(branch: str) -> bool:
    return any(fnmatch.fnmatchcase(branch, pat) for pat in RESTRICTED_BRANCHES)


def _restricted_reason(branch: str) -> str:
    return REASON_RESTRICTED.format(branch=branch, patterns=", ".join(f"`{p}`" for p in RESTRICTED_BRANCHES))


_REV_SUFFIX_RE = re.compile(r"^(.*?)(?:[~^].*|@\{.*)?$")


def _check_git_push(args: list[str], repo_dir: str | None) -> str | None:
    force = delete = all_branches = dry_run = prune = False
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
            if _has_long_prefix(name, "--force", 6) or _has_long_prefix(name, "--force-with-lease", 8):
                force = True
            if _has_long_prefix(name, "--mirror", 3):
                force = all_branches = prune = True
            if _has_long_prefix(name, "--all", 4) or _has_long_prefix(name, "--branches", 3):
                all_branches = True
            if _has_long_prefix(name, "--prune", 5):
                prune = True
            if _has_long_prefix(name, "--delete", 4):
                delete = True
            if name in ("--repo", "--push-option", "--receive-pack", "--exec") and "=" not in a:
                i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            # Short bundle: `-o` takes the rest of the word (or the next word) as its value.
            bundle = a[1:]
            for pos, c in enumerate(bundle):
                if c == "o":
                    if pos == len(bundle) - 1:
                        i += 1
                    break
                dry_run = dry_run or c == "n"
                force = force or c == "f"
                delete = delete or c == "d"
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
        base = _REV_SUFFIX_RE.match(ref).group(1)  # type: ignore[union-attr]  # `develop~0`, `HEAD^` -> branch part
        if base in ("HEAD", "@", ""):
            return current_branch() if ref else None
        return _branch_name(base)

    if refspecs:
        k = 0
        while k < len(refspecs):
            spec = refspecs[k]
            if spec == "tag" and k + 1 < len(refspecs):
                # `tag <name>` pushes `refs/tags/<name>`; still inspect any trailing refspecs after `<name>`
                k += 2
                continue
            k += 1
            plus = spec.startswith("+")
            body = spec.lstrip("+")
            if body == ":" or any(c in body for c in "*?["):
                # Matching (`:`) or wildcard (`refs/heads/*`) refspecs push arbitrary branch sets; require explicit branch
                return REASON_FORCE_UNKNOWN
            src, sep, dst = body.partition(":")
            dst_branch = _branch_name(dst) if sep and dst else named(src)
            src_branch = named(src) if src else None
            for branch in (src_branch, dst_branch):
                if branch and _is_restricted(branch):
                    return _restricted_reason(branch)
            destructive = force or plus or delete or (bool(sep) and not src)
            if destructive and _is_protected(dst_branch):
                return REASON_FORCE.format(branch=dst_branch)
            if destructive and dst_branch is None:
                return REASON_FORCE_UNKNOWN
        if not all_branches:
            return None

    if all_branches:
        if prune:
            # `--prune --all` / `--mirror` delete every remote branch without a local counterpart
            return REASON_FORCE_UNKNOWN
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
            if _is_protected(branch):
                return REASON_FORCE.format(branch=branch)
    return None


# Built-in git commands; any other subcommand may be an alias and is looked up in the git config.
_GIT_BUILTINS = frozenset("""
add am annotate apply archive bisect blame branch bundle cat-file check-attr check-ignore check-mailmap
check-ref-format checkout checkout-index cherry cherry-pick citool clean clone column commit commit-graph
commit-tree config count-objects credential daemon describe diff diff-files diff-index diff-tree difftool
fast-export fast-import fetch fetch-pack filter-branch for-each-ref for-each-repo format-patch fsck gc
get-tar-commit-id grep gui hash-object help hook index-pack init instaweb interpret-trailers log ls-files
ls-remote ls-tree mailinfo mailsplit maintenance merge merge-base merge-file merge-index merge-tree mktag
mktree multi-pack-index mv name-rev notes pack-objects pack-redundant pack-refs patch-id prune prune-packed
pull push range-diff read-tree rebase reflog remote repack replace request-pull rerere reset restore rev-list
rev-parse revert rm send-email send-pack shortlog show show-branch show-index show-ref sparse-checkout stage
stash status stripspace submodule switch symbolic-ref tag unpack-file unpack-objects update-index update-ref
update-server-info var verify-commit verify-pack verify-tag version whatchanged worktree write-tree lfs
""".split())
_GIT_FALSE = ("false", "no", "off", "0", "")


def _git_config_overrides(global_args: list[str], ctx: _Context) -> dict[str, str]:
    """`-c key=value` options plus `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_PARAMETERS` assignments."""
    pairs: list[str] = []
    k = 0
    while k < len(global_args):
        a = global_args[k]
        if a == "-c" and k + 1 < len(global_args):
            pairs.append(global_args[k + 1])
            k += 2
            continue
        if a.startswith("-c") and len(a) > 2:
            pairs.append(a[2:])
        k += 1
    env = ctx.vars
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    except ValueError:
        count = 0
    for n in range(min(count, 64)):
        key = env.get(f"GIT_CONFIG_KEY_{n}")
        if key:
            pairs.append(f"{key}={env.get(f'GIT_CONFIG_VALUE_{n}', '')}")
    params = env.get("GIT_CONFIG_PARAMETERS", "")
    if params:
        pairs.extend(_tokens(params))
    out: dict[str, str] = {}
    for kv in pairs:
        key, sep, value = kv.partition("=")
        out[key.strip().lower()] = value if sep else "true"
    return out


def _check_git(args: list[str], ctx: _Context, depth: int = 0) -> str | None:
    repo_dir = ctx.cwd
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            repo_dir = _resolve(_expand(args[i + 1], ctx), repo_dir)
            i += 2
            continue
        if a == "-c" and i + 1 < len(args):
            i += 2
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
    global_args, sub, rest = args[:i], args[i], args[i + 1 :]
    overrides = _git_config_overrides(global_args, ctx)

    alias = overrides.get(f"alias.{sub.lower()}")
    if alias is None and sub not in _GIT_BUILTINS and depth < _MAX_DEPTH:
        alias = _git_output(repo_dir or ctx.cwd, "config", "--get", f"alias.{sub}")
    if alias:
        if depth >= _MAX_DEPTH:
            return REASON_TOO_COMPLEX
        if alias.startswith("!"):
            shell_ctx = _Context(repo_dir, dict(ctx.vars))
            return _check_script(alias[1:] + " " + " ".join(shlex.quote(x) for x in rest), shell_ctx, depth + 1)
        return _check_git(global_args + _tokens(alias) + rest, ctx, depth + 1)

    clean_require_force_false = overrides.get("clean.requireforce", "true").strip().lower() in _GIT_FALSE
    value_opts_by_sub = {
        "clean": {"-e", "--exclude"},
        "restore": {"-s", "--source", "--conflict"},
        "checkout": {"-b", "-B", "--orphan", "--conflict", "-U", "--unified", "--pathspec-from-file"},
        "switch": {"-c", "-C", "--create", "--force-create", "--orphan", "--conflict"},
        "worktree": {"-b", "-B", "--reason", "--expire"},
    }
    value_opts = value_opts_by_sub.get(sub, set())
    flags: list[str] = []
    pos_before_dd: list[str] = []
    pos_after_dd: list[str] = []
    seen_dd = False
    k = 0
    while k < len(rest):
        tok = rest[k]
        if not seen_dd and tok == "--":
            seen_dd = True
            k += 1
            continue
        if seen_dd:
            pos_after_dd.append(tok)
            k += 1
            continue
        if tok in value_opts and k + 1 < len(rest):
            flags.append(tok)
            k += 2
            continue
        if tok.startswith("-") and len(tok) > 1:
            # Handle attached short value opt like `-e-n` in `git clean`
            if sub == "clean" and tok.startswith("-e") and len(tok) > 2:
                flags.append("-e")
            else:
                flags.append(tok)
        elif not _REDIRECT_RE.match(tok):
            pos_before_dd.append(tok)
        k += 1
    long_names = [a.split("=", 1)[0] for a in flags if a.startswith("--")]
    short = "".join(a[1:] for a in flags if not a.startswith("--"))
    dry_run = any(_has_long_prefix(n, "--dry-run", 5) for n in long_names) or "n" in short

    def has_long(target: str, min_len: int) -> bool:
        return any(_has_long_prefix(n, target, min_len) for n in long_names)

    if sub == "reset" and has_long("--hard", 3):
        return REASON_RESET
    if sub == "clean" and not dry_run:
        if has_long("--force", 3) or "f" in short or clean_require_force_false:
            return REASON_CLEAN
        persisted = _git_output(repo_dir, "config", "--get", "clean.requireForce")
        if persisted is not None and persisted.strip().lower() in _GIT_FALSE:
            return REASON_CLEAN
    if sub == "checkout":
        if has_long("--force", 3) or "f" in short:
            return REASON_RESET
        if any(p in (".", "./", "*", ":/") for p in pos_after_dd + pos_before_dd):
            return REASON_RESET
    if sub == "switch" and (has_long("--discard-changes", 4) or "--force" in long_names or "f" in short):
        return REASON_RESET
    if sub == "restore":
        staged_only = (has_long("--staged", 6) or "S" in short) and not (has_long("--worktree", 6) or "W" in short)
        if not staged_only:
            if any(p in (".", "./", "*", ":/") for p in pos_before_dd + pos_after_dd):
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
                if _has_long_prefix(ra.split("=", 1)[0], "--force", 3):
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


_OPEN_CONTROL_KW = {"if", "while", "until", "for", "select", "case"}
_CLOSE_CONTROL_KW = {"fi", "done", "esac"}


def _update_control_depth(raw_tok: list[str], cur_depth: int) -> tuple[int, bool]:
    """Track compound control-flow nesting (`if..fi`, `while..done`, `case..esac`) across shell segments."""
    saw_control = cur_depth > 0
    for t in raw_tok:
        if t in _OPEN_CONTROL_KW:
            cur_depth += 1
            saw_control = True
        elif t in _CLOSE_CONTROL_KW:
            saw_control = True
            cur_depth = max(0, cur_depth - 1)
        elif t in ("then", "else", "elif", "do"):
            saw_control = True
    return cur_depth, saw_control


def _short_option_value(opt: str, takes_value: set[str], tok: list[str], i: int) -> tuple[str | None, str | None, int]:
    """Parse a short option bundle such as `-iu root` / `-Cdir`; return (option, value, next_index)."""
    for pos in range(1, len(opt)):
        flag = "-" + opt[pos]
        if flag in takes_value:
            attached = opt[pos + 1 :]
            if attached:
                return flag, attached, i
            if i < len(tok):
                return flag, tok[i], i + 1
            return flag, None, i
    return None, None, i


def _strip_wrappers_with_meta(
    tok: list[str],
    ctx: _Context,
    persist_assignments: bool = True,
) -> tuple[list[str], bool, _Context]:
    """Drop leading assignments, keywords, and wrappers; return (remaining_tokens, via_xargs, effective_ctx).

    `env -S 'cmd args'` is re-split into the command, and `env -C dir` / `sudo -D dir` move the working directory.
    """
    tok = list(tok)
    i, n = 0, len(tok)
    via_xargs = False
    temp_vars: dict[str, str] = {}
    chdir_seen = False
    new_cwd: str | None = ctx.cwd
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
        base = _program_name(t)
        if base not in _WRAPPERS:
            break
        if base == "xargs":
            via_xargs = True
        takes_value = _WRAPPERS[base]
        chdir_opts = _CHDIR_OPTS.get(base, set())
        split_value: str | None = None
        i += 1
        while i < n and tok[i].startswith("-") and (len(tok[i]) > 1 or base == "env"):
            opt = tok[i]
            i += 1
            if opt == "--":
                break
            name: str | None = None
            value: str | None = None
            if opt.startswith("--"):
                name, eq, attached = opt.partition("=")
                if eq:
                    value = attached
                elif name in takes_value and i < n:
                    value = tok[i]
                    i += 1
            else:
                name, value, i = _short_option_value(opt, takes_value, tok, i)
            if value is None:
                continue
            if name in chdir_opts:
                chdir_seen = True
                new_cwd = _resolve(_expand(value, ctx), new_cwd)
            if base == "env" and name in _ENV_SPLIT_OPTS:
                split_value = value
        if base == "env":
            while i < n:
                m_env = _ASSIGN_RE.match(tok[i])
                if not m_env:
                    break
                temp_vars[m_env.group(1)] = _expand(m_env.group(2), ctx)
                i += 1
            if split_value is not None:
                tok = tok[:i] + _tokens(split_value) + tok[i:]
                n = len(tok)
        elif base in ("timeout", "gtimeout") and i < n:
            i += 1
    rem = tok[i:]
    if not rem and temp_vars:
        if persist_assignments:
            ctx.vars.update(temp_vars)
        return [], via_xargs, ctx
    if chdir_seen:
        return rem, via_xargs, _Context(new_cwd, {**ctx.vars, **temp_vars}, {new_cwd})
    eff_ctx = _Context(ctx.cwd, {**ctx.vars, **temp_vars}, ctx.possible_cwds) if temp_vars else ctx
    return rem, via_xargs, eff_ctx


_REDIRECT_OP_RE = re.compile(r"^\d*(?:<<?|>>?|<>|>\||&>>?|>&|<&)$")


def _shell_reads_stdin(args: list[str]) -> bool:
    """True when `sh`/`bash`/... with these args takes its commands from stdin (no `-c`, no script operand)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("<<<"):
            i += 2 if a == "<<<" else 1
            continue
        if _REDIRECT_RE.match(a) or a.startswith("&>"):
            i += 2 if _REDIRECT_OP_RE.match(a) else 1
            continue
        if a == "-s":
            return True
        if a in ("-o", "+o", "--rcfile", "--init-file"):
            i += 2
            continue
        if a.startswith(("-", "+")):
            i += 1
            continue
        return False
    return True


def _here_string(args: list[str]) -> str | None:
    for idx, a in enumerate(args):
        if a == "<<<" and idx + 1 < len(args):
            return args[idx + 1]
        if a.startswith("<<<") and len(a) > 3:
            return a[3:]
    return None


_PRINTF_CONV_RE = re.compile(r"%(?:%|[-#0 +]*\d*(?:\.\d+)?[a-zA-Z])")


def _echo_output(tok: list[str]) -> str | None:
    """Best-effort text printed by `echo` / `print` / `printf` (escapes decoded), or None for other programs."""
    prog, args = _program_name(tok[0]), tok[1:]
    if prog in ("echo", "print"):
        while args and re.match(r"^-[neErR]+$", args[0]):
            args = args[1:]
        return _decode_ansi_c(" ".join(args))
    if prog != "printf":
        return None
    if args and args[0] == "--":
        args = args[1:]
    if not args or args[0] == "-v":
        return ""
    fmt, rest = args[0], args[1:]
    text = ""
    for _ in range(64):
        used = [0]

        def _sub(m: re.Match[str], rest=rest, used=used) -> str:
            if m.group(0) == "%%":
                return "%"
            value = rest[used[0]] if used[0] < len(rest) else ""
            used[0] += 1
            return value

        text += _PRINTF_CONV_RE.sub(_sub, fmt)
        rest = rest[used[0] :]
        if not rest or not used[0]:
            break
    return _decode_ansi_c(text)


def _check_single_cwd_command(
    segment: str,
    tok: list[str],
    via_xargs: bool,
    eff_ctx: _Context,
    depth: int,
) -> str | None:
    if "<" in tok[0] and not tok[0].startswith("<"):
        head, _, tail = tok[0].partition("<")  # `cat<file` is `cat < file`
        tok = [head, "<" + tail, *tok[1:]]
    prog, args = _program_name(tok[0]), tok[1:]
    if prog in ("cd", "pushd", "popd"):
        return None
    redirect_reason = _check_input_redirects(tok, eff_ctx)
    if redirect_reason:
        return redirect_reason
    if prog in _SHELLS and depth < _MAX_DEPTH:
        sub_script = _extract_shell_c_script(args)
        if sub_script is None and _shell_reads_stdin(args):
            sub_script = _here_string(args)
        if sub_script is not None:
            return _check_script(sub_script, eff_ctx.clone(), depth + 1)
        return None
    if prog == "eval" and depth < _MAX_DEPTH:
        return _check_script(" ".join(args), eff_ctx, depth + 1)
    if prog == "xcrun":
        if "simctl" in args:
            return _check_simctl(args[args.index("simctl") + 1 :])
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
        return _check_git(args, eff_ctx, depth)
    if prog == "gh":
        return _check_gh(args)
    if prog == "rm":
        return _check_rm(args, eff_ctx, via_xargs=via_xargs)
    if prog == "find":
        return _check_find(args, eff_ctx)
    if prog == "rsync":
        return _check_rsync(args, eff_ctx)
    if _PYTHON_PROG_RE.match(prog):
        return _check_python(args)
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
    if prog in _SECRET_READ_PROGS:
        return _check_shell_secret_read(prog, args, eff_ctx)
    return None


def _apply_cd_transition(
    prog: str,
    args: list[str],
    ctx: _Context,
    temp_vars: dict[str, str],
    next_op: str,
    in_conditional: bool,
) -> None:
    """Update `ctx.possible_cwds` conservatively across `&&` vs `;`/`||`/conditionals/background `&`."""
    if next_op in ("|", "&"):
        # `cd /tmp & rm -rf .` or `cd /tmp | ...` runs `cd` in a child process; parent cwd is unchanged!
        return
    if prog == "popd":
        new_targets: set[str | None] = {None}
    else:
        words = [a for a in args if not a.startswith("-") or a == "-"]
        if not words:
            new_targets = {str(Path.home())}
        elif words[0] == "-":
            new_targets = {None}
        else:
            new_targets = set()
            for base_cwd in (ctx.possible_cwds or {ctx.cwd}):
                probe_ctx = _Context(base_cwd, {**ctx.vars, **temp_vars})
                new_targets.add(_resolve(_expand(words[0], probe_ctx), base_cwd))

    if next_op == "&&" and not in_conditional:
        # Subsequent command runs ONLY if `cd` succeeded -> safe to narrow to target cwd(s)
        ctx.possible_cwds = new_targets
        ctx.cwd = next(iter(new_targets)) if len(new_targets) == 1 else None
    else:
        # `;`, `\n`, `||`, or inside `if/else/while` -> `cd` may fail or branch may not execute!
        # Conservatively keep BOTH previous possible cwds AND new target cwds.
        ctx.possible_cwds = (ctx.possible_cwds or {ctx.cwd}) | new_targets
        if len(ctx.possible_cwds) == 1:
            ctx.cwd = next(iter(ctx.possible_cwds))


def _check_segment(
    segment: str,
    ctx: _Context,
    depth: int,
    next_op: str = ";",
    in_conditional: bool = False,
) -> str | None:
    s_lstrip = segment.lstrip()
    if s_lstrip == "echo" or s_lstrip.startswith(("echo ", "printf ")):
        return None

    raw_tok = _tokens(segment)
    if not raw_tok:
        return None

    # First check pure assignments / wrappers on primary context
    persist = next_op not in ("|", "&") and not in_conditional
    tok, via_xargs, eff_base = _strip_wrappers_with_meta(raw_tok, ctx, persist_assignments=persist)
    if not tok:
        return None

    prog, args = _program_name(tok[0]), tok[1:]
    if prog in ("cd", "pushd", "popd"):
        _apply_cd_transition(prog, args, ctx, eff_base.vars, next_op=next_op, in_conditional=in_conditional)
        return None

    # Evaluate across ALL candidate working directories in `ctx.possible_cwds` (fail-closed if ANY cwd blocks!)
    candidate_cwds = list(ctx.possible_cwds) if ctx.possible_cwds else [ctx.cwd]
    for cand_cwd in candidate_cwds:
        cand_ctx = _Context(cand_cwd, dict(ctx.vars), ctx.possible_cwds)
        c_tok, c_xargs, c_eff = _strip_wrappers_with_meta(raw_tok, cand_ctx, persist_assignments=False)
        if not c_tok:
            continue
        reason = _check_single_cwd_command(segment, c_tok, c_xargs, c_eff, depth)
        if reason:
            return reason
    return None


def _piped_shell_script(node: _ParsedNode, nxt: _ParsedNode | None, ctx: _Context) -> str | None:
    """Text that `echo ... | sh` / `printf ... | bash` feeds into a shell (downloads like `curl | sh` stay out of scope)."""
    if node.next_op != "|" or nxt is None or nxt.kind != "cmd" or not nxt.text:
        return None
    left, _x, _c = _strip_wrappers_with_meta(_tokens(node.text), ctx.clone(), persist_assignments=False)
    output = _echo_output(left) if left else None
    if not output:
        return None
    right, _x, _c = _strip_wrappers_with_meta(_tokens(nxt.text), ctx.clone(), persist_assignments=False)
    if not right or _program_name(right[0]) not in _SHELLS:
        return None
    if _extract_shell_c_script(right[1:]) is not None or not _shell_reads_stdin(right[1:]) or _here_string(right[1:]):
        return None
    return output


def _check_script(script: str, ctx: _Context, depth: int = 0) -> str | None:
    control_depth = 0
    nodes = _parse_shell_nodes(script, depth)
    for idx, node in enumerate(nodes):
        if depth < _MAX_DEPTH and node.kind == "cmd":
            piped = _piped_shell_script(node, nodes[idx + 1] if idx + 1 < len(nodes) else None, ctx)
            if piped:
                piped_reason = _check_script(piped, ctx.clone(), depth + 1)
                if piped_reason:
                    return piped_reason
        # 1. Command substitutions `$(...)` / `` `...` `` run in subshells: check in a cloned context!
        if depth < _MAX_DEPTH:
            for sub in node.subs:
                sub_reason = _check_script(sub, ctx.clone(), depth + 1)
                if sub_reason:
                    return sub_reason

        # 2. Explicit `( ... )` subshells (and piped shell heredocs) run in a cloned context!
        if node.kind == "subshell":
            if depth < _MAX_DEPTH:
                sub_reason = _check_script(node.text, ctx.clone(), depth + 1)
                if sub_reason:
                    return sub_reason
            continue

        if not node.text:
            continue

        raw_tok = _tokens(node.text)
        control_depth, in_cond = _update_control_depth(raw_tok, control_depth)
        reason = _check_segment(node.text, ctx, depth, next_op=node.next_op, in_conditional=in_cond)
        if reason:
            return reason
    return None


def _mentions_path(command: str, path: str) -> bool:
    return bool(re.search(re.escape(path) + r"(?![\w.-])", command, re.IGNORECASE))


def _mentions_volume(command: str, vol_path: str) -> bool:
    """Literal path, `$AI_VOLUME_PATH`, or `/Volumes` plus the bare volume name (`cd /Volumes && cd <name>`)."""
    if _mentions_path(command, vol_path) or re.search(r"\$\{?AI_VOLUME_PATH\b", command):
        return True
    name = os.path.basename(vol_path.rstrip("/"))
    return bool(
        name
        and re.search(r"/Volumes(?![\w.-])", command, re.IGNORECASE)
        and re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])", command, re.IGNORECASE)
    )


_SECRET_READ_PROGS = frozenset({
    "cat", "head", "tail", "less", "more", "bat", "sed", "awk",
    "strings", "base64", "xxd", "hexdump", "pbcopy", "open",
    "grep", "egrep", "fgrep", "rg", "nl", "od", "cut", "diff", "tac", "rev",
    "fold", "paste", "sort", "uniq", "jq", "yq", "dd", "column", "pr", "expand",
})
_GREP_VALUE_OPTS = {"-e", "-f", "-m", "-A", "-B", "-C", "--include", "--exclude", "--color", "--file", "--regexp"}
_SECRET_READ_VALUE_OPTS: dict[str, set[str]] = {
    "head": {"-n", "-c"},
    "tail": {"-n", "-c"},
    "sed": {"-e", "-f"},
    "awk": {"-F", "-v", "-f"},
    "base64": {"-i", "-o", "-b"},
    "open": {"-a", "-b"},
    "grep": _GREP_VALUE_OPTS,
    "egrep": _GREP_VALUE_OPTS,
    "fgrep": _GREP_VALUE_OPTS,
    "rg": {"-e", "-f", "-g", "-t", "-T", "-m", "-A", "-B", "-C", "--glob", "--type", "--max-count", "--regexp", "--file"},
    "cut": {"-d", "-f", "-c", "-b"},
    "od": {"-t", "-N", "-j", "-A"},
    "sort": {"-k", "-t", "-o"},
    "fold": {"-w"},
    "jq": {"--arg", "--argjson", "-f", "--from-file"},
}
# Programs whose first operand is an expression/pattern unless it was given with -e/-f.
_EXPR_FIRST_PROGS = frozenset({"sed", "awk", "grep", "egrep", "fgrep", "rg", "jq", "yq"})
_SAFE_CRED_SUFFIXES = (".pub", ".example", ".sample", ".template", ".md", ".txt", ".rst")
_PRIVATE_KEY_EXTS = frozenset({".p8", ".p12", ".pfx", ".jks", ".keystore"})
_AUTHKEY_RE = re.compile(r"^AuthKey_[A-Za-z0-9]+", re.IGNORECASE)
_INPUT_REDIRECT_RE = re.compile(r"^\d*<(?![<&(])(.+)$")
_HIGH_CONFLICT_NAMES = frozenset({
    "project.pbxproj", "Package.resolved", "Podfile.lock",
    "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "Cargo.lock",
})


def _secret_read_allowed() -> bool:
    return (os.environ.get("GUARD_ALLOW_SECRET_READ") or _CFG.get("GUARD_ALLOW_SECRET_READ") or "").strip() == "1"


def _is_sensitive_credential_path(path: str | None) -> str | None:
    """Return a block reason when `path` points to a private key or plaintext credential vault."""
    if not path or _secret_read_allowed():
        return None
    norm = os.path.normpath(path)
    base = os.path.basename(norm)
    base_low = base.lower()
    if any(base_low.endswith(s) for s in _SAFE_CRED_SUFFIXES):
        return None

    parts_low = [p.lower() for p in Path(norm).parts]
    ext = os.path.splitext(base_low)[1]

    kind = ""
    if ".ssh" in parts_low and (base_low.startswith("id_") or ext in (".pem", ".key")):
        kind = "SSH private key"
    elif ext in _PRIVATE_KEY_EXTS or _AUTHKEY_RE.match(base):
        kind = "signing/API private key"
    elif base_low == "keychain-secrets.json":
        kind = "plaintext keychain export"
    elif base_low in (".netrc", ".pypirc"):
        kind = "plaintext credentials file"
    elif len(parts_low) >= 2 and parts_low[-2] == ".aws" and base_low == "credentials":
        kind = "AWS credentials file"

    if not kind:
        return None
    return (
        f"Blocked reading {kind} `{base}`: private keys and plaintext credentials must not enter the model context. "
        "Inspect metadata with `ls -l` or `openssl ... -noout`, or set GUARD_ALLOW_SECRET_READ=1 for one run."
    )


def _check_sensitive_read_target(file_path: str, cwd: str | None = None) -> tuple[bool, str]:
    if not file_path or not isinstance(file_path, str):
        return True, ""
    ctx = _Context(cwd)
    expanded = _expand(file_path.strip(), ctx)
    resolved = _resolve(expanded, cwd) or expanded
    reason = (
        _is_sensitive_credential_path(resolved)
        or _is_sensitive_credential_path(expanded)
        or _is_sensitive_credential_path(_physical(resolved, follow_last=True))
    )
    if reason:
        return False, reason
    return True, ""


def _sensitive_operand(value: str, ctx: _Context) -> str | None:
    return _is_sensitive_credential_path(_resolve(_expand(value, ctx), ctx.cwd))


def _check_input_redirects(tok: list[str], ctx: _Context) -> str | None:
    """`cmd < ~/.ssh/id_rsa` / `cmd <~/.ssh/id_rsa` feed a secret into any program."""
    if _secret_read_allowed():
        return None
    for idx, a in enumerate(tok):
        if re.match(r"^\d*<$", a) and idx + 1 < len(tok):
            reason = _sensitive_operand(tok[idx + 1], ctx)
        else:
            m = _INPUT_REDIRECT_RE.match(a)
            reason = _sensitive_operand(m.group(1), ctx) if m else None
        if reason:
            return reason
    return None


def _check_shell_secret_read(prog: str, args: list[str], ctx: _Context) -> str | None:
    if _secret_read_allowed():
        return None
    val_opts = _SECRET_READ_VALUE_OPTS.get(prog, set())
    expr_given = False
    expr_skipped = prog not in _EXPR_FIRST_PROGS
    i = 0
    opts_done = False
    while i < len(args):
        a = args[i]
        if not opts_done and a == "--":
            opts_done = True
            i += 1
            continue
        if not opts_done and a.startswith("--") and "=" in a:
            name, _, value = a.partition("=")
            expr_given = expr_given or name in ("--regexp", "--file", "--from-file")
            reason = _sensitive_operand(value, ctx)
            if reason:
                return reason
            i += 1
            continue
        if not opts_done and a in val_opts and i + 1 < len(args):
            expr_given = expr_given or a in ("-e", "-f", "--regexp", "--file", "--from-file")
            # Option values such as `base64 -i <file>` or `grep -f <file>` are read too.
            reason = _sensitive_operand(args[i + 1], ctx)
            if reason:
                return reason
            i += 2
            continue
        if not opts_done and a.startswith("-") and len(a) > 1:
            i += 1
            continue
        if _REDIRECT_RE.match(a):
            i += 2 if _REDIRECT_OP_RE.match(a) else 1
            continue
        if not expr_skipped and not expr_given:
            # The first operand of sed/awk/grep/rg/jq is the expression, not a file
            expr_skipped = True
            i += 1
            continue
        if prog == "dd":
            a = a[3:] if a.startswith("if=") else ""
        reason = _sensitive_operand(a, ctx) if a else None
        if reason:
            return reason
        i += 1
    return None


def _find_git_worktree_root(path: Path) -> tuple[Path, Path] | None:
    """Walk parent directories to locate `(wt_root, common_git_dir)` with 0 subprocesses."""
    cur = path if path.is_dir() else path.parent
    for cand in (cur, *cur.parents):
        dot_git = cand / ".git"
        if dot_git.is_dir():
            return cand, dot_git
        if dot_git.is_file():
            try:
                first = dot_git.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            except (OSError, IndexError):
                return None
            if not first.lower().startswith("gitdir:"):
                return None
            raw_gd = first.split(":", 1)[1].strip()
            gd = Path(raw_gd) if os.path.isabs(raw_gd) else (cand / raw_gd).resolve()
            cd_file = gd / "commondir"
            if cd_file.is_file():
                try:
                    raw_cd = cd_file.read_text(encoding="utf-8", errors="ignore").strip()
                    cd = Path(raw_cd) if os.path.isabs(raw_cd) else (gd / raw_cd).resolve()
                    return cand, cd
                except OSError:
                    return cand, gd
            return cand, gd
    return None


def _scan_sibling_worktrees_dirty(common_dir: Path, cache_ttl: float = 10.0) -> dict[str, dict]:
    """Return `{wt_realpath: {'branch': str, 'dirty': {rel_path: mtime}}}` using a 10s `/tmp` cache."""
    wt_meta_dir = common_dir / "worktrees"
    if not wt_meta_dir.is_dir():
        return {}
    try:
        has_linked = any(wt_meta_dir.iterdir())
    except OSError:
        return {}
    if not has_linked:
        return {}

    key = hashlib.sha1(str(common_dir).encode("utf-8", errors="ignore")).hexdigest()[:16]
    cache_file = Path(tempfile.gettempdir()) / f"agent-guard-wt-{os.getuid()}-{key}.json"
    now = time.time()
    try:
        if cache_file.is_file() and (now - cache_file.stat().st_mtime) < cache_ttl:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                return cached
    except (OSError, ValueError):
        pass

    # Collect main clone + linked worktree roots directly from gitdir files (no git worktree list needed)
    wt_roots: list[Path] = []
    if common_dir.name == ".git" and common_dir.parent.is_dir():
        wt_roots.append(common_dir.parent)
    try:
        for entry in sorted(wt_meta_dir.iterdir()):
            gf = entry / "gitdir"
            if not gf.is_file():
                continue
            try:
                raw = gf.read_text(encoding="utf-8", errors="ignore").strip()
                p = Path(raw) if os.path.isabs(raw) else (entry / raw).resolve()
                wt_dir = p.parent if p.name == ".git" else p
                if wt_dir.is_dir():
                    wt_roots.append(wt_dir)
            except OSError:
                continue
    except OSError:
        return {}

    result: dict[str, dict] = {}
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    unique_roots = list(dict.fromkeys(os.path.realpath(str(w)) for w in wt_roots))[:_MAX_SCANNED_WORKTREES]

    def _status(real_wt: str) -> tuple[str, dict] | None:
        try:
            st = subprocess.run(
                ["git", "-C", real_wt, "-c", "core.fsmonitor=false", "-c", "core.quotePath=false",
                 "status", "--porcelain", "-z", "--untracked-files=no", "-b"],
                capture_output=True,
                text=True,
                timeout=1.5,
                env=env,
            )
        except Exception:
            return None
        if st.returncode != 0:
            return None
        branch = ""
        dirty: dict[str, float] = {}
        entries = st.stdout.split("\0")
        k = 0
        while k < len(entries):
            ent = entries[k]
            k += 1
            if ent.startswith("## "):
                branch = ent[3:].split("...", 1)[0].strip()
                continue
            if len(ent) < 4:
                continue
            if ent[0] in "RC":
                k += 1  # `-z` puts the rename/copy source in the next entry
            rel = ent[3:]
            try:
                mt = (Path(real_wt) / rel).stat().st_mtime
            except OSError:
                mt = now
            dirty[rel] = mt
        if not dirty:
            return None
        return real_wt, {"branch": branch or Path(real_wt).name, "dirty": dirty}

    # Worktrees are scanned in parallel so the hook stays well inside the agents' hook timeout.
    if unique_roots:
        import concurrent.futures  # only needed on this path; keeps hook start-up fast

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(unique_roots))) as pool:
            for item in pool.map(_status, unique_roots):
                if item:
                    result[item[0]] = item[1]

    try:
        tmp_c = cache_file.with_suffix(f".tmp-{os.getpid()}")
        tmp_c.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_c, cache_file)
    except OSError:
        pass
    return result


def check_worktree_file_collision(
    file_path: str,
    cwd: str | None = None,
    max_age_sec: float = 86400.0,
    cache_ttl: float = 10.0,
) -> tuple[str, str]:
    """Detect concurrent uncommitted edits to the same relative file in a sibling worktree.

    Returns `(severity, message)` where severity is `""`, `"warn"`, or `"block"`.
    """
    if not file_path or not isinstance(file_path, str):
        return "", ""
    if (os.environ.get("GUARD_ALLOW_WORKTREE_CONFLICT") or _CFG.get("GUARD_ALLOW_WORKTREE_CONFLICT") or "").strip() == "1":
        return "", ""
    ctx = _Context(cwd)
    expanded = _expand(file_path.strip(), ctx)
    resolved = _resolve(expanded, cwd) or expanded
    target = Path(resolved)
    found = _find_git_worktree_root(target)
    if not found:
        return "", ""
    wt_root, common_dir = found
    real_wt = os.path.realpath(str(wt_root))
    real_target = os.path.realpath(str(target))
    try:
        rel = os.path.relpath(real_target, real_wt).replace(os.sep, "/")
    except ValueError:
        return "", ""
    if rel.startswith("../") or rel == ".." or rel.startswith(".git/"):
        return "", ""

    siblings = _scan_sibling_worktrees_dirty(common_dir, cache_ttl=cache_ttl)
    if not siblings:
        return "", ""

    now = time.time()
    hits: list[tuple[str, str]] = []
    for sib_root, info in siblings.items():
        if sib_root == real_wt:
            continue
        dirty = info.get("dirty") or {}
        if rel in dirty:
            mt = float(dirty[rel] or 0.0)
            if (now - mt) <= max_age_sec:
                hits.append((str(info.get("branch") or Path(sib_root).name), sib_root))
    if not hits:
        return "", ""

    sib_desc = ", ".join(f"[{br}] ({Path(p).name})" for br, p in hits[:3])
    base = os.path.basename(rel)
    high_conflict = base in _HIGH_CONFLICT_NAMES or base.endswith(".pbxproj")
    if high_conflict:
        return (
            "block",
            f"Blocked concurrent edit of high-conflict file `{rel}`: sibling worktree(s) {sib_desc} have uncommitted "
            "changes to it from the last 24 h. Coordinate or wait for their merge "
            "(override once with GUARD_ALLOW_WORKTREE_CONFLICT=1).",
        )
    return (
        "warn",
        f"[agent-guard] Sibling worktree(s) {sib_desc} also have uncommitted changes to `{rel}` from the last 24 h; "
        "avoid overlapping edits and compare before merging.",
    )


def build_compact_anchor(cwd: str | None = None) -> str:
    """Build a compact (~80-120 token) deterministic ground-truth anchor after compaction."""
    work_dir = os.path.abspath(cwd or os.getcwd())
    parts = [f"[compact-anchor] cwd={work_dir}"]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        top = subprocess.run(
            ["git", "-C", work_dir, "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            env=env,
        )
        if top.returncode == 0 and top.stdout.strip():
            repo_top = top.stdout.strip()
            br = subprocess.run(
                ["git", "-C", repo_top, "-c", "core.fsmonitor=false", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=2, env=env,
            ).stdout.strip()
            head = subprocess.run(
                ["git", "-C", repo_top, "-c", "core.fsmonitor=false", "log", "-1", "--format=%h %s"],
                capture_output=True, text=True, timeout=2, env=env,
            ).stdout.strip()[:90]
            st = subprocess.run(
                ["git", "-C", repo_top, "-c", "core.fsmonitor=false", "status", "--short"],
                capture_output=True, text=True, timeout=2, env=env,
            ).stdout.splitlines()
            parts.append(f"branch={br} head=({head})")
            if st:
                sample = ", ".join(ln.strip() for ln in st[:10])
                more = f" (+{len(st) - 10} more)" if len(st) > 10 else ""
                parts.append(f"dirty({len(st)}): {sample}{more}")
            else:
                parts.append("dirty=0")
    except Exception:
        pass

    sim_udid = os.environ.get("AGENT_TEST_UDID", "").strip()
    if sim_udid:
        parts.append(f"test_sim={sim_udid}")
    else:
        sim_locks = Path(VOLUME_PATH) / "AI/Data/simulator-locks"
        if sim_locks.is_dir():
            leases = sorted(p.name.replace("active-", "") for p in sim_locks.glob("active-*") if p.is_dir())
            if leases:
                parts.append(f"sim_leases={','.join(leases[:3])}")
    return " | ".join(parts)


def run_compact_anchor() -> int:
    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
    cwd = None
    if raw:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
                cwd = payload["cwd"]
        except ValueError:
            pass
    anchor = build_compact_anchor(cwd)
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": anchor,
        }
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


_MAX_VIEW_FILE_SLICE_BYTES = 1_000_000

_BINARY_VIEW_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".heic", ".heif", ".webp", ".gif", ".bmp",
    ".tiff", ".tif", ".ico", ".icns", ".avif", ".dng", ".cr2", ".cr3",
    ".arw", ".nef", ".raf", ".mp4", ".mov", ".m4v", ".mkv", ".avi",
    ".webm", ".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".pdf",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z", ".rar",
    ".dmg", ".iso", ".pkg", ".cpio", ".par", ".so", ".dylib", ".dll",
    ".exe", ".o", ".a", ".class", ".pyc", ".wasm", ".db", ".sqlite",
    ".sqlite3", ".nib", ".car", ".ttc", ".ttf", ".otf", ".woff", ".woff2",
})

_BINARY_MAGIC_PREFIXES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"%PDF-",
    b"PK\x03\x04",
    b"SQLite format 3\x00",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
    b"\xca\xfe\xba\xbe",
    b"\x7fELF",
    b"\x1f\x8b\x08",
    b"BZh",
    b"\xfd7zXZ\x00",
    b"\x28\xb5\x2f\xfd",
    b"bplist00",
)


def _sniff_binary_header(head: bytes) -> bool:
    if not head:
        return False
    if head.startswith(_BINARY_MAGIC_PREFIXES):
        return True
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] in (b"WEBP", b"WAVE", b"AVI "):
        return True
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return True
    if head.startswith((b"\xff\xfe", b"\xfe\xff")) and b"\x00" in head[2:64]:
        return True
    nul_count = head.count(b"\x00")
    if nul_count > max(8, len(head) // 20):
        return True
    return False


def evaluate_view_file_target(
    file_path: str,
    tool_args: dict | None = None,
    cwd: str | None = None,
) -> tuple[bool, str]:
    """Guard Antigravity `view_file` / `client_view_file` against binary and oversized dumps.

    Antigravity's `view_file` decodes any file as UTF-8 text without a binary or size check. Viewing images,
    binaries or multi-MB files floods the turn with replacement characters and can break the session
    (Gemini rejects the turn with a missing `thought_signature` error).
    """
    if not file_path or not isinstance(file_path, str):
        return True, ""
    ctx = _Context(cwd)
    expanded = _expand(file_path.strip(), ctx)
    resolved = _resolve(expanded, cwd) or expanded
    p = Path(resolved)
    try:
        st = p.stat()
        if not p.is_file():
            return True, ""
    except (OSError, ValueError):
        return True, ""

    ext = p.suffix.lower()
    if ext in _BINARY_VIEW_EXTENSIONS:
        return (
            False,
            f"Blocked `view_file` on binary/media file `{p.name}` ({ext}): it is read as text and floods the context. "
            "Use sips, exiftool, plutil -p, sqlite3 or a script instead.",
        )

    try:
        with p.open("rb") as fh:
            head = fh.read(8192)
    except OSError:
        return True, ""

    if _sniff_binary_header(head):
        return (
            False,
            f"Blocked `view_file` on binary file `{p.name}` (binary header or many NUL bytes). "
            "Use file, plutil -p, sqlite3, otool or a script instead.",
        )

    if st.st_size > _MAX_VIEW_FILE_SLICE_BYTES:
        args = tool_args if isinstance(tool_args, dict) else {}
        s_raw = args.get("start_line") if args.get("start_line") is not None else args.get("StartLine")
        e_raw = args.get("end_line") if args.get("end_line") is not None else args.get("EndLine")
        s_line = s_raw if isinstance(s_raw, int) and not isinstance(s_raw, bool) and s_raw > 0 else 1
        e_line = e_raw if isinstance(e_raw, int) and not isinstance(e_raw, bool) and e_raw > 0 else None
        if e_line is None or e_line < s_line or (e_line - s_line) > 2000:
            size_mb = st.st_size / (1024 * 1024)
            return (
                False,
                f"Blocked unbounded `view_file` of `{p.name}` ({size_mb:.1f} MiB). "
                "Pass StartLine and EndLine (at most 2000 lines) or search with rg.",
            )
        # Verify the requested line slice itself does not exceed 1 MB (e.g. single-line minified JS).
        slice_bytes = 0
        try:
            with p.open("rb") as fh:
                for line_no, raw_line in enumerate(fh, 1):
                    if line_no < s_line:
                        continue
                    if line_no > e_line:
                        break
                    slice_bytes += len(raw_line)
                    if slice_bytes > _MAX_VIEW_FILE_SLICE_BYTES:
                        return (
                            False,
                            f"Blocked `view_file` of lines {s_line}..{e_line} of `{p.name}`: the slice exceeds 1 MB "
                            "(minified file?). Use rg or a script to extract a fragment.",
                        )
        except OSError:
            return True, ""

    return True, ""


def evaluate_file_target(
    file_path: str,
    check_volume: bool = True,
    vol_path: str | None = None,
    expected_uuid: str | None = None,
    cwd: str | None = None,
) -> tuple[bool, str]:
    """Validate direct file-writing tool paths (`Write`, `Edit`, `MultiEdit`, `write_to_file`, `write_file`, ...)."""
    if not file_path or not isinstance(file_path, str):
        return True, ""
    ctx = _Context(cwd)
    expanded = _expand(file_path.strip(), ctx)
    resolved = _resolve(expanded, cwd)
    target_vol = (vol_path or VOLUME_PATH).rstrip("/")
    target_uuid = expected_uuid if expected_uuid is not None else VOLUME_UUID
    if check_volume and target_vol.startswith("/Volumes/"):
        physical = _physical(resolved, follow_last=True) if resolved else ""
        for cand in (expanded, resolved or "", physical, cwd or ""):
            if cand and _same_or_under(cand, target_vol):
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
        cwd_on_volume = bool(cwd) and _same_or_under(cwd, target_vol)
        if cwd_on_volume or _mentions_volume(command, target_vol):
            ok, err = verify_volume_mounted_and_uuid(target_vol, target_uuid)
            if not ok:
                return False, err
    try:
        reason = _check_script(command, _Context(cwd))
    except RecursionError:
        reason = REASON_TOO_COMPLEX
    if reason:
        return False, reason
    return True, ""


# ------------------------------------------------------------------------------
# Entry points
# ------------------------------------------------------------------------------
# Hook protocols (one `agent-guard --mode hook` command serves every agent):
# - Claude Code / Codex PreToolUse and Gemini CLI BeforeTool: snake_case `{tool_name, tool_input, cwd}`.
#   Deny = exit 2 + stderr, with a `permissionDecision: "deny"` JSON on stdout. Allow = `{}`. The guard never
#   emits "allow" (that would skip the agent's own permission prompts) and never "ask" (Codex treats it as a
#   hook failure and runs the command anyway).
# - Antigravity PreToolUse: camelCase `{toolCall: {name, args}, conversationId}`. Deny = `{"decision": "deny"}`
#   with exit 0; allow = no output.
_READ_ONLY_FILE_TOOLS = frozenset({"view_file", "client_view_file", "read_file", "read_many_files", "Read"})
_FILE_TARGET_KEYS = ("file_path", "path", "absolute_path", "notebook_path")
_ANTIGRAVITY_FILE_KEYS = ("TargetFile", "AbsolutePath", "absolute_path", "file_path")


def _safe_text(text: str) -> str:
    return text.encode("utf-8", "replace").decode("utf-8", "replace")


def _hook_event(payload: dict) -> str:
    event = payload.get("hook_event_name") or payload.get("hookEventName")
    return event if isinstance(event, str) and event else "PreToolUse"


def _hook_tool_name(payload: dict) -> str:
    raw_tc = payload.get("toolCall")
    tool_call = raw_tc if isinstance(raw_tc, dict) else {}
    name = payload.get("tool_name") or tool_call.get("name") or ""
    return name.strip().rsplit(":", 1)[-1] if isinstance(name, str) else ""


def _emit_hook_deny(reason: str, is_antigravity: bool, event: str = "PreToolUse") -> int:
    reason = _safe_text(reason)
    if is_antigravity:
        print(json.dumps({"decision": "deny", "reason": reason}))
        print(f"[agent-guard] BLOCKED: {reason}", file=sys.stderr)
        return 0
    response = {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": event,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }
    print(json.dumps(response))
    print(f"[agent-guard] BLOCKED: {reason}", file=sys.stderr)
    return 2


def _emit_hook_allow(is_antigravity: bool, event: str = "PreToolUse", context: str = "") -> int:
    context = _safe_text(context)
    if is_antigravity:
        if context:
            print(f"[agent-guard] WARN: {context}", file=sys.stderr)
        return 0
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}))
    else:
        print("{}")
    return 0


def _run_hook_payload(payload: dict, is_antigravity: bool, event: str) -> int:
    raw_tc = payload.get("toolCall")
    tool_call = raw_tc if isinstance(raw_tc, dict) else {}
    raw_ta = tool_call.get("args")
    tool_args = raw_ta if isinstance(raw_ta, dict) else {}
    raw_ti = payload.get("tool_input")
    tool_input = raw_ti if isinstance(raw_ti, dict) else {}

    base_cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    cwd_raw = (
        tool_input.get("cwd")
        or tool_input.get("workdir")
        or tool_input.get("dir_path")  # Gemini CLI run_shell_command
        or tool_input.get("directory")
        or tool_args.get("Cwd")
        or tool_args.get("cwd")
        or base_cwd
        or None
    )
    cwd = cwd_raw if isinstance(cwd_raw, str) else os.getcwd()
    if not os.path.isabs(cwd):
        cwd = os.path.normpath(os.path.join(base_cwd or os.getcwd(), cwd))

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
            return _emit_hook_deny(reason, is_antigravity, event)

    tool_name = _hook_tool_name(payload)
    file_target = next(
        (v for v in [*(tool_input.get(k) for k in _FILE_TARGET_KEYS), *(tool_args.get(k) for k in _ANTIGRAVITY_FILE_KEYS)]
         if isinstance(v, str) and v.strip()),
        "",
    )
    warnings: list[str] = []
    if file_target:
        if tool_name in _READ_ONLY_FILE_TOOLS:
            s_ok, s_reason = _check_sensitive_read_target(file_target, cwd=cwd)
            if not s_ok:
                return _emit_hook_deny(s_reason, is_antigravity, event)
            if is_antigravity:
                v_ok, v_reason = evaluate_view_file_target(file_target, tool_args=tool_args, cwd=cwd)
                if not v_ok:
                    return _emit_hook_deny(v_reason, is_antigravity, event)
        else:
            f_ok, f_reason = evaluate_file_target(file_target, cwd=cwd)
            if not f_ok:
                return _emit_hook_deny(f_reason, is_antigravity, event)
            sev, col_msg = check_worktree_file_collision(file_target, cwd=cwd)
            if sev == "block":
                return _emit_hook_deny(col_msg, is_antigravity, event)
            if sev == "warn" and col_msg:
                warnings.append(col_msg)
    return _emit_hook_allow(is_antigravity, event, "\n".join(warnings))


def run_hook() -> int:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        raw = ""
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    is_antigravity = "toolCall" in payload or "conversationId" in payload
    event = _hook_event(payload)
    try:
        return _run_hook_payload(payload, is_antigravity, event)
    except Exception as exc:  # noqa: BLE001 - a guard must fail in a predictable direction
        if _hook_tool_name(payload) in _READ_ONLY_FILE_TOOLS:
            print(f"[agent-guard] internal error ignored for a read-only tool: {type(exc).__name__}", file=sys.stderr)
            return _emit_hook_allow(is_antigravity, event)
        return _emit_hook_deny(REASON_INTERNAL.format(error=type(exc).__name__), is_antigravity, event)


def _is_null_sha(sha: str) -> bool:
    return bool(sha) and set(sha) == {"0"}


def run_git_pre_push() -> int:
    """git pre-push hook: stdin lines are `<local ref> <local sha> <remote ref> <remote sha>`."""
    for line in sys.stdin:
        parts = line.strip().split()
        if len(parts) < 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts[:4]
        for ref in (local_ref, remote_ref):
            branch = _branch_name(ref)
            if _is_restricted(branch):
                print(f"[agent-guard] {_restricted_reason(branch)}", file=sys.stderr)
                return 1
        if not remote_ref.startswith("refs/heads/"):
            continue
        target = _branch_name(remote_ref)
        if not _is_protected(target) or _is_null_sha(remote_sha):
            continue
        if _is_null_sha(local_sha):
            print(f"[agent-guard] Blocked deletion of shared branch `{target}` on the remote.", file=sys.stderr)
            return 1
        try:
            ancestor = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "merge-base", "--is-ancestor", remote_sha, local_sha],
                capture_output=True,
                timeout=20,
            ).returncode
        except Exception:
            ancestor = 1
        if ancestor != 0:
            print(
                f"[agent-guard] Blocked non-fast-forward push to shared branch `{target}` (it would rewrite remote "
                "history). Run `git pull --rebase` or push a task branch.",
                file=sys.stderr,
            )
            return 1
    return 0


_PRE_PUSH_MARKER = "# agent-guard pre-push"


def install_pre_push(repo: str) -> int:
    """Install a git pre-push hook in one repository (never overwrites a foreign hook)."""
    hook_rel = _git_output(repo, "rev-parse", "--git-path", "hooks/pre-push")
    if not hook_rel:
        print(f"[agent-guard] `{repo}` is not a git repository; no pre-push hook installed.", file=sys.stderr)
        return 1
    hook = Path(hook_rel) if os.path.isabs(hook_rel) else Path(repo) / hook_rel
    if hook.exists() and _PRE_PUSH_MARKER not in hook.read_text(encoding="utf-8", errors="ignore"):
        print(
            f"[agent-guard] `{hook}` is another pre-push hook and was left alone; call "
            "`agent-guard --mode pre-push` from it yourself.",
            file=sys.stderr,
        )
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
    p = argparse.ArgumentParser(description="Multi-agent pre-tool and git pre-push safety guardrail")
    p.add_argument("--mode", choices=["hook", "check", "verify-volume", "pre-push", "install-pre-push", "compact-anchor"], default="hook")
    p.add_argument("--command", default="", help="Shell command to evaluate in check mode")
    p.add_argument("--cwd", default=None, help="Working directory for relative paths and git checks")
    p.add_argument("--repo", default=".", help="Repository path for install-pre-push")
    args, _extra = p.parse_known_args()  # git passes `<remote> <url>` to pre-push hooks

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
        return run_git_pre_push()

    if args.mode == "install-pre-push":
        return install_pre_push(args.repo)

    if args.mode == "compact-anchor":
        return run_compact_anchor()

    return run_hook()


if __name__ == "__main__":
    sys.exit(main())
