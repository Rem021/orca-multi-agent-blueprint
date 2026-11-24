#!/usr/bin/env python3
"""Backup, storage watchdog and hygiene installer (setup_backups.py).

Installs and schedules eight LaunchAgents:
1. volume-guard    (every 60 s; 10 min in internal mode) : workspace volume UUID & USB session watchdog
2. backup-hot      (hourly)       : Restic backup of the agent state on the internal disk
3. icloud-package  (daily 02:30)  : encrypted, authenticated compact package of the agent state to iCloud Drive
4. backup-projects (daily 03:00)  : Restic backup of ${AI_VOLUME_PATH}/AI (Projects, Archive, Data)
5. backup-media    (Sun 04:00)    : Restic backup of the media folder (bandwidth-capped)
6. maintenance     (Sun 05:30)    : Restic retention, prune and integrity check (monthly 5 % read-back)
7. restore-drill   (Sun 07:00)    : restore drill of the newest hot snapshot
8. agent-hygiene   (hourly; GC every 4 h) : iCloud upload poll + rolling prune, TTL cache GC

Settings come from the environment (deploy.sh exports config.env), then config.env, then the previous
~/.orca/backup-config.env. Both files are evaluated with zsh, so `${VAR:-default}` and `$HOME` forms
resolve exactly as they do for deploy.sh; a value that would still contain an unexpanded `${...}` is
never written to backup-config.env.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time


def keychain_add(service: str, account: str, password: str) -> bool:
    """Add or update a generic password without putting the secret on a command line.

    `security -i` reads the command from stdin and `-X` takes the secret hex-encoded, so the value
    never shows up in `ps` and needs no shell or `security` quoting."""

    def quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    if any(ch in service + account for ch in "\r\n"):
        return False
    command = (
        f"add-generic-password -U -a {quoted(account)} -s {quoted(service)} "
        f"-X {password.encode('utf-8').hex()}\n"
    )
    try:
        r = subprocess.run(["/usr/bin/security", "-i"], input=command, capture_output=True, text=True, timeout=10)
    except Exception:
        return False
    return r.returncode == 0


def local_host_name() -> str:
    """Stable per-machine name: LocalHostName does not change with the network the Mac is on."""
    try:
        r = subprocess.run(["/usr/sbin/scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return socket.gethostname().split(".")[0] or "Mac"


def machine_id() -> str:
    """Short stable id of this Mac (hash of the hardware UUID); "" when it cannot be read.

    Hostnames cannot tell two Macs apart: a new Mac often gets the same default name as the old one."""
    try:
        out = subprocess.run(
            ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return ""
    m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
    return hashlib.sha256(m.group(1).encode("utf-8")).hexdigest()[:12] if m else ""


def resolve_host_tag(
    configured: str,
    current: str,
    migrated_from: str = "",
    keep: bool = False,
    source_machine: str = "",
    this_machine: str = "",
    suffix: str = "",
) -> tuple[str, str]:
    """Return (host tag, where it came from).

    The tag names this machine's Restic snapshots and iCloud packages. A tag restored from another
    Mac's snapshot (it equals that Mac's hostname) is replaced by this Mac's own name, otherwise both
    machines publish under one name and prune each other's history. When both Macs carry the same
    name, a short machine suffix keeps them apart. BACKUP_HOST_TAG_KEEP=true opts out.

    `suffix` (a short machine id) is appended whenever the tag is derived rather than configured, so
    two Macs that happen to share a LocalHostName never share a tag. A configured tag is kept as is."""
    configured = configured.strip()
    source = migrated_from.split(".")[0].strip()
    other_machine = bool(source_machine and this_machine and source_machine != this_machine)
    inherited = bool(source) and configured == source and not keep and (current != source or other_machine)
    if configured and not inherited:
        return configured, "configured (BACKUP_HOST_TAG)"
    tag = current
    why = (
        f"re-derived: configured tag '{configured}' was inherited from the source Mac of the migration"
        if configured
        else "derived from this Mac's LocalHostName"
    )
    if suffix and not tag.endswith(f"-{suffix}"):
        tag = f"{current}-{suffix}"
        why += " plus a short machine id"
    if other_machine and tag == source and not keep:
        tag = f"{current}-{this_machine[:6]}"
        why += "; machine suffix added because the source Mac has the same name"
    return tag, why


ICLOUD_DRIVE = "Library/Mobile Documents/com~apple~CloudDocs"


def volume_root(path: str) -> str:
    """/Volumes/<name> for a folder inside a volume (AI_VOLUME_PATH may name one), else `path`."""
    parts = Path(path).parts
    return str(Path(*parts[:3])) if len(parts) >= 3 and parts[:2] == ("/", "Volumes") else path


def default_icloud_dir(home: Path) -> Path:
    """New installs publish to `Agent Backups/Nightly`; one that already uses the legacy folder keeps it."""
    legacy = home / ICLOUD_DRIVE / "AI备份/每晚小包"
    return legacy if legacy.is_dir() else home / ICLOUD_DRIVE / "Agent Backups/Nightly"


_ASSIGNMENT = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=", re.M)
_ENV_SPLIT = "--orca-setup-backups-env--"


def _unexpanded(value: str) -> bool:
    return "${" in value or "$(" in value or "`" in value


def read_env_file(path: Path, home: Path) -> dict[str, str]:
    """Return the variables an env file assigns, evaluated by zsh exactly as deploy.sh sources it.

    Only keys that the file assigns are returned (with their value after evaluation, "" when the file
    clears them). Falls back to a plain parser when zsh is unavailable; that parser expands only
    `$HOME`/`${HOME}` and drops any value that still holds an unexpanded `${...}`, `$(...)` or backtick."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    keys = list(dict.fromkeys(_ASSIGNMENT.findall(text)))
    if not keys:
        return {}
    script = f'set -a; source -- "$1" >/dev/null 2>&1 </dev/null; set +a; print -rn -- "{_ENV_SPLIT}"; env -0'
    try:
        r = subprocess.run(
            ["/bin/zsh", "-f", "-c", script, "zsh", str(path)],
            capture_output=True,
            timeout=15,
            env={**os.environ, "HOME": str(home)},
        )
        dump = r.stdout.decode("utf-8", errors="replace")
        if _ENV_SPLIT not in dump:
            raise ValueError("zsh did not finish evaluating the file")
        evaluated: dict[str, str] = {}
        for entry in dump.split(_ENV_SPLIT, 1)[1].split("\0"):
            name, sep, value = entry.partition("=")
            if sep:
                evaluated[name] = value
        return {k: evaluated.get(k, "") for k in keys}
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'").replace("${HOME}", str(home)).replace("$HOME", str(home))
        if not _unexpanded(v):
            values[k.strip()] = v
    return values


