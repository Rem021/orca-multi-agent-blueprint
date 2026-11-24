#!/usr/bin/env python3
"""Unit and integration tests for guard.py."""
import contextlib
import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

GUARD = Path(__file__).resolve().parent.parent / "scripts/guard.py"
sys.path.insert(0, str(GUARD.parent))

# Hermetic: never read the machine's ~/.orca/*.env or GUARD_* / AI_VOLUME_* settings, neither at import time
# nor in the guard subprocesses the tests start.
_SCRUB_PREFIXES = (
    "GUARD_", "AI_VOLUME_", "BACKUP_MEDIA_", "EXTERNAL_VOLUME_", "STORAGE_MODE",
    "PRIMARY_SIMULATOR_", "SECONDARY_SIMULATOR_", "GIT_CONFIG_",
)
_HOME_DIR = tempfile.TemporaryDirectory(prefix="guard-test-home-")
_HOME = str(Path(_HOME_DIR.name).resolve())


def hermetic_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB_PREFIXES)}
    env["HOME"] = _HOME
    env.update(extra)
    return env


with mock.patch.dict(os.environ, hermetic_env(), clear=True):
    import guard  # noqa: E402

_ENV_PATCH = mock.patch.dict(os.environ, hermetic_env(), clear=True)


def setUpModule():
    _ENV_PATCH.start()


def tearDownModule():
    _ENV_PATCH.stop()
    _HOME_DIR.cleanup()


