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
        self._orig_restricted = ship.RESTRICTED
        self._orig_prefix = ship.BRANCH_PREFIX
        self._tmp = tempfile.mkdtemp(prefix="ship-hermetic-")
        ship.ORCA = "/nonexistent/orca"
        ship.CONFIG = os.path.join(self._tmp, "nonexistent-ship.json")
        ship.LOCK_DIR = os.path.join(self._tmp, "locks")
        # Values read from the real ~/.orca/guard.env at import time must not leak into the tests.
        ship.RESTRICTED = ()
        ship.BRANCH_PREFIX = ""

    def tearDown(self):
        ship.ORCA = self._orig_orca
        ship.CONFIG = self._orig_config
        ship.LOCK_DIR = self._orig_lock
        ship.RESTRICTED = self._orig_restricted
        ship.BRANCH_PREFIX = self._orig_prefix
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
            "SampleApp/SampleApp.entitlements",
            "App/App.Entitlements",
            "SampleApp/Billing/StoreKitManager.swift",
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
        self.assertEqual(ship.scan_high_risk_paths(["SampleApp/UI/Toolbar.swift", "README.md"]), [])

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
        self.assertIsNone(ship.refuse_branch("develop", "main"), "no restricted branches by default")
        self.assertIsNone(ship.refuse_branch("agent/ui-fix", "main"))
        from unittest import mock
        with mock.patch.object(ship, "RESTRICTED", ("develop", "secret/*")):
            self.assertIsNotNone(ship.refuse_branch("develop", "main"))
            self.assertIsNotNone(ship.refuse_branch("secret/12", "main"))
            self.assertIsNone(ship.refuse_branch("agent/ui-fix", "main"))

    def test_github_slug(self):
        self.assertEqual(ship.github_slug("https://github.com/example-org/sample-app.git"), "example-org/sample-app")
        self.assertEqual(ship.github_slug("git@github.com:example-org/home_lab-v2.git\n"), "example-org/home_lab-v2")
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


    def test_r6_github_ci_no_checks_reported_fails_closed_without_merging(self):
        """R6: When workflows expect CI checks yet `gh pr checks` returns `no checks reported` 3x, never merge."""
        import json
        from types import SimpleNamespace
        from unittest import mock

        calls = []

        def fake_git(top, *args, **kwargs):
            output = ""
            if args[0] == "rev-list":
                output = "1"
            elif args[0] == "rev-parse":
                output = "1234567890abcdef"
            elif args[:2] == ("log", "--format=%s"):
                output = "fix: deterministic ci gate"
            return subprocess.CompletedProcess(list(args), 0, output, "")

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(cmd, 1, "", "no checks reported on the 'codex/feat' branch")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        args = SimpleNamespace(
            no_merge=False, force_merge=False, dry_run=False, check=None, title=None, body=None, method=None
        )
        with mock.patch.object(ship, "acquire_lock"), mock.patch.object(
            ship, "git", side_effect=fake_git
        ), mock.patch.object(ship, "run", side_effect=fake_run), mock.patch.object(
            ship, "collect_changed_paths", return_value=[]
        ), mock.patch.object(
            ship, "has_ci", return_value=True
        ), mock.patch.object(
            ship, "configured_checks", return_value=[]
        ), mock.patch.object(
            ship, "ensure_clean_worktree"
        ), mock.patch.object(
            ship,
            "gh_json",
            return_value=[{"number": 9, "url": "https://github.com/example/repo/pull/9", "baseRefName": "main"}],
        ), mock.patch.object(
            ship.time, "sleep"
        ), mock.patch.object(
            ship, "resolve_main_clone", return_value=None
        ), mock.patch.object(
            ship, "orca_status"
        ), mock.patch.object(
            ship, "say"
        ):
            with self.assertRaises(ship.Stop) as ctx:
                ship._ship_github_locked(
                    args, "/fixture", "codex/feat", "example/repo", {"allow_squash_merge": True}, "main", None
                )
        self.assertEqual(ctx.exception.code, 6)
        self.assertEqual(sum(c[:3] == ["gh", "pr", "checks"] for c in calls), 3)
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in calls))

        # End-to-end real Git + subprocess CLI test (zero Python mocks on ship.py internals):
        main_repo, wt_repo = self._make_fixture("ci-e2e")
        wf_dir = os.path.join(main_repo, ".github", "workflows")
        os.makedirs(wf_dir)
        with open(os.path.join(wf_dir, "ci.yml"), "w") as fh:
            fh.write("name: CI\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n")
        subprocess.check_call(["git", "add", ".github/workflows/ci.yml"], cwd=main_repo)
        subprocess.check_call(["git", "commit", "-q", "-m", "add ci"], cwd=main_repo)

        bare_origin = os.path.join(self._tmp, "origin-e2e.git")
        subprocess.check_call(["git", "init", "-q", "--bare", "-b", "main", bare_origin])
        gh_url = "https://github.com/example/e2e-repo.git"
        subprocess.check_call(["git", "remote", "add", "origin", bare_origin], cwd=main_repo)
        subprocess.check_call(["git", "push", "-q", "-u", "origin", "main"], cwd=main_repo)
        subprocess.check_call(["git", "remote", "set-url", "origin", gh_url], cwd=main_repo)

        subprocess.check_call(["git", "rebase", "-q", "main"], cwd=wt_repo)
        with open(os.path.join(wt_repo, "app.py"), "w") as fh:
            fh.write("x = 99\n")
        subprocess.check_call(["git", "commit", "-q", "-am", "feat: update x"], cwd=wt_repo)

        bin_dir = os.path.join(self._tmp, "fake-bin")
        os.makedirs(bin_dir)
        real_git = shutil.which("git") or "/usr/bin/git"
        git_shim = os.path.join(bin_dir, "git")
        with open(git_shim, "w") as fh:
            fh.write(
                "#!" + sys.executable + "\n"
                "import os, sys\n"
                "args = sys.argv[1:]\n"
                "bare = os.environ['BARE_ORIGIN']\n"
                "if 'fetch' in args and 'origin' in args:\n"
                "    idx = args.index('origin')\n"
                "    args[idx:idx+2] = [bare, '+refs/heads/main:refs/remotes/origin/main']\n"
                "elif 'push' in args and 'origin' in args:\n"
                "    idx = args.index('origin')\n"
                "    args[idx] = bare\n"
                "os.execv(" + repr(real_git) + ", [" + repr(real_git) + "] + args)\n"
            )
        os.chmod(git_shim, 0o755)
        gh_log = os.path.join(self._tmp, "gh-calls.jsonl")
        gh_shim = os.path.join(bin_dir, "gh")
        with open(gh_shim, "w") as fh:
            fh.write(
                "#!" + sys.executable + "\n"
                "import json, os, sys\n"
                "args = sys.argv[1:]\n"
                "with open(os.environ['GH_CALL_LOG'], 'a') as lf:\n"
                "    lf.write(json.dumps(args) + '\\n')\n"
                "if args[:2] == ['api', 'repos/example/e2e-repo']:\n"
                "    print(json.dumps({'default_branch': 'main', 'allow_squash_merge': True}))\n"
                "elif args[:2] == ['pr', 'list']:\n"
                "    print('[]')\n"
                "elif args[:2] == ['pr', 'create']:\n"
                "    print('https://github.com/example/e2e-repo/pull/42')\n"
                "elif args[:2] == ['pr', 'checks']:\n"
                "    sys.stderr.write('no checks reported on codex/feat\\n')\n"
                "    sys.exit(1)\n"
                "elif args[:2] == ['pr', 'merge']:\n"
                "    sys.exit(0)\n"
            )
        os.chmod(gh_shim, 0o755)

        ship_script = os.path.abspath(ship.__file__)
        cli_env = {
            **os.environ,
            "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
            "BARE_ORIGIN": bare_origin,
            "GH_CALL_LOG": gh_log,
            "SHIP_CI_RETRY_DELAY": "0",
            "HOME": self._tmp,
        }
        proc = subprocess.run([sys.executable, ship_script], cwd=wt_repo, capture_output=True, text=True, env=cli_env, timeout=30)
        self.assertEqual(proc.returncode, 6, f"Expected exit 6, got {proc.returncode}: {proc.stdout} {proc.stderr}")
        with open(gh_log) as lf:
            logged_calls = [json.loads(line) for line in lf if line.strip()]
        self.assertEqual(sum(c[:2] == ["pr", "checks"] for c in logged_calls), 3)
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in logged_calls))

        # Also verify `has_ci()` ignores manual/schedule-only workflows while detecting PR/push workflows
        wf_repo = os.path.join(self._tmp, "wf-check")
        os.makedirs(os.path.join(wf_repo, ".github/workflows"))
        with open(os.path.join(wf_repo, ".github/workflows/nightly.yml"), "w") as fh:
            fh.write("name: Nightly\non:\n  workflow_dispatch:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n  sync:\n    steps:\n      - run: git push origin main\n")
        self.assertFalse(ship.has_ci(wf_repo))
        with open(os.path.join(wf_repo, ".github/workflows/ci.yml"), "w") as fh:
            fh.write("name: CI\non: [push, pull_request]\n")
        self.assertTrue(ship.has_ci(wf_repo))

    def test_scan_test_skip_additions_and_cli_gate(self):
        py_skip = "@" + "unittest.skip('flaky')"
        pt_skip = "@" + "pytest.mark.skip(reason='later')"
        sw_skip = "throw " + "XCT" + "Skip('broken')"
        js_skip = "it" + ".skip('todo', () => {})"
        rs_skip = "#" + "[ignore]"
        go_skip = "t." + "Skip('nope')"

        diff = "\n".join([
            "+++ b/tests/test_demo.py",
            "+" + py_skip,
            "+" + pt_skip,
            "-" + py_skip,
            "+# comment mentioning " + py_skip,
            "+++ b/AppTests/HomeTests.swift",
            "+" + sw_skip,
            "+++ b/web/app.test.ts",
            "+" + js_skip,
            "+++ b/crates/core/tests/net_test.rs",
            "+" + rs_skip,
            "+++ b/pkg/api_test.go",
            "+" + go_skip,
            "+++ b/src/normal_module.py",
            "+" + py_skip,
        ])
        hits = ship.scan_test_skip_additions(diff)
        self.assertEqual(len(hits), 6, hits)

        # End-to-end local-only repo ship refuses added skip marker unless --allow-test-skip
        repo = os.path.join(self._tmp, "skip-gate-repo")
        os.makedirs(os.path.join(repo, "tests"))
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
        with open(os.path.join(repo, "tests/test_sample.py"), "w", encoding="utf-8") as fh:
            fh.write("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
        subprocess.run(["git", "switch", "-q", "-c", "codex/skip-test"], cwd=repo, check=True, env=env)
        with open(os.path.join(repo, "tests/test_sample.py"), "w", encoding="utf-8") as fh:
            fh.write("import unittest\nclass T(unittest.TestCase):\n    " + py_skip + "\n    def test_ok(self):\n        self.assertTrue(True)\n")
        subprocess.run(["git", "commit", "-q", "-am", "skip test"], cwd=repo, check=True, env=env)

        old_cwd = os.getcwd()
        try:
            os.chdir(repo)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc_refused = ship.main(["--dry-run"])
            self.assertEqual(rc_refused, 4, buf.getvalue())
            self.assertIn("allow-test-skip", buf.getvalue())

            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                rc_allowed = ship.main(["--dry-run", "--allow-test-skip"])
            self.assertEqual(rc_allowed, 0, buf2.getvalue())
        finally:
            os.chdir(old_cwd)

    # --- hardening: content the scanner used to miss -------------------------------------------------

    def _commit(self, repo, files, msg):
        for rel, text in files.items():
            path = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            subprocess.check_call(["git", "add", rel], cwd=repo)
        subprocess.check_call(["git", "commit", "-q", "-m", msg], cwd=repo)

    def test_token_formats_and_redaction(self):
        hit = (
            "+-----BEGIN PGP " + "PRIVATE KEY BLOCK-----",
            "+id = AS" + "IA" + "ABCDEFGHIJKLMNOP",
            "+x = xa" + "pp-1-A0123456789-1234567890123-" + "a" * 40,
            "+x = xo" + "xe-1-" + "a" * 40,
            "+x = gl" + "pat-" + "a" * 20,
            "+x = np" + "m_" + "a" * 36,
            "+x = h" + "f_" + "a" * 34,
            "+x = S" + "G." + "a" * 22 + "." + "b" * 43,
            "+DATABASE_URL=postgres://admin:" + "S3cr3tPassw0rd" + "@db.internal.test/prod",
        )
        for line in hit:
            self.assertTrue(ship.scan_secrets(line), line)
        allow = (
            "+DATABASE_URL=postgres://postgres:postgres@localhost/db",
            "+url = 'https://user:${TOKEN}@github.com/example-org/sample-app'",
            "+remote = git@github.com:example-org/sample-app.git",
        )
        for line in allow:
            self.assertFalse(ship.scan_secrets(line), line)
        # A secret in key position is never echoed back.
        tok = "gh" + "p_" + "Z" * 36
        hits = ship.scan_secrets("+" + tok + ": value")
        self.assertEqual(hits, [("GitHub token", "<REDACTED>")])
        self.assertEqual(ship.scan_secrets("+api_" + "key = 'sk-" + "x" * 30 + "'")[0][1], "api_key = <REDACTED>")

    def test_combined_diff_only_flags_merge_introduced_lines(self):
        tok = "gh" + "p_" + "C" * 36
        diff = "\n".join([
            "diff --cc c.txt",
            "+++ b/c.txt",
            "@@@ -1,1 -1,1 +1,2 @@@",
            "+ from base " + tok,
            " +from branch " + tok,
            "++introduced by merge " + tok,
        ])
        hits = ship.scan_secrets(diff)
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0][1].startswith("c.txt: "))

    def test_hidden_content_commit_messages_and_credential_files_are_blocked(self):
        tok = "gh" + "p_" + "D" * 36
        cases = {
            "attr": ({".gitattributes": "*.cfg -diff\n", "deploy.cfg": "token = %s\n" % tok}, "add cfg"),
            "binary": ({"blob.bin": "\x00\x01 token = %s\n" % tok}, "add blob"),
            "message": ({"notes.txt": "hello\n"}, "use token %s" % tok),
            "envfile": ({".env": "MODE=dev\n"}, "add env"),
        }
        for name, (files, msg) in cases.items():
            with self.subTest(name):
                _, wt = self._make_fixture(name)
                self._commit(wt, files, msg)
                rc, out = self._run_ship_in(wt, ["--dry-run"])
                self.assertEqual(rc, 5, out)
                self.assertNotIn(tok, out)
        _, wt = self._make_fixture("envexample")
        self._commit(wt, {".env.example": "API_URL=\n"}, "add env example")
        self.assertEqual(self._run_ship_in(wt, ["--dry-run"])[0], 0)

    def test_evil_merge_is_scanned_and_merged_branch_ships_without_rebase_loop(self):
        tok = "gh" + "p_" + "E" * 36
        main_repo, wt = self._make_fixture("evil")
        self._commit(wt, {"app.py": "x = 'branch'\n"}, "branch edit")
        self._commit(main_repo, {"app.py": "x = 'main'\n"}, "main edit")
        subprocess.run(["git", "merge", "-q", "main"], cwd=wt, capture_output=True)
        self._commit(wt, {"app.py": "x = 'resolved'\n", "extra.txt": "key: %s\n" % tok}, "merge main")
        rc, out = self._run_ship_in(wt, [])
        self.assertEqual(rc, 5, out)

        main2, wt2 = self._make_fixture("resolved")
        self._commit(wt2, {"app.py": "x = 'branch'\n"}, "branch edit")
        self._commit(main2, {"app.py": "x = 'main'\n"}, "main edit")
        subprocess.run(["git", "merge", "-q", "main"], cwd=wt2, capture_output=True)
        self._commit(wt2, {"app.py": "x = 'resolved'\n"}, "merge main")
        rc, out = self._run_ship_in(wt2, [])
        self.assertEqual(rc, 0, out)
        with open(os.path.join(main2, "app.py")) as f:
            self.assertEqual(f.read(), "x = 'resolved'\n")
        self.assertIn("git switch -c codex/<task> main", out)

    def test_commit_made_by_a_check_is_rescanned_and_checks_are_additive(self):
        tok = "gh" + "p_" + "F" * 36
        main_repo, wt = self._make_fixture("checkcommit")
        self._commit(wt, {"app.py": "x = 2\n"}, "bump")
        sneaky = "printf 'token = %s\\n' > gen.txt && git add gen.txt && git commit -qm autofix" % tok
        rc, out = self._run_ship_in(wt, ["--check", sneaky])
        self.assertEqual(rc, 5, out)
        self.assertNotIn(tok, subprocess.check_output(["git", "log", "-p", "main"], cwd=main_repo, text=True))

        marker = os.path.join(self._tmp, "configured-check-ran")
        with open(ship.CONFIG, "w", encoding="utf-8") as fh:
            json_text = '{"local/Main": "touch %s"}' % marker
            fh.write(json_text)
        _, wt2 = self._make_fixture("additive")
        self._commit(wt2, {"app.py": "x = 3\n"}, "bump")
        rc, out = self._run_ship_in(wt2, ["--check", "true"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(marker), "--check must not replace the configured check")
        _, wt3 = self._make_fixture("additive-fail")
        self._commit(wt3, {"app.py": "x = 4\n"}, "bump")
        self.assertEqual(self._run_ship_in(wt3, ["--check", "false"])[0], 4)

    def test_new_test_skip_markers(self):
        cases = [
            ("tests/test_a.py", "pytestmark = pytest." + "mark.skip(reason='x')"),
            ("tests/test_a.py", "@pytest." + "mark.xfail"),
            ("tests/test_a.py", "@unittest." + "expectedFailure"),
            ("conftest.py", "collect_" + "ignore = ['tests/test_a.py']"),
            ("pytest.ini", "addopts = --de" + "select tests/test_a.py::test_x"),
            ("web/app.test.ts", "it." + "only('x', () => {})"),
            ("AppTests/HomeTests.swift", "XCTExpect" + "Failure(\"flaky\")"),
        ]
        for path, line in cases:
            self.assertTrue(ship.scan_test_skip_additions("+++ b/%s\n+%s\n" % (path, line)), (path, line))
        for path, line in [("tests/test_ml.py", "model.fit(x, y)"), ("src/app.py", "@pytest." + "mark.skip")]:
            self.assertFalse(ship.scan_test_skip_additions("+++ b/%s\n+%s\n" % (path, line)), (path, line))

    def test_ci_checks_are_awaited_even_without_workflows(self):
        from unittest import mock

        def scenario(checks_result):
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                if cmd[:3] == ["gh", "pr", "checks"]:
                    return subprocess.CompletedProcess(cmd, *checks_result)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(ship, "run", side_effect=fake_run), mock.patch.object(ship.time, "sleep"), \
                    mock.patch.object(ship, "orca_status"), mock.patch.object(ship, "say"):
                try:
                    ship.wait_for_ci("/fixture", 7, "https://github.com/example-org/sample-app/pull/7",
                                     "example-org/sample-app", "abcdef0123", False)
                    return 0, calls
                except ship.Stop as e:
                    return e.code, calls

        rc, calls = scenario((1, "", "no checks reported on the 'agent/x' branch"))
        self.assertEqual(rc, 0)
        self.assertEqual(sum(c[:3] == ["gh", "pr", "checks"] for c in calls), 1)
        rc, _ = scenario((1, "external-ci\tfail\t1m\thttps://ci.example.com/1", ""))
        self.assertEqual(rc, 6, "a failing external status check must block the merge")
        rc, _ = scenario((0, "external-ci\tpass\t1m\thttps://ci.example.com/1", ""))
        self.assertEqual(rc, 0)

    def test_watch_exit_code_is_rechecked_before_failing(self):
        """A non-zero `gh pr checks --watch` (API hiccup, late check) is confirmed against the check list."""
        from unittest import mock

        def scenario(json_payload):
            def fake_run(cmd, **kwargs):
                if cmd[:3] == ["gh", "pr", "checks"] and "--json" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, json_payload, "")
                if cmd[:3] == ["gh", "pr", "checks"]:
                    return subprocess.CompletedProcess(cmd, 1, "", "HTTP 502: Bad Gateway")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(ship, "run", side_effect=fake_run), mock.patch.object(ship.time, "sleep"), \
                    mock.patch.object(ship, "orca_status"), mock.patch.object(ship, "say"):
                try:
                    ship.wait_for_ci("/fixture", 7, "https://github.com/example-org/sample-app/pull/7",
                                     "example-org/sample-app", "abcdef0123", True)
                    return 0
                except ship.Stop as e:
                    return e.code

        green = '[{"name":"ci","bucket":"pass"},{"name":"lint","bucket":"skipping"}]'
        self.assertEqual(scenario(green), 0)
        self.assertEqual(scenario('[{"name":"ci","bucket":"pass"},{"name":"e2e","bucket":"fail"}]'), 6)
        self.assertEqual(scenario('[{"name":"ci","bucket":"pending"}]'), 6, "still pending after re-checks")
        self.assertEqual(scenario("not json"), 6, "unreadable check list fails closed")

    def test_unexpected_errors_and_missing_tools_map_to_documented_codes(self):
        from unittest import mock
        with mock.patch.object(ship, "ship", side_effect=OSError(28, "No space left on device")), \
                redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(ship.main([]), 7)
        self.assertIn("unexpected error", buf.getvalue())
        with self.assertRaises(ship.Stop) as ctx:
            ship.run(["definitely-not-a-real-tool-xyz", "--version"])
        self.assertEqual(ctx.exception.code, 2)
        with mock.patch.object(ship, "LOCK_DIR", "/dev/null/locks"):
            with self.assertRaises(ship.Stop) as ctx:
                ship.open_lock("x.lock")
        self.assertEqual(ctx.exception.code, 2)

    def test_next_task_hint_prefix(self):
        from unittest import mock
        self.assertEqual(ship.next_task_hint("claude/ui-fix", "main"), "git switch -c claude/<task> main")
        self.assertEqual(ship.next_task_hint("feature", "main"), "git switch -c agent/<task> main")
        with mock.patch.object(ship, "BRANCH_PREFIX", "team"):
            self.assertEqual(ship.next_task_hint("claude/ui-fix", "origin/main"), "git switch -c team/<task> origin/main")


if __name__ == "__main__":
    unittest.main()
