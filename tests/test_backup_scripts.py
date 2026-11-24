#!/usr/bin/env python3
"""Tests for the scheduled backup scripts (scripts/codex-storage), the CLI installer and the
bootstrapper: job lock, status receipts, skip escalation, NAS diagnostics, SQLite snapshots, the
authenticated iCloud package format, iCloud package pruning and config evaluation.

Every test runs the real zsh scripts against a throwaway $HOME, so nothing on the machine is touched.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "scripts/codex-storage"
sys.path.insert(0, str(SRC))

import fcntl  # noqa: E402
import icloud_crypto  # noqa: E402
import icloud_prune  # noqa: E402
import sqlite_snapshot  # noqa: E402

sys.path.insert(0, str(REPO / "scripts"))
import setup_backups  # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"
HAVE_ZSH = Path("/bin/zsh").exists()


def uploaded_status_helper(storage):
    binary = storage / "bin/icloud_status"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("""#!/bin/sh
echo '{"isUploaded":true}'
""")
    binary.chmod(0o755)
    return binary


@unittest.skipUnless(HAVE_ZSH and sys.platform == "darwin", "requires macOS zsh")
class BackupScriptCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve() / "home"
        self.storage = self.home / "Library/Application Support/Codex Storage"
        self.agents = self.storage / "agents"
        self.status = self.storage / "status"
        self.agents.mkdir(parents=True)
        (self.home / "Library/Caches").mkdir(parents=True)
        (self.home / ".orca").mkdir()
        (self.home / "AI-Workspace/AI").mkdir(parents=True)
        for f in SRC.iterdir():
            if f.is_file():
                shutil.copy2(f, self.agents / f.name)
        local_bin = self.home / ".local/bin"
        local_bin.mkdir(parents=True)
        (local_bin / "python3").symlink_to(sys.executable)
        self.config: dict[str, str] = {
            "STORAGE_MODE": "internal",
            "AI_VOLUME_PATH": str(self.home / "AI-Workspace"),
            "BACKUP_NOTIFY": "off",
        }
        self.write_config()

    def tearDown(self):
        self._tmp.cleanup()

    def write_config(self, **extra: str) -> None:
        self.config.update(extra)
        lines = [f'export {k}="{v}"' for k, v in self.config.items()]
        (self.home / ".orca/backup-config.env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def env(self) -> dict[str, str]:
        return {"HOME": str(self.home), "USER": "tester", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}

    def script(self, name: str, body: str) -> Path:
        path = self.agents / name
        path.write_text('#!/bin/zsh\nsource "$(dirname "$0")/lib.sh"\n' + body, encoding="utf-8")
        return path

    def run_zsh(self, path: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/zsh", str(path), *args], env=self.env(), capture_output=True, text=True, timeout=timeout
        )

    def receipt(self, job: str) -> dict:
        return json.loads((self.status / f"{job}.json").read_text(encoding="utf-8"))


class TestInstaller(BackupScriptCase):
    LOCK = REPO / "scripts/cli-tools.lock"
    TOOLS = ("gh", "rg", "fd", "ast-grep", "just", "xcbeautify", "restic")

    def test_lock_pins_every_tool_for_both_architectures(self):
        rows = [ln.split() for ln in self.LOCK.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
        self.assertTrue(all(len(r) == 6 for r in rows), rows)
        self.assertEqual(sorted((r[0], r[3]) for r in rows), sorted((t, a) for t in self.TOOLS for a in ("arm64", "x86_64")))
        for tool, repo, tag, _arch, asset, digest in rows:
            self.assertRegex(digest, r"^[0-9a-f]{64}$", tool)
            self.assertNotEqual(tag, "latest")
            self.assertIn("/", repo)
            self.assertTrue(asset)

    def test_mismatched_download_keeps_installed_binary(self):
        local_bin = self.home / ".local/bin"
        gh = local_bin / "gh"
        original = "#!/bin/sh\necho installed-gh\n"
        gh.write_text(original)
        gh.chmod(0o755)
        urls = self.home / "curl-urls.log"
        curl = local_bin / "curl"
        curl.write_text(f'#!/bin/sh\nfor arg; do dest="$arg"; done\necho "$*" >> "{urls}"\nprintf corrupt > "$dest"\n')
        curl.chmod(0o755)
        result = self.run_zsh(REPO / "scripts/install_cli_tools.sh", "--upgrade")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256 mismatch for gh", result.stderr)
        self.assertEqual(gh.read_text(), original)
        # The pinned release is downloaded, never `releases/latest`
        self.assertIn("/releases/download/", urls.read_text())
        self.assertNotIn("latest", urls.read_text())

    def test_print_lock_builds_entries_from_published_digests(self):
        suffixes = ["macOS_arm64.zip", "macOS_amd64.zip", "aarch64-apple-darwin.tar.gz", "x86_64-apple-darwin.tar.gz",
                    "app-aarch64-apple-darwin.zip", "app-x86_64-apple-darwin.zip", "arm64-apple-macosx.zip",
                    "x86_64-apple-macosx.zip", "darwin_arm64.bz2", "darwin_amd64.bz2"]
        release = {"tag_name": "v9.9.9", "assets": [
            {"name": f"tool-9.9.9-{sfx}", "digest": "sha256:" + format(i, "x") * 64} for i, sfx in enumerate(suffixes, 1)]}
        gh = self.home / ".local/bin/gh"
        gh.write_text("#!/bin/sh\ncat <<'END'\n" + json.dumps(release) + "\nEND\n")
        gh.chmod(0o755)
        result = self.run_zsh(REPO / "scripts/install_cli_tools.sh", "--print-lock")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [ln.split() for ln in result.stdout.splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(len(rows), 14)
        self.assertIn(["restic", "restic/restic", "v9.9.9", "arm64", "tool-9.9.9-darwin_arm64.bz2", "9" * 64], rows)


class TestVolumeGuard(BackupScriptCase):
    def test_identical_state_preserves_receipt(self):
        script = self.agents / "volume-guard.sh"
        self.run_zsh(script).check_returncode()
        receipt = self.status / "volume-guard.json"
        first = receipt.stat().st_mtime_ns
        self.run_zsh(script).check_returncode()
        self.assertEqual(receipt.stat().st_mtime_ns, first)


class TestJobLock(BackupScriptCase):
    def locked(self) -> bool:
        """True while another process holds the job's fcntl lock."""
        path = self.status / ".lock-demo.flock"
        if not path.exists():
            return False
        with open(path, "a") as handle:
            try:
                fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.lockf(handle, fcntl.LOCK_UN)
            return False

    def test_lock_is_held_until_the_script_exits(self):
        # Regression: an EXIT trap set inside a zsh function fires when that function returns,
        # which used to release the lock immediately after take_lock. Writing to the lock file from
        # the holding shell would release it too (fcntl semantics), so status and logs never touch it.
        probe = (
            f'"{sys.executable}" -c \'import fcntl,sys\nf=open(sys.argv[1],"a")\ntry:\n    fcntl.lockf(f, fcntl.LOCK_EX|fcntl.LOCK_NB); print("FREE")\n'
            f'except OSError: print("HELD")\' "$ST/.lock-demo.flock"\n'
        )
        s = self.script("held.sh", 'take_lock demo\nstatus_write demo running x\nlog demo hello\n' + probe)
        r = self.run_zsh(s)
        self.assertEqual(r.stdout.strip(), "HELD", r.stderr)
        self.assertFalse(self.locked(), "lock must be released on exit")
        self.assertFalse((self.status / ".lock-demo.pid").exists())

    def test_second_instance_is_skipped_while_first_runs(self):
        s = self.script("slow.sh", 'take_lock demo\necho started\nsleep "${1:-0}"\necho finished\n')
        first = subprocess.Popen(
            ["/bin/zsh", str(s), "3"], env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            deadline = time.time() + 5
            while not (self.status / ".lock-demo.pid").exists() and time.time() < deadline:
                time.sleep(0.05)
            second = self.run_zsh(s)
            self.assertEqual(second.stdout, "", "second instance must exit before doing any work")
            self.assertTrue(self.locked(), "second instance must not release the lock")
        finally:
            out, _ = first.communicate(timeout=15)
        self.assertIn("finished", out)
        self.assertFalse(self.locked())

    def test_lock_of_killed_owner_is_free_at_once(self):
        s = self.script("slow.sh", 'take_lock demo\necho started\nsleep 30\n')
        first = subprocess.Popen(["/bin/zsh", str(s)], env=self.env(), stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(first.stdout.readline().strip(), "started")
            self.assertTrue(self.locked())
            first.kill()  # SIGKILL: no EXIT trap runs
            first.wait(timeout=10)
        finally:
            first.stdout.close()
        after = self.script("after.sh", "take_lock demo\necho ran\n")
        self.assertEqual(self.run_zsh(after).stdout.strip(), "ran")

    def test_stale_legacy_lock_directory_of_dead_owner_is_reclaimed(self):
        lock = self.status / ".lock-demo"
        lock.mkdir(parents=True)
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        (lock / "pid").write_text(f"{dead.pid}\n")
        s = self.script("reclaim.sh", "take_lock demo\necho ran\n")
        r = self.run_zsh(s)
        self.assertEqual(r.stdout.strip(), "ran", r.stderr)
        self.assertFalse(lock.exists())

    def test_lock_without_pid_is_respected_until_it_ages_out(self):
        lock = self.status / ".lock-demo"
        lock.mkdir(parents=True)
        s = self.script("legacy.sh", "take_lock demo\necho ran\n")
        self.assertEqual(self.run_zsh(s).stdout, "")
        self.assertTrue(lock.is_dir())
        old = time.time() - 13 * 3600
        os.utime(lock, (old, old))
        self.assertEqual(self.run_zsh(s).stdout.strip(), "ran")
        self.assertFalse(lock.exists())

    def test_lock_released_when_terminated(self):
        s = self.script("term.sh", "take_lock demo\necho started\nsleep 20 &\nwait\n")
        p = subprocess.Popen(["/bin/zsh", str(s)], env=self.env(), stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "started")
        p.terminate()
        p.wait(timeout=10)
        p.stdout.close()
        self.assertFalse(self.locked())
        self.assertFalse((self.status / ".lock-demo.pid").exists())


class TestStatusReceipts(BackupScriptCase):
    def test_last_ok_survives_later_failures(self):
        s = self.script("st.sh", 'status_write demo "$1" "$2"\n')
        self.run_zsh(s, "ok", "fine")
        first = self.receipt("demo")
        self.assertEqual(first["last_ok"], first["time"])
        self.assertNotIn("since", first)
        self.run_zsh(s, "running", "working")
        self.run_zsh(s, "fail", 'boom "quoted" | pipe')
        failed = self.receipt("demo")
        self.assertEqual(failed["state"], "fail")
        self.assertEqual(failed["message"], 'boom "quoted" | pipe')
        self.assertEqual(failed["last_ok"], first["last_ok"])
        self.assertIn("since", failed)
        self.run_zsh(s, "ok", "recovered")
        self.assertNotIn("since", self.receipt("demo"))
        dashboard = (self.storage / "backup-status.md").read_text(encoding="utf-8")
        self.assertIn("| demo | ✅ ok |", dashboard)

    def test_repeated_skips_escalate_to_stale(self):
        s = self.script("skip.sh", 'skip_job demo "NAS is not mounted" "${1:-24}"\n')
        self.run_zsh(s)
        self.assertEqual(self.receipt("demo")["state"], "skipped")
        stamp = time.strftime(FMT, time.localtime(time.time() - 30 * 3600))
        (self.status / "demo.json").write_text(
            json.dumps({"name": "demo", "state": "skipped", "message": "x", "time": stamp, "last_ok": stamp})
        )
        self.run_zsh(s)
        stale = self.receipt("demo")
        self.assertEqual(stale["state"], "stale")
        self.assertIn("No successful run for 30 hours", stale["message"])
        self.assertEqual(stale["last_ok"], stamp)
        self.assertIn("last_alert", stale)
        # Escalation can be disabled
        self.run_zsh(s, "0")
        self.assertEqual(self.receipt("demo")["state"], "skipped")
        self.assertIn("demo", (self.storage / "backup-status.md").read_text(encoding="utf-8"))


class TestNasDiagnostics(BackupScriptCase):
    def reason(self) -> str:
        s = self.script("nas.sh", 'nas_mounted && echo MOUNTED || print -r -- "$NAS_REASON"\n')
        return self.run_zsh(s).stdout.strip()

    def test_missing_local_repository(self):
        self.assertIn("Restic repository is missing", self.reason())

    def test_initialised_local_repository(self):
        repo = self.home / "AI-Workspace/AI/Backups/restic"
        repo.mkdir(parents=True)
        (repo / "config").write_text("x")
        self.assertEqual(self.reason(), "MOUNTED")

    def test_unmounted_smb_share_names_the_host_and_never_prompts(self):
        # 192.0.2.0/24 is reserved for documentation. Behind a TUN proxy it may still answer pings,
        # in which case the missing saved credential must stop the job before any mount dialog.
        self.write_config(BACKUP_NAS_SMB_MATCH="//u@192.0.2.1/Data on /Volumes/NoSuchShare (smbfs,")
        reason = self.reason()
        self.assertIn("192.0.2.1", reason)
        self.assertTrue("unreachable" in reason or "no SMB password is saved" in reason, reason)

    def probe(self, smbutil_output: str) -> str:
        """Run _smb_host_alive against stub ping/smbutil binaries."""
        ping = self.agents / "stub-ping"
        ping.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        smb = self.agents / "stub-smbutil"
        smb.write_text("#!/bin/sh\nprintf '%s\\n' '" + smbutil_output + "'\nexit 68\n", encoding="utf-8")
        for f in (ping, smb):
            f.chmod(0o755)
        s = self.script(
            "probe.sh",
            f'SMB_PROBE_PING="{ping}" SMB_PROBE_SMBUTIL="{smb}"\n'
            "_smb_host_alive 192.0.2.9 && echo ALIVE || echo DEAD\n",
        )
        return self.run_zsh(s).stdout.strip()

    def test_smb_authentication_error_means_the_server_is_up(self):
        # macOS 27 smbutil reports a refused guest view this way; the NAS is reachable
        self.assertEqual(self.probe("smbutil: server connection failed: Authentication error"), "ALIVE")

    def test_smb_connection_failure_means_the_server_is_down(self):
        self.assertEqual(self.probe("smbutil: server connection failed: Connection refused"), "DEAD")
        self.assertEqual(self.probe("smbutil: server connection failed: No route to host"), "DEAD")

    def test_automount_can_be_disabled(self):
        self.write_config(
            BACKUP_NAS_SMB_MATCH="//u@127.0.0.1/Data on /Volumes/NoSuchShare (smbfs,", BACKUP_NAS_AUTOMOUNT="never"
        )
        self.assertIn("BACKUP_NAS_AUTOMOUNT=never", self.reason())

    def test_unmounted_nas_volume(self):
        self.write_config(BACKUP_NAS_MOUNT="/Volumes/NoSuchShare")
        self.assertIn("Backup volume is not mounted", self.reason())


class TestSqliteSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.src = self.dir / "live.sqlite"
        self.dst = self.dir / "stage/copy.sqlite"

    def tearDown(self):
        self._tmp.cleanup()

    def rows(self) -> int:
        con = sqlite3.connect(self.dst)
        try:
            return con.execute("SELECT count(*) FROM t").fetchone()[0]
        finally:
            con.close()

    def test_rows_still_in_the_wal_are_captured(self):
        con = sqlite3.connect(self.src)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
        con.execute("CREATE TABLE t (v TEXT)")
        con.executemany("INSERT INTO t VALUES (?)", [("x" * 200,)] * 500)
        con.commit()
        self.assertTrue(Path(str(self.src) + "-wal").stat().st_size > 0)
        result = sqlite_snapshot.snapshot(self.src, self.dst)
        self.assertEqual(result, {"method": "backup-api", "consistent": True})
        # Checked before reading the copy back: Apple's SQLite keeps a -wal file once a WAL database is opened
        self.assertFalse(Path(str(self.dst) + "-wal").exists())
        self.assertEqual(self.rows(), 500)
        con.close()

    def test_exclusively_locked_database_falls_back_to_a_stable_clone(self):
        con = sqlite3.connect(self.src, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (v TEXT)")
        con.execute("PRAGMA locking_mode=EXCLUSIVE")
        con.executemany("INSERT INTO t VALUES (?)", [("y",)] * 40)
        result = sqlite_snapshot.snapshot(self.src, self.dst, timeout=0.2)
        self.assertEqual(result["method"], "clone")
        self.assertTrue(result["consistent"])
        self.assertEqual(self.rows(), 40)
        con.close()

    def test_failed_snapshot_keeps_the_previous_copy(self):
        self.dst.parent.mkdir()
        self.dst.write_text("previous good copy")
        with self.assertRaises(sqlite_snapshot.SnapshotError):
            sqlite_snapshot.snapshot(self.dir / "missing.sqlite", self.dst)
        self.assertEqual(self.dst.read_text(), "previous good copy")
        self.assertEqual([p.name for p in self.dst.parent.iterdir()], ["copy.sqlite"])

    def test_failed_clone_does_not_replace_previous_snapshot(self):
        self.src.write_bytes(b"invalid database")
        self.dst.parent.mkdir()
        self.dst.write_bytes(b"previous")
        for stable in (False, True):
            with self.subTest(stable=stable), mock.patch.object(
                sqlite_snapshot, "_clone_stable", wraps=sqlite_snapshot._clone_stable
            ) as clone:
                if not stable:
                    clone.return_value = False
                with self.assertRaises(sqlite_snapshot.SnapshotError):
                    sqlite_snapshot.snapshot(self.src, self.dst)
                self.assertEqual(self.dst.read_bytes(), b"previous")


    def test_corrupt_copy_fails_quick_check_and_keeps_previous(self):
        con = sqlite3.connect(self.src)
        con.execute("CREATE TABLE t (v TEXT)")
        con.executemany("INSERT INTO t VALUES (?)", [("z" * 300,)] * 400)
        con.commit()
        con.close()
        data = bytearray(self.src.read_bytes())
        page = 4096
        data[3 * page : 3 * page + 64] = b"\xff" * 64  # wreck a b-tree page header
        self.src.write_bytes(bytes(data))
        self.dst.parent.mkdir()
        self.dst.write_bytes(b"previous")
        with self.assertRaises(sqlite_snapshot.SnapshotError):
            sqlite_snapshot.snapshot(self.src, self.dst)
        self.assertEqual(self.dst.read_bytes(), b"previous")

    def test_cli_reports_method(self):
        con = sqlite3.connect(self.src)
        con.execute("CREATE TABLE t (v TEXT)")
        con.commit()
        con.close()
        r = subprocess.run(
            [sys.executable, str(SRC / "sqlite_snapshot.py"), str(self.src), str(self.dst)],
            capture_output=True,
            text=True,
        )
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "backup-api"), r.stderr)


class TestIcloudPackage(BackupScriptCase):
    def setUp(self):
        super().setUp()
        self.icloud = self.home / "iCloud/packages"
        stub = self.home / "stub-security"
        stub.write_text('#!/bin/sh\n[ "$1" = find-generic-password ] && { echo test-passphrase; exit 0; }\nexit 1\n')
        stub.chmod(0o755)
        self.write_config(BACKUP_ICLOUD_DIR=str(self.icloud), BACKUP_HOST_TAG="TestMac", BACKUP_ICLOUD_KEEP_LATEST="1",
                          BACKUP_SECURITY_BIN=str(stub))
        (self.home / ".claude").mkdir()
        (self.home / ".claude/settings.json").write_text('{"hello": "world"}')
        (self.home / ".zshrc").write_text("# test\n")
        db = sqlite3.connect(self.home / ".codex-placeholder.sqlite")
        db.close()
        (self.home / ".codex").mkdir()
        con = sqlite3.connect(self.home / ".codex/state_5.sqlite")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (v TEXT)")
        con.execute("INSERT INTO t VALUES ('kept')")
        con.commit()
        self.live = con

    def tearDown(self):
        self.live.close()
        super().tearDown()

    def packages(self) -> list[str]:
        return sorted(p.name for p in self.icloud.glob("*.tar.gz.enc"))

    def test_package_is_published_verified_and_pruned_per_host(self):
        uploaded_status_helper(self.storage)
        free_kb = shutil.disk_usage(self.home).free // 1024
        if free_kb < 8388608:
            self.skipTest("needs 8 GiB free space")
        self.icloud.mkdir(parents=True)
        foreign = self.icloud / "mac-agents-OtherMac-20260101-000000.tar.gz.enc"
        foreign.write_text("belongs to another host")
        # A work folder left behind by a killed run of an older version (it held a plaintext archive)
        leftover = self.home / "Library/Caches/icloud-package.OLD123"
        leftover.mkdir()
        (leftover / "archive.tar.gz").write_text("plaintext secrets")
        script = self.agents / "icloud-package.sh"

        r1 = self.run_zsh(script, timeout=120)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(self.receipt("icloud-package")["state"], "ok")
        first = [p for p in self.packages() if "TestMac" in p]
        self.assertEqual(len(first), 1)

        time.sleep(1.1)  # package names carry a per-second timestamp
        r2 = self.run_zsh(script, timeout=120)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        mine = [p for p in self.packages() if "TestMac" in p]
        self.assertEqual(len(mine), 1, "only the latest package of this host is kept")
        self.assertNotEqual(mine, first)
        self.assertTrue(foreign.exists(), "packages of other hosts are never pruned")
        ledger = (self.storage / "icloud-published-TestMac.list").read_text().split()
        self.assertEqual(ledger, mine)

        self.assertFalse(leftover.exists(), "leftover work folders are removed at start")
        self.assertEqual(list((self.home / "Library/Caches").glob("icloud-package.*")), [],
                         "no work folder (and no plaintext archive) is left behind")

        # The published package carries a valid MAC, decrypts and contains the consistent SQLite snapshot
        pkg = self.icloud / mine[0]
        self.assertEqual(icloud_crypto.package_format(pkg), "v1")
        self.assertEqual(icloud_crypto.verify(pkg, b"test-passphrase"), "v1")
        out = self.home / "restore"
        out.mkdir()
        dec = subprocess.run(
            f"printf 'test-passphrase\\n' | '{sys.executable}' '{SRC / 'icloud_crypto.py'}' decrypt --require-mac '{pkg}'"
            f" | tar -xzf - -C '{out}'",
            shell=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(dec.returncode, 0, dec.stderr)
        self.assertEqual(pkg.stat().st_mode & 0o777, 0o600)
        self.assertTrue((out / ".claude/settings.json").is_file())
        snap = out / ".orca/sqlite-backups/codex-state_5.sqlite"
        con = sqlite3.connect(snap)
        try:
            self.assertEqual(con.execute("SELECT v FROM t").fetchone()[0], "kept")
        finally:
            con.close()
        self.assertFalse((self.status / ".lock-icloud-package.pid").exists())
        self.assertFalse((self.home / ".orca/sqlite-backups").exists(), "staging area is cleaned up")

    def test_missing_keychain_key_fails_closed(self):
        stub = self.home / "stub-security"
        stub.write_text("#!/bin/sh\nexit 44\n")
        r = self.run_zsh(self.agents / "icloud-package.sh", timeout=60)
        self.assertNotEqual(r.returncode, 0)
        receipt = self.receipt("icloud-package")
        self.assertEqual(receipt["state"], "fail")
        self.assertIn("no new key was generated", receipt["message"])
        self.assertEqual(self.packages() if self.icloud.exists() else [], [])




    def test_unconfirmed_upload_preserves_older_packages_until_confirmed(self):
        free_kb = shutil.disk_usage(self.home).free // 1024
        if free_kb < 8388608:
            self.skipTest("needs 8 GiB free space")
        self.icloud.mkdir(parents=True)
        bin_dir = self.storage / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        flag_file = self.home / "upload_ok_flag"
        flag_file.write_text("true", encoding="utf-8")
        ic_bin = bin_dir / "icloud_status"
        ic_bin.write_text(
            "#!/bin/sh\n"
            f'val=$(cat "{flag_file}")\n'
            'echo "{\\"isUploaded\\": $val, \\"hasConflicts\\": false}"\n',
            encoding="utf-8",
        )
        ic_bin.chmod(0o755)
        self.write_config(
            ICLOUD_UPLOAD_POLL_ATTEMPTS="2",
            ICLOUD_UPLOAD_POLL_SECONDS="0.05",
            ICLOUD_UPLOAD_SYNC_WAIT="1",
        )
        script = self.agents / "icloud-package.sh"

        # Run 1: upload succeeds -> state=ok, 1 package
        r1 = self.run_zsh(script, timeout=120)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(self.receipt("icloud-package")["state"], "ok")
        first = [p for p in self.packages() if "TestMac" in p]
        self.assertEqual(len(first), 1)

        # Run 2: upload stalls (isUploaded=false) -> state=warn, BOTH packages preserved!
        time.sleep(1.1)
        flag_file.write_text("false", encoding="utf-8")
        r2 = self.run_zsh(script, timeout=120)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        rec2 = self.receipt("icloud-package")
        self.assertEqual(rec2["state"], "warn", rec2)
        both = [p for p in self.packages() if "TestMac" in p]
        self.assertEqual(len(both), 2, "must NOT prune confirmed older package while new upload is unconfirmed")
        self.assertIn(first[0], both)

        # Run 3: upload recovers (isUploaded=true) -> state=ok, pruned down to newest 1 package
        time.sleep(1.1)
        flag_file.write_text("true", encoding="utf-8")
        r3 = self.run_zsh(script, timeout=120)
        self.assertEqual(r3.returncode, 0, r3.stderr)
        self.assertEqual(self.receipt("icloud-package")["state"], "ok")
        final = [p for p in self.packages() if "TestMac" in p]
        self.assertEqual(len(final), 1)
        self.assertNotIn(first[0], final)

    def test_prune_only_mode_dry_run_and_apply(self):
        uploaded_status_helper(self.storage)
        self.icloud.mkdir(parents=True)
        for name in (
            "mac-agents-20261107-221446.tar.gz.enc",
            "mac-agents-OtherMac-20261118-023005.tar.gz.enc",
            "mac-agents-OtherMac-20261119-023005.tar.gz.enc",
            "mac-agents-TestMac-20261118-023005.tar.gz.enc",
            "mac-agents-TestMac-20261119-023005.tar.gz.enc",
        ):
            (self.icloud / name).write_text("enc-data", encoding="utf-8")

        script = self.agents / "icloud-package.sh"
        dry = self.run_zsh(script, "--prune-only", "--dry-run", "--json")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        dry_data = json.loads(dry.stdout)
        self.assertFalse((self.storage / "icloud-known-hosts.list").exists())
        self.assertEqual(dry_data["pruned_count"], 3)
        self.assertEqual(len(self.packages()), 5, "--dry-run must not delete any file")

        applied = self.run_zsh(script, "--prune-only", "--apply", "--json")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        app_data = json.loads(applied.stdout)
        self.assertEqual(app_data["pruned_count"], 3)
        self.assertEqual(
            self.packages(),
            [
                "mac-agents-OtherMac-20261119-023005.tar.gz.enc",
                "mac-agents-TestMac-20261119-023005.tar.gz.enc",
            ],
        )


class TestResticRetryLock(BackupScriptCase):
    def test_restic_runners_pass_retry_lock_flag(self):
        repo = self.home / "AI-Workspace/AI/Backups/restic"
        repo.mkdir(parents=True)
        (repo / "config").write_text("repo", encoding="utf-8")
        bin_dir = self.storage / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        argv_log = self.home / "restic_argv.log"
        stub = bin_dir / "restic"
        stub.write_text(
            "#!/bin/sh\n"
            f'printf " %s " "$*" >> "{argv_log}"\n'
            f'printf "\\n" >> "{argv_log}"\n'
            'case " $* " in\n'
            '  *" restore "*) \n'
            '    prev=""\n'
            '    for a in "$@"; do\n'
            '      if [ "$prev" = "--target" ]; then mkdir -p "$a"; echo ok > "$a/restored.txt"; fi\n'
            '      prev="$a"\n'
            '    done\n'
            '    ;;\n'
            '  *" backup "*) echo "Files: 1 new"; echo "snapshot deadbeef saved" ;;\n'
            '  *" stats "*) echo "Total Size: 1 MiB" ;;\n'
            '  *" snapshots "*) printf \'[{"id":"abc123def456","time":"2026-11-24T01:00:00Z","paths":["%s/.zshrc","%s/.codex"]}]\\n\' "$HOME" "$HOME" ;;\n'
            'esac\n'
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        self.write_config(BACKUP_RESTIC_RETRY_LOCK="7m")

        for script_name, args in (
            ("backup-restic.sh", ("hot",)),
            ("maintenance.sh", ()),
            ("restore-drill.sh", ()),
        ):
            r = self.run_zsh(self.agents / script_name, *args)
            self.assertEqual(r.returncode, 0, f"{script_name} failed: {r.stderr}")

        calls = argv_log.read_text(encoding="utf-8").splitlines()
        locked_ops = [c for c in calls if any(op in c for op in (" backup ", " forget ", " check ", " stats ", " restore ", " snapshots "))]
        self.assertGreaterEqual(len(locked_ops), 6, calls)
        for c in locked_ops:
            self.assertIn("--retry-lock 7m", c, f"Missing --retry-lock on restic invocation: {c}")
        # The drill restores the snapshot it listed, only paths that snapshot contains
        restore = next(c for c in calls if " restore " in c)
        self.assertIn(" restore abc123def456 ", restore)
        self.assertIn(f"--include {self.home}/.zshrc", restore)
        self.assertIn(f"--include {self.home}/.codex/memories", restore)
        self.assertNotIn(".claude/projects", restore)
        self.assertEqual(self.receipt("restore-drill")["state"], "ok")

    def test_restore_drill_without_a_snapshot_is_skipped(self):
        repo = self.home / "AI-Workspace/AI/Backups/restic"
        repo.mkdir(parents=True)
        (repo / "config").write_text("repo", encoding="utf-8")
        stub = self.storage / "bin/restic"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text('#!/bin/sh\ncase " $* " in *" snapshots "*) echo "[]" ;; *" restore "*) exit 9 ;; esac\nexit 0\n')
        stub.chmod(0o755)
        r = self.run_zsh(self.agents / "restore-drill.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        receipt = self.receipt("restore-drill")
        self.assertEqual(receipt["state"], "skipped")
        self.assertIn("No hot snapshot", receipt["message"])

class TestAgentHygieneScript(BackupScriptCase):
    def test_agent_hygiene_reclaims_expired_scratch_and_writes_ok_receipt(self):
        shutil.copy2(REPO / "scripts/agent_governor.py", self.home / ".local/bin/agent-governor")
        os.chmod(self.home / ".local/bin/agent-governor", 0o755)
        proj = self.home / "AI-Workspace/AI/Projects/Demo"
        stale = proj / "work/_scratch/20261101-codex-old"
        fresh = proj / "work/_scratch/20261114-codex-fresh"
        legacy = proj / "work/historical-experiment"
        for d in (stale, fresh, legacy):
            d.mkdir(parents=True)
            (d / "note.txt").write_text("payload\n", encoding="utf-8")
        now_ts = time.time()
        ref_now = now_ts + 5 * 86400
        old_ts = now_ts - 5 * 86400
        os.utime(stale / "note.txt", (old_ts, old_ts))
        os.utime(stale, (old_ts, old_ts))
        os.utime(fresh / "note.txt", (ref_now, ref_now))
        os.utime(fresh, (ref_now, ref_now))
        os.utime(legacy / "note.txt", (old_ts, old_ts))
        os.utime(legacy, (old_ts, old_ts))
        self.write_config(AGENT_GOVERNOR_GC_ARGS=f"--reference-now {ref_now:.0f}")

        r = self.run_zsh(self.agents / "agent-hygiene.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.receipt("agent-hygiene")
        self.assertEqual(rec["state"], "ok", rec)
        self.assertIn("reclaimed 1 item(s)", rec["message"])
        self.assertFalse(stale.exists(), "expired _scratch item should be moved to Trash")
        self.assertTrue(fresh.exists(), "fresh _scratch item must stay")
        self.assertTrue(legacy.exists(), "grandfathered legacy work/ folder must never be touched")

    def test_agent_hygiene_polls_icloud_pruner_and_reconciles_warn_receipt(self):
        shutil.copy2(REPO / "scripts/agent_governor.py", self.home / ".local/bin/agent-governor")
        os.chmod(self.home / ".local/bin/agent-governor", 0o755)
        icloud = self.home / "iCloud/packages"
        icloud.mkdir(parents=True)
        own_old = icloud / "mac-agents-TestMac-20261118-023005.tar.gz.enc"
        own_new = icloud / "mac-agents-TestMac-20261119-023005.tar.gz.enc"
        other_old = icloud / "mac-agents-MacBook-Pro-20261118-023005.tar.gz.enc"
        other_new = icloud / "mac-agents-MacBook-Pro-20261119-023005.tar.gz.enc"
        legacy = icloud / "mac-agents-20261107-221446.tar.gz.enc"
        restore_md = icloud / "RESTORE.md"
        for p in (own_old, own_new, other_old, other_new, legacy, restore_md):
            p.write_text("payload-data", encoding="utf-8")

        bin_dir = self.storage / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        flag_file = self.home / "upload_ok_flag"
        flag_file.write_text("false", encoding="utf-8")
        ic_bin = bin_dir / "icloud_status"
        ic_bin.write_text(
            "#!/bin/sh\n"
            f'val=$(cat "{flag_file}")\n'
            'case "$1" in\n'
            '  *20261119-023005*) echo "{\\"isUploaded\\": $val, \\"hasConflicts\\": false}" ;;\n'
            '  *) echo "{\\"isUploaded\\": true, \\"hasConflicts\\": false}" ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        ic_bin.chmod(0o755)

        self.status.mkdir(parents=True, exist_ok=True)
        (self.status / "icloud-package.json").write_text(
            json.dumps(
                {
                    "name": "icloud-package",
                    "state": "warn",
                    "message": f"waiting for the iCloud upload: {own_new.name}",
                    "time": "2026-11-19 02:37:00",
                }
            ),
            encoding="utf-8",
        )
        self.write_config(
            BACKUP_ICLOUD_DIR=str(icloud),
            BACKUP_HOST_TAG="TestMac",
            BACKUP_ICLOUD_KEEP_LATEST="1",
            BACKUP_ICLOUD_PRUNE_SCOPE="all-hosts",
            BACKUP_ICLOUD_PRUNE_INTERVAL_SEC="3600",
            AGENT_HYGIENE_SKIP_SLIM="1",
        )

        # Poll 1: 20261119 uploads are still pending -> keep BOTH own_old and own_new, keep icloud-package=warn
        r1 = self.run_zsh(self.agents / "agent-hygiene.sh")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertTrue(own_old.exists(), "confirmed own_old must stay while own_new upload is pending")
        self.assertTrue(own_new.exists())
        self.assertTrue(other_old.exists(), "confirmed other_old must stay while other_new upload is pending")
        self.assertTrue(other_new.exists())
        self.assertFalse(legacy.exists(), "superseded legacy package older than confirmed packages is pruned")
        self.assertTrue(restore_md.exists(), "RESTORE.md must never be pruned")
        self.assertEqual(self.receipt("icloud-package")["state"], "warn")

        # Poll 2 (immediately, bypassing 3600s throttle because state==warn): upload finishes -> prunes & reconciles ok!
        flag_file.write_text("true", encoding="utf-8")
        r2 = self.run_zsh(self.agents / "agent-hygiene.sh")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertFalse(own_old.exists(), "own_old should be pruned once own_new upload is confirmed")
        self.assertTrue(own_new.exists())
        self.assertFalse(other_old.exists(), "other_old should be pruned once other_new upload is confirmed")
        self.assertTrue(other_new.exists())
        self.assertTrue(restore_md.exists())
        rec_ic = self.receipt("icloud-package")
        self.assertEqual(rec_ic["state"], "ok", rec_ic)
        self.assertIn("mac-agents-TestMac-20261119-023005.tar.gz.enc", rec_ic["message"])
        self.assertIn("pruned 2 old iCloud package(s)", self.receipt("agent-hygiene")["message"])


    def test_agent_hygiene_throttles_gc_between_hourly_wakeups(self):
        shutil.copy2(REPO / "scripts/agent_governor.py", self.home / ".local/bin/agent-governor")
        os.chmod(self.home / ".local/bin/agent-governor", 0o755)
        self.write_config(
            AGENT_HYGIENE_GC_INTERVAL_SEC="14400",
            AGENT_HYGIENE_SKIP_ICLOUD_PRUNE="1",
        )
        r1 = self.run_zsh(self.agents / "agent-hygiene.sh")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertTrue((self.status / ".agent-gc.stamp").is_file())
        self.assertIn("Cache GC: reclaimed", self.receipt("agent-hygiene")["message"])

        r2 = self.run_zsh(self.agents / "agent-hygiene.sh")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        msg = self.receipt("agent-hygiene")["message"]
        self.assertIn("min ago); next GC within", msg)
        self.assertIn("Cache GC: reclaimed", msg, "the throttled run repeats the last GC result")


class TestAgentHygieneDecoupling(BackupScriptCase):
    """iCloud polling never depends on the GC or the workspace volume; GC results map to receipts."""

    def setUp(self):
        super().setUp()
        self.report = self.home / "gc-report.json"
        self.gc_calls = self.home / "gc-calls.log"
        gov = self.home / ".local/bin/agent-governor"
        gov.write_text(
            "import sys\n"
            f"open({str(self.gc_calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
            f"text = open({str(self.report)!r}).read()\n"
            "print(text)\n"
            "sys.exit(1 if '\"errors\": [{' in text else 0)\n",
            encoding="utf-8",
        )
        gov.chmod(0o755)
        self.icloud = self.home / "iCloud/packages"
        self.icloud.mkdir(parents=True)
        for day in ("18", "19"):
            (self.icloud / f"mac-agents-TestMac-202611{day}-023005.tar.gz.enc").write_text("x")
        uploaded_status_helper(self.storage)
        self.write_config(BACKUP_ICLOUD_DIR=str(self.icloud), BACKUP_HOST_TAG="TestMac")

    def gc_report(self, execution: str, errors=()):
        self.report.write_text(json.dumps({
            "execution": execution, "total_candidates": 0, "reclaimable_human": "0 B",
            "protected_skips_count": 0, "launchservices_ghosts": [], "errors": list(errors)}))

    def run_hygiene(self):
        r = self.run_zsh(self.agents / "agent-hygiene.sh")
        return r, self.receipt("agent-hygiene")

    def gc_runs(self) -> int:
        return len(self.gc_calls.read_text().splitlines()) if self.gc_calls.exists() else 0

    def test_failed_gc_still_prunes_icloud_and_is_not_retried_hourly(self):
        self.gc_report("aborted-process-check-failed", [{"path": "/bin/ps", "error": "ps failed"}])
        r, rec = self.run_hygiene()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(rec["state"], "fail")
        self.assertIn("Cache GC aborted", rec["message"])
        self.assertFalse((self.icloud / "mac-agents-TestMac-20261118-023005.tar.gz.enc").exists(),
                         "iCloud pruning runs although the GC failed")
        self.assertTrue((self.status / ".agent-gc.stamp").is_file(), "a failed GC is stamped too")
        r2, rec2 = self.run_hygiene()
        self.assertEqual(self.gc_runs(), 1, "the GC interval applies after a failure")
        self.assertEqual(rec2["state"], "fail", "the throttled run repeats the last GC result")

    def test_partial_gc_errors_are_a_warning(self):
        self.gc_report("applied", [{"path": "/x", "error": "busy"}])
        r, rec = self.run_hygiene()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(rec["state"], "warn")
        self.assertIn("1 item(s) failed", rec["message"])

    def test_skipped_locked_gc_is_ok(self):
        self.gc_report("skipped-locked")
        r, rec = self.run_hygiene()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(rec["state"], "ok")
        self.assertIn("another GC run holds the lock", rec["message"])

    def test_custom_gc_args_respect_the_interval(self):
        self.gc_report("applied")
        self.write_config(AGENT_GOVERNOR_GC_ARGS="--reference-now '1800000000'")
        self.run_hygiene()
        self.run_hygiene()
        self.assertEqual(self.gc_runs(), 1)
        self.assertIn("--reference-now 1800000000", self.gc_calls.read_text(), "quotes are removed from the arguments")

    def test_icloud_polling_runs_while_the_workspace_is_missing(self):
        self.gc_report("applied")
        self.write_config(AI_VOLUME_PATH=str(self.home / "no-such-workspace"))
        r, rec = self.run_hygiene()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(rec["state"], "skipped")
        self.assertIn("cache GC skipped", rec["message"])
        self.assertEqual(self.gc_runs(), 0)
        self.assertFalse((self.icloud / "mac-agents-TestMac-20261118-023005.tar.gz.enc").exists())

class TestIcloudRollingPrune(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.icloud = self.root / "icloud"
        self.storage = self.root / "storage"
        self.icloud.mkdir(parents=True)
        self.storage.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def touch_pkg(self, name: str, size: int = 128) -> Path:
        p = self.icloud / name
        p.write_bytes(b"x" * size)
        return p

    def test_missing_upload_helper_keeps_local_packages(self):
        for day in ("18", "19"):
            self.touch_pkg(f"mac-agents-TestMac-202611{day}-023005.tar.gz.enc")
        report = icloud_prune.run_prune(icloud_dir=self.icloud, storage_dir=self.storage,
                                       current_host="TestMac", apply=True)
        self.assertEqual(report["pruned_count"], 0)
        self.assertEqual(len(report["pending_own_uploads"]), 2)


    def test_parse_package_name_variants(self):
        p1 = icloud_prune.parse_package_name("mac-agents-Studio-MacBook-Pro-20261119-023005.tar.gz.enc")
        self.assertIsNotNone(p1)
        self.assertEqual(p1[1], "Studio-MacBook-Pro")
        self.assertFalse(p1[2])
        self.assertFalse(p1[4])

        p2 = icloud_prune.parse_package_name("mac-agents-20261107-221446.tar.gz.enc")
        self.assertIsNotNone(p2)
        self.assertEqual(p2[1], icloud_prune.LEGACY_HOST)
        self.assertTrue(p2[2])

        p3 = icloud_prune.parse_package_name(".mac-agents-MacBook-Pro-20261110-0230.tar.gz.enc.icloud")
        self.assertIsNotNone(p3)
        self.assertEqual(p3[0], "mac-agents-MacBook-Pro-20261110-0230.tar.gz.enc")
        self.assertEqual(p3[1], "MacBook-Pro")
        self.assertTrue(p3[4])

        self.assertIsNone(icloud_prune.parse_package_name("RESTORE.md"))
        self.assertIsNone(icloud_prune.parse_package_name(".published-Studio-MacBook-Pro.list"))

    def test_all_hosts_rolling_prune_and_legacy_cleanup(self):
        self.touch_pkg("RESTORE.md", 64)
        self.touch_pkg("mac-agents-20261107-221446.tar.gz.enc", 100)
        self.touch_pkg("mac-agents-20261108-023005.tar.gz.enc", 100)
        for day in ("09", "10", "11", "18", "19"):
            self.touch_pkg(f"mac-agents-MacBook-Pro-202611{day}-023005.tar.gz.enc", 200)
        self.touch_pkg("mac-agents-Studio-MacBook-Pro-20261118-023005.tar.gz.enc", 300)
        self.touch_pkg("mac-agents-Studio-MacBook-Pro-20261119-023005.tar.gz.enc", 300)

        dry = icloud_prune.run_prune(
            icloud_dir=self.icloud,
            storage_dir=self.storage,
            icloud_status_bin=uploaded_status_helper(self.storage),
            current_host="Studio-MacBook-Pro",
            keep_latest=1,
            scope="all-hosts",
            apply=False,
        )
        self.assertEqual(dry["pruned_count"], 7)
        self.assertEqual(dry["kept_count"], 2)
        self.assertTrue((self.icloud / "mac-agents-20261107-221446.tar.gz.enc").exists(), "dry-run must not delete")

        applied = icloud_prune.run_prune(
            icloud_dir=self.icloud,
            storage_dir=self.storage,
            icloud_status_bin=uploaded_status_helper(self.storage),
            current_host="Studio-MacBook-Pro",
            keep_latest=1,
            scope="all-hosts",
            apply=True,
        )
        self.assertEqual(applied["pruned_count"], 7)
        self.assertEqual(applied["failed_count"], 0)
        remaining = sorted(p.name for p in self.icloud.iterdir() if not p.name.startswith("."))
        self.assertEqual(
            remaining,
            [
                "RESTORE.md",
                "mac-agents-MacBook-Pro-20261119-023005.tar.gz.enc",
                "mac-agents-Studio-MacBook-Pro-20261119-023005.tar.gz.enc",
            ],
        )

    def test_self_scope_leaves_foreign_and_legacy_untouched(self):
        self.touch_pkg("mac-agents-20261107-221446.tar.gz.enc")
        self.touch_pkg("mac-agents-MacBook-Pro-20261118-023005.tar.gz.enc")
        self.touch_pkg("mac-agents-MacBook-Pro-20261119-023005.tar.gz.enc")
        self.touch_pkg("mac-agents-Studio-MacBook-Pro-20261118-023005.tar.gz.enc")
        self.touch_pkg("mac-agents-Studio-MacBook-Pro-20261119-023005.tar.gz.enc")

        res = icloud_prune.run_prune(
            icloud_dir=self.icloud,
            storage_dir=self.storage,
            icloud_status_bin=uploaded_status_helper(self.storage),
            current_host="Studio-MacBook-Pro",
            keep_latest=1,
            scope="self",
            apply=True,
        )
        self.assertEqual(res["pruned_count"], 1)
        self.assertTrue((self.icloud / "mac-agents-20261107-221446.tar.gz.enc").exists())
        self.assertTrue((self.icloud / "mac-agents-MacBook-Pro-20261118-023005.tar.gz.enc").exists())
        self.assertFalse((self.icloud / "mac-agents-Studio-MacBook-Pro-20261118-023005.tar.gz.enc").exists())

    def test_max_age_and_stale_host_retirement(self):
        self.touch_pkg("mac-agents-Studio-MacBook-Pro-20261119-023005.tar.gz.enc")
        self.touch_pkg("mac-agents-Studio-MacBook-Pro-20261117-023005.tar.gz.enc")
        self.touch_pkg("mac-agents-Studio-MacBook-Pro-20261101-023005.tar.gz.enc")
        self.touch_pkg("mac-agents-RetiredMac-20260901-023005.tar.gz.enc")

        ref_now = time.mktime(time.strptime("2026-11-19 12:00:00", "%Y-%m-%d %H:%M:%S"))
        # keep_latest=5 + max_age_days=7 -> drops 20261101 (18d old), keeps 20261119 & 20261117, keeps RetiredMac (stale=0)
        r1 = icloud_prune.run_prune(
            icloud_dir=self.icloud,
            storage_dir=self.storage,
            icloud_status_bin=uploaded_status_helper(self.storage),
            current_host="Studio-MacBook-Pro",
            keep_latest=5,
            max_age_days=7,
            stale_host_days=0,
            scope="all-hosts",
            apply=True,
            now_ts=ref_now,
        )
        self.assertEqual([p["name"] for p in r1["pruned"]], ["mac-agents-Studio-MacBook-Pro-20261101-023005.tar.gz.enc"])
        self.assertTrue((self.icloud / "mac-agents-RetiredMac-20260901-023005.tar.gz.enc").exists())

        # Opt-in stale_host_days=30 retires RetiredMac (79d old) since current host has confirmed backup
        r2 = icloud_prune.run_prune(
            icloud_dir=self.icloud,
            storage_dir=self.storage,
            icloud_status_bin=uploaded_status_helper(self.storage),
            current_host="Studio-MacBook-Pro",
            keep_latest=5,
            max_age_days=7,
            stale_host_days=30,
            scope="all-hosts",
            apply=True,
            now_ts=ref_now,
        )
        self.assertEqual([p["name"] for p in r2["pruned"]], ["mac-agents-RetiredMac-20260901-023005.tar.gz.enc"])
        self.assertFalse((self.icloud / "mac-agents-RetiredMac-20260901-023005.tar.gz.enc").exists())



class TestIcloudPruneSafety(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.icloud = self.root / "icloud"
        self.storage = self.root / "storage"
        self.icloud.mkdir()
        self.storage.mkdir()
        self.now = time.mktime(time.strptime("2026-11-24 12:00:00", "%Y-%m-%d %H:%M:%S"))

    def tearDown(self):
        self._tmp.cleanup()

    def prune(self, **kw):
        return icloud_prune.run_prune(icloud_dir=self.icloud, storage_dir=self.storage,
                                      icloud_status_bin=uploaded_status_helper(self.storage),
                                      current_host="MyMac", keep_latest=1, apply=True, now_ts=self.now, **kw)

    def test_future_dated_package_never_anchors_retention(self):
        future = self.icloud / "mac-agents-MyMac-20271124-023000.tar.gz.enc"
        tonight = self.icloud / "mac-agents-MyMac-20261124-023000.tar.gz.enc"
        older = self.icloud / "mac-agents-MyMac-20261123-023000.tar.gz.enc"
        for f in (future, tonight, older):
            f.write_text("x")
        report = self.prune()
        self.assertTrue(tonight.exists(), "the real newest backup must survive")
        self.assertTrue(future.exists(), "a future-dated package is never deleted automatically")
        self.assertFalse(older.exists())
        self.assertEqual(report["future_dated"], [future.name])
        self.assertEqual(report["latest_own_uploaded"]["name"], tonight.name)

    def test_tomorrow_within_tolerance_is_treated_normally(self):
        soon = self.icloud / "mac-agents-MyMac-20261125-010000.tar.gz.enc"  # 13 h ahead: time zones
        today = self.icloud / "mac-agents-MyMac-20261124-023000.tar.gz.enc"
        for f in (soon, today):
            f.write_text("x")
        report = self.prune()
        self.assertEqual(report["future_dated"], [])
        self.assertTrue(soon.exists())
        self.assertFalse(today.exists())

    def test_record_appends_to_both_ledgers_once(self):
        for _ in range(2):
            icloud_prune.record_published(self.icloud, self.storage, "My Mac", "mac-agents-My_Mac-20261124-023000.tar.gz.enc")
        self.assertEqual((self.storage / "icloud-published-My_Mac.list").read_text().split(),
                         ["mac-agents-My_Mac-20261124-023000.tar.gz.enc"])
        self.assertEqual((self.icloud / ".published-My_Mac.list").read_text().split(),
                         ["mac-agents-My_Mac-20261124-023000.tar.gz.enc"])

    def test_default_folder_keeps_an_existing_legacy_folder(self):
        home = self.root / "home"
        self.assertEqual(icloud_prune.default_icloud_dir(home).parts[-2:], ("Agent Backups", "Nightly"))
        self.assertEqual(setup_backups.default_icloud_dir(home), icloud_prune.default_icloud_dir(home))
        legacy = home / icloud_prune.ICLOUD_DRIVE / icloud_prune.LEGACY_ICLOUD_SUBDIR
        legacy.mkdir(parents=True)
        self.assertEqual(icloud_prune.default_icloud_dir(home), legacy)
        self.assertEqual(setup_backups.default_icloud_dir(home), legacy)


class TestIcloudCrypto(unittest.TestCase):
    PASSWORD = b"correct horse battery staple"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name).resolve()
        self.cli = [sys.executable, str(SRC / "icloud_crypto.py")]

    def tearDown(self):
        self._tmp.cleanup()

    def encrypt(self, payload: bytes, out: Path) -> None:
        r, w = os.pipe()
        os.write(w, self.PASSWORD + b"\n")
        os.close(w)
        try:
            proc = subprocess.run(self.cli + ["encrypt", "--out", str(out), "--password-fd", str(r)],
                                  input=payload, pass_fds=(r,), capture_output=True)
        finally:
            os.close(r)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def decrypt(self, pkg: Path, *extra: str, password: bytes = PASSWORD) -> subprocess.CompletedProcess:
        return subprocess.run(self.cli + ["decrypt", str(pkg), *extra], input=password + b"\n", capture_output=True)

    def test_round_trip_and_format(self):
        pkg = self.dir / "p.enc"
        payload = os.urandom(300_000)
        self.encrypt(payload, pkg)
        raw = pkg.read_bytes()
        self.assertTrue(raw.startswith(b"Salted__"))
        self.assertTrue(raw.endswith(icloud_crypto.MAGIC))
        self.assertEqual(self.decrypt(pkg, "--require-mac").stdout, payload)

    def test_tampered_truncated_or_wrong_password_is_rejected_before_decryption(self):
        pkg = self.dir / "p.enc"
        self.encrypt(b"secret archive" * 1000, pkg)
        good = pkg.read_bytes()
        flipped = bytearray(good)
        flipped[100] ^= 0x01
        for name, data in (("flipped", bytes(flipped)), ("truncated", good[:-60] + good[-48:])):
            bad = self.dir / name
            bad.write_bytes(data)
            r = self.decrypt(bad)
            self.assertEqual(r.returncode, 1, name)
            self.assertEqual(r.stdout, b"", f"{name}: nothing may be decrypted")
            self.assertIn(b"MAC mismatch", r.stderr)
        r = self.decrypt(pkg, password=b"wrong")
        self.assertEqual((r.returncode, r.stdout), (1, b""))

    def test_legacy_packages_still_decrypt(self):
        legacy = self.dir / "legacy.enc"
        # The command earlier versions of icloud-package.sh used
        subprocess.run(
            f"printf '%s\\n' '{self.PASSWORD.decode()}' | /usr/bin/openssl enc -aes-256-cbc -pbkdf2 -salt "
            f"-in /dev/stdin -out '{legacy}' -pass file:<(printf '%s\\n' '{self.PASSWORD.decode()}')",
            shell=True, executable="/bin/zsh", check=True, capture_output=True,
        )
        self.assertEqual(icloud_crypto.package_format(legacy), "legacy")
        r = self.decrypt(legacy)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, self.PASSWORD + b"\n")
        self.assertIn(b"legacy format", r.stderr)
        self.assertEqual(self.decrypt(legacy, "--require-mac").returncode, 1)

    def test_empty_password_is_refused(self):
        r, w = os.pipe()
        os.close(w)
        try:
            proc = subprocess.run(self.cli + ["encrypt", "--out", str(self.dir / "x"), "--password-fd", str(r)],
                                  input=b"data", pass_fds=(r,), capture_output=True)
        finally:
            os.close(r)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"empty password", proc.stderr)
        self.assertFalse((self.dir / "x").exists())


class TestSetupBackupsConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def test_shell_default_forms_are_evaluated_never_persisted_literally(self):
        cfg = self.dir / "config.env"
        cfg.write_text(
            'AI_VOLUME_PATH="${AI_VOLUME_PATH:-/Volumes/Work}"\n'
            'BACKUP_NAS_MOUNT="${BACKUP_NAS_MOUNT:-}"\n'
            'BACKUP_HOST_TAG="${BACKUP_HOST_TAG:-}"\n'
            'BACKUP_RESTIC_REPO="${BACKUP_RESTIC_REPO:-${AI_VOLUME_PATH}/AI/Backups/restic}"\n'
            'export BACKUP_ICLOUD_DIR="$HOME/Cloud/Nightly"\n'
            '# BACKUP_MEDIA_PATH="/commented/out"\n',
            encoding="utf-8",
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BACKUP_", "AI_VOLUME"))}
        with mock.patch.dict(os.environ, env, clear=True):
            values = setup_backups.read_env_file(cfg, self.dir)
        self.assertEqual(values, {
            "AI_VOLUME_PATH": "/Volumes/Work",
            "BACKUP_NAS_MOUNT": "",
            "BACKUP_HOST_TAG": "",
            "BACKUP_RESTIC_REPO": "/Volumes/Work/AI/Backups/restic",
            "BACKUP_ICLOUD_DIR": f"{self.dir}/Cloud/Nightly",
        })

    def test_fallback_parser_drops_unexpanded_values(self):
        cfg = self.dir / "config.env"
        cfg.write_text('A="${A:-x}"\nB="$HOME/b"\nC="plain"\n', encoding="utf-8")
        with mock.patch.object(setup_backups.subprocess, "run", side_effect=OSError("no zsh")):
            values = setup_backups.read_env_file(cfg, self.dir)
        self.assertEqual(values, {"B": f"{self.dir}/b", "C": "plain"})

    def test_volume_root(self):
        self.assertEqual(setup_backups.volume_root("/Volumes/My SSD/Workspace"), "/Volumes/My SSD")
        self.assertEqual(setup_backups.volume_root("/Volumes/My SSD"), "/Volumes/My SSD")
        self.assertEqual(setup_backups.volume_root("/Users/me/AI-Workspace"), "/Users/me/AI-Workspace")

    def test_new_installs_get_a_machine_suffix_configured_tags_stay(self):
        resolve = setup_backups.resolve_host_tag
        self.assertEqual(resolve("", "MacBook-Pro", suffix="a1b2c3")[0], "MacBook-Pro-a1b2c3")
        self.assertEqual(resolve("studio", "MacBook-Pro", suffix="a1b2c3")[0], "studio")
        self.assertEqual(resolve("MacBook-Pro", "MacBook-Pro", suffix="a1b2c3")[0], "MacBook-Pro",
                         "an existing install keeps the tag in its backup-config.env")
        # A tag inherited through a migration is re-derived, now with the suffix
        self.assertEqual(resolve("Old-Mac", "New-Mac", "Old-Mac.local", suffix="a1b2c3")[0], "New-Mac-a1b2c3")


class TestLibHelpers(BackupScriptCase):
    def lib(self, body: str, **config: str) -> str:
        self.write_config(**config)
        return self.run_zsh(self.script("helper.sh", body)).stdout.strip()

    def test_volume_root_of_a_workspace_folder(self):
        self.assertEqual(self.lib('print -r -- "$VOL_ROOT"', STORAGE_MODE="external",
                                  AI_VOLUME_PATH="/Volumes/My SSD/Workspace/AI-Root"), "/Volumes/My SSD")
        self.assertEqual(self.lib('print -r -- "$VOL_ROOT"', STORAGE_MODE="external",
                                  AI_VOLUME_PATH="/Volumes/My SSD"), "/Volumes/My SSD")
        import importlib.util
        spec = importlib.util.spec_from_file_location("volume_observer", SRC / "volume-observer.py")
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        self.assertEqual(observer.volume_root("/Volumes/My SSD/Workspace"), "/Volumes/My SSD")
        self.assertEqual(observer.volume_root("/Users/me/AI-Workspace"), "/Users/me/AI-Workspace")

    def test_extra_paths_are_trimmed_and_group_containers_skipped(self):
        out = self.lib('extra_paths " Work/A ,Library/Group Containers/x,~/Notes, " "Media"; print -rl -- "${reply[@]}"')
        self.assertEqual(out.splitlines(), ["Work/A", "Notes", "Media"])

    def test_default_icloud_folder(self):
        self.write_config()
        self.assertTrue(self.lib('print -r -- "$ICLOUD"').endswith("/com~apple~CloudDocs/Agent Backups/Nightly"))
        legacy = self.home / "Library/Mobile Documents/com~apple~CloudDocs/AI备份/每晚小包"
        legacy.mkdir(parents=True)
        self.assertEqual(self.lib('print -r -- "$ICLOUD"'), str(legacy))


@unittest.skipUnless(HAVE_ZSH and sys.platform == "darwin" and shutil.which("git"), "requires macOS zsh and git")
class TestBootstrap(unittest.TestCase):
    OFFICIAL = "https://github.com/Rem021/orca-multi-agent-blueprint.git"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.ws = self.root / "ws"

    def tearDown(self):
        self._tmp.cleanup()

    def checkout(self, rel: str, origin: str = OFFICIAL, git: bool = True) -> Path:
        d = self.ws / rel
        d.mkdir(parents=True)
        (d / "deploy.sh").write_text('print -r -- "DEPLOY $PWD ARGS: $*"\n')
        if git:
            subprocess.run(["git", "init", "-q", str(d)], check=True)
            subprocess.run(["git", "-C", str(d), "remote", "add", "origin", origin], check=True)
        return d  # untracked deploy.sh keeps it "dirty", so bootstrap never tries to pull

    def boot(self, *args: str) -> subprocess.CompletedProcess:
        env = {"HOME": str(self.root), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "INTERNAL_WORKSPACE_PATH": str(self.ws)}
        return subprocess.run(["/bin/zsh", str(REPO / "bootstrap.sh"), "--internal", *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_official_checkout_runs_with_arguments_passed_through(self):
        d = self.checkout("AI/Projects/orca-multi-agent-blueprint")
        r = self.boot("--no-supervisor")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"DEPLOY {d} ARGS: --auto --internal --no-supervisor", r.stdout)
        r = self.boot("--supervisor", "--weird-name-is-not-a-mode")
        self.assertNotEqual(r.returncode, 0, "a value that looks like a flag is refused")
        r = self.boot("--supervisor", "orca-mobile", "--dry-run")
        self.assertIn("ARGS: --internal --supervisor orca-mobile --dry-run", r.stdout)

    def test_existing_checkout_at_the_earlier_location_is_kept(self):
        d = self.checkout("AI/Projects/Mac/outputs/orca-multi-agent-blueprint", origin="git@github.com:Rem021/orca-multi-agent-blueprint.git")
        r = self.boot()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"DEPLOY {d} ARGS: --auto --internal", r.stdout)

    def test_foreign_or_unverifiable_checkouts_are_refused(self):
        d = self.checkout("AI/Projects/orca-multi-agent-blueprint", origin="https://github.com/someone/else.git")
        r = self.boot()
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("DEPLOY", r.stdout)
        self.assertIn("Refusing to run it", r.stderr)
        shutil.rmtree(d)
        self.checkout("AI/Projects/orca-multi-agent-blueprint", git=False)
        r = self.boot()
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("DEPLOY", r.stdout)
        self.assertIn("not a git checkout", r.stderr)

    def test_truncated_download_runs_nothing(self):
        text = (REPO / "bootstrap.sh").read_text()
        cut = text[: text.index("main \"$@\"\n", text.index("\nmain() {"))]
        r = subprocess.run(["/bin/zsh", "-s", "--", "--internal"], input=cut, capture_output=True, text=True,
                           env={"HOME": str(self.root), "PATH": "/usr/bin:/bin", "INTERNAL_WORKSPACE_PATH": str(self.ws)})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertFalse(self.ws.exists())


class TestResticPasswordCommand(unittest.TestCase):
    def test_lib_sh_preserves_positional_args_in_restic_password_command(self):
        import shlex
        with tempfile.TemporaryDirectory(prefix="lib-pw-test-") as tmp:
            fake_home = Path(tmp).resolve()
            (fake_home / ".orca").mkdir()
            (fake_home / ".orca/backup-config.env").write_text(
                'BACKUP_KEYCHAIN_RESTIC="svc-test"\nBACKUP_KEYCHAIN_ACCOUNT="acct-test"\n',
                encoding="utf-8",
            )
            r = subprocess.run(
                ["/bin/zsh", "-c", f'source "{REPO}/scripts/codex-storage/lib.sh"; printf "%s" "$RESTIC_PASSWORD_COMMAND"'],
                env={**os.environ, "HOME": str(fake_home)},
                capture_output=True,
                text=True,
                check=True,
            )
            cmd = r.stdout
            parts = shlex.split(cmd)
            self.assertEqual(parts[0:2], ["/bin/sh", "-c"])
            self.assertIn('"$1"', parts[2], f"positional $1 was prematurely expanded in: {cmd}")
            self.assertIn('"$2"', parts[2], f"positional $2 was prematurely expanded in: {cmd}")
            self.assertEqual(parts[3:], ["_", "svc-test", "acct-test"])


if __name__ == "__main__":
    unittest.main()
