#!/usr/bin/env python3
"""Unit tests for the universal cross-Mac lossless migration engine (scripts/migrate.py)."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import migrate  # noqa: E402

_ISOLATION = []


def setUpModule():
    """Every test runs against temp dirs: HOME points at an empty folder (so nothing reaches the real
    ~/Library/Caches or ~/.orca), and configuration from the developer's shell cannot leak in."""
    fake_home = tempfile.mkdtemp(prefix="migrate-test-home-")
    env = mock.patch.dict(os.environ, {"HOME": fake_home, "STORAGE_MODE": "internal", "AI_VOLUME_UUID": "AUTO"})
    env.start()
    for key in [k for k in os.environ if k.startswith(("EXTRA_", "BACKUP_KEYCHAIN_")) or k == "AI_VOLUME_PATH"]:
        os.environ.pop(key)
    _ISOLATION.append((env, fake_home))


def tearDownModule():
    for env, fake_home in _ISOLATION:
        env.stop()
        shutil.rmtree(fake_home, ignore_errors=True)


class TestMigrateEngine(unittest.TestCase):
    def test_vm_exclusions_present(self):
        app_support = "/Library/Application Support/**/"
        for name in ("vm_bundles", "claudevm.bundle", "Cache", "GPUCache"):
            self.assertIn(f"{app_support}{name}/", migrate.RSYNC_EXCLUDES)
            self.assertNotIn(name, migrate.RSYNC_EXCLUDES)  # never unanchored: repositories have such folders
        self.assertIn("/Library/Developer/CoreSimulator/", migrate.RSYNC_EXCLUDES)
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

            res = migrate.sqlite_online_backup(src_db, dst_db, cache_root=tmp_path / "cache")
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
            self.assertEqual(len(plists), 8)
            self.assertIn("local.orca.agent-hygiene", plists)
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
            res = migrate.sqlite_online_backup(corrupt_db, dst_db, cache_root=tmp_path / "cache")
            self.assertEqual(res["status"], "error")

    def test_discover_keychain_services_includes_mandatory_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            svcs = migrate.discover_keychain_services(Path(tmp))
            self.assertIn("restic-mac-agents", svcs)
            self.assertIn("icloud-package-key", svcs)

    def test_keychain_db_excluded_from_home_targets(self):
        self.assertFalse([t for t in migrate.HOME_RELATIVE_TARGETS if t.startswith("Library/Keychains")])
        self.assertIn("/Library/Keychains/", migrate.RSYNC_EXCLUDES)

    def test_relocate_home_references_boundary_and_binary_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            new_home = Path(tmp)
            cfg_dir = new_home / ".claude"
            cfg_dir.mkdir(parents=True)
            settings = cfg_dir / "settings.json"
            settings.write_text(
                '{"a": "/Users/alice/.local/bin/agent-guard", "b": "/Users/alice2/keep-me"}',
                encoding="utf-8",
            )
            la_dir = new_home / "Library/LaunchAgents"
            la_dir.mkdir(parents=True)
            bplist = la_dir / "local.orca.test.plist"
            with bplist.open("wb") as f:
                plistlib.dump(
                    {
                        "Label": "local.orca.test",
                        "ProgramArguments": ["/Users/alice/.local/bin/runner", "/Users/alice2/untouched"],
                    },
                    f,
                    fmt=plistlib.FMT_BINARY,
                )

            stats = migrate.relocate_home_references(new_home, "alice", "newuser")
            self.assertGreaterEqual(stats["rewritten_configs"], 2)
            updated_json = settings.read_text(encoding="utf-8")
            self.assertIn(f"{new_home}/.local/bin/agent-guard", updated_json)
            self.assertIn("/Users/alice2/keep-me", updated_json)

            raw_plist = bplist.read_bytes()
            self.assertTrue(raw_plist.startswith(b"bplist00"))
            parsed = plistlib.loads(raw_plist)
            self.assertEqual(parsed["ProgramArguments"][0], f"{new_home}/.local/bin/runner")
            self.assertEqual(parsed["ProgramArguments"][1], "/Users/alice2/untouched")

    def test_setup_backups_ensure_keychain_secret_fails_closed(self):
        import setup_backups

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_dir = tmp_path / "repo"
            repo_dir.mkdir(parents=True)
            (repo_dir / "config").write_text("restic-repo-marker", encoding="utf-8")

            # No real Keychain query: every `security` lookup reports "item not found"
            not_found = mock.Mock(returncode=44, stdout="", stderr="")
            with mock.patch.object(setup_backups.subprocess, "run", return_value=not_found) as run:
                status = setup_backups.ensure_keychain_secret(
                    "nonexistent-test-service-999999",
                    "testuser",
                    apply=True,
                    init_new_secrets=False,
                    restic_repo=str(repo_dir),
                    migration_secrets_file=tmp_path / "missing-secrets.json",
                )
            self.assertEqual(status, "missing_fail_closed")
            self.assertTrue(all(call.args[0][0] == "/usr/bin/security" for call in run.call_args_list))

    def test_cmd_restore_pack_state_gate_and_fake_home_roundtrip(self):
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
                        "source_user": "alice",
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
        self._write_plist(home / "Library/LaunchAgents/com.example.worker.plist", "com.example.worker")
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
            cache = Path(tmp) / "cache"
            first = migrate.sqlite_online_backup(src, dst, cache_root=cache)
            self.assertEqual((first["status"], first["integrity"]), ("ok", "ok"))
            good = dst.read_bytes()

            # The source becomes unreadable garbage: the snapshot must still hold the last good copy
            src.write_bytes(b"SQLite format 3\x00" + b"\xff" * 8192)
            second = migrate.sqlite_online_backup(src, dst, first, cache_root=cache)
            self.assertEqual(second["status"], "error")
            self.assertEqual(dst.read_bytes(), good)
            self.assertEqual(migrate.verify_sqlite_quick_check(dst)[0], True)
            self.assertEqual(sorted(p.name for p in dst.parent.iterdir()), ["live.sqlite"])

    def test_unchanged_sqlite_backup_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "live.sqlite"
            dst = Path(tmp) / "snapshot/live.sqlite"
            self._make_db(src, 5)
            cache = Path(tmp) / "cache"
            first = migrate.sqlite_online_backup(src, dst, cache_root=cache)
            again = migrate.sqlite_online_backup(src, dst, first, cache_root=cache)
            self.assertEqual(again.get("method"), "reused")
            self.assertEqual(again["sha256"], first["sha256"])

            # A same-size in-place change right after the copy is picked up (no mtime tolerance)
            conn = sqlite3.connect(src)
            conn.execute("UPDATE t SET v = 'changed' WHERE id = 1")
            conn.commit()
            conn.close()
            third = migrate.sqlite_online_backup(src, dst, again, cache_root=cache)
            self.assertNotEqual(third.get("method"), "reused")
            check = sqlite3.connect(dst)
            self.assertEqual(check.execute("SELECT v FROM t WHERE id = 1").fetchone()[0], "changed")
            check.close()

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
        self.assertTrue(migrate.is_blueprint_agent("local.alice.backup-hot.plist"))
        self.assertTrue(migrate.is_blueprint_agent("local.orca.volume-guard"))
        self.assertFalse(migrate.is_blueprint_agent("local.alice.something-else.plist"))
        self.assertFalse(migrate.is_blueprint_agent("com.example.backup-hot.plist"))

        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "live"
            snap = Path(tmp) / "snap"
            for name in ("com.example.worker", "com.example.sync", "local.orca.backup-hot", "com.apple.thing"):
                self._write_plist(live / f"{name}.plist", name)
                self._write_plist(snap / f"{name}.plist", name)
            # Already on this Mac before the restore and not part of the snapshot: not ours to manage
            self._write_plist(live / "com.example.preexisting.plist", "com.example.preexisting")

            plan = migrate.plan_restored_agents(live, snap, {"com.example.sync"})
            labels = {kind: [label for label, _ in entries] for kind, entries in plan.items()}
            self.assertEqual(labels, {"blueprint": ["local.orca.backup-hot"], "enable": ["com.example.sync"], "hold": ["com.example.worker"]})
            self.assertEqual(
                [label for label, _ in migrate.third_party_agents(live)],
                ["com.example.preexisting", "com.example.sync", "com.example.worker"],
            )

    def test_restored_backup_config_gets_this_macs_host_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".orca").mkdir()
            env_file = home / ".orca/backup-config.env"
            env_file.write_text('export BACKUP_PREFIX="alice"\nexport BACKUP_HOST_TAG="MacBook-Pro"\nexport X="1"\n', encoding="utf-8")
            manifest = {"source_hostname": "MacBook-Pro.local"}

            # Derived tags carry a short machine id, like a fresh install, so two Macs never share one
            self.assertEqual(migrate.rewrite_inherited_host_tag(home, manifest, "New-Mac", "m1"), "New-Mac-m1")
            text = env_file.read_text(encoding="utf-8")
            self.assertIn('export BACKUP_HOST_TAG="New-Mac-m1"\n', text)
            self.assertIn('export BACKUP_PREFIX="alice"\n', text)
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
            self.assertEqual(manifest["schema_version"], "1.5")
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
            self.assertIn("com.example.worker", guide)
            self.assertIn("local.orca.backup-hot", guide)
            self.assertNotIn("None", guide)
            self.assertIn("com.example.worker", (vol / "AI/Migration/AGENT_RESTORE.md").read_text(encoding="utf-8"))

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
            self.assertTrue((target / "Library/LaunchAgents/com.example.worker.plist").is_file())
            self.assertIn("1 blueprint, 0 enabled, 1 held", out)
            self.assertIn("com.example.worker", out)
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
            "third_party_agents": ["com.example.worker"], "skills_count": 2, "claude_profiles": 1, "codex_accounts": 0,
            "nas_smb_url": "smb://u@nas.local/Data",
        }  # fmt: skip
        vol = Path("/Volumes/EXAMPLE")
        guide = migrate.render_guide(vol, {}, "complete", facts, extra="## 我的备注\n\nkeep me")
        for expected in ("Old-Mac", "**not encrypted**", "com.example.worker", "smb://u@nas.local/Data", "## 我的备注", "agents --enable", "drive you trust"):
            self.assertIn(expected, guide)
        self.assertNotIn("Restore will refuse", guide)
        self.assertNotIn("None", guide)

        failed = migrate.render_guide(vol, {}, "failed", dict(facts, encrypted=True, third_party_agents=[], nas_smb_url=""))
        self.assertIn("Restore will refuse", failed)
        self.assertNotIn("not encrypted", failed)
        self.assertNotIn("smb://", failed)

        launcher = migrate.render_launcher("AI/Projects/Mac/outputs/orca-multi-agent-blueprint")
        self.assertIn("--restore-from-ssd", launcher)
        self.assertNotIn("python", launcher.lower())  # Python is located by deploy.sh, not by the launcher