def shell_dq(value: str) -> str:
    """Escape a value for use inside double quotes in the sourced backup-config.env."""
    out = value.replace("\\", "\\\\")
    for ch in ('"', "$", "`"):
        out = out.replace(ch, "\\" + ch)
    return out.replace("\n", " ").replace("\r", " ")


def write_private_file(path: Path, text: str) -> None:
    """Atomically replace `path`; the file is never readable by other users, not even briefly."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_launch_agent(uid: int, label: str, plist: Path, changed: bool) -> str:
    """(Re)load one LaunchAgent. Returns "" on success, otherwise the launchctl error."""
    target = f"gui/{uid}/{label}"

    def loaded() -> bool:
        try:
            return subprocess.run(["/bin/launchctl", "print", target], capture_output=True, timeout=10).returncode == 0
        except Exception:
            return False

    if loaded():
        if not changed:
            return ""  # same definition already loaded: do not interrupt a job that may be running
        subprocess.run(["/bin/launchctl", "bootout", target], capture_output=True, timeout=20)
    last_error = ""
    for attempt in range(5):  # bootout is asynchronous; bootstrap fails with EIO until the old job is gone
        try:
            r = subprocess.run(
                ["/bin/launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, text=True, timeout=20
            )
        except Exception as exc:
            last_error = str(exc)
        else:
            if r.returncode == 0 or loaded():
                return ""
            last_error = (r.stderr or r.stdout).strip() or f"launchctl bootstrap exited {r.returncode}"
        time.sleep(0.5 * (attempt + 1))
    return last_error


def ensure_keychain_secret(
    service: str,
    account: str,
    apply: bool,
    init_new_secrets: bool = False,
    restic_repo: str = "",
    migration_secrets_file: Path | None = None,
) -> str:
    for cmd in (
        ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
        ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return "preserved_existing"
        except Exception:
            pass

    # If a migration snapshot has keychain-secrets.json with this service, import it instead of generating a new secret
    if migration_secrets_file and migration_secrets_file.is_file():
        try:
            raw_secrets = json.loads(migration_secrets_file.read_text(encoding="utf-8"))
            entry = raw_secrets.get(service)
            pw = entry.get("password") if isinstance(entry, dict) else (entry if isinstance(entry, str) else None)
            acct = (entry.get("account") if isinstance(entry, dict) else None) or account
            if pw:
                if not apply:
                    return "would_import_from_migration"
                if keychain_add(service, acct, pw):
                    return "imported_from_migration"
        except Exception:
            pass

    # Fail-closed guard (B2): never silently generate a conflicting secret if an existing Restic repo or migration marker is present
    repo_exists = bool(restic_repo and (Path(restic_repo) / "config").is_file())
    migrated_marker = (Path.home() / ".orca/.migrated-from").exists()
    if (repo_exists or migrated_marker) and not init_new_secrets:
        return "missing_fail_closed"

    if not apply:
        return "would_create"

    return "created" if keychain_add(service, account, secrets.token_urlsafe(32)) else "skipped"


def detect_label_prefix(launch_agents_dir: Path, user: str) -> str:
    """Preserve existing `local.<user>.*` labels if already installed on this Mac so we never create duplicates;
    otherwise use `local.orca` on fresh installations."""
    user_candidate = f"local.{user}"
    if (launch_agents_dir / f"{user_candidate}.icloud-package.plist").exists():
        return user_candidate
    for p in launch_agents_dir.glob("local.*.icloud-package.plist"):
        return p.name.replace(".icloud-package.plist", "")
    return "local.orca"


def build_launchd_specs(prefix: str, agents_dir: Path, logs_dir: Path, storage_mode: str = "external") -> list[tuple[str, dict]]:
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
                {"StartInterval": 600 if storage_mode == "internal" else 60},
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
                {"StartCalendarInterval": {"Hour": 2, "Minute": 30}, "AbandonProcessGroup": True},
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
        (
            f"{prefix}.agent-hygiene",
            base_dict(
                f"{prefix}.agent-hygiene",
                [zsh, str(agents_dir / "agent-hygiene.sh")],
                {"StartInterval": 3600, "RunAtLoad": True},
            ),
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply backup suite installation & LaunchAgents")
    parser.add_argument("--dry-run", action="store_true", help="Preview installation without writing files")
    parser.add_argument(
        "--init-new-secrets",
        action="store_true",
        help="Explicitly allow generating new Keychain secrets even if an existing Restic repo is present",
    )
    args = parser.parse_args()
    apply = args.apply and not args.dry_run

    home = Path.home()
    user = os.environ.get("USER") or home.name
    uid = os.getuid()
    repo_root = Path(__file__).resolve().parent.parent
    src_scripts_dir = repo_root / "scripts/codex-storage"

    storage_root = home / "Library/Application Support/Codex Storage"
    agents_dir = storage_root / "agents"
    bin_dir = storage_root / "bin"
    launchd_backup_dir = agents_dir / "launchd"
    status_dir = storage_root / "status"
    logs_dir = storage_root / "logs"
    launch_agents_dir = home / "Library/LaunchAgents"
    orca_cfg_dir = home / ".orca"
    local_bin_dir = home / ".local/bin"
    backup_env_file = orca_cfg_dir / "backup-config.env"

    # The previous backup-config.env supplies values (non-empty only); config.env overrides it,
    # including deliberate clears
    cfg_fallback: dict[str, str] = {
        k: v for k, v in (read_env_file(backup_env_file, home) if backup_env_file.is_file() else {}).items() if v
    }
    config_env = Path(os.environ.get("ORCA_CONFIG_FILE") or repo_root / "config.env")
    if config_env.is_file():
        cfg_fallback.update(read_env_file(config_env, home))

    def get_cfg(key: str, default: str = "") -> str:
        val = os.environ.get(key, "").strip()
        if val:
            val = val.replace("${HOME}", str(home)).replace("$HOME", str(home))
            if not _unexpanded(val):
                return val
        val = cfg_fallback.get(key, default)
        return default if _unexpanded(val) else val

    storage_mode = get_cfg("STORAGE_MODE", "auto")
    vol_path = get_cfg("AI_VOLUME_PATH", str(home / "AI-Workspace"))
    vol_uuid = get_cfg("AI_VOLUME_UUID", "AUTO")
    if storage_mode == "internal" or not str(vol_path).startswith("/Volumes/"):
        storage_mode = "internal"
        vol_uuid = "LOCAL"
    elif vol_uuid == "AUTO" and os.path.ismount(volume_root(str(vol_path))):
        try:
            d_out = subprocess.check_output(
                ["/usr/sbin/diskutil", "info", "-plist", volume_root(str(vol_path))],
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            d_info = plistlib.loads(d_out)
            detected_uuid = str(d_info.get("VolumeUUID", "") or "").strip()
            if detected_uuid:
                vol_uuid = detected_uuid
        except Exception:
            pass
    else:
        storage_mode = "external"

    icloud_dir = get_cfg("BACKUP_ICLOUD_DIR") or str(default_icloud_dir(home))
    icloud_keep_latest = get_cfg("BACKUP_ICLOUD_KEEP_LATEST", "1")
    restic_repo = get_cfg("BACKUP_RESTIC_REPO", f"{vol_path}/AI/Backups/restic")
    nas_mount = get_cfg("BACKUP_NAS_MOUNT", "")
    nas_smb_match = get_cfg("BACKUP_NAS_SMB_MATCH", "")
    media_path = get_cfg("BACKUP_MEDIA_PATH", f"{vol_path}/Work")
    migrated_from = source_machine = ""
    try:
        marker = json.loads((orca_cfg_dir / ".migrated-from").read_text(encoding="utf-8"))
        migrated_from = str(marker.get("source_local_hostname") or marker.get("source_hostname") or "")
        source_machine = str(marker.get("source_machine_id") or "")
    except (OSError, ValueError, AttributeError):
        pass
    this_machine = machine_id()
    host_tag, host_tag_source = resolve_host_tag(
        get_cfg("BACKUP_HOST_TAG"),
        local_host_name(),
        migrated_from,
        keep=get_cfg("BACKUP_HOST_TAG_KEEP").lower() in ("1", "true", "yes"),
        source_machine=source_machine,
        this_machine=this_machine if source_machine else "",
        suffix=this_machine[:6],
    )
    kc_account = get_cfg("BACKUP_KEYCHAIN_ACCOUNT", user) or user
    kc_icloud_svc = get_cfg("BACKUP_KEYCHAIN_ICLOUD", "icloud-package-key") or "icloud-package-key"
    kc_restic_svc = get_cfg("BACKUP_KEYCHAIN_RESTIC", "restic-mac-agents") or "restic-mac-agents"
    # Optional tuning keys are written only when set, so lib.sh defaults stay in one place
    optional_cfg = {
        key: get_cfg(key)
        for key in (
            "BACKUP_NAS_AUTOMOUNT",
            "BACKUP_MAINTENANCE_RETRY_LOCK",
            "BACKUP_RESTIC_RETRY_LOCK",
            "BACKUP_STALE_ALERT_HOURS",
            "BACKUP_NOTIFY",
            "BACKUP_ICLOUD_MAX_AGE_DAYS",
            "BACKUP_ICLOUD_PRUNE_SCOPE",
            "BACKUP_ICLOUD_STALE_HOST_DAYS",
            "BACKUP_ICLOUD_PRUNE_INTERVAL_SEC",
        )
        if get_cfg(key)
    }
    raw_extra_paths = get_cfg("EXTRA_MIGRATION_PATHS", "")
    extra_paths = ",".join(
        p.strip()
        for p in raw_extra_paths.split(",")
        if p.strip() and not p.strip().startswith("Library/Group Containers")
    )
    raw_restic_only_paths = get_cfg("EXTRA_RESTIC_PATHS", "")
    extra_restic_paths = ",".join(
        p.strip()
        for p in raw_restic_only_paths.split(",")
        if p.strip() and not p.strip().startswith("Library/Group Containers")
    )
    if extra_restic_paths:
        optional_cfg["EXTRA_RESTIC_PATHS"] = extra_restic_paths

    prefix = detect_label_prefix(launch_agents_dir, user)
    specs = build_launchd_specs(prefix, agents_dir, logs_dir, storage_mode=storage_mode)

    script_files = (
        sorted(p.name for p in src_scripts_dir.iterdir() if p.is_file() and not p.name.endswith(".swift"))
        if src_scripts_dir.is_dir()
        else []
    )

    mig_secrets_file = Path(vol_path) / "AI/Migration/latest/metadata/keychain-secrets.json"
    kc_icloud = ensure_keychain_secret(
        kc_icloud_svc,
        kc_account,
        apply=apply,
        init_new_secrets=args.init_new_secrets,
        restic_repo=restic_repo,
        migration_secrets_file=mig_secrets_file,
    )
    kc_restic = ensure_keychain_secret(
        kc_restic_svc,
        kc_account,
        apply=apply,
        init_new_secrets=args.init_new_secrets,
        restic_repo=restic_repo,
        migration_secrets_file=mig_secrets_file,
    )
    keychain_report = {kc_icloud_svc: kc_icloud, kc_restic_svc: kc_restic}
    warnings: list[str] = []

    if apply and "missing_fail_closed" in (kc_icloud, kc_restic):
        print(
            json.dumps(
                {
                    "error": "Keychain secret missing while existing Restic repository or migration state is present. "
                    "Import old Mac secrets first (`security add-generic-password`) or pass `--init-new-secrets`.",
                    "keychain_services": keychain_report,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    launchd_errors: dict[str, str] = {}
    if apply:
        for d in (agents_dir, bin_dir, launchd_backup_dir, status_dir, logs_dir, launch_agents_dir, orca_cfg_dir, local_bin_dir):
            d.mkdir(parents=True, exist_ok=True)

        # 1. Write ~/.orca/backup-config.env (mode 0600)
        env_values = {
            "STORAGE_MODE": storage_mode,
            "AI_VOLUME_PATH": vol_path,
            "AI_VOLUME_UUID": vol_uuid,
            "BACKUP_ICLOUD_DIR": icloud_dir,
            "BACKUP_ICLOUD_KEEP_LATEST": icloud_keep_latest,
            "BACKUP_RESTIC_REPO": restic_repo,
            "BACKUP_NAS_MOUNT": nas_mount,
            "BACKUP_NAS_SMB_MATCH": nas_smb_match,
            "BACKUP_MEDIA_PATH": media_path,
            "BACKUP_HOST_TAG": host_tag,
            "BACKUP_KEYCHAIN_ACCOUNT": kc_account,
            "BACKUP_KEYCHAIN_ICLOUD": kc_icloud_svc,
            "BACKUP_KEYCHAIN_RESTIC": kc_restic_svc,
            "EXTRA_MIGRATION_PATHS": extra_paths,
            **optional_cfg,
        }
        for key, value in list(env_values.items()):
            if _unexpanded(value):
                warnings.append(f"{key} was not written: its value still contains an unexpanded expression ({value})")
                env_values[key] = ""
        env_lines = ["# Auto-generated by setup_backups.py — DO NOT COMMIT"]
        env_lines += [f'export {key}="{shell_dq(value)}"' for key, value in env_values.items()]
        write_private_file(backup_env_file, "\n".join(env_lines) + "\n")

        # 2. Install scripts into ~/Library/Application Support/Codex Storage/agents/
        #    Each file is swapped in with a rename: zsh reads scripts incrementally, so rewriting one
        #    in place would corrupt a job that is running it.
        for fname in script_files:
            src_f = src_scripts_dir / fname
            dst_f = agents_dir / fname
            tmp_f = agents_dir / f".{fname}.new"
            shutil.copy2(src_f, tmp_f)
            tmp_f.chmod(0o755)
            os.replace(tmp_f, dst_f)

        # 2b. Ensure icloud_status helper & restic binary link in Codex Storage/bin
        swift_src = src_scripts_dir / "icloud_status.swift"
        if swift_src.is_file():
            dst_swift = bin_dir / "icloud_status.swift"
            shutil.copy2(swift_src, dst_swift)
            icloud_bin = bin_dir / "icloud_status"
            needs_compile = (
                not icloud_bin.is_file()
                or dst_swift.stat().st_mtime > icloud_bin.stat().st_mtime
            )
            if needs_compile and Path("/usr/bin/swiftc").is_file():
                tmp_bin = bin_dir / f".icloud_status.new.{os.getpid()}"
                try:
                    # The first swiftc run on a new Mac builds the SDK module cache and can take minutes
                    rc_c = subprocess.run(
                        ["/usr/bin/swiftc", "-O", str(dst_swift), "-o", str(tmp_bin)],
                        capture_output=True,
                        timeout=300,
                    ).returncode
                except (OSError, subprocess.SubprocessError) as exc:
                    rc_c = -1
                    warnings.append(f"icloud_status helper was not compiled ({exc}); rerun --setup-backups later")
                if rc_c == 0 and tmp_bin.is_file():
                    tmp_bin.chmod(0o755)
                    os.replace(tmp_bin, icloud_bin)
                elif tmp_bin.exists():
                    tmp_bin.unlink()

        bin_restic = bin_dir / "restic"
        local_restic = local_bin_dir / "restic"
        if bin_restic.is_file() and not local_restic.exists():
            try:
                local_restic.symlink_to(bin_restic)
            except OSError:
                pass
        elif local_restic.is_file() and not bin_restic.exists():
            try:
                bin_restic.symlink_to(local_restic)
            except OSError:
                pass

        # 3. Write LaunchAgents; reload only the ones whose definition changed or that are not loaded,
        #    so re-running the installer never kills a backup that is in progress
        for label, pl_dict in specs:
            payload = plistlib.dumps(pl_dict, sort_keys=False)
            live_plist = launch_agents_dir / f"{label}.plist"
            try:
                changed = live_plist.read_bytes() != payload
            except OSError:
                changed = True
            for target_dir in (launchd_backup_dir, launch_agents_dir):
                pl_path = target_dir / f"{label}.plist"
                pl_path.write_bytes(payload)
                pl_path.chmod(0o644)
            error = load_launch_agent(uid, label, live_plist, changed)
            if error:
                launchd_errors[label] = error

        # 4. Run volume-guard.sh and status.sh once to initialise the status dashboard. A slow drive
        #    (diskutil can wait for a disk to spin up) must not abort the installation.
        for script_name, limit in (("volume-guard.sh", 90), ("status.sh", 30)):
            try:
                subprocess.run(["/bin/zsh", str(agents_dir / script_name)], capture_output=True, timeout=limit)
            except (OSError, subprocess.SubprocessError) as exc:
                warnings.append(f"{script_name} did not finish its first run ({exc}); the LaunchAgent runs it again")

    result = {
        "mode": "apply" if apply else "dry-run",
        "storage_mode": storage_mode,
        "workspace_root": vol_path,
        "volume_uuid": vol_uuid,
        "backup_config_env": str(backup_env_file),
        "agents_dir": str(agents_dir),
        "installed_scripts": script_files,
        "host_tag": host_tag,
        "host_tag_source": host_tag_source,
        "launchd_label_prefix": prefix,
        "scheduled_jobs": [label for label, _ in specs],
        "keychain_services": keychain_report,
        "status_dashboard": str(storage_root / "backup-status.md"),
    }
    if warnings:
        result["warnings"] = warnings
    if launchd_errors:
        result["launchd_errors"] = launchd_errors
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if launchd_errors:
        print(
            "ERROR: these LaunchAgents could not be loaded and will not run: " + ", ".join(sorted(launchd_errors)),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
