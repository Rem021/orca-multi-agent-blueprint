#!/usr/bin/env python3
"""Comprehensive unit tests for agent_governor.py covering audit, adopt, leases, 5-gate GC, triage, restore & adversarial audit edge cases."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import re
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
        # Hermetic: the host agent's session identity must not leak into lease reuse / session-end decisions
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for var in ("ORCA_PANE_KEY", "CLAUDE_SESSION_ID", "AI_VOLUME_PATH"):
            os.environ.pop(var, None)

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

    def _allocate(self, zone: str, argv: list[str], owner_pids: list[int]) -> Path:
        """Allocate a lease as if created by a process chain `owner_pids` (e.g. a PID that does not exist)."""
        ns = ag.build_parser().parse_args(argv)
        with mock.patch.object(ag, "collect_ancestor_pids", return_value=list(owner_pids)):
            return ag.allocate_ephemeral_dir(zone, ns)

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

    def test_fresh_entries_are_not_walked_in_full(self) -> None:
        """Only entries that are actually due pay for the full tree inspection."""
        proj = self._make_git_project("Speed")
        fresh = proj / "work/_tests/fresh-build"
        stale = proj / "work/_tests/stale-build"
        for d in (fresh, stale):
            (d / "deep/tree").mkdir(parents=True)
            (d / "deep/tree/out.bin").write_bytes(b"x" * 16)
        # ctime cannot be moved back, so "now" moves forward: everything created above is 30 days old,
        # except one file deep inside the fresh entry that was touched a minute ago
        now = time.time() + 30 * 86400
        os.utime(fresh / "deep/tree/out.bin", (now - 60, now - 60))
        inspected = []
        real_inspect = ag.inspect_tree

        def spy(path):
            inspected.append(path.name)
            return real_inspect(path)

        with mock.patch.object(ag, "inspect_tree", side_effect=spy):
            cands, skips = ag.scan_work_ephemeral_candidates(
                [proj], now, scratch_ttl_h=72, tests_ttl_h=168, min_idle_h=2, live_pids=set(), proc_cmds=[]
            )
        self.assertEqual([c.path.name for c in cands], ["stale-build"])
        self.assertEqual(inspected, ["stale-build"])
        self.assertIn({"path": str(fresh), "gate": "Gate01_MinIdleGuard"}, skips)
        self.assertEqual(ag.first_activity_after(stale, now - 3600), 0.0)
        self.assertGreater(ag.first_activity_after(fresh, now - 3600), now - 3600)

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
        self.assertNotIn("missing_GEMINI.md", p_rep["issues"])
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
        self.assertFalse((proj / "GEMINI.md").exists(), "adopt must not recreate redundant GEMINI.md shims")
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
        # Every owner is recorded with its process start time so a reused PID is not mistaken for the owner
        owners = {pid: lstart for pid, lstart in lease1["owners"]}
        self.assertEqual(owners[os.getpid()], ag.process_lstart(os.getpid()))

        # Simulate a second live agent session on the same day with a different live PID
        with mock.patch.object(ag, "pid_alive", return_value=True):
            sdir2 = self._allocate(
                "scratch", ["scratch", "--project", str(proj), "--agent", "codex", "--topic", "API Probe"], [999991]
            )
        self.assertNotEqual(sdir1, sdir2)

    def test_nested_worktrees_depth_1_2_3_discovered_for_gc_audit_and_triage(self) -> None:
        core = self._make_git_project("Core", commit_initial=True)
        wt_d1 = self.ai_root / "Claude/Worktrees/flat-wt"
        wt_d2 = self.ai_root / "Orca/Worktrees/Game/scylla"
        wt_d3 = self.ai_root / "Orca/Worktrees/ExampleApp/Camera/flatback"

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
        self.assertIn("Orca/Worktrees/ExampleApp/Camera/flatback", audit_rep["worktrees_clutter"][0]["worktree"])

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
        # non-existent owner PID so only the lease TTL protects it
        long_dir = self._allocate(
            "scratch",
            ["scratch", "--project", str(proj), "--agent", "claude", "--topic", "long-benchmark", "--ttl-days", "30"],
            [999992],
        )
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
            other_empty = self._allocate(
                "scratch", ["scratch", "--project", str(proj), "--agent", "claude", "--topic", "other-empty"], [999993]
            )

        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sess-exiting"}, clear=False):
            my_empty = self._allocate(
                "scratch", ["scratch", "--project", str(proj), "--agent", "claude", "--topic", "my-empty"], [999994]
            )

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
        self.assertIsNone(ag.EPHEMERAL_SIM_RE.search("Clone of iPhone Pro"))

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



    def test_gc_preserves_simulator_leases_and_live_test_owner(self):
        udid = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
        data = self.home / "device-data"
        data.mkdir()
        coord = self.home / "coord"
        coord.mkdir()
        finished = subprocess.Popen(["/usr/bin/true"])
        finished.wait()
        # Exact agent-test-device naming (scripts/test_device.py) with an owner that has exited
        dev = {"udid": udid, "name": f"agent-tmp-job-{finished.pid}-{'a' * 32}", "state": "Shutdown", "dataPath": str(data)}
        def inventory(*args, **kwargs):
            return subprocess.CompletedProcess([], 0, stdout=json.dumps({"devices": {"runtime": [dev]}}))
        def scan(broad: bool = False):
            return ag.scan_ephemeral_simulators(time.time(), 0, set(), [], self.home, coord, broad_names=broad)
        with mock.patch.object(ag.subprocess, "run", side_effect=inventory), mock.patch.object(Path, "is_file", return_value=True):
            self.assertEqual(len(scan()), 1)
            with ag.reserve_simulator_for_gc(coord, udid) as reserved:
                self.assertTrue(reserved)
                self.assertEqual(scan(), [])
                with ag.reserve_simulator_for_gc(coord, udid) as reserved_again:
                    self.assertFalse(reserved_again)
            self.assertEqual(len(scan()), 1)
            self.assertEqual(ag.scan_ephemeral_simulators(time.time(), 0, {udid}, [], self.home, coord), [], "protected UDID")
            dev["name"] = f"agent-tmp-job-{os.getpid()}-{'a' * 32}"
            self.assertEqual(scan(), [], "live agent-test-device owner keeps its simulator")
            # Names that merely look ephemeral (possibly shared/user-made) are only reclaimed when explicitly asked
            for name in ("agent-tmp-test", "Ephemeral iPhone", "QA team ephemeral-sim"):
                dev["name"] = name
                self.assertEqual(scan(), [], name)
                self.assertEqual(len(scan(broad=True)), 1, name)

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
                    broad_names=True,
                )
                self.assertEqual(cands, [])


    def test_r1_real_child_process_holding_nested_grandchild_fd_blocks_gc(self) -> None:
        """R1: Real child process holding an open FD on `<cand>/a/b/live.bin` without `<cand>` in argv or cwd."""
        proj = self._make_git_project("RealLsofNested")
        cand = proj / "work/_scratch/20261101-codex-nested-open"
        grandchild = cand / "a/b/live.bin"
        grandchild.parent.mkdir(parents=True)
        grandchild.write_bytes(b"active-stream")

        # Pass target path via stdin so child's `ps` command line does NOT contain `cand`!
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys, time; p = sys.stdin.readline().strip(); fh = open(p, 'rb'); sys.stdout.write('READY\\n'); sys.stdout.flush(); time.sleep(30)",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/tmp",
            text=True,
        )
        try:
            assert child.stdin is not None and child.stdout is not None
            child.stdin.write(str(grandchild) + "\n")
            child.stdin.flush()
            ready = child.stdout.readline().strip()
            self.assertEqual(ready, "READY")

            busy = ag.find_lsof_busy_paths([cand])
            self.assertIn(cand, busy, "Parent candidate directory must be marked busy when grandchild FD is open")

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
                    "--skip-simulators",
                    "--reference-now",
                    str(time.time() + 10 * 86400),
                    "--apply",
                ]
            )
            rep = ag.run_gc(ns_gc)
            self.assertTrue(grandchild.is_file(), "Directory with open grandchild FD must never be moved to Trash")
            self.assertTrue(
                any(s["path"] == str(cand) and s["gate"] == "Gate01_OpenFileDescriptor" for s in rep["protected_skips"])
            )
        finally:
            child.kill()
            child.wait(timeout=5)
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe is not None:
                    pipe.close()

    def test_r2_lock_and_lsof_errors_fail_closed(self) -> None:
        """R2: Lock EACCES / EIO / ENOENT and lsof rc=2 / stderr warnings / truncated stdout must fail-closed."""
        proj = self._make_git_project("FailClosedLockLsof")
        cand = proj / "work/_scratch/20261101-codex-expired"
        cand.mkdir(parents=True)
        (cand / "payload.txt").write_text("keep-safe", encoding="utf-8")

        for exc in (
            PermissionError(errno.EACCES, "Permission denied"),
            FileNotFoundError(errno.ENOENT, "No such file or directory"),
        ):
            errs: list[str] = []
            with mock.patch.object(Path, "mkdir", side_effect=exc):
                with ag.acquire_gc_lock(self.home / "bad-lock", error_out=errs) as allowed:
                    self.assertFalse(allowed)
            self.assertTrue(errs)

        errs_eio: list[str] = []
        with mock.patch.object(ag.fcntl, "flock", side_effect=OSError(errno.EIO, "Input/output error")):
            with ag.acquire_gc_lock(self.home / "eio-lock", error_out=errs_eio) as allowed:
                self.assertFalse(allowed)
        self.assertTrue(errs_eio)

        # Verify `run_gc --apply` aborts fail-closed without moving `cand` when lock fails with EIO
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
                "--skip-simulators",
                "--reference-now",
                str(time.time() + 10 * 86400),
                "--apply",
            ]
        )
        with mock.patch.object(ag.fcntl, "flock", side_effect=OSError(errno.EIO, "Input/output error")):
            rep_abort = ag.run_gc(ns_gc)
        self.assertEqual(rep_abort["execution"], "aborted-lock-error")
        self.assertTrue(cand.is_dir())
        self.assertTrue(rep_abort["errors"])

        # lsof non-(0,1) exit codes, stderr warnings, rc=0 empty stdout, and truncated stdout all fail-closed
        bad_lsof_runs = [
            subprocess.CompletedProcess(["lsof"], 2, "", "lsof: fatal error"),
            subprocess.CompletedProcess(["lsof"], 1, "", "lsof: WARNING: can't opendir(...): Permission denied\n"),
            subprocess.CompletedProcess(["lsof"], 0, "", ""),
            subprocess.CompletedProcess(["lsof"], 0, "p123\nn/incomplete-no-newline", ""),
            subprocess.CompletedProcess(["lsof"], 0, "corrupt_line\n", ""),
        ]
        for fake_cp in bad_lsof_runs:
            with mock.patch.object(ag.subprocess, "run", return_value=fake_cp):
                self.assertEqual(ag.find_lsof_busy_paths([cand]), {cand}, f"Must fail-closed on {fake_cp!r}")

    def test_r4_wal_journal_stepwise_crash_recovery_and_immediate_manifest(self) -> None:
        """R4: Every staged item has an immediate manifest + WAL receipt, and mid-rename crashes recover cleanly."""
        # Case A: Normal `stage()` immediately writes both `journal.jsonl` and `manifest.json` before final flush
        src_a = self.root / "immediate_item"
        src_a.write_text("alpha", encoding="utf-8")
        stager_a = ag.TrashStager(trash_root=self.trash, run_label="wal-a")
        dest_a = stager_a.stage(src_a, reason="probe", category="test", size_bytes=5)
        self.assertFalse(src_a.exists())
        self.assertTrue(dest_a.is_file())
        self.assertTrue(stager_a.journal_path.is_file())
        self.assertTrue(stager_a.manifest_path.is_file())

        # Case B: Simulated crash AFTER `os.rename` succeeds, BEFORE `staged` WAL record & `manifest.json` are written!
        trash_b = self.root / "TrashCrashAfterRename"
        trash_b.mkdir()
        src_b = self.root / "crash_after_rename_dir"
        src_b.mkdir()
        (src_b / "important.txt").write_text("recover-me", encoding="utf-8")
        stager_b = ag.TrashStager(trash_root=trash_b, run_label="wal-b")
        real_append = ag._append_wal_record

        def crash_on_staged(jpath: Path, record: dict) -> None:
            if record.get("phase") == "staged":
                raise RuntimeError("simulated SIGKILL right after os.rename")
            real_append(jpath, record)

        with mock.patch.object(ag, "_append_wal_record", side_effect=crash_on_staged):
            with self.assertRaises(RuntimeError):
                stager_b.stage(src_b, reason="crash test", category="work_scratch", size_bytes=10)

        self.assertFalse(src_b.exists(), "os.rename already moved source")
        self.assertFalse(stager_b.manifest_path.exists(), "manifest.json was never written due to crash")
        self.assertTrue(stager_b.journal_path.is_file(), "planned WAL entry must already be on disk")

        # `restore --latest --apply` must discover `journal.jsonl`, verify inode match, and restore `src_b`!
        ns_res_b = ag.build_parser().parse_args(["restore", "--latest", "--trash-dir", str(trash_b), "--apply"])
        rep_b = ag.run_restore(ns_res_b)
        self.assertEqual(rep_b["restored_count"], 1)
        self.assertEqual((src_b / "important.txt").read_text(encoding="utf-8"), "recover-me")

        # Case C: Subprocess hard `os._exit(99)` right after `os.rename` (bypassing Python `finally` blocks)
        trash_c = self.root / "TrashSubprocHardExit"
        trash_c.mkdir()
        src_c = self.root / "hard_exit_file.txt"
        src_c.write_text("hard-exit-payload", encoding="utf-8")
        helper_code = (
            "import os, sys; from pathlib import Path; "
            f"sys.path.insert(0, {str(SCRIPT_DIR)!r}); import agent_governor as ag; "
            f"st = ag.TrashStager(trash_root=Path({str(trash_c)!r}), run_label='hardkill'); "
            "orig_rename = os.rename; "
            "os.rename = lambda a, b: (orig_rename(a, b), os._exit(99)); "
            f"st.stage(Path({str(src_c)!r}), 'kill', 'probe', 17)"
        )
        proc = subprocess.run([sys.executable, "-c", helper_code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 99)
        self.assertFalse(src_c.exists())
        ns_res_c = ag.build_parser().parse_args(["restore", "--latest", "--trash-dir", str(trash_c), "--apply"])
        rep_c = ag.run_restore(ns_res_c)
        self.assertEqual(rep_c["restored_count"], 1)
        self.assertEqual(src_c.read_text(encoding="utf-8"), "hard-exit-payload")

    def test_r5_toctou_pin_lease_and_new_activity_between_scan_and_stage_plus_atomic_allocator(self) -> None:
        """R5: `.keep`, extended lease, or new file activity appearing after scan must block staging; allocator is atomic."""
        proj = self._make_git_project("ToctouGuard")
        sc = proj / "work/_scratch"
        c_pin = sc / "cand_pin"
        c_lease = sc / "cand_lease"
        c_newfile = sc / "cand_newfile"
        c_ok = sc / "cand_reapable"
        for d in (c_pin, c_lease, c_newfile, c_ok):
            d.mkdir(parents=True)
            (d / "old.txt").write_text("old", encoding="utf-8")

        future_now = time.time() + 10 * 86400

        def mutate_during_lsof(paths: list[Path]) -> set[Path]:
            (c_pin / ".keep").write_text("pinned late", encoding="utf-8")
            far_exp = (datetime.fromtimestamp(future_now, tz=timezone.utc) + timedelta(days=20)).isoformat()
            (c_lease / ".agent-lease.json").write_text(json.dumps({"expires_at": far_exp}), encoding="utf-8")
            fresh = c_newfile / "fresh.txt"
            fresh.write_text("concurrent write", encoding="utf-8")
            os.utime(fresh, (future_now, future_now))
            return set()

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
                "--skip-simulators",
                "--reference-now",
                str(future_now),
                "--apply",
            ]
        )
        with mock.patch.object(ag, "find_lsof_busy_paths", side_effect=mutate_during_lsof):
            rep = ag.run_gc(ns_gc)

        self.assertTrue(c_pin.is_dir(), "Late .keep must protect candidate")
        self.assertTrue(c_lease.is_dir(), "Late lease renewal must protect candidate")
        self.assertTrue(c_newfile.is_dir(), "Concurrent file write since scan must protect candidate")
        self.assertFalse(c_ok.exists(), "Unmodified expired candidate should still be staged")
        self.assertEqual(rep["errors"], [])

        # Concurrent first-time `allocate_ephemeral_dir` from distinct sessions never collides onto the same path
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "agent_governor.py"),
                    "scratch",
                    "--project",
                    str(proj),
                    "--agent",
                    "codex",
                    "--topic",
                    "race-topic",
                ],
                env={**os.environ, "CLAUDE_SESSION_ID": f"race-sess-{idx}"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for idx in range(4)
        ]
        allocated = [Path(p.communicate(timeout=10)[0].strip()) for p in procs]
        self.assertEqual(len(set(allocated)), 4, f"Expected 4 distinct directories, got: {allocated}")

    def test_restore_retries_partial_batch_and_checks_staged_identity(self):
        stager = ag.TrashStager(trash_root=self.trash)
        first, second = self.root / "first", self.root / "second"
        for path in (first, second):
            path.write_text(path.name)
        staged = stager.stage(first, "test", "work_scratch")
        stager.stage(second, "test", "work_scratch")
        held = self.root / "saved"
        staged.rename(held)
        staged.write_text("replacement")
        args = ag.build_parser().parse_args(["restore", "--latest", "--trash-dir", str(self.trash), "--apply"])
        self.assertEqual(ag.run_restore(args)["restored"], [str(second)])
        self.assertFalse(first.exists())
        self.assertNotIn("restored_at", json.loads(stager.manifest_path.read_text()))
        held.replace(staged)
        self.assertEqual(ag.run_restore(args)["restored"], [str(first)])
        self.assertEqual(json.loads(stager.manifest_path.read_text())["restored_count"], 2)

    def test_r7_restore_dry_run_overrides_apply_in_any_flag_order(self) -> None:
        """R7: Combining `--apply` and `--dry-run` in either order must never move files."""
        staged = self.trash / "staged_item.txt"
        staged.write_text("payload", encoding="utf-8")
        orig = self.root / "restored_item.txt"
        manifest = self.trash / "manifest.json"
        manifest.write_text(
            json.dumps({"items": [{"staged_path": str(staged), "original_path": str(orig)}]}),
            encoding="utf-8",
        )

        for argv in (
            ["restore", str(manifest), "--apply", "--dry-run"],
            ["restore", str(manifest), "--dry-run", "--apply"],
        ):
            args = ag.build_parser().parse_args(argv)
            rep = ag.run_restore(args)
            self.assertEqual(rep["mode"], "dry-run")
            self.assertTrue(staged.is_file(), f"Staged item was moved under {argv}")
            self.assertFalse(orig.exists(), f"Destination was mutated under {argv}")


    def test_gc_dry_run_never_deletes_ephemeral_simulators(self) -> None:
        fake_sim = {
            "udid": "00000000-1111-2222-3333-444444444444",
            "name": "agent-tmp-iphone",
            "state": "Shutdown",
            "size_bytes": 1024,
            "idle_hours": 96.0,
        }
        calls: list[list[str]] = []
        orig_run = ag.subprocess.run

        def fake_run(cmd, *a, **kw):
            if isinstance(cmd, list) and len(cmd) >= 3 and cmd[:3] == ["/usr/bin/xcrun", "simctl", "delete"]:
                calls.append(list(cmd))
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return orig_run(cmd, *a, **kw)

        with mock.patch.object(ag, "scan_ephemeral_simulators", return_value=[fake_sim]), mock.patch.object(
            ag.subprocess, "run", side_effect=fake_run
        ):
            # 1. Default dry-run: zero simctl delete calls
            dry_args = ag.build_parser().parse_args(
                ["gc", "--root", str(self.projects), "--home", str(self.home), "--trash-dir", str(self.trash)]
            )
            dry_rep = ag.run_gc(dry_args)
            self.assertEqual(dry_rep["execution"], "dry-run")
            self.assertEqual(calls, [])

            # 2. --apply + --dry-run conflict: dry-run still wins, zero simctl delete calls
            conflict_args = ag.build_parser().parse_args(
                [
                    "gc",
                    "--root",
                    str(self.projects),
                    "--home",
                    str(self.home),
                    "--trash-dir",
                    str(self.trash),
                    "--apply",
                    "--dry-run",
                ]
            )
            conflict_rep = ag.run_gc(conflict_args)
            self.assertEqual(conflict_rep["execution"], "dry-run")
            self.assertEqual(calls, [])

            # 3. Explicit --apply: invokes simctl delete once and records external action in manifest
            apply_args = ag.build_parser().parse_args(
                [
                    "gc",
                    "--root",
                    str(self.projects),
                    "--home",
                    str(self.home),
                    "--trash-dir",
                    str(self.trash),
                    "--apply",
                ]
            )
            apply_rep = ag.run_gc(apply_args)
            self.assertEqual(apply_rep["execution"], "applied")
            self.assertEqual(len(calls), 1)
            self.assertIsNotNone(apply_rep["manifest"])

            with mock.patch.object(ag, "scan_ephemeral_simulators", side_effect=[[fake_sim], []]):
                ag.run_gc(apply_args)
            self.assertEqual(len(calls), 1, "a device that became ineligible must survive")


    def test_v132_multi_account_parity_broken_mcp_and_memory_bloat(self) -> None:
        rule_text = "# Global Rules\nKeep root clean.\n"
        for rel in (
            ".codex/AGENTS.md",
            ".claude/CLAUDE.md",
            ".gemini/GEMINI.md",
            ".gemini/antigravity/GEMINI.md",
            "Library/Application Support/orca/codex-accounts/acc1/home/AGENTS.md",
            "Library/Application Support/orca/claude-profiles/p1/home/CLAUDE.md",
        ):
            p = self.home / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(rule_text, encoding="utf-8")

        # Drift one profile + add broken MCP + oversized Claude memory
        (self.home / "Library/Application Support/orca/claude-profiles/p1/home/CLAUDE.md").write_text(
            "# Drifted Rule\n", encoding="utf-8"
        )
        cfg = self.home / ".codex/config.toml"
        cfg.write_text(
            '[mcp_servers.dead_local]\ncommand = "/nonexistent/dead-mcp-bin"\nenabled = true\n',
            encoding="utf-8",
        )
        mem = self.home / ".claude/projects/demo-proj/memory/huge.md"
        mem.parent.mkdir(parents=True, exist_ok=True)
        mem.write_text("x" * 16000, encoding="utf-8")

        args = ag.build_parser().parse_args(["audit", "--root", str(self.projects), "--home", str(self.home)])
        rep = ag.run_audit(args)
        attn = "\n".join(rep["summary"]["attention"])
        adv = "\n".join(rep["summary"]["advice"])
        self.assertIn("drifted out of sync", attn)
        self.assertIn("dead_local (/nonexistent/dead-mcp-bin)", attn)
        self.assertIn("Oversized Claude memory file(s) >15KB", adv)

    def test_v140_xcode_deriveddata_runner_sweep_and_shared_cache_protection(self) -> None:
        proj = self._make_git_project("Gamma", commit_initial=True)
        ai_root = self.projects.parent
        data_dir = ai_root / "Data/CameraLab"
        sys_dd = self.home / "Library/Developer/Xcode/DerivedData"

        # 1. System DerivedData: stale project (>16h), recent project (3h: keep DD, sweep runner!), shared ModuleCache, pinned
        stale_dd = sys_dd / "OldProj-abcdef"
        (stale_dd / "Build/Intermediates.noindex").mkdir(parents=True)
        (stale_dd / "Build/Intermediates.noindex/a.o").write_bytes(b"OBJ")

        mid_dd = sys_dd / "MidProj-123456"
        mid_runner = mid_dd / "Build/Products/Debug-iphonesimulator/MidProjUITests-Runner.app"
        mid_app = mid_dd / "Build/Products/Debug-iphonesimulator/MidProj.app"
        mid_runner.mkdir(parents=True)
        (mid_runner / "MidProjUITests-Runner").write_bytes(b"RUNNER")
        mid_app.mkdir(parents=True)
        (mid_app / "MidProj").write_bytes(b"APP")

        mod_cache = sys_dd / "ModuleCache.noindex"
        mod_cache.mkdir(parents=True)
        (mod_cache / "swift.pcm").write_bytes(b"PCM")

        pinned_dd = sys_dd / "PinnedProj-999999"
        pinned_dd.mkdir(parents=True)
        (pinned_dd / ".keep").write_text("pin\n", encoding="utf-8")
        (pinned_dd / "artifact.bin").write_bytes(b"PINNED")

        # 2. AI/Data: custom DerivedDataUI (>16h) + retained run backup with runner-app vs target-app
        dd_ui = data_dir / "layout-20261114/DerivedDataUI/Build/Products"
        dd_ui.mkdir(parents=True)
        (dd_ui / "blob.bin").write_bytes(b"UI")

        run_r4 = data_dir / "stage6/runs/preservation-r4/before/backup"
        r4_runner = run_r4 / "runner-app/GammaUITests-Runner.app"
        r4_target = run_r4 / "target-app/Gamma.app"
        r4_runner.mkdir(parents=True)
        (r4_runner / "GammaUITests-Runner").write_bytes(b"R4_RUNNER")
        r4_target.mkdir(parents=True)
        (r4_target / "Gamma").write_bytes(b"R4_TARGET")
        (data_dir / "stage6/runs/preservation-r4/manifest.json").write_text('{"ok": true}\n', encoding="utf-8")

        # 3. work/_tests/<2d-old-run>: outer _tests (7d TTL) must stay, inner DerivedData (>16h) swept!
        test_run = proj / "work/_tests/20261114-codex-ui-audit"
        inner_dd = test_run / "DerivedData/Build"
        inner_xcres = test_run / "Smoke.xcresult"
        inner_dd.mkdir(parents=True)
        (inner_dd / "cache.db").write_bytes(b"DB")
        inner_xcres.mkdir(parents=True)
        (inner_xcres / "Info.plist").write_text("<plist/>", encoding="utf-8")

        # Advance reference clock by +18h for stale items, and touch mid_dd +21h - 3h => 3h old relative to ref_now!
        base_now = time.time()
        ref_now = base_now + 18 * 3600
        mid_ts = ref_now - 3 * 3600
        for p in (mid_dd, mid_dd / "Build", mid_dd / "Build/Products", mid_app.parent, mid_runner, mid_runner / "MidProjUITests-Runner", mid_app, mid_app / "MidProj"):
            os.utime(p, (mid_ts, mid_ts))
        # Note inspect_tree uses max(mtime, ctime); since base_now < mid_ts (mid_ts = base_now + 15h!),
        # max(mid_ts, ctime=base_now) == mid_ts => idle = 3.0h (< 12h DD TTL, >= 2h runner min_idle_hours)!

        gc_args = ag.build_parser().parse_args(
            [
                "gc",
                "--root",
                str(self.projects),
                "--home",
                str(self.home),
                "--trash-dir",
                str(self.trash),
                "--skip-simulators",
                "--skip-launchservices",
                "--reference-now",
                str(ref_now),
                "--apply",
            ]
        )
        rep = ag.run_gc(gc_args)
        self.assertEqual(rep["execution"], "applied")
        cats = rep["categories"]
        # Custom -derivedDataPath trees use the 12h TTL; ~/Library/Developer/Xcode/DerivedData keeps 7 days (daily)
        self.assertEqual(cats["stale_derived_data"]["count"], 2, rep)
        self.assertEqual(cats["xcode_test_runner"]["count"], 2, rep)
        self.assertTrue(stale_dd.exists(), "system DerivedData idle 18h must survive the 7-day daily TTL")

        # Verify swept items
        self.assertFalse((data_dir / "layout-20261114/DerivedDataUI").exists())
        self.assertFalse((test_run / "DerivedData").exists())
        self.assertFalse(mid_runner.exists())
        self.assertFalse(r4_runner.exists())

        # Verify protected items
        self.assertTrue(mid_dd.exists())
        self.assertTrue(mid_app.exists())
        self.assertTrue(mod_cache.exists())
        self.assertTrue(pinned_dd.exists())
        self.assertTrue(r4_target.exists())
        self.assertTrue(inner_xcres.exists())
        self.assertTrue((sys_dd / ".metadata_never_index").is_file())
        self.assertTrue((ai_root / "Data/.metadata_never_index").is_file())

        # After 8 idle days the system DerivedData project folder is reclaimed; pinned and shared caches stay
        week_args = ag.build_parser().parse_args(
            [
                "gc", "--root", str(self.projects), "--home", str(self.home), "--trash-dir", str(self.trash),
                "--skip-simulators", "--skip-launchservices", "--reference-now", str(base_now + 8 * 86400), "--apply",
            ]
        )
        ag.run_gc(week_args)
        self.assertFalse(stale_dd.exists())
        self.assertTrue(pinned_dd.exists())
        self.assertTrue(mod_cache.exists())

    def test_v140_launchservices_ghost_unregistration_and_spotlight_shields(self) -> None:
        self._make_git_project("Delta", commit_initial=True)
        ai_root = self.projects.parent
        (ai_root / "Data").mkdir(parents=True, exist_ok=True)
        sys_dd = self.home / "Library/Developer/Xcode/DerivedData"
        sys_cs = self.home / "Library/Developer/CoreSimulator"
        sys_dd.mkdir(parents=True, exist_ok=True)
        sys_cs.mkdir(parents=True, exist_ok=True)

        runner_on_disk = ai_root / "Data/DerivedDataUI/Build/Products/Debug-iphonesimulator/DeltaUITests-Runner.app"
        legit_helper = self.home / "Applications/Claude Code URL Handler.app"
        runner_on_disk.mkdir(parents=True)
        legit_helper.mkdir(parents=True)

        unreg_log = self.home / "lsreg-unreg.log"
        stub_ls = self.home / "stub-lsregister"
        stub_ls.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-dump" ]; then\n'
            f'  echo "  path:          {runner_on_disk} (0x20)"\n'
            '  echo "path:          /NonExistent/Sparkle/Launcher/Updater.app (0x21)"\n'
            f'  echo "path:          {legit_helper} (0x22)"\n'
            '  echo "path:          /System/Library/CoreServices/Finder.app (0x23)"\n'
            'elif [ "$1" = "-u" ]; then\n'
            f'  echo "$2" >> "{unreg_log}"\n'
            "fi\n",
            encoding="utf-8",
        )
        stub_ls.chmod(0o755)

        with mock.patch.dict(os.environ, {"AGENT_GOVERNOR_LSREGISTER": str(stub_ls)}):
            dry_ns = ag.build_parser().parse_args(
                ["gc", "--root", str(self.projects), "--home", str(self.home), "--trash-dir", str(self.trash), "--skip-simulators"]
            )
            dry_rep = ag.run_gc(dry_ns)
            self.assertEqual(len(dry_rep["launchservices_ghosts"]), 2, dry_rep)
            self.assertFalse(unreg_log.exists(), "dry-run must never invoke lsregister -u")

            apply_ns = ag.build_parser().parse_args(
                [
                    "gc",
                    "--root",
                    str(self.projects),
                    "--home",
                    str(self.home),
                    "--trash-dir",
                    str(self.trash),
                    "--skip-simulators",
                    "--apply",
                ]
            )
            apply_rep = ag.run_gc(apply_ns)
            self.assertEqual(apply_rep["categories"]["launchservices_ghost"]["count"], 2)
            self.assertIsNotNone(apply_rep["manifest"])
            logged = unreg_log.read_text(encoding="utf-8").splitlines()
            self.assertIn(str(runner_on_disk), logged)
            self.assertIn("/NonExistent/Sparkle/Launcher/Updater.app", logged)
            self.assertNotIn(str(legit_helper), logged)
            self.assertTrue((sys_dd / ".metadata_never_index").is_file())
            self.assertTrue((sys_cs / ".metadata_never_index").is_file())
            self.assertTrue((ai_root / "Data/.metadata_never_index").is_file())

        # Regression: overriding $HOME to a throwaway directory (where Path.home() == self.home)
        # must still stay 100% hermetic and never query the host UID's LaunchServices daemon!
        env_clean = {k: v for k, v in os.environ.items() if k != "AGENT_GOVERNOR_LSREGISTER"}
        env_clean["HOME"] = str(self.home)
        with mock.patch.dict(os.environ, env_clean, clear=True):
            with mock.patch.object(ag.subprocess, "run", side_effect=AssertionError("must not invoke host lsregister")):
                self.assertEqual(ag.scan_launchservices_ghost_apps(home=self.home, proc_cmds=[]), [])


    def test_v141_lsregister_skips_volume_containers_and_audit_resolves_global_pointer(self) -> None:
        self._make_git_project("Epsilon", commit_initial=True)

        # 1. Global rule `@~/.claude/CLAUDE.md` pointer in an Orca Claude profile must match canonical SHA-256
        prof_home = self.home / "Library/Application Support/orca/claude-profiles/prof-ptr/home"
        prof_home.mkdir(parents=True, exist_ok=True)
        (prof_home / "CLAUDE.md").write_text("@~/.claude/CLAUDE.md\n", encoding="utf-8")
        audit_ns = ag.build_parser().parse_args(["audit", "--root", str(self.projects), "--home", str(self.home)])
        audit_rep = ag.run_audit(audit_ns)
        self.assertTrue(audit_rep["global_rules"]["in_sync"], audit_rep["global_rules"])
        self.assertFalse(any("Global rule files" in a for a in audit_rep["summary"]["attention"]), audit_rep["summary"])
        ptr_entry = audit_rep["global_rules"]["files"]["orca_claude_p1"]
        self.assertEqual(ptr_entry["pointer_to"], str((self.home / ".claude/CLAUDE.md").resolve()))

        # 2. LaunchServices dump containing unmounted DMG container volumes & cryptex mounts must produce 0 ghosts
        #    and must NEVER create empty Trash batch directories or invoke `lsregister -gc`.
        unreg_log = self.home / "lsreg-v141.log"
        stub_ls = self.home / "stub-lsregister-v141"
        stub_ls.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-dump" ]; then\n'
            '  echo "--------------------------------------------------------------------------------"\n'
            '  echo "container id:               /Volumes/Google Chrome (0xc)"\n'
            '  echo "path:                       /Volumes/Google Chrome (0x1494)"\n'
            '  echo "state:                      not mounted"\n'
            '  echo "--------------------------------------------------------------------------------"\n'
            '  echo "container id:               /Volumes/Antigravity (0xd)"\n'
            '  echo "path:                       /Volumes/Antigravity (0x1500)"\n'
            '  echo "--------------------------------------------------------------------------------"\n'
            '  echo "path:                       /private/var/run/com.apple.security.cryptexd/mnt/com.apple.iPhoneOS.SimulatorRuntime-v24.2 (0x1600)"\n'
            'else\n'
            f'  echo "$@" >> "{unreg_log}"\n'
            "fi\n",
            encoding="utf-8",
        )
        stub_ls.chmod(0o755)

        with mock.patch.dict(os.environ, {"AGENT_GOVERNOR_LSREGISTER": str(stub_ls)}):
            apply_ns = ag.build_parser().parse_args(
                [
                    "gc",
                    "--root",
                    str(self.projects),
                    "--home",
                    str(self.home),
                    "--trash-dir",
                    str(self.trash),
                    "--skip-simulators",
                    "--apply",
                ]
            )
            rep = ag.run_gc(apply_ns)
            self.assertEqual(rep["launchservices_ghosts"], [], rep)
            self.assertIsNone(rep["manifest"], "No empty Trash manifest should be written on 0 candidates")
            self.assertFalse(unreg_log.exists(), "Must not invoke lsregister -u/-gc when 0 bundle ghosts exist")
            self.assertEqual(list(self.trash.iterdir()), [], "Must not create empty Trash batch dirs")

    # ------------------------------------------------------------------------------------------------
    # Regression tests for the v1.5 audit fixes
    # ------------------------------------------------------------------------------------------------
    def _gc_args(self, *extra: str, ref_days: float = 0.0) -> argparse.Namespace:
        return ag.build_parser().parse_args(
            [
                "gc", "--root", str(self.projects), "--home", str(self.home), "--trash-dir", str(self.trash),
                "--skip-simulators", "--skip-launchservices", "--reference-now", str(time.time() + ref_days * 86400),
                *extra,
            ]
        )

    def _live_owner(self) -> subprocess.Popen:
        proc = subprocess.Popen(["/bin/sleep", "120"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        return proc

    def test_nested_xcode_sweep_respects_enclosing_lease_owner_and_pin(self) -> None:
        proj = self._make_git_project("NestedLease")
        owner = self._live_owner()
        now = datetime.now(timezone.utc)
        cases = {
            "live-owner": {"owners": [[owner.pid, ag.process_lstart(owner.pid)]], "expires_at": now.isoformat()},
            "active-ttl": {"owners": [[999995, ""]], "expires_at": (now + timedelta(days=60)).isoformat()},
            "pinned": None,
            "unprotected": {"owners": [[999996, ""]], "expires_at": now.isoformat()},
        }
        entries = {}
        for name, lease in cases.items():
            entry = proj / f"work/_tests/20261101-claude-{name}"
            (entry / "DerivedData/Logs/Test/Run.xcresult").mkdir(parents=True)
            (entry / "DerivedData/Logs/Test/Run.xcresult/Info.plist").write_text("results", encoding="utf-8")
            (entry / "Build/Products/Debug-iphonesimulator/AppUITests-Runner.app").mkdir(parents=True)
            if lease is None:
                (entry / ".keep").write_text("", encoding="utf-8")
            else:
                (entry / ".agent-lease.json").write_text(json.dumps(lease), encoding="utf-8")
            entries[name] = entry

        rep = ag.run_gc(self._gc_args("--tests-ttl-days", "30", "--apply", ref_days=2))
        for name in ("live-owner", "active-ttl", "pinned"):
            self.assertTrue((entries[name] / "DerivedData/Logs/Test/Run.xcresult/Info.plist").is_file(), name)
            self.assertTrue((entries[name] / "Build/Products/Debug-iphonesimulator/AppUITests-Runner.app").is_dir(), name)
        self.assertFalse((entries["unprotected"] / "DerivedData").exists(), "unleased nested DerivedData is still swept")
        gates = {s["gate"] for s in rep["protected_skips"]}
        self.assertTrue({"Gate01_EnclosingActiveProcessLock", "Gate02_EnclosingActiveLeaseTTL"} <= gates, gates)

    def test_swiftpm_checkout_git_dirs_do_not_block_reclaim_but_real_repos_do(self) -> None:
        proj = self._make_git_project("SpmCheckouts")
        spm = proj / "work/_tests/20261001-claude-uitest"
        git_dir = spm / "DerivedData/SourcePackages/checkouts/swift-collections/.git"
        git_dir.mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        build_git = proj / "work/_scratch/20261001-claude-pkg/.build/checkouts/dep/.git"
        build_git.mkdir(parents=True)
        real_repo = proj / "work/_scratch/20261001-claude-clone"
        (real_repo / ".git").mkdir(parents=True)
        (real_repo / "main.swift").write_text("print(1)\n", encoding="utf-8")

        self.assertFalse(ag.inspect_tree(spm).has_nested_git)
        self.assertTrue(ag.inspect_tree(real_repo).has_nested_git)
        ag.run_gc(self._gc_args("--apply", ref_days=30))
        self.assertFalse(spm.exists(), "SwiftPM checkout .git must not pin an expired test dir forever")
        self.assertFalse((proj / "work/_scratch/20261001-claude-pkg").exists())
        self.assertTrue(real_repo.is_dir(), "a real nested repository is still protected")

    def test_lease_owner_pid_reuse_and_session_end_exiting_chain(self) -> None:
        owner = self._live_owner()
        real = ag.process_lstart(owner.pid)
        self.assertTrue(real)
        self.assertTrue(ag.lease_has_live_owner({"owners": [[owner.pid, real]]}))
        self.assertFalse(ag.lease_has_live_owner({"owners": [[owner.pid, "Thu Jan  1 00:00:00 1970"]]}), "reused PID")
        self.assertTrue(ag.lease_has_live_owner({"pid": owner.pid}), "legacy lease: PID-only liveness")
        live, _ = ag.snapshot_running_processes()
        self.assertEqual(live[owner.pid], real)
        self.assertFalse(ag.lease_has_live_owner({"owners": [[owner.pid, real]]}, live, ignore_pids={owner.pid}))

        proj = self._make_git_project("SessionEnd")
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sess-exit"}):
            mine = self._allocate("scratch", ["scratch", "--project", str(proj), "--topic", "mine"], [owner.pid])
        other_owner = self._live_owner()
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sess-other"}):
            other = self._allocate("scratch", ["scratch", "--project", str(proj), "--topic", "other"], [other_owner.pid])

        se_args = ag.build_parser().parse_args(
            ["gc", "--mode", "session-end", "--root", str(self.projects), "--home", str(self.home),
             "--trash-dir", str(self.trash), "--reference-now", str(time.time() + 3 * 3600), "--apply"]
        )
        # The SessionEnd hook runs inside the exiting agent: that chain is alive but must not count as an owner
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sess-exit"}), mock.patch.object(
            ag, "collect_ancestor_pids", return_value=[owner.pid]
        ):
            ag.run_gc(se_args)
        self.assertFalse(mine.exists(), "exiting session's own empty dir is reaped although its process is alive")
        self.assertTrue(other.is_dir(), "another live session's dir is kept")

    def test_launchservices_never_unregisters_unreadable_or_existing_real_apps(self) -> None:
        locked = self.root / "locked"
        hidden_app = locked / "RealTool.app"
        hidden_app.mkdir(parents=True)
        worktree_app = self.ai_root / "Orca/Worktrees/feature/dist/Tool.app"
        worktree_app.mkdir(parents=True)
        runner = self.ai_root / "Orca/Worktrees/feature/Build/Products/AppUITests-Runner.app"
        runner.mkdir(parents=True)
        gone = self.root / "Deleted/Old.app"
        stub = self.home / "stub-lsregister-v15"
        stub.write_text(
            "#!/bin/sh\nif [ \"$1\" = -dump ]; then\n"
            + "".join(f"  echo 'path:  {p} (0x{i})'\n" for i, p in enumerate((hidden_app, worktree_app, runner, gone), 1))
            + "fi\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        self._lock_mode(locked, 0o000)
        with mock.patch.dict(os.environ, {"AGENT_GOVERNOR_LSREGISTER": str(stub)}):
            found = ag.scan_launchservices_ghost_apps(home=self.home, proc_cmds=[])
        self.assertEqual(found, sorted([str(runner), str(gone)]))

    def test_env_values_drop_inline_comments(self) -> None:
        cfg = self.home / ".orca/backup-config.env"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(
            "PRIMARY_SIMULATOR_UDID=AAAA-1111 # main device\n"
            'export SECONDARY_SIMULATOR_UDID="BBBB-2222"  # second\n'
            "STORAGE_MODE='internal'\n"
            "INTERNAL_WORKSPACE_PATH=$HOME/WS#1\n",
            encoding="utf-8",
        )
        env = ag.load_orca_env(self.home)
        self.assertEqual(env["PRIMARY_SIMULATOR_UDID"], "AAAA-1111")
        self.assertEqual(env["SECONDARY_SIMULATOR_UDID"], "BBBB-2222")
        self.assertEqual(env["STORAGE_MODE"], "internal")
        self.assertEqual(env["INTERNAL_WORKSPACE_PATH"], f"{self.home}/WS#1")

    def test_restore_apply_exits_nonzero_when_an_item_cannot_be_restored(self) -> None:
        batch = self.trash / "agent-governor-2026-11-24/gc-x"
        (batch / "items").mkdir(parents=True)
        staged = batch / "items/0001_a"
        staged.write_text("payload", encoding="utf-8")
        ro_parent = self.root / "proj/ro"
        ro_parent.mkdir(parents=True)
        st = staged.lstat()
        (batch / "manifest.json").write_text(
            json.dumps({"items": [{"seq": 1, "original_path": str(ro_parent / "a"), "staged_path": str(staged),
                                   "src_ino": st.st_ino, "src_dev": st.st_dev}]}),
            encoding="utf-8",
        )
        self._lock_mode(ro_parent, 0o555)
        with mock.patch("sys.stdout"):
            self.assertEqual(ag.main(["restore", str(batch), "--dry-run"]), 0)
            self.assertEqual(ag.main(["restore", str(batch), "--apply"]), 1)
        self.assertTrue(staged.is_file())

    def test_scratch_refuses_outside_a_project_and_accepts_projects_root_members(self) -> None:
        outside = self.root / "Downloads/sub"
        outside.mkdir(parents=True)
        with mock.patch("sys.stderr") as err:
            self.assertEqual(ag.main(["scratch", "--project", str(outside), "--topic", "probe"]), 2)
        self.assertIn("not inside a project", "".join(c.args[0] for c in err.write.call_args_list))
        self.assertFalse((outside / "work").exists())

        plain = self.projects / "PlainFolder/src"
        plain.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"AI_VOLUME_PATH": str(self.root)}):
            self.assertEqual(ag.infer_project_dir(str(plain)), (self.projects / "PlainFolder").resolve())
        repo = self._make_git_project("Repo")
        (repo / "app/work").mkdir(parents=True)  # a source folder named `work` is not a project root
        self.assertEqual(ag.infer_project_dir(str(repo / "app")), repo.resolve())

    def test_duplicate_global_rule_phrases_cover_both_rule_templates(self) -> None:
        for tmpl in ("AGENTS.md.tmpl", "AGENTS.zh-CN.md.tmpl"):
            proj = self._make_git_project(f"Dup-{tmpl.split('.')[1]}")
            text = (SCRIPT_DIR.parent / "templates" / tmpl).read_text(encoding="utf-8")
            (proj / "AGENTS.md").write_text(re.sub(r"\$\{[A-Z_]+\}", "", text), encoding="utf-8")
            smells = ag.audit_project_rules(proj)["smells"]
            dup = [s for s in smells if s.startswith("duplicates_global_rules")]
            self.assertTrue(dup, tmpl)
            self.assertGreaterEqual(dup[0].count(","), 3, dup)

    def test_open_file_snapshot_prefix_matching_and_failure(self) -> None:
        a = self.root / "a"
        (a / "b").mkdir(parents=True)
        (self.root / "ab").mkdir()
        snap = ag.OpenFileSnapshot([str(a / "b/c.txt"), str(self.root / "abc")])
        self.assertTrue(snap.is_busy(a))
        self.assertTrue(snap.is_busy(a / "b"))
        self.assertFalse(snap.is_busy(self.root / "ab"), "sibling sharing a name prefix is not busy")
        self.assertFalse(snap.is_busy(self.root / "missing"))
        self.assertTrue(ag.OpenFileSnapshot(None).is_busy(a), "failed lsof is fail-closed")

    def test_git_tracked_index_directory_lookup(self) -> None:
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        idx = ag.GitTrackedIndex()
        idx._cache[str(repo)] = {"a/b/c.txt", "top.md"}
        self.assertTrue(idx.has_tracked(repo / "a"))
        self.assertTrue(idx.has_tracked(repo / "a/b"))
        self.assertTrue(idx.has_tracked(repo / "a/b/c.txt"))
        self.assertTrue(idx.has_tracked(repo / "top.md"))
        self.assertFalse(idx.has_tracked(repo / "ab"))
        self.assertFalse(idx.has_tracked(repo / "a/b/d"))

    def test_mcp_server_count_ignores_subtables_with_and_without_tomllib(self) -> None:
        raw = '[mcp_servers.alpha]\ncommand = "/bin/a"\n[mcp_servers.alpha.env]\nX = "1"\n[mcp_servers."beta.io"]\ncommand = "/bin/b"\n'
        expected = {"alpha", "beta.io"}
        self.assertEqual(set(ag.parse_codex_mcp_servers(raw)), expected)
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            fallback = ag.parse_codex_mcp_servers(raw)
        self.assertEqual(set(fallback), expected)
        self.assertEqual(fallback["alpha"]["command"], "/bin/a")

    def test_gc_reports_share_one_schema_and_apply_walks_each_tree_twice(self) -> None:
        proj = self._make_git_project("Schema")
        cand = proj / "work/_scratch/20261001-claude-old"
        cand.mkdir(parents=True)
        (cand / "f.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(ag, "snapshot_running_processes", return_value=({}, [])):
            aborted = ag.run_gc(self._gc_args("--apply", ref_days=10))
        calls = []
        real_inspect = ag.inspect_tree
        with mock.patch.object(ag, "inspect_tree", side_effect=lambda p: calls.append(p) or real_inspect(p)):
            applied = ag.run_gc(self._gc_args("--workspace-only", "--apply", ref_days=10))
        self.assertEqual(set(aborted), set(applied))
        self.assertEqual(applied["categories"]["work_scratch"]["count"], 1)
        self.assertEqual(calls.count(cand), 2, "scan + pre-stage verification; staging reuses the verified walk")

    def test_optional_project_agents_md_untracked_info_and_redundant_gemini_pointer(self) -> None:
        # Project without AGENTS.md inherits global rules cleanly (no error)
        proj = self._make_git_project("GlobalOnly", commit_initial=False)
        (proj / "AGENTS.md").unlink()
        (proj / "README.md").write_text("# GlobalOnly\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(proj), "add", "README.md"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(proj), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
            check=True,
            capture_output=True,
        )
        for z in ag.FOUR_ZONES:
            (proj / z).mkdir(exist_ok=True)
        ag.ensure_git_info_exclude(proj, "/work/", apply=True)
        rep = ag.audit_project_rules(proj)
        self.assertEqual(rep["issues"], [])
        self.assertTrue(any("inherits_global_rules_only" in x for x in rep["infos"]))

        # Dangling CLAUDE.md pointer without AGENTS.md is flagged as an issue
        (proj / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        rep_dangling = ag.audit_project_rules(proj)
        self.assertTrue(any("dangling_CLAUDE.md_pointer" in x for x in rep_dangling["issues"]))

        # Untracked AGENTS.md emits info; redundant GEMINI.md pointer emits smell (not issue)
        (proj / "AGENTS.md").write_text("# GlobalOnly\n- Test: `make test`\n", encoding="utf-8")
        (proj / "GEMINI.md").write_text("@AGENTS.md\n", encoding="utf-8")
        rep_untracked = ag.audit_project_rules(proj)
        self.assertEqual(rep_untracked["issues"], [])
        self.assertIs(rep_untracked["agents_md_tracked"], False)
        self.assertTrue(any("untracked_AGENTS.md" in x for x in rep_untracked["infos"]))
        self.assertTrue(any("redundant_GEMINI.md_pointer" in x for x in rep_untracked["smells"]))


if __name__ == "__main__":
    unittest.main()
