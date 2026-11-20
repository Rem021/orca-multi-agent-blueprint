#!/usr/bin/env python3
"""Rolling & polling retention pruner for nightly encrypted iCloud backup packages (icloud_prune.py).

Shared by:
  - icloud-package.sh (after upload confirmation, or via `--prune-only`)
  - agent-hygiene.sh  (periodic LaunchAgent polling every 30m / throttled by interval)
  - Manual / Justfile (`just prune-icloud`, `just prune-icloud-dry-run`)

Design invariants (fail-closed):
  1. Only files matching `mac-agents-[<host>-]<YYYYMMDD>-<HHMMSS|HHMM>.tar.gz.enc`
     (or their pre-FileProvider `.NAME.icloud` placeholders) are ever considered.
     Files such as `RESTORE.md` or dotfiles are never touched.
  2. A package that is not confirmed uploaded
     (`isUploaded == true` with no conflicts/errors) is NEVER deleted and NEVER
     counted as satisfying the retention quota for its host — so previously uploaded
     packages stay intact until the newer upload finishes.
  3. Every active named host always keeps at least `keep_latest >= 1` confirmed
     uploaded package (unless `stale_host_days > 0` explicitly retires a foreign host
     while the current host already has a confirmed uploaded package).
  4. In `all-hosts` scope (default), each host group is pruned independently to its
     own rolling window (`keep_latest` / `max_age_days`), and superseded hostless
     legacy packages (`mac-agents-YYYYMMDD-HHMMSS.tar.gz.enc`) are cleaned once any
     newer host-tagged package is confirmed uploaded.
  5. When macOS TCC prevents a background LaunchAgent from listing the iCloud folder
     (`PermissionError`), the pruner degrades gracefully to known filenames from local
     ledgers (`$STORAGE/icloud-published-*.list`) and shared host ledgers
     (`$ICLOUD/.published-<host>.list`).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

LEGACY_HOST = "__legacy__"

PKG_RE = re.compile(
    r"^mac-agents-(?:(?P<host>[A-Za-z0-9._-]+)-)?(?P<date>\d{8})-(?P<time>\d{4,6})\.tar\.gz\.enc$"
)


@dataclass
class PackageEntry:
    canonical_name: str
    disk_name: str
    path: Path
    host: str
    is_legacy: bool
    timestamp: datetime
    is_placeholder: bool
    size: int = 0
    uploaded: bool = False
    download_status: str = ""
    upload_checked: bool = False

    def sort_key(self) -> Tuple[datetime, str]:
        return (self.timestamp, self.canonical_name)


def sanitize_host(host: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", (host or "").strip())
    return cleaned or "Mac"


def human_bytes(num: int) -> str:
    value = float(max(0, int(num)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{int(num)} B"


def parse_package_name(disk_name: str) -> Optional[Tuple[str, str, bool, datetime, bool]]:
    """Parse a package filename or `.NAME.icloud` placeholder.

    Returns `(canonical_name, host, is_legacy, timestamp, is_placeholder)` or `None`.
    """
    is_placeholder = False
    canonical = disk_name
    if disk_name.startswith(".") and disk_name.endswith(".icloud") and len(disk_name) > len("..icloud"):
        canonical = disk_name[1 : -len(".icloud")]
        is_placeholder = True

    m = PKG_RE.match(canonical)
    if not m:
        return None

    date_s = m.group("date")
    time_s = m.group("time")
    if len(time_s) == 4:
        time_s = time_s + "00"
    elif len(time_s) != 6:
        return None

    try:
        ts = datetime.strptime(f"{date_s}-{time_s}", "%Y%m%d-%H%M%S")
    except ValueError:
        return None

    raw_host = m.group("host")
    if raw_host:
        return (canonical, raw_host, False, ts, is_placeholder)
    return (canonical, LEGACY_HOST, True, ts, is_placeholder)


def _atomic_write_lines(path: Path, lines: Sequence[str]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                if lines:
                    fh.write("\n".join(lines) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
    except OSError:
        return False


def _read_lines(path: Path) -> List[str]:
    try:
        return [ln.strip() for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    except OSError:
        return []


def _query_icloud_status(path: Path, icloud_status_bin: Optional[Path]) -> Optional[dict]:
    if not icloud_status_bin or not os.access(str(icloud_status_bin), os.X_OK):
        return None
    try:
        proc = subprocess.run(
            [str(icloud_status_bin), str(path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                return data
        except ValueError:
            return None
    return None


def _is_confirmed_uploaded(status_payload: Optional[dict]) -> bool:
    if not isinstance(status_payload, dict):
        return False
    return (
        status_payload.get("isUploaded") is True
        and not status_payload.get("hasConflicts")
        and not status_payload.get("uploadingError")
        and not status_payload.get("uploadError")
        and not status_payload.get("errorCode")
    )


def _probe_entry(entry: PackageEntry, icloud_status_bin: Optional[Path]) -> None:
    try:
        st = os.lstat(entry.path)
        entry.size = int(st.st_size)
    except OSError:
        entry.size = 0

    if entry.is_placeholder:
        entry.uploaded = True
        entry.download_status = "NotDownloaded"
        entry.upload_checked = True
        return

    if icloud_status_bin and os.access(str(icloud_status_bin), os.X_OK):
        info = _query_icloud_status(entry.path, icloud_status_bin)
        entry.upload_checked = True
        if info is not None:
            entry.uploaded = _is_confirmed_uploaded(info)
            entry.download_status = str(info.get("downloadStatus") or "")
            if isinstance(info.get("size"), int) and info["size"] > entry.size:
                entry.size = int(info["size"])
        else:
            entry.uploaded = False
    else:
        entry.upload_checked = False
        entry.uploaded = False


def _collect_ledger_candidates(
    icloud_dir: Path,
    storage_dir: Optional[Path],
    current_host: str,
    scope: str,
) -> List[str]:
    names: List[str] = []
    hosts_to_check = [current_host]

    if storage_dir is not None:
        if scope == "all-hosts":
            known_hosts_file = storage_dir / "icloud-known-hosts.list"
            for h in _read_lines(known_hosts_file):
                sh = sanitize_host(h)
                if sh not in hosts_to_check:
                    hosts_to_check.append(sh)
            try:
                for p in sorted(storage_dir.glob("icloud-published-*.list")):
                    names.extend(_read_lines(p))
            except OSError:
                pass
        else:
            names.extend(_read_lines(storage_dir / f"icloud-published-{current_host}.list"))

    for h in hosts_to_check:
        names.extend(_read_lines(icloud_dir / f".published-{h}.list"))

    seen = set()
    unique: List[str] = []
    for n in names:
        base = os.path.basename(n)
        if base and base not in seen:
            seen.add(base)
            unique.append(base)
    return unique


def discover_packages(
    icloud_dir: Path,
    storage_dir: Optional[Path],
    current_host: str,
    scope: str,
    icloud_status_bin: Optional[Path],
    persist_hosts: bool = False,
) -> Tuple[List[PackageEntry], bool]:
    """Return `(entries, listing_restricted)`."""
    by_canonical: Dict[str, PackageEntry] = {}
    listing_restricted = False

    def register(disk_name: str, full_path: Path) -> None:
        if full_path.is_symlink() or not full_path.is_file():
            return
        parsed = parse_package_name(disk_name)
        if not parsed:
            return
        canonical, host, is_legacy, ts, is_placeholder = parsed
        # Prefer concrete file over `.icloud` placeholder if both somehow appear
        existing = by_canonical.get(canonical)
        if existing is not None and not existing.is_placeholder:
            return
        by_canonical[canonical] = PackageEntry(
            canonical_name=canonical,
            disk_name=disk_name,
            path=full_path,
            host=host,
            is_legacy=is_legacy,
            timestamp=ts,
            is_placeholder=is_placeholder,
        )

    try:
        with os.scandir(icloud_dir) as it:
            for de in it:
                if not (de.is_file(follow_symlinks=False) or de.is_symlink()):
                    continue
                register(de.name, Path(de.path))
    except FileNotFoundError:
        pass
    except PermissionError:
        listing_restricted = True
    except OSError:
        listing_restricted = True

    # Always merge ledger-known entries (crucial when listing_restricted=True under LaunchAgent TCC)
    for cand in _collect_ledger_candidates(icloud_dir, storage_dir, current_host, scope):
        if cand in by_canonical:
            continue
        direct = icloud_dir / cand
        placeholder = icloud_dir / f".{cand}.icloud"
        if os.path.lexists(direct):
            register(cand, direct)
        elif os.path.lexists(placeholder):
            register(f".{cand}.icloud", placeholder)

    entries = list(by_canonical.values())
    for entry in entries:
        _probe_entry(entry, icloud_status_bin)

    # Persist discovered non-legacy hosts so future TCC-restricted runs know which shared ledgers exist
    if persist_hosts and not listing_restricted and storage_dir is not None:
        known_hosts_file = storage_dir / "icloud-known-hosts.list"
        discovered_hosts = {e.host for e in entries if not e.is_legacy}
        discovered_hosts.add(current_host)
        for existing_h in _read_lines(known_hosts_file):
            discovered_hosts.add(sanitize_host(existing_h))
        _atomic_write_lines(known_hosts_file, sorted(discovered_hosts))

    return entries, listing_restricted


def plan_prune(
    entries: Sequence[PackageEntry],
    *,
    current_host: str,
    keep_latest: int = 1,
    max_age_days: int = 7,
    scope: str = "all-hosts",
    stale_host_days: int = 0,
    now_ts: Optional[float] = None,
) -> Tuple[List[PackageEntry], List[Tuple[PackageEntry, str]]]:
    """Compute `(keep_list, prune_list_with_reasons)` safely."""
    keep_n = max(1, int(keep_latest))
    age_days = max(0, int(max_age_days))
    stale_days = max(0, int(stale_host_days))
    scope_norm = "self" if scope == "self" else "all-hosts"
    now_dt = datetime.fromtimestamp(now_ts if now_ts is not None else time.time())

    grouped: Dict[str, List[PackageEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.host, []).append(entry)
    for host_entries in grouped.values():
        host_entries.sort(key=lambda e: e.sort_key(), reverse=True)

    named_confirmed = [e for e in entries if not e.is_legacy and e.uploaded]
    newest_named_confirmed_ts: Optional[datetime] = (
        max(e.timestamp for e in named_confirmed) if named_confirmed else None
    )
    own_has_confirmed = any(e.host == current_host and e.uploaded for e in entries)

    kept: List[PackageEntry] = []
    to_prune: List[Tuple[PackageEntry, str]] = []

    for host, host_entries in sorted(grouped.items()):
        if scope_norm == "self" and host != current_host:
            kept.extend(host_entries)
            continue

        # Handle hostless legacy packages in all-hosts scope
        if host == LEGACY_HOST:
            confirmed_kept = 0
            for entry in host_entries:
                if not entry.uploaded:
                    kept.append(entry)
                    continue
                if newest_named_confirmed_ts is not None and entry.timestamp <= newest_named_confirmed_ts:
                    to_prune.append((entry, "superseded_legacy"))
                    continue
                age_sec = (now_dt - entry.timestamp).total_seconds()
                if confirmed_kept == 0:
                    kept.append(entry)
                    confirmed_kept += 1
                elif confirmed_kept >= keep_n:
                    to_prune.append((entry, "exceeds_keep_latest"))
                elif age_days > 0 and age_sec > age_days * 86400:
                    to_prune.append((entry, "expired_max_age"))
                else:
                    kept.append(entry)
                    confirmed_kept += 1
            continue

        # Optional stale foreign host retirement (only when opted in AND current host is healthy)
        if (
            stale_days > 0
            and host != current_host
            and own_has_confirmed
            and host_entries
            and all(e.uploaded for e in host_entries)
        ):
            newest_age_sec = (now_dt - host_entries[0].timestamp).total_seconds()
            if newest_age_sec > stale_days * 86400:
                for entry in host_entries:
                    to_prune.append((entry, "stale_foreign_host"))
                continue

        # Standard per-host rolling retention:
        #   - Unconfirmed uploads are always kept and never count toward the safe-uploaded quota.
        #   - The newest confirmed uploaded package is ALWAYS kept as anchor #1.
        #   - Confirmed packages #2..K are pruned if beyond keep_latest OR older than max_age_days.
        confirmed_kept = 0
        for entry in host_entries:
            if not entry.uploaded:
                kept.append(entry)
                continue
            age_sec = (now_dt - entry.timestamp).total_seconds()
            if confirmed_kept == 0:
                kept.append(entry)
                confirmed_kept += 1
            elif confirmed_kept >= keep_n:
                to_prune.append((entry, "exceeds_keep_latest"))
            elif age_days > 0 and age_sec > age_days * 86400:
                to_prune.append((entry, "expired_max_age"))
            else:
                kept.append(entry)
                confirmed_kept += 1

    kept.sort(key=lambda e: (e.host, e.sort_key()), reverse=True)
    to_prune.sort(key=lambda item: (item[0].host, item[0].sort_key()), reverse=True)
    return kept, to_prune


def _evict_local_copy(entry: PackageEntry) -> bool:
    if entry.is_placeholder:
        return False
    if entry.download_status == "NSURLUbiquitousItemDownloadingStatusNotDownloaded":
        return False
    try:
        r = subprocess.run(
            ["/usr/bin/brctl", "evict", str(entry.path)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _sync_ledgers(
    kept_entries: Sequence[PackageEntry],
    icloud_dir: Path,
    storage_dir: Optional[Path],
    current_host: str,
) -> None:
    own_kept = sorted(
        (e.canonical_name for e in kept_entries if e.host == current_host),
        reverse=True,
    )
    if storage_dir is not None:
        _atomic_write_lines(storage_dir / f"icloud-published-{current_host}.list", own_kept)
    if icloud_dir.exists() and own_kept:
        _atomic_write_lines(icloud_dir / f".published-{current_host}.list", own_kept)


def run_prune(
    *,
    icloud_dir: Path,
    storage_dir: Optional[Path],
    current_host: str,
    keep_latest: int = 1,
    max_age_days: int = 7,
    scope: str = "all-hosts",
    stale_host_days: int = 0,
    icloud_status_bin: Optional[Path] = None,
    apply: bool = False,
    evict_kept: bool = False,
    now_ts: Optional[float] = None,
) -> dict:
    safe_host = sanitize_host(current_host)
    entries, listing_restricted = discover_packages(
        icloud_dir=icloud_dir,
        storage_dir=storage_dir,
        current_host=safe_host,
        scope=scope,
        icloud_status_bin=icloud_status_bin,
        persist_hosts=apply,
    )
    kept, to_prune = plan_prune(
        entries,
        current_host=safe_host,
        keep_latest=keep_latest,
        max_age_days=max_age_days,
        scope=scope,
        stale_host_days=stale_host_days,
        now_ts=now_ts,
    )

    pruned_records: List[dict] = []
    failed_records: List[dict] = []
    final_kept: List[PackageEntry] = list(kept)
    reclaimed_bytes = 0

    for entry, reason in to_prune:
        rec = {
            "name": entry.canonical_name,
            "disk_name": entry.disk_name,
            "host": entry.host,
            "size": entry.size,
            "size_human": human_bytes(entry.size),
            "reason": reason,
        }
        if not apply:
            pruned_records.append(rec)
            reclaimed_bytes += entry.size
            continue
        try:
            os.unlink(entry.path)
            if not os.path.lexists(entry.path):
                pruned_records.append(rec)
                reclaimed_bytes += entry.size
            else:
                failed_records.append({**rec, "error": "still_exists_after_unlink"})
                final_kept.append(entry)
        except OSError as exc:
            failed_records.append({**rec, "error": str(exc)})
            final_kept.append(entry)

    evicted_names: List[str] = []
    if apply and evict_kept:
        for entry in final_kept:
            if entry.host == safe_host and entry.uploaded:
                if _evict_local_copy(entry):
                    evicted_names.append(entry.canonical_name)

    if apply:
        _sync_ledgers(final_kept, icloud_dir, storage_dir, safe_host)

    own_entries = sorted((e for e in final_kept if e.host == safe_host), key=lambda e: e.sort_key(), reverse=True)
    pending_own = [e.canonical_name for e in own_entries if not e.uploaded]
    latest_own_uploaded = next((e for e in own_entries if e.uploaded), None)

    kept_records = [
        {
            "name": e.canonical_name,
            "disk_name": e.disk_name,
            "host": e.host,
            "size": e.size,
            "size_human": human_bytes(e.size),
            "uploaded": e.uploaded,
        }
        for e in sorted(final_kept, key=lambda x: (x.host, x.sort_key()), reverse=True)
    ]

    return {
        "icloud_dir": str(icloud_dir),
        "host": safe_host,
        "scope": "self" if scope == "self" else "all-hosts",
        "keep_latest": max(1, int(keep_latest)),
        "max_age_days": max(0, int(max_age_days)),
        "stale_host_days": max(0, int(stale_host_days)),
        "apply": apply,
        "listing_restricted": listing_restricted,
        "discovered_count": len(entries),
        "kept_count": len(kept_records),
        "pruned_count": len(pruned_records),
        "failed_count": len(failed_records),
        "reclaimed_bytes": reclaimed_bytes,
        "reclaimed_human": human_bytes(reclaimed_bytes),
        "kept": kept_records,
        "pruned": pruned_records,
        "failed": failed_records,
        "evicted": evicted_names,
        "pending_own_uploads": pending_own,
        "latest_own_uploaded": (
            {
                "name": latest_own_uploaded.canonical_name,
                "size": latest_own_uploaded.size,
                "size_human": human_bytes(latest_own_uploaded.size),
            }
            if latest_own_uploaded
            else None
        ),
    }


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def main(argv: Optional[Sequence[str]] = None) -> int:
    home = Path.home()
    default_storage = home / "Library/Application Support/Codex Storage"
    default_icloud = os.environ.get(
        "BACKUP_ICLOUD_DIR",
        str(home / "Library/Mobile Documents/com~apple~CloudDocs/AI备份/每晚小包"),
    )
    default_host = os.environ.get("BACKUP_HOST_TAG") or ""

    ap = argparse.ArgumentParser(description="Rolling retention pruner for nightly iCloud backup packages.")
    ap.add_argument("--icloud-dir", default=default_icloud, help="iCloud backup package directory")
    ap.add_argument("--storage-dir", default=str(default_storage), help="Codex Storage state directory")
    ap.add_argument("--host", default=default_host, help="Current Mac host tag (BACKUP_HOST_TAG)")
    ap.add_argument("--keep-latest", type=int, default=_env_int("BACKUP_ICLOUD_KEEP_LATEST", 1))
    ap.add_argument("--max-age-days", type=int, default=_env_int("BACKUP_ICLOUD_MAX_AGE_DAYS", 7))
    ap.add_argument(
        "--scope",
        choices=("all-hosts", "self"),
        default=os.environ.get("BACKUP_ICLOUD_PRUNE_SCOPE", "all-hosts") or "all-hosts",
    )
    ap.add_argument("--stale-host-days", type=int, default=_env_int("BACKUP_ICLOUD_STALE_HOST_DAYS", 0))
    ap.add_argument("--icloud-status-bin", default="", help="Path to icloud_status binary")
    ap.add_argument("--apply", action="store_true", help="Delete superseded packages (default is dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="Preview deletions without removing files")
    ap.add_argument("--evict-kept", action="store_true", help="Run brctl evict on kept uploaded own packages")
    ap.add_argument("--reference-now", type=float, default=None, help="Override current unix timestamp (tests)")
    ap.add_argument("--json", action="store_true", help="Print JSON summary to stdout")
    args = ap.parse_args(argv)

    host = args.host
    if not host:
        try:
            r = subprocess.run(["/usr/sbin/scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=5)
            host = r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            host = ""
    if not host:
        host = os.uname().nodename.split(".")[0]

    storage_dir = Path(args.storage_dir).expanduser() if args.storage_dir else None
    icloud_status_bin: Optional[Path] = None
    if args.icloud_status_bin:
        icloud_status_bin = Path(args.icloud_status_bin).expanduser()
    elif storage_dir is not None and (storage_dir / "bin/icloud_status").is_file():
        icloud_status_bin = storage_dir / "bin/icloud_status"

    apply = bool(args.apply and not args.dry_run)
    report = run_prune(
        icloud_dir=Path(args.icloud_dir).expanduser(),
        storage_dir=storage_dir,
        current_host=host,
        keep_latest=args.keep_latest,
        max_age_days=args.max_age_days,
        scope=args.scope,
        stale_host_days=args.stale_host_days,
        icloud_status_bin=icloud_status_bin,
        apply=apply,
        evict_kept=bool(args.evict_kept),
        now_ts=args.reference_now,
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        mode = "APPLY" if apply else "DRY-RUN"
        print(
            f"[{mode}] iCloud packages ({report['scope']}, keep={report['keep_latest']}, "
            f"max_age={report['max_age_days']}d): discovered={report['discovered_count']} "
            f"kept={report['kept_count']} pruned={report['pruned_count']} ({report['reclaimed_human']})"
        )
        if report["listing_restricted"]:
            print("  note: directory listing restricted by macOS privacy (TCC); used ledger fallback")
        for item in report["kept"]:
            state = "uploaded" if item["uploaded"] else "pending-upload"
            print(f"  KEEP   {item['name']} ({item['size_human']}, {item['host']}, {state})")
        for item in report["pruned"]:
            action = "PRUNED" if apply else "WOULD-PRUNE"
            print(f"  {action:<11} {item['name']} ({item['size_human']}, {item['host']}, {item['reason']})")
        for item in report["failed"]:
            print(f"  FAILED {item['name']} ({item['error']})")

    return 0 if report["failed_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
