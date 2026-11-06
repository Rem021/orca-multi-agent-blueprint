#!/usr/bin/env python3
"""Deterministic safety guardrail for multi-agent commands and git pushes.

Installed runtime path: ~/.local/bin/agent-guard

Enforces hard boundaries defined in AGENTS.md / CLAUDE.md / GEMINI.md:
1. Volume protection: if a command accesses an external volume under `/Volumes/<Drive>/AI` while `<Drive>/AI` is not mounted, block immediately so agents never create fake directories on an unmounted mountpoint.
2. No `rm -rf` on project/worktree/archive directories (archive to `${AI_VOLUME_PATH}/AI/Archive` first, then move to Trash).
3. No `git reset --hard` or `git clean -fdx` (preserve uncommitted work via git bundle / archive).
4. No unflagged `git worktree prune` without `--dry-run`.
5. No emptying Trash (`rm -rf ~/.Trash/*` or `.Trashes` deletion).
6. No creating or cloning new iOS Simulators (`xcrun simctl create` / `xcrun simctl clone`); reuse leased devices.
7. No pushing forbidden plaintext-secret branches (`develop`, `feat/C-*`).
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

VOLUME_PATH = os.environ.get("AI_VOLUME_PATH", "/Volumes/AI_Workspace").rstrip("/")
VOLUME_AI_DIR = Path(f"{VOLUME_PATH}/AI")

FORBIDDEN_PATTERNS = [
    (
        re.compile(r"(?:^|[;&|]\s*|\bsudo\s+)git\s+reset\s+--hard\b"),
        "严禁执行 `git reset --hard`（AGENTS.md 规则）：请先通过 `git bundle` 或压缩包归档未提交改动到 `AI/Archive/<日期>-<主题>/`。",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*|\bsudo\s+)git\s+clean\s+-[a-zA-Z]*f[a-zA-Z]*\b"),
        "严禁执行 `git clean -f/-fdx`（AGENTS.md 规则）：未跟踪文件可能包含交付物或实验回执，请先核验并归档到 `AI/Archive/`。",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*|\bsudo\s+)git\s+worktree\s+prune\b(?!.*--dry-run)"),
        "严禁直接执行不带 `--dry-run` 的 `git worktree prune`（AGENTS.md 规则）：外置盘离线或兼容链接需先核查，不能直接清除工作树记录。",
    ),
    (
        re.compile(r"\bxcrun\s+simctl\s+(?:create|clone)\b"),
        "严禁新建或克隆 iOS 模拟器实例（AGENTS.md 规则）：请复用已分配的主/副模拟器并检查 `SimulatorCoordination` 租约。",
    ),
    (
        re.compile(r"(?:rm\s+-[a-zA-Z]*r[a-zA-Z]*\s+.*(?:\.Trash|\.Trashes)|EmptyTrash)"),
        "严禁清空废纸篓（AGENTS.md 规则）：移入废纸篓的工作树与归档副本须保留可恢复性。",
    ),
    (
        re.compile(
            r"(?:^|[;&|]\s*|\bsudo\s+)rm\s+-[a-zA-Z]*(?:rf|fr)[a-zA-Z]*\s+[^\n;|&]*(?:/AI/(?:Projects|Orca/Worktrees|Codex/Worktrees|Claude/Worktrees|Archive))\b"
        ),
        "严禁对项目、工作树或归档目录直接执行 `rm -rf`（AGENTS.md 规则）：退役工作树请先归档至 `AI/Archive/`，移入同卷废纸篓后使用 `orca worktree rm` 或 `git worktree remove`。",
    ),
    (
        re.compile(r"\bgit\s+push\b[^\n;|&]*\b(?:develop|feat/C-[A-Za-z0-9_-]*)\b"),
        "严禁推送含历史明文密钥的受限分支 `develop` 或 `feat/C-*`（AGENTS.md 规则）。",
    ),
]


def evaluate_command(command: str, cwd: str = "", check_volume: bool = True) -> tuple[bool, str]:
    """Return (allowed, reason). True means allowed."""
    if not command or not isinstance(command, str):
        return True, ""
    if check_volume and VOLUME_PATH.startswith("/Volumes/") and VOLUME_PATH in command and not VOLUME_AI_DIR.is_dir():
        return (
            False,
            f"外置盘 `{VOLUME_PATH}` 未挂载或 `AI/` 目录不可见（AGENTS.md 规则）：已阻止操作，严禁在挂载点自建目录。",
        )
    for pattern, reason in FORBIDDEN_PATTERNS:
        if pattern.search(command):
            return False, reason
    return True, ""


def run_hook() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print("{}")
            return 0
        payload = json.loads(raw)
    except Exception:
        print("{}")
        return 0

    tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    if not isinstance(tool_input, dict):
        print("{}")
        return 0

    if tool_name.lower() in {"bash", "run_command", "shell", "exec", "terminal"} or "command" in tool_input:
        cmd = tool_input.get("command") or tool_input.get("CommandLine") or ""
        cwd = str(payload.get("cwd") or tool_input.get("cwd") or "")
        allowed, reason = evaluate_command(str(cmd), cwd)
        if not allowed:
            out = {
                "decision": "block",
                "reason": reason,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
            }
            print(json.dumps(out, ensure_ascii=False))
            print(reason, file=sys.stderr)
            return 2

    print("{}")
    return 0


def run_git_pre_push(remote: str, url: str) -> int:
    for line in sys.stdin:
        parts = line.strip().split()
        if len(parts) < 4:
            continue
        local_ref, _local_sha, remote_ref, _remote_sha = parts[:4]
        for ref in (local_ref, remote_ref):
            branch = ref.replace("refs/heads/", "")
            if branch == "develop" or branch.startswith("feat/C-"):
                print(
                    f"[agent-guard] 阻止推送：受限分支 `{branch}` 含历史敏感配置，严禁推送到远端（AGENTS.md 规则）。",
                    file=sys.stderr,
                )
                return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=["hook", "check-command", "git-pre-push"], default="hook")
    p.add_argument("--command", help="Command string to validate in check-command mode")
    p.add_argument("--remote", default="", help="Git remote name in git-pre-push mode")
    p.add_argument("--url", default="", help="Git remote URL in git-pre-push mode")
    args = p.parse_args()

    if args.mode == "hook":
        return run_hook()
    if args.mode == "check-command":
        allowed, reason = evaluate_command(args.command or "")
        result = {"allowed": allowed, "reason": reason, "command": args.command or ""}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if allowed else 2
    if args.mode == "git-pre-push":
        return run_git_pre_push(args.remote, args.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
