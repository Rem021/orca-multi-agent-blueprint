import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import ship  # noqa: E402


class ShipTest(unittest.TestCase):
    def setUp(self):
        self._orig_orca = ship.ORCA
        self._orig_config = ship.CONFIG
        self._orig_lock = ship.LOCK_DIR
        self._tmp = tempfile.mkdtemp(prefix="ship-hermetic-")
        ship.ORCA = "/nonexistent/orca"
        ship.CONFIG = os.path.join(self._tmp, "nonexistent-ship.json")
        ship.LOCK_DIR = os.path.join(self._tmp, "locks")

    def tearDown(self):
        ship.ORCA = self._orig_orca
        ship.CONFIG = self._orig_config
        ship.LOCK_DIR = self._orig_lock
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_secrets_in_added_lines_only_and_redacted(self):
        key = "gh" + "p_" + "A" * 36
        diff = "\n".join([
            "+++ b/config.py",
            "+TOKEN = '%s'" % key,
            "-old_password = 'abcdefghijklmnop'",
            "+print('hello')",
        ])
        hits = ship.scan_secrets(diff)
        self.assertEqual(len(hits), 1)
        self.assertNotIn(key, hits[0][1])
        self.assertNotIn("AAAA", hits[0][1])

        bare_hits = ship.scan_secrets("+" + key)
        self.assertEqual(len(bare_hits), 1)
        self.assertEqual(bare_hits[0][1], "<REDACTED>")

    def test_secret_kinds_comprehensive(self):
        val24 = "abcdEFGH" + "ijklMNOP" + "qrstUVWX"
        must_hit = (
            "+-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "+aws = AKIA" + "ABCDEFGHIJKLMNOP",
            "+aws_secret_" + "access_key = '" + "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY'",
            "+api_" + "key: 'sk-" + "ant-" + "x" * 30 + "'",
            "+pass" + 'word = "' + "Tr0ub4dor" + '&3xyzQ"',
            "+OPENAI_" + "KEY_BACKUP=" + val24,
            "+DB_PASS" + 'WORD = "' + ("hunter2" * 3) + '"',
            "+client_" + 'secret = "' + val24[:20] + '"',
            "+STRIPE_SECRET_" + 'KEY = "rk_' + "live_" + val24[:20] + '1234"',
            "+key = sk_" + "live_" + val24[:20] + "1234",
            "+  pass" + "word: " + ("hunter2" * 3),
            '+  "tok' + 'en": "' + val24[:22] + '"',
            "+Author" + "ization: Bear" + "er eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxIn0.abc",
        )
        for line in must_hit:
            self.assertTrue(ship.scan_secrets(line), line)

        must_allow = (
            "+password = os.environ['PW']",
            "+TOKEN = None",
            "+token_count = 12",
            "+see docs/api-key.md",
            '+api_key = "${OPENAI_API_KEY}"',
        )
        for line in must_allow:
            self.assertFalse(ship.scan_secrets(line), line)

    def test_high_risk_paths_comprehensive(self):
        must_hold = [
            "Phayen/Phayen.entitlements",
            "App/App.Entitlements",
            "Phayen/Billing/StoreKitManager.swift",
            "App/Billing/PurchaseManager.swift",
            "App/IAP/SubscriptionStore.swift",
            "drizzle/0004_drop_users.sql",
            "prisma/migrations/20261114_init/migration.sql",
            "prisma/schema.prisma",
            "alembic/versions/abc_init.py",
            "db/migrate/20261114_add.rb",
            "App/Model.xcdatamodeld/Model.xcdatamodel/contents",
            "App.xcodeproj/project.pbxproj",
            "App/Info.plist",
            "Certs/dist.p12",
            "App/embedded.mobileprovision",
            ".github/workflows/ci.yml",
            "Config/Signing.xcconfig",
        ]
        for p in must_hold:
            self.assertTrue(ship.scan_high_risk_paths([p]), p)
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

    def _make_fixture(self, name):
        root = tempfile.mkdtemp(prefix=name + "-", dir=self._tmp)
        main_repo = os.path.join(root, "Main")
        wt_repo = os.path.join(root, "wt")
        os.makedirs(main_repo)
        subprocess.check_call(["git", "init", "-q", "-b", "main"], cwd=main_repo)
        subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=main_repo)
        subprocess.check_call(["git", "config", "user.name", "Test"], cwd=main_repo)
        with open(os.path.join(main_repo, "app.py"), "w") as f:
            f.write("x = 1\n")
        subprocess.check_call(["git", "add", "app.py"], cwd=main_repo)
        subprocess.check_call(["git", "commit", "-q", "-m", "init"], cwd=main_repo)
        subprocess.check_call(["git", "worktree", "add", "-q", "-b", "codex/feat", wt_repo, "main"], cwd=main_repo)
        return main_repo, wt_repo

    def _run_ship_in(self, cwd, argv):
        old_cwd = os.getcwd()
        buf = io.StringIO()
        os.chdir(cwd)
        try:
            with redirect_stdout(buf):
                rc = ship.main(argv)
        finally:
            os.chdir(old_cwd)
        return rc, buf.getvalue()

    def test_local_only_repo_fast_forward_and_high_risk_hold(self):
        main_repo, wt_repo = self._make_fixture("ff")
        with open(os.path.join(wt_repo, "app.py"), "w") as f:
            f.write("x = 2\n")
        subprocess.check_call(["git", "commit", "-q", "-am", "bump x"], cwd=wt_repo)

        rc, _ = self._run_ship_in(wt_repo, ["--check", "python3 -c 'import app; assert app.x == 2'"])
        self.assertEqual(rc, 0)
        with open(os.path.join(main_repo, "app.py")) as f:
            self.assertEqual(f.read(), "x = 2\n")

        subprocess.check_call(["git", "switch", "-q", "-c", "codex/entitlements", "main"], cwd=wt_repo)
        with open(os.path.join(wt_repo, "App.entitlements"), "w") as f:
            f.write("<plist/>\n")
        subprocess.check_call(["git", "add", "App.entitlements"], cwd=wt_repo)
        subprocess.check_call(["git", "commit", "-q", "-m", "add entitlements"], cwd=wt_repo)
        self.assertEqual(self._run_ship_in(wt_repo, [])[0], 10)
        self.assertFalse(os.path.exists(os.path.join(main_repo, "App.entitlements")))
        self.assertEqual(self._run_ship_in(wt_repo, ["--force-merge"])[0], 0)
        self.assertTrue(os.path.exists(os.path.join(main_repo, "App.entitlements")))

    def test_secret_added_then_scrubbed_in_later_commit_is_blocked(self):
        main_repo, wt_repo = self._make_fixture("secret-hist")
        tok = "gh" + "p_" + "B" * 36
        with open(os.path.join(wt_repo, "cfg.py"), "w") as f:
            f.write("TOKEN = '%s'\n" % tok)
        subprocess.check_call(["git", "add", "cfg.py"], cwd=wt_repo)
        subprocess.check_call(["git", "commit", "-q", "-m", "add cfg"], cwd=wt_repo)
        with open(os.path.join(wt_repo, "cfg.py"), "w") as f:
            f.write("TOKEN = None\n")
        subprocess.check_call(["git", "commit", "-q", "-am", "scrub"], cwd=wt_repo)

        rc, out = self._run_ship_in(wt_repo, [])
        self.assertEqual(rc, 5)
        self.assertNotIn(tok, out)
        hist = subprocess.check_output(["git", "log", "-p", "main"], cwd=main_repo, text=True)
        self.assertNotIn(tok, hist)

    def test_high_risk_rename_untracked_file_dry_run_upstream_and_mainclone(self):
        # 1. High-risk file renamed away -> MUST hold (exit 10)
        main_repo, wt_repo = self._make_fixture("rename")
        with open(os.path.join(main_repo, "App.entitlements"), "w") as f:
            f.write("<plist/>\n")
        subprocess.check_call(["git", "add", "App.entitlements"], cwd=main_repo)
        subprocess.check_call(["git", "commit", "-q", "-m", "ent"], cwd=main_repo)
        subprocess.check_call(["git", "rebase", "-q", "main"], cwd=wt_repo)
        os.makedirs(os.path.join(wt_repo, "Legacy"))
        subprocess.check_call(["git", "mv", "App.entitlements", "Legacy/App.entitlements.disabled"], cwd=wt_repo)
        subprocess.check_call(["git", "commit", "-q", "-m", "move ent"], cwd=wt_repo)
        self.assertEqual(self._run_ship_in(wt_repo, [])[0], 10)

        # 2. Untracked source file -> refused (exit 2) before false-green check
        main2, wt2 = self._make_fixture("untracked")
        with open(os.path.join(wt2, "app.py"), "w") as f:
            f.write("import helper\nx = helper.VALUE\n")
        subprocess.check_call(["git", "commit", "-q", "-am", "use helper"], cwd=wt2)
        with open(os.path.join(wt2, "helper.py"), "w") as f:
            f.write("VALUE = 2\n")
        self.assertEqual(self._run_ship_in(wt2, ["--check", "python3 -c 'import app'"])[0], 2)

        # 3. Non-origin remote (`upstream`) -> refused (exit 2), never silently treated as local-only
        main3, wt3 = self._make_fixture("upstream")
        bare = os.path.join(self._tmp, "upstream-remote.git")
        subprocess.check_call(["git", "init", "-q", "--bare", bare])
        subprocess.check_call(["git", "remote", "add", "upstream", bare], cwd=main3)
        with open(os.path.join(wt3, "app.py"), "w") as f:
            f.write("x = 3\n")
        subprocess.check_call(["git", "commit", "-q", "-am", "bump"], cwd=wt3)
        self.assertEqual(self._run_ship_in(wt3, [])[0], 2)

        # 4. Run from main clone on a task branch -> switches back to main & deletes task branch
        main4, _ = self._make_fixture("mainclone")
        subprocess.check_call(["git", "switch", "-q", "-c", "codex/inplace"], cwd=main4)
        with open(os.path.join(main4, "app.py"), "w") as f:
            f.write("x = 4\n")
        subprocess.check_call(["git", "commit", "-q", "-am", "inplace"], cwd=main4)
        self.assertEqual(self._run_ship_in(main4, [])[0], 0)
        head_ref = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=main4, text=True).strip()
        self.assertEqual(head_ref, "main")
        branches = subprocess.check_output(["git", "branch", "--list", "codex/inplace"], cwd=main4, text=True).strip()
        self.assertEqual(branches, "")

        # 5. `--dry-run` never rewrites HEAD
        main5, wt5 = self._make_fixture("dryrun")
        with open(os.path.join(wt5, "b.py"), "w") as f:
            f.write("y = 1\n")
        subprocess.check_call(["git", "add", "b.py"], cwd=wt5)
        subprocess.check_call(["git", "commit", "-q", "-m", "b"], cwd=wt5)
        before_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=wt5, text=True).strip()
        with open(os.path.join(main5, "c.py"), "w") as f:
            f.write("z = 1\n")
        subprocess.check_call(["git", "add", "c.py"], cwd=main5)
        subprocess.check_call(["git", "commit", "-q", "-m", "c"], cwd=main5)
        self.assertEqual(self._run_ship_in(wt5, ["--dry-run"])[0], 0)
        after_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=wt5, text=True).strip()
        self.assertEqual(before_sha, after_sha)


if __name__ == "__main__":
    unittest.main()
