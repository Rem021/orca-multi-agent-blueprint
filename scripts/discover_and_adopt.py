#!/usr/bin/env python3
"""Smart Auto-Discovery & Non-Destructive Adoption Engine.

Works on both:
1. Brand-new Macs (auto-detects storage volume, iOS simulators, installed agents,
   generates config.env, renders global AGENTS.md/CLAUDE.md/GEMINI.md, and initializes taxonomy).
2. Existing Macs with many projects & agents (discovers all repos registered in Orca
   or under AI/Projects, creates missing docs/reports/outputs/work folders, adds /work/
   to .git/info/exclude, builds projects.json registry, and aligns all agent profiles).
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
                for d in dev_list:
                    if d.get("isAvailable") and "iPhone" in d.get("name", ""):
                        score = (
                            100 if "Pro Max" in d["name"] else (80 if "Pro" in d["name"] else 50),
                            os_ver,
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
        primary = (pref_primary or "NONE", "iPhone Simulator (Auto)")

    if pref_secondary and pref_secondary in by_udid and pref_secondary != primary[0]:
        secondary = (pref_secondary, by_udid[pref_secondary])
    else:
        rem = [x for x in devices if x[1] != primary[0]]
        secondary = (rem[0][1], rem[0][2]) if rem else primary

    return primary, secondary


def detect_installed_agents() -> list[str]:
    extra_path = f"{Path.home() / '.local/bin'}:/usr/local/bin:/opt/homebrew/bin:{os.environ.get('PATH', '')}"
    found = []
    checks = [
        ("claude", ["claude"]),
        ("codex", ["codex"]),
        ("antigravity", ["agy", "antigravity"]),
        ("dsh", ["dsh", "dsh-tui"]),
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
    return found or ["claude", "codex", "antigravity"]


def discover_and_adopt_projects(volume_root: Path, apply: bool) -> dict:
    projects_dir = volume_root / "AI/Projects"
    orca_bin = os.environ.get("ORCA_CLI_COMMAND", "/Applications/Orca.app/Contents/Resources/bin/orca")
    discovered = {}

    # 1. Discover from AI/Projects/*
    if projects_dir.is_dir():
        for p in sorted(projects_dir.iterdir()):
            if not p.is_dir() or p.is_symlink() or p.name.startswith("."):
                continue
            discovered[p.name] = p.resolve()

    # 2. Discover from Orca repo list (if running)
    if Path(orca_bin).is_file():
        rc, out = run_cmd([orca_bin, "repo", "list", "--json"], timeout=15)
        if rc == 0 and out:
            try:
                repos = (json.loads(out).get("result") or {}).get("repos") or []
                for r in repos:
                    rpath = Path(r.get("path", ""))
                    if rpath.is_dir() and r.get("displayName"):
                        discovered.setdefault(r["displayName"], rpath.resolve())
            except Exception:
                pass

    adopted_details = []
    registry_entries = []

    for name, path in sorted(discovered.items()):
        # Only mutate subdirs inside volume_root/AI/Projects to stay strictly non-intrusive
        in_ai_projects = projects_dir in path.parents or path.parent == projects_dir
        rc_git, git_top = run_cmd(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
        is_git = rc_git == 0 and bool(git_top)
        remote_url = ""
        repo_slug = ""
        if is_git:
            _, remote_url = run_cmd(["git", "-C", str(path), "remote", "get-url", "origin"])
            m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$", remote_url)
            if m:
                repo_slug = m.group(1)

        actions_for_project = []
        if in_ai_projects:
            for sub in ("docs", "reports", "outputs", "work"):
                sd = path / sub
                if not sd.exists():
                    actions_for_project.append(f"mkdir:{sub}")
                    if apply:
                        sd.mkdir(parents=True, exist_ok=True)

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

        key = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
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


def render_global_rule_files(
    template_path: Path,
    volume_path: Path,
    volume_uuid: str,
    branch_prefix: str,
    primary_sim: tuple[str, str],
    secondary_sim: tuple[str, str],
    apply: bool,
    force_render: bool = False,
) -> list[dict]:
    if not template_path.is_file():
        return []
    raw = template_path.read_text(encoding="utf-8")
    rendered = (
        raw.replace("${AI_VOLUME_PATH}", str(volume_path))
        .replace("${AI_VOLUME_UUID}", volume_uuid or "AUTO")
        .replace("${DEFAULT_BRANCH_PREFIX}", branch_prefix)
        .replace("${PRIMARY_SIMULATOR_UDID}", primary_sim[0])
        .replace("${PRIMARY_SIMULATOR_NAME}", primary_sim[1])
        .replace("${SECONDARY_SIMULATOR_UDID}", secondary_sim[0])
        .replace("${SECONDARY_SIMULATOR_NAME}", secondary_sim[1])
    )

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
    for t in targets:
        exists = t.is_file()
        if exists and not force_render:
            results.append({"target": str(t), "status": "preserved_existing"})
            continue
        results.append({"target": str(t), "status": "rendered" if apply else "would_render"})
        if apply:
            t.parent.mkdir(parents=True, exist_ok=True)
            if exists:
                bak = t.with_name(f"{t.name}.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
                shutil.copy2(t, bak)
            t.write_text(rendered, encoding="utf-8")
    return results


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
    if apply:
        registry_path = Path(__file__).with_name("projects.json")
        registry_path.write_text(
            json.dumps({"projects": projects_info["registry"]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    rule_results = render_global_rule_files(
        repo_root / "templates/AGENTS.md.tmpl",
        vol_path,
        vol_uuid,
        pref_prefix,
        sim1,
        sim2,
        apply=apply,
        force_render=args.render_rules,
    )

    if args.write_config:
        cfg_path = Path(args.write_config)
        if apply and not cfg_path.exists():
            cfg_content = (
                f'STORAGE_MODE="{resolved_mode}"\n'
                f'AI_VOLUME_PATH="{vol_path}"\n'
                f'AI_VOLUME_UUID="{vol_uuid}"\n'
                f'INTERNAL_WORKSPACE_PATH="{internal_ws}"\n'
                f'SUPERVISOR_AGENT="{os.environ.get("SUPERVISOR_AGENT", "muse")}"\n'
                f'WORKER_AGENTS="{",".join(agents)}"\n'
                f'DEFAULT_BRANCH_PREFIX="{pref_prefix}"\n'
                f'ENABLE_XCODE_MCP="{os.environ.get("ENABLE_XCODE_MCP", "true")}"\n'
                f'PRIMARY_SIMULATOR_UDID="{sim1[0]}"\n'
                f'PRIMARY_SIMULATOR_NAME="{sim1[1]}"\n'
                f'SECONDARY_SIMULATOR_UDID="{sim2[0]}"\n'
                f'SECONDARY_SIMULATOR_NAME="{sim2[1]}"\n'
                f'INSTALL_CLI_TOOLCHAIN="true"\n'
                f'ENABLE_GIT_ACCELERATION="true"\n'
                f'ENABLE_PRETOOLUSE_GUARDRAILS="true"\n'
                f'ENABLE_AUTOMATED_BACKUPS="true"\n'
            )
            cfg_path.write_text(cfg_content, encoding="utf-8")

    summary = {
        "mode": "apply" if apply else "dry-run",
        "discovered_environment": {
            "storage_mode": resolved_mode,
            "volume_path": str(vol_path),
            "volume_uuid": vol_uuid,
            "is_external_volume": is_external,
            "installed_worker_agents": agents,
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
