#!/usr/bin/env python3
"""Comprehensive unit tests for agent_governor.py (v1.2.0) covering audit, adopt, leases, 5-gate GC, triage, restore & adversarial audit edge cases."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agent_governor as ag  # noqa: E402


class AgentGovernorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.ai_root = self.root / "AI"
        self.projects = self.ai_root / "Projects"
        self.trash = self.root / "Trash"
        self.home.mkdir(parents=True)
        self.projects.mkdir(parents=True)
        self.trash.mkdir(parents=True)
        self._chmod_restore: list[tuple[Path, int]] = []

        for rel in (".codex/AGENTS.md", ".claude/CLAUDE.md", ".gemini/GEMINI.md"):
            p = self.home / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# Global Rules\n- Keep rules concise.\n", encoding="utf-8")

    def tearDown(self) -> None:
        for p, mode in reversed(self._chmod_restore):
            try:
                if p.exists():
                    os.chmod(p, mode)
            except OSError:
                pass
        self.tmp.cleanup()

    def _lock_mode(self, p: Path, mode: int) -> None:
        orig = stat.S_IMODE(p.lstat().st_mode)
        self._chmod_restore.append((p, orig))
        os.chmod(p, mode)

    def _make_git_project(self, name: str, commit_initial: bool = False) -> Path:
        proj = self.projects / name
        proj.mkdir(parents=True)
        subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(proj), "init", "-q"], check=True)
        (proj / "AGENTS.md").write_text(f"# {name}\n- Run `pytest` before shipping.\n", encoding="utf-8")
        if commit_initial:
            subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(proj), "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(proj), "config", "user.name", "Tester"], check=True)
            subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(proj), "add", "AGENTS.md"], check=True)
            subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(proj), "commit", "-q", "-m", "init"], check=True)
        return proj

    def test_audit_detects_missing_pointers_zones_and_clutter_then_adopt_heals_and_restores_relocation(self) -> None:
        proj = self._make_git_project("Alpha", commit_initial=True)
        (proj / "tmp").mkdir()
        (proj / "tmp/probe.pdf").write_text("data", encoding="utf-8")
        (proj / ".worktrees").mkdir()
        (proj / "tmpclaude-9999-cwd").write_text("/tmp", encoding="utf-8")

        ns_audit = ag.build_parser().parse_args(["audit", "--root", str(self.projects), "--home", str(self.home)])
        rep = ag.run_audit(ns_audit)
        self.assertFalse(rep["summary"]["healthy"])
        p_rep = rep["projects"][0]
        self.assertIn("missing_CLAUDE.md", p_rep["issues"])
        self.assertIn("missing_GEMINI.md", p_rep["issues"])
        self.assertIn("work_not_in_git_info_exclude", p_rep["issues"])
        self.assertTrue(any("root_clutter_dir (tmp/)" in x for x in p_rep["issues"]))

        ns_adopt = ag.build_parser().parse_args(
            [
                "adopt",
                "--root",
                str(self.projects),
                "--home",
                str(self.home),
                "--relocate-clutter",
                "--trash-dir",
                str(self.trash),
                "--apply",
            ]
        )
        adopt_rep = ag.run_adopt(ns_adopt)
        self.assertGreater(adopt_rep["action_count"], 0)
        self.assertIsNotNone(adopt_rep["manifest"])
        self.assertEqual((proj / "CLAUDE.md").read_text(encoding="utf-8").strip(), "@AGENTS.md")
        self.assertEqual((proj / "GEMINI.md").read_text(encoding="utf-8").strip(), "@AGENTS.md")
        self.assertTrue((proj / "work/_scratch").is_dir())
        self.assertTrue((proj / "work/_tests").is_dir())
        self.assertTrue((proj / "work/retained").is_dir())
        self.assertTrue((proj / "work/.metadata_never_index").is_file())
        self.assertFalse((proj / ".worktrees").exists())
        self.assertFalse((proj / "tmp").exists())
        relocated = list((proj / "work/retained").glob("from-root-tmp-*/probe.pdf"))
        self.assertEqual(len(relocated), 1)

        # Verify `git status --porcelain` stays 100% clean
        st = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True, check=True)
        self.assertEqual(st.stdout.strip(), "")

        rep_after = ag.run_audit(ns_audit)
        self.assertTrue(rep_after["summary"]["healthy"], msg=str(rep_after["summary"]))

        # Verify `restore --latest --apply` reverses BOTH trashed items AND relocated root `tmp/`!
        ns_restore = ag.build_parser().parse_args(["restore", "--latest", "--trash-dir", str(self.trash), "--apply"])
        res = ag.run_restore(ns_restore)
        self.assertGreaterEqual(res["restored_count"], 3)
        self.assertTrue((proj / "tmp/probe.pdf").is_file())

    def test_adopt_skips_nonempty_worktrees_containing_git_checkout(self) -> None:
        proj = self._make_git_project("WorktreeGuard", commit_initial=True)
        nested_wt = proj / ".worktrees/feat-branch"
        nested_wt.mkdir(parents=True)
        (nested_wt / ".git").write_text("gitdir: /tmp/somewhere\n", encoding="utf-8")
        (nested_wt / "dirty.swift").write_text("let x = 1\n", encoding="utf-8")

        ns_adopt = ag.build_parser().parse_args(
            ["adopt", "--root", str(self.projects), "--home", str(self.home), "--relocate-clutter", "--trash-dir", str(self.trash), "--apply"]
        )
        rep = ag.run_adopt(ns_adopt)
        self.assertTrue((nested_wt / ".git").is_file())
        self.assertTrue(any(a["action"] == "skip_unsafe_root_clutter" for a in rep["actions"]))

    def test_scratch_and_test_dir_allocate_ancestor_pids_and_disambiguate_parallel_leases(self) -> None:
        proj = self._make_git_project("Beta", commit_initial=True)
        # Allocate via a transient subshell to verify ancestor_pids walks past transient subshell
        out = subprocess.check_output(
            [sys.executable, str(SCRIPT_DIR / "agent_governor.py"), "scratch", "--project", str(proj), "--agent", "codex", "--topic", "API Probe", "--ttl-hours", "48"],
            text=True,
        ).strip()
        sdir1 = Path(out)
        self.assertTrue(sdir1.is_dir())
        lease1 = json.loads((sdir1 / ".agent-lease.json").read_text(encoding="utf-8"))
        self.assertEqual(lease1["agent"], "codex")
        self.assertEqual(lease1["ttl_hours"], 48.0)
        self.assertIn(os.getpid(), lease1["ancestor_pids"])

        # Simulate a second live agent session on the same day with a different live PID
        ns_s2 = ag.build_parser().parse_args(
            ["scratch", "--project", str(proj), "--agent", "codex", "--topic", "API Probe"]
        )
        setattr(ns_s2, "pid", 999991)
        with mock.patch.object(ag, "pid_alive", return_value=True):
            sdir2 = ag.allocate_ephemeral_dir("scratch", ns_s2)
        self.assertNotEqual(sdir1, sdir2)

    def test_nested_worktrees_depth_1_2_3_discovered_for_gc_audit_and_triage(self) -> None:
        core = self._make_git_project("Core", commit_initial=True)
        wt_d1 = self.ai_root / "Claude/Worktrees/flat-wt"
        wt_d2 = self.ai_root / "Orca/Worktrees/Game/scylla"
        wt_d3 = self.ai_root / "Orca/Worktrees/Phayen/NEGA/flatback"

        for idx, wt in enumerate((wt_d1, wt_d2, wt_d3), 1):
            wt.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(core), "worktree", "add", "-q", "-b", f"wt-{idx}", str(wt)], check=True)
            s = wt / "work/_scratch/20261101-agent-stale"
            s.mkdir(parents=True)
            (s / "trace.log").write_text("log", encoding="utf-8")

        (wt_d3 / "tmp").mkdir()
        (wt_d2 / "work/retained/dataset").mkdir(parents=True)
        (wt_d2 / "work/retained/dataset/weights.bin").write_bytes(b"123456")

        discovered = ag.discover_worktree_checkouts(self.ai_root)
        self.assertEqual(set(discovered), {wt_d1, wt_d2, wt_d3})

        ns_audit = ag.build_parser().parse_args(["audit", "--root", str(self.projects), "--home", str(self.home)])
        audit_rep = ag.run_audit(ns_audit)
        self.assertEqual(len(audit_rep["worktrees_clutter"]), 1)
        self.assertIn("Orca/Worktrees/Phayen/NEGA/flatback", audit_rep["worktrees_clutter"][0]["worktree"])

        md_out = self.root / "wt-triage.md"
        ns_tr = ag.build_parser().parse_args(
            ["triage", "--root", str(self.projects), "--home", str(self.home), "--output", str(md_out)]
        )
        tr_rep = ag.run_triage(ns_tr)
        self.assertTrue(any("Game/scylla" in w["worktree"] for w in tr_rep["worktree_stranded_work"]))
        self.assertTrue(md_out.is_file())

        ref_now = time.time() + (6 * 86400)
        ns_gc = ag.build_parser().parse_args(
            [
                "gc",
                "--root",
                str(self.projects),
                "--home",
                str(self.home),
                "--trash-dir",
                str(self.trash),
                "--workspace-only",
                "--reference-now",
                str(ref_now),
                "--apply",
            ]
        )
        ag.run_gc(ns_gc)
        for wt in (wt_d1, wt_d2, wt_d3):
            self.assertFalse((wt / "work/_scratch/20261101-agent-stale").exists(), str(wt))
        self.assertTrue((wt_d2 / "work/retained/dataset/weights.bin").is_file())

    def test_gc_five_gates_lsof_flock_deep_mode_and_restore_workflows(self) -> None:
        proj = self._make_git_project("Gamma")
        for sub in ("_scratch", "_tests", "retained"):
            (proj / "work" / sub).mkdir(parents=True, exist_ok=True)

        base_now = time.time()
        ref_now = base_now + (10 * 86400)  # 10 days after creation
        mid_ts = ref_now - (36 * 3600)     # 36h before ref_now (kept by daily 72h, swept by deep 24h)

        # 1. Expired _scratch (created at base_now => 10d idle at ref_now)
        exp_scratch = proj / "work/_scratch/20261101-codex-old"
        exp_scratch.mkdir()
        (exp_scratch / "dump.json").write_text("{}", encoding="utf-8")

        # 2. `.pinned` marker -> protected by Gate 02
        pinned = proj / "work/_scratch/20261101-codex-pinned"
        pinned.mkdir()
        (pinned / ".pinned").write_text("", encoding="utf-8")

        # 2b. Nested `.keep` inside subdirectory -> protected by Gate 02
        nested_keep = proj / "work/_scratch/20261101-codex-nested-keep"
        (nested_keep / "data").mkdir(parents=True)
        (nested_keep / "data/.keep").write_text("", encoding="utf-8")

        # 3. Open file descriptor (`lsof`) -> protected by Gate 01
        lsof_busy = proj / "work/_scratch/20261101-codex-openfd"
        lsof_busy.mkdir()
        (lsof_busy / "live.log").write_text("streaming", encoding="utf-8")

        # 4. 36-hour-old scratch -> kept by daily, reclaimed by `--mode deep`
        mid_scratch = proj / "work/_scratch/20261112-codex-mid"
        mid_scratch.mkdir()
        f_mid = mid_scratch / "mid.txt"
        f_mid.write_text("mid", encoding="utf-8")
        os.utime(f_mid, (mid_ts, mid_ts))
        os.utime(mid_scratch, (mid_ts, mid_ts))

        # 5. Legacy untagged folder directly under work/ -> protected by Grandfathering
        legacy = proj / "work/sony-firmware-20261024"
        legacy.mkdir()
        (legacy / "rootfs.cpio").write_text("precious", encoding="utf-8")

        # Test flock contention in session-end mode
        lock_file = self.home / ".orca/locks/agent-governor-gc.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        with lock_file.open("w", encoding="utf-8") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            ns_locked = ag.build_parser().parse_args(
                ["gc", "--mode", "session-end", "--root", str(self.projects), "--home", str(self.home), "--apply"]
            )
            locked_rep = ag.run_gc(ns_locked)
            self.assertEqual(locked_rep["execution"], "skipped-locked")
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)

        ns_daily = ag.build_parser().parse_args(
            [
                "gc",
                "--root",
                str(self.projects),
                "--home",
                str(self.home),
                "--trash-dir",
                str(self.trash),
                "--skip-simulators",
                "--reference-now",
                str(ref_now),
                "--apply",
            ]
        )
        with mock.patch.object(ag, "find_lsof_busy_paths", return_value={lsof_busy}):
            daily_rep = ag.run_gc(ns_daily)

        self.assertFalse(exp_scratch.exists())
        self.assertTrue(pinned.exists())
        self.assertTrue(nested_keep.exists())
        self.assertTrue(lsof_busy.exists())
        self.assertTrue(mid_scratch.exists())
        self.assertTrue(legacy.exists())
        self.assertTrue(any(s["gate"] == "Gate01_OpenFileDescriptor" for s in daily_rep["protected_skips"]))

        time.sleep(0.02)
        ns_deep = ag.build_parser().parse_args(
            [
                "gc",
                "--mode",
                "deep",
                "--root",
                str(self.projects),
                "--home",
                str(self.home),
                "--trash-dir",
                str(self.trash),
                "--skip-simulators",
                "--reference-now",
                str(ref_now),
                "--apply",
            ]
        )
        with mock.patch.object(ag, "find_lsof_busy_paths", return_value={lsof_busy}):
            deep_rep = ag.run_gc(ns_deep)
        self.assertFalse(mid_scratch.exists())
        self.assertIsNotNone(deep_rep["manifest"])

        ns_dry = ag.build_parser().parse_args(["restore", "--latest", "--trash-dir", str(self.trash), "--dry-run"])
        dry_res = ag.run_restore(ns_dry)
        self.assertEqual(dry_res["mode"], "dry-run")
        self.assertEqual(dry_res["restored_count"], 1)
        self.assertFalse(mid_scratch.exists())

        ns_apply1 = ag.build_parser().parse_args(["restore", "--latest", "--trash-dir", str(self.trash), "--apply"])
        res1 = ag.run_restore(ns_apply1)
        self.assertEqual(res1["restored_count"], 1)
        self.assertTrue(mid_scratch.exists())

        res2 = ag.run_restore(ns_apply1)
        self.assertEqual(res2["restored_count"], 1)
        self.assertTrue(exp_scratch.exists())

    def test_cp_rp_old_mtime_protected_by_ctime_then_reaped_after_real_ttl(self) -> None:
        proj = self._make_git_project("CopyArchive")
        sc = proj / "work/_scratch"
        sc.mkdir(parents=True)
        old_src = self.root / "old_archive_src"
        old_src.mkdir()
        f = old_src / "dataset.bin"
        f.write_bytes(b"x" * 256)
        thirty_days_ago = time.time() - 30 * 86400
        os.utime(f, (thirty_days_ago, thirty_days_ago))
        os.utime(old_src, (thirty_days_ago, thirty_days_ago))

        copied = sc / "copied_just_now"
        subprocess.run(["cp", "-Rp", str(old_src), str(copied)], check=True)

        # Immediately after `cp -Rp`, mtime is 30d old, but ctime is NOW -> MUST NOT be reaped!
        ns_now = ag.build_parser().parse_args(
            ["gc", "--root", str(self.projects), "--home", str(self.home), "--trash-dir", str(self.trash), "--workspace-only", "--apply"]
        )
        rep_now = ag.run_gc(ns_now)
        self.assertTrue(copied.is_dir())
        self.assertEqual(rep_now["total_candidates"], 0)

        # 4 days later, both mtime (34d) and ctime (4d) exceed 3d scratch TTL -> reaped cleanly!
        ns_later = ag.build_parser().parse_args(
            [
                "gc",
                "--root",
                str(self.projects),
                "--home",
                str(self.home),
                "--trash-dir",
                str(self.trash),
                "--workspace-only",
                "--reference-now",
                str(time.time() + 4 * 86400),
                "--apply",
            ]
        )
        ag.run_gc(ns_later)
        self.assertFalse(copied.exists())

    def test_codex_arg0_runtime_shim_never_reaped(self) -> None:
        self._make_git_project("Dummy")
        codex_tmp = self.home / ".codex/tmp"
        arg0_dir = codex_tmp / "arg0-macos-dispatch"
        stale_tmp = codex_tmp / "stale-output.txt"
        arg0_dir.mkdir(parents=True)
        (arg0_dir / "apply_patch").write_text("#!/bin/sh\n", encoding="utf-8")
        stale_tmp.write_text("old", encoding="utf-8")

        ns_gc = ag.build_parser().parse_args(
            [
                "gc",
                "--root",
                str(self.projects),
                "--home",
                str(self.home),
                "--trash-dir",
                str(self.trash),
                "--skip-simulators",
                "--reference-now",
                str(time.time() + 5 * 86400),
                "--apply",
            ]
        )
        ag.run_gc(ns_gc)
        self.assertTrue((arg0_dir / "apply_patch").is_file(), "arg0 dispatch shim must survive GC")
        self.assertFalse(stale_tmp.exists(), "regular stale tmp file should be reaped")

    def test_extended_lease_ttl_30d_and_session_end_isolation(self) -> None:
        proj = self._make_git_project("LeaseHonor")
        ns_30d = ag.build_parser().parse_args(
            ["scratch", "--project", str(proj), "--agent", "claude", "--topic", "long-benchmark", "--ttl-days", "30"]
        )
        setattr(ns_30d, "pid", 999992)  # non-existent PID so only lease TTL protects it
        long_dir = ag.allocate_ephemeral_dir("scratch", ns_30d)
        (long_dir / "results.csv").write_text("1,2,3\n", encoding="utf-8")

        # At +10 days (> default 3d scratch TTL, but < 30d lease): MUST be protected by Gate02_ActiveLeaseTTL!
        ns_day10 = ag.build_parser().parse_args(
            [
                "gc",
                "--root",
                str(self.projects),
                "--home",
                str(self.home),
                "--trash-dir",
                str(self.trash),
                "--workspace-only",
                "--reference-now",
                str(time.time() + 10 * 86400),
                "--apply",
            ]
        )
        with mock.patch.object(ag, "pid_alive", return_value=False):
            rep10 = ag.run_gc(ns_day10)
        self.assertTrue(long_dir.is_dir())
        self.assertTrue(any(s["gate"] == "Gate02_ActiveLeaseTTL" for s in rep10["protected_skips"]))

        # At +31 days: lease expired -> reaped!
        ns_day31 = ag.build_parser().parse_args(
            [
                "gc",
                "--root",
                str(self.projects),
                "--home",
                str(self.home),
                "--trash-dir",
                str(self.trash),
                "--workspace-only",
                "--reference-now",
                str(time.time() + 31 * 86400),
                "--apply",
            ]
        )
        with mock.patch.object(ag, "pid_alive", return_value=False):
            ag.run_gc(ns_day31)
        self.assertFalse(long_dir.exists())

        # Session-end isolation: another session's 10-minute-old empty scratch dir is NOT reaped,
        # while the exiting session's own empty scratch dir IS reaped immediately!
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sess-other"}, clear=False):
            ns_other = ag.build_parser().parse_args(["scratch", "--project", str(proj), "--agent", "claude", "--topic", "other-empty"])
            setattr(ns_other, "pid", 999993)
            other_empty = ag.allocate_ephemeral_dir("scratch", ns_other)

        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sess-exiting"}, clear=False):
            ns_mine = ag.build_parser().parse_args(["scratch", "--project", str(proj), "--agent", "claude", "--topic", "my-empty"])
            setattr(ns_mine, "pid", 999994)
            my_empty = ag.allocate_ephemeral_dir("scratch", ns_mine)

            ns_se = ag.build_parser().parse_args(
                [
                    "gc",
                    "--mode",
                    "session-end",
                    "--root",
                    str(self.projects),
                    "--home",
                    str(self.home),
                    "--trash-dir",
                    str(self.trash),
                    "--reference-now",
                    str(time.time() + 600),  # +10 minutes
                    "--apply",
                ]
            )
            with mock.patch.object(ag, "pid_alive", return_value=False):
                ag.run_gc(ns_se)

        self.assertFalse(my_empty.exists(), "exiting session's own empty dir should be reaped")
        self.assertTrue(other_empty.is_dir(), "another session's 10m-old empty dir must NOT be reaped")

    def test_fail_closed_unreadable_dirs_nested_git_ps_failure_mountpoint_and_dup_readonly_staging(self) -> None:
        p1 = self._make_git_project("P1")
        p2 = self._make_git_project("P2")
        for p in (p1, p2):
            (p / "work/_scratch").mkdir(parents=True)
            (p / "work/_tests").mkdir(parents=True)

        # 1. Nested git repo inside _scratch -> protected by Gate03_NestedGitRepoGuard
        nested_git = p1 / "work/_scratch/nested_repo"
        nested_git.mkdir()
        subprocess.run(["git", "-c", "core.fsmonitor=false", "init", "-q", str(nested_git)], check=True)
        (nested_git / "uncommitted.py").write_text("x = 1\n", encoding="utf-8")

        # 2. Unreadable subdir inside _scratch + unreadable _tests tier dir -> no crash, fail-closed!
        unreadable_cand = p1 / "work/_scratch/unreadable_sub"
        (unreadable_cand / "locked").mkdir(parents=True)
        (unreadable_cand / "locked/live.db").write_text("secret", encoding="utf-8")
        self._lock_mode(unreadable_cand / "locked", 0o000)
        self._lock_mode(p2 / "work/_tests", 0o000)

        # 3. Simulated mounted subvolume inside _scratch -> Gate03_MountpointBoundaryGuard
        mounted_cand = p1 / "work/_scratch/with_dmg_mount"
        (mounted_cand / "mnt").mkdir(parents=True)

        # 4. Duplicate basename across P1 and P2 containing 0o500 read-only subdirectory `dup/ro`
        for p in (p1, p2):
            dup = p / "work/_scratch/dup"
            (dup / "ro").mkdir(parents=True)
            (dup / "ro/f.txt").write_text(p.name, encoding="utf-8")
            self._lock_mode(dup / "ro", 0o500)

        ref_now = time.time() + 10 * 86400
        ns_gc = ag.build_parser().parse_args(
            [
                "gc",
                "--root",
                str(self.projects),
                "--home",
                str(self.home),
                "--trash-dir",
                str(self.trash),
                "--workspace-only",
                "--reference-now",
                str(ref_now),
                "--apply",
            ]
        )

        # Verify empty `ps` snapshot aborts fail-closed without deleting anything
        with mock.patch.object(ag, "snapshot_running_processes", return_value=(set(), [])):
            aborted = ag.run_gc(ns_gc)
        self.assertEqual(aborted["execution"], "aborted-process-check-failed")
        self.assertTrue((p1 / "work/_scratch/dup").exists())

        # Verify mountpoint check + unreadable fail-closed + nested git + duplicate read-only atomic staging
        real_ismount = os.path.ismount
        with mock.patch("os.path.ismount", side_effect=lambda x: Path(x) == (mounted_cand / "mnt") or real_ismount(x)):
            rep = ag.run_gc(ns_gc)

        self.assertEqual(rep["errors"], [])
        self.assertTrue(nested_git.is_dir())
        self.assertTrue(unreadable_cand.is_dir())
        self.assertTrue(mounted_cand.is_dir())
        self.assertFalse((p1 / "work/_scratch/dup").exists())
        self.assertFalse((p2 / "work/_scratch/dup").exists())
        gates = {s["gate"] for s in rep["protected_skips"]}
        self.assertIn("Gate03_NestedGitRepoGuard", gates)
        self.assertIn("Gate03_UnreadableFailClosed", gates)
        self.assertIn("Gate03_MountpointBoundaryGuard", gates)

        # Restore both `dup` directories cleanly
        ns_res = ag.build_parser().parse_args(["restore", "--latest", "--trash-dir", str(self.trash), "--apply"])
        res = ag.run_restore(ns_res)
        self.assertEqual(res["restored_count"], 2)
        self.assertEqual((p1 / "work/_scratch/dup/ro/f.txt").read_text(encoding="utf-8"), "P1")
        self.assertEqual((p2 / "work/_scratch/dup/ro/f.txt").read_text(encoding="utf-8"), "P2")

    def test_cross_device_trash_guard_and_git_ls_files_failure_fail_closed(self) -> None:
        proj = self._make_git_project("XDevAndGit", commit_initial=True)
        cand = proj / "work/_scratch/stale_item"
        cand.mkdir(parents=True)
        (cand / "a.txt").write_text("a", encoding="utf-8")

        # 1. If git ls-files fails (e.g. corrupt index / timeout), has_tracked MUST return True (fail-closed)
        idx = ag.GitTrackedIndex()
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=4)):
            self.assertTrue(idx.has_tracked(cand))

        # 2. If Trash resides on a different st_dev and no same-volume .Trashes exists, skip via Gate05_CrossDeviceTrashGuard
        ns_gc = ag.build_parser().parse_args(
            [
                "gc",
                "--root",
                str(self.projects),
                "--home",
                str(self.home),
                "--trash-dir",
                str(self.trash),
                "--workspace-only",
                "--reference-now",
                str(time.time() + 10 * 86400),
                "--apply",
            ]
        )
        with mock.patch.object(ag.TrashStager, "_base_dev", return_value=-999999):
            rep = ag.run_gc(ns_gc)
        self.assertTrue(cand.is_dir(), "must never copy+rmtree across devices")
        self.assertTrue(any(s["gate"] == "Gate05_CrossDeviceTrashGuard" for s in rep["protected_skips"]))

    def test_nested_subrepo_gc_and_anchored_simulator_regex(self) -> None:
        proj = self._make_git_project("MacLike")
        for z in ag.FOUR_ZONES:
            (proj / z).mkdir(exist_ok=True)
        subrepo = proj / "outputs/my-tool"
        subrepo.mkdir(parents=True)
        subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(subrepo), "init", "-q"], check=True)
        leaked = subrepo / "tmpclaude-1234-cwd"
        leaked.write_text("cwd", encoding="utf-8")

        ns_gc = ag.build_parser().parse_args(
            [
                "gc",
                "--root",
                str(self.projects),
                "--home",
                str(self.home),
                "--trash-dir",
                str(self.trash),
                "--workspace-only",
                "--reference-now",
                str(time.time() + 5 * 86400),
                "--apply",
            ]
        )
        ag.run_gc(ns_gc)
        self.assertFalse(leaked.exists(), "nested outputs/<subrepo> leaked tmpclaude should be cleaned")

        # Anchored simulator regex checks
        self.assertIsNotNone(ag.EPHEMERAL_SIM_RE.search("agent-tmp-iphone16"))
        self.assertIsNotNone(ag.EPHEMERAL_SIM_RE.search("scratch-sim-ui"))
        self.assertIsNotNone(ag.EPHEMERAL_SIM_RE.search("iPhone 16 agent-tmp"))
        self.assertIsNone(ag.EPHEMERAL_SIM_RE.search("iPhone 16 测试主力"))
        self.assertIsNone(ag.EPHEMERAL_SIM_RE.search("Scratchpad iPhone Pro"))

    def test_yaml_block_scalar_skill_budget_and_codeblock_lint_immunity(self) -> None:
        proj = self._make_git_project("Epsilon")
        for z in ag.FOUR_ZONES:
            (proj / z).mkdir(exist_ok=True)
        for t in ag.WORK_TIERS:
            (proj / "work" / t).mkdir(parents=True, exist_ok=True)
        (proj / "work/.metadata_never_index").write_text("", encoding="utf-8")
        ag.ensure_git_info_exclude(proj, "/work/", apply=True)
        (proj / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        (proj / "GEMINI.md").write_text("@AGENTS.md\n", encoding="utf-8")

        (proj / "AGENTS.md").write_text(
            "# Epsilon\n"
            "- Example inline `[example](does-not-exist.md)` and `${EXAMPLE_ENV}`.\n"
            "```bash\n"
            "echo ${UNSET_VAR} # [code-link](missing-in-code.md)\n"
            "```\n",
            encoding="utf-8",
        )

        sdir = self.home / ".agents/skills/big-skill"
        sdir.mkdir(parents=True)
        skill_body = "\n".join([f"Line {i}" for i in range(510)])
        (sdir / "SKILL.md").write_text(
            "---\nname: big-skill\ndescription: >-\n  Multi-line folded YAML description\n  spanning two indented lines.\n---\n"
            + skill_body
            + "\n",
            encoding="utf-8",
        )

        meta = ag.parse_skill_frontmatter(sdir / "SKILL.md")
        self.assertGreater(meta["description_chars"], 50)
        self.assertGreater(meta["body_lines"], 500)

        ns_audit = ag.build_parser().parse_args(["audit", "--root", str(self.projects), "--home", str(self.home)])
        rep = ag.run_audit(ns_audit)
        self.assertTrue(rep["summary"]["healthy"], msg=str(rep["summary"]))
        self.assertEqual(len(rep["skills_budget"]["oversized_skill_files"]), 1)

        with (proj / "AGENTS.md").open("a", encoding="utf-8") as fh:
            fh.write("\nSee [Broken Spec](docs/missing-spec.md) and {{UNRESOLVED_TOKEN}}.\n")
        rep_bad = ag.run_audit(ns_audit)
        self.assertFalse(rep_bad["summary"]["healthy"])
        issues = rep_bad["projects"][0]["issues"]
        self.assertTrue(any("broken_doc_link" in x for x in issues))
        self.assertTrue(any("unresolved_placeholders" in x for x in issues))



    def test_custom_home_ignores_host_simulators_outside_home(self):
        fake_sim_json = json.dumps(
            {
                "devices": {
                    "com.apple.CoreSimulator.SimRuntime.iOS-27-0": [
                        {
                            "udid": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
                            "name": "test-ephemeral-sim",
                            "state": "Shutdown",
                            "dataPath": "/Users/realhost/Library/Developer/CoreSimulator/Devices/AAAAAAAA/data",
                        }
                    ]
                }
            }
        )
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "home"
            fake_home.mkdir()
            with unittest.mock.patch("subprocess.run") as mrun, unittest.mock.patch("pathlib.Path.is_dir", return_value=True):
                mrun.return_value = subprocess.CompletedProcess(["xcrun"], 0, stdout=fake_sim_json, stderr="")
                cands = ag.scan_ephemeral_simulators(
                    now_ts=time.time() + 999999,
                    sim_ttl_h=1.0,
                    protected_udids=set(),
                    proc_cmds=[],
                    home=fake_home,
                )
                self.assertEqual(cands, [])


if __name__ == "__main__":
    unittest.main()
