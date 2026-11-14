#!/usr/bin/env python3
"""Tests for the scheduled backup scripts (scripts/codex-storage): job lock, status receipts,
skip escalation, NAS diagnostics, SQLite snapshots and iCloud package pruning.

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

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "scripts/codex-storage"
sys.path.insert(0, str(SRC))

import sqlite_snapshot  # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"
HAVE_ZSH = Path("/bin/zsh").exists()


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


class TestJobLock(BackupScriptCase):
    def test_lock_is_held_until_the_script_exits(self):
        # Regression: an EXIT trap set inside a zsh function fires when that function returns,
        # which used to release the lock immediately after take_lock.
        s = self.script("held.sh", 'take_lock demo\n[[ -d "$ST/.lock-demo" ]] && echo HELD || echo GONE\n')
        r = self.run_zsh(s)
        self.assertEqual(r.stdout.strip(), "HELD", r.stderr)
        self.assertFalse((self.status / ".lock-demo").exists(), "lock must be released on exit")

    def test_second_instance_is_skipped_while_first_runs(self):
        s = self.script("slow.sh", 'take_lock demo\necho started\nsleep "${1:-0}"\necho finished\n')
        first = subprocess.Popen(
            ["/bin/zsh", str(s), "3"], env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            deadline = time.time() + 5
            while not (self.status / ".lock-demo/pid").exists() and time.time() < deadline:
                time.sleep(0.05)
            second = self.run_zsh(s)
            self.assertEqual(second.stdout, "", "second instance must exit before doing any work")
            self.assertTrue((self.status / ".lock-demo").is_dir(), "second instance must not release the lock")
        finally:
            out, _ = first.communicate(timeout=15)
        self.assertIn("finished", out)
        self.assertFalse((self.status / ".lock-demo").exists())

    def test_stale_lock_of_dead_owner_is_reclaimed(self):
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
        self.assertFalse((self.status / ".lock-demo").exists())


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
        s = self.script("skip.sh", 'skip_job demo "NAS 未挂载" "${1:-24}"\n')
        self.run_zsh(s)
        self.assertEqual(self.receipt("demo")["state"], "skipped")
        stamp = time.strftime(FMT, time.localtime(time.time() - 30 * 3600))
        (self.status / "demo.json").write_text(
            json.dumps({"name": "demo", "state": "skipped", "message": "x", "time": stamp, "last_ok": stamp})
        )
        self.run_zsh(s)
        stale = self.receipt("demo")
        self.assertEqual(stale["state"], "stale")
        self.assertIn("30 小时", stale["message"])
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
        self.assertIn("restic 仓库不存在", self.reason())

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
        self.assertTrue("不可达" in reason or "没有保存的 SMB 凭据" in reason, reason)

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
        self.assertIn("备份卷未挂载", self.reason())


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
        self.write_config(BACKUP_ICLOUD_DIR=str(self.icloud), BACKUP_HOST_TAG="TestMac", BACKUP_ICLOUD_KEEP_LATEST="1")
        stub = self.home / ".local/bin/security"
        stub.write_text('#!/bin/sh\n[ "$1" = find-generic-password ] && { echo test-passphrase; exit 0; }\nexit 1\n')
        stub.chmod(0o755)
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
        free_kb = shutil.disk_usage(self.home).free // 1024
        if free_kb < 8388608:
            self.skipTest("needs 8 GiB free space")
        self.icloud.mkdir(parents=True)
        foreign = self.icloud / "mac-agents-OtherMac-20260101-000000.tar.gz.enc"
        foreign.write_text("belongs to another host")
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

        # The published package decrypts and contains the consistent SQLite snapshot
        out = self.home / "restore"
        out.mkdir()
        dec = subprocess.run(
            f"openssl enc -d -aes-256-cbc -pbkdf2 -in '{self.icloud / mine[0]}' -pass pass:test-passphrase"
            f" | tar -xzf - -C '{out}'",
            shell=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(dec.returncode, 0, dec.stderr)
        self.assertTrue((out / ".claude/settings.json").is_file())
        snap = out / ".orca/sqlite-backups/codex-state_5.sqlite"
        con = sqlite3.connect(snap)
        try:
            self.assertEqual(con.execute("SELECT v FROM t").fetchone()[0], "kept")
        finally:
            con.close()
        self.assertFalse((self.status / ".lock-icloud-package").exists())
        self.assertFalse((self.home / ".orca/sqlite-backups").exists(), "staging area is cleaned up")




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
        locked_ops = [c for c in calls if any(op in c for op in (" backup ", " forget ", " check ", " stats ", " restore "))]
        self.assertGreaterEqual(len(locked_ops), 5, calls)
        for c in locked_ops:
            self.assertIn("--retry-lock 7m", c, f"Missing --retry-lock on restic invocation: {c}")

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
        self.assertIn("回收 1 项", rec["message"])
        self.assertFalse(stale.exists(), "expired _scratch item should be moved to Trash")
        self.assertTrue(fresh.exists(), "fresh _scratch item must stay")
        self.assertTrue(legacy.exists(), "grandfathered legacy work/ folder must never be touched")


if __name__ == "__main__":
    unittest.main()
