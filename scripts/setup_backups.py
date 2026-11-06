#!/usr/bin/env python3
"""Universal Automated Backup & Storage Watchdog Installer (setup_backups.py).

Installs and schedules the 7-job automated backup & file management suite:
1. volume-guard    (Every 60s)        : External SSD hardware UUID & USB session watchdog (or internal-mode check)
2. backup-hot      (Every 60m)        : Hourly Restic hot backup of internal-disk AI Agent states, memories & profiles
3. icloud-package  (Daily 02:30)      : Nightly AES-256-CBC encrypted compact package + APFS CoW SQLite snapshots to iCloud Drive
4. backup-projects (Daily 03:00)      : Daily Restic backup of ${AI_VOLUME_PATH}/AI (Projects, Archive, Data)
5. backup-media    (Sun 04:00)        : Weekly Restic backup of media/work directory (with bandwidth cap)
6. maintenance     (Sun 05:30)        : Weekly Restic retention prune & integrity check (monthly 5% raw readback)
7. restore-drill   (Sun 07:00)        : Weekly automated disaster-recovery restore verification drill
"""
import argparse
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import socket
import subprocess
import sys


def ensure_keychain_secret(service: str, account: str, apply: bool) -> str:
    try:
        r = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return "preserved_existing"
    except Exception:
        pass

    if not apply:
        return "would_create"

    new_secret = secrets.token_urlsafe(32)
    try:
        r = subprocess.run(
            ["/usr/bin/security", "add-generic-password", "-U", "-a", account, "-s", service, "-w", new_secret],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "created" if r.returncode == 0 else "skipped"
    except Exception:
        return "skipped"


def detect_label_prefix(launch_agents_dir: Path, user: str) -> str:
    """Preserve existing `local.<user>.*` labels if already installed on this Mac so we never create duplicates;
    otherwise use `local.orca` on fresh installations."""
    user_candidate = f"local.{user}"
    if (launch_agents_dir / f"{user_candidate}.icloud-package.plist").exists():
        return user_candidate
    for p in launch_agents_dir.glob("local.*.icloud-package.plist"):
        return p.name.replace(".icloud-package.plist", "")
    return "local.orca"


def build_launchd_specs(prefix: str, agents_dir: Path, logs_dir: Path) -> list[tuple[str, dict]]:
    def base_dict(label: str, argv: list[str], schedule: dict) -> dict:
        log_file = str(logs_dir / f"launchd-{label}.log")
        d = {
            "Label": label,
            "ProgramArguments": argv,
            "RunAtLoad": False,
            "Nice": 10,
            "LowPriorityIO": True,
            "ProcessType": "Background",
            "StandardOutPath": log_file,
            "StandardErrorPath": log_file,
        }
        d.update(schedule)
        return d

    zsh = "/bin/zsh"
    restic_sh = str(agents_dir / "backup-restic.sh")
    return [
        (
            f"{prefix}.volume-guard",
            base_dict(
                f"{prefix}.volume-guard",
                [zsh, str(agents_dir / "volume-guard.sh")],
                {"StartInterval": 60},
            ),
        ),
        (
            f"{prefix}.backup-hot",
            base_dict(
                f"{prefix}.backup-hot",
                [zsh, restic_sh, "hot"],
                {"StartInterval": 3600},
            ),
        ),
        (
            f"{prefix}.icloud-package",
            base_dict(
                f"{prefix}.icloud-package",
                [zsh, str(agents_dir / "icloud-package.sh")],
                {"StartCalendarInterval": {"Hour": 2, "Minute": 30}},
            ),
        ),
        (
            f"{prefix}.backup-projects",
            base_dict(
                f"{prefix}.backup-projects",
                [zsh, restic_sh, "projects"],
                {"StartCalendarInterval": {"Hour": 3, "Minute": 0}},
            ),
        ),
        (
            f"{prefix}.backup-media",
            base_dict(
                f"{prefix}.backup-media",
                [zsh, restic_sh, "media"],
                {"StartCalendarInterval": {"Weekday": 0, "Hour": 4, "Minute": 0}},
            ),
        ),
        (
            f"{prefix}.maintenance",
            base_dict(
                f"{prefix}.maintenance",
                [zsh, str(agents_dir / "maintenance.sh")],
                {"StartCalendarInterval": {"Weekday": 0, "Hour": 5, "Minute": 30}},
            ),
        ),
        (
            f"{prefix}.restore-drill",
            base_dict(
                f"{prefix}.restore-drill",
                [zsh, str(agents_dir / "restore-drill.sh")],
                {"StartCalendarInterval": {"Weekday": 0, "Hour": 7, "Minute": 0}},
            ),
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply backup suite installation & LaunchAgents")
    parser.add_argument("--dry-run", action="store_true", help="Preview installation without writing files")
    args = parser.parse_args()
    apply = args.apply and not args.dry_run

    home = Path.home()
    user = os.environ.get("USER") or home.name
    uid = os.getuid()
    repo_root = Path(__file__).resolve().parent.parent
    src_scripts_dir = repo_root / "scripts/codex-storage"

    storage_root = home / "Library/Application Support/Codex Storage"
    agents_dir = storage_root / "agents"
    launchd_backup_dir = agents_dir / "launchd"
    status_dir = storage_root / "status"
    logs_dir = storage_root / "logs"
    launch_agents_dir = home / "Library/LaunchAgents"
    orca_cfg_dir = home / ".orca"
    backup_env_file = orca_cfg_dir / "backup-config.env"

    storage_mode = os.environ.get("STORAGE_MODE", "auto")
    vol_path = os.environ.get("AI_VOLUME_PATH", str(home / "AI-Workspace"))
    vol_uuid = os.environ.get("AI_VOLUME_UUID", "AUTO")
    if storage_mode == "internal" or not str(vol_path).startswith("/Volumes/"):
        storage_mode = "internal"
        vol_uuid = "LOCAL"
    else:
        storage_mode = "external"

    icloud_dir = os.environ.get(
        "BACKUP_ICLOUD_DIR",
        str(home / "Library/Mobile Documents/com~apple~CloudDocs/AI备份/每晚小包"),
    )
    restic_repo = os.environ.get("BACKUP_RESTIC_REPO", f"{vol_path}/AI/Backups/restic")
    nas_mount = os.environ.get("BACKUP_NAS_MOUNT", "")
    nas_smb_match = os.environ.get("BACKUP_NAS_SMB_MATCH", "")
    media_path = os.environ.get("BACKUP_MEDIA_PATH", f"{vol_path}/Work")
    host_tag = os.environ.get("BACKUP_HOST_TAG") or socket.gethostname().split(".")[0] or "Mac"
    extra_paths = os.environ.get("EXTRA_MIGRATION_PATHS", "")

    prefix = detect_label_prefix(launch_agents_dir, user)
    specs = build_launchd_specs(prefix, agents_dir, logs_dir)

    script_files = sorted(p.name for p in src_scripts_dir.iterdir() if p.is_file()) if src_scripts_dir.is_dir() else []

    if apply:
        for d in (agents_dir, launchd_backup_dir, status_dir, logs_dir, launch_agents_dir, orca_cfg_dir):
            d.mkdir(parents=True, exist_ok=True)

        # 1. Write ~/.orca/backup-config.env (mode 0600)
        env_lines = [
            "# Auto-generated by setup_backups.py — DO NOT COMMIT",
            f'export STORAGE_MODE="{storage_mode}"',
            f'export AI_VOLUME_PATH="{vol_path}"',
            f'export AI_VOLUME_UUID="{vol_uuid}"',
            f'export BACKUP_ICLOUD_DIR="{icloud_dir}"',
            f'export BACKUP_RESTIC_REPO="{restic_repo}"',
            f'export BACKUP_NAS_MOUNT="{nas_mount}"',
            f'export BACKUP_NAS_SMB_MATCH="{nas_smb_match}"',
            f'export BACKUP_MEDIA_PATH="{media_path}"',
            f'export BACKUP_HOST_TAG="{host_tag}"',
            f'export BACKUP_KEYCHAIN_ACCOUNT="{user}"',
            'export BACKUP_KEYCHAIN_ICLOUD="icloud-package-key"',
            'export BACKUP_KEYCHAIN_RESTIC="restic-mac-agents"',
            f'export EXTRA_MIGRATION_PATHS="{extra_paths}"',
        ]
        backup_env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        backup_env_file.chmod(0o600)

        # 2. Install scripts into ~/Library/Application Support/Codex Storage/agents/
        for fname in script_files:
            src_f = src_scripts_dir / fname
            dst_f = agents_dir / fname
            shutil.copy2(src_f, dst_f)
            dst_f.chmod(0o755)

        # 3. Write and bootstrap LaunchAgents
        for label, pl_dict in specs:
            for target_dir in (launchd_backup_dir, launch_agents_dir):
                pl_path = target_dir / f"{label}.plist"
                with pl_path.open("wb") as f:
                    plistlib.dump(pl_dict, f, sort_keys=False)
                pl_path.chmod(0o644)
            live_plist = launch_agents_dir / f"{label}.plist"
            subprocess.run(
                ["/bin/launchctl", "bootstrap", f"gui/{uid}", str(live_plist)],
                capture_output=True,
                timeout=5,
            )

        # 4. Run volume-guard.sh and status.sh once to initialize status dashboard
        subprocess.run(["/bin/zsh", str(agents_dir / "volume-guard.sh")], capture_output=True, timeout=15)
        subprocess.run(["/bin/zsh", str(agents_dir / "status.sh")], capture_output=True, timeout=10)

    kc_icloud = ensure_keychain_secret("icloud-package-key", user, apply=apply)
    kc_restic = ensure_keychain_secret("restic-mac-agents", user, apply=apply)

    result = {
        "mode": "apply" if apply else "dry-run",
        "storage_mode": storage_mode,
        "workspace_root": vol_path,
        "volume_uuid": vol_uuid,
        "backup_config_env": str(backup_env_file),
        "agents_dir": str(agents_dir),
        "installed_scripts": script_files,
        "launchd_label_prefix": prefix,
        "scheduled_jobs": [label for label, _ in specs],
        "keychain_services": {
            "icloud-package-key": kc_icloud,
            "restic-mac-agents": kc_restic,
        },
        "status_dashboard": str(storage_root / "backup-status.md"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
