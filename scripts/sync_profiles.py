#!/usr/bin/env python3
"""Multi-Agent & Multi-Account Profile Synchronizer for Orca + CLI Harnesses.

Ensures zero configuration drift when Orca fails over between primary and backup
accounts across Claude Code, OpenAI Codex, Google Antigravity (Gemini), and DSH.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile


# SHA-256 of the standalone SKILL.md versions shipped before the unified skill.
RETIRED_SKILLS = {
    "agent-governor": {
        "1ba39e5531219849acd992545b91c0046b56ea570120723c318dccb53a88f389",
        "70cf355394aedc08ec0a0d7e83fea8ce8c3a184d55785526d4d724d0ba81ce1e",
        "8342d1ef92f5f496fab25cc30ad2320d45532bd7e32a49dd872fd9d51be61293",
        "a66f5d51024c9413030f8c937c9e029e7e30bc8d9e1aadb242365dc987914876",
        "b2ae546691be1cfb6e976ddcf4ed4d5750e5b1014a2628769e9dbc3f90a2a358",
        "d772565281aef6787b33f8b9398d007af8b83ee42b978de7a7d674c864d07dda",
        "e86befd241be146afabc2d8db2fa2ef41e155cbc6554d85c50bc56b9019250d4",
    },
    "agent-slim": {
        "d154f0bac75ab2791197f3e5a968376cb4fd10d4be97e4e88a48ab944a22ec30",
        "f6648451beb529942b522342701691aac760ac7459ab83f3a68fbdb8040a8f49",
    },
    "ast-grep": {
        "4ed251d3af4db54b7eabb5516e0f6f52a84da1d157529de8c6d42d86f8521256",
        "751533cbd4243a81d1410e109cdc1997317a83c8dc39becdcc7079596e70a292",
        "f2b2c1bca8dac37fa74f09cecfd3e3606be5a8f41abe8ad3c77f17428196decf",
    },
}


def shipped_legacy(directory, name):
    if name not in RETIRED_SKILLS or not directory.is_dir():
        return False
    doc = directory / "SKILL.md"
    if not doc.is_file() or doc.is_symlink():
        return False
    if any(p.name not in {"SKILL.md", ".DS_Store"} for p in directory.iterdir()):
        return False
    return hashlib.sha256(doc.read_bytes()).hexdigest() in RETIRED_SKILLS[name]


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
    path = path.resolve()
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


def managed_hook(hook, executable, arguments):
    """Match only a command installed by the blueprint, including its old home after migration."""
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    try:
        cmd = hook.get("command")
        if not isinstance(cmd, str):
            return False
        argv = shlex.split(cmd)
    except ValueError:
        return False
    return bool(argv and Path(argv[0]).parts[-3:] == (".local", "bin", executable)
                and argv[1:] == arguments)


def canonical_guard(entry, matcher, command):
    if not isinstance(entry, dict) or entry.get("matcher") != matcher:
        return False
    hooks = entry.get("hooks")
    return isinstance(hooks, list) and any(
        isinstance(h, dict) and h.get("type") == "command" and h.get("command") == command
        for h in hooks
    )


def session_hygiene_enabled():
    # deploy.sh exports config.env. Direct invocations should also honor a
    # literal machine preference without evaluating arbitrary shell code.
    value = os.environ.get("ENABLE_SESSION_HYGIENE")
    if value is None:
        config = Path(__file__).resolve().parents[1] / "config.env"
        if config.is_file():
            for line in config.read_text(encoding="utf-8").splitlines():
                key, _, raw = line.strip().removeprefix("export ").partition("=")
                if key == "ENABLE_SESSION_HYGIENE" and raw.strip().strip("\"'").lower() in {"true", "false"}:
                    value = raw.strip().strip("\"'")
    return (value or "true").lower() == "true"


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
    guard_cmd = shlex.quote(str(home / ".local/bin/agent-guard")) + " --mode hook"

    actions = []
    skipped = []

    def link_skill(target_dir: Path, name: str, source: Path):
        if name in RETIRED_SKILLS and (target_dir / name).exists():
            return
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
            if (item.is_symlink() and not item.exists()
                    and (item.parent / os.readlink(item)).parent.resolve() == agents_skills.resolve()):
                actions.append({"action": "prune_broken_symlink", "dest": str(item)})
                if apply:
                    try:
                        item.unlink()
                    except OSError:
                        pass

    def sync_skill_copy(target_dir: Path, name: str, source: Path):
        """Antigravity's pre-tool hook resolves realpath() against allowed workspaces, so symlinks to
        ~/.agents/skills are rejected. Materialize real copies inside Antigravity's own skill folders."""
        src = source.resolve()
        if not src.is_dir():
            return
        dest = target_dir / name
        if name in RETIRED_SKILLS and dest.exists():
            return
        ignore_fn = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".DS_Store")

        def tree_fingerprint(root: Path) -> str:
            h = hashlib.sha256()
            def unreadable(error):
                raise error
            for cur, dirs, files in os.walk(root, onerror=unreadable):
                ignored = ignore_fn(cur, dirs + files)
                dirs[:] = sorted(d for d in dirs if d not in ignored)
                for fn in sorted(dirs + [f for f in files if f not in ignored]):
                    fp = Path(cur) / fn
                    h.update(os.fsencode(fp.relative_to(root)) + b"\0")
                    if fp.is_symlink():
                        target = os.readlink(fp)
                        if os.path.isabs(target) or not fp.resolve(strict=True).is_relative_to(root):
                            raise ValueError(f"skill link leaves its copy: {fp}")
                        h.update(b"link:" + os.fsencode(target))
                    elif fp.is_file():
                        content = hashlib.sha256()
                        with fp.open("rb") as stream:
                            for chunk in iter(lambda: stream.read(65536), b""):
                                content.update(chunk)
                        h.update(b"file:" + content.digest())
                    elif fp.is_dir():
                        h.update(b"dir")
                    else:
                        raise ValueError(f"unsupported skill file: {fp}")
                    h.update(b"\0")
            return h.hexdigest()

        try:
            fingerprint = tree_fingerprint(src)
            needs_copy = dest.is_symlink() or not dest.is_dir() or tree_fingerprint(dest.resolve()) != fingerprint
        except (OSError, ValueError, RuntimeError) as exc:
            skipped.append({"file": str(dest), "reason": str(exc)})
            return
        if not needs_copy:
            return
        actions.append({"action": "copy_skill", "dest": str(dest), "source": str(src)})
        if apply:
            target_dir.mkdir(parents=True, exist_ok=True)
            tmp_dest = target_dir / f".{name}.sync-{os.getpid()}"
            backup_dest = target_dir / f".{name}.old-{os.getpid()}"
            if tmp_dest.exists() or tmp_dest.is_symlink():
                shutil.rmtree(tmp_dest) if tmp_dest.is_dir() and not tmp_dest.is_symlink() else tmp_dest.unlink()
            if backup_dest.exists() or backup_dest.is_symlink():
                shutil.rmtree(backup_dest) if backup_dest.is_dir() and not backup_dest.is_symlink() else backup_dest.unlink()
            shutil.copytree(src, tmp_dest, symlinks=True, ignore=ignore_fn)
            if dest.is_symlink() or dest.is_file():
                dest.unlink()
                os.replace(tmp_dest, dest)
            elif dest.is_dir():
                os.rename(dest, backup_dest)
                try:
                    os.rename(tmp_dest, dest)
                except Exception:
                    os.rename(backup_dest, dest)
                    raise
                shutil.rmtree(backup_dest, ignore_errors=True)
            else:
                os.rename(tmp_dest, dest)

    retired_split_skills = RETIRED_SKILLS
    trash_retire_root = home / ".Trash" / f"orca-skill-retirement-{os.getpid()}"

    def retire_legacy_skill(target_dir: Path, name: str):
        dest = target_dir / name
        if not dest.exists() and not dest.is_symlink():
            return
        if not shipped_legacy(dest, name):
            return
        actions.append({"action": "retire_merged_skill", "dest": str(dest), "replaced_by": "orca-workspace"})
        if not apply:
            return
        if dest.is_symlink() or dest.is_file():
            try:
                dest.unlink()
            except OSError:
                pass
        elif dest.is_dir():
            trash_retire_root.mkdir(parents=True, exist_ok=True)
            safe_dst = trash_retire_root / f"{target_dir.parent.name}-{target_dir.name}-{name}"
            idx = 1
            while safe_dst.exists():
                idx += 1
                safe_dst = trash_retire_root / f"{target_dir.parent.name}-{target_dir.name}-{name}-{idx}"
            try:
                shutil.move(str(dest), str(safe_dst))
            except OSError:
                pass

    has_unified = (agents_skills / "orca-workspace/SKILL.md").is_file()
    if has_unified:
        for legacy_name in retired_split_skills:
            retire_legacy_skill(agents_skills, legacy_name)

    shared_skills = [
        d.name for d in sorted(agents_skills.iterdir())
        if (d.is_dir() or d.is_symlink())
        and (d / "SKILL.md").is_file()
        and not d.name.startswith(".")
        and d.name not in {"synced", "computer-use"}
        and not (has_unified and shipped_legacy(d, d.name))
    ] if agents_skills.is_dir() else []

    # 1a. Antigravity skill directories (real copies so realpath() stays inside Antigravity workspaces)
    for agy_copy_dir in [
        orca_dir / "antigravity-chat/config/skills",
        orca_dir / "antigravity-chat/antigravity-cli/skills",
    ]:
        prune_broken_symlinks(agy_copy_dir)
        if has_unified:
            for legacy_name in retired_split_skills:
                retire_legacy_skill(agy_copy_dir, legacy_name)
        # Remove legacy duplicate computer-use alias if present
        legacy_cu = agy_copy_dir / "computer-use"
        if legacy_cu.is_symlink():
            actions.append({"action": "remove_legacy_alias", "dest": str(legacy_cu)})
            if apply:
                legacy_cu.unlink()
        for s in shared_skills:
            sync_skill_copy(agy_copy_dir, s, agents_skills / s)

    # 1b. Gemini CLI, Claude Code & Codex CLI skill directories (symlinks)
    for cli_skill_dir in [
        home / ".gemini/skills",
        home / ".gemini/config/skills",
        home / ".claude/skills",
        home / ".codex/skills",
    ]:
        prune_broken_symlinks(cli_skill_dir)
        if has_unified:
            for legacy_name in retired_split_skills:
                retire_legacy_skill(cli_skill_dir, legacy_name)
        for s in shared_skills:
            link_skill(cli_skill_dir, s, agents_skills / s)

    # Sync ~/.gemini/GEMINI.md -> antigravity-chat/GEMINI.md -> antigravity-acp/GEMINI.md
    base_gemini_md = home / ".gemini/GEMINI.md"
    chat_gemini_md = orca_dir / "antigravity-chat/GEMINI.md"
    acp_gemini_md = orca_dir / "antigravity-chat/antigravity-acp/GEMINI.md"
    if base_gemini_md.is_file() and chat_gemini_md.parent.is_dir():
        if not chat_gemini_md.is_file() or chat_gemini_md.read_text(encoding="utf-8") != base_gemini_md.read_text(encoding="utf-8"):
            actions.append({"action": "sync_antigravity_chat_gemini_md", "dest": str(chat_gemini_md)})
            if apply:
                shutil.copy2(base_gemini_md, chat_gemini_md)
    effective_chat_md = base_gemini_md if (base_gemini_md.is_file() and chat_gemini_md.parent.is_dir()) else chat_gemini_md
    if effective_chat_md.is_file() and acp_gemini_md.parent.is_dir():
        if not acp_gemini_md.is_file() or acp_gemini_md.read_text(encoding="utf-8") != effective_chat_md.read_text(encoding="utf-8"):
            actions.append({"action": "sync_antigravity_acp_gemini_md", "dest": str(acp_gemini_md)})
            if apply:
                shutil.copy2(effective_chat_md, acp_gemini_md)

    # Sync additional Antigravity & Codex runtime global rule files when directories exist
    for extra_gemini in [home / ".gemini/antigravity/GEMINI.md", home / ".gemini/antigravity-cli/GEMINI.md"]:
        if base_gemini_md.is_file() and extra_gemini.parent.is_dir():
            if not extra_gemini.is_file() or extra_gemini.read_text(encoding="utf-8") != base_gemini_md.read_text(encoding="utf-8"):
                actions.append({"action": "sync_gemini_md", "dest": str(extra_gemini)})
                if apply:
                    shutil.copy2(base_gemini_md, extra_gemini)
    rt_agents_md = orca_dir / "codex-runtime-home/home/AGENTS.md"
    if (home / ".codex/AGENTS.md").is_file() and rt_agents_md.parent.is_dir():
        if not rt_agents_md.is_file() or rt_agents_md.read_text(encoding="utf-8") != (home / ".codex/AGENTS.md").read_text(encoding="utf-8"):
            actions.append({"action": "sync_codex_runtime_agents_md", "dest": str(rt_agents_md)})
            if apply:
                shutil.copy2(home / ".codex/AGENTS.md", rt_agents_md)

    # 2. Claude profiles (MCP + PreToolUse guardrail + full SHA-256 CLAUDE.md parity)
    for prof_home in sorted((orca_dir / "claude-profiles").glob("*/home")):
        if (home / ".claude/CLAUDE.md").is_file():
            target_cm = prof_home / "CLAUDE.md"
            src_cm_txt = (home / ".claude/CLAUDE.md").read_text(encoding="utf-8")
            current_rules = target_cm.read_text(encoding="utf-8") if target_cm.is_file() else ""
            if current_rules != src_cm_txt and current_rules.strip() != "@~/.claude/CLAUDE.md":
                actions.append({"action": "sync_claude_md", "dest": str(target_cm)})
                if apply:
                    shutil.copy2(home / ".claude/CLAUDE.md", target_cm)
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
        if has_unified:
            for legacy_name in retired_split_skills:
                retire_legacy_skill(acct_home / "skills", legacy_name)
        if codex_skills_dir.is_dir():
            for item in sorted(codex_skills_dir.iterdir()):
                if not item.name.startswith(".") and item.name not in {"synced", "computer-use"}:
                    link_skill(acct_home / "skills", item.name, item.resolve())

    # 4. PreToolUse Guardrail wiring (also heals stale cross-Mac /Users/<old_user> paths)
    def wire_guard(
        path: Path,
        agent: str,
        new_entry: dict,
        container_keys: tuple,
        *,
        create_if_missing: bool = False,
    ) -> None:
        """Add the guard to the PreToolUse list found under `container_keys`, or repair its path/matcher."""
        if not path.is_file():
            if not create_if_missing or not path.parent.is_dir():
                return
            data = {}
        else:
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
        # Keep custom hooks in their original matcher group. Only move our own command.
        updated = []
        found = False
        for entry in pre:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                updated.append(entry)
                continue
            others = []
            for hook in entry["hooks"]:
                if managed_hook(hook, "agent-guard", ["--mode", "hook"]):
                    if not found and entry.get("matcher") == new_entry.get("matcher"):
                        # Preserve per-hook options and the group's position.
                        others.append({**hook, "command": guard_cmd})
                        found = True
                else:
                    others.append(hook)
            if others or not entry["hooks"]:
                updated.append({**entry, "hooks": others})
        if not found:
            updated.append(new_entry)
        changed = pre != updated
        if changed:
            node["PreToolUse"] = updated
            actions.append({"action": f"update_{agent}_guard", "file": str(path)})
        if changed and apply:
            write_json(path, data)

    if enable_guard:
        guard_hook = {"type": "command", "command": guard_cmd, "timeout": 5}
        compact_cmd = shlex.quote(str(home / ".local/bin/agent-guard")) + " --mode compact-anchor"
        compact_hook = {"type": "command", "command": compact_cmd, "timeout": 5}
        for sf in [home / ".claude/settings.json"] + sorted((orca_dir / "claude-profiles").glob("*/home/settings.json")):
            if sf.is_file():
                wire_guard(
                    sf,
                    "claude",
                    {"matcher": "Bash|Read|Write|Edit|MultiEdit", "hooks": [dict(guard_hook)]},
                    ("hooks",),
                )
                # Wire SessionStart(compact) physical ground-truth anchor (0 token tax on normal turns)
                cdata = load_json(sf)
                if cdata is not None and isinstance(cdata.get("hooks", {}), dict):
                    chooks = cdata.setdefault("hooks", {})
                    ss_groups = chooks.get("SessionStart", [])
                    if isinstance(ss_groups, list):
                        ss_updated = []
                        ss_found = False
                        for grp in ss_groups:
                            if not isinstance(grp, dict) or not isinstance(grp.get("hooks"), list):
                                ss_updated.append(grp)
                                continue
                            others = []
                            for hk in grp["hooks"]:
                                if managed_hook(hk, "agent-guard", ["--mode", "compact-anchor"]):
                                    if not ss_found and grp.get("matcher") == "compact":
                                        others.append({**hk, "command": compact_cmd, "timeout": 5})
                                        ss_found = True
                                else:
                                    others.append(hk)
                            if others or not grp["hooks"]:
                                ss_updated.append({**grp, "hooks": others})
                        if not ss_found:
                            ss_updated.append({"matcher": "compact", "hooks": [dict(compact_hook)]})
                        if ss_updated != ss_groups:
                            chooks["SessionStart"] = ss_updated
                            actions.append({"action": "update_claude_compact_anchor", "file": str(sf)})
                            if apply:
                                write_json(sf, cdata)

        for hf in [home / ".codex/hooks.json"] + sorted((orca_dir / "codex-accounts").glob("*/home/hooks.json")):
            if hf.is_file():
                wire_guard(hf, "codex", {"hooks": [dict(guard_hook)]}, ("hooks",))

        # Antigravity groups hooks by owner; include view_file / client_view_file so binary/oversized reads cannot
        # trigger mid-turn CORTEX_STEP_STATUS_CLEARED and strip parallel Gemini thought_signature blobs.
        # Orca creates `antigravity-chat/config/skills` without `hooks.json`, so create `hooks.json` when `config/` exists.
        agy_matcher = "run_command|write_to_file|replace_file_content|view_file|client_view_file"
        for gf in [home / ".gemini/config/hooks.json", orca_dir / "antigravity-chat/config/hooks.json"]:
            if gf.is_file() or gf.parent.is_dir():
                wire_guard(
                    gf,
                    "antigravity",
                    {"matcher": agy_matcher, "hooks": [dict(guard_hook)]},
                    ("agent-guard",),
                    create_if_missing=True,
                )

    # When unset, keep the historical default. Explicitly disabling the feature
    # removes previously installed hooks as well as suppressing new ones.
    enable_session_gc = session_hygiene_enabled()
    session_args = ["gc", "--mode", "session-end", "--apply", ">/dev/null", "2>&1", "||", "true"]
    session_cmd = shlex.quote(str(home / ".local/bin/agent-governor")) + " " + " ".join(session_args)
    for sf in [home / ".claude/settings.json"] + sorted((orca_dir / "claude-profiles").glob("*/home/settings.json")):
        data = load_json(sf)
        if data is None or not isinstance(data.get("hooks", {}), dict):
            continue
        hooks = data.setdefault("hooks", {})
        groups = hooks.get("SessionEnd", [])
        if not isinstance(groups, list):
            continue
        updated = []
        installed = False
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                updated.append(group)
                continue
            keep = []
            for hook in group["hooks"]:
                if not managed_hook(hook, "agent-governor", session_args):
                    keep.append(hook)
                elif enable_session_gc and not installed and not group.get("matcher"):
                    keep.append({**hook, "command": session_cmd, "timeout": 15})
                    installed = True
            if keep or not group["hooks"]:
                updated.append({**group, "hooks": keep})
        if enable_session_gc and not installed:
            updated.append({"hooks": [{"type": "command", "command": session_cmd, "timeout": 15}]})
        if updated != groups:
            hooks["SessionEnd"] = updated
            actions.append({"action": "update_claude_session_hygiene", "file": str(sf)})
            if apply:
                write_json(sf, data)

    report = {"mode": "apply" if apply else "dry-run", "PlannedOrAppliedCount": len(actions), "actions": actions}
    if skipped:
        report["skipped"] = skipped
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
