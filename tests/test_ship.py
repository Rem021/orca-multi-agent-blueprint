import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import ship  # noqa: E402


class ShipTest(unittest.TestCase):
    def test_secrets_in_added_lines_only(self):
        key = "gh" + "p_" + "A" * 36
        diff = "\n".join([
            "+++ b/config.py",
            "+TOKEN = '%s'" % key,
            "-old_password = 'abcdefghijklmnop'",
            "+print('hello')",
        ])
        hits = ship.scan_secrets(diff)
        self.assertEqual(len(hits), 1)
        self.assertNotIn(key, hits[0][1])          # the finding never echoes the whole secret

    def test_secret_kinds(self):
        for line in ("+-----BEGIN " + "OPENSSH PRIVATE KEY-----", "+aws = AKIA" + "ABCDEFGHIJKLMNOP",
                     "+api_key: 'sk-" + "ant-" + "x" * 30 + "'", "+password = \"Tr0ub4dor&3xyzQ\""):
            self.assertTrue(ship.scan_secrets(line), line)
        for line in ("+password = os.environ['PW']", "+token_count = 12", "+see docs/api-key.md"):
            self.assertFalse(ship.scan_secrets(line), line)

    def test_high_risk_paths(self):
        hits = ship.scan_high_risk_paths([
            "Phayen/Phayen.entitlements",
            "Phayen/Billing/StoreKitManager.swift",
            "drizzle/0004_drop_users.sql",
            "Phayen/UI/CameraToolbar.swift",
        ])
        self.assertEqual(len(hits), 3)
        self.assertEqual(ship.scan_high_risk_paths(["Phayen/UI/CameraToolbar.swift", "README.md"]), [])

    def test_merge_method(self):
        self.assertEqual(ship.choose_method({"squash": True, "merge": True}), "squash")
        self.assertEqual(ship.choose_method({"squash": False, "merge": True}), "merge")
        with self.assertRaises(ship.Stop):
            ship.choose_method({"squash": False, "merge": True}, "squash")
        with self.assertRaises(ship.Stop):
            ship.choose_method({})

    def test_branch_refusals(self):
        self.assertIsNotNone(ship.refuse_branch("main", "main"))
        self.assertIsNotNone(ship.refuse_branch("HEAD", "main"))
        self.assertIsNotNone(ship.refuse_branch("develop", "main"))
        self.assertIsNotNone(ship.refuse_branch("feat/C-12", "main"))
        self.assertIsNone(ship.refuse_branch("codex/camera-fix", "main"))

    def test_github_slug(self):
        self.assertEqual(ship.github_slug("https://github.com/Rem021/Phayen.git"), "Rem021/Phayen")
        self.assertEqual(ship.github_slug("git@github.com:Sorasukiawa/Homelab_v2.git\n"), "Sorasukiawa/Homelab_v2")
        self.assertIsNone(ship.github_slug("/Volumes/x/repo.git"))

    def test_local_only_repo_fast_forward_and_high_risk_hold(self):
        tmp = tempfile.mkdtemp(prefix="ship-local-test-")
        old_cwd = os.getcwd()
        old_orca = ship.ORCA
        old_lock = ship.LOCK_DIR
        try:
            ship.ORCA = "/nonexistent/orca"
            ship.LOCK_DIR = os.path.join(tmp, "locks")
            main_repo = os.path.join(tmp, "BridgeTest")
            wt_repo = os.path.join(tmp, "wt-feature")
            os.makedirs(main_repo)
            subprocess.check_call(["git", "init", "-q", "-b", "main"], cwd=main_repo)
            subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=main_repo)
            subprocess.check_call(["git", "config", "user.name", "Test"], cwd=main_repo)
            with open(os.path.join(main_repo, "app.py"), "w") as f:
                f.write("x = 1\n")
            subprocess.check_call(["git", "add", "app.py"], cwd=main_repo)
            subprocess.check_call(["git", "commit", "-q", "-m", "init"], cwd=main_repo)

            subprocess.check_call(["git", "worktree", "add", "-q", "-b", "codex/feat-a", wt_repo, "main"], cwd=main_repo)
            with open(os.path.join(wt_repo, "app.py"), "w") as f:
                f.write("x = 2\n")
            subprocess.check_call(["git", "commit", "-q", "-am", "bump x"], cwd=wt_repo)

            os.chdir(wt_repo)
            rc = ship.main(["--check", "python3 -c 'import app; assert app.x == 2'"])
            self.assertEqual(rc, 0)
            with open(os.path.join(main_repo, "app.py")) as f:
                self.assertEqual(f.read(), "x = 2\n")

            subprocess.check_call(["git", "switch", "-q", "-c", "codex/entitlements", "main"], cwd=wt_repo)
            with open(os.path.join(wt_repo, "App.entitlements"), "w") as f:
                f.write("<plist/>\n")
            subprocess.check_call(["git", "add", "App.entitlements"], cwd=wt_repo)
            subprocess.check_call(["git", "commit", "-q", "-m", "add entitlements"], cwd=wt_repo)
            self.assertEqual(ship.main([]), 10)
            self.assertFalse(os.path.exists(os.path.join(main_repo, "App.entitlements")))
            self.assertEqual(ship.main(["--force-merge"]), 0)
            self.assertTrue(os.path.exists(os.path.join(main_repo, "App.entitlements")))
        finally:
            os.chdir(old_cwd)
            ship.ORCA = old_orca
            ship.LOCK_DIR = old_lock
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
