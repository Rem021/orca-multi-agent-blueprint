#!/usr/bin/env python3
"""Deterministic safety guardrail for multi-agent commands and git pushes.

Canonical source: outputs/orca-multi-agent-blueprint/scripts/guard.py
Installed runtime path: ~/.local/bin/agent-guard

Enforces hard boundaries defined in AGENTS.md / CLAUDE.md / GEMINI.md:
1. Volume & UUID protection: if a command accesses an external volume under `/Volumes/<Drive>/AI`
   while `<Drive>/AI` is not mounted or its VolumeUUID mismatches `AI_VOLUME_UUID`, block immediately
   so agents never create fake directories on an unmounted mountpoint.
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
import plistlib
import re
import subprocess
import sys


def _load_env_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    home = Path.home()
    candidates = [
        Path(__file__).resolve().parent.parent / "config.env",
        home / ".orca/backup-config.env",
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
                if v:
                    cfg[k.strip()] = v
        except OSError:
            pass
    for k in ("AI_VOLUME_PATH", "AI_VOLUME_UUID", "PRIMARY_SIMULATOR_UDID", "SECONDARY_SIMULATOR_UDID", "STORAGE_MODE"):
        env_val = os.environ.get(k, "").strip()
        if env_val:
            cfg[k] = env_val
    return cfg


_CFG = _load_env_config()
VOLUME_PATH = _CFG.get("AI_VOLUME_PATH", "/Volumes/AI_Workspace").rstrip("/")
VOLUME_UUID = _CFG.get("AI_VOLUME_UUID", "AUTO").strip()
VOLUME_AI_DIR = Path(f"{VOLUME_PATH}/AI")
PRIMARY_SIM = _CFG.get("PRIMARY_SIMULATOR_UDID", "")
SECONDARY_SIM = _CFG.get("SECONDARY_SIMULATOR_UDID", "")

_SIM_HINT = (
    f"主模拟器 `{PRIMARY_SIM}` 或副机 `{SECONDARY_SIM}`"
    if PRIMARY_SIM and SECONDARY_SIM
    else "已分配的主/副模拟器"
)

FORBIDDEN_PATTERNS = [
    (
        re.compile(r"(?:^|[;&|]\s*|\bsudo\s+)git\s+reset\s+--hard\b"),
        f"严禁执行 `git reset --hard`（AGENTS.md 规则）：请先通过 `git bundle` 或压缩包归档未提交改动到 `{VOLUME_PATH}/AI/Archive/<日期>-<主题>/`。",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*|\bsudo\s+)git\s+clean\s+-[a-zA-Z]*f[a-zA-Z]*\b"),
        f"严禁执行 `git clean -f/-fdx`（AGENTS.md 规则）：未跟踪文件可能包含交付物或实验回执，请先核验并归档到 `{VOLUME_PATH}/AI/Archive/`。",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*|\bsudo\s+)git\s+worktree\s+prune\b(?!.*--dry-run)"),
        "严禁不带 `--dry-run` 直接执行 `git worktree prune`（AGENTS.md 规则）：请先运行 `git worktree prune --dry-run` 核对路径。",
    ),
    (
        re.compile(r"\bxcrun\s+simctl\s+(?:create|clone)\b"),
        f"严禁新建或克隆 iOS 模拟器实例（AGENTS.md 规则）：请复用{_SIM_HINT}并检查 `SimulatorCoordination` 租约。",
    ),
    (
        re.compile(r"(?:rm\s+-[a-zA-Z]*r[a-zA-Z]*\s+.*(?:\.Trash|\.Trashes)|EmptyTrash)"),
        "严禁清空废纸篓或直接删除 `.Trash` / `.Trashes` 内容（AGENTS.md 规则）。",
    ),
    (
        re.compile(
            r"(?:^|[;&|]\s*|\bsudo\s+)rm\s+-[a-zA-Z]*(?:rf|fr)[a-zA-Z]*\s+[^\n;|&]*(?:/AI/(?:Projects|Orca/Worktrees|Codex/Worktrees|Claude/Worktrees|Archive))\b"
        ),
        f"严禁对项目、工作树或归档目录直接执行 `rm -rf`（AGENTS.md 规则）：退役工作树请先归档至 `{VOLUME_PATH}/AI/Archive/`，移入同卷废纸篓后使用 `orca worktree rm` 或 `git worktree remove`。",
    ),
    (
        re.compile(r"\bgit\s+push\b[^\n;|&]*\b(?:develop|feat/C-[A-Za-z0-9_-]*)\b"),
        "严禁推送含历史明文密钥的受限分支 `develop` 或 `feat/C-*`（AGENTS.md 规则）。",
    ),
]


def verify_volume_mounted_and_uuid(vol_path: str, expected_uuid: str = "AUTO") -> tuple[bool, str]:
    """Verify external volume mount state and VolumeUUID."""
    p = Path(vol_path)
    ai_dir = p / "AI"
    if not ai_dir.is_dir():
        return (
            False,
            f"外置盘 `{vol_path}` 未挂载或 `AI/` 目录不可见（AGENTS.md 规则）：已阻止操作，严禁在挂载点自建目录。",
        )
    if vol_path.startswith("/Volumes/") and expected_uuid and expected_uuid not in ("AUTO", "LOCAL", "INTERNAL"):
        try:
            out = subprocess.check_output(
                ["/usr/sbin/diskutil", "info", "-plist", vol_path],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            info = plistlib.loads(out)
            actual_uuid = info.get("VolumeUUID", "")
            if actual_uuid and actual_uuid != expected_uuid:
                return (
                    False,
                    f"外置盘 `{vol_path}` 的硬件 UUID 不匹配（期望 `{expected_uuid}`，实际 `{actual_uuid}`）：已阻止操作。",
                )
        except Exception:
            pass
    return True, ""


def evaluate_command(
    command: str,
    check_volume: bool = True,
    vol_path: str | None = None,
    expected_uuid: str | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason). True means allowed."""
    if not command or not isinstance(command, str):
        return True, ""
    target_vol = (vol_path or VOLUME_PATH).rstrip("/")
    target_uuid = expected_uuid if expected_uuid is not None else VOLUME_UUID
    if check_volume and target_vol.startswith("/Volumes/") and target_vol in command:
        ok, err = verify_volume_mounted_and_uuid(target_vol, target_uuid)
        if not ok:
            return False, err
    for pattern, reason in FORBIDDEN_PATTERNS:
        if pattern.search(command):
            return False, reason
    return True, ""


def run_hook() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    tool_input = payload.get("tool_input") or {}
    cmd = (
        tool_input.get("command")
        or tool_input.get("cmd")
        or payload.get("command")
        or ""
    )
    allowed, reason = evaluate_command(cmd)
    if not allowed:
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
    parser = argparse.ArgumentParser(description="Multi-Agent Deterministic Safety Guardrail")
    parser.add_argument("--mode", choices=["hook", "check", "pre-push"], default="hook")
    parser.add_argument("--command", default="", help="Command string when --mode=check")
    parser.add_argument("extra", nargs="*", help="Extra git hook args")
    args = parser.parse_args()

    if args.mode == "hook":
        return run_hook()
    if args.mode == "pre-push":
        remote = args.extra[0] if len(args.extra) > 0 else "origin"
        url = args.extra[1] if len(args.extra) > 1 else ""
        return run_git_pre_push(remote, url)
    if args.mode == "check":
        allowed, reason = evaluate_command(args.command)
        if not allowed:
            print(reason, file=sys.stderr)
            return 2
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
