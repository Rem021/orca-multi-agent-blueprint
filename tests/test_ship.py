import os
import sys
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


if __name__ == "__main__":
    unittest.main()
