#!/usr/bin/env python3
"""Unit tests for the universal cross-Mac lossless migration engine (scripts/migrate.py)."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

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

    def test_corrupted_sqlite_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            corrupt_db = tmp_path / "corrupt.sqlite"
            # Valid SQLite header followed by garbage pages so quick_check/backup fails
            corrupt_db.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
            ok, detail = migrate.verify_sqlite_quick_check(corrupt_db)
            self.assertFalse(ok)

            dst_db = tmp_path / "out/corrupt_dst.sqlite"
            res = migrate.sqlite_online_backup(corrupt_db, dst_db)
            self.assertEqual(res["status"], "error")

    def test_discover_keychain_services_includes_mandatory_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            svcs = migrate.discover_keychain_services(Path(tmp))
            self.assertIn("restic-mac-agents", svcs)
            self.assertIn("icloud-package-key", svcs)

    def test_keychain_db_excluded_from_home_targets(self):
        self.assertNotIn("Library/Keychains/login.keychain-db", migrate.HOME_RELATIVE_TARGETS)
        self.assertIn("Library/Keychains", migrate.RSYNC_EXCLUDES)

    def test_relocate_home_references_boundary_and_binary_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            new_home = Path(tmp)
            cfg_dir = new_home / ".claude"
            cfg_dir.mkdir(parents=True)
            settings = cfg_dir / "settings.json"
            settings.write_text(
                '{"a": "/Users/kafu/.local/bin/agent-guard", "b": "/Users/kafu2/keep-me"}',
                encoding="utf-8",
            )
            la_dir = new_home / "Library/LaunchAgents"
            la_dir.mkdir(parents=True)
            bplist = la_dir / "local.orca.test.plist"
            with bplist.open("wb") as f:
                plistlib.dump(
                    {
                        "Label": "local.orca.test",
                        "ProgramArguments": ["/Users/kafu/.local/bin/runner", "/Users/kafu2/untouched"],
                    },
                    f,
                    fmt=plistlib.FMT_BINARY,
                )

            stats = migrate.relocate_home_references(new_home, "kafu", "newuser")
            self.assertGreaterEqual(stats["rewritten_configs"], 2)
            updated_json = settings.read_text(encoding="utf-8")
            self.assertIn(f"{new_home}/.local/bin/agent-guard", updated_json)
            self.assertIn("/Users/kafu2/keep-me", updated_json)

            raw_plist = bplist.read_bytes()
            self.assertTrue(raw_plist.startswith(b"bplist00"))
            parsed = plistlib.loads(raw_plist)
            self.assertEqual(parsed["ProgramArguments"][0], f"{new_home}/.local/bin/runner")
            self.assertEqual(parsed["ProgramArguments"][1], "/Users/kafu2/untouched")

    def test_setup_backups_ensure_keychain_secret_fails_closed(self):
        import setup_backups

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_dir = tmp_path / "repo"
            repo_dir.mkdir(parents=True)
            (repo_dir / "config").write_text("restic-repo-marker", encoding="utf-8")

            status = setup_backups.ensure_keychain_secret(
                "nonexistent-test-service-999999",
                "testuser",
                apply=True,
                init_new_secrets=False,
                restic_repo=str(repo_dir),
                migration_secrets_file=tmp_path / "missing-secrets.json",
            )
            self.assertEqual(status, "missing_fail_closed")

    def test_cmd_restore_pack_state_gate_and_fake_home_roundtrip(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            vol_root = tmp_path / "vol"
            bundle = vol_root / "AI/Migration/latest"
            meta = bundle / "metadata"
            home_snap = bundle / "home"
            (home_snap / ".claude").mkdir(parents=True)
            meta.mkdir(parents=True)
            (home_snap / ".claude/settings.json").write_text('{"ok": true}', encoding="utf-8")
            (meta / "MANIFEST.json").write_text(
                json.dumps(
                    {
                        "source_user": "kafu",
                        "source_uid": 501,
                        "source_hostname": "old-mac",
                        "targets": [".claude"],
                        "bundled_core_apps": [],
                        "sqlite_backups": {},
                    }
                ),
                encoding="utf-8",
            )

            # 1. Incomplete PACK_STATE must fail closed
            (meta / "PACK_STATE.json").write_text(
                json.dumps({"state": "in_progress"}),
                encoding="utf-8",
            )
            target_home = tmp_path / "target_home"
            (target_home / ".claude").mkdir(parents=True)
            (target_home / ".claude/settings.json").write_text('{"old": true}', encoding="utf-8")

            rc_incomplete = migrate.cmd_restore(
                vol=vol_root,
                dry_run=False,
                force=False,
                enable_agents=[],
                target_home=target_home,
            )
            self.assertEqual(rc_incomplete, 1)

            # 2. Complete PACK_STATE succeeds and backs up preexisting file
            (meta / "PACK_STATE.json").write_text(
                json.dumps({"state": "complete"}),
                encoding="utf-8",
            )
            rc_complete = migrate.cmd_restore(
                vol=vol_root,
                dry_run=False,
                force=False,
                enable_agents=[],
                target_home=target_home,
            )
            self.assertEqual(rc_complete, 0)
            self.assertEqual(
                json.loads((target_home / ".claude/settings.json").read_text(encoding="utf-8")),
                {"ok": True},
            )
            self.assertTrue((target_home / ".orca/.migrated-from").is_file())
            backups = list((target_home / ".migration-preexisting").rglob("settings.json"))
            self.assertEqual(len(backups), 1)


class TestMigrationSafety(unittest.TestCase):
    """Pack/restore behaviour that protects data: every test runs against temp dirs only."""

    def setUp(self):
        # The repo's own config.env may describe a real external volume; pin the tests to plain dirs
        env = mock.patch.dict(os.environ, {"STORAGE_MODE": "internal", "AI_VOLUME_UUID": "AUTO"})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def _quiet(fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = fn(*args, **kwargs)
        return rc, out.getvalue() + err.getvalue()

    @staticmethod
    def _make_db(path: Path, rows: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(rows)])
        conn.commit()
        conn.close()

    @staticmethod
    def _write_plist(path: Path, label: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            plistlib.dump({"Label": label, "ProgramArguments": ["/usr/bin/true"]}, f)

    def _fake_home(self, root: Path) -> Path:
        home = root / "srcuser"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude/settings.json").write_text('{"ok": true}', encoding="utf-8")
        (home / ".gemini").mkdir()
        (home / ".gemini/notes.txt").write_text("to be removed later", encoding="utf-8")
        self._make_db(home / ".codex/state_5.sqlite", 50)
        self._write_plist(home / "Library/LaunchAgents/com.example.trader.plist", "com.example.trader")
        self._write_plist(home / "Library/LaunchAgents/local.orca.backup-hot.plist", "local.orca.backup-hot")
        return home

    def test_resolve_host_tag(self):
        import setup_backups

        resolve = setup_backups.resolve_host_tag
        # A tag inherited from the source Mac is replaced by this Mac's name
        self.assertEqual(resolve("MacBook-Pro", "New-Mac", "MacBook-Pro.local")[0], "New-Mac")
        # A deliberately chosen tag, an explicit keep, and a restore onto the same Mac are respected
        self.assertEqual(resolve("studio", "New-Mac", "MacBook-Pro.local")[0], "studio")
        self.assertEqual(resolve("MacBook-Pro", "New-Mac", "MacBook-Pro.local", keep=True)[0], "MacBook-Pro")
        self.assertEqual(
            resolve("MacBook-Pro", "MacBook-Pro", "MacBook-Pro", source_machine="aaa", this_machine="aaa")[0],
            "MacBook-Pro",
        )
        # Two different Macs that share the default name still get distinct tags
        tag, _ = resolve("MacBook-Pro", "MacBook-Pro", "MacBook-Pro", source_machine="aaaaaaaaaaaa", this_machine="bbbbbbbbbbbb")
        self.assertEqual(tag, "MacBook-Pro-bbbbbb")
        self.assertEqual(resolve("", "New-Mac")[0], "New-Mac")

    def test_private_file_and_shell_quoting(self):
        import setup_backups

        self.assertEqual(setup_backups.shell_dq('a"b$c`d\\e'), 'a\\"b\\$c\\`d\\\\e')
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "secret.env"
            target.write_text("old", encoding="utf-8")
            target.chmod(0o644)
            setup_backups.write_private_file(target, "new")
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["secret.env"])

            copied = Path(tmp) / "copy.bin"
            migrate.copy_private(target, copied)
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
            self.assertEqual(copied.read_text(encoding="utf-8"), "new")

    def test_failed_sqlite_backup_keeps_previous_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "live.sqlite"
            dst = Path(tmp) / "snapshot/live.sqlite"
            self._make_db(src, 20)
            first = migrate.sqlite_online_backup(src, dst)
            self.assertEqual((first["status"], first["integrity"]), ("ok", "ok"))
            good = dst.read_bytes()

            # The source becomes unreadable garbage: the snapshot must still hold the last good copy
            src.write_bytes(b"SQLite format 3\x00" + b"\xff" * 8192)
            second = migrate.sqlite_online_backup(src, dst, first)
            self.assertEqual(second["status"], "error")
            self.assertEqual(dst.read_bytes(), good)
            self.assertEqual(migrate.verify_sqlite_quick_check(dst)[0], True)
            self.assertEqual(sorted(p.name for p in dst.parent.iterdir()), ["live.sqlite"])

    def test_unchanged_sqlite_backup_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "live.sqlite"
            dst = Path(tmp) / "snapshot/live.sqlite"
            self._make_db(src, 5)
            first = migrate.sqlite_online_backup(src, dst)
            again = migrate.sqlite_online_backup(src, dst, first)
            self.assertEqual(again.get("method"), "reused")
            self.assertEqual(again["sha256"], first["sha256"])

    def test_snapshot_origin_detection(self):
        foreign = migrate.snapshot_is_foreign
        self.assertFalse(foreign({}, "A", "m1"))
        # Machine ids win over names: same name on another Mac is foreign, a renamed Mac is not
        self.assertTrue(foreign({"source_local_hostname": "A", "source_machine_id": "m2"}, "A", "m1"))
        self.assertFalse(foreign({"source_local_hostname": "Old-Name", "source_machine_id": "m1"}, "A", "m1"))
        # Snapshots written before machine ids existed fall back to the host name
        self.assertTrue(foreign({"source_hostname": "MacBook-Pro.local"}, "New-Mac", "m1"))
        self.assertFalse(foreign({"source_hostname": "MacBook-Pro.local"}, "MacBook-Pro", "m1"))

    def test_manifest_paths_must_stay_inside_home(self):
        self.assertTrue(migrate.is_safe_relative("Library/Application Support/orca"))
        for bad in ("", "/etc/passwd", "../outside", "a/../../b", None, 3):
            self.assertFalse(migrate.is_safe_relative(bad), bad)

    def test_launch_agent_classification_and_restore_plan(self):
        self.assertTrue(migrate.is_blueprint_agent("local.kafu.backup-hot.plist"))
        self.assertTrue(migrate.is_blueprint_agent("local.orca.volume-guard"))
        self.assertFalse(migrate.is_blueprint_agent("local.kafu.something-else.plist"))
        self.assertFalse(migrate.is_blueprint_agent("com.example.backup-hot.plist"))

        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "live"
            snap = Path(tmp) / "snap"
            for name in ("com.example.trader", "com.example.sync", "local.orca.backup-hot", "com.apple.thing"):
                self._write_plist(live / f"{name}.plist", name)
                self._write_plist(snap / f"{name}.plist", name)
            # Already on this Mac before the restore and not part of the snapshot: not ours to manage
            self._write_plist(live / "com.example.preexisting.plist", "com.example.preexisting")

            plan = migrate.plan_restored_agents(live, snap, {"com.example.sync"})
            labels = {kind: [label for label, _ in entries] for kind, entries in plan.items()}
            self.assertEqual(labels, {"blueprint": ["local.orca.backup-hot"], "enable": ["com.example.sync"], "hold": ["com.example.trader"]})
            self.assertEqual(
                [label for label, _ in migrate.third_party_agents(live)],
                ["com.example.preexisting", "com.example.sync", "com.example.trader"],
            )

    def test_restored_backup_config_gets_this_macs_host_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".orca").mkdir()
            env_file = home / ".orca/backup-config.env"
            env_file.write_text('export BACKUP_PREFIX="kafu"\nexport BACKUP_HOST_TAG="MacBook-Pro"\nexport X="1"\n', encoding="utf-8")
            manifest = {"source_hostname": "MacBook-Pro.local"}

            self.assertEqual(migrate.rewrite_inherited_host_tag(home, manifest, "New-Mac", "m1"), "New-Mac")
            text = env_file.read_text(encoding="utf-8")
            self.assertIn('export BACKUP_HOST_TAG="New-Mac"\n', text)
            self.assertIn('export BACKUP_PREFIX="kafu"\n', text)
            self.assertIn('export X="1"\n', text)
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
            # Idempotent, and a hand-picked tag is left alone
            self.assertEqual(migrate.rewrite_inherited_host_tag(home, manifest, "New-Mac", "m1"), "")
            env_file.write_text('BACKUP_HOST_TAG="studio"\n', encoding="utf-8")
            self.assertEqual(migrate.rewrite_inherited_host_tag(home, manifest, "New-Mac", "m1"), "")

    def test_pack_restore_roundtrip_in_temp_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._fake_home(root)
            vol = root / "vol"
            vol.mkdir()
            snap = vol / "AI/Migration/latest"

            # Dry-run writes nothing
            rc, _ = self._quiet(migrate.cmd_pack, vol, dry_run=True, fast_config_only=False, home=home)
            self.assertEqual(rc, 0)
            self.assertEqual(list(vol.iterdir()), [])

            rc, out = self._quiet(migrate.cmd_pack, vol, dry_run=False, fast_config_only=False, home=home)
            self.assertEqual(rc, 0, out)
            manifest = json.loads((snap / "metadata/MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "1.4")
            self.assertEqual(manifest["errors"], [])
            self.assertTrue(manifest["source_local_hostname"])
            self.assertIn(".gemini", manifest["targets"])
            self.assertEqual(manifest["sqlite_backups"][".codex/state_5.sqlite"]["integrity"], "ok")
            self.assertEqual(json.loads((snap / "metadata/PACK_STATE.json").read_text())["state"], "complete")
            self.assertTrue((snap / "home/.gemini/notes.txt").is_file())
            self.assertTrue((snap / "blueprint/deploy.sh").is_file())
            self.assertFalse((snap / "blueprint/.git").exists())

            launcher = vol / "AI/Migration/restore-on-new-mac.sh"
            self.assertTrue(os.access(launcher, os.X_OK))
            self.assertTrue(os.access(vol / migrate.ROOT_COMMAND_NAME, os.X_OK))
            guide = (vol / migrate.ROOT_GUIDE_NAME).read_text(encoding="utf-8")
            self.assertIn("com.example.trader", guide)
            self.assertIn("local.orca.backup-hot", guide)
            self.assertNotIn("None", guide)
            self.assertIn("com.example.trader", (vol / "AI/Migration/AGENT_RESTORE.md").read_text(encoding="utf-8"))

            # A directory deleted on the source does not linger in the snapshot
            shutil.rmtree(home / ".gemini")
            rc, out = self._quiet(migrate.cmd_pack, vol, dry_run=False, fast_config_only=False, home=home)
            self.assertEqual(rc, 0, out)
            self.assertFalse((snap / "home/.gemini").exists())
            self.assertTrue((snap / "home/.claude/settings.json").is_file())

            # A snapshot that belongs to another Mac is not silently replaced
            manifest_file = snap / "metadata/MANIFEST.json"
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            manifest.update({"source_local_hostname": "Some-Other-Mac", "source_hostname": "Some-Other-Mac.local", "source_machine_id": "ffffffffffff"})
            manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
            before = manifest_file.read_bytes()
            rc, out = self._quiet(migrate.cmd_pack, vol, dry_run=False, fast_config_only=False, home=home)
            self.assertEqual(rc, 1)
            self.assertIn("different Mac", out)
            self.assertEqual(manifest_file.read_bytes(), before)
            self.assertEqual(json.loads((snap / "metadata/PACK_STATE.json").read_text())["state"], "complete")
            rc, out = self._quiet(migrate.cmd_pack, vol, dry_run=False, fast_config_only=False, home=home, force=True)
            self.assertEqual(rc, 0, out)

            # Restore into a fresh home under a different user name
            target = root / "newuser"
            target.mkdir()
            rc, out = self._quiet(migrate.cmd_restore, vol, dry_run=True, target_home=target)
            self.assertEqual(rc, 0, out)
            self.assertEqual(list(target.iterdir()), [])
            rc, out = self._quiet(migrate.cmd_restore, vol, dry_run=False, target_home=target)
            self.assertEqual(rc, 0, out)
            self.assertEqual(json.loads((target / ".claude/settings.json").read_text()), {"ok": True})
            conn = sqlite3.connect(target / ".codex/state_5.sqlite")
            self.assertEqual(conn.execute("SELECT count(*) FROM t").fetchone()[0], 50)
            conn.close()
            self.assertTrue((target / "Library/LaunchAgents/com.example.trader.plist").is_file())
            self.assertIn("1 blueprint, 0 enabled, 1 held", out)
            self.assertIn("com.example.trader", out)
            marker = json.loads((target / ".orca/.migrated-from").read_text(encoding="utf-8"))
            repacked = json.loads(manifest_file.read_text(encoding="utf-8"))
            self.assertNotEqual(repacked["source_local_hostname"], "Some-Other-Mac")
            self.assertEqual(marker["source_local_hostname"], repacked["source_local_hostname"])
            self.assertEqual(marker["source_machine_id"], repacked["source_machine_id"])

    def test_restore_ignores_paths_that_escape_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snap = root / "vol/AI/Migration/latest"
            (snap / "home/.claude").mkdir(parents=True)
            (snap / "home/.claude/a.txt").write_text("a", encoding="utf-8")
            (snap / "outside").mkdir()
            (snap / "outside/evil.txt").write_text("x", encoding="utf-8")
            (snap / "metadata").mkdir()
            (snap / "metadata/MANIFEST.json").write_text(
                json.dumps({"source_user": "u", "targets": [".claude", "../outside", "/etc"], "sqlite_backups": {"../x.db": {}}}),
                encoding="utf-8",
            )
            (snap / "metadata/PACK_STATE.json").write_text('{"state": "complete"}', encoding="utf-8")
            target = root / "home/user"
            target.mkdir(parents=True)
            rc, out = self._quiet(migrate.cmd_restore, root / "vol", dry_run=False, target_home=target)
            self.assertEqual(rc, 0, out)
            self.assertTrue((target / ".claude/a.txt").is_file())
            self.assertFalse((root / "home/outside").exists())

    def test_guide_reflects_snapshot_state(self):
        facts = {
            "packed_local": "2026-01-02 03:04 PST", "source_host": "Old-Mac", "source_user": "u", "macos": "26.0", "arch": "arm64",
            "volume_uuid": "UUID-1", "encrypted": False, "fast_config_only": False, "targets_count": 12,
            "sqlite_total": 3, "sqlite_ok": 3, "apps": ["Orca"], "uv_pythons": ["cpython-3.12.1-macos-aarch64-none"],
            "python_framework": False, "keychain_services": ["restic-mac-agents"], "blueprint_jobs": ["local.u.backup-hot"],
            "third_party_agents": ["com.example.trader"], "skills_count": 2, "claude_profiles": 1, "codex_accounts": 0,
            "nas_smb_url": "smb://u@nas.local/Data", "has_tailscale": True,
        }  # fmt: skip
        vol = Path("/Volumes/EXAMPLE")
        guide = migrate.render_guide(vol, {}, "complete", facts, extra="## 我的备注\n\nkeep me")
        for expected in ("Old-Mac", "**未加密**", "com.example.trader", "smb://u@nas.local/Data", "## 我的备注", "agents --enable"):
            self.assertIn(expected, guide)
        self.assertNotIn("恢复会被拒绝", guide)
        self.assertNotIn("None", guide)

        failed = migrate.render_guide(vol, {}, "failed", dict(facts, encrypted=True, third_party_agents=[], nas_smb_url=""))
        self.assertIn("恢复会被拒绝", failed)
        self.assertNotIn("未加密", failed)
        self.assertNotIn("smb://", failed)

        launcher = migrate.render_launcher("AI/Projects/Mac/outputs/orca-multi-agent-blueprint")
        self.assertIn("--restore-from-ssd", launcher)
        self.assertNotIn("python", launcher.lower())  # Python is located by deploy.sh, not by the launcher


if __name__ == "__main__":
    unittest.main()
