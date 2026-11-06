#!/usr/bin/env python3
"""Unit tests for the universal cross-Mac lossless migration engine (scripts/migrate.py)."""
from pathlib import Path
import plistlib
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

    def test_is_valid_sqlite_file_skips_encrypted_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            encrypted_file = tmp_path / "encrypted.sqlite"
            encrypted_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
            self.assertFalse(migrate.is_valid_sqlite_file(encrypted_file))

            real_db = tmp_path / "real.sqlite"
            conn = sqlite3.connect(real_db)
            conn.execute("CREATE TABLE t (x INT);")
            conn.commit()
            conn.close()
            self.assertTrue(migrate.is_valid_sqlite_file(real_db))

    def test_discover_launchagent_dependencies_universal(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            la_dir = fake_home / "Library/LaunchAgents"
            la_dir.mkdir(parents=True)
            svc_dir = fake_home / "Library/Application Support/CustomAgentService"
            svc_dir.mkdir(parents=True)
            script_file = svc_dir / "runner.sh"
            script_file.write_text(
                '#!/bin/zsh\nsecurity find-generic-password -s "custom-agent-secret" -w\n',
                encoding="utf-8",
            )

            plist_path = la_dir / "com.example.customagent.plist"
            with plist_path.open("wb") as f:
                plistlib.dump(
                    {
                        "Label": "com.example.customagent",
                        "ProgramArguments": ["/bin/zsh", str(script_file)],
                        "WorkingDirectory": str(svc_dir),
                    },
                    f,
                )

            rels, _, keychains = migrate.discover_launchagent_dependencies(fake_home)
            self.assertIn("Library/Application Support/CustomAgentService", rels)
            self.assertIn("custom-agent-secret", keychains)

    def test_relocate_home_references_cross_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            new_home = Path(tmp)
            cfg_dir = new_home / ".claude"
            cfg_dir.mkdir(parents=True)
            settings = cfg_dir / "settings.json"
            settings.write_text(
                '{"hooks": {"PreToolUse": [{"command": "/Users/olduser/.local/bin/agent-guard --mode hook"}]}}',
                encoding="utf-8",
            )
            skills_dir = new_home / ".agents/skills"
            skills_dir.mkdir(parents=True)
            broken_link = cfg_dir / "skill-link"
            broken_link.symlink_to("/Users/olduser/.agents/skills/demo")

            stats = migrate.relocate_home_references(new_home, "olduser", "newuser")
            self.assertEqual(stats["rewritten_configs"], 1)
            self.assertEqual(stats["relocated_symlinks"], 1)
            self.assertIn(str(new_home), settings.read_text(encoding="utf-8"))
            self.assertEqual(str(broken_link.readlink()), f"{new_home}/.agents/skills/demo")

    def test_setup_backups_plist_generation_and_dual_storage_mode(self):
        import discover_and_adopt
        import setup_backups

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            agents_dir = fake_home / "Library/Application Support/Codex Storage/agents"
            logs_dir = fake_home / "Library/Logs/CodexStorage"
            plists = dict(setup_backups.build_launchd_specs("local.orca", agents_dir, logs_dir))
            self.assertEqual(len(plists), 7)
            self.assertIn("local.orca.icloud-package", plists)
            self.assertIn("local.orca.backup-hot", plists)
            self.assertIn("local.orca.backup-projects", plists)
            self.assertIn("local.orca.volume-guard", plists)
            self.assertEqual(
                plists["local.orca.icloud-package"]["StartCalendarInterval"],
                {"Hour": 2, "Minute": 30},
            )

            vol_path, vol_uuid, is_ext, resolved_mode = discover_and_adopt.detect_volume(storage_mode="internal")
            self.assertFalse(is_ext)
            self.assertEqual(resolved_mode, "internal")
            self.assertEqual(vol_uuid, "LOCAL")
            self.assertTrue(str(vol_path).endswith("AI-Workspace"))


if __name__ == "__main__":
    unittest.main()
