#!/usr/bin/env python3
"""
agent-governor v1.0.0 — Multi-Agent Workspace Governance, Context Optimizer & Ephemeral GC
==========================================================================================
A zero-dependency governance, startup-token optimizer, and 5-gate TTL garbage collection
engine for multi-agent coding environments (Claude Code, OpenAI Codex, Google Gemini /
Antigravity, Cursor, Windsurf, Orca).

Core Capabilities:
  1. `audit`     : Audits startup context token budgets (AGENTS.md / CLAUDE.md / GEMINI.md /
                   SKILL.md frontmatter / eager MCP count), configuration smells, 4-zone
                   directory norms (`docs`, `reports`, `outputs`, `work`), `.git/info/exclude`
                   indexing safety, and unauthorized project-root clutter.
  2. `adopt`     : Non-destructively initializes the 4-zone layout + 3-tier `work/` lifecycle
                   subdirectories (`_scratch`, `_tests`, `retained`), heals `@AGENTS.md`
                   pointers, and optionally relocates root clutter safely.
  3. `scratch`   : Allocates a deterministic, lease-stamped per-agent scratch directory under
                   `<project>/work/_scratch/<YYYYMMDD>-<agent>-<topic>/` (default 3-day TTL).
  4. `test-dir`  : Allocates a deterministic, lease-stamped per-agent test output directory
                   under `<project>/work/_tests/<YYYYMMDD>-<agent>-<topic>/` (default 7-day TTL).
  5. `gc`        : Enforces 5 hard safety gates to sweep expired `work/_scratch`, `work/_tests`,
                   multi-account agent internal caches (`.tmp`, `shell-snapshots`, `paste-cache`,
                   leaked `tmpclaude-*-cwd`, excess `.bak` backups), and idle ephemeral test
                   iOS Simulators — moving files into `~/.Trash/agent-governor-<date>/` with a
                   reversible `manifest.json` receipt (never `rm -rf`).
  6. `triage`    : Read-only inventory & family grouping of legacy untagged `work/*` folders
                   so historical datasets and superseded iterations can be reviewed safely.
  7. `restore`   : Atomically restores any `gc` or `adopt` run from its `manifest.json`.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

VERSION = "1.0.0"

FOUR_ZONES = ("docs", "reports", "outputs", "work")
WORK_TIERS = ("_scratch", "_tests", "retained")

FORBIDDEN_ROOT_DIRS = {
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

BACKUP_FILE_PATTERNS = (
    re.compile(r"^(AGENTS|CLAUDE|GEMINI)\.md\.(bak|backup)(?:[-._].*)?$"),
    re.compile(r"^(config\.toml|settings\.json|hooks\.json|\.codex-global-state\.json|\.claude\.json)\.(bak|backup)(?:[-._].*)?$"),
)

EPHEMERAL_SIM_RE = re.compile(
    r"(?:agent[-_ ]?tmp|scratch|ephemeral|clone[-_ ]|temp[-_ ]sim|测试\s*\d*|临时)",
    re.IGNORECASE,
)

MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
UNRESOLVED_TOKEN_RE = re.compile(r"(\{\{[A-Za-z0-9_.-]+\}\}|\$\{[A-Za-z0-9_]+\})")

GLOBAL_DUPLICATE_PHRASES = (
    "git reset --hard",
    "git clean -fdx",
    "git worktree prune",
    "默认中文",
    "一写一树",
)

WORK_README_CONTENT = """# `work/` — Three-Tier Agent Workspace Lifecycle Contract

This directory is excluded via `.git/info/exclude` (never committed to Git, yet searchable by local agents) and shielded from macOS Spotlight via `.metadata_never_index`.

| Subdirectory | Purpose | Default Retention (TTL) |
| :--- | :--- | :--- |
| `_scratch/<YYYYMMDD>-<agent>-<topic>/` | Throwaway probes, debug logs, screenshots, API dumps | **Auto-reclaimed after 3 days** (into `~/.Trash` with receipt) |
| `_tests/<YYYYMMDD>-<agent>-<topic>/` | Ephemeral test outputs, beta install bundles, `.xcresult` | **Auto-reclaimed after 7 days** (into `~/.Trash` with receipt) |
| `retained/<topic>/` | Long-lived datasets, firmware baselines, reference media | **Protected forever** (never auto-cleaned; `.keep` supported) |

