#!/usr/bin/env python3
"""Generate a deterministic, token-compact cross-agent HANDOFF packet (JSON + Markdown).

Captures git state, branch, base/head SHA, changed files with SHA-256 digests,
untracked files, and verification receipts without dumping noisy conversation logs.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys

import os
VOLUME = Path(os.environ.get("AI_VOLUME_PATH", "/Volumes/AI_Workspace"))
UUID = os.environ.get("AI_VOLUME_UUID", "AUTO")


def run_git(repo: Path, *args: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_packet(repo: Path, goal: str, non_goals: list[str], receipts: list[str], base_ref: str) -> dict:
    repo = repo.resolve()
    git_root = run_git(repo, "rev-parse", "--show-toplevel")
    is_git = bool(git_root)
    root_path = Path(git_root) if is_git else repo

    branch = run_git(root_path, "rev-parse", "--abbrev-ref", "HEAD") if is_git else "non-git"
    head_sha = run_git(root_path, "rev-parse", "HEAD") if is_git else "none"
    base_sha = run_git(root_path, "merge-base", base_ref, "HEAD") if is_git else "none"
    status_lines = [
        line for line in (run_git(root_path, "status", "--porcelain=v1").splitlines() if is_git else []) if line.strip()
    ]

    changed_files = []
    if is_git:
        diff_names = run_git(root_path, "diff", "--name-only", base_sha if base_sha != "none" else "HEAD").splitlines()
        staged_names = run_git(root_path, "diff", "--cached", "--name-only").splitlines()
        all_names = sorted({n.strip() for n in (diff_names + staged_names) if n.strip()})
        for rel in all_names[:200]:
            fp = root_path / rel
            changed_files.append(
                {
                    "path": rel,
                    "exists": fp.is_file(),
                    "size_bytes": fp.stat().st_size if fp.is_file() else None,
                    "sha256": sha256_file(fp) if fp.is_file() else None,
                }
            )

    verified_receipts = []
    for r_str in receipts:
        rp = Path(r_str).expanduser()
        if not rp.is_absolute():
            rp = (repo / rp).resolve()
        verified_receipts.append(
            {
                "path": str(rp),
                "exists": rp.is_file(),
                "size_bytes": rp.stat().st_size if rp.is_file() else None,
                "sha256": sha256_file(rp) if rp.is_file() else None,
            }
        )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace_path": str(repo),
        "git_root": str(root_path),
        "is_git_repo": is_git,
        "branch": branch,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "working_tree_clean": len(status_lines) == 0,
        "dirty_entries_count": len(status_lines),
        "dirty_entries": status_lines[:100],
        "goal": goal,
        "non_goals": non_goals,
        "changed_files_count": len(changed_files),
        "changed_files": changed_files,
        "receipts": verified_receipts,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=".", help="Target workspace or git worktree path")
    p.add_argument("--goal", default="Cross-agent task handoff snapshot", help="Task goal summary")
    p.add_argument("--non-goal", action="append", default=[], help="Explicit non-goals / unverified boundaries")
    p.add_argument("--receipt", action="append", default=[], help="Verification receipt file paths to hash")
    p.add_argument("--base", default="main", help="Base branch/ref for diff calculation (default: main)")
    p.add_argument("--output", help="Optional JSON output path inside workspace volume")
    args = p.parse_args()

    packet = build_packet(Path(args.repo), args.goal, args.non_goal, args.receipt, args.base)
    payload = json.dumps(packet, ensure_ascii=False, indent=2) + "\n"

    if args.output:
        if str(VOLUME).startswith("/Volumes/") and UUID and UUID != "AUTO":
            info = subprocess.check_output(["diskutil", "info", "-plist", str(VOLUME)], timeout=10)
            if plistlib.loads(info).get("VolumeUUID") != UUID:
                raise RuntimeError("Wrong or missing workspace volume")
        out_path = Path(args.output).resolve()
        if VOLUME.resolve() not in out_path.parents:
            raise ValueError("Output must reside inside AI_VOLUME_PATH")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")

    print(payload, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
