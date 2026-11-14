#!/usr/bin/env python3
"""Unit and integration tests for guard.py."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

GUARD = Path(__file__).resolve().parent.parent / "scripts/guard.py"
sys.path.insert(0, str(GUARD.parent))
import guard  # noqa: E402

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout.strip()


def make_repo(root: Path, branch: str = "main") -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", branch)
    (repo / "a.txt").write_text("1\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "first")
    return repo


class TestGuard(unittest.TestCase):
    def setUp(self):
        # The module reads ~/.orca at import time; pin the branch rules so the machine's own settings cannot leak in
        for name, value in (("PROTECTED_BRANCHES", ["main", "master"]), ("RESTRICTED_BRANCHES", ["develop", "feat/C-*"])):
            patcher = mock.patch.object(guard, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def assertAllowed(self, cmd, **kw):
        allowed, reason = guard.evaluate_command(cmd, check_volume=False, **kw)
        self.assertTrue(allowed, f"Expected command to be allowed: {cmd!r} (reason: {reason})")

    def assertBlocked(self, cmd, **kw):
        allowed, reason = guard.evaluate_command(cmd, check_volume=False, **kw)
        self.assertFalse(allowed, f"Expected command to be blocked: {cmd!r}")
        self.assertTrue(len(reason) > 5)
        return reason

    def test_safe_commands_allowed(self):
        safe_cmds = [
            "git status --short",
            "git worktree prune --dry-run",
            "git worktree prune -n",
            "git worktree remove /Volumes/AI_Workspace/AI/Codex/Worktrees/foo",
            "xcrun simctl boot 11111111-2222-3333-4444-555555555555",
            "xcrun simctl list devices",
            "xcrun simctl create 'Test Sim' 'iPhone 18 Pro Max'",   # allowed by default since 2026-11-13
            "xcrun simctl clone 11111111-2222-3333-4444-555555555555 'Clone'",
            "rm -rf /tmp/test-build-cache",
            "rm -f /Volumes/AI_Workspace/AI/Projects/NovaCam/notes.tmp",
            "git push origin codex/feature-branch",
            "git push origin main",
            "git push -u origin HEAD:codex/topic",
            "git push origin codex/develop-notes",
            "git push --force-with-lease origin codex/topic",
            "git push origin +codex/topic",
            "git push origin tag v1.2.0",
            "git clean -n",
            "git clean -nfd",
            "git reset --soft HEAD~1",
            "git -C /tmp/repo status",
        ]
        for cmd in safe_cmds:
            self.assertAllowed(cmd)

    def test_quoted_text_is_not_a_command(self):
        for cmd in [
            'git commit -m "docs: explain why git reset --hard is blocked; git clean -fdx too"',
            "grep -rn 'git worktree prune' docs/ | head",
            'echo "rm -rf /Volumes/AI_Workspace/AI/Projects/NovaCam"',
            "cat > notes.md <<'EOF'\ngit reset --hard\nrm -rf ~/.Trash/old\ngit push --force origin main\nEOF",
            'git commit -m "$(cat <<\'EOF\'\nfix(guard): block things\n\ngit reset --hard is now blocked (even with -C).\nEOF\n)"',
            "python3 - <<'PY'\nprint('git clean -fdx')\nPY",
        ]:
            self.assertAllowed(cmd)

    def test_forbidden_commands_blocked(self):
        blocked_cmds = [
            "git reset --hard HEAD~1",
            "git clean -fdx",
            "git clean --force -d",
            "git worktree prune",
            "git worktree prune --verbose",
            "rm -rf ~/.Trash/*",
            "rm -rf /Volumes/Other/.Trashes/501/item",
            "osascript -e 'tell application \"Finder\" to empty trash'",
            "rm -rf /Volumes/AI_Workspace/AI/Projects/NovaCam",
            "rm -rf /Volumes/AI_Workspace/AI/Codex/Worktrees/task-1",
            "rm -rf /Volumes/AI_Workspace/AI/Archive/2026-01-01-topic",
            "git push origin develop",
            "git push origin feat/C-123",
            "git push origin HEAD:refs/heads/develop",
        ]
        for cmd in blocked_cmds:
            self.assertBlocked(cmd)

    def test_simulator_create_blocked_only_when_opted_in(self):
        cmds = [
            "xcrun simctl create 'Test Sim' 'iPhone 18 Pro Max'",
            "xcrun simctl clone 11111111-2222-3333-4444-555555555555 'Clone'",
            "xcrun simctl --set /tmp/devices create 'Test Sim' 'iPhone 18 Pro Max'",
        ]
        for cmd in cmds:
            self.assertAllowed(cmd)
        saved = guard.BLOCK_SIM_CREATE
        guard.BLOCK_SIM_CREATE = True
        try:
            for cmd in cmds:
                self.assertBlocked(cmd)
            self.assertAllowed("xcrun simctl boot 11111111-2222-3333-4444-555555555555")
        finally:
            guard.BLOCK_SIM_CREATE = saved


    def test_root_ephemeral_clutter_blocked_while_work_tiers_allowed(self):
        proj = "/Volumes/AI_Workspace/AI/Projects/NovaCam"
        orca_wt = "/Volumes/AI_Workspace/AI/Orca/Worktrees/novacam/novacam/NovaCam-feat"
        codex_wt = "/Volumes/AI_Workspace/AI/Codex/Worktrees/task-42"

        # Blocked at project root & worktree roots
        for bad in [
            f"mkdir {proj}/tmp",
            f"mkdir -p {proj}/tmp/pdfs",
            f"mkdir -p {proj}/.worktrees/feat-x",
            f"mkdir -p {orca_wt}/scratch",
            f"mkdir {codex_wt}/test_tmp",
            "mkdir tmp",
            "mkdir -p .scratch/probe",
            "git worktree add .worktrees/feat-a -b codex/feat-a",
        ]:
            reason = self.assertBlocked(bad, cwd=proj)
            self.assertTrue("work/_scratch/" in reason or "Worktrees/" in reason, reason)

        # Allowed inside work/ tiers, nested source dirs, and system /tmp
        for ok_cmd in [
            f"mkdir -p {proj}/work/_scratch/20261114-codex-probe/tmp",
            f"mkdir -p {proj}/work/_tests/20261114-claude-smoke",
            f"mkdir -p {proj}/work/retained/dataset",
            f"mkdir -p {orca_wt}/work/_scratch/run-1",
            "mkdir -p work/_scratch/tmp",
            "mkdir -p src/tmp_fixtures",
            "mkdir -p /tmp/novacam-build",
            f"git worktree add /Volumes/AI_Workspace/AI/Codex/Worktrees/wt-ok -b codex/ok",
        ]:
            self.assertAllowed(ok_cmd, cwd=proj)

    def test_bypass_variants_blocked(self):
        project = "/Volumes/AI_Workspace/AI/Projects/NovaCam"
        for cmd in [
            "git -C /tmp/repo reset --hard",
            "git -c core.editor=true reset --hard HEAD",
            "git --git-dir=/tmp/repo/.git --work-tree=/tmp/repo clean -fd",
            "echo ok\ngit reset --hard",
            "echo ok && git reset --hard",
            "env FOO=1 git reset --hard",
            "FOO=1 BAR=2 git clean -fd",
            "sudo -u root git clean -xfd",
            "command git worktree prune",
            "/usr/bin/git reset --hard",
            'bash -c "git reset --hard"',
            "sh -lc 'git worktree prune'",
            'eval "git reset --hard"',
            'echo "$(git reset --hard)"',
            "echo `git clean -fd`",
            "bash <<EOF\ngit reset --hard\nEOF",
            "cat <<EOF | sh\ngit clean -fd\nEOF",
            "if true; then git reset --hard; fi",
            "for d in a b; do git clean -fd; done",
            f"rm -r -f {project}",
            f"rm -fR {project}",
            f"rm --recursive --force {project}",
            f"rm -r {project}",
            f"sudo rm -rf -- {project}",
            f"find . -name x | xargs echo; rm -rf '{project}'",
        ]:
            self.assertBlocked(cmd)

    def test_force_push_to_shared_branch_blocked(self):
        for cmd in [
            "git push --force origin main",
            "git push -f origin master",
            "git push origin main --force",
            "git push --force-with-lease origin main",
            "git push --force-with-lease=main origin HEAD:main",
            "git push origin +main",
            "git push origin +HEAD:refs/heads/main",
            "git push origin :main",
            "git push --delete origin main",
            "git -C /tmp/repo push -f origin main",
        ]:
            reason = self.assertBlocked(cmd)
            self.assertIn("共享分支", reason)
        # A force push that names no branch cannot be proven safe without a repository.
        self.assertBlocked("git push --force")

    def test_push_uses_repository_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            cwd = str(repo)
            self.assertAllowed("git push", cwd=cwd)
            self.assertBlocked("git push -f", cwd=cwd)
            self.assertBlocked("git push --force-with-lease origin HEAD", cwd=cwd)
            self.assertBlocked(f"cd {cwd} && git push --force")
            git(repo, "checkout", "-q", "-b", "codex/topic")
            self.assertAllowed("git push --force-with-lease", cwd=cwd)
            self.assertAllowed(f"git -C {cwd} push -f origin HEAD")
            git(repo, "checkout", "-q", "-b", "develop")
            self.assertBlocked("git push", cwd=cwd)
            self.assertBlocked("git push origin HEAD", cwd=cwd)
            git(repo, "checkout", "-q", "codex/topic")
            self.assertAllowed("git push", cwd=cwd)
            self.assertBlocked("git push --all origin", cwd=cwd)

    def test_relative_rm_resolved_against_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = root / "AI/Projects/Foo"
            (project / "node_modules").mkdir(parents=True)
            (root / "AI/Codex/Worktrees/Foo-topic").mkdir(parents=True)
            projects = str(project.parent)
            self.assertBlocked("rm -rf Foo", cwd=projects)
            self.assertBlocked("rm -rf ./*", cwd=projects)
            self.assertBlocked(f"cd {projects} && rm -rf Foo")
            self.assertBlocked("rm -rf .", cwd=str(project))
            self.assertBlocked("rm -rf ../Foo", cwd=str(project))
            self.assertBlocked("rm -rf Projects", cwd=str(root / "AI"))
            self.assertBlocked("rm -rf Codex", cwd=str(root / "AI"))
            self.assertBlocked("rm -rf AI", cwd=str(root))
            self.assertBlocked("rm -rf Foo-topic", cwd=str(root / "AI/Codex/Worktrees"))
            # Build artefacts inside a project stay removable with a relative path.
            self.assertAllowed("rm -rf node_modules", cwd=str(project))
            self.assertAllowed("rm -rf build 2>/dev/null", cwd=str(project))
            self.assertAllowed("rm -rf Foo", cwd=str(root))

    def test_hook_stdin_protocol(self):
        # 1. Blocked payload
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git reset --hard"}})
        r = subprocess.run(
            [sys.executable, str(GUARD), "--mode", "hook"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(r.returncode, 2)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

        # 2. Allowed payload
        payload_ok = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
        r_ok = subprocess.run(
            [sys.executable, str(GUARD), "--mode", "hook"],
            input=payload_ok,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(r_ok.returncode, 0)
        self.assertEqual(json.loads(r_ok.stdout), {})

        # 3. Malformed / non-object payloads never crash the hook
        for raw in ("not json", "[1, 2]", '{"tool_input": null}'):
            r_bad = subprocess.run(
                [sys.executable, str(GUARD), "--mode", "hook"], input=raw, capture_output=True, text=True, timeout=5
            )
            self.assertEqual(r_bad.returncode, 0, raw)

    def test_antigravity_hook_protocol(self):
        def run(command):
            payload = json.dumps({"conversationId": "c1", "toolCall": {"args": {"CommandLine": command}}})
            return subprocess.run(
                [sys.executable, str(GUARD), "--mode", "hook"], input=payload, capture_output=True, text=True, timeout=5
            )

        denied = run("git clean -fdx")
        self.assertEqual(denied.returncode, 0)
        self.assertEqual(json.loads(denied.stdout)["decision"], "deny")
        allowed = run("ls -la")
        self.assertEqual(allowed.returncode, 0)
        self.assertEqual(json.loads(allowed.stdout), {"decision": "ask"})

    def test_hook_uses_payload_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp).resolve() / "AI/Projects"
            (projects / "Foo").mkdir(parents=True)
            payload = json.dumps({"tool_name": "Bash", "cwd": str(projects), "tool_input": {"command": "rm -rf Foo"}})
            r = subprocess.run(
                [sys.executable, str(GUARD), "--mode", "hook"], input=payload, capture_output=True, text=True, timeout=5
            )
            self.assertEqual(r.returncode, 2)

    def test_volume_mount_and_uuid_verification(self):
        ok, msg = guard.verify_volume_mounted_and_uuid(
            "/Volumes/NonExistentDrive_99999",
            "00000000-0000-0000-0000-000000000000",
        )
        self.assertFalse(ok)
        self.assertIn("未挂载", msg)

        allowed, reason = guard.evaluate_command(
            "ls /Volumes/NonExistentDrive_99999/AI/Projects",
            check_volume=True,
            vol_path="/Volumes/NonExistentDrive_99999",
            expected_uuid="00000000-0000-0000-0000-000000000000",
        )
        self.assertFalse(allowed)
        self.assertIn("未挂载", reason)

        # A similarly named sibling path is not the guarded volume.
        allowed, _ = guard.evaluate_command(
            "ls /Volumes/NonExistentDrive_99999-backup",
            check_volume=True,
            vol_path="/Volumes/NonExistentDrive_99999",
        )
        self.assertTrue(allowed)

        # The working directory alone is enough to trigger the check.
        allowed, reason = guard.evaluate_command(
            "touch notes.md",
            check_volume=True,
            vol_path="/Volumes/NonExistentDrive_99999",
            cwd="/Volumes/NonExistentDrive_99999/AI/Projects/Foo",
        )
        self.assertFalse(allowed)

    def test_fake_mountpoint_directory_fails_closed(self):
        uuid = "00000000-0000-0000-0000-000000000000"
        # A plain directory at /Volumes/<Drive> with an AI/ folder inside is NOT a mounted volume.
        with mock.patch.object(guard.os.path, "ismount", return_value=False), mock.patch.object(
            guard.Path, "is_dir", return_value=True
        ):
            ok, msg = guard.verify_volume_mounted_and_uuid("/Volumes/FakeStub", uuid)
        self.assertFalse(ok)
        self.assertIn("未挂载", msg)

        def diskutil(found_uuid, mount_point="/Volumes/FakeStub"):
            import plistlib

            return plistlib.dumps({"VolumeUUID": found_uuid, "MountPoint": mount_point})

        with mock.patch.object(guard.os.path, "ismount", return_value=True), mock.patch.object(
            guard.Path, "is_dir", return_value=True
        ):
            with mock.patch.object(guard.subprocess, "check_output", return_value=diskutil("11111111-1111-1111-1111-111111111111")):
                ok, msg = guard.verify_volume_mounted_and_uuid("/Volumes/FakeStub", uuid)
                self.assertFalse(ok)
                self.assertIn("UUID", msg)
            with mock.patch.object(guard.subprocess, "check_output", return_value=diskutil(uuid)):
                self.assertTrue(guard.verify_volume_mounted_and_uuid("/Volumes/FakeStub", uuid)[0])
                self.assertTrue(guard.verify_volume_mounted_and_uuid("/Volumes/FakeStub/", uuid)[0])
            with mock.patch.object(guard.subprocess, "check_output", side_effect=OSError("diskutil busy")):
                self.assertTrue(guard.verify_volume_mounted_and_uuid("/Volumes/FakeStub", uuid)[0])

    def run_pre_push(self, repo, line):
        return subprocess.run(
            [sys.executable, str(GUARD), "--mode", "pre-push", "origin", "https://example.invalid/r.git"],
            input=line + "\n",
            capture_output=True,
            text=True,
            timeout=30,
            cwd=repo,
            env=GIT_ENV,
        )

    def test_pre_push_blocks_history_rewrite_on_shared_branch(self):
        zero = "0" * 40
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            first = git(repo, "rev-parse", "HEAD")
            (repo / "a.txt").write_text("2\n")
            git(repo, "commit", "-q", "-am", "second")
            second = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-q", "-b", "side", first)
            (repo / "b.txt").write_text("x\n")
            git(repo, "add", "b.txt")
            git(repo, "commit", "-q", "-m", "diverged")
            diverged = git(repo, "rev-parse", "HEAD")

            fast_forward = self.run_pre_push(repo, f"refs/heads/main {second} refs/heads/main {first}")
            self.assertEqual(fast_forward.returncode, 0, fast_forward.stderr)
            new_branch = self.run_pre_push(repo, f"refs/heads/main {second} refs/heads/main {zero}")
            self.assertEqual(new_branch.returncode, 0, new_branch.stderr)
            rewrite = self.run_pre_push(repo, f"refs/heads/side {diverged} refs/heads/main {second}")
            self.assertEqual(rewrite.returncode, 1)
            delete = self.run_pre_push(repo, f"(delete) {zero} refs/heads/main {second}")
            self.assertEqual(delete.returncode, 1)
            task_rewrite = self.run_pre_push(repo, f"refs/heads/side {diverged} refs/heads/codex/topic {second}")
            self.assertEqual(task_rewrite.returncode, 0, task_rewrite.stderr)
            restricted = self.run_pre_push(repo, f"refs/heads/side {diverged} refs/heads/feat/C-9 {zero}")
            self.assertEqual(restricted.returncode, 1)

    def test_install_pre_push_never_overwrites_foreign_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            hook = repo / ".git/hooks/pre-push"
            self.assertEqual(guard.install_pre_push(str(repo)), 0)
            self.assertIn("agent-guard", hook.read_text())
            self.assertTrue(os.access(hook, os.X_OK))
            self.assertEqual(guard.install_pre_push(str(repo)), 0)  # idempotent
            hook.write_text("#!/bin/sh\necho custom\n")
            self.assertEqual(guard.install_pre_push(str(repo)), 1)
            self.assertEqual(hook.read_text(), "#!/bin/sh\necho custom\n")
            self.assertEqual(guard.install_pre_push(tmp), 1)  # not a repository


class TestInstalledGuardConfig(unittest.TestCase):
    """The installed copy lives in ~/.local/bin with no checkout beside it; its settings come from ~/.orca."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / "home"
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".orca").mkdir()
        self.installed = self.home / ".local/bin/agent-guard"
        self.installed.write_bytes(GUARD.read_bytes())
        self.repo = make_repo(Path(self.tmp.name).resolve())

    def check(self, command: str, **env) -> int:
        clean = {
            k: v
            for k, v in GIT_ENV.items()
            if not k.startswith(("GUARD_", "AI_VOLUME_", "STORAGE_MODE", "BACKUP_MEDIA_", "EXTERNAL_VOLUME_"))
        }
        clean.update({"HOME": str(self.home), "STORAGE_MODE": "internal", **env})
        return subprocess.run(
            [sys.executable, str(self.installed), "--mode", "check", "--command", command],
            cwd=self.repo, env=clean, capture_output=True, text=True, timeout=10,
        ).returncode

    def write(self, name: str, text: str):
        (self.home / ".orca" / name).write_text(text, encoding="utf-8")

    def test_defaults_apply_without_any_config(self):
        self.assertEqual(self.check("git push origin develop"), 2)
        self.assertEqual(self.check("git push origin feature/login"), 0)

    def test_guard_env_reaches_the_installed_copy(self):
        self.write("guard.env", 'GUARD_RESTRICTED_BRANCHES="secret/* staging"\nGUARD_PROTECTED_BRANCHES="trunk"\n')
        self.assertEqual(self.check("git push origin staging"), 2)
        self.assertEqual(self.check("git push origin secret/keys"), 2)
        self.assertEqual(self.check("git push origin develop"), 0)
        self.assertEqual(self.check("git push --force origin trunk"), 2)

    def test_none_disables_the_restricted_list(self):
        self.write("guard.env", 'GUARD_RESTRICTED_BRANCHES="none"\n')
        self.assertEqual(self.check("git push origin develop"), 0)
        self.assertEqual(self.check("git push --force origin main"), 2)

    def test_guard_env_wins_over_backup_config_and_environment_wins_over_both(self):
        self.write("backup-config.env", 'export GUARD_RESTRICTED_BRANCHES="release"\n')
        self.assertEqual(self.check("git push origin release"), 2)
        self.write("guard.env", 'GUARD_RESTRICTED_BRANCHES="hotfix"\n')
        self.assertEqual(self.check("git push origin release"), 0)
        self.assertEqual(self.check("git push origin hotfix"), 2)
        self.assertEqual(self.check("git push origin hotfix", GUARD_RESTRICTED_BRANCHES="none"), 0)

    def test_internal_mode_protects_workspace_and_external_cold_volume(self):
        ws = self.home / "AI-Workspace"
        (ws / "AI/Projects/NovaCam").mkdir(parents=True)
        self.write(
            "guard.env",
            f'STORAGE_MODE="internal"\nAI_VOLUME_PATH="{ws}"\nAI_VOLUME_UUID="LOCAL"\nBACKUP_MEDIA_PATH="/Volumes/Media_SSD/Work"\n',
        )
        for blocked in (
            f"rm -rf {ws}",
            f"rm -rf {ws}/AI",
            f"rm -rf {ws}/AI/Projects/NovaCam",
            "rm -rf /Volumes/Media_SSD",
            "rm -rf /Volumes/Media_SSD/Work",
            "rm -rf /Volumes/Media_SSD/Frozen-20261112",
            "rm -rf /Volumes/Media_SSD/AI/Migration",
        ):
            self.assertEqual(self.check(blocked), 2, blocked)
        self.assertEqual(self.check("rm -rf /tmp/scratch-build-test"), 0)

    def test_shell_default_syntax_is_expanded(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("DEMO_UNSET", None)
            os.environ["DEMO_SET"] = "from-env"
            self.assertEqual(guard._expand_defaults("${DEMO_UNSET:-main master}", {}), "main master")
            self.assertEqual(guard._expand_defaults("${DEMO_UNSET:-}", {}), "")
            self.assertEqual(guard._expand_defaults("${DEMO_SET:-fallback}", {}), "from-env")
            self.assertEqual(guard._expand_defaults("${DEMO_UNSET}/AI", {"DEMO_UNSET": "/Volumes/X"}), "/Volumes/X/AI")
        self.assertEqual(guard._split_list("none"), [])
        self.assertEqual(guard._split_list("develop, feat/C-*"), ["develop", "feat/C-*"])


if __name__ == "__main__":
    unittest.main()