class TestAuditRegressions(unittest.TestCase):
    """Regression tests for the migrate.py audit findings; temp dirs only, no launchd or Keychain."""

    _quiet = staticmethod(TestMigrationSafety._quiet)
    _make_db = staticmethod(TestMigrationSafety._make_db)
    _write_plist = staticmethod(TestMigrationSafety._write_plist)

    @staticmethod
    def _snapshot(root, targets, sqlite_backups=None, state="complete"):
        snap = root / "vol/AI/Migration/latest"
        (snap / "home").mkdir(parents=True, exist_ok=True)
        (snap / "metadata").mkdir(exist_ok=True)
        manifest = {"source_user": "u", "targets": targets, "sqlite_backups": sqlite_backups or {}}
        (snap / "metadata/MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
        if state is not None:
            (snap / "metadata/PACK_STATE.json").write_text(json.dumps({"state": state}), encoding="utf-8")
        return snap

    # 1 ------------------------------------------------------------------------------------------
    def test_pack_never_writes_through_a_symlinked_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "alice"
            home.mkdir()
            real = root / "codex-real"
            real.mkdir()
            (home / ".codex").symlink_to(real)
            orca_real = root / "orca-real"
            (orca_real / "daemon").mkdir(parents=True)
            (orca_real / "SingletonLock").write_text("live", encoding="utf-8")
            (orca_real / "daemon/orcad.sock").write_text("live", encoding="utf-8")
            (home / "Library/Application Support").mkdir(parents=True)
            (home / "Library/Application Support/orca").symlink_to(orca_real)

            db = real / "state_5.sqlite"
            live = sqlite3.connect(db)
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("PRAGMA wal_autocheckpoint=0")
            live.execute("CREATE TABLE t (v)")
            live.execute("INSERT INTO t VALUES ('in-wal')")
            live.commit()
            inode = db.stat().st_ino
            vol = root / "vol"
            vol.mkdir()
            try:
                rc, out = self._quiet(migrate.cmd_pack, vol, dry_run=False, fast_config_only=False, home=home)
                self.assertEqual(rc, 0, out)
                self.assertEqual(db.stat().st_ino, inode)  # the live database file was not replaced
                self.assertTrue(Path(str(db) + "-wal").exists())
                self.assertTrue((orca_real / "SingletonLock").exists())
                self.assertTrue((orca_real / "daemon/orcad.sock").exists())
            finally:
                live.close()
            manifest = json.loads((vol / "AI/Migration/latest/metadata/MANIFEST.json").read_text(encoding="utf-8"))
            self.assertNotIn(".codex/state_5.sqlite", manifest["sqlite_backups"])
            self.assertTrue(any("~/.codex is a symlink" in w for w in manifest["warnings"]))

    # 2 ------------------------------------------------------------------------------------------
    def test_restore_holds_third_party_agents_even_when_a_later_step_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snap = self._snapshot(root, ["Library/LaunchAgents", ".codex"], {".codex/state_5.sqlite": {}})
            self._write_plist(snap / "home/Library/LaunchAgents/com.example.worker.plist", "com.example.worker")
            bad = snap / "home/.codex/state_5.sqlite"
            bad.parent.mkdir(parents=True)
            bad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 8192)  # bit rot on the drive
            target = root / "home/u"
            target.mkdir(parents=True)
            rc, out = self._quiet(migrate.cmd_restore, root / "vol", dry_run=False, target_home=target)
            self.assertEqual(rc, 1)
            self.assertIn("integrity check failed", out)
            self.assertIn("Held 1 third-party LaunchAgent(s)", out)
            self.assertIn("com.example.worker", out)

    def test_restore_refuses_a_snapshot_without_pack_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._snapshot(root, [], state=None)
            target = root / "home/u"
            target.mkdir(parents=True)
            rc, out = self._quiet(migrate.cmd_restore, root / "vol", dry_run=False, target_home=target)
            self.assertEqual(rc, 1)
            self.assertIn("'missing'", out)

    # 3 ------------------------------------------------------------------------------------------
    def test_running_cli_agents_are_detected(self):
        comm = "\n".join(
            [
                "  101   501 /Applications/Orca.app/Contents/MacOS/Orca",
                "  102   501 /Users/u/.local/bin/claude",
                "  103   501 /opt/homebrew/bin/node",
                "  104   502 /Users/other/.local/bin/codex",
                "  105   501 /usr/bin/vim",
                "  106   501 /Applications/Codex.app/Contents/Resources/codex",
            ]
        )
        args = "\n".join(["  103 node /opt/homebrew/bin/gemini --yolo", "  105 vim notes.txt"])
        found = migrate.running_agents_from_ps(comm, args, 501)
        self.assertEqual(found, ["Orca", "Codex", "claude (CLI)", "gemini (CLI)"])  # uid 502 is not ours

    # 4 ------------------------------------------------------------------------------------------
    def test_restore_keeps_the_wal_of_a_replaced_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snap = self._snapshot(root, [".codex"], {".codex/state_5.sqlite": {}})
            self._make_db(snap / "home/.codex/state_5.sqlite", 3)
            target = root / "home/u"
            (target / ".codex").mkdir(parents=True)
            work = root / "work"
            work.mkdir()
            live = sqlite3.connect(work / "state_5.sqlite")
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("PRAGMA wal_autocheckpoint=0")
            live.execute("CREATE TABLE t (v)")
            live.execute("INSERT INTO t VALUES ('new-mac-work')")
            live.commit()
            for suffix in ("", "-wal"):  # as a crashed app leaves them
                shutil.copyfile(str(work / "state_5.sqlite") + suffix, str(target / ".codex/state_5.sqlite") + suffix)
            live.close()

            rc, out = self._quiet(migrate.cmd_restore, root / "vol", dry_run=False, target_home=target)
            self.assertEqual(rc, 0, out)
            self.assertFalse((target / ".codex/state_5.sqlite-wal").exists())
            kept = next((target / ".migration-preexisting").glob("*/.codex/state_5.sqlite"))
            self.assertTrue(Path(str(kept) + "-wal").is_file())
            conn = sqlite3.connect(kept)
            self.assertEqual(conn.execute("SELECT v FROM t").fetchall(), [("new-mac-work",)])
            conn.close()

    def test_restore_keeps_the_wal_of_a_database_it_did_not_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snap = self._snapshot(root, [".codex"], {".codex/state_5.sqlite": {}})
            self._make_db(snap / "home/.codex/state_5.sqlite", 3)
            target = root / "home/u"
            (target / ".codex").mkdir(parents=True)
            shutil.copy2(snap / "home/.codex/state_5.sqlite", target / ".codex/state_5.sqlite")  # identical: rsync skips it
            (target / ".codex/state_5.sqlite-wal").write_bytes(b"")
            rc, out = self._quiet(migrate.cmd_restore, root / "vol", dry_run=False, target_home=target)
            self.assertEqual(rc, 0, out)
            self.assertTrue((target / ".codex/state_5.sqlite-wal").exists())

    # 5 + 12 -------------------------------------------------------------------------------------
    def test_keychain_import_never_overwrites_a_different_value(self):
        existing = {"svc-same": ("u", "same"), "svc-other": ("u", "this-macs-own")}
        added = []
        with mock.patch.object(migrate, "keychain_lookup", side_effect=lambda svc, acct="": existing.get(svc)), mock.patch.object(
            migrate, "keychain_add", side_effect=lambda svc, acct, pw: added.append((svc, acct)) or True
        ):
            res = migrate.import_keychain_secrets(
                {
                    "svc-same": {"account": "u", "password": "same"},
                    "svc-other": {"account": "u", "password": "example-from-snapshot"},
                    "svc-new": {"account": "bot", "password": "x"},
                },
                "u",
            )
        self.assertEqual(res, {"imported": ["svc-new"], "present": ["svc-same"], "conflicts": ["svc-other"], "failed": []})
        self.assertEqual(added, [("svc-new", "bot")])

    def test_keychain_export_records_the_real_account(self):
        def fake_security(args):
            if "-a" in args:  # nothing stored under $USER
                return mock.Mock(returncode=44, stdout="")
            if "-w" in args:
                return mock.Mock(returncode=0, stdout="s3cret\n")
            return mock.Mock(returncode=0, stdout='keychain: "x"\nattributes:\n    "acct"<blob>="service-bot"\n')

        with mock.patch.object(migrate, "_security", side_effect=fake_security):
            self.assertEqual(
                migrate.export_keychain_secrets("u", ["svc"]), {"svc": {"account": "service-bot", "password": "s3cret"}}
            )

    # 6 ------------------------------------------------------------------------------------------
    def test_restore_rejects_symlinked_parents_and_option_like_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snap = self._snapshot(root, [".x", ".x/evil.txt", ".y/evil.txt", ".claude", "--remove-source-files"])
            (snap / "home/.x").symlink_to("..")
            (snap / "home/.y").symlink_to("..")
            (snap / "evil.txt").write_text("x", encoding="utf-8")
            (snap / "home/.claude").mkdir()
            (snap / "home/.claude/a.txt").write_text("a", encoding="utf-8")
            (snap / "home/--remove-source-files").write_text("", encoding="utf-8")
            target = root / "home/u"
            target.mkdir(parents=True)
            rc, out = self._quiet(migrate.cmd_restore, root / "vol", dry_run=False, target_home=target)
            self.assertEqual(rc, 0, out)
            self.assertFalse((target.parent / "evil.txt").exists())
            self.assertIn(".y/evil.txt", out)  # reported as rejected
            self.assertTrue((target / ".claude/a.txt").is_file())
            self.assertTrue((snap / "home/.claude/a.txt").is_file())  # taken as a file name, not as an rsync option

    # 7 + 9 --------------------------------------------------------------------------------------
    def test_pack_keeps_repository_folders_named_like_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "alice"
            (home / "Developer/MyApp/Sources/Cache").mkdir(parents=True)
            (home / "Developer/MyApp/Sources/Cache/DiskCache.swift").write_text("struct DiskCache {}", encoding="utf-8")
            (home / "Library/Application Support/Claude/Cache").mkdir(parents=True)
            (home / "Library/Application Support/Claude/Cache/blob").write_text("x", encoding="utf-8")
            (home / "Library/Application Support/Claude/config.json").write_text("{}", encoding="utf-8")
            vol = root / "vol"
            vol.mkdir()
            rc, out = self._quiet(migrate.cmd_pack, vol, dry_run=False, fast_config_only=False, home=home)
            self.assertEqual(rc, 0, out)
            snap_home = vol / "AI/Migration/latest/home"
            self.assertTrue((snap_home / "Developer/MyApp/Sources/Cache/DiskCache.swift").is_file())
            self.assertTrue((snap_home / "Library/Application Support/Claude/config.json").is_file())
            self.assertFalse((snap_home / "Library/Application Support/Claude/Cache").exists())

    def test_sqlite_discovery_walks_once_and_skips_dependency_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for rel in ("Developer/app/data/live.db", "Developer/app/node_modules/pkg/fixture.db"):
                self._make_db(home / rel, 1)
                (home / (rel + "-wal")).write_bytes(b"")
            found = migrate.discover_all_sqlite_targets(home, ["Developer"])
            self.assertEqual(found, ["Developer/app/data/live.db"])

    def test_large_launchagent_folders_are_not_packed_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "alice"
            svc = home / "Library/Application Support/BigService"
            svc.mkdir(parents=True)
            (svc / "data.bin").write_bytes(b"\0" * 64 * 1024)
            plist = home / "Library/LaunchAgents/com.example.big.plist"
            plist.parent.mkdir(parents=True)
            with plist.open("wb") as f:
                plistlib.dump({"Label": "com.example.big", "ProgramArguments": [str(svc / "run.sh")]}, f)
            vol = root / "vol"
            vol.mkdir()
            with mock.patch.object(migrate, "DISCOVERED_DIR_MAX_BYTES", 1024):
                rc, out = self._quiet(migrate.cmd_pack, vol, dry_run=True, fast_config_only=False, home=home)
            self.assertEqual(rc, 0, out)
            self.assertIn("BigService (used by a LaunchAgent)", out)
            self.assertNotIn("• ~/Library/Application Support/BigService", out)

    # 10 -----------------------------------------------------------------------------------------
    def test_newest_node_is_picked_by_version_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for version in ("node-v9.11.2", "node-v22.9.0", "node-v22.11.0"):
                (home / "opt" / version / "bin").mkdir(parents=True)
                (home / "opt" / version / "bin/node").write_text("", encoding="utf-8")
            migrate.ensure_local_bin_runtime_symlinks(home)
            self.assertEqual(os.readlink(home / ".local/bin/node"), str(home / "opt/node-v22.11.0/bin/node"))

    # 11 -----------------------------------------------------------------------------------------
    def test_relocation_covers_shell_syntax_and_claude_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bob"
            (home / ".claude/projects/-Users-alice-Developer-app").mkdir(parents=True)
            (home / ".claude/projects/-Users-alice2-x").mkdir()
            zshrc = home / ".zshrc"
            zshrc.write_text(
                "A=${HOME:-/Users/alice}\nB=$(cd /Users/alice)\nC=[/Users/alice]\nD=/Users/alice2\nE=/Users/alice.old\n",
                encoding="utf-8",
            )
            zshrc.chmod(0o600)
            claude_json = home / ".claude.json"
            claude_json.write_bytes(b'{"projects": {"/Users/alice/Developer/app": {}}, "raw": "\xff"}')
            stats = migrate.relocate_home_references(home, "alice", "bob")
            self.assertEqual(
                zshrc.read_text(encoding="utf-8").splitlines(),
                [f"A=${{HOME:-{home}}}", f"B=$(cd {home})", f"C=[{home}]", "D=/Users/alice2", "E=/Users/alice.old"],
            )
            self.assertEqual(zshrc.stat().st_mode & 0o777, 0o600)
            self.assertIn(f'"{home}/Developer/app"'.encode(), claude_json.read_bytes())
            self.assertTrue(claude_json.read_bytes().endswith(b'"\xff"}'))  # undecodable bytes survive
            encoded = "".join(c if c.isalnum() else "-" for c in str(home))
            self.assertTrue((home / f".claude/projects/{encoded}-Developer-app").is_dir())
            self.assertTrue((home / ".claude/projects/-Users-alice2-x").is_dir())
            self.assertEqual(stats["renamed_project_dirs"], 1)

    # 13 -----------------------------------------------------------------------------------------
    def test_launcher_quotes_the_blueprint_path(self):
        payload = 'AI/My "Projects"/$(echo INJECTED)/`id`'
        script = migrate.render_launcher(payload)
        line = next(l for l in script.splitlines() if l.startswith("LIVE_BLUEPRINT_REL="))
        out = subprocess.run(["/bin/zsh", "-f", "-c", f'{line}; print -r -- "$LIVE_BLUEPRINT_REL"'], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), payload)
        self.assertIn(f"zsh {shlex.quote('/Volumes/My SSD/' + migrate.ROOT_COMMAND_NAME)}", migrate.render_guide(
            Path("/Volumes/My SSD"), {}, "complete", dict(self._facts())))

    @staticmethod
    def _facts() -> dict:
        return {
            "packed_local": "t", "source_host": "h", "source_user": "u", "macos": "26.0", "arch": "arm64", "volume_uuid": "U",
            "encrypted": True, "fast_config_only": False, "targets_count": 1, "sqlite_total": 0, "sqlite_ok": 0, "apps": [],
            "uv_pythons": [], "python_framework": False, "keychain_services": [], "blueprint_jobs": [],
            "third_party_agents": [], "skills_count": 0, "claude_profiles": 0, "codex_accounts": 0, "nas_smb_url": "",
        }  # fmt: skip

    # 14 -----------------------------------------------------------------------------------------
    def test_pack_checks_free_space_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "alice"
            (home / ".claude").mkdir(parents=True)
            (home / ".claude/settings.json").write_text("{}", encoding="utf-8")
            vol = root / "vol"
            vol.mkdir()
            tiny = mock.Mock(free=1024)
            with mock.patch.object(migrate.shutil, "disk_usage", return_value=tiny):
                rc, out = self._quiet(migrate.cmd_pack, vol, dry_run=False, fast_config_only=False, home=home)
            self.assertEqual(rc, 1)
            self.assertIn("Free up space", out)
            self.assertEqual(list(vol.iterdir()), [])

    def test_pack_without_an_external_drive_needs_an_explicit_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(migrate, "detect_volume", return_value=Path(tmp)), mock.patch.object(
                sys, "argv", ["migrate.py", "pack", "--apply"]
            ), mock.patch.object(migrate, "cmd_pack") as pack:
                rc, out = self._quiet(migrate.main)
            self.assertEqual(rc, 1)
            self.assertIn("No external drive found", out)
            pack.assert_not_called()

    def test_status_does_not_print_secret_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snap = self._snapshot(root, [])
            manifest = json.loads((snap / "metadata/MANIFEST.json").read_text(encoding="utf-8"))
            manifest["keychain_digests"] = {"svc": "deadbeef"}
            (snap / "metadata/MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            rc, out = self._quiet(migrate.cmd_status, root / "vol")
            self.assertEqual(rc, 0)
            self.assertNotIn("deadbeef", out)

    def test_guide_refresh_leaves_older_guide_files_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._snapshot(root, [])
            vol = root / "vol"
            legacy = vol / migrate.LEGACY_ROOT_FILES[0]
            legacy.write_text("old launcher", encoding="utf-8")
            with mock.patch.object(migrate, "sync_blueprint_copy", return_value=""):
                rc, out = self._quiet(migrate.cmd_guide, vol, dry_run=False)
            self.assertEqual(rc, 0, out)
            self.assertEqual(legacy.read_text(encoding="utf-8"), "old launcher")
            self.assertIn(str(legacy), out)
            self.assertTrue((vol / "Restore on New Mac.command").is_file())
            self.assertTrue((vol / "RESTORE-GUIDE.md").is_file())

    def test_builtin_lists_are_generic(self):
        removed_targets = {".dsh", ".dsh-tui", ".grokbot", ".hermes", "opt", ".pymobiledevice3", "Applications",
                           "Documents/deepseek-harness", "Library/Preferences/io.tailscale.ipn.macsys.plist"}  # fmt: skip
        self.assertFalse(removed_targets & set(migrate.HOME_RELATIVE_TARGETS))
        personal = ("Grok", "deepseek", "com.meta.endo", "cc-same", "MoleApp", "FBAnalytics")
        self.assertFalse([t for t in migrate.HOME_RELATIVE_TARGETS + migrate.SQLITE_RELATIVE_TARGETS if any(p in t for p in personal)])
        for app in ("Muse.app", "DeepSeek Harness.app", "Grok Bot.app", "CC Same.app", "Mole.app", "Ghostty.app", "Tailscale.app"):
            self.assertNotIn(app, migrate.CORE_APP_BUNDLES)
        self.assertFalse({"Muse", "DeepSeek Harness", "Grok Bot"} & set(migrate.AGENT_APPS))
        self.assertTrue(migrate.ROOT_COMMAND_NAME.isascii() and migrate.ROOT_GUIDE_NAME.isascii())


if __name__ == "__main__":
    unittest.main()
