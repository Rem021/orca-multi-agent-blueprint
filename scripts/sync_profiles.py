#!/usr/bin/env python3
"""Multi-Agent & Multi-Account Profile Synchronizer for Orca + CLI Harnesses.

Keeps skills, global rules, the Xcode MCP server and the agent-guard hooks aligned across every
installed coding agent (Claude Code, OpenAI Codex, Gemini CLI, Google Antigravity) and every Orca
account profile, so a failover between accounts never changes how an agent behaves.

Only agents that are installed (or listed in WORKER_AGENTS) are touched; nothing is created for an
agent that is not on this Mac. Dry-run by default; `--apply` writes. Every JSON/TOML file is replaced
atomically and keeps its permissions; files that cannot be parsed are reported and left alone.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile

KNOWN_AGENTS = ("claude", "codex", "gemini", "antigravity")

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

CLAUDE_GUARD_MATCHER = "Bash|Read|Write|Edit|MultiEdit"
ANTIGRAVITY_GUARD_MATCHER = "run_command|write_to_file|replace_file_content|view_file|client_view_file"
GEMINI_GUARD_MATCHER = "run_shell_command|write_file|replace|read_file"
# Claude Code, Codex and Antigravity hook timeouts are seconds; Gemini CLI uses milliseconds.
GUARD_TIMEOUT_SECONDS = 5
GEMINI_GUARD_TIMEOUT_MS = 5000


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


def atomic_write_text(path: Path, text: str, mode=None) -> None:
    """Replace `path` atomically, keeping its permissions (agents read these files while running)."""
    path = path.resolve()
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = 0o600
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path: Path, data, mode=None) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode)


# ---------------------------------------------------------------------------------------------------
# TOML (Python 3.9 has no tomllib: a line scanner that understands every way a table can be defined)

_TOML_KEY_PART = r'(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|\'[^\']*\')'
_TOML_DOTTED = _TOML_KEY_PART + r"(?:\s*\.\s*" + _TOML_KEY_PART + r")*"


def _toml_key_parts(dotted):
    return tuple(p[1:-1] if p[:1] in "\"'" else p for p in re.findall(_TOML_KEY_PART, dotted))


def _tomllib():
    try:
        import tomllib  # Python 3.11+
        return tomllib
    except ImportError:
        return None


def toml_defines(text, path):
    """True when TOML `text` defines `path` (e.g. ("mcp_servers", "xcode")) or anything below it, in
    any syntax: `[a.b]`, `[a."b"]`, `[[a.b]]`, `[a.b.c]`, `a.b = ...`, `b = ...` under `[a]`, or an
    inline table `a = { b = ... }`. Comments and multi-line strings are skipped."""
    path = tuple(path)
    lib = _tomllib()
    if lib is not None:
        try:
            node = lib.loads(text)
        except lib.TOMLDecodeError:
            node = None
        if node is not None:
            for key in path:
                if not isinstance(node, dict) or key not in node:
                    return False
                node = node[key]
            return True
    table = ()
    multiline = None
    for raw in text.splitlines():
        line = raw.strip()
        if multiline:
            if multiline in line:
                multiline = None
            continue
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[\[?\s*(" + _TOML_DOTTED + r")\s*\]\]?", line)
        if m:
            table = _toml_key_parts(m.group(1))
            if table[:len(path)] == path:
                return True
            continue
        m = re.match(r"^(" + _TOML_DOTTED + r")\s*=\s*(.*)$", line)
        if not m:
            continue
        key = table + _toml_key_parts(m.group(1))
        value = m.group(2)
        if key[:len(path)] == path:
            return True
        if len(key) < len(path) and path[:len(key)] == key and value.startswith("{"):
            nxt = re.escape(path[len(key)])
            if re.search(r"[{,]\s*(?:%s|\"%s\"|'%s')\s*[.=]" % (nxt, nxt, nxt), value):
                return True
        for delim in ('"""', "'''"):
            if value.count(delim) % 2 == 1:
                multiline = delim
                break
    return False


