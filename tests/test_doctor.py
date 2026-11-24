#!/usr/bin/env python3
"""Tests for the health verdicts of scripts/doctor.py and the volume filter of discover_and_adopt.py.

Every test points $HOME at a throwaway directory and stubs launchd, so nothing on the machine is read
or changed.
"""
import hashlib
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
import sync_profiles

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
        shutil.copy2(REPO / "scripts/test_device.py", self.home / ".local/bin/agent-test-device")
        shutil.copy2(REPO / "scripts/agent_governor.py", self.home / ".local/bin/agent-governor")
        (self.home / ".orca/guard.env").write_text('GUARD_PROTECTED_BRANCHES="main master"\n', encoding="utf-8")
        return agents

    def test_identical_copies_are_healthy(self):
        self.deploy()
        result = doctor.check_deployment_drift()
        self.assertEqual(result["attention"], [])
        self.assertTrue(result["agent_guard_current"])
        self.assertTrue(result["agent_governor_current"])

    def test_outdated_script_guard_and_governor_are_reported(self):
        agents = self.deploy()
        with (agents / "lib.sh").open("a", encoding="utf-8") as f:
            f.write("# old\n")
        (agents / "status.sh").unlink()
        (self.home / ".local/bin/agent-guard").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        (self.home / ".local/bin/agent-governor").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        result = doctor.check_deployment_drift()
        self.assertEqual(result["stale_backup_scripts"], ["lib.sh", "status.sh"])
        self.assertFalse(result["agent_governor_current"])
        self.assertEqual(len(result["attention"]), 3)

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
            json.dumps({"hooks": {"PreToolUse": [{"hooks": [
                {"type": "command", "command": f"{self.home}/.local/bin/agent-guard --mode hook"}
            ]}]}}),
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




