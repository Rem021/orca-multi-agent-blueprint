#!/usr/bin/env python3
"""Smart Auto-Discovery & Non-Destructive Adoption Engine.

Works on both:
1. Brand-new Macs (auto-detects storage volume, iOS simulators, installed agents,
   generates config.env, renders global AGENTS.md/CLAUDE.md/GEMINI.md, and initializes taxonomy).
2. Existing Macs with many projects & agents (discovers all repos registered in Orca
   or under AI/Projects, creates missing docs/reports/outputs/work folders, adds /work/
   to .git/info/exclude, and aligns all agent profiles).
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys


def run_cmd(argv: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=Path.home())
        return r.returncode, r.stdout.strip()
    except Exception:
        return 1, ""


UUID_SENTINELS = ("", "AUTO", "LOCAL", "INTERNAL")
SYSTEM_VOLUME_NAMES = {"Macintosh HD", "Macintosh HD - Data", "Preboot", "Recovery", "VM", "Update"}


def usable_external_volume(info: dict) -> bool:
    """False for volumes a workspace must never land on by accident: the internal disk, mounted disk
    images (installers), network shares, read-only media and Time Machine destinations."""
    if info.get("Internal") is True:
        return False
    if info.get("BusProtocol") == "Disk Image":
        return False
    if info.get("WritableVolume") is False or info.get("Writable") is False:
        return False
    if info.get("FilesystemType") in ("smbfs", "afpfs", "nfs", "webdav"):
        return False
    return True


def detect_volume(
    preferred_path: str = "",
    preferred_uuid: str = "",
    storage_mode: str = "auto",
    internal_default: str = "",
) -> tuple[Path, str, bool, str]:
    """Return (volume_or_workspace_root, volume_uuid, is_external_volume, resolved_storage_mode)."""
    mode = (storage_mode or "auto").strip().lower()
    fallback = Path(internal_default).expanduser() if internal_default else (Path.home() / "AI-Workspace")

    if mode == "internal":
        if preferred_path and not str(preferred_path).startswith("/Volumes/"):
            return Path(preferred_path).expanduser(), "LOCAL", False, "internal"
        return fallback, "LOCAL", False, "internal"

    if preferred_path:
        p = Path(preferred_path).expanduser()
        if str(p).startswith("/Volumes/") and p.is_dir():
            rc, out = run_cmd(["/usr/sbin/diskutil", "info", "-plist", str(p)])
            if rc == 0 and out:
                try:
                    uuid = plistlib.loads(out.encode("utf-8")).get("VolumeUUID", "")
                    return p, (uuid if preferred_uuid in UUID_SENTINELS else preferred_uuid), True, "external"
                except Exception:
                    pass
            return p, preferred_uuid, True, "external"
        if not str(p).startswith("/Volumes/") and mode != "external":
            return p, "LOCAL", False, "internal"

    # Auto-scan /Volumes/*. A volume that already carries an AI/ layout is adopted in every mode; a
    # blank external drive is only claimed when the caller asked for external storage explicitly.
    volumes_root = Path("/Volumes")
    if volumes_root.is_dir():
        candidates = []
        for v in sorted(volumes_root.iterdir()):
            if not v.is_dir() or v.is_symlink() or v.name in SYSTEM_VOLUME_NAMES or v.name.startswith("com.apple."):
                continue
            if not os.path.ismount(v) or (v / "Backups.backupdb").exists():
                continue
            rc, out = run_cmd(["/usr/sbin/diskutil", "info", "-plist", str(v)])
            uuid = ""
            if rc == 0 and out:
                try:
                    info = plistlib.loads(out.encode("utf-8"))
                    if not usable_external_volume(info):
                        continue
                    uuid = info.get("VolumeUUID", "")
                except Exception:
                    pass
            has_ai = (v / "AI").is_dir()
            if not has_ai and (mode != "external" or not os.access(v, os.W_OK)):
                continue
            candidates.append((1 if has_ai else 0, v, uuid))
        if candidates:
            candidates.sort(key=lambda x: (-x[0], x[1].name))
            _, chosen_v, chosen_uuid = candidates[0]
            resolved_uuid = chosen_uuid if preferred_uuid in UUID_SENTINELS else preferred_uuid
            return chosen_v, resolved_uuid, True, "external"

    # Fallback for single-disk MacBook without external SSD
    return fallback, "LOCAL", False, "internal"


def detect_simulators(pref_primary: str = "", pref_secondary: str = "") -> tuple[tuple[str, str], tuple[str, str]]:
    """Auto-detect available iPhone simulators via xcrun simctl."""
    rc, out = run_cmd(["/usr/bin/xcrun", "simctl", "list", "devices", "available", "-j"], timeout=20)
    devices = []
    if rc == 0 and out:
        try:
            data = json.loads(out)
            for runtime, dev_list in (data.get("devices") or {}).items():
                if "iOS" not in runtime:
                    continue
                os_ver = runtime.split("iOS-")[-1].replace("-", ".")
                os_key = tuple(int(x) for x in re.findall(r"\d+", os_ver))
                for d in dev_list:
                    if d.get("isAvailable") and "iPhone" in d.get("name", ""):
                        score = (
                            100 if "Pro Max" in d["name"] else (80 if "Pro" in d["name"] else 50),
                            os_key,
                            1 if d.get("state") == "Booted" else 0,
                        )
                        devices.append((score, d["udid"], f"{d['name']} (iOS {os_ver})"))
        except Exception:
            pass

    by_udid = {u: label for _, u, label in devices}
    if pref_primary and pref_primary in by_udid:
        primary = (pref_primary, by_udid[pref_primary])
    elif devices:
        devices.sort(reverse=True)
        primary = (devices[0][1], devices[0][2])
    else:
        # No simulator runtime installed (or Xcode missing): record nothing rather than a fake UDID
        return ("", ""), ("", "")

    if pref_secondary and pref_secondary in by_udid and pref_secondary != primary[0]:
        secondary = (pref_secondary, by_udid[pref_secondary])
    else:
        rem = [x for x in devices if x[1] != primary[0]]
        secondary = (rem[0][1], rem[0][2]) if rem else ("", "")

    return primary, secondary


def detect_installed_agents() -> list[str]:
    extra_path = f"{Path.home() / '.local/bin'}:/usr/local/bin:/opt/homebrew/bin:{os.environ.get('PATH', '')}"
    found = []
    checks = [
        ("claude", ["claude"]),
        ("codex", ["codex"]),
        ("gemini", ["gemini"]),
        ("antigravity", ["agy", "antigravity"]),
        ("opencode", ["opencode"]),
        ("cursor", ["cursor"]),
        ("aider", ["aider"]),
    ]
    for agent_name, bins in checks:
        if any(shutil.which(b, path=extra_path) for b in bins):
            found.append(agent_name)
    # Also check Orca antigravity-chat directory
    if "antigravity" not in found and (Path.home() / "Library/Application Support/orca/antigravity-chat").is_dir():
        found.append("antigravity")
    return found


def discover_and_adopt_projects(volume_root: Path, apply: bool) -> dict:
    projects_dir = volume_root / "AI/Projects"
    orca_bin = os.environ.get("ORCA_CLI_COMMAND", "/Applications/Orca.app/Contents/Resources/bin/orca")
    by_path: dict[Path, str] = {}

    # 1. Discover from AI/Projects/*
    if projects_dir.is_dir():
        for p in sorted(projects_dir.iterdir()):
            if not p.is_dir() or p.is_symlink() or p.name.startswith("."):
                continue
            by_path[p.resolve()] = p.name

    # 2. Discover from Orca repo list (if running)
    if Path(orca_bin).is_file():
        rc, out = run_cmd([orca_bin, "repo", "list", "--json"], timeout=15)
        if rc == 0 and out:
            try:
                repos = (json.loads(out).get("result") or {}).get("repos") or []
                for r in repos:
                    rpath = Path(r.get("path", ""))
                    if rpath.is_dir() and r.get("displayName") and "/Worktrees/" not in str(rpath):
                        by_path.setdefault(rpath.resolve(), r["displayName"])
            except Exception:
                pass

    adopted_details = []
    registry_entries = []
    used_keys: set[str] = set()

    for path, name in sorted(by_path.items(), key=lambda kv: (kv[1].lower(), str(kv[0]))):
        # Only initialize 4-zone folders on direct top-level workspace projects (`AI/Projects/<Project>`),
        # never inside nested standalone Git sub-repos under `<Project>/outputs/*` or `<Project>/work/*`.
        in_ai_projects = path.resolve().parent == projects_dir.resolve()
        rc_git, git_top = run_cmd(["git", "-c", "core.fsmonitor=false", "-C", str(path), "rev-parse", "--show-toplevel"])
        is_git = rc_git == 0 and bool(git_top)
        remote_url = ""
        repo_slug = ""
        if is_git:
            _, remote_url = run_cmd(["git", "-c", "core.fsmonitor=false", "-C", str(path), "remote", "get-url", "origin"])
            m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$", remote_url)
            if m:
                repo_slug = m.group(1)

        actions_for_project = []
        if in_ai_projects:
            for sub in ("docs", "reports", "outputs", "work", "work/_scratch", "work/_tests", "work/retained"):
                sd = path / sub
                if not sd.exists():
                    actions_for_project.append(f"mkdir:{sub}")
                    if apply:
                        sd.mkdir(parents=True, exist_ok=True)
            shield = path / "work/.metadata_never_index"
            if not shield.exists():
                actions_for_project.append("touch:work/.metadata_never_index")
                if apply:
                    shield.parent.mkdir(parents=True, exist_ok=True)
                    shield.write_text("", encoding="utf-8")

            if is_git and Path(git_top).resolve() == path.resolve():
                exclude_file = path / ".git/info/exclude"
                if exclude_file.is_file():
                    ex_text = exclude_file.read_text(encoding="utf-8", errors="ignore")
                    if "/work/" not in ex_text and "\nwork/\n" not in f"\n{ex_text}\n":
                        actions_for_project.append("git_exclude:/work/")
                        if apply:
                            with exclude_file.open("a", encoding="utf-8") as f:
                                if not ex_text.endswith("\n"):
                                    f.write("\n")
                                f.write("/work/\n")

        base_key = re.sub(r"[^\w-]+", "-", name.lower(), flags=re.UNICODE).strip("-_")
        if not base_key and repo_slug:
            base_key = re.sub(r"[^\w-]+", "-", repo_slug.split("/")[-1].lower(), flags=re.UNICODE).strip("-_")
        base_key = base_key or "project"
        key = base_key
        if key in used_keys and not in_ai_projects:
            parent_hint = re.sub(r"[^\w-]+", "-", path.parent.name.lower(), flags=re.UNICODE).strip("-_")
            if parent_hint in ("outputs", "work", "repos") and path.parent.parent != path.parent:
                parent_hint = re.sub(r"[^\w-]+", "-", path.parent.parent.name.lower(), flags=re.UNICODE).strip("-_")
            if parent_hint and parent_hint != base_key:
                key = f"{parent_hint}-{base_key}"
        suffix = 2
        while key in used_keys:
            key = f"{base_key}-{suffix}"
            suffix += 1
        used_keys.add(key)
        registry_entries.append({
            "key": key,
            "name": name,
            "repo": repo_slug,
            "remote": "origin" if remote_url else "",
            "path": str(path),
            "is_git": is_git,
        })
        adopted_details.append({
            "name": name,
            "path": str(path),
            "is_git": is_git,
            "repo": repo_slug,
            "actions": actions_for_project,
        })

    return {"count": len(adopted_details), "projects": adopted_details, "registry": registry_entries}


RULE_SNIPPETS = {
    "en": {
        "supervisor": "- **Supervisor**: tasks may be dispatched by `{name}`, the user's personal assistant. Treat its "
        "dispatches as the user's requests, report back to it when done (outcome, tests run, PR link), and take "
        "major decisions to the user, not to the supervisor.",
        "volume": "- **External drive**: the workspace lives on `{path}`{uuid}. Check that it is mounted before "
        "writing; never create folders under `/Volumes/` for a drive that is missing.",
        "volume_uuid": " (volume UUID `{uuid}`)",
        "restricted": " Never push repositories excluded in `~/.orca/ship.json` (`_deny_repos`){branches}.",
        "restricted_branches": " or the restricted branches {branches}",
        "simulators": " Shared simulators: primary `{udid1}` ({name1}){secondary}; never run two tests on one UDID at once.",
        "secondary": ", secondary `{udid2}` ({name2})",
    },
    "zh-CN": {
        "supervisor": "- **总指挥**：任务可能由用户的个人助理 `{name}` 派发，视同用户本人的需求；完成后向它回报结果、"
        "已跑测试与 PR 链接；重大抉择仍直接请示用户本人。",
        "volume": "- **外置工作盘**：工作区位于 `{path}`{uuid}，写入前先核验挂载；硬盘未挂载时严禁在 `/Volumes/` 下新建同名目录。",
        "volume_uuid": "（卷 UUID `{uuid}`）",
        "restricted": "**严禁推送 `~/.orca/ship.json` `_deny_repos` 受限仓库{branches}**。",
        "restricted_branches": "及 {branches} 分支",
        "simulators": "共享模拟器：主机 `{udid1}`（{name1}）{secondary}，避免两项测试同时占用同一 UDID。",
        "secondary": "、副机 `{udid2}`（{name2}）",
    },
}
NO_SUPERVISOR = ("", "none", "off", "false", "no")
NO_RESTRICTION = ("", "none", "off", "false")


def rules_language(value: str) -> str:
    return "zh-CN" if (value or "").strip().lower().startswith("zh") else "en"


def rules_template_path(repo_root: Path, language: str) -> Path:
    name = "AGENTS.zh-CN.md.tmpl" if rules_language(language) == "zh-CN" else "AGENTS.md.tmpl"
    return repo_root / "templates" / name


def detect_rules_language() -> str:
    """zh-CN when the first preferred macOS language is Chinese, otherwise en."""
    rc, out = run_cmd(["/usr/bin/defaults", "read", "-g", "AppleLanguages"], timeout=5)
    match = re.search(r'"?([A-Za-z]{2,3})[-_A-Za-z]*"?', out.strip("()\n ")) if rc == 0 else None
    return "zh-CN" if match and match.group(1).lower() == "zh" else "en"


def render_rules(
    raw: str,
    language: str,
    volume_path: Path,
    volume_uuid: str,
    is_external: bool,
    branch_prefix: str,
    primary_sim: tuple,
    secondary_sim: tuple,
    supervisor: str = "",
    restricted_branches: str = "",
) -> str:
    text = RULE_SNIPPETS[rules_language(language)]
    supervisor = (supervisor or "").strip()
    restricted = [] if restricted_branches.strip().lower() in NO_RESTRICTION else restricted_branches.split()
    sim_rule = ""
    if primary_sim and primary_sim[0]:
        secondary = ""
        if secondary_sim and secondary_sim[0] and secondary_sim[0] != primary_sim[0]:
            secondary = text["secondary"].format(udid2=secondary_sim[0], name2=secondary_sim[1] or "iPhone")
        sim_rule = text["simulators"].format(udid1=primary_sim[0], name1=primary_sim[1] or "iPhone", secondary=secondary)
    joiner = "、" if rules_language(language) == "zh-CN" else ", "
    branches = text["restricted_branches"].format(branches=joiner.join(f"`{b}`" for b in restricted)) if restricted else ""
    volume_rule = ""
    if is_external:
        uuid = text["volume_uuid"].format(uuid=volume_uuid) if volume_uuid not in UUID_SENTINELS else ""
        volume_rule = text["volume"].format(path=volume_path, uuid=uuid)
    values = {
        "WORKSPACE_ROOT": f"{volume_path}/AI",
        "BRANCH_PREFIX": branch_prefix or "codex",
        "SUPERVISOR_RULE": "" if supervisor.lower() in NO_SUPERVISOR else text["supervisor"].format(name=supervisor),
        "VOLUME_RULE": volume_rule,
        "RESTRICTED_RULE": text["restricted"].format(branches=branches),
        "SIMULATOR_RULE": sim_rule,
    }
    rendered = raw
    for key, value in values.items():
        rendered = rendered.replace("${" + key + "}", value)
    return re.sub(r"\n{3,}", "\n\n", rendered)


def render_global_rule_files(
    template_path: Path,
    rendered: str,
    apply: bool,
    force_render: bool = False,
) -> list:
    if not template_path.is_file():
        return []
    home = Path.home()
    targets = [
        home / ".codex/AGENTS.md",
        home / ".claude/CLAUDE.md",
        home / ".gemini/GEMINI.md",
        home / ".gemini/antigravity/GEMINI.md",
        home / "Library/Application Support/orca/antigravity-chat/GEMINI.md",
        home / "Library/Application Support/orca/antigravity-chat/antigravity-acp/GEMINI.md",
    ]
    results = []
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    for t in targets:
        exists = t.is_file()
        if exists and not force_render:
            results.append({"target": str(t), "status": "preserved_existing"})
            continue
        if exists and t.read_text(encoding="utf-8", errors="replace") == rendered:
            results.append({"target": str(t), "status": "unchanged"})
            continue
        results.append({"target": str(t), "status": "rendered" if apply else "would_render"})
        if apply:
            # Write through a symlinked rule file instead of replacing the link with a copy
            real = t.resolve() if t.is_symlink() else t
            real.parent.mkdir(parents=True, exist_ok=True)
            if exists:
                shutil.copy2(real, real.with_name(f"{real.name}.bak-{stamp}"))
            tmp = real.with_name(f".{real.name}.tmp-{os.getpid()}")
            tmp.write_text(rendered, encoding="utf-8")
            os.replace(tmp, real)
    return results


def env_line(key: str, value: str) -> str:
    """A KEY="value" line that zsh `source` reads back verbatim (no expansion, no command substitution)."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    escaped = escaped.replace("\n", " ")
    return f'{key}="{escaped}"\n'


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Apply discovery & adoption changes")
    p.add_argument("--dry-run", action="store_true", help="Preview discovery without writing files")
    p.add_argument("--storage-mode", default=os.environ.get("STORAGE_MODE", "auto"), help="auto | external | internal")
    p.add_argument("--render-rules", action="store_true", help="Force render global AGENTS.md/CLAUDE.md/GEMINI.md (backs up existing)")
    p.add_argument("--write-config", help="Path to write auto-discovered config.env if missing")
    args = p.parse_args()
    apply = args.apply and not args.dry_run

    repo_root = Path(__file__).resolve().parent.parent
    pref_vol = os.environ.get("AI_VOLUME_PATH", "")
    pref_uuid = os.environ.get("AI_VOLUME_UUID", "")
    internal_ws = os.environ.get("INTERNAL_WORKSPACE_PATH", str(Path.home() / "AI-Workspace"))
    pref_prefix = os.environ.get("DEFAULT_BRANCH_PREFIX", "codex")
    pref_sim1 = os.environ.get("PRIMARY_SIMULATOR_UDID", "")
    pref_sim2 = os.environ.get("SECONDARY_SIMULATOR_UDID", "")

    vol_path, vol_uuid, is_external, resolved_mode = detect_volume(
        pref_vol, pref_uuid, storage_mode=args.storage_mode, internal_default=internal_ws
    )
    sim1, sim2 = detect_simulators(pref_sim1, pref_sim2)
    agents = detect_installed_agents()
    projects_info = discover_and_adopt_projects(vol_path, apply=apply)
    supervisor = os.environ.get("SUPERVISOR_AGENT", "").strip() or "none"
    language = os.environ.get("RULES_LANGUAGE", "").strip() or detect_rules_language()
    template = rules_template_path(repo_root, language)
    rendered_rules = render_rules(
        template.read_text(encoding="utf-8") if template.is_file() else "",
        language,
        vol_path,
        vol_uuid,
        is_external,
        pref_prefix,
        sim1,
        sim2,
        supervisor=supervisor,
        restricted_branches=os.environ.get("GUARD_RESTRICTED_BRANCHES", "none"),
    )
    rule_results = render_global_rule_files(template, rendered_rules, apply=apply, force_render=args.render_rules)

    if args.write_config:
        cfg_path = Path(args.write_config)
        if apply and not cfg_path.exists():
            values = [
                ("STORAGE_MODE", resolved_mode),
                ("AI_VOLUME_PATH", str(vol_path)),
                ("AI_VOLUME_UUID", vol_uuid),
                ("INTERNAL_WORKSPACE_PATH", internal_ws),
                ("SUPERVISOR_AGENT", supervisor),
                ("RULES_LANGUAGE", rules_language(language)),
                ("WORKER_AGENTS", ",".join(agents)),
                ("DEFAULT_BRANCH_PREFIX", pref_prefix),
                ("ENABLE_XCODE_MCP", os.environ.get("ENABLE_XCODE_MCP", "true")),
                ("PRIMARY_SIMULATOR_UDID", sim1[0]),
                ("PRIMARY_SIMULATOR_NAME", sim1[1]),
                ("SECONDARY_SIMULATOR_UDID", sim2[0]),
                ("SECONDARY_SIMULATOR_NAME", sim2[1]),
                ("INSTALL_CLI_TOOLCHAIN", "true"),
                ("ENABLE_GIT_ACCELERATION", "true"),
                ("ENABLE_PRETOOLUSE_GUARDRAILS", "true"),
                ("ENABLE_AUTOMATED_BACKUPS", os.environ.get("ENABLE_AUTOMATED_BACKUPS", "true")),
            ]
            header = "# Written by ./deploy.sh --auto; see config.example.env for every key.\n"
            fd = os.open(str(cfg_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(header + "".join(env_line(k, v) for k, v in values))

    summary = {
        "mode": "apply" if apply else "dry-run",
        "discovered_environment": {
            "storage_mode": resolved_mode,
            "volume_path": str(vol_path),
            "volume_uuid": vol_uuid,
            "is_external_volume": is_external,
            "installed_worker_agents": agents,
            "supervisor": supervisor,
            "rules_language": rules_language(language),
            "primary_simulator": {"udid": sim1[0], "name": sim1[1]},
            "secondary_simulator": {"udid": sim2[0], "name": sim2[1]},
        },
        "global_rule_files": rule_results,
        "adopted_projects_count": projects_info["count"],
        "adopted_projects": projects_info["projects"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