GIT_ENV = {
    **hermetic_env(),
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
        for name, value in (("PROTECTED_BRANCHES", ["main", "master"]), ("RESTRICTED_BRANCHES", ["develop", "secret/*"])):
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
            "xcrun simctl create 'Test Sim' 'Test Phone'",   # allowed by default since 2026-11-13
            "xcrun simctl clone 11111111-2222-3333-4444-555555555555 'Clone'",
            "rm -rf /tmp/test-build-cache",
            "rm -f /Volumes/AI_Workspace/AI/Projects/ExampleApp/notes.tmp",
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
            'echo "rm -rf /Volumes/AI_Workspace/AI/Projects/ExampleApp"',
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
            "rm -rf /Volumes/AI_Workspace/AI/Projects/ExampleApp",
            "rm -rf /Volumes/AI_Workspace/AI/Codex/Worktrees/task-1",
            "rm -rf /Volumes/AI_Workspace/AI/Archive/2026-01-01-topic",
            "git push origin develop",
            "git push origin secret/123",
            "git push origin HEAD:refs/heads/develop",
        ]
        for cmd in blocked_cmds:
            self.assertBlocked(cmd)

    def test_simulator_create_blocked_only_when_opted_in(self):
        cmds = [
            "xcrun simctl create 'Test Sim' 'Test Phone'",
            "xcrun simctl clone 11111111-2222-3333-4444-555555555555 'Clone'",
            "xcrun simctl --set /tmp/devices create 'Test Sim' 'Test Phone'",
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
        proj = "/Volumes/AI_Workspace/AI/Projects/ExampleApp"
        orca_wt = "/Volumes/AI_Workspace/AI/Orca/Worktrees/exampleapp/exampleapp/ExampleApp-feat"
        codex_wt = "/Volumes/AI_Workspace/AI/Codex/Worktrees/task-42"

        orca_wt_d1 = "/Volumes/AI_Workspace/AI/Orca/Worktrees/Plugins"
        orca_wt_d2 = "/Volumes/AI_Workspace/AI/Orca/Worktrees/Game/alpha"
        orca_wt_d3_asym = "/Volumes/AI_Workspace/AI/Orca/Worktrees/Org/Repo/feature"
        codex_wt_d2 = "/Volumes/AI_Workspace/AI/Codex/Worktrees/Repo/main-lane"

        # Blocked at project root, depth 1/2/3 worktree roots, and symlink bypasses
        for bad in [
            f"mkdir {proj}/tmp",
            f"mkdir -p {proj}/tmp/pdfs",
            f"mkdir -p {proj}/.worktrees/feat-x",
            f"mkdir -p {orca_wt}/scratch",
            f"mkdir -p {orca_wt_d1}/tmp",
            f"mkdir -p {orca_wt_d2}/tmp",
            f"mkdir -p {orca_wt_d3_asym}/scratch",
            f"mkdir -p {codex_wt_d2}/test_tmp",
            f"mkdir {codex_wt}/test_tmp",
            "mkdir tmp",
            "mkdir -p .scratch/probe",
            "ln -s /tmp/ext tmp",
            f"ln -sfn /tmp/ext {orca_wt_d2}/scratch",
            "git worktree add .worktrees/feat-a -b codex/feat-a",
        ]:
            reason = self.assertBlocked(bad, cwd=proj)
            self.assertTrue("work/_scratch/" in reason or "Worktrees/" in reason, reason)

        # Allowed inside work/ tiers, nested source dirs, and system /tmp across all worktree depths
        for ok_cmd in [
            f"mkdir -p {proj}/work/_scratch/20261114-codex-probe/tmp",
            f"mkdir -p {proj}/work/_tests/20261114-claude-smoke",
            f"mkdir -p {proj}/work/retained/dataset",
            f"mkdir -p {orca_wt}/work/_scratch/run-1",
            f"mkdir -p {orca_wt_d1}/work/_scratch/tmp",
            f"mkdir -p {orca_wt_d2}/work/_scratch/tmp",
            f"mkdir -p {orca_wt_d2}/src/tmp",
            f"mkdir -p {orca_wt_d3_asym}/work/_tests/tmp",
            "mkdir -p work/_scratch/tmp",
            "mkdir -p src/tmp_fixtures",
            "ln -s /tmp/ext work/_scratch/link-out",
            "mkdir -p /tmp/exampleapp-build",
            f"git worktree add /Volumes/AI_Workspace/AI/Codex/Worktrees/wt-ok -b codex/ok",
        ]:
            self.assertAllowed(ok_cmd, cwd=proj)

        # Verify on-disk `.git` worktree root check for both `mkdir tmp` and relative `rm -rf .`
        with tempfile.TemporaryDirectory() as tmpd:
            wt_git = Path(tmpd).resolve() / "AI/Orca/Worktrees/Game/alpha"
            (wt_git / "node_modules").mkdir(parents=True)
            (wt_git / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
            self.assertBlocked("mkdir tmp", cwd=str(wt_git))
            self.assertAllowed("mkdir -p work/_scratch/tmp", cwd=str(wt_git))
            self.assertBlocked("rm -rf .", cwd=str(wt_git))
            self.assertBlocked("rm -rf alpha", cwd=str(wt_git.parent))
            self.assertAllowed("rm -rf node_modules", cwd=str(wt_git))

    def test_bypass_variants_blocked(self):
        project = "/Volumes/AI_Workspace/AI/Projects/ExampleApp"
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
            self.assertIn("shared branch", reason)
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
        self.assertEqual(allowed.stdout.strip(), "")  # allow = no output (never "ask")

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
        self.assertIn("not mounted", msg)

        allowed, reason = guard.evaluate_command(
            "ls /Volumes/NonExistentDrive_99999/AI/Projects",
            check_volume=True,
            vol_path="/Volumes/NonExistentDrive_99999",
            expected_uuid="00000000-0000-0000-0000-000000000000",
        )
        self.assertFalse(allowed)
        self.assertIn("not mounted", reason)

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
        self.assertIn("not mounted", msg)

        def diskutil(found_uuid, mount_point="/Volumes/FakeStub"):
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
            # Lower-case configured UUIDs match diskutil's upper-case output.
            with mock.patch.object(guard.subprocess, "check_output", return_value=diskutil(uuid.upper())):
                self.assertTrue(guard.verify_volume_mounted_and_uuid("/Volumes/FakeStub", uuid.lower())[0])
            # diskutil failing or reporting no VolumeUUID can no longer be read as "verified" (fail-closed).
            with mock.patch.object(guard.subprocess, "check_output", side_effect=OSError("diskutil busy")):
                ok, msg = guard.verify_volume_mounted_and_uuid("/Volumes/FakeStub2", uuid)
                self.assertFalse(ok)
                self.assertIn("could not read", msg)
            with mock.patch.object(guard.subprocess, "check_output", return_value=plistlib.dumps({"MountPoint": "/Volumes/FakeStub3"})):
                self.assertFalse(guard.verify_volume_mounted_and_uuid("/Volumes/FakeStub3", uuid)[0])

    def run_pre_push(self, repo, line):
        return subprocess.run(
            [sys.executable, str(GUARD), "--mode", "pre-push", "origin", "https://example.invalid/r.git"],
            input=line + "\n",
            capture_output=True,
            text=True,
            timeout=30,
            cwd=repo,
            env={**GIT_ENV, "GUARD_RESTRICTED_BRANCHES": "develop secret/*"},
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
            restricted = self.run_pre_push(repo, f"refs/heads/side {diverged} refs/heads/secret/9 {zero}")
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
        # No branch is restricted by default; main/master stay protected against force pushes.
        self.assertEqual(self.check("git push origin develop"), 0)
        self.assertEqual(self.check("git push origin feature/login"), 0)
        self.assertEqual(self.check("git push --force origin main"), 2)
        self.assertEqual(guard._split_list(""), [])

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
        (ws / "AI/Projects/ExampleApp").mkdir(parents=True)
        self.write(
            "guard.env",
            f'STORAGE_MODE="internal"\nAI_VOLUME_PATH="{ws}"\nAI_VOLUME_UUID="LOCAL"\nBACKUP_MEDIA_PATH="/Volumes/Media_SSD/Work"\n',
        )
        for blocked in (
            f"rm -rf {ws}",
            f"rm -rf {ws}/AI",
            f"rm -rf {ws}/AI/Projects/ExampleApp",
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
        self.assertEqual(guard._split_list("develop, secret/*"), ["develop", "secret/*"])




class TestBatchAAdversarialHardening(unittest.TestCase):
    """Regression suite covering all Batch-A audit findings on guard.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake_ai = Path(self.tmp.name).resolve() / "fake" / "AI"
        self.proj = self.fake_ai / "Projects" / "DemoProj"
        (self.proj / ".git").mkdir(parents=True)
        (self.proj / "node_modules").mkdir()
        (self.proj / "work" / "_scratch").mkdir(parents=True)
        (self.proj / "outputs" / "subrepo" / ".git").mkdir(parents=True)
        self.orca_wt = self.fake_ai / "Orca" / "Worktrees" / "Repo" / "wt1"
        self.orca_wt.mkdir(parents=True)
        (self.orca_wt / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
        for name, value in (
            ("PROTECTED_BRANCHES", ["main", "master"]),
            ("RESTRICTED_BRANCHES", ["develop", "secret/*"]),
        ):
            patcher = mock.patch.object(guard, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_blocks_ancestors_comments_dynamic_vars_and_2level_orca(self):
        rm = "rm " + "-rf"
        reset = "git reset " + "--hard"
        must_block = [
            (f"{rm} ~", None),
            (f"{rm} ~/", None),
            (f"{rm} ~/*", None),
            (f"{rm} $HOME/*", None),
            (f"{rm} /Users", None),
            (f"cd {self.proj} && {rm} *", None),
            (f"{rm} ./*", str(self.proj)),
            (f"{rm} .git", str(self.proj)),
            (f'{rm} "$(pwd)"', str(self.proj)),
            (f"{rm} $(git rev-parse --show-toplevel)", str(self.proj)),
            (f'{rm} "$PWD"', str(self.proj)),
            (f'D={self.proj}; {rm} "$D"', None),
            (f'{rm} "$UNSET_DANGEROUS_TARGET"', str(self.proj)),
            (f"ls -d {self.proj} | xargs {rm}", None),
            (f"find {self.proj} -delete", None),
            (f"find {self.proj.parent} -maxdepth 1 -name DemoProj -exec {rm} {{}} +", None),
            (f'python3 -c "import shutil; shutil.rmtree(\x27{self.proj}\x27)"', None),
            (f"rsync -a --delete /tmp/empty/ {self.proj}/", None),
            (f"# Don't keep old cache\n{rm} {self.proj}", None),
            (f"# it's fine\n{reset}", None),
            (f"cd {self.orca_wt.parent} && {rm} wt1", None),
            (f"{rm} ./*", str(self.orca_wt)),
            (f"{rm} {self.proj}/outputs/subrepo", None),
            (f"{rm} {self.proj}/work", None),
            (f"{rm} ~/AI-Workspace/Data", None),
            ("git worktree remove --force " + str(self.orca_wt), None),
            ("git checkout -- .", str(self.proj)),
            ("git restore --staged --worktree .", str(self.proj)),
            ("git stash clear", str(self.proj)),
            ("git reset --har HEAD", None),
            ("git push --forc origin main", None),
            ("git push --force origin tag v1 main", None),
            ("git push origin 'refs/heads/*:refs/heads/*'", None),
            ("git push origin :", None),
            ("git -c clean.requireForce=false clean -dx", None),
            ("xcrun " + reset, None),
            (f"bash -c -- '{reset}'", None),
            ("gh api -X DELETE repos/o/r/git/refs/heads/main", None),
            ('mkdir -p "$(pwd)/tmp"', str(self.proj)),
            ("mkdir -p $PWD/tmp", str(self.proj)),
            ("mkdir -p {build,tmp}", str(self.proj)),
            ("cp -R src tmp", str(self.proj)),
            ("find ~/.Trash -mindepth 1 -delete", None),
        ]
        for cmd, cwd in must_block:
            ok, _ = guard.evaluate_command(cmd, check_volume=False, cwd=cwd)
            self.assertFalse(ok, f"Expected BLOCK for: {cmd!r} (cwd={cwd})")


    def test_r3_subshell_cmdsub_conditional_and_failed_cd_never_confuse_cwd(self):
        """R3: Subshells, command substitutions, background `&`, conditionals, and `;`/`||` after `cd` keep outer cwd."""
        rm = "rm " + "-rf"
        proj_cwd = str(self.proj)
        must_block = [
            (f"(cd /tmp); {rm} .", proj_cwd),
            (f"(cd /tmp/safe_sub) && {rm} .", proj_cwd),
            (f'echo "$(cd /tmp)"; {rm} .', proj_cwd),
            (f"echo `cd /tmp`; {rm} .", proj_cwd),
            (f"if false; then cd /tmp/safe_sub; fi; {rm} .", proj_cwd),
            (f"if true; then echo hi; else cd /tmp/safe_sub; fi; {rm} .", proj_cwd),
            (f"while false; do cd /tmp/safe_sub; done; {rm} .", proj_cwd),
            (f"cd /nonexistent_dir_xyz; {rm} .", proj_cwd),
            (f"cd /tmp/safe_sub || {rm} .", proj_cwd),
            (f"cd /tmp/safe_sub & {rm} .", proj_cwd),
            (f"cd /tmp && {rm} .", proj_cwd),
            (f"(D=/tmp/safe_sub); {rm} \"${{D:-.}}\"", proj_cwd),
        ]
        for cmd, cwd in must_block:
            ok, _ = guard.evaluate_command(cmd, check_volume=False, cwd=cwd)
            self.assertFalse(ok, f"Expected BLOCK for scope/control-flow command: {cmd!r}")
            # End-to-end CLI `--mode hook` subprocess verification (real JSON hook protocol + exit code 2)
            hook_env = {**os.environ, "STORAGE_MODE": "internal", "AI_VOLUME_PATH": str(self.fake_ai.parent)}
            proc = subprocess.run(
                [sys.executable, str(GUARD), "--mode", "hook"],
                input=json.dumps({"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": cmd}}),
                capture_output=True,
                text=True,
                env=hook_env,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 2, f"Expected CLI hook exit 2 for {cmd!r}, got {proc.returncode}: {proc.stderr}")

        # Genuine `cd <safe-tmp-subdir> && rm -rf .` or subshell wholly inside `/tmp/<subpath>` remains allowed!
        must_allow = [
            (f"cd /tmp/safe_build_subdir && {rm} .", proj_cwd),
            (f"(cd /tmp/safe_build_subdir && {rm} .)", proj_cwd),
            (f"cd {self.proj}/work/_scratch/run1 && {rm} .", None),
        ]
        for cmd, cwd in must_allow:
            ok, reason = guard.evaluate_command(cmd, check_volume=False, cwd=cwd)
            self.assertTrue(ok, f"Expected ALLOW for {cmd!r}: {reason}")

    def test_allows_legitimate_scratch_builds_comments_and_dry_runs(self):
        rm = "rm " + "-rf"
        reset = "git reset " + "--hard"
        clean = "git clean " + "-fdx"
        must_allow = [
            (f"{rm} {self.proj}/node_modules", None),
            (f"{rm} node_modules", str(self.proj)),
            (f"{rm} {self.proj}/work/_scratch/20261114-codex-demo", None),
            ("rg -n EmptyTrash scripts/", None),
            (f"echo done # cleanup; {reset} is banned", None),
            (f"# step: never && {clean}\nls", None),
            ("git push -n --force origin main", None),
            (f"cat > notes.md <<'EOF'  # bash notes\n{reset}\nEOF", None),
        ]
        for cmd, cwd in must_allow:
            ok, reason = guard.evaluate_command(cmd, check_volume=False, cwd=cwd)
            self.assertTrue(ok, f"Expected ALLOW for: {cmd!r} -> {reason}")

    def test_file_target_hook_and_malformed_payload_and_latency(self):
        ok_bad, _ = guard.evaluate_file_target(f"{self.proj}/tmp/x.txt", check_volume=False, cwd=str(self.proj))
        self.assertFalse(ok_bad)
        ok_good, _ = guard.evaluate_file_target(f"{self.proj}/work/_scratch/x.txt", check_volume=False, cwd=str(self.proj))
        self.assertTrue(ok_good)

        for bad_payload in (
            {"tool_name": "Bash", "tool_input": "not-a-dict"},
            {"tool_name": "Bash", "tool_input": {"command": {"x": 1}}},
            {"toolCall": {"args": "str"}, "conversationId": "c"},
        ):
            r = subprocess.run(
                [sys.executable, str(GUARD), "--mode", "hook"],
                input=json.dumps(bad_payload),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(r.returncode, 0, r.stderr)

        w_payload = {
            "tool_name": "Write",
            "cwd": str(self.proj),
            "tool_input": {"file_path": f"{self.proj}/tmp/x.txt", "content": "hi"},
        }
        rw = subprocess.run(
            [sys.executable, str(GUARD), "--mode", "hook"],
            input=json.dumps(w_payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(rw.returncode, 2)

        deep = "bash -c '" + ("(" * 2000) + "ls" + (")" * 2000) + "'; git reset --hard"
        ok_deep, _ = guard.evaluate_command(deep, check_volume=False)
        self.assertFalse(ok_deep)

    def test_blocks_host_orca_quit_and_launchctl_submit(self):
        must_block = [
            'osascript -e \'tell application "Orca" to quit\'',
            'osascript -e \'tell application "/Applications/Orca.app" to quit\'',
            'osascript -e \'tell application id "com.stablyai.orca" to quit\'',
            'osascript -l JavaScript -e \'Application("Orca").quit()\'',
            "osascript <<'APPLESCRIPT'\ntell application \"Orca\"\n  quit\nend tell\nAPPLESCRIPT",
            "killall Orca",
            "killall -9 'Orca Helper'",
            "pkill Orca",
            "pkill -x Orca",
            "pkill -f '/Applications/Orca.app/Contents/MacOS/Orca'",
            "pkill -9 -f Orca.app",
            "launchctl submit -l local.orca.gemini-resource-fix -- /usr/bin/python3 fix.py",
            "python3 -c 'import subprocess; subprocess.run([\"launchctl\", \"submit\", \"-l\", \"bad\", \"--\", \"true\"])'",
            'python3 -c \'import subprocess; subprocess.run(["osascript", "-e", "tell application \\"Orca\\" to quit"])\'',
        ]
        for cmd in must_block:
            ok, reason = guard.evaluate_command(cmd, check_volume=False)
            self.assertFalse(ok, f"Expected BLOCK for: {cmd!r}")
            self.assertTrue(len(reason) > 5)

        must_allow = [
            'osascript -e \'tell application "Orca" to activate\'',
            'osascript -e \'tell application "Safari" to quit\'',
            "pkill -f orca-test-fixture-server",
            "pkill -f '/Applications/Orca.app/Contents/Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler'",
            "kill 12121",
            "launchctl list",
            "launchctl print gui/501/local.orca.gemini-resource-fix",
            "launchctl remove local.orca.gemini-resource-fix",
            "launchctl bootout gui/501/local.orca.gemini-resource-fix",
        ]
        for cmd in must_allow:
            ok, reason = guard.evaluate_command(cmd, check_volume=False)
            self.assertTrue(ok, f"Expected ALLOW for: {cmd!r} -> {reason}")

        with mock.patch.object(guard, "ALLOW_HOST_ORCA_QUIT", True):
            ok_quit, _ = guard.evaluate_command('osascript -e \'tell application "Orca" to quit\'', check_volume=False)
            self.assertTrue(ok_quit)
            # launchctl submit remains blocked even if host Orca quit is explicitly opted in
            ok_sub, _ = guard.evaluate_command("launchctl submit -l job -- /bin/true", check_volume=False)
            self.assertFalse(ok_sub)



    def test_antigravity_view_file_blocks_binary_images_and_unbounded_large_files(self):
        png_file = self.proj / "work/_scratch/IMG_4303.png"
        png_file.parent.mkdir(parents=True, exist_ok=True)
        png_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x11\x22\x33" * 64)

        magic_no_ext = self.proj / "work/_scratch/raw_macho_blob"
        magic_no_ext.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x01\x02\x03\x04" * 64)

        nul_blob = self.proj / "work/_scratch/firmware.dat"
        nul_blob.write_bytes(b"HDR!" + b"\x00" * 512)

        big_log = self.proj / "work/_scratch/huge.jsonl"
        big_log.write_text(("{\"ok\": true, \"msg\": \"" + ("x" * 100) + "\"}\n") * 12000, encoding="utf-8")

        minified_js = self.proj / "work/_scratch/bundle.js"
        minified_js.write_text("var a=" + ("1+" * 600000) + "0;\n", encoding="utf-8")

        tmp_note = self.proj / "tmp/existing.txt"
        tmp_note.parent.mkdir(parents=True, exist_ok=True)
        tmp_note.write_text("normal utf-8 text\nline 2\n", encoding="utf-8")

        # Binary extension, magic header, NUL-dense blob, unbounded >1MB, and >1MB single-line slice must all be denied
        for path, extra_args in (
            (png_file, {}),
            (magic_no_ext, {}),
            (nul_blob, {}),
            (big_log, {}),
            (big_log, {"StartLine": 1, "EndLine": 5000}),
            (minified_js, {"StartLine": 1, "EndLine": 5}),
        ):
            payload = {
                "toolCall": {"name": "view_file", "args": {"AbsolutePath": str(path), **extra_args}},
                "conversationId": "conv-test",
            }
            proc = subprocess.run(
                [sys.executable, str(GUARD), "--mode", "hook"],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout.strip())
            self.assertEqual(out.get("decision"), "deny", f"Expected deny for {path} {extra_args}: {out}")
            self.assertIn("view_file", out.get("reason", ""))

        # Bounded line slice on >1MB multi-line file AND reading an existing text file under proj/tmp must be allowed
        for path, extra_args in (
            (big_log, {"StartLine": 10, "EndLine": 60}),
            (tmp_note, {}),
        ):
            payload = {
                "toolCall": {"name": "default_api:view_file", "args": {"AbsolutePath": str(path), **extra_args}},
                "conversationId": "conv-test",
            }
            proc = subprocess.run(
                [sys.executable, str(GUARD), "--mode", "hook"],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "", f"Expected allow (no output) for {path} {extra_args}")


    def test_v230_guardrail_bypass_hardening(self):
        def assert_blocked(cmd):
            allowed, reason = guard.evaluate_command(cmd, check_volume=False)
            self.assertFalse(allowed, f"Expected blocked: {cmd!r}")
            self.assertTrue(len(reason) > 5)

        # 1. Non-recursive rm on ~/.Trash/* must be blocked
        assert_blocked("rm -f ~/.Trash/file.txt")
        # 2. Path traversal inside dynamic /tmp/$VAR must be blocked
        assert_blocked('rm -rf "/tmp/../../Users/$USER/$UNKNOWN"')
        # 3. git checkout . / HEAD . without `--` must be blocked
        assert_blocked("git checkout .")
        assert_blocked("git checkout HEAD .")
        # 4. Flag spoofing after `--` or inside option values must not bypass clean/restore/worktree prune
        assert_blocked("git clean -fd -- -n")
        assert_blocked("git clean -fd -e-n")
        assert_blocked("git restore . -- -S")
        assert_blocked("git worktree prune -- -n")
        # 5. Stdin/heredoc osascript quitting Orca & combined python3 -uc flags must be blocked
        assert_blocked("osascript <<< 'tell application \"Orca\" to quit'")
        assert_blocked("python3 -uc \"import shutil; shutil.rmtree('/Users/alice/.Trash/x')\"")
        # 6. Glob patterns in PROTECTED_BRANCHES must work via _is_protected
        orig_prot = guard.PROTECTED_BRANCHES
        try:
            guard.PROTECTED_BRANCHES = frozenset({"main", "release/*"})
            self.assertTrue(guard._is_protected("release/1.2"))
            assert_blocked("gh api -X DELETE repos/example-org/demo/git/refs/heads/release/1.2")
            assert_blocked("git push --force origin release/1.2")
        finally:
            guard.PROTECTED_BRANCHES = orig_prot


    def test_v240_sensitive_credential_read_blocked(self):
        # Shell readers must block private keys and credential vaults
        for cmd in (
            "cat ~/.ssh/id_ed25519",
            "head -n 5 ~/.ssh/id_rsa",
            "base64 -i /tmp/AuthKey_ABC12345.p8",
            "cat /Users/alice/certs/dist.p12",
            "cat ~/.aws/credentials",
            "cat /tmp/keychain-secrets.json",
        ):
            ok, reason = guard.evaluate_command(cmd, check_volume=False)
            self.assertFalse(ok, f"Expected blocked: {cmd!r}")
            self.assertIn("Blocked reading", reason)

        # Public keys, templates, and normal .env files must NOT be blocked
        for cmd in (
            "cat ~/.ssh/id_ed25519.pub",
            "cat /tmp/AuthKey_ABC12345.p8.example",
            "cat /tmp/project/.env",
            "ls -l ~/.ssh/id_ed25519",
        ):
            ok, reason = guard.evaluate_command(cmd, check_volume=False)
            self.assertTrue(ok, f"Unexpectedly blocked: {cmd!r}: {reason}")

        # Claude Read & Antigravity view_file hooks must both block private key reads
        guard_script = str(Path(guard.__file__).resolve())
        for payload, exp_rc in (
            ({"tool_name": "Read", "tool_input": {"file_path": "/Users/alice/.ssh/id_ed25519"}}, 2),
            ({"conversationId": "c1", "toolCall": {"name": "view_file", "args": {"AbsolutePath": "/tmp/AuthKey_1234.p8"}}}, 0),
        ):
            proc = subprocess.run(
                [sys.executable, guard_script, "--mode", "hook"],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(proc.returncode, exp_rc, proc.stdout + proc.stderr)
            out = json.loads(proc.stdout.strip())
            self.assertIn(out.get("decision"), ("block", "deny"), out)

    def test_v240_cross_worktree_collision_warn_and_pbxproj_block(self):
        with tempfile.TemporaryDirectory(prefix="guard-wt-col-") as tmp:
            root = Path(tmp).resolve()
            main_repo = root / "repo"
            wt_b = root / "wt-b"
            main_repo.mkdir()
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=main_repo, check=True, env=env)
            (main_repo / "App.swift").write_text("struct App {}\n", encoding="utf-8")
            (main_repo / "App.xcodeproj").mkdir()
            (main_repo / "App.xcodeproj/project.pbxproj").write_text("// pbx\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=main_repo, check=True, env=env)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=main_repo, check=True, env=env)
            subprocess.run(["git", "worktree", "add", "-q", "-b", "codex/feat-b", str(wt_b), "main"], cwd=main_repo, check=True, env=env)

            # Dirty App.swift and project.pbxproj in wt_b
            (wt_b / "App.swift").write_text("struct App { var x = 1 }\n", encoding="utf-8")
            (wt_b / "App.xcodeproj/project.pbxproj").write_text("// pbx changed\n", encoding="utf-8")

            # Editing App.swift in main_repo -> warn (non-blocking additionalContext)
            sev, msg = guard.check_worktree_file_collision(str(main_repo / "App.swift"), cwd=str(main_repo), cache_ttl=0)
            self.assertEqual(sev, "warn", msg)
            self.assertIn("codex/feat-b", msg)

            # Editing project.pbxproj in main_repo -> block
            sev2, msg2 = guard.check_worktree_file_collision(str(main_repo / "App.xcodeproj/project.pbxproj"), cwd=str(main_repo), cache_ttl=0)
            self.assertEqual(sev2, "block", msg2)
            self.assertIn("project.pbxproj", msg2)

            # Hook mode end-to-end: normal file returns exit 0 + additionalContext, never permissionDecision "allow"
            guard_script = str(Path(guard.__file__).resolve())
            p_warn = subprocess.run(
                [sys.executable, guard_script, "--mode", "hook"],
                input=json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(main_repo / "App.swift")}, "cwd": str(main_repo)}),
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(p_warn.returncode, 0, p_warn.stderr)
            out_w = json.loads(p_warn.stdout.strip())
            self.assertNotIn("permissionDecision", out_w["hookSpecificOutput"])
            self.assertIn("codex/feat-b", out_w["hookSpecificOutput"]["additionalContext"])

            # Hook mode end-to-end: pbxproj returns exit 2 + block
            p_blk = subprocess.run(
                [sys.executable, guard_script, "--mode", "hook"],
                input=json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(main_repo / "App.xcodeproj/project.pbxproj")}, "cwd": str(main_repo)}),
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(p_blk.returncode, 2, p_blk.stdout + p_blk.stderr)

    def test_v240_compact_anchor_output(self):
        with tempfile.TemporaryDirectory(prefix="guard-anchor-") as tmp:
            repo = Path(tmp).resolve()
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "AGENT_TEST_UDID": "SIM-1234"}
            subprocess.run(["git", "init", "-q", "-b", "codex/anchor-test"], cwd=repo, check=True, env=env)
            (repo / "a.txt").write_text("hi\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
            subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=repo, check=True, env=env)
            (repo / "a.txt").write_text("dirty\n", encoding="utf-8")

            guard_script = str(Path(guard.__file__).resolve())
            proc = subprocess.run(
                [sys.executable, guard_script, "--mode", "compact-anchor"],
                input=json.dumps({"cwd": str(repo)}),
                capture_output=True, text=True, env=env, timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout.strip())
            ctx_str = out["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertIn("branch=codex/anchor-test", ctx_str)
            self.assertIn("a.txt", ctx_str)
            self.assertIn("test_sim=SIM-1234", ctx_str)


class TestAuditRegressions(unittest.TestCase):
    """Bypasses, false positives and hook-protocol defects found in the guard audit."""

    RM = "rm " + "-rf"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="guard-audit-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ai = self.root / "AI"
        self.proj = self.ai / "Projects" / "Foo"
        for d in (self.proj / "src", self.proj / "docs", self.ai / "Data" / "cache", self.ai / "Archive" / "2026-01-01-x"):
            d.mkdir(parents=True)
        (self.proj / ".git").mkdir()
        self.other = self.root / "other"
        self.other.mkdir()
        self.data_link = self.root / "datalink"
        self.data_link.symlink_to(self.ai / "Data")
        for name, value in (("PROTECTED_BRANCHES", ["main", "master"]), ("RESTRICTED_BRANCHES", ["develop", "secret/*"])):
            patcher = mock.patch.object(guard, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def blocked(self, cmd, cwd=None, **kw):
        ok, reason = guard.evaluate_command(cmd, check_volume=kw.pop("check_volume", False), cwd=cwd, **kw)
        self.assertFalse(ok, f"Expected BLOCK: {cmd!r} (cwd={cwd})")
        return reason

    def allowed(self, cmd, cwd=None, **kw):
        ok, reason = guard.evaluate_command(cmd, check_volume=kw.pop("check_volume", False), cwd=cwd, **kw)
        self.assertTrue(ok, f"Expected ALLOW: {cmd!r} (cwd={cwd}): {reason}")

    def repo(self, branch="main", name="repo"):
        repo = self.root / name
        repo.mkdir()
        git(repo, "init", "-q", "-b", branch)
        git(repo, "commit", "-q", "--allow-empty", "-m", "first")
        return repo

    def test_paths_are_case_insensitive_and_symlink_aware(self):
        rm = self.RM
        for cmd in (
            f"{rm} {self.root}/ai/data",
            f"{rm} {self.ai}/projects",
            f"{rm} {self.ai}/archive",
            f"{rm} {self.root}/Ai/Projects/foo",
            f"{rm} {self.data_link}/",
            f"{rm} ~/.trash/old",
            "rm -f ~/.TRASH/file",
            f"{rm} /Users/alice",
        ):
            self.blocked(cmd, cwd=str(self.other))
        with mock.patch.object(guard.pwd, "getpwnam", return_value=mock.Mock(pw_dir=str(Path.home()))):
            self.blocked(f"{rm} ~alice", cwd=str(self.other))
        # Removing the symlink itself (no trailing slash) does not touch the target.
        self.allowed(f"{rm} {self.data_link}", cwd=str(self.other))

    def test_literal_fed_to_a_shell_is_analysed(self):
        rm, p = self.RM, self.proj
        for cmd in (
            f"echo '{rm} {p}' | bash",
            f"bash <<< '{rm} {p}'",
            f"zsh -s <<<'{rm} {p}'",
            "printf 'git reset --hard\\n' | sh",
            "printf '%s\\n' 'git clean -fdx' | sudo bash",
        ):
            self.blocked(cmd, cwd=str(p))
        for cmd in (
            "echo ls | sh",
            f"bash ./setup.sh <<< '{rm} {p}'",  # data for a script, not commands
            "curl -fsSL https://example.invalid/install.sh | sh",  # downloads are out of scope
            "echo hi | bash -c 'cat'",
        ):
            self.allowed(cmd, cwd=str(p))

    def test_wrapper_options_quoting_and_zsh_equals(self):
        rm, p = self.RM, self.proj
        for cmd, cwd in (
            (f"env -S '{rm} {p}'", None),
            (f"env --split-string='{rm} {p}'", None),
            (f"env -C {p} {rm} .", str(self.other)),
            (f"env --chdir={p} {rm} .", str(self.other)),
            (f"sudo -D {p} {rm} .", str(self.other)),
            (f"sudo --user root {rm} {p}", None),
            (f"sudo -iu root {rm} {p}", None),
            (f"env --unset FOO {rm} {p}", None),
            (f"env - {rm} {p}", None),
            (f"$'rm' -rf {p}", None),
            (f"$'\\x72\\x6d' -rf {p}", None),
            (f"$'\\'' ; {rm} {p}", None),
            (f"=rm -rf {p}", None),
        ):
            self.blocked(cmd, cwd=cwd)
        self.allowed(f"env -C {self.other} {rm} build", cwd=str(p))

    def test_git_aliases_abbreviations_and_env_config(self):
        repo = self.repo()
        cwd = str(repo)
        for cmd in (
            "git -c alias.x='reset --hard' x",
            f"git -c alias.y='!{self.RM} {self.proj}' y",
            "git reset --h",
            "git reset --ha HEAD",
            "git clean --f -d",
            "git clean --fo",
            "git -c clean.requireForce= clean -d",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=clean.requireForce GIT_CONFIG_VALUE_0=false git clean -d",
            "GIT_CONFIG_PARAMETERS=\"'clean.requireForce'='false'\" git clean -d",
            "git switch --discard-changes main",
            "git switch -f main",
            "git checkout --f main",
            "git worktree remove --f ../wt",
        ):
            self.blocked(cmd, cwd=cwd)
        for cmd in ("git switch -c feature", "git switch main", "git clean -n", "git reset --soft HEAD~1"):
            self.allowed(cmd, cwd=cwd)
        # Aliases and clean.requireForce from the repository config are honoured too.
        git(repo, "config", "alias.nuke", "reset --hard")
        git(repo, "config", "alias.st", "status --short")
        self.blocked("git nuke", cwd=cwd)
        self.allowed("git st", cwd=cwd)
        git(repo, "config", "clean.requireForce", "false")
        self.blocked("git clean -d", cwd=cwd)

    def test_brace_expansion_is_bounded_and_fails_closed(self):
        p = self.proj
        start = time.time()
        self.blocked(f"{self.RM} {p} \"x" + "{a,b}" * 24 + "\"")
        self.blocked(f"{self.RM} {p} \"x" + "{a,b}" * 1100 + "\"")
        self.assertLess(time.time() - start, 2.0)
        many = ",".join(f"n{i}" for i in range(80))
        self.blocked(f"{self.RM} {self.ai}/{{{many},Projects}}")  # collapses to a glob over the AI root
        self.allowed(f"{self.RM} /tmp/guard-test/{{a,b,c}}")
        self.assertEqual(guard._expand_braces("a{b,c}d{e,f}"), ["abde", "abdf", "acde", "acdf"])
        with mock.patch.object(guard, "_check_script", side_effect=RecursionError):
            self.assertEqual(self.blocked("ls"), guard.REASON_TOO_COMPLEX)

    def test_find_xargs_rsync_deletion_forms(self):
        rm, p = self.RM, self.proj
        for cmd, cwd in (
            (f"find {p} -maxdepth 0 -ok rm -rf {{}} ';'", None),
            (f"find {p} -maxdepth 0 -okdir rm -rf {{}} ';'", None),
            (f"find {p} -maxdepth 0 -exec sudo rm -rf {{}} +", None),
            (f"find {p} -maxdepth 0 -exec sh -c '{rm} \"$1\"' _ {{}} ';'", None),
            (f"echo {p} | xargs {rm} ./dummy", str(self.other)),
            (f"echo {p} | xargs -I {{}} {rm} {{}}", str(self.other)),
            (f"rsync -a --del /tmp/empty/ {p}/", None),
            ("find . -name '.git' -exec rm -rf {} +", str(p)),
            ("find . -name '*' -delete", str(p)),
            ("find . -mtime +7 -delete", str(p)),
            ("find . -name docs -exec rm -rf {} +", str(p)),
            ("find . -name '*.pyc' -o -name x -delete", str(p)),
            (f"find {self.ai}/Projects -name Foo -exec rm -rf {{}} +", None),
        ):
            self.blocked(cmd, cwd=cwd)
        for cmd, cwd in (
            ("find . -name '*.pyc' -delete", str(p)),
            ("find src -name '*.o' -delete", str(p)),
            ("find . -type d -empty -delete", str(p)),
            ("find . -name node_modules -prune -exec rm -rf {} +", str(p)),
            ("find . -name '__pycache__' -exec sh -c 'rm -rf \"$1\"' _ {} ';'", str(p)),
            ("find . -type f -exec grep -l TODO {} +", str(p)),
        ):
            self.allowed(cmd, cwd=cwd)

    def test_volume_check_follows_variables_and_relative_paths(self):
        vol = "/Volumes/NonExistentDrive_99999"
        kw = {"check_volume": True, "vol_path": vol, "expected_uuid": "AUTO"}
        self.blocked('mkdir -p "$AI_VOLUME_PATH/AI/Projects/x"', **kw)
        self.blocked("mkdir -p ${AI_VOLUME_PATH}/AI/x", **kw)
        self.blocked("cd /Volumes && mkdir -p NonExistentDrive_99999/AI/x", **kw)
        self.allowed("ls /Volumes", **kw)

    def test_hook_never_auto_allows_and_fails_closed_on_internal_errors(self):
        def run_in_process(payload):
            stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()), encoding="utf-8")
            out = io.StringIO()
            with mock.patch.object(guard.sys, "stdin", stdin), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = guard.run_hook()
            return rc, out.getvalue().strip()

        with mock.patch.object(guard, "_run_hook_payload", side_effect=RuntimeError("boom")):
            rc, out = run_in_process({"tool_name": "Bash", "tool_input": {"command": "ls"}})
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
            rc, out = run_in_process({"tool_name": "Write", "tool_input": {"file_path": "/tmp/x"}})
            self.assertEqual(rc, 2)
            rc, out = run_in_process({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}})
            self.assertEqual((rc, out), (0, "{}"))
            rc, out = run_in_process({"conversationId": "c", "toolCall": {"name": "view_file", "args": {"AbsolutePath": "/tmp/x"}}})
            self.assertEqual((rc, out), (0, ""))
            rc, out = run_in_process({"conversationId": "c", "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}})
            self.assertEqual((rc, json.loads(out)["decision"]), (0, "deny"))
        # A NUL byte in a view_file path is handled, not a traceback.
        proc = subprocess.run(
            [sys.executable, str(GUARD), "--mode", "hook"],
            input=json.dumps({"conversationId": "c", "toolCall": {"name": "view_file", "args": {"AbsolutePath": "/tmp/a\u0000b"}}}),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, ""), proc.stderr)
        # Non-UTF-8 stdin is decoded, not a crash.
        proc = subprocess.run(
            [sys.executable, str(GUARD), "--mode", "hook"],
            input=b'{"tool_name":"Bash","tool_input":{"command":"git reset --hard \xff"}}',
            capture_output=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 2)

    def test_gemini_cli_before_tool_payloads(self):
        def run(payload):
            return subprocess.run(
                [sys.executable, str(GUARD), "--mode", "hook"],
                input=json.dumps({"hook_event_name": "BeforeTool", "cwd": str(self.proj), **payload}),
                capture_output=True, text=True, timeout=10,
            )

        denied = run({"tool_name": "run_shell_command", "tool_input": {"command": "git reset --hard", "dir_path": "src"}})
        self.assertEqual(denied.returncode, 2, denied.stderr)
        out = json.loads(denied.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "BeforeTool")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("git reset --hard", denied.stderr)
        relative = run({"tool_name": "run_shell_command", "tool_input": {"command": f"{self.RM} ..", "dir_path": "src"}})
        self.assertEqual(relative.returncode, 2)
        ok = run({"tool_name": "run_shell_command", "tool_input": {"command": "ls -la"}})
        self.assertEqual((ok.returncode, ok.stdout.strip()), (0, "{}"))
        write = run({"tool_name": "write_file", "tool_input": {"file_path": str(self.proj / "tmp/x.txt"), "content": "x"}})
        self.assertEqual(write.returncode, 2)
        read = run({"tool_name": "read_file", "tool_input": {"absolute_path": str(Path.home() / ".ssh/id_rsa")}})
        self.assertEqual(read.returncode, 2)

    def test_push_and_gh_api_gaps(self):
        develop = self.repo("develop", "dev-repo")
        task = self.repo("task", "task-repo")
        for cmd, cwd in (
            ("git push -ofoo origin +main", task),
            ("git push -ofn origin +main", task),  # `n` belongs to the push option, not --dry-run
            ("git push origin HEAD~0:refs/heads/leak", develop),
            ("git push origin develop^:refs/heads/leak", develop),
            ("git push --prune --all origin", task),
            ("git push --mirror origin", task),
            ("gh api -X PATCH repos/o/r/git/refs/heads/main -F force=true -f sha=abc", task),
            ("gh api --method PATCH repos/o/r/git/refs/heads/main --input body.json", task),
            ("gh api -X DELETE repos/o/r/git/refs/heads/ma%69n", task),
        ):
            self.blocked(cmd, cwd=str(cwd))
        for cmd, cwd in (
            ("git push -o ci.skip origin task", task),
            ("git push --prune origin task", task),
            ("gh api -X PATCH repos/o/r/git/refs/heads/task -F force=true", task),
            ("gh api repos/o/r/git/refs/heads/main", task),
        ):
            self.allowed(cmd, cwd=str(cwd))

    def test_common_cleanup_patterns_are_not_false_positives(self):
        rm, p = self.RM, self.proj
        for cmd, cwd in (
            (f't=$(mktemp -d) && cp src/a "$t"/ && {rm} "$t"', str(p)),
            (f'{rm} "$(mktemp -d)"', str(p)),
            (f'{rm} "${{TMPDIR:-/tmp}}/guard-test"', str(p)),
            (f"{rm} .git/rebase-merge", str(p)),
            (f"{rm} .git/rebase-apply", str(p)),
        ):
            self.allowed(cmd, cwd=cwd)
        self.blocked(f"{rm} .git/objects", cwd=str(p))
        self.blocked(f'{rm} "$(mktemp -d)/../.."', cwd=str(p))

    def test_finder_empty_and_secret_reads(self):
        for cmd in (
            "osascript -e 'tell application \"Finder\" to empty'",
            "osascript -l JavaScript -e 'Application(\"Finder\").empty()'",
            "cat <~/.ssh/id_rsa",
            "cat < ~/.ssh/id_rsa",
            "cat<~/.ssh/id_rsa",
            "grep '' ~/.ssh/id_ed25519",
            "grep -f ~/.ssh/id_rsa notes.txt",
            "nl ~/.ssh/id_rsa",
            "dd if=$HOME/.ssh/id_rsa",
            "while read l; do echo $l; done < ~/.ssh/id_rsa",
            "rg --file=$HOME/.aws/credentials .",
        ):
            self.blocked(cmd, cwd=str(self.other))
        for cmd in (
            "osascript -e 'tell application \"Finder\" to get name of startup disk'",
            "grep id_rsa ~/.ssh/config",
            "grep -rn secret docs/",
            "wc -l < notes.txt",
            "cat ~/.ssh/id_rsa.pub",
        ):
            self.allowed(cmd, cwd=str(self.other))

    def test_sibling_scan_handles_non_ascii_paths(self):
        main_repo = self.repo("main", "col-repo")
        name = "Übersicht.swift"
        (main_repo / name).write_text("a\n", encoding="utf-8")
        git(main_repo, "add", name)
        git(main_repo, "commit", "-q", "-m", "add")
        sibling = self.root / "col-wt"
        git(main_repo, "worktree", "add", "-q", "-b", "side", str(sibling))
        (sibling / name).write_text("changed\n", encoding="utf-8")
        sev, msg = guard.check_worktree_file_collision(str(main_repo / name), cwd=str(main_repo), cache_ttl=0)
        self.assertEqual(sev, "warn", msg)
        self.assertIn("side", msg)


if __name__ == "__main__":
    unittest.main()
