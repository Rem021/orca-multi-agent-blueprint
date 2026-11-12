#!/usr/bin/env python3
"""Consistent point-in-time snapshot of a live SQLite database (sqlite_snapshot.py).

Usage: sqlite_snapshot.py <src.db> <dst.db>

Shared by the backup jobs (lib.sh `sqlite_snapshot`) and by migrate.py. It never writes next
to the live database and never leaves a torn or half-written file at <dst>:

1. SQLite online backup API through a read-only connection. The copy is transactionally
   consistent even while another process keeps writing.
2. If the database cannot be opened that way (EXCLUSIVE locking mode, unreadable wal-index, not
   a SQLite file, ...), clone DB + WAL + rollback journal (APFS copy-on-write, falling back to a
   byte copy) and accept the clone only when the source fingerprint (sizes, mtimes, WAL header)
   is identical before and after the copy. Copying the files one after another without that
   check can pair a main file and a WAL from different checkpoints.

The result is built in a temp file beside <dst> and renamed over it, so a failed snapshot keeps
the previous good copy.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

SIDE_FILES = ("-wal", "-journal")


class SnapshotError(RuntimeError):
    pass


def _stat_sig(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _fingerprint(src: Path) -> tuple:
    wal = Path(str(src) + "-wal")
    header = b""
    try:
        with wal.open("rb") as f:
            header = f.read(32)  # magic, salts and checksum change on every WAL reset
    except OSError:
        pass
    return (_stat_sig(src), _stat_sig(wal), header, _stat_sig(Path(str(src) + "-journal")))


def _clone(src: Path, dst: Path) -> None:
    r = subprocess.run(["/bin/cp", "-c", str(src), str(dst)], capture_output=True)
    if r.returncode != 0:
        shutil.copyfile(src, dst)


def _remove(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            Path(str(path) + suffix).unlink()
        except OSError:
            pass


def _backup_api(src: Path, tmp: Path, timeout: float) -> None:
    con = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)
    try:
        out = sqlite3.connect(str(tmp))
        try:
            # Connection.backup() retries a busy/locked source forever; give up after `timeout`
            # so the caller can fall back to a clone instead of hanging the backup job.
            deadline = time.monotonic() + timeout

            def progress(status: int, remaining: int, total: int) -> None:
                if status in (5, 6) and time.monotonic() > deadline:  # SQLITE_BUSY, SQLITE_LOCKED
                    raise sqlite3.OperationalError("database is locked")

            con.backup(out, progress=progress, sleep=0.1)
        finally:
            out.close()
    finally:
        con.close()


def _clone_stable(src: Path, tmp: Path, attempts: int) -> bool:
    """Clone DB and side files; True when the source did not change during the copy."""
    for attempt in range(attempts):
        _remove(tmp)
        before = _fingerprint(src)
        _clone(src, tmp)
        for suffix in SIDE_FILES:
            side = Path(str(src) + suffix)
            if side.is_file():
                try:
                    _clone(side, Path(str(tmp) + suffix))
                except OSError:
                    pass
        if _fingerprint(src) == before:
            return True
        time.sleep(0.15 * (attempt + 1))
    return False


def _fold_side_files(tmp: Path) -> bool:
    """Replay the cloned WAL / roll back the cloned hot journal inside the private copy."""
    folded = True
    try:
        con = sqlite3.connect(str(tmp), timeout=5)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            con.execute("SELECT count(*) FROM sqlite_master").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        folded = False
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            Path(str(tmp) + suffix).unlink()
        except OSError:
            pass
    return folded


def snapshot(src: Path | str, dst: Path | str, timeout: float = 10.0, attempts: int = 5) -> dict:
    """Write a consistent copy of `src` to `dst`. Returns {"method", "consistent"}."""
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        raise SnapshotError(f"source database not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.snap-{os.getpid()}"
    _remove(tmp)
    try:
        try:
            _backup_api(src, tmp, timeout)
            result = {"method": "backup-api", "consistent": True}
        except sqlite3.Error:
            stable = _clone_stable(src, tmp, attempts)
            if not tmp.is_file():
                raise SnapshotError(f"could not copy {src}")
            folded = _fold_side_files(tmp)
            result = {"method": "clone" if folded else "clone-raw", "consistent": stable}
        os.replace(tmp, dst)
        # A stale side file from an earlier run must not be replayed into the new snapshot
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                Path(str(dst) + suffix).unlink()
            except OSError:
                pass
        return result
    except OSError as exc:
        raise SnapshotError(str(exc)) from exc
    finally:
        _remove(tmp)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    try:
        result = snapshot(argv[1], argv[2])
    except SnapshotError as exc:
        print(f"{time.strftime('%F %T')} sqlite_snapshot failed: {exc}", file=sys.stderr)
        return 1
    if not result["consistent"]:
        print(
            f"{time.strftime('%F %T')} sqlite_snapshot: {argv[1]} kept changing during the copy "
            f"({result['method']}); snapshot may lag the live database",
            file=sys.stderr,
        )
    print(result["method"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