def toml_is_valid(text):
    """None when it cannot be checked (no tomllib on this Python), else whether `text` parses."""
    lib = _tomllib()
    if lib is None:
        return None
    try:
        lib.loads(text)
        return True
    except lib.TOMLDecodeError:
        return False


# ---------------------------------------------------------------------------------------------------
# Hooks

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


def reconcile_groups(groups, executable, arguments, matcher, hook, overrides, enabled):
    """Matcher groups with exactly one managed `executable arguments` hook (enabled) or none.

    The managed hook stays in the first group whose matcher equals `matcher` (keeping its per-hook
    options, `overrides` applied); copies elsewhere are dropped and groups they leave empty go with
    them. Custom hooks never move."""
    updated = []
    found = False
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            updated.append(group)
            continue
        keep = []
        for h in group["hooks"]:
            if not managed_hook(h, executable, arguments):
                keep.append(h)
            elif enabled and not found and (group.get("matcher") or None) == (matcher or None):
                keep.append({**h, **overrides})
                found = True
        if keep or not group["hooks"]:
            updated.append({**group, "hooks": keep})
    if enabled and not found:
        new_group = {"matcher": matcher, "hooks": [dict(hook)]} if matcher else {"hooks": [dict(hook)]}
        updated.append(new_group)
    return updated


# ---------------------------------------------------------------------------------------------------
# Configuration and agent detection

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.env"


def config_value(key):
    """`key` from the environment (deploy.sh exports config.env), else a literal (or `${KEY:-default}`)
    value in the checkout's config.env. Never evaluates shell code."""
    value = os.environ.get(key)
    if value is not None:
        return value
    if REPO_CONFIG.is_file():
        try:
            lines = REPO_CONFIG.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in lines:
            name, _, raw = line.strip().removeprefix("export ").partition("=")
            if name.strip() != key:
                continue
            raw = raw.strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1]
            m = re.fullmatch(r"\$\{(\w+):?-(.*)\}", raw)
            if m:
                raw = os.environ.get(m.group(1)) or m.group(2)
            if "$" in raw or "`" in raw:
                return None
            return raw
    return None


def session_hygiene_enabled():
    return (config_value("ENABLE_SESSION_HYGIENE") or "true").strip().lower() == "true"


def detect_agents(home: Path):
    """Agents present on this Mac: a CLI on PATH, or the configuration it (or Orca) created."""
    search = os.pathsep.join([str(home / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "")])
    orca_dir = home / "Library/Application Support/orca"

    def has(*names):
        return any(shutil.which(n, path=search) for n in names)

    found = set()
    if has("claude") or (home / ".claude").is_dir() or any((orca_dir / "claude-profiles").glob("*/home")):
        found.add("claude")
    if has("codex") or (home / ".codex").is_dir() or any((orca_dir / "codex-accounts").glob("*/home")):
        found.add("codex")
    if has("gemini") or (home / ".gemini/settings.json").is_file():
        found.add("gemini")
    if (has("agy", "antigravity") or (orca_dir / "antigravity-chat").is_dir()
            or (home / ".gemini/antigravity").is_dir() or (home / ".gemini/config").is_dir()):
        found.add("antigravity")
    return found


def selected_agents(home=None):
    """WORKER_AGENTS when set (comma or space separated), else every detected agent.

    `gemini` in WORKER_AGENTS also covers Antigravity when it is installed: both are Gemini front-ends
    sharing ~/.gemini."""
    home = Path(home) if home else Path.home()
    detected = detect_agents(home)
    raw = (config_value("WORKER_AGENTS") or "").strip().lower()
    if not raw or raw == "auto":
        return detected
    chosen = {a for a in re.split(r"[,\s]+", raw) if a in KNOWN_AGENTS}
    if "gemini" in chosen and "antigravity" in detected:
        chosen.add("antigravity")
    return chosen