class TestDiscoverAdoptAndSyncProfiles(unittest.TestCase):
    def test_cjk_keys_disambiguation_and_nested_subrepo_no_four_zones(self):
        with tempfile.TemporaryDirectory() as tmp:
            vol = Path(tmp).resolve()
            projs = vol / "AI/Projects"
            for n in ("示例项目", "演示工具", "Hub", "Lab"):
                (projs / n).mkdir(parents=True)
            nested_repo = projs / "Hub/outputs/lab"
            (nested_repo / ".git").mkdir(parents=True)

            fake_orca_out = json.dumps(
                {
                    "result": {
                        "repos": [
                            {"displayName": "Lab", "path": str(nested_repo)},
                        ]
                    }
                }
            )
            orig_run = discover_and_adopt.run_cmd

            def fake_run(argv, timeout=15):
                if len(argv) >= 3 and argv[1:3] == ["repo", "list"]:
                    return 0, fake_orca_out
                return orig_run(argv, timeout=timeout)

            with mock.patch.object(discover_and_adopt, "run_cmd", side_effect=fake_run), mock.patch.dict(
                os.environ, {"ORCA_CLI_COMMAND": sys.executable}
            ):
                res = discover_and_adopt.discover_and_adopt_projects(vol, apply=True)

            keys = [e["key"] for e in res["registry"]]
            self.assertEqual(len(keys), len(set(keys)), f"Duplicate keys generated: {keys}")
            self.assertIn("示例项目", keys)
            self.assertIn("演示工具", keys)
            self.assertTrue((projs / "Hub/docs").is_dir())
            self.assertFalse((nested_repo / "docs").exists())
            self.assertFalse((nested_repo / "work").exists())

    def test_sync_profiles_upgrades_legacy_bash_only_matcher(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp).resolve()
            claude_sf = fake_home / ".claude/settings.json"
            claude_sf.parent.mkdir(parents=True)
            claude_sf.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "Bash",
                                    "hooks": [{"type": "command", "command": f"{fake_home}/.local/bin/agent-guard --mode hook"}],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            agy_hf = fake_home / "Library/Application Support/orca/antigravity-chat/config/hooks.json"
            agy_hf.parent.mkdir(parents=True)
            agy_hf.write_text(
                json.dumps(
                    {
                        "agent-guard": {
                            "PreToolUse": [
                                {
                                    "matcher": "run_command",
                                    "hooks": [{"type": "command", "command": f"{fake_home}/.local/bin/agent-guard --mode hook"}],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            env = {**os.environ, "HOME": str(fake_home), "ENABLE_PRETOOLUSE_GUARDRAILS": "true", "ENABLE_XCODE_MCP": "false"}
            r = subprocess.run(
                [sys.executable, str(REPO / "scripts/sync_profiles.py"), "--apply"],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            updated_claude = json.loads(claude_sf.read_text(encoding="utf-8"))
            self.assertEqual(updated_claude["hooks"]["PreToolUse"][0]["matcher"], "Bash|Read|Write|Edit|MultiEdit")
            ss_groups = updated_claude["hooks"].get("SessionStart", [])
            self.assertTrue(any(g.get("matcher") == "compact" and any("--mode compact-anchor" in h.get("command", "") for h in g.get("hooks", [])) for g in ss_groups), ss_groups)
            updated_agy = json.loads(agy_hf.read_text(encoding="utf-8"))
            self.assertEqual(
                updated_agy["agent-guard"]["PreToolUse"][0]["matcher"],
                "run_command|write_to_file|replace_file_content|view_file|client_view_file",
            )

    def test_sync_preserves_import_and_custom_hooks_and_removes_managed_duplicates(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cli = home / ".claude"
            profile = home / "Library/Application Support/orca/claude-profiles/test/home"
            profile.mkdir(parents=True)
            cli.mkdir()
            (cli / "CLAUDE.md").write_text("shared rules")
            pointer = profile / "CLAUDE.md"
            pointer.write_text("@~/.claude/CLAUDE.md")
            guard = {"type": "command", "command": f"{home}/.local/bin/agent-guard --mode hook"}
            cleanup = {"type": "command", "command": f"{home}/.local/bin/agent-governor gc --mode session-end --apply >/dev/null 2>&1 || true"}
            custom = {"type": "command", "command": "echo agent-guard agent-governor"}
            settings = cli / "settings.json"
            group = {"matcher": "Bash", "hooks": [guard, custom, guard]}
            settings.write_text(json.dumps({"hooks": {"PreToolUse": [group], "SessionEnd": [{"hooks": [cleanup, custom]}]}}))
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                self.assertFalse(doctor.check_guard_hooks()["wired"]["claude"])
            cmd = [sys.executable, str(REPO / "scripts/sync_profiles.py"), "--apply"]
            env = {**os.environ, "HOME": tmp, "ENABLE_SESSION_HYGIENE": "false", "ENABLE_PRETOOLUSE_GUARDRAILS": "true"}
            subprocess.run(cmd, env=env, capture_output=True, check=True)
            hooks = json.loads(settings.read_text())["hooks"]
            self.assertEqual(hooks["PreToolUse"][0], {"matcher": "Bash", "hooks": [custom]})
            self.assertEqual(len(hooks["PreToolUse"]), 2)
            self.assertEqual(hooks["SessionEnd"], [{"hooks": [custom]}])
            self.assertEqual(pointer.read_text(), "@~/.claude/CLAUDE.md")
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                self.assertTrue(doctor.check_guard_hooks()["wired"]["claude"])
            self.assertEqual(json.loads(subprocess.check_output(cmd, env=env))["actions"], [])

    def test_skill_copy_preserves_internal_link_and_skips_external_link(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            skill = home / ".agents/skills/demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("demo")
            link = skill / "reference"
            link.symlink_to("SKILL.md")
            env = {**os.environ, "HOME": tmp}
            cmd = [sys.executable, str(REPO / "scripts/sync_profiles.py"), "--apply"]
            subprocess.run(cmd, env=env, check=True, capture_output=True)
            copy = home / "Library/Application Support/orca/antigravity-chat/config/skills/demo/reference"
            self.assertEqual(copy.read_text(), "demo")
            self.assertEqual(copy.resolve().parent, copy.parent.resolve())
            link.unlink()
            external = home / "private-file"
            external.write_text("private")
            link.symlink_to(external)
            report = json.loads(subprocess.check_output(cmd, env=env))
            self.assertTrue(report["skipped"])
            self.assertEqual(copy.read_text(), "demo")

    def test_sync_profiles_creates_missing_antigravity_chat_hooks_json_and_doctor_verifies(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp).resolve()
            agy_skills_dir = fake_home / "Library/Application Support/orca/antigravity-chat/config/skills"
            agy_skills_dir.mkdir(parents=True)
            agy_hf = agy_skills_dir.parent / "hooks.json"
            self.assertFalse(agy_hf.exists())

            with mock.patch.dict(os.environ, {"HOME": str(fake_home), "ENABLE_PRETOOLUSE_GUARDRAILS": "true"}):
                before = doctor.check_guard_hooks()
                self.assertEqual(before["wired"].get("antigravity_chat"), False)
                self.assertTrue(any("antigravity_chat" in item for item in before["attention"]))

            env = {**os.environ, "HOME": str(fake_home), "ENABLE_PRETOOLUSE_GUARDRAILS": "true", "ENABLE_XCODE_MCP": "false"}
            r = subprocess.run(
                [sys.executable, str(REPO / "scripts/sync_profiles.py"), "--apply"],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(agy_hf.is_file())
            created = json.loads(agy_hf.read_text(encoding="utf-8"))
            self.assertEqual(
                created["agent-guard"]["PreToolUse"][0]["matcher"],
                "run_command|write_to_file|replace_file_content|view_file|client_view_file",
            )

            with mock.patch.dict(os.environ, {"HOME": str(fake_home), "ENABLE_PRETOOLUSE_GUARDRAILS": "true"}):
                after = doctor.check_guard_hooks()
                self.assertEqual(after["wired"].get("antigravity_chat"), True)
                self.assertEqual(after["attention"], [])



    def test_sync_profiles_retires_legacy_split_skills_when_orca_workspace_present(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp).resolve()
            agents_skills = fake_home / ".agents/skills"
            (agents_skills / "orca-workspace").mkdir(parents=True)
            (agents_skills / "orca-workspace/SKILL.md").write_text("---\nname: orca-workspace\ndescription: unified\n---\n", encoding="utf-8")

            # Seed legacy split skills as real dirs in ~/.agents/skills and symlinks in client roots
            for legacy in ("agent-governor", "agent-slim", "ast-grep"):
                (agents_skills / legacy).mkdir(parents=True)
                (agents_skills / legacy / "SKILL.md").write_text(f"legacy {legacy}\n", encoding="utf-8")
            claude_skills = fake_home / ".claude/skills"
            claude_skills.mkdir(parents=True)
            (claude_skills / "agent-governor").symlink_to(agents_skills / "agent-governor")
            (claude_skills / "agent-slim").symlink_to(agents_skills / "agent-slim")
            (claude_skills / "ast-grep").symlink_to(agents_skills / "ast-grep")

            env = {**os.environ, "HOME": str(fake_home), "ENABLE_PRETOOLUSE_GUARDRAILS": "false", "ENABLE_XCODE_MCP": "false"}
            custom = fake_home / ".gemini/skills/ast-grep"
            custom.mkdir(parents=True)
            (custom / "SKILL.md").write_text("customized skill")
            digests = {name: {hashlib.sha256(f"legacy {name}\n".encode()).hexdigest()}
                       for name in sync_profiles.RETIRED_SKILLS}
            with mock.patch.dict(sync_profiles.RETIRED_SKILLS, digests), mock.patch.dict(os.environ, env), \
                    mock.patch.object(sys, "argv", ["sync_profiles", "--apply"]), mock.patch("builtins.print"):
                self.assertEqual(sync_profiles.main(), 0)
            self.assertEqual((custom / "SKILL.md").read_text(), "customized skill")
            self.assertTrue((agents_skills / "orca-workspace/SKILL.md").is_file())
            self.assertTrue((claude_skills / "orca-workspace").is_symlink())
            for legacy in ("agent-governor", "agent-slim", "ast-grep"):
                self.assertFalse((agents_skills / legacy).exists(), f"{legacy} should be retired from ~/.agents/skills")
                self.assertFalse((claude_skills / legacy).exists() or (claude_skills / legacy).is_symlink(), f"{legacy} should be retired from ~/.claude/skills")
            # Real directories moved to fake_home/.Trash instead of rm -rf
            trash_hits = list((fake_home / ".Trash").glob("orca-skill-retirement-*/*"))
            self.assertEqual(len(trash_hits), 3)

class TestGitOptimizations(unittest.TestCase):
    def test_global_fsmonitor_true_emits_advice(self):
        fake = {
            "status": "ok",
            "stdout": (
                "feature.manyfiles=true\n"
                "core.fsmonitor=true\n"
                "rerere.enabled=true\n"
                "worktree.userelativepaths=true\n"
                "branch.sort=-committerdate\n"
            ),
        }
        with mock.patch.object(doctor, "command", return_value=fake):
            res = doctor.check_git_optimizations()
        self.assertTrue(res["all_enabled"])
        self.assertFalse(res["global_fsmonitor_disabled"])
        self.assertEqual(len(res["advice"]), 1)
        self.assertIn("core.fsmonitor", res["advice"][0])

    def test_unset_global_fsmonitor_is_healthy(self):
        fake = {
            "status": "ok",
            "stdout": (
                "feature.manyfiles=true\n"
                "rerere.enabled=true\n"
                "worktree.userelativepaths=true\n"
                "branch.sort=-committerdate\n"
            ),
        }
        with mock.patch.object(doctor, "command", return_value=fake):
            res = doctor.check_git_optimizations()
        self.assertTrue(res["all_enabled"])
        self.assertTrue(res["global_fsmonitor_disabled"])
        self.assertEqual(res["advice"], [])


    def test_launchd_specs_internal_volume_guard_and_icloud_abandon_pg(self):
        import setup_backups
        ext_specs = dict(setup_backups.build_launchd_specs("local.test", Path("/tmp/a"), Path("/tmp/l"), storage_mode="external"))
        int_specs = dict(setup_backups.build_launchd_specs("local.test", Path("/tmp/a"), Path("/tmp/l"), storage_mode="internal"))
        self.assertEqual(ext_specs["local.test.volume-guard"]["StartInterval"], 60)
        self.assertEqual(int_specs["local.test.volume-guard"]["StartInterval"], 600)
        self.assertTrue(int_specs["local.test.icloud-package"].get("AbandonProcessGroup"))


class TestProfileParity(DoctorCase):
    def test_profile_parity_tracks_installed_shared_skills_without_retired_hardcodes(self):
        (self.home / ".zshenv").write_text('export PATH="$HOME/.local/bin:$PATH"\n', encoding="utf-8")
        core = ("orca-cli", "orca-computer-use", "orca-emulator", "orca-workspace", "orchestration")
        agents_skills = self.home / ".agents/skills"
        for name in (*core, "custom-team-skill"):
            (agents_skills / name).mkdir(parents=True)
            (agents_skills / name / "SKILL.md").write_text(name)
        (agents_skills / "synced").mkdir(parents=True)

        orca_dir = self.home / "Library/Application Support/orca"
        targets = [
            orca_dir / "antigravity-chat/config/skills",
            orca_dir / "antigravity-chat/antigravity-cli/skills",
            self.home / ".gemini/config/skills",
            orca_dir / "codex-accounts/acct-1/home/skills",
        ]
        for t in targets:
            for name in (*core, "custom-team-skill"):
                (t / name).mkdir(parents=True)

        codex_home = orca_dir / "codex-accounts/acct-1/home"
        (codex_home / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
        (codex_home / "hooks.json").write_text("{}\n", encoding="utf-8")
        (codex_home / "config.toml").write_text("[mcp_servers.xcode]\n", encoding="utf-8")

        res = doctor.check_profile_parity()
        self.assertTrue(res["all_aligned"], res)
        self.assertEqual(res["antigravity_missing_skills"], {})
        self.assertEqual(res["codex_accounts"][0]["missing_shared_skills"], [])

        # Adding a new shared skill to ~/.agents/skills without syncing it should fail parity
        (agents_skills / "new-unsynced-skill").mkdir(parents=True)
        (agents_skills / "new-unsynced-skill/SKILL.md").write_text("new")
        res2 = doctor.check_profile_parity()
        self.assertFalse(res2["all_aligned"])
        self.assertIn("new-unsynced-skill", res2["codex_accounts"][0]["missing_shared_skills"])


class TestTomlDetection(unittest.TestCase):
    CASES = {
        "[mcp_servers.xcode]\ncommand = 'x'\n": True,
        '[mcp_servers."xcode"]\ncommand = "x"\n': True,
        "[mcp_servers]\nxcode = { command = 'x' }\n": True,
        "[mcp_servers.xcode.env]\nA = '1'\n": True,
        "mcp_servers.xcode.command = 'x'\n": True,
        "mcp_servers = { xcode = { command = 'x' } }\n": True,
        "# [mcp_servers.xcode]\n[mcp_servers.other]\ncommand = 'x'\n": False,
        'note = """\n[mcp_servers.xcode]\n"""\n': False,
        "[mcp_servers.xcodebuild]\ncommand = 'x'\n": False,
    }

    def test_every_table_syntax_is_recognised_without_tomllib(self):
        with mock.patch.object(sync_profiles, "_tomllib", return_value=None):
            for text, expected in self.CASES.items():
                self.assertEqual(sync_profiles.toml_defines(text, ("mcp_servers", "xcode")), expected, text)
            self.assertIsNone(sync_profiles.toml_is_valid("x = 1\n"))

    def test_tomllib_agrees_when_available(self):
        lib = sync_profiles._tomllib()
        self.assertEqual(lib is None, sys.version_info < (3, 11))
        for text, expected in (self.CASES.items() if lib is not None else ()):
            self.assertEqual(sync_profiles.toml_defines(text, ("mcp_servers", "xcode")), expected, text)


class TestAgentAwareSync(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="sync-agents-")).resolve()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def sync(self, **env):
        import subprocess
        base = {k: v for k, v in os.environ.items() if k not in (
            "WORKER_AGENTS", "ENABLE_PRETOOLUSE_GUARDRAILS", "ENABLE_SESSION_HYGIENE", "ENABLE_XCODE_MCP")}
        full = {**base, "HOME": str(self.home), "PATH": "/usr/bin:/bin", "ENABLE_SESSION_HYGIENE": "false", **env}
        r = subprocess.run([sys.executable, str(REPO / "scripts/sync_profiles.py"), "--apply"],
                           env=full, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_gemini_cli_before_tool_hook_is_added_idempotently_and_removed_when_disabled(self):
        guard = f"{self.home}/.local/bin/agent-guard --mode hook"
        custom = {"type": "command", "command": "/opt/custom/check.sh", "timeout": 1000}
        settings = self.home / ".gemini/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"theme": "dark", "hooks": {"BeforeTool": [
            {"matcher": "write_file", "hooks": [custom]}]}}), encoding="utf-8")
        claude = self.home / ".claude/settings.json"
        claude.parent.mkdir(parents=True)
        claude.write_text("{}", encoding="utf-8")

        report = self.sync(WORKER_AGENTS="claude,gemini", ENABLE_PRETOOLUSE_GUARDRAILS="true")
        self.assertEqual(report["agents"], ["claude", "gemini"])
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        before_tool = data["hooks"]["BeforeTool"]
        self.assertEqual(before_tool[0], {"matcher": "write_file", "hooks": [custom]})
        self.assertEqual(before_tool[1], {"matcher": sync_profiles.GEMINI_GUARD_MATCHER, "hooks": [
            {"type": "command", "command": guard, "timeout": sync_profiles.GEMINI_GUARD_TIMEOUT_MS}]})
        self.assertNotIn("PreToolUse", data["hooks"])
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "WORKER_AGENTS": "claude,gemini",
                                          "ENABLE_PRETOOLUSE_GUARDRAILS": "true"}):
            wired = doctor.check_guard_hooks()["wired"]
        self.assertEqual(wired, {"claude": True, "gemini": True})
        # Codex and Antigravity are not selected: nothing is created for them.
        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse((self.home / ".gemini/config").exists())
        self.assertFalse((self.home / "Library/Application Support/orca/antigravity-chat").exists())
        self.assertEqual(self.sync(WORKER_AGENTS="claude,gemini", ENABLE_PRETOOLUSE_GUARDRAILS="true")["actions"], [])

        report = self.sync(WORKER_AGENTS="claude,gemini", ENABLE_PRETOOLUSE_GUARDRAILS="false")
        self.assertEqual({a["action"] for a in report["actions"]},
                         {"update_gemini_guard", "update_claude_guard", "update_claude_compact_anchor"})
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["hooks"],
                         {"BeforeTool": [{"matcher": "write_file", "hooks": [custom]}]})
        self.assertEqual(json.loads(claude.read_text(encoding="utf-8")), {"hooks": {}})
        self.assertEqual(self.sync(WORKER_AGENTS="claude,gemini", ENABLE_PRETOOLUSE_GUARDRAILS="false")["actions"], [])

    def test_codex_config_with_inline_xcode_table_is_left_valid(self):
        account = self.home / "Library/Application Support/orca/codex-accounts/a1/home"
        account.mkdir(parents=True)
        cfg = account / "config.toml"
        original = 'model = "gpt-5"\n[mcp_servers]\nxcode = { command = "/usr/bin/xcrun", args = ["mcpbridge"] }\n'
        cfg.write_text(original, encoding="utf-8")
        plain = self.home / "Library/Application Support/orca/codex-accounts/a2/home"
        plain.mkdir(parents=True)
        (plain / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
        report = self.sync(WORKER_AGENTS="codex", ENABLE_PRETOOLUSE_GUARDRAILS="false", ENABLE_XCODE_MCP="true")
        self.assertEqual(cfg.read_text(encoding="utf-8"), original)
        self.assertEqual([a["file"] for a in report["actions"] if a["action"] == "codex_xcode_mcp"],
                         [str(plain / "config.toml")])
        added = (plain / "config.toml").read_text(encoding="utf-8")
        self.assertEqual(added.count("[mcp_servers.xcode]"), 1)
        self.assertEqual(oct((plain / "config.toml").stat().st_mode & 0o777), "0o600")
        self.assertEqual([a for a in self.sync(WORKER_AGENTS="codex", ENABLE_PRETOOLUSE_GUARDRAILS="false",
                                               ENABLE_XCODE_MCP="true")["actions"]], [])

    def test_agents_that_are_not_installed_get_nothing(self):
        with mock.patch.object(sync_profiles.shutil, "which", return_value=None):
            self.assertEqual(sync_profiles.detect_agents(self.home), set())
            (self.home / ".claude").mkdir()
            (self.home / "Library/Application Support/orca/antigravity-chat").mkdir(parents=True)
            self.assertEqual(sync_profiles.detect_agents(self.home), {"claude", "antigravity"})
            with mock.patch.dict(os.environ, {"WORKER_AGENTS": "claude, gemini"}):
                self.assertEqual(sync_profiles.selected_agents(self.home), {"claude", "gemini", "antigravity"})
            with mock.patch.dict(os.environ, {"WORKER_AGENTS": "codex"}):
                self.assertEqual(sync_profiles.selected_agents(self.home), {"codex"})
        (self.home / ".agents/skills/demo").mkdir(parents=True)
        (self.home / ".agents/skills/demo/SKILL.md").write_text("demo", encoding="utf-8")
        report = self.sync(WORKER_AGENTS="claude", ENABLE_PRETOOLUSE_GUARDRAILS="true")
        self.assertTrue((self.home / ".claude/skills/demo").is_symlink())
        for absent in (".gemini", ".codex"):
            self.assertFalse((self.home / absent).exists(), absent)
        self.assertFalse((self.home / "Library/Application Support/orca/antigravity-chat/config").exists())
        self.assertNotIn("antigravity", report["agents"])


class TestDoctorFreshMac(DoctorCase):
    def test_missing_global_git_config_is_not_an_unknown_check(self):
        failed = {"status": "unknown", "reason": "command_failed", "exit_code": 128}
        with mock.patch.object(doctor, "command", return_value=failed):
            res = doctor.check_git_optimizations()
        self.assertEqual(res["status"], "ok")
        self.assertFalse(res["all_enabled"])
        (self.home / ".gitconfig").write_text("[broken\n", encoding="utf-8")
        with mock.patch.object(doctor, "command", return_value=failed):
            self.assertEqual(doctor.check_git_optimizations()["status"], "unknown")

    def test_missing_xcode_is_advice(self):
        with mock.patch.object(doctor, "command", return_value={"status": "unknown", "reason": "command_failed"}):
            res = doctor.check_xcode()
        self.assertEqual(res["status"], "ok")
        self.assertFalse(res["installed"])
        self.assertTrue(res["advice"])
        summary = doctor.summarize({"xcode": res})
        self.assertTrue(summary["ok"])

    def test_unknown_orca_and_other_checks_are_named_separately(self):
        summary = doctor.summarize({"runtime": {"status": "unknown"}, "swap": {"status": "unknown"}})
        self.assertEqual(summary["attention"], ["Could not be checked: swap.",
                                                "Could not be checked: runtime (is Orca installed and running?)."])

    def test_parity_skips_agents_that_are_not_installed(self):
        (self.home / ".agents/skills/demo").mkdir(parents=True)
        (self.home / ".agents/skills/demo/SKILL.md").write_text("demo", encoding="utf-8")
        with mock.patch.dict(os.environ, {"WORKER_AGENTS": "claude"}):
            res = doctor.check_profile_parity()
        self.assertTrue(res["all_aligned"], res)
        self.assertEqual(res["antigravity_missing_skills"], {})
        self.assertTrue(res["advice"], "a missing ~/.zshenv is advice, not misalignment")
        with mock.patch.dict(os.environ, {"WORKER_AGENTS": "antigravity"}):
            self.assertFalse(doctor.check_profile_parity()["all_aligned"])

    def test_codex_guard_approval(self):
        guard = f"{self.home}/.local/bin/agent-guard --mode hook"
        hooks = self.home / ".codex/hooks.json"
        hooks.parent.mkdir()
        hooks.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"hooks": [{"type": "command", "command": "/opt/other.sh"}]},
            {"hooks": [{"type": "command", "command": guard, "timeout": 5}]}]}}), encoding="utf-8")
        config = self.home / ".codex/config.toml"

        def check(toml_text):
            if toml_text is not None:
                config.write_text(toml_text, encoding="utf-8")
            with mock.patch.dict(os.environ, {"WORKER_AGENTS": "codex", "ENABLE_PRETOOLUSE_GUARDRAILS": "true"}):
                return doctor.check_guard_hooks()

        other = f'[hooks.state."{hooks}:pre_tool_use:0:0"]\nenabled = true\ntrusted_hash = "sha256:aa"\n'
        res = check(None)
        self.assertEqual(res["codex_approval"], {"codex": "unknown"})
        self.assertEqual(res["attention"], [])
        self.assertTrue(res["advice"])
        res = check(other)
        self.assertEqual(res["codex_approval"], {"codex": "needs_approval"})
        self.assertIn("/hooks", res["attention"][0])
        res = check(other + f'\n[hooks.state."{hooks}:pre_tool_use:1:0"]\nenabled = true\ntrusted_hash = "sha256:bb"\n')
        self.assertEqual(res["codex_approval"], {"codex": "approved"})
        self.assertEqual(res["attention"], [])
        res = check(other + f'\n[hooks.state."{hooks}:pre_tool_use:1:0"]\nenabled = false\ntrusted_hash = "sha256:bb"\n')
        self.assertEqual(res["codex_approval"], {"codex": "disabled"})

    def test_report_is_written_only_where_asked_and_never_overwrites(self):
        out_dir = self.home / "reports"
        out_dir.mkdir()
        first = doctor.save(str(out_dir), "{}\n")
        self.assertEqual(first.parent, out_dir.resolve())
        self.assertTrue(first.name.startswith("doctor-"))
        target = out_dir / "fixed.json"
        doctor.save(str(target), "{}\n")
        with self.assertRaises(FileExistsError):
            doctor.save(str(target), "{}\n")
        with self.assertRaises(ValueError):
            doctor.save(str(self.home / "missing/dir/x.json"), "{}\n")
        with mock.patch.object(doctor, "DEFAULT_REPORT_DIR", self.storage / "reports"):
            self.assertEqual(doctor.save("default", "{}\n").parent, (self.storage / "reports").resolve())


if __name__ == "__main__":
    unittest.main()