> **Grandfathering Rule**: Legacy directories directly under `work/<name>` (outside `_scratch/` and `_tests/`) are treated as protected historical data and are **never** deleted automatically. Run `agent-governor triage` to inspect them.
"""


# ==============================================================================
# Environment & Config Helpers
# ==============================================================================
def load_orca_env(home: Path | None = None) -> dict[str, str]:
    h = home or Path.home()
    cfg: dict[str, str] = {}
    for p in (h / ".orca/backup-config.env", h / ".orca/guard.env"):
        if not p.is_file():
            continue
        try:
            for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'").replace("$HOME", str(h)).replace("${HOME}", str(h))
                if v:
                    cfg[k.strip()] = v
        except OSError:
            pass
    for k in (
        "STORAGE_MODE",
        "AI_VOLUME_PATH",
        "INTERNAL_WORKSPACE_PATH",
        "PRIMARY_SIMULATOR_UDID",
        "SECONDARY_SIMULATOR_UDID",
    ):
        if os.environ.get(k):
            cfg[k] = os.environ[k].strip()
    return cfg


def resolve_projects_root(explicit_root: str | None = None, home: Path | None = None) -> Path:
    h = home or Path.home()
    if explicit_root:
        p = Path(explicit_root).expanduser().resolve()
        if (p / "AI/Projects").is_dir():
            return p / "AI/Projects"
        if (p / "Projects").is_dir() and p.name == "AI":
            return p / "Projects"
        return p
    cfg = load_orca_env(h)
    vol = cfg.get("AI_VOLUME_PATH") or cfg.get("INTERNAL_WORKSPACE_PATH") or str(h / "AI-Workspace")
    cand = Path(vol).expanduser() / "AI/Projects"
    if cand.is_dir():
        return cand
    fallback = h / "AI-Workspace/AI/Projects"
    if fallback.is_dir():
        return fallback
    return Path.cwd()


def resolve_ai_root(projects_root: Path) -> Path | None:
    if projects_root.name == "Projects" and projects_root.parent.name == "AI":
        return projects_root.parent
    return None


def estimate_tokens(text: str) -> int:
    """Fast deterministic token estimator for mixed English/Code/CJK markdown."""
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii = len(text) - ascii_chars
    # ~4 ASCII chars/token; ~1.3 tokens per CJK/non-ASCII char in BPE tokenizers
    return max(1, int(round(ascii_chars / 4.0 + non_ascii * 1.3))) if text else 0


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value.strip(), flags=re.UNICODE)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-._")
    return cleaned[:64] or "task"


def dir_stats(path: Path) -> tuple[int, float, int]:
    """Return (total_bytes, latest_mtime, file_count) without following symlinks."""
    total = 0
    latest = 0.0
    files = 0
    try:
        st0 = path.lstat()
        latest = st0.st_mtime
        if not path.is_dir() or path.is_symlink():
            return st0.st_size, latest, 1
    except OSError:
        return 0, 0.0, 0

    for root, dirs, filenames in os.walk(path, followlinks=False):
        rpath = Path(root)
        try:
            latest = max(latest, rpath.lstat().st_mtime)
        except OSError:
            pass
        kept_dirs = []
        for d in dirs:
            dp = rpath / d
            try:
                dst = dp.lstat()
                latest = max(latest, dst.st_mtime)
                if not dp.is_symlink():
                    kept_dirs.append(d)
                else:
                    total += dst.st_size
            except OSError:
                pass
        dirs[:] = kept_dirs
        for fn in filenames:
            fp = rpath / fn
            try:
                fst = fp.lstat()
                total += fst.st_size
                latest = max(latest, fst.st_mtime)
                files += 1
            except OSError:
                pass
    return total, latest, files


def format_bytes(num: int) -> str:
    val = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(val) < 1024.0 or unit == "TiB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} TiB"


# ==============================================================================
# Process Liveness & Git Safety Helpers
# ==============================================================================
def snapshot_running_processes() -> tuple[set[int], list[str]]:
    pids: set[int] = set()
    cmds: list[str] = []
    try:
        r = subprocess.run(
            ["/bin/ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                s = line.strip()
                if not s:
                    continue
                parts = s.split(None, 1)
                if parts and parts[0].isdigit():
                    pid = int(parts[0])
                    if pid != os.getpid():
                        pids.add(pid)
                        if len(parts) > 1:
                            cmds.append(parts[1])
    except Exception:
        pass
    return pids, cmds


def pid_alive(pid: int | None, live_pids: set[int] | None = None) -> bool:
    if not pid or pid <= 1:
        return False
    if live_pids is not None:
        return pid in live_pids
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def path_used_by_processes(path: Path, cmds: list[str]) -> bool:
    needle = str(path)
    return any(needle in c for c in cmds)


def is_git_root(project_dir: Path) -> bool:
    return (project_dir / ".git").exists()


def resolve_git_common_exclude(project_dir: Path) -> Path | None:
    dot_git = project_dir / ".git"
    if dot_git.is_dir():
        return dot_git / "info/exclude"
    if dot_git.is_file():
        try:
            raw = dot_git.read_text(encoding="utf-8", errors="ignore").strip()
            if raw.startswith("gitdir:"):
                gitdir = Path(raw.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = (project_dir / gitdir).resolve()
                commondir_file = gitdir / "commondir"
                if commondir_file.is_file():
                    rel = commondir_file.read_text(encoding="utf-8", errors="ignore").strip()
                    common = (gitdir / rel).resolve()
                    return common / "info/exclude"
                return gitdir / "info/exclude"
        except OSError:
            return None
    return None


def has_git_tracked_files(target: Path) -> bool:
    parent = target if target.is_dir() else target.parent
    try:
        r = subprocess.run(
            ["git", "-C", str(parent), "ls-files", "--", str(target)],
            capture_output=True,
            text=True,
            timeout=4,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


# ==============================================================================
# Reversible Trash Staging Engine (Zero `rm -rf`)
# ==============================================================================
class TrashStager:
    def __init__(self, trash_root: Path | None = None, run_label: str = "gc"):
        base = trash_root or (Path.home() / ".Trash")
        day = datetime.now().strftime("%Y-%m-%d")
        stamp = datetime.now().strftime("%H%M%S") + f"-{os.getpid()}"
        self.batch_dir = base / f"agent-governor-{day}" / f"{run_label}-{stamp}"
        self.items_dir = self.batch_dir / "items"
        self.manifest_path = self.batch_dir / "manifest.json"
        self.records: list[dict[str, Any]] = []

    def stage(self, path: Path, reason: str, category: str, size_bytes: int = 0) -> Path:
        self.items_dir.mkdir(parents=True, exist_ok=True)
        idx = len(self.records) + 1
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name)[:80] or "item"
        dest = self.items_dir / f"{idx:04d}_{safe_name}"
        shutil.move(str(path), str(dest))
        rec = {
            "index": idx,
            "category": category,
            "reason": reason,
            "original_path": str(path),
            "staged_path": str(dest),
            "size_bytes": size_bytes,
            "staged_at": datetime.now(timezone.utc).isoformat(),
        }
        self.records.append(rec)
        return dest

    def record_external_action(self, action_type: str, target: str, reason: str, metadata: dict[str, Any]) -> None:
        self.batch_dir.mkdir(parents=True, exist_ok=True)
        self.records.append(
            {
                "index": len(self.records) + 1,
                "category": action_type,
                "reason": reason,
                "original_path": target,
                "staged_path": None,
                "size_bytes": 0,
                "metadata": metadata,
                "staged_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def write_manifest(self, extra: dict[str, Any] | None = None) -> Path | None:
        if not self.records:
            return None
        self.batch_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "agent-governor-manifest/v1",
            "version": VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "total_items": len(self.records),
            "total_bytes": sum(int(r.get("size_bytes", 0)) for r in self.records),
            **(extra or {}),
            "items": self.records,
        }
        self.manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return self.manifest_path


# ==============================================================================
# 1. AUDIT: Context Budget, Config Smells & 4-Zone File Governance
# ==============================================================================
def parse_skill_frontmatter(skill_md: Path) -> dict[str, Any]:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {"valid": False, "frontmatter_bytes": 0, "description_chars": 0}
    if not text.startswith("---\n"):
        return {"valid": False, "frontmatter_bytes": 0, "description_chars": 0}
    end = text.find("\n---", 4)
    if end == -1:
        return {"valid": False, "frontmatter_bytes": 0, "description_chars": 0}
    fm = text[4:end]
    desc = ""
    for line in fm.splitlines():
        if line.startswith("description:"):
            desc = line.split(":", 1)[1].strip()
            break
    fm_bytes = len(fm.encode("utf-8"))
    return {
        "valid": True,
        "frontmatter_bytes": fm_bytes,
        "estimated_tokens": estimate_tokens(fm),
        "description_chars": len(desc),
    }


def count_mcp_servers(home: Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    cj = home / ".claude.json"
    if cj.is_file():
        try:
            d = json.loads(cj.read_text(encoding="utf-8", errors="ignore"))
            mcp = d.get("mcpServers") or {}
            if isinstance(mcp, dict):
                counts["claude_global"] = len(mcp)
        except Exception:
            pass
    ct = home / ".codex/config.toml"
    if ct.is_file():
        try:
            txt = ct.read_text(encoding="utf-8", errors="ignore")
            counts["codex_global"] = len(re.findall(r"^\[mcp_servers\.[^\]]+\]", txt, flags=re.MULTILINE))
        except OSError:
            pass
    return counts


def audit_project_rules(
    proj: Path,
    max_lines: int = 40,
    max_bytes: int = 3000,
) -> dict[str, Any]:
    agents_md = proj / "AGENTS.md"
    claude_md = proj / "CLAUDE.md"
    gemini_md = proj / "GEMINI.md"
    issues: list[str] = []
    smells: list[str] = []

    info: dict[str, Any] = {
        "name": proj.name,
        "path": str(proj),
        "is_git": is_git_root(proj),
        "has_agents_md": agents_md.is_file(),
        "claude_pointer_ok": False,
        "gemini_pointer_ok": False,
        "agents_lines": 0,
        "agents_bytes": 0,
        "agents_tokens": 0,
        "missing_zones": [],
        "missing_work_tiers": [],
        "work_excluded_in_git": None,
        "work_in_gitignore_trap": False,
        "work_spotlight_shielded": (proj / "work/.metadata_never_index").is_file(),
        "root_clutter": [],
        "issues": issues,
        "smells": smells,
    }

    if agents_md.is_file():
        txt = agents_md.read_text(encoding="utf-8", errors="ignore")
        b_len = len(txt.encode("utf-8"))
        l_cnt = len(txt.splitlines())
        info["agents_lines"] = l_cnt
        info["agents_bytes"] = b_len
        info["agents_tokens"] = estimate_tokens(txt)
        if l_cnt > max_lines or b_len > max_bytes:
            smells.append(
                f"oversized_agents_md ({l_cnt} lines / {b_len} B > budget {max_lines}L/{max_bytes}B; "
                "move runbook details to docs/ for progressive disclosure)"
            )
        dups = [p for p in GLOBAL_DUPLICATE_PHRASES if p in txt]
        if dups:
            smells.append(f"duplicates_global_rules ({', '.join(dups)})")
        unresolved = UNRESOLVED_TOKEN_RE.findall(txt)
        if unresolved:
            issues.append(f"unresolved_placeholders ({', '.join(sorted(set(unresolved))[:3])})")
        for m in MD_LINK_RE.finditer(txt):
            raw_target = m.group(1).strip().split("#", 1)[0].strip()
            if not raw_target or "://" in raw_target or raw_target.startswith("mailto:"):
                continue
            target_path = (proj / raw_target).resolve()
            if not target_path.exists():
                issues.append(f"broken_doc_link ({raw_target})")
    else:
        issues.append("missing_AGENTS.md")

    for label, ptr_file in (("claude", claude_md), ("gemini", gemini_md)):
        if not ptr_file.is_file():
            if agents_md.is_file():
                issues.append(f"missing_{ptr_file.name}")
        else:
            ptxt = ptr_file.read_text(encoding="utf-8", errors="ignore").strip()
            if ptxt == "@AGENTS.md":
                info[f"{label}_pointer_ok"] = True
            elif agents_md.is_file() and ptxt != agents_md.read_text(encoding="utf-8", errors="ignore").strip():
                smells.append(f"{ptr_file.name}_diverges_from_AGENTS.md (recommend `@AGENTS.md` pointer)")
            else:
                info[f"{label}_pointer_ok"] = True

    # 4-Zone check
    for z in FOUR_ZONES:
        if not (proj / z).is_dir():
            info["missing_zones"].append(z)
    if info["missing_zones"]:
        issues.append(f"missing_zones ({', '.join(info['missing_zones'])})")

    if (proj / "work").is_dir():
        for tier in WORK_TIERS:
            if not (proj / "work" / tier).is_dir():
                info["missing_work_tiers"].append(tier)
        if not info["work_spotlight_shielded"]:
            smells.append("missing_work_spotlight_shield (.metadata_never_index)")

    # Git exclude vs .gitignore check
    if info["is_git"]:
        ex = resolve_git_common_exclude(proj)
        excluded = False
        if ex and ex.is_file():
            ex_txt = ex.read_text(encoding="utf-8", errors="ignore")
            excluded = bool(re.search(r"(?m)^/?work/?$", ex_txt))
        info["work_excluded_in_git"] = excluded
        if not excluded:
            issues.append("work_not_in_git_info_exclude")
        gi = proj / ".gitignore"
        if gi.is_file():
            gi_txt = gi.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"(?m)^/?work/?$", gi_txt):
                info["work_in_gitignore_trap"] = True
                smells.append("work_in_gitignore (may block IDE/agent search indexers; prefer .git/info/exclude)")

    # Root clutter scan
    try:
        for child in sorted(proj.iterdir()):
            cname = child.name
            if cname in FORBIDDEN_ROOT_DIRS:
                info["root_clutter"].append({"name": cname, "kind": "forbidden_ephemeral_dir"})
                issues.append(f"root_clutter_dir ({cname}/)")
            elif cname.startswith("tmpclaude-"):
                info["root_clutter"].append({"name": cname, "kind": "leaked_claude_cwd_file"})
                issues.append(f"leaked_temp_file ({cname})")
            elif cname == ".DS_Store":
                info["root_clutter"].append({"name": cname, "kind": "ds_store"})
            elif any(pat.match(cname) for pat in BACKUP_FILE_PATTERNS):
                info["root_clutter"].append({"name": cname, "kind": "backup_file"})
                smells.append(f"root_backup_file ({cname})")
    except OSError:
        pass

    return info


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else Path.home()
    projects_root = resolve_projects_root(args.root, home=home)

    # 1. Global rules parity & budget
    global_files = {
        "codex": home / ".codex/AGENTS.md",
        "claude": home / ".claude/CLAUDE.md",
        "gemini": home / ".gemini/GEMINI.md",
    }
    global_report: dict[str, Any] = {}
    hashes: set[str] = set()
    for agent, gf in global_files.items():
        if gf.is_file():
            txt = gf.read_text(encoding="utf-8", errors="ignore")
            sha = hashlib.sha256(txt.encode("utf-8")).hexdigest()[:12]
            hashes.add(sha)
            global_report[agent] = {
                "path": str(gf),
                "present": True,
                "lines": len(txt.splitlines()),
                "bytes": len(txt.encode("utf-8")),
                "estimated_tokens": estimate_tokens(txt),
                "sha256_12": sha,
            }
        else:
            global_report[agent] = {"path": str(gf), "present": False}

    # 2. Skills frontmatter budget
    skills_dir = home / ".agents/skills"
    skill_items: list[dict[str, Any]] = []
    if skills_dir.is_dir():
        for sd in sorted(skills_dir.iterdir()):
            if sd.name.startswith(".") or not (sd.is_dir() or sd.is_symlink()):
                continue
            smd = sd / "SKILL.md"
            if smd.is_file():
                meta = parse_skill_frontmatter(smd)
                skill_items.append({"skill": sd.name, **meta})
    total_skill_tokens = sum(int(s.get("estimated_tokens", 0)) for s in skill_items)
    heavy_skills = [s["skill"] for s in skill_items if int(s.get("description_chars", 0)) > 1200]

    # 3. MCP count
    mcp_counts = count_mcp_servers(home)

    # 4. Projects audit
    project_reports: list[dict[str, Any]] = []
    if projects_root.is_dir():
        candidates = sorted(
            p for p in projects_root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
        )
        # Support auditing a single project passed directly as --root
        if any((projects_root / z).is_dir() for z in FOUR_ZONES) and (projects_root / "AGENTS.md").is_file():
            candidates = [projects_root]
        for p in candidates:
            project_reports.append(
                audit_project_rules(p, max_lines=args.max_project_lines, max_bytes=args.max_project_bytes)
            )

    # 5. Summary aggregation
    attention: list[str] = []
    advice: list[str] = []

    if len(hashes) > 1:
        attention.append("Global rule files (~/.codex/AGENTS.md, ~/.claude/CLAUDE.md, ~/.gemini/GEMINI.md) have drifted out of sync.")
    for agent, ginfo in global_report.items():
        if ginfo.get("present") and (
            ginfo["lines"] > args.max_global_lines or ginfo["bytes"] > args.max_global_bytes
        ):
            advice.append(
                f"Global {agent} rule file ({ginfo['lines']}L / {ginfo['bytes']}B) exceeds lean startup budget "
                f"({args.max_global_lines}L / {args.max_global_bytes}B)."
            )

    for mcp_label, cnt in mcp_counts.items():
        if cnt > 6:
            advice.append(
                f"{mcp_label} has {cnt} eager MCP servers (>6 increases startup token tax; scope per-project where possible)."
            )
    if heavy_skills:
        advice.append(
            f"{len(heavy_skills)} skill(s) have frontmatter descriptions >1200 chars ({', '.join(heavy_skills[:5])}); "
            "trim YAML descriptions so detailed docs load only on trigger."
        )

    for pr in project_reports:
        for iss in pr["issues"]:
            attention.append(f"[{pr['name']}] {iss}")
        if pr["missing_work_tiers"]:
            advice.append(f"[{pr['name']}] work/ missing lifecycle tiers: {', '.join(pr['missing_work_tiers'])}")
        for sm in pr["smells"]:
            advice.append(f"[{pr['name']}] {sm}")

    return {
        "schema": "agent-governor-audit/v1",
        "version": VERSION,
        "projects_root": str(projects_root),
        "global_rules": {
            "in_sync": len(hashes) <= 1,
            "files": global_report,
        },
        "skills_budget": {
            "installed_count": len(skill_items),
            "startup_frontmatter_tokens_est": total_skill_tokens,
            "oversized_descriptions": heavy_skills,
        },
        "mcp_servers": mcp_counts,
        "projects_audited": len(project_reports),
        "projects": project_reports,
        "summary": {
            "healthy": len(attention) == 0,
            "attention_count": len(attention),
            "advice_count": len(advice),
            "attention": attention,
            "advice": advice,
        },
    }


# ==============================================================================
# 2. ADOPT / FIX: Non-Destructive 4-Zone + 3-Tier `work/` & Pointer Healer
# ==============================================================================
def run_adopt(args: argparse.Namespace) -> dict[str, Any]:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else Path.home()
    projects_root = resolve_projects_root(args.root, home=home)
    apply = bool(args.apply)
    trash_root = Path(args.trash_dir).expanduser().resolve() if getattr(args, "trash_dir", None) else None
    stager = TrashStager(trash_root=trash_root, run_label="adopt")
    actions: list[dict[str, Any]] = []

    candidates: list[Path] = []
    if projects_root.is_dir():
        if any((projects_root / z).is_dir() for z in FOUR_ZONES) and (projects_root / "AGENTS.md").is_file():
            candidates = [projects_root]
        else:
            candidates = sorted(
                p for p in projects_root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
            )

    today = datetime.now().strftime("%Y%m%d")

    for proj in candidates:
        # 1. 4 zones + 3 work tiers
        for z in FOUR_ZONES:
            zp = proj / z
            if not zp.exists():
                actions.append({"project": proj.name, "action": "mkdir_zone", "path": str(zp)})
                if apply:
                    zp.mkdir(parents=True, exist_ok=True)

        work_dir = proj / "work"
        for tier in WORK_TIERS:
            tp = work_dir / tier
            if not tp.exists():
                actions.append({"project": proj.name, "action": "mkdir_work_tier", "path": str(tp)})
                if apply:
                    tp.mkdir(parents=True, exist_ok=True)

        shield = work_dir / ".metadata_never_index"
        if not shield.exists():
            actions.append({"project": proj.name, "action": "touch_spotlight_shield", "path": str(shield)})
            if apply and work_dir.is_dir():
                shield.write_text("", encoding="utf-8")

        wreadme = work_dir / "README.md"
        if not wreadme.exists():
            actions.append({"project": proj.name, "action": "write_work_contract", "path": str(wreadme)})
            if apply and work_dir.is_dir():
                wreadme.write_text(WORK_README_CONTENT, encoding="utf-8")

        # 2. .git/info/exclude
        if is_git_root(proj):
            ex = resolve_git_common_exclude(proj)
            if ex:
                existing = ex.read_text(encoding="utf-8", errors="ignore") if ex.is_file() else ""
                if not re.search(r"(?m)^/?work/?$", existing):
                    actions.append({"project": proj.name, "action": "git_info_exclude_work", "path": str(ex)})
                    if apply:
                        ex.parent.mkdir(parents=True, exist_ok=True)
                        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
                        with ex.open("a", encoding="utf-8") as f:
                            f.write(f"{prefix}/work/\n")

        # 3. @AGENTS.md pointer healing
        if (proj / "AGENTS.md").is_file():
            for ptr_name in ("CLAUDE.md", "GEMINI.md"):
                ptr = proj / ptr_name
                if not ptr.exists():
                    actions.append({"project": proj.name, "action": f"create_pointer_{ptr_name}", "path": str(ptr)})
                    if apply:
                        ptr.write_text("@AGENTS.md\n", encoding="utf-8")

        # 4. Optional root clutter safe relocation / staging
        if args.relocate_clutter:
            for child in sorted(proj.iterdir()):
                cname = child.name
                if cname == ".DS_Store" and child.is_file() and not child.is_symlink():
                    sz = child.stat().st_size
                    actions.append({"project": proj.name, "action": "trash_ds_store", "path": str(child)})
                    if apply:
                        stager.stage(child, reason="root .DS_Store cleanup", category="root_ds_store", size_bytes=sz)
                elif cname.startswith("tmpclaude-") and child.is_file() and not child.is_symlink():
                    if not has_git_tracked_files(child):
                        sz = child.stat().st_size
                        actions.append({"project": proj.name, "action": "trash_leaked_tmpclaude", "path": str(child)})
                        if apply:
                            stager.stage(child, reason="leaked tmpclaude root file", category="tmpclaude", size_bytes=sz)
                elif cname in FORBIDDEN_ROOT_DIRS and child.is_dir() and not child.is_symlink():
                    if has_git_tracked_files(child):
                        continue
                    entries = [x for x in child.iterdir() if x.name != ".DS_Store"]
                    if not entries:
                        sz, _, _ = dir_stats(child)
                        actions.append({"project": proj.name, "action": "trash_empty_root_clutter", "path": str(child)})
                        if apply:
                            stager.stage(
                                child,
                                reason=f"empty unauthorized root dir `{cname}/`",
                                category="empty_root_clutter",
                                size_bytes=sz,
                            )
                    else:
                        reloc_base = proj / "work/retained" / f"from-root-{cname}-{today}"
                        reloc_dest = reloc_base
                        seq = 2
                        while reloc_dest.exists():
                            reloc_dest = Path(f"{reloc_base}-{seq}")
                            seq += 1
                        actions.append(
                            {
                                "project": proj.name,
                                "action": "relocate_nonempty_root_clutter",
                                "from": str(child),
                                "to": str(reloc_dest),
                            }
                        )
                        if apply:
                            reloc_dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(child), str(reloc_dest))

    manifest = stager.write_manifest({"mode": "adopt", "projects_root": str(projects_root)}) if apply else None
    return {
        "mode": "apply" if apply else "dry-run",
        "projects_root": str(projects_root),
        "action_count": len(actions),
        "manifest": str(manifest) if manifest else None,
        "actions": actions,
    }


# ==============================================================================
# 3. SCRATCH & TEST-DIR ALLOCATOR: Lease-Stamped Per-Agent Directories
# ==============================================================================
def infer_project_dir(explicit_project: str | None, cwd: Path | None = None) -> Path:
    start = Path(explicit_project).expanduser().resolve() if explicit_project else (cwd or Path.cwd()).resolve()
    if start.is_file():
        start = start.parent
    for cand in [start, *start.parents]:
        if (cand / "work").is_dir() or (cand / ".git").exists() or (cand / "AGENTS.md").is_file():
            return cand
    return start


def allocate_ephemeral_dir(zone: str, args: argparse.Namespace) -> Path:
    proj = infer_project_dir(args.project)
    agent = slugify(
        args.agent
        or os.environ.get("ORCA_AGENT_TYPE")
        or ("claude" if os.environ.get("CLAUDECODE") else "")
        or ("codex" if os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_CI") else "")
        or "agent"
    )
    topic = slugify(args.topic)
    ttl_hours = float(args.ttl_days) * 24.0 if getattr(args, "ttl_days", None) is not None else float(args.ttl_hours)
    sub = "_scratch" if zone == "scratch" else "_tests"
    day = datetime.now().strftime("%Y%m%d")

    work_dir = proj / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    shield = work_dir / ".metadata_never_index"
    if not shield.exists():
        shield.write_text("", encoding="utf-8")

    tier_dir = work_dir / sub
    tier_dir.mkdir(parents=True, exist_ok=True)

    target = tier_dir / f"{day}-{agent}-{topic}"
    if args.unique and target.exists():
        stamp = datetime.now().strftime("%H%M%S")
        target = tier_dir / f"{day}-{agent}-{topic}-{stamp}"
    target.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    lease = {
        "schema": "agent-governor-lease/v1",
        "zone": zone,
        "project": str(proj),
        "agent": agent,
        "topic": topic,
        "pid": os.getppid(),
        "session_id": os.environ.get("ORCA_PANE_KEY") or os.environ.get("CLAUDE_SESSION_ID") or "",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(),
        "ttl_hours": ttl_hours,
    }
    (target / ".agent-lease.json").write_text(json.dumps(lease, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


# ==============================================================================
# 4. FIVE-GATE GARBAGE COLLECTOR (`gc`)
# ==============================================================================
@dataclass
class GCCandidate:
    category: str
    path: Path
    size_bytes: int
    age_hours: float
    reason: str


def has_pin_marker(directory: Path) -> bool:
    return any((directory / marker).exists() for marker in (".keep", ".pin", "HOLD", ".hold"))


def read_lease(directory: Path) -> dict[str, Any]:
    lf = directory / ".agent-lease.json"
    if not lf.is_file():
        return {}
    try:
        d = json.loads(lf.read_text(encoding="utf-8", errors="ignore"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def scan_work_ephemeral_candidates(
    projects_dirs: list[Path],
    now_ts: float,
    scratch_ttl_h: float,
    tests_ttl_h: float,
    min_idle_h: float,
    live_pids: set[int],
    proc_cmds: list[str],
    session_end_only: bool = False,
) -> tuple[list[GCCandidate], list[dict[str, str]]]:
    candidates: list[GCCandidate] = []
    protected_skips: list[dict[str, str]] = []

    zones = [("_scratch", scratch_ttl_h)]
    if not session_end_only:
        zones.append(("_tests", tests_ttl_h))

    for proj in projects_dirs:
        # Sweep leaked root `tmpclaude-*-cwd` files in both modes if idle & unreferenced
        try:
            for item in proj.iterdir():
                if item.name.startswith("tmpclaude-") and item.is_file() and not item.is_symlink():
                    st = item.lstat()
                    age_h = max(0.0, (now_ts - st.st_mtime) / 3600.0)
                    if age_h >= max(0.25, min_idle_h) and not path_used_by_processes(item, proc_cmds):
                        if not has_git_tracked_files(item):
                            candidates.append(
                                GCCandidate(
                                    category="leaked_tmpclaude",
                                    path=item,
                                    size_bytes=st.st_size,
                                    age_hours=round(age_h, 2),
                                    reason=f"leaked root {item.name} idle {age_h:.1f}h",
                                )
                            )
        except OSError:
            pass

        work_dir = proj / "work"
        if not work_dir.is_dir() or work_dir.is_symlink():
            continue

        for sub_name, ttl_h in zones:
            tier_dir = work_dir / sub_name
            if not tier_dir.is_dir() or tier_dir.is_symlink():
                continue
            for entry in sorted(tier_dir.iterdir()):
                if entry.name.startswith("."):
                    continue
                if entry.is_symlink():
                    protected_skips.append({"path": str(entry), "gate": "Gate03_SymlinkGuard"})
                    continue
                if entry.is_dir() and has_pin_marker(entry):
                    protected_skips.append({"path": str(entry), "gate": "Gate02_PinMarker"})
                    continue

                lease = read_lease(entry) if entry.is_dir() else {}
                lease_pid = lease.get("pid") if isinstance(lease.get("pid"), int) else None
                if pid_alive(lease_pid, live_pids) or path_used_by_processes(entry, proc_cmds):
                    protected_skips.append({"path": str(entry), "gate": "Gate01_ActiveProcessLock"})
                    continue

                sz, latest_mtime, file_cnt = dir_stats(entry)
                idle_h = max(0.0, (now_ts - latest_mtime) / 3600.0)

                # Session-end fast reaper: only reap empty scratch folders whose creator PID is gone
                if session_end_only:
                    non_lease_files = 0
                    if entry.is_dir():
                        non_lease_files = sum(
                            1 for p in entry.rglob("*") if p.is_file() and p.name not in {".agent-lease.json", ".DS_Store"}
                        )
                    if entry.is_dir() and non_lease_files == 0 and idle_h >= 0.05:
                        candidates.append(
                            GCCandidate(
                                category=f"empty_{sub_name.lstrip('_')}",
                                path=entry,
                                size_bytes=sz,
                                age_hours=round(idle_h, 2),
                                reason="empty agent scratch directory after session exit",
                            )
                        )
                    continue

                if idle_h < min_idle_h:
                    protected_skips.append({"path": str(entry), "gate": "Gate01_MinIdleGuard"})
                    continue

                # Check lease explicit expiration OR mtime TTL
                expired = idle_h >= ttl_h
                if not expired and lease.get("expires_at"):
                    try:
                        exp_dt = datetime.fromisoformat(str(lease["expires_at"]))
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                        if datetime.now(timezone.utc) >= exp_dt and idle_h >= min_idle_h:
                            expired = True
                    except ValueError:
                        pass

                if not expired:
                    continue

                if has_git_tracked_files(entry):
                    protected_skips.append({"path": str(entry), "gate": "Gate03_GitTrackedGuard"})
                    continue

                candidates.append(
                    GCCandidate(
                        category=f"work{sub_name}",
                        path=entry,
                        size_bytes=sz,
                        age_hours=round(idle_h, 1),
                        reason=f"{sub_name} expired (idle {idle_h / 24.0:.1f}d >= TTL {ttl_h / 24.0:.1f}d)",
                    )
                )

    return candidates, protected_skips


def scan_agent_home_ephemerals(
    home: Path,
    now_ts: float,
    tmp_ttl_h: float,
    cache_ttl_h: float,
    keep_backups: int,
    proc_cmds: list[str],
) -> list[GCCandidate]:
    candidates: list[GCCandidate] = []
    orca_dir = home / "Library/Application Support/orca"

    codex_homes = [home / ".codex"]
    if (orca_dir / "codex-accounts").is_dir():
        codex_homes.extend(sorted((orca_dir / "codex-accounts").glob("*/home")))

    claude_homes = [home / ".claude"]
    if (orca_dir / "claude-profiles").is_dir():
        claude_homes.extend(sorted((orca_dir / "claude-profiles").glob("*/home")))

    # 1. Codex internal .tmp, tmp, shell_snapshots
    for ch in codex_homes:
        if not ch.is_dir() or ch.is_symlink():
            continue
        for tdir_name in (".tmp", "tmp"):
            tdir = ch / tdir_name
            if tdir.is_dir() and not tdir.is_symlink():
                for item in sorted(tdir.iterdir()):
                    if item.is_symlink() or path_used_by_processes(item, proc_cmds):
                        continue
                    sz, mtime, _ = dir_stats(item)
                    age_h = max(0.0, (now_ts - mtime) / 3600.0)
                    if age_h >= tmp_ttl_h:
                        candidates.append(
                            GCCandidate(
                                category="codex_tmp",
                                path=item,
                                size_bytes=sz,
                                age_hours=round(age_h, 1),
                                reason=f"Codex {tdir_name} artifact idle {age_h:.1f}h >= {tmp_ttl_h:.0f}h",
                            )
                        )
        ss = ch / "shell_snapshots"
        if ss.is_dir() and not ss.is_symlink():
            for item in sorted(ss.iterdir()):
                if item.is_symlink() or path_used_by_processes(item, proc_cmds):
                    continue
                sz, mtime, _ = dir_stats(item)
                age_h = max(0.0, (now_ts - mtime) / 3600.0)
                if age_h >= cache_ttl_h:
                    candidates.append(
                        GCCandidate(
                            category="codex_shell_snapshot",
                            path=item,
                            size_bytes=sz,
                            age_hours=round(age_h, 1),
                            reason=f"Codex shell snapshot idle {age_h / 24.0:.1f}d",
                        )
                    )

    # 2. Claude internal shell-snapshots, paste-cache, empty session-env
    for clh in claude_homes:
        if not clh.is_dir() or clh.is_symlink():
            continue
        for sub_name, cat_name in (("shell-snapshots", "claude_shell_snapshot"), ("paste-cache", "claude_paste_cache")):
            sdir = clh / sub_name
            if sdir.is_dir() and not sdir.is_symlink():
                for item in sorted(sdir.iterdir()):
                    if item.is_symlink() or path_used_by_processes(item, proc_cmds):
                        continue
                    sz, mtime, _ = dir_stats(item)
                    age_h = max(0.0, (now_ts - mtime) / 3600.0)
                    if age_h >= cache_ttl_h:
                        candidates.append(
                            GCCandidate(
                                category=cat_name,
                                path=item,
                                size_bytes=sz,
                                age_hours=round(age_h, 1),
                                reason=f"Claude {sub_name} idle {age_h / 24.0:.1f}d",
                            )
                        )
        senv = clh / "session-env"
        if senv.is_dir() and not senv.is_symlink():
            for item in sorted(senv.iterdir()):
                if not item.is_dir() or item.is_symlink() or path_used_by_processes(item, proc_cmds):
                    continue
                sz, mtime, cnt = dir_stats(item)
                age_h = max(0.0, (now_ts - mtime) / 3600.0)
                if (cnt == 0 and age_h >= tmp_ttl_h) or age_h >= cache_ttl_h * 2:
                    candidates.append(
                        GCCandidate(
                            category="claude_session_env",
                            path=item,
                            size_bytes=sz,
                            age_hours=round(age_h, 1),
                            reason=f"stale Claude session-env ({cnt} files, idle {age_h / 24.0:.1f}d)",
                        )
                    )

    # 3. Orca chat-scratch (>2x cache_ttl_h) & Antigravity brain/*/scratch (>cache_ttl_h)
    chat_scratch = orca_dir / "chat-scratch"
    if chat_scratch.is_dir() and not chat_scratch.is_symlink():
        for item in sorted(chat_scratch.iterdir()):
            if item.is_symlink() or path_used_by_processes(item, proc_cmds):
                continue
            sz, mtime, _ = dir_stats(item)
            age_h = max(0.0, (now_ts - mtime) / 3600.0)
            if age_h >= cache_ttl_h * 2:
                candidates.append(
                    GCCandidate(
                        category="orca_chat_scratch",
                        path=item,
                        size_bytes=sz,
                        age_hours=round(age_h, 1),
                        reason=f"Orca chat-scratch idle {age_h / 24.0:.1f}d",
                    )
                )

    agy_brain = orca_dir / "antigravity-chat/antigravity-acp/brain"
    if agy_brain.is_dir() and not agy_brain.is_symlink():
        for conv_dir in sorted(agy_brain.iterdir()):
            if not conv_dir.is_dir() or conv_dir.is_symlink():
                continue
            sdir = conv_dir / "scratch"
            if not sdir.is_dir() or sdir.is_symlink():
                continue
            for item in sorted(sdir.iterdir()):
                if item.is_symlink() or path_used_by_processes(item, proc_cmds):
                    continue
                sz, mtime, _ = dir_stats(item)
                age_h = max(0.0, (now_ts - mtime) / 3600.0)
                if age_h >= cache_ttl_h:
                    candidates.append(
                        GCCandidate(
                            category="antigravity_scratch",
                            path=item,
                            size_bytes=sz,
                            age_hours=round(age_h, 1),
                            reason=f"Antigravity scratch artifact idle {age_h / 24.0:.1f}d",
                        )
                    )

    # 4. Config backup file retention (`*.bak*`, `*.backup*`) in agent config dirs
    if keep_backups >= 0:
        for cfg_dir in [*codex_homes, *claude_homes, home / ".gemini"]:
            if not cfg_dir.is_dir() or cfg_dir.is_symlink():
                continue
            grouped: dict[str, list[tuple[float, int, Path]]] = defaultdict(list)
            for item in cfg_dir.iterdir():
                if not item.is_file() or item.is_symlink():
                    continue
                for pat in BACKUP_FILE_PATTERNS:
                    m = pat.match(item.name)
                    if m:
                        st = item.lstat()
                        grouped[m.group(1)].append((st.st_mtime, st.st_size, item))
                        break
            for base_name, entries in grouped.items():
                entries.sort(key=lambda x: x[0], reverse=True)
                for mtime, sz, old_bak in entries[keep_backups:]:
                    age_h = max(0.0, (now_ts - mtime) / 3600.0)
                    candidates.append(
                        GCCandidate(
                            category="excess_config_backup",
                            path=old_bak,
                            size_bytes=sz,
                            age_hours=round(age_h, 1),
                            reason=f"superseded backup of {base_name} (retaining newest {keep_backups})",
                        )
                    )

    return candidates


def scan_ephemeral_simulators(
    now_ts: float,
    sim_ttl_h: float,
    protected_udids: set[str],
    proc_cmds: list[str],
) -> list[dict[str, Any]]:
    if not Path("/usr/bin/xcrun").is_file():
        return []
    try:
        r = subprocess.run(
            ["/usr/bin/xcrun", "simctl", "list", "devices", "-j"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode != 0 or not r.stdout:
            return []
        data = json.loads(r.stdout)
    except Exception:
        return []

    reapable: list[dict[str, Any]] = []
    for runtime, devices in (data.get("devices") or {}).items():
        if not isinstance(devices, list):
            continue
        for dev in devices:
            udid = str(dev.get("udid") or "")
            name = str(dev.get("name") or "")
            state = str(dev.get("state") or "")
            if not udid or udid in protected_udids:
                continue
            if state != "Shutdown":
                continue
            if not EPHEMERAL_SIM_RE.search(name):
                continue
            if any(udid in c for c in proc_cmds):
                continue
            data_path = Path(str(dev.get("dataPath") or ""))
            if not data_path.is_dir():
                continue
            sz, mtime, _ = dir_stats(data_path)
            idle_h = max(0.0, (now_ts - mtime) / 3600.0)
            if idle_h >= sim_ttl_h:
                reapable.append(
                    {
                        "udid": udid,
                        "name": name,
                        "runtime": runtime,
                        "data_path": str(data_path),
                        "size_bytes": sz,
                        "idle_hours": round(idle_h, 1),
                    }
                )
    return reapable


def collect_project_targets(projects_root: Path) -> list[Path]:
    targets: list[Path] = []
    if not projects_root.is_dir():
        return targets
    if any((projects_root / z).is_dir() for z in FOUR_ZONES) and (projects_root / "AGENTS.md").is_file():
        targets.append(projects_root)
    else:
        for p in sorted(projects_root.iterdir()):
            if p.is_dir() and not p.is_symlink() and not p.name.startswith("."):
                targets.append(p)
    ai_root = resolve_ai_root(projects_root)
    if ai_root:
        for wt_parent in (ai_root / "Orca/Worktrees", ai_root / "Codex/Worktrees", ai_root / "Claude/Worktrees"):
            if wt_parent.is_dir():
                for child in sorted(wt_parent.iterdir()):
                    if child.is_dir() and not child.is_symlink():
                        targets.append(child)
    return targets


def run_gc(args: argparse.Namespace) -> dict[str, Any]:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else Path.home()
    projects_root = resolve_projects_root(args.root, home=home)
    apply = bool(args.apply)
    mode = args.mode
    now_ts = time.time()

    live_pids, proc_cmds = snapshot_running_processes()
    targets = collect_project_targets(projects_root)

    scratch_ttl_h = float(args.scratch_ttl_days) * 24.0
    tests_ttl_h = float(args.tests_ttl_days) * 24.0
    cache_ttl_h = float(args.cache_ttl_days) * 24.0
    tmp_ttl_h = float(args.tmp_ttl_hours)
    sim_ttl_h = float(args.sim_ttl_days) * 24.0
    min_idle_h = float(args.min_idle_hours)

    work_cands, skips = scan_work_ephemeral_candidates(
        projects_dirs=targets,
        now_ts=now_ts,
        scratch_ttl_h=scratch_ttl_h,
        tests_ttl_h=tests_ttl_h,
        min_idle_h=min_idle_h,
        live_pids=live_pids,
        proc_cmds=proc_cmds,
        session_end_only=(mode == "session-end"),
    )

    home_cands: list[GCCandidate] = []
    sim_cands: list[dict[str, Any]] = []

    if mode != "session-end" and not args.workspace_only:
        home_cands = scan_agent_home_ephemerals(
            home=home,
            now_ts=now_ts,
            tmp_ttl_h=tmp_ttl_h,
            cache_ttl_h=cache_ttl_h,
            keep_backups=args.keep_backups,
            proc_cmds=proc_cmds,
        )
        if not args.skip_simulators:
            cfg = load_orca_env(home)
            protected_udids = {
                u
                for u in (
                    cfg.get("PRIMARY_SIMULATOR_UDID", ""),
                    cfg.get("SECONDARY_SIMULATOR_UDID", ""),
                )
                if u
            }
            sim_cands = scan_ephemeral_simulators(
                now_ts=now_ts,
                sim_ttl_h=sim_ttl_h,
                protected_udids=protected_udids,
                proc_cmds=proc_cmds,
            )

    all_fs_cands = work_cands + home_cands
    trash_root = Path(args.trash_dir).expanduser().resolve() if getattr(args, "trash_dir", None) else None
    stager = TrashStager(trash_root=trash_root, run_label=f"gc-{mode}")
    errors: list[dict[str, str]] = []

    if apply:
        for cand in all_fs_cands:
            if not cand.path.exists():
                continue
            try:
                stager.stage(cand.path, reason=cand.reason, category=cand.category, size_bytes=cand.size_bytes)
            except OSError as exc:
                errors.append({"path": str(cand.path), "error": str(exc)})

        for sim in sim_cands:
            try:
                r = subprocess.run(
                    ["/usr/bin/xcrun", "simctl", "delete", sim["udid"]],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if r.returncode == 0:
                    stager.record_external_action(
                        action_type="ephemeral_simulator_delete",
                        target=f"{sim['name']} ({sim['udid']})",
                        reason=f"idle Shutdown ephemeral simulator ({sim['idle_hours']}h >= {sim_ttl_h:.0f}h)",
                        metadata=sim,
                    )
                else:
                    errors.append({"path": sim["udid"], "error": (r.stderr or r.stdout).strip()})
            except Exception as exc:
                errors.append({"path": sim["udid"], "error": str(exc)})

    manifest = stager.write_manifest({"mode": mode, "projects_root": str(projects_root)}) if apply else None
    fs_bytes = sum(c.size_bytes for c in all_fs_cands)
    sim_bytes = sum(int(s["size_bytes"]) for s in sim_cands)

    by_cat: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "bytes": 0})
    for c in all_fs_cands:
        by_cat[c.category]["count"] += 1
        by_cat[c.category]["bytes"] += c.size_bytes
    if sim_cands:
        by_cat["ephemeral_simulator"]["count"] = len(sim_cands)
        by_cat["ephemeral_simulator"]["bytes"] = sim_bytes

    return {
        "schema": "agent-governor-gc/v1",
        "version": VERSION,
        "execution": "applied" if apply else "dry-run",
        "mode": mode,
        "projects_root": str(projects_root),
        "total_candidates": len(all_fs_cands) + len(sim_cands),
        "reclaimable_bytes": fs_bytes + sim_bytes,
        "reclaimable_human": format_bytes(fs_bytes + sim_bytes),
        "categories": dict(by_cat),
        "protected_skips_count": len(skips),
        "manifest": str(manifest) if manifest else None,
        "candidates": [
            {
                "category": c.category,
                "path": str(c.path),
                "size_bytes": c.size_bytes,
                "size_human": format_bytes(c.size_bytes),
                "age_hours": c.age_hours,
                "reason": c.reason,
            }
            for c in all_fs_cands
        ],
        "ephemeral_simulators": sim_cands,
        "errors": errors,
    }


# ==============================================================================
# 5. TRIAGE: Read-Only Legacy `work/` Family Grouping & Superseded Run Analyzer
# ==============================================================================
_ITER_STRIP_RE = re.compile(
    r"(?:[-_](?:20\d{6}|20\d{2}-\d{2}-\d{2}|beta\d+|rc\d+|v\d+(?:\.\d+)*|r\d+|\d{3}-\d{2,3}))+$",
    re.IGNORECASE,
)


def normalize_family_stem(name: str) -> str:
    s = re.sub(r"^20\d{6}[-_]", "", name)
    s = re.sub(r"^20\d{2}-\d{2}-\d{2}[-_]", "", s)
    s = _ITER_STRIP_RE.sub("", s)
    return s or name


def run_triage(args: argparse.Namespace) -> dict[str, Any]:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else Path.home()
    projects_root = resolve_projects_root(args.root, home=home)
    now_ts = time.time()

    project_summaries: list[dict[str, Any]] = []
    grand_legacy_bytes = 0
    grand_legacy_count = 0

    candidates = (
        sorted(p for p in projects_root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
        if projects_root.is_dir()
        else []
    )
    if any((projects_root / z).is_dir() for z in FOUR_ZONES) and (projects_root / "work").is_dir():
        candidates = [projects_root]

    for proj in candidates:
        work_dir = proj / "work"
        if not work_dir.is_dir() or work_dir.is_symlink():
            continue
        legacy_items: list[dict[str, Any]] = []
        families: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for entry in sorted(work_dir.iterdir()):
            if entry.name.startswith(".") or entry.name in WORK_TIERS or entry.name == "README.md":
                continue
            if entry.is_symlink():
                continue
            sz, mtime, fcnt = dir_stats(entry)
            age_d = round(max(0.0, (now_ts - mtime) / 86400.0), 1)
            fam = normalize_family_stem(entry.name)
            rec = {
                "name": entry.name,
                "path": str(entry),
                "family": fam,
                "size_bytes": sz,
                "size_human": format_bytes(sz),
                "files": fcnt,
                "age_days": age_d,
                "pinned": entry.is_dir() and has_pin_marker(entry),
            }
            legacy_items.append(rec)
            families[fam].append(rec)

        if not legacy_items:
            continue

        proj_bytes = sum(x["size_bytes"] for x in legacy_items)
        grand_legacy_bytes += proj_bytes
        grand_legacy_count += len(legacy_items)

        multi_series = []
        for fam, members in sorted(families.items(), key=lambda kv: sum(m["size_bytes"] for m in kv[1]), reverse=True):
            if len(members) >= 2:
                members_sorted = sorted(members, key=lambda m: m["age_days"])
                multi_series.append(
                    {
                        "family": fam,
                        "count": len(members),
                        "total_bytes": sum(m["size_bytes"] for m in members),
                        "total_human": format_bytes(sum(m["size_bytes"] for m in members)),
                        "newest": members_sorted[0]["name"],
                        "older_candidates_count": len(members_sorted) - 1,
                        "older_candidates_bytes": sum(m["size_bytes"] for m in members_sorted[1:]),
                        "older_candidates_human": format_bytes(sum(m["size_bytes"] for m in members_sorted[1:])),
                    }
                )

        legacy_items.sort(key=lambda x: x["size_bytes"], reverse=True)
        project_summaries.append(
            {
                "project": proj.name,
                "work_path": str(work_dir),
                "legacy_count": len(legacy_items),
                "legacy_bytes": proj_bytes,
                "legacy_human": format_bytes(proj_bytes),
                "iterative_families": multi_series[:15],
                "largest_items": legacy_items[:15],
            }
        )

    project_summaries.sort(key=lambda x: x["legacy_bytes"], reverse=True)
    report = {
        "schema": "agent-governor-triage/v1",
        "version": VERSION,
        "note": "Read-only report. Pre-existing untagged `work/<dir>` folders are protected by Grandfathering and never auto-deleted.",
        "projects_root": str(projects_root),
        "total_legacy_items": grand_legacy_count,
        "total_legacy_bytes": grand_legacy_bytes,
        "total_legacy_human": format_bytes(grand_legacy_bytes),
        "projects": project_summaries,
    }

    if getattr(args, "markdown", None):
        md_path = Path(args.markdown).expanduser().resolve()
        md_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Legacy `work/` Triage Report (Grandfathered & Protected)",
            "",
            f"- **Scanned Root**: `{projects_root}`",
            f"- **Protected Legacy Folders**: **{grand_legacy_count}** items (**{format_bytes(grand_legacy_bytes)}**)",
            "- **Policy**: Zero automatic deletion. Promote long-term assets into `work/retained/` or archive/trash obsolete iterative series after review.",
            "",
            "| Project | Legacy Folders | Total Size | Iterative Series Detected |",
            "| :--- | ---: | ---: | ---: |",
        ]
        for ps in project_summaries:
            lines.append(
                f"| `{ps['project']}` | {ps['legacy_count']} | {ps['legacy_human']} | {len(ps['iterative_families'])} |"
            )
        for ps in project_summaries:
            if not ps["iterative_families"] and ps["legacy_bytes"] < 50 * 1024 * 1024:
                continue
            lines.extend(["", f"## `{ps['project']}` ({ps['legacy_human']}, {ps['legacy_count']} folders)"])
            if ps["iterative_families"]:
                lines.append("")
                lines.append("### Multi-Run Iterative Series (Newest vs Older Runs)")
                lines.append("| Series Family | Runs | Total | Newest Run | Older Runs Size |")
                lines.append("| :--- | ---: | ---: | :--- | ---: |")
                for fam in ps["iterative_families"][:10]:
                    lines.append(
                        f"| `{fam['family']}` | {fam['count']} | {fam['total_human']} | `{fam['newest']}` | {fam['older_candidates_human']} |"
                    )
            lines.append("")
            lines.append("### Largest Legacy Folders")
            for item in ps["largest_items"][:8]:
                lines.append(f"- `{item['name']}` — **{item['size_human']}** ({item['files']} files, idle {item['age_days']}d)")
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return report


# ==============================================================================
# 6. RESTORE: Atomic Manifest Restoration
# ==============================================================================
def find_latest_manifest(trash_root: Path | None = None) -> Path:
    base = trash_root or (Path.home() / ".Trash")
    manifests = sorted(base.glob("agent-governor-*/*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not manifests:
        raise FileNotFoundError(f"No agent-governor manifest.json found under {base}")
    return manifests[0]


def run_restore(args: argparse.Namespace) -> dict[str, Any]:
    trash_root = Path(args.trash_dir).expanduser().resolve() if getattr(args, "trash_dir", None) else None
    if getattr(args, "latest", False):
        mpath = find_latest_manifest(trash_root)
    elif getattr(args, "manifest", None):
        cand = Path(args.manifest).expanduser().resolve()
        mpath = (cand / "manifest.json") if cand.is_dir() else cand
    else:
        raise ValueError("Specify a manifest.json path or pass --latest")
    if not mpath.is_file():
        raise FileNotFoundError(f"Manifest not found: {mpath}")
    data = json.loads(mpath.read_text(encoding="utf-8"))
    restored: list[str] = []
    skipped: list[dict[str, str]] = []
    for item in data.get("items", []):
        staged = item.get("staged_path")
        orig = item.get("original_path")
        if not staged or not orig:
            skipped.append({"target": str(orig), "reason": "non-filesystem action (e.g. simulator)"})
            continue
        sp = Path(staged)
        op = Path(orig)
        if not sp.exists():
            skipped.append({"target": orig, "reason": "staged item missing"})
            continue
        if op.exists():
            skipped.append({"target": orig, "reason": "destination already exists"})
            continue
        op.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(sp), str(op))
        restored.append(orig)
    return {
        "manifest": str(mpath),
        "restored_count": len(restored),
        "restored": restored,
        "skipped": skipped,
    }


# ==============================================================================
# Human-Readable Printers & CLI Entrypoint
# ==============================================================================
def print_audit_human(rep: dict[str, Any]) -> None:
    print("=" * 78)
    print(f"agent-governor v{VERSION} — Governance, Context Budget & File Norm Audit")
    print("=" * 78)
    g = rep["global_rules"]
    sync_badge = "IN SYNC" if g["in_sync"] else "DRIFT DETECTED"
    print(f"Global Rules Parity : {sync_badge}")
    for agent, info in g["files"].items():
        if info.get("present"):
            print(
                f"  - {agent:<8}: {info['lines']:>3} lines | {info['bytes']:>5} B | "
                f"~{info['estimated_tokens']:>4} tokens | sha256:{info['sha256_12']}"
            )
    sb = rep["skills_budget"]
    print(
        f"Skills Startup Tax  : {sb['installed_count']} skills (~{sb['startup_frontmatter_tokens_est']} frontmatter tokens)"
    )
    if rep["mcp_servers"]:
        mcp_str = ", ".join(f"{k}={v}" for k, v in rep["mcp_servers"].items())
        print(f"Configured MCPs     : {mcp_str}")
    print(f"Projects Audited    : {rep['projects_audited']} ({rep['projects_root']})")
    print("-" * 78)
    s = rep["summary"]
    if s["attention"]:
        print(f"Attention Required ({s['attention_count']}):")
        for idx, msg in enumerate(s["attention"], 1):
            print(f"  {idx}. {msg}")
    else:
        print("Attention Required  : 0 (All projects comply with 4-zone & pointer rules)")
    if s["advice"]:
        print(f"\nOptimization Advice ({s['advice_count']}):")
        for idx, msg in enumerate(s["advice"][:20], 1):
            print(f"  - {msg}")
        if len(s["advice"]) > 20:
            print(f"  ... and {len(s['advice']) - 20} more (use --json for full list)")


def print_gc_human(rep: dict[str, Any]) -> None:
    print("=" * 78)
    print(f"agent-governor v{VERSION} — 5-Gate TTL Garbage Collector ({rep['execution'].upper()} / {rep['mode']})")
    print("=" * 78)
    print(f"Total Candidates    : {rep['total_candidates']} ({rep['reclaimable_human']})")
    print(f"Protected by Gates  : {rep['protected_skips_count']} active/pinned items skipped")
    if rep["categories"]:
        print("-" * 78)
        for cat, stats in sorted(rep["categories"].items(), key=lambda kv: kv[1]["bytes"], reverse=True):
            print(f"  {cat:<28} : {stats['count']:>5} items | {format_bytes(stats['bytes']):>10}")
    if rep["manifest"]:
        print("-" * 78)
        print(f"Reversible Trash Manifest : {rep['manifest']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-governor",
        description="Multi-Agent File Norm Governance, Startup Context Optimizer & 5-Gate Ephemeral GC",
    )
    p.add_argument("--version", action="version", version=f"agent-governor {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    # audit
    pa = sub.add_parser("audit", help="Read-only audit of context budgets, 4-zone norms, pointers & root clutter")
    pa.add_argument("--root", default=None, help="Projects root or single project path")
    pa.add_argument("--home", default=None, help="Override HOME directory (for testing)")
    pa.add_argument("--max-global-lines", type=int, default=50)
    pa.add_argument("--max-global-bytes", type=int, default=4500)
    pa.add_argument("--max-project-lines", type=int, default=40)
    pa.add_argument("--max-project-bytes", type=int, default=3000)
    pa.add_argument("--strict", action="store_true", help="Exit 1 on attention items")
    pa.add_argument("--json", action="store_true", help="Output JSON report")

    # adopt
    pf = sub.add_parser("adopt", help="Initialize 4-zone + 3-tier work/ layout, heal pointers & relocate clutter")
    pf.add_argument("--root", default=None, help="Projects root or single project path")
    pf.add_argument("--home", default=None, help="Override HOME directory")
    pf.add_argument("--relocate-clutter", action="store_true", help="Trash empty root clutter & move non-empty root tmp/ into work/retained/")
    pf.add_argument("--trash-dir", default=None, help="Custom Trash staging root")
    pf.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    pf.add_argument("--json", action="store_true", help="Output JSON report")

    # scratch & test-dir
    for zone_cmd, default_ttl, help_txt in (
        ("scratch", 72.0, "Allocate a lease-stamped per-agent directory in work/_scratch/ (default 72h TTL)"),
        ("test-dir", 168.0, "Allocate a lease-stamped per-agent directory in work/_tests/ (default 168h TTL)"),
    ):
        ps = sub.add_parser(zone_cmd, help=help_txt)
        ps.add_argument("--project", default=None, help="Project directory (auto-inferred from cwd if omitted)")
        ps.add_argument("--agent", default=None, help="Agent identifier (e.g. claude, codex, gemini)")
        ps.add_argument("--topic", "--slug", dest="topic", required=True, help="Short task/topic slug")
        ps.add_argument("--ttl-hours", type=float, default=default_ttl, help=f"Retention TTL in hours (default {default_ttl:g})")
        ps.add_argument("--ttl-days", type=float, default=None, help="Optional TTL override in days")
        ps.add_argument("--unique", action="store_true", help="Append HHMMSS suffix if same-day directory already exists")

    # gc
    pg = sub.add_parser("gc", help="Run 5-gate TTL garbage collection into ~/.Trash with reversible manifest")
    pg.add_argument("--root", default=None, help="Projects root or single project path")
    pg.add_argument("--home", default=None, help="Override HOME directory")
    pg.add_argument("--mode", choices=("daily", "session-end", "deep"), default="daily")
    pg.add_argument("--scratch-ttl-days", type=float, default=3.0, help="TTL days for work/_scratch/* (default 3)")
    pg.add_argument("--tests-ttl-days", type=float, default=7.0, help="TTL days for work/_tests/* (default 7)")
    pg.add_argument("--cache-ttl-days", type=float, default=7.0, help="TTL days for shell-snapshots/paste-cache (default 7)")
    pg.add_argument("--tmp-ttl-hours", type=float, default=24.0, help="TTL hours for agent .tmp/* dirs (default 24)")
    pg.add_argument("--sim-ttl-days", type=float, default=3.0, help="TTL days for idle ephemeral test simulators (default 3)")
    pg.add_argument("--keep-backups", type=int, default=2, help="Newest config .bak files to keep per config (default 2, -1 to disable)")
    pg.add_argument("--min-idle-hours", type=float, default=2.0, help="Minimum idle hours before daily GC touches an item (default 2)")
    pg.add_argument("--workspace-only", action="store_true", help="Only scan project work/_scratch & work/_tests, skip ~/.claude & ~/.codex")
    pg.add_argument("--skip-simulators", action="store_true", help="Skip iOS Simulator checks")
    pg.add_argument("--trash-dir", default=None, help="Custom Trash staging root")
    pg.add_argument("--apply", action="store_true", help="Move expired items to Trash with manifest (default is dry-run)")
    pg.add_argument("--json", action="store_true", help="Output JSON report")

    # triage
    pt = sub.add_parser("triage", help="Read-only inventory & iteration grouping of grandfathered work/* folders")
    pt.add_argument("--root", default=None, help="Projects root or single project path")
    pt.add_argument("--home", default=None, help="Override HOME directory")
    pt.add_argument("--markdown", "--output", dest="markdown", default=None, help="Optional path to write Markdown triage report")
    pt.add_argument("--json", action="store_true", help="Output JSON report")

    # restore
    pr = sub.add_parser("restore", help="Atomically restore staged items from a manifest.json receipt")
    pr.add_argument("manifest", nargs="?", default=None, help="Path to manifest.json inside ~/.Trash/agent-governor-*/")
    pr.add_argument("--batch", dest="manifest", default=None, help="Alias path to batch directory or manifest.json")
    pr.add_argument("--latest", action="store_true", help="Restore the most recent ~/.Trash/agent-governor-* manifest")
    pr.add_argument("--trash-dir", default=None, help="Custom Trash staging root")
    pr.add_argument("--apply", action="store_true", default=True, help="Execute restore (accepted for CLI symmetry)")
    pr.add_argument("--json", action="store_true", help="Output JSON report")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0

    if args.cmd == "audit":
        rep = run_audit(args)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print_audit_human(rep)
        return 1 if (args.strict and not rep["summary"]["healthy"]) else 0

    if args.cmd == "adopt":
        rep = run_adopt(args)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print(f"[{rep['mode'].upper()}] {rep['action_count']} governance action(s) across {rep['projects_root']}")
            for a in rep["actions"]:
                target = a.get("path") or f"{a.get('from')} -> {a.get('to')}"
                print(f"  - [{a['project']}] {a['action']}: {target}")
            if rep["manifest"]:
                print(f"  Receipt: {rep['manifest']}")
        return 0

    if args.cmd in ("scratch", "test-dir"):
        zone = "scratch" if args.cmd == "scratch" else "tests"
        allocated = allocate_ephemeral_dir(zone, args)
        print(str(allocated))
        return 0

    if args.cmd == "gc":
        rep = run_gc(args)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print_gc_human(rep)
        return 1 if rep["errors"] else 0

    if args.cmd == "triage":
        rep = run_triage(args)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print(
                f"Grandfathered Legacy work/ Inventory: {rep['total_legacy_items']} folders "
                f"({rep['total_legacy_human']}) across {len(rep['projects'])} project(s)"
            )
            for ps in rep["projects"][:15]:
                print(
                    f"  - {ps['project']:<20}: {ps['legacy_human']:>10} ({ps['legacy_count']} folders, "
                    f"{len(ps['iterative_families'])} multi-run series)"
                )
            if args.markdown:
                print(f"Markdown report written to: {args.markdown}")
        return 0

    if args.cmd == "restore":
        rep = run_restore(args)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print(f"Restored {rep['restored_count']} item(s) from {rep['manifest']}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
