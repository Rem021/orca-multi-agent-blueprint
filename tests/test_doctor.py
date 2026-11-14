#!/usr/bin/env python3
"""Tests for the health verdicts of scripts/doctor.py and the volume filter of discover_and_adopt.py.

Every test points $HOME at a throwaway directory and stubs launchd, so nothing on the machine is read
or changed.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import discover_and_adopt  # noqa: E402
import doctor  # noqa: E402

FMT = doctor.STATUS_TIME_FORMAT


def stamp(hours_ago: float) -> str:
    return time.strftime(FMT, time.localtime(time.time() - hours_ago * 3600))


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="doctor-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.storage = self.home / "Library/Application Support/Codex Storage"
        (self.storage / "status").mkdir(parents=True)
        (self.home / "Library/LaunchAgents").mkdir(parents=True)
        (self.home / ".orca").mkdir()
        for patcher in (
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch.object(doctor, "STORAGE_ROOT", self.storage),
            mock.patch.object(doctor, "launchd_loaded", lambda label: True),
            mock.patch.object(doctor, "local_host_name", lambda: "NewMac"),
            mock.patch.object(doctor, "machine_id", lambda: "MACHINE-NEW"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def install_job(self, job: str, **receipt):
        (self.home / f"Library/LaunchAgents/local.test.{job}.plist").write_text("<plist/>", encoding="utf-8")
        if receipt:
            receipt.setdefault("name", job)
            (self.storage / "status" / f"{job}.json").write_text(json.dumps(receipt), encoding="utf-8")

    def write_backup_config(self, **values):
        text = "".join(f'{k}="{v}"\n' for k, v in values.items())
        (self.home / ".orca/backup-config.env").write_text(text, encoding="utf-8")


class TestBackupHealth(DoctorCase):
    def test_recent_success_is_healthy(self):
        self.install_job("icloud-package", state="ok", message="uploaded", time=stamp(3))
        result = doctor.check_backup_health()
        self.assertEqual(result["attention"], [])
        self.assertEqual(result["jobs"]["icloud-package"]["state"], "ok")
        self.assertFalse(result["jobs"]["backup-hot"]["installed"])

    def test_job_that_only_ever_skipped_is_reported(self):
        # The regression this check exists for: installed and loaded, yet it has never backed anything up
        self.install_job("backup-hot", state="skipped", message="NAS not mounted", time=stamp(1))
        attention = doctor.check_backup_health()["attention"]
        self.assertEqual(len(attention), 1)
        self.assertIn("backup-hot", attention[0])
        self.assertIn("no successful run", attention[0])

    def test_skip_after_a_recent_success_is_tolerated(self):
        self.install_job("backup-hot", state="skipped", message="NAS not mounted", time=stamp(1), last_ok=stamp(5))
        self.assertEqual(doctor.check_backup_health()["attention"], [])

    def test_success_older_than_the_job_limit_is_reported(self):
        self.install_job("backup-hot", state="skipped", message="NAS not mounted", time=stamp(1), last_ok=stamp(30))
        attention = doctor.check_backup_health()["attention"]
        self.assertEqual(len(attention), 1)
        self.assertIn("has not succeeded for 30 h", attention[0])

    def test_failed_and_stale_states_are_reported(self):
        self.install_job("maintenance", state="fail", message="prune failed", time=stamp(2), last_ok=stamp(2))
        self.install_job("icloud-package", state="stale", message="skipped for 3 days", time=stamp(2))
        attention = "\n".join(doctor.check_backup_health()["attention"])
        self.assertIn("maintenance is fail", attention)
        self.assertIn("icloud-package is stale", attention)

    def test_unloaded_job_is_reported(self):
        self.install_job("backup-media", state="ok", message="done", time=stamp(2))
        with mock.patch.object(doctor, "launchd_loaded", lambda label: False):
            attention = doctor.check_backup_health()["attention"]
        self.assertEqual(len(attention), 1)
        self.assertIn("not loaded", attention[0])

    def test_job_waiting_for_its_first_run_is_not_reported(self):
        self.install_job("restore-drill")
        self.assertEqual(doctor.check_backup_health()["attention"], [])

    def test_unreadable_timestamps_do_not_crash(self):
        self.assertIsNone(doctor.hours_since("yesterday"))
        self.assertIsNone(doctor.hours_since(None))
        self.assertAlmostEqual(doctor.hours_since(stamp(2)), 2, delta=0.1)


class TestHostTag(DoctorCase):
    def marker(self, **values):
        (self.home / ".orca/.migrated-from").write_text(json.dumps(values), encoding="utf-8")

    def test_tag_inherited_from_the_source_mac_is_reported(self):
        self.write_backup_config(BACKUP_HOST_TAG="OldMac")
        self.marker(source_hostname="OldMac.local")
        result = doctor.check_host_tag()
        self.assertEqual(result["expected"], "NewMac")
        self.assertEqual(len(result["attention"]), 1)
        self.assertIn("'OldMac'", result["attention"][0])

    def test_keep_flag_silences_the_report(self):
        self.write_backup_config(BACKUP_HOST_TAG="OldMac", BACKUP_HOST_TAG_KEEP="true")
        self.marker(source_hostname="OldMac.local")
        self.assertEqual(doctor.check_host_tag()["attention"], [])

    def test_own_tag_is_healthy(self):
        self.write_backup_config(BACKUP_HOST_TAG="NewMac")
        self.marker(source_hostname="OldMac.local")
        self.assertEqual(doctor.check_host_tag()["attention"], [])

    def test_custom_tag_without_a_migration_is_left_alone(self):
        self.write_backup_config(BACKUP_HOST_TAG="studio")
        self.assertEqual(doctor.check_host_tag()["attention"], [])

    def test_no_backup_config_means_nothing_to_check(self):
        self.assertFalse(doctor.check_host_tag()["configured"])


class TestDeploymentDrift(DoctorCase):
    def deploy(self):
        agents = self.storage / "agents"
        agents.mkdir()
        for f in (REPO / "scripts/codex-storage").iterdir():
            if f.is_file() and not f.name.endswith(".swift") and not f.name.startswith("."):
                shutil.copy2(f, agents / f.name)
        (self.home / ".local/bin").mkdir(parents=True)
        shutil.copy2(REPO / "scripts/guard.py", self.home / ".local/bin/agent-guard")
        (self.home / ".orca/guard.env").write_text('GUARD_PROTECTED_BRANCHES="main master"\n', encoding="utf-8")
        return agents

    def test_identical_copies_are_healthy(self):
        self.deploy()
        result = doctor.check_deployment_drift()
        self.assertEqual(result["attention"], [])
        self.assertTrue(result["agent_guard_current"])

    def test_outdated_script_and_guard_are_reported(self):
        agents = self.deploy()
        with (agents / "lib.sh").open("a", encoding="utf-8") as f:
            f.write("# old\n")
        (agents / "status.sh").unlink()
        (self.home / ".local/bin/agent-guard").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        result = doctor.check_deployment_drift()
        self.assertEqual(result["stale_backup_scripts"], ["lib.sh", "status.sh"])
        self.assertEqual(len(result["attention"]), 2)

    def test_guard_without_its_settings_file_is_reported(self):
        self.deploy()
        (self.home / ".orca/guard.env").unlink()
        result = doctor.check_deployment_drift()
        self.assertFalse(result["agent_guard_config"])
        self.assertEqual(len(result["attention"]), 1)
        self.assertIn("guard.env", result["attention"][0])

    def test_nothing_installed_is_not_drift(self):
        self.assertEqual(doctor.check_deployment_drift()["attention"], [])


class TestNasAndHooks(DoctorCase):
    def test_no_nas_configured(self):
        self.write_backup_config(BACKUP_HOST_TAG="NewMac")
        self.assertFalse(doctor.check_nas()["configured"])

    def test_unmounted_share_tells_the_user_how_to_connect(self):
        self.write_backup_config(BACKUP_NAS_SMB_MATCH="//user@nas.invalid/Data on /Volumes/Data (smbfs,")
        result = doctor.check_nas()
        self.assertEqual(result["share"], "smb://user@nas.invalid/Data")
        self.assertFalse(result["mounted"])
        self.assertIn("Connect to Server", result["attention"][0])

    def test_hook_file_without_the_guard_is_reported(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude/settings.json").write_text('{"hooks": {}}', encoding="utf-8")
        (self.home / ".codex").mkdir()
        (self.home / ".codex/hooks.json").write_text(
            '{"hooks": {"PreToolUse": [{"hooks": [{"command": "~/.local/bin/agent-guard --mode hook"}]}]}}',
            encoding="utf-8",
        )
        with mock.patch.dict(doctor._CFG_ENV, {"ENABLE_PRETOOLUSE_GUARDRAILS": "true"}), mock.patch.dict(os.environ):
            os.environ.pop("ENABLE_PRETOOLUSE_GUARDRAILS", None)
            result = doctor.check_guard_hooks()
        self.assertEqual(result["wired"], {"claude": False, "codex": True})
        self.assertEqual(len(result["attention"]), 1)
        self.assertIn("claude", result["attention"][0])


class TestSummary(unittest.TestCase):
    def test_collects_attention_and_advice_from_every_check(self):
        summary = doctor.summarize(
            {
                "a": {"status": "ok", "attention": ["fix a"]},
                "b": {"status": "ok", "advice": ["consider b"]},
                "c": {"status": "unknown"},
                "profile_parity": {"status": "ok", "all_aligned": False},
                "cli_toolchain": {"restic": "/opt/homebrew/bin/restic", "gitleaks": None},
                "schema_version": 1,
            }
        )
        self.assertFalse(summary["ok"])
        self.assertEqual(len(summary["attention"]), 3)
        self.assertTrue(summary["attention"][0].startswith("Could not be checked: c"))
        self.assertIn("fix a", summary["attention"])
        self.assertEqual(len(summary["advice"]), 2)
        self.assertIn("gitleaks", summary["advice"][1])

    def test_healthy_report(self):
        summary = doctor.summarize({"a": {"status": "ok", "attention": []}})
        self.assertTrue(summary["ok"])
        self.assertIn("nothing needs attention", doctor.render_summary(summary))

    def test_rendered_summary_numbers_every_item(self):
        text = doctor.render_summary({"ok": False, "attention": ["one", "two"], "advice": ["three"]})
        self.assertIn("2 item(s) need attention", text)
        self.assertIn("  2. two", text)
        self.assertIn("  - three", text)


class TestVolumeFilter(unittest.TestCase):
    def test_only_real_writable_external_drives_qualify(self):
        usable = discover_and_adopt.usable_external_volume
        self.assertTrue(usable({"Internal": False, "BusProtocol": "USB", "WritableVolume": True}))
        self.assertFalse(usable({"Internal": True, "BusProtocol": "Apple Fabric"}))
        self.assertFalse(usable({"Internal": False, "BusProtocol": "Disk Image", "WritableVolume": True}))
        self.assertFalse(usable({"Internal": False, "BusProtocol": "USB", "WritableVolume": False}))
        self.assertFalse(usable({"Internal": False, "FilesystemType": "smbfs"}))

    def test_internal_mode_never_returns_a_volume(self):
        path, uuid, external, mode = discover_and_adopt.detect_volume("/Volumes/Anything", "AUTO", "internal", "~/ws")
        self.assertEqual((uuid, external, mode), ("LOCAL", False, "internal"))
        self.assertEqual(path, Path("~/ws").expanduser())


class TestMigrationSnapshot(DoctorCase):
    def test_internal_mode_finds_snapshot_on_external_media_volume(self):
        internal_ws = self.home / "AI-Workspace"
        (internal_ws / "AI/Projects").mkdir(parents=True)
        ext_ssd = self.home / "ExternalSSD"
        meta = ext_ssd / "AI/Migration/latest/metadata"
        meta.mkdir(parents=True)
        (meta / "MANIFEST.json").write_text(
            json.dumps({"source_local_hostname": "NewMac", "source_machine_id": "MACHINE-NEW"}),
            encoding="utf-8",
        )
        (meta / "PACK_STATE.json").write_text(json.dumps({"state": "complete"}), encoding="utf-8")
        self.write_backup_config(BACKUP_MEDIA_PATH=str(ext_ssd / "Work"))
        with mock.patch.object(doctor, "VOLUME", internal_ws), mock.patch.object(
            doctor, "REPO_ROOT", self.home / "no-checkout"
        ), mock.patch.dict(doctor._CFG_ENV, {}, clear=True), mock.patch.dict(os.environ):
            os.environ.pop("BACKUP_MEDIA_PATH", None)
            result = doctor.check_migration_snapshot()
        self.assertTrue(result["present"])
        self.assertEqual(result["volume"], str(ext_ssd))
        self.assertEqual(result["pack_state"], "complete")
        self.assertTrue(result["packed_on_this_mac"])
        self.assertEqual(result["attention"], [])


if __name__ == "__main__":
    unittest.main()
