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

    def link_skill(target_dir: Path, name: str, source: Path):
        if not source.exists():
            return
        dest = target_dir / name
        if dest.exists() or dest.is_symlink():
            return
        actions.append({"action": "symlink", "dest": str(dest), "source": str(source)})
        if apply:
            target_dir.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(source)

    shared_skills = [
        d.name for d in sorted(agents_skills.iterdir())
        if (d.is_dir() or d.is_symlink()) and not d.name.startswith(".") and d.name != "synced"
    ] if agents_skills.is_dir() else []

    # 1. Antigravity & Gemini skill directories
    for agy_dir in [
        orca_dir / "antigravity-chat/config/skills",
        orca_dir / "antigravity-chat/antigravity-cli/skills",
        home / ".gemini/config/skills",
        home / ".claude/skills",
        home / ".codex/skills",
    ]:
        for s in shared_skills:
            link_skill(agy_dir, s, agents_skills / s)

    # 2. Claude profiles (MCP + PreToolUse guardrail)
    for prof_home in sorted((orca_dir / "claude-profiles").glob("*/home")):
        if (home / ".claude/CLAUDE.md").is_file() and not (prof_home / "CLAUDE.md").exists():
            actions.append({"action": "write_claude_md_ref", "dest": str(prof_home / "CLAUDE.md")})
            if apply:
                (prof_home / "CLAUDE.md").write_text("@~/.claude/CLAUDE.md\n", encoding="utf-8")
        if (home / ".claude/skills").is_dir() and not (prof_home / "skills").exists():
            link_skill(prof_home, "skills", home / ".claude/skills")

        cj = prof_home / ".claude.json"
        if enable_xcode_mcp and cj.is_file():
            try:
                data = json.loads(cj.read_text(encoding="utf-8"))
                mcp = data.setdefault("mcpServers", {})
                if "xcode" not in mcp:
                    actions.append({"action": "claude_xcode_mcp", "file": str(cj)})
                    if apply:
                        mcp["xcode"] = {"type": "stdio", "command": "/usr/bin/xcrun", "args": ["mcpbridge"], "env": {}}
                        cj.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                        os.chmod(cj, 0o600)
            except Exception:
                pass

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
        if codex_skills_dir.is_dir():
            for item in sorted(codex_skills_dir.iterdir()):
                if not item.name.startswith("."):
                    link_skill(acct_home / "skills", item.name, item.resolve())

    # 4. PreToolUse Guardrail wiring
    if enable_guard:
        for sf in [home / ".claude/settings.json"] + list((orca_dir / "claude-profiles").glob("*/home/settings.json")):
            if sf.is_file():
                data = json.loads(sf.read_text(encoding="utf-8"))
                pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
                if not any("agent-guard" in json.dumps(x) for x in pre):
                    actions.append({"action": "wire_claude_guard", "file": str(sf)})
                    if apply:
                        pre.append({"matcher": "Bash", "hooks": [{"type": "command", "command": guard_cmd, "timeout": 5}]})
                        sf.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        for hf in [home / ".codex/hooks.json"] + list((orca_dir / "codex-accounts").glob("*/home/hooks.json")):
            if hf.is_file():
                data = json.loads(hf.read_text(encoding="utf-8"))
                pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
                if not any("agent-guard" in json.dumps(x) for x in pre):
                    actions.append({"action": "wire_codex_guard", "file": str(hf)})
                    if apply:
                        pre.append({"hooks": [{"type": "command", "command": guard_cmd, "timeout": 5}]})
                        hf.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(json.dumps({"mode": "apply" if apply else "dry-run", "PlannedOrAppliedCount": len(actions), "actions": actions}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