# ---------------------------------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    p.add_argument("--dry-run", action="store_true", help="Preview changes without modifying files")
    args = p.parse_args()
    apply = args.apply and not args.dry_run

    home = Path.home()
    orca_dir = home / "Library/Application Support/orca"
    agents_skills = home / ".agents/skills"
    enable_xcode_mcp = (config_value("ENABLE_XCODE_MCP") or "true").strip().lower() == "true"
    enable_guard = (config_value("ENABLE_PRETOOLUSE_GUARDRAILS") or "true").strip().lower() == "true"
    try:
        startup_timeout = int(config_value("XCODE_MCP_STARTUP_TIMEOUT_SEC") or "120")
        tool_timeout = int(config_value("XCODE_MCP_TOOL_TIMEOUT_SEC") or "600")
    except ValueError:
        startup_timeout, tool_timeout = 120, 600
    guard_cmd = shlex.quote(str(home / ".local/bin/agent-guard")) + " --mode hook"
    agents = selected_agents(home)

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
            for stale in (tmp_dest, backup_dest):
                if stale.is_dir() and not stale.is_symlink():
                    shutil.rmtree(stale)
                elif stale.exists() or stale.is_symlink():
                    stale.unlink()
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
        for legacy_name in RETIRED_SKILLS:
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
    if "antigravity" in agents:
        for agy_copy_dir in [
            orca_dir / "antigravity-chat/config/skills",
            orca_dir / "antigravity-chat/antigravity-cli/skills",
        ]:
            prune_broken_symlinks(agy_copy_dir)
            if has_unified:
                for legacy_name in RETIRED_SKILLS:
                    retire_legacy_skill(agy_copy_dir, legacy_name)
            # Remove legacy duplicate computer-use alias if present
            legacy_cu = agy_copy_dir / "computer-use"
            if legacy_cu.is_symlink():
                actions.append({"action": "remove_legacy_alias", "dest": str(legacy_cu)})
                if apply:
                    legacy_cu.unlink()
            for s in shared_skills:
                sync_skill_copy(agy_copy_dir, s, agents_skills / s)

    # 1b. Gemini CLI, Antigravity, Claude Code & Codex CLI skill directories (symlinks)
    for agent, cli_skill_dir in [
        ("gemini", home / ".gemini/skills"),
        ("antigravity", home / ".gemini/config/skills"),
        ("claude", home / ".claude/skills"),
        ("codex", home / ".codex/skills"),
    ]:
        if agent not in agents:
            continue
        prune_broken_symlinks(cli_skill_dir)
        if has_unified:
            for legacy_name in RETIRED_SKILLS:
                retire_legacy_skill(cli_skill_dir, legacy_name)
        for s in shared_skills:
            link_skill(cli_skill_dir, s, agents_skills / s)

    def sync_copy(src: Path, dest: Path, action: str):
        if not dest.is_file() or dest.read_text(encoding="utf-8") != src.read_text(encoding="utf-8"):
            actions.append({"action": action, "dest": str(dest)})
            if apply:
                shutil.copy2(src, dest)

    # Global rule files of runtimes that keep their own copy (only where the runtime's directory exists)
    base_gemini_md = home / ".gemini/GEMINI.md"
    chat_gemini_md = orca_dir / "antigravity-chat/GEMINI.md"
    acp_gemini_md = orca_dir / "antigravity-chat/antigravity-acp/GEMINI.md"
    if base_gemini_md.is_file() and chat_gemini_md.parent.is_dir():
        sync_copy(base_gemini_md, chat_gemini_md, "sync_antigravity_chat_gemini_md")
    effective_chat_md = base_gemini_md if (base_gemini_md.is_file() and chat_gemini_md.parent.is_dir()) else chat_gemini_md
    if effective_chat_md.is_file() and acp_gemini_md.parent.is_dir():
        sync_copy(effective_chat_md, acp_gemini_md, "sync_antigravity_acp_gemini_md")
    for extra_gemini in [home / ".gemini/antigravity/GEMINI.md", home / ".gemini/antigravity-cli/GEMINI.md"]:
        if base_gemini_md.is_file() and extra_gemini.parent.is_dir():
            sync_copy(base_gemini_md, extra_gemini, "sync_gemini_md")
    rt_agents_md = orca_dir / "codex-runtime-home/home/AGENTS.md"
    if (home / ".codex/AGENTS.md").is_file() and rt_agents_md.parent.is_dir():
        sync_copy(home / ".codex/AGENTS.md", rt_agents_md, "sync_codex_runtime_agents_md")

    # 2. Claude profiles (MCP + full CLAUDE.md parity unless the profile imports the shared file)
    if "claude" in agents:
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
                            write_json(cj, data, 0o600)

    # 3. Codex accounts (AGENTS.md + hooks.json + xcode MCP + skills)
    codex_skills_dir = home / ".codex/skills"
    xcode_toml = (
        f"\n[mcp_servers.xcode]\n"
        f"startup_timeout_sec = {startup_timeout}\n"
        f"tool_timeout_sec = {tool_timeout}\n"
        f'command = "/usr/bin/xcrun"\n'
        f'args = ["mcpbridge"]\n'
    )
    for acct_home in (sorted((orca_dir / "codex-accounts").glob("*/home")) if "codex" in agents else []):
        if (home / ".codex/AGENTS.md").is_file():
            link_skill(acct_home, "AGENTS.md", home / ".codex/AGENTS.md")
        if (home / ".codex/hooks.json").is_file() and not (acct_home / "hooks.json").is_file():
            actions.append({"action": "copy_hooks_json", "dest": str(acct_home / "hooks.json")})
            if apply:
                shutil.copy2(home / ".codex/hooks.json", acct_home / "hooks.json")
        cfg = acct_home / "config.toml"
        if enable_xcode_mcp and cfg.is_file():
            try:
                text = cfg.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                skipped.append({"file": str(cfg), "reason": str(exc)})
                text = None
            if text is not None and toml_is_valid(text) is False:
                skipped.append({"file": str(cfg), "reason": "invalid TOML"})
            elif text is not None and not toml_defines(text, ("mcp_servers", "xcode")):
                new_text = text.rstrip() + "\n" + xcode_toml
                if toml_is_valid(new_text) is False:
                    skipped.append({"file": str(cfg), "reason": "adding [mcp_servers.xcode] would make it invalid TOML"})
                else:
                    actions.append({"action": "codex_xcode_mcp", "file": str(cfg)})
                    if apply:
                        atomic_write_text(cfg, new_text, 0o600)
        prune_broken_symlinks(acct_home / "skills")
        if has_unified:
            for legacy_name in RETIRED_SKILLS:
                retire_legacy_skill(acct_home / "skills", legacy_name)
        if codex_skills_dir.is_dir():
            for item in sorted(codex_skills_dir.iterdir()):
                if not item.name.startswith(".") and item.name not in {"synced", "computer-use"}:
                    link_skill(acct_home / "skills", item.name, item.resolve())

    # 4. Hooks: agent-guard (PreToolUse / BeforeTool), Claude compact anchor and session hygiene.
    #    Turning a feature off removes the hooks it installed; custom hooks are never touched.
    def update_hook_file(path: Path, plan, *, create_if_missing=False):
        """plan: [(action, container_keys, event, reconcile_args, enabled)] applied to one JSON file."""
        if path.is_file():
            data = load_json(path)
            if data is None:
                skipped.append({"file": str(path), "reason": "invalid JSON"})
                return
        elif create_if_missing and path.parent.is_dir() and any(step[4] for step in plan):
            data = {}
        else:
            return
        changed = False
        for action, keys, event, spec, enabled in plan:
            node = data
            for key in keys:
                child = node.get(key)
                if child is None:
                    if not enabled:
                        node = None
                        break
                    child = node[key] = {}
                if not isinstance(child, dict):
                    skipped.append({"file": str(path), "reason": f"unexpected structure at {key}"})
                    node = None
                    break
                node = child
            if node is None:
                continue
            groups = node.get(event, [])
            if not isinstance(groups, list):
                skipped.append({"file": str(path), "reason": f"unexpected structure at {event}"})
                continue
            updated = reconcile_groups(groups, *spec, enabled=enabled)
            if updated != groups:
                if updated:
                    node[event] = updated
                else:
                    node.pop(event, None)
                actions.append({"action": action, "file": str(path)})
                changed = True
        if changed and apply:
            write_json(path, data)

    def guard_on(agent):
        return enable_guard and agent in agents

    def guard_spec(matcher, timeout):
        hook = {"type": "command", "command": guard_cmd, "timeout": timeout}
        return ("agent-guard", ["--mode", "hook"], matcher, hook, {"command": guard_cmd})

    compact_cmd = shlex.quote(str(home / ".local/bin/agent-guard")) + " --mode compact-anchor"
    compact_spec = ("agent-guard", ["--mode", "compact-anchor"], "compact",
                    {"type": "command", "command": compact_cmd, "timeout": GUARD_TIMEOUT_SECONDS},
                    {"command": compact_cmd, "timeout": GUARD_TIMEOUT_SECONDS})
    session_args = ["gc", "--mode", "session-end", "--apply", ">/dev/null", "2>&1", "||", "true"]
    session_cmd = shlex.quote(str(home / ".local/bin/agent-governor")) + " " + " ".join(session_args)
    session_spec = ("agent-governor", session_args, None,
                    {"type": "command", "command": session_cmd, "timeout": 15},
                    {"command": session_cmd, "timeout": 15})
    enable_session_gc = session_hygiene_enabled()

    claude_selected = "claude" in agents
    for sf in [home / ".claude/settings.json"] + sorted((orca_dir / "claude-profiles").glob("*/home/settings.json")):
        if not claude_selected and enable_guard:
            break  # an agent left out of WORKER_AGENTS is only ever cleaned up, never configured
        on = guard_on("claude")
        plan = [
            ("update_claude_guard", ("hooks",), "PreToolUse", guard_spec(CLAUDE_GUARD_MATCHER, GUARD_TIMEOUT_SECONDS), on),
            # SessionStart(compact) physical ground-truth anchor (0 token tax on normal turns)
            ("update_claude_compact_anchor", ("hooks",), "SessionStart", compact_spec, on),
        ]
        if claude_selected:
            plan.append(("update_claude_session_hygiene", ("hooks",), "SessionEnd", session_spec, enable_session_gc))
        update_hook_file(sf, plan)

    def guard_files(agent, files):
        """Files whose guard hook is reconciled: all of them to remove it, a selected agent's to add it."""
        return files if (not enable_guard or agent in agents) else []

    for hf in guard_files("codex", [home / ".codex/hooks.json"] + sorted((orca_dir / "codex-accounts").glob("*/home/hooks.json"))):
        # Codex runs a new or changed hook only after the user approves it in Codex's /hooks screen.
        update_hook_file(hf, [("update_codex_guard", ("hooks",), "PreToolUse",
                               guard_spec(None, GUARD_TIMEOUT_SECONDS), guard_on("codex"))])

    # Antigravity groups hooks by owner; include view_file / client_view_file so binary/oversized reads cannot
    # trigger mid-turn CORTEX_STEP_STATUS_CLEARED and strip parallel Gemini thought_signature blobs.
    # Orca creates `antigravity-chat/config/skills` without `hooks.json`, so create `hooks.json` when `config/` exists.
    for gf in guard_files("antigravity", [home / ".gemini/config/hooks.json", orca_dir / "antigravity-chat/config/hooks.json"]):
        update_hook_file(gf, [("update_antigravity_guard", ("agent-guard",), "PreToolUse",
                               guard_spec(ANTIGRAVITY_GUARD_MATCHER, GUARD_TIMEOUT_SECONDS), guard_on("antigravity"))],
                         create_if_missing=True)

    # Gemini CLI: ~/.gemini/settings.json hooks.BeforeTool (regex matcher, timeout in milliseconds)
    for gs in guard_files("gemini", [home / ".gemini/settings.json"]):
        update_hook_file(gs, [("update_gemini_guard", ("hooks",), "BeforeTool",
                               guard_spec(GEMINI_GUARD_MATCHER, GEMINI_GUARD_TIMEOUT_MS), guard_on("gemini"))],
                         create_if_missing=True)

    report = {"mode": "apply" if apply else "dry-run", "agents": sorted(agents),
              "PlannedOrAppliedCount": len(actions), "actions": actions}
    if skipped:
        report["skipped"] = skipped
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
