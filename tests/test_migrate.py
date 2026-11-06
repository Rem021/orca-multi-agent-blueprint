#!/usr/bin/env python3
"""Unit tests for the cross-Mac lossless migration engine (scripts/migrate.py)."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import migrate  # noqa: E402


class TestMigrateEngine(unittest.TestCase):
    def test_vm_exclusions_present(self):
        self.assertIn("vm_bundles", migrate.RSYNC_EXCLUDES)
        self.assertIn("claudevm.bundle", migrate.RSYNC_EXCLUDES)
        self.assertIn("CoreSimulator", migrate.RSYNC_EXCLUDES)
        self.assertIn("SingletonLock", migrate.RSYNC_EXCLUDES)
        self.assertIn("orcad.lock", migrate.RSYNC_EXCLUDES)

    def test_sqlite_online_backup_and_quick_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src_db = tmp_path / "src.sqlite"
            dst_db = tmp_path / "out/dst.sqlite"

            conn = sqlite3.connect(src_db)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, title TEXT);")
            conn.execute("INSERT INTO sessions (title) VALUES ('Orca Migration Test');")
            conn.commit()

            res = migrate.sqlite_online_backup(src_db, dst_db)
            conn.close()

            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["integrity"], "ok")
            self.assertTrue(dst_db.is_file())

            verify_conn = sqlite3.connect(dst_db)
            row = verify_conn.execute("SELECT title FROM sessions WHERE id=1;").fetchone()
            verify_conn.close()
            self.assertEqual(row[0], "Orca Migration Test")


if __name__ == "__main__":
    unittest.main()
