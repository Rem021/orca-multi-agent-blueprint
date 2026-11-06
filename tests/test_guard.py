#!/usr/bin/env python3
"""Unit and integration tests for guard.py."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "scripts/guard.py"
sys.path.insert(0, str(GUARD.parent))
import guard  # noqa: E402


class TestGuard(unittest.TestCase):
    def test_safe_commands_allowed(self):
        safe_cmds = [
            "git status --short",
            "git worktree prune --dry-run",
            "git worktree remove /Volumes/AI_Workspace/AI/Codex/Worktrees/foo",
            "xcrun simctl boot 11111111-2222-3333-4444-555555555555",
            "rm -rf /tmp/test-build-cache",
            "git push origin codex/feature-branch",
        ]
        for cmd in safe_cmds:
            allowed, reason = guard.evaluate_command(cmd, check_volume=False)
            self.assertTrue(allowed, f"Expected safe command to be allowed: {cmd} (reason: {reason})")

    def test_forbidden_commands_blocked(self):
        blocked_cmds = [
            "git reset --hard HEAD~1",
            "git clean -fdx",
            "git worktree prune",
            "xcrun simctl create 'Test Sim' 'iPhone 18 Pro Max'",
            "xcrun simctl clone 11111111-2222-3333-4444-555555555555 'Clone'",
            "rm -rf ~/.Trash/*",
            "rm -rf /Volumes/AI_Workspace/AI/Projects/NovaCam",
            "rm -rf /Volumes/AI_Workspace/AI/Codex/Worktrees/task-1",
            "git push origin develop",
            "git push origin feat/C-123",
        ]
        for cmd in blocked_cmds:
            allowed, reason = guard.evaluate_command(cmd, check_volume=False)
            self.assertFalse(allowed, f"Expected forbidden command to be blocked: {cmd}")
            self.assertTrue(len(reason) > 5)

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


if __name__ == "__main__":
    unittest.main()
