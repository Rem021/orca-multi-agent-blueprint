#!/usr/bin/env python3
"""Multi-Agent & Multi-Account Profile Synchronizer for Orca + CLI Harnesses.

Ensures zero configuration drift when Orca fails over between primary and backup
accounts across Claude Code, OpenAI Codex, Google Antigravity (Gemini), and DSH.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


def load_json(path: Path):
    """Parsed JSON object of `path`, or None when it is unreadable or not an object.

    A profile file that is mid-write or hand-edited into invalid JSON must not abort the sync of
    every other profile."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data) -> None:
    """Replace `path` atomically, keeping its permissions (agents read these files while running)."""
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    p.add_argument("--dry-run", action="store_true", help="Preview changes without modifying files")
    args = p.parse_args()
    apply = args.apply and not args.dry_run

    home = Path.home()
    orca_dir = home / "Library/Application Support/orca"
    agents_skills = home / ".agents/skills"
    enable_xcode_mcp = os.environ.get("ENABLE_XCODE_MCP", "true").lower() == "true"
    enable_guard = os.environ.get("ENABLE_PRETOOLUSE_GUARDRAILS", "true").lower() == "true"
    startup_timeout = int(os.environ.get("XCODE_MCP_STARTUP_TIMEOUT_SEC", "120"))
    tool_timeout = int(os.environ.get("XCODE_MCP_TOOL_TIMEOUT_SEC", "600"))
    guard_cmd = str(home / ".local/bin/agent-guard") + " --mode hook"

    actions = []
    skipped = []

    def link_skill(target_dir: Path, name: str, source: Path):
        if not source.exists():
            return
        dest = target_dir / name
        if dest.is_symlink():
            try:
                resolved = dest.resolve()
                if not dest.exists() or resolved != source.resolve():
                    actions.append({"action": "heal_symlink", "dest": str(dest), "source": str(source)})
                    if apply:
                        dest.unlink()
                        target_dir.mkdir(parents=True, exist_ok=True)
                        dest.symlink_to(source)
            except OSError:
                if apply:
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    target_dir.mkdir(parents=True, exist_ok=True)
                    dest.symlink_to(source)
            return
        if dest.exists():
            return
        actions.append({"action": "symlink", "dest": str(dest), "source": str(source)})
        if apply:
            target_dir.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(source)

    def prune_broken_symlinks(target_dir: Path):
        if not target_dir.is_dir():
            return
        for item in sorted(target_dir.iterdir()):
            if item.is_symlink() and not item.exists():
                actions.append({"action": "prune_broken_symlink", "dest": str(item)})
                if apply:
                    try:
                        item.unlink()
                    except OSError:
                        pass

    shared_skills = [
        d.name for d in sorted(agents_skills.iterdir())
        if (d.is_dir() or d.is_symlink())
        and not d.name.startswith(".")
        and d.name not in {"synced", "computer-use"}
    ] if agents_skills.is_dir() else []

    # 1. Antigravity & Gemini & Claude & Codex skill directories
    for agy_dir in [
        orca_dir / "antigravity-chat/config/skills",
        orca_dir / "antigravity-chat/antigravity-cli/skills",
        home / ".gemini/skills",
        home / ".gemini/config/skills",
        home / ".claude/skills",
        home / ".codex/skills",
    ]:
        prune_broken_symlinks(agy_dir)
        for s in shared_skills:
            link_skill(agy_dir, s, agents_skills / s)

    chat_gemini_md = orca_dir / "antigravity-chat/GEMINI.md"
    acp_gemini_md = orca_dir / "antigravity-chat/antigravity-acp/GEMINI.md"
    if chat_gemini_md.is_file() and acp_gemini_md.parent.is_dir():
        if not acp_gemini_md.is_file() or acp_gemini_md.read_text(encoding="utf-8") != chat_gemini_md.read_text(encoding="utf-8"):
            actions.append({"action": "sync_antigravity_acp_gemini_md", "dest": str(acp_gemini_md)})
            if apply:
                shutil.copy2(chat_gemini_md, acp_gemini_md)

    # 2. Claude profiles (MCP + PreToolUse guardrail)
    for prof_home in sorted((orca_dir / "claude-profiles").glob("*/home")):
        if (home / ".claude/CLAUDE.md").is_file() and not (prof_home / "CLAUDE.md").exists():
            actions.append({"action": "write_claude_md_ref", "dest": str(prof_home / "CLAUDE.md")})
            if apply:
                (prof_home / "CLAUDE.md").write_text("@~/.claude/CLAUDE.md\n", encoding="utf-8")
        if (home / ".claude/skills").is_dir():
            link_skill(prof_home, "skills", home / ".claude/skills")

        cj = prof_home / ".claude.json"
        if enable_xcode_mcp and cj.is_file():
            data = load_json(cj)
            if data is None:
                skipped.append({"file": str(cj), "reason": "invalid JSON"})
            else:
                mcp = data.setdefault("mcpServers", {})
                if isinstance(mcp, dict) and "xcode" not in mcp:
                    actions.append({"action": "claude_xcode_mcp", "file": str(cj)})
                    if apply:
                        mcp["xcode"] = {"type": "stdio", "command": "/usr/bin/xcrun", "args": ["mcpbridge"], "env": {}}
                        write_json(cj, data)
                        os.chmod(cj, 0o600)

    # 3. Codex accounts (AGENTS.md + hooks.json + xcode MCP + skills)
    codex_skills_dir = home / ".codex/skills"
    xcode_toml = (
        f"\n[mcp_servers.xcode]\n"
        f"startup_timeout_sec = {startup_timeout}\n"
        f"tool_timeout_sec = {tool_timeout}\n"
        f'command = "/usr/bin/xcrun"\n'
        f'args = ["mcpbridge"]\n'
    )
    for acct_home in sorted((orca_dir / "codex-accounts").glob("*/home")):
        if (home / ".codex/AGENTS.md").is_file():
            link_skill(acct_home, "AGENTS.md", home / ".codex/AGENTS.md")
        if (home / ".codex/hooks.json").is_file() and not (acct_home / "hooks.json").is_file():
            actions.append({"action": "copy_hooks_json", "dest": str(acct_home / "hooks.json")})
            if apply:
                shutil.copy2(home / ".codex/hooks.json", acct_home / "hooks.json")
        cfg = acct_home / "config.toml"
        if enable_xcode_mcp and cfg.is_file():
            text = cfg.read_text(encoding="utf-8")
            if "[mcp_servers.xcode]" not in text:
                actions.append({"action": "codex_xcode_mcp", "file": str(cfg)})
                if apply:
                    text = text.rstrip() + "\n" + xcode_toml
                    cfg.write_text(text, encoding="utf-8")
                    os.chmod(cfg, 0o600)
        prune_broken_symlinks(acct_home / "skills")
        if codex_skills_dir.is_dir():
            for item in sorted(codex_skills_dir.iterdir()):
                if not item.name.startswith(".") and item.name not in {"synced", "computer-use"}:
                    link_skill(acct_home / "skills", item.name, item.resolve())

    # 4. PreToolUse Guardrail wiring (also heals stale cross-Mac /Users/<old_user> paths)
    def wire_guard(path: Path, agent: str, new_entry: dict, container_keys: tuple) -> None:
        """Add the guard to the PreToolUse list found under `container_keys`, or repair its path."""
        data = load_json(path)
        if data is None:
            skipped.append({"file": str(path), "reason": "invalid JSON"})
            return
        node = data
        for key in container_keys:
            node = node.setdefault(key, {})
            if not isinstance(node, dict):
                skipped.append({"file": str(path), "reason": f"unexpected structure at {key}"})
                return
        pre = node.setdefault("PreToolUse", [])
        if not isinstance(pre, list):
            skipped.append({"file": str(path), "reason": "unexpected structure at PreToolUse"})
            return
        raw_pre = json.dumps(pre)
        if "agent-guard" not in raw_pre:
            actions.append({"action": f"wire_{agent}_guard", "file": str(path)})
            pre.append(new_entry)
        elif guard_cmd not in raw_pre:
            actions.append({"action": f"update_{agent}_guard_path", "file": str(path)})
            for entry in pre:
                for h_obj in entry.get("hooks", []) if isinstance(entry, dict) else []:
                    if isinstance(h_obj, dict) and "agent-guard" in str(h_obj.get("command", "")):
                        h_obj["command"] = guard_cmd
        else:
            return
        if apply:
            write_json(path, data)

    if enable_guard:
        guard_hook = {"type": "command", "command": guard_cmd, "timeout": 5}
        for sf in [home / ".claude/settings.json"] + sorted((orca_dir / "claude-profiles").glob("*/home/settings.json")):
            if sf.is_file():
                wire_guard(sf, "claude", {"matcher": "Bash", "hooks": [dict(guard_hook)]}, ("hooks",))

        for hf in [home / ".codex/hooks.json"] + sorted((orca_dir / "codex-accounts").glob("*/home/hooks.json")):
            if hf.is_file():
                wire_guard(hf, "codex", {"hooks": [dict(guard_hook)]}, ("hooks",))

        # Antigravity groups hooks by owner; shell commands arrive as the `run_command` tool
        for gf in [home / ".gemini/config/hooks.json", orca_dir / "antigravity-chat/config/hooks.json"]:
            if gf.is_file():
                wire_guard(
                    gf, "antigravity", {"matcher": "run_command", "hooks": [dict(guard_hook)]}, ("agent-guard",)
                )

    report = {"mode": "apply" if apply else "dry-run", "PlannedOrAppliedCount": len(actions), "actions": actions}
    if skipped:
        report["skipped"] = skipped
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
