#!/usr/bin/env python3
"""Comprehensive unit tests for agent_governor.py (v1.1.0) covering audit, adopt, leases, 5-gate GC, triage & restore."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
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

        for rel in (".codex/AGENTS.md", ".claude/CLAUDE.md", ".gemini/GEMINI.md"):
            p = self.home / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# Global Rules\n- Keep rules concise.\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_git_project(self, name: str, commit_initial: bool = False) -> Path:
        proj = self.projects / name
        proj.mkdir(parents=True)
        subprocess.run(["git", "-C", str(proj), "init", "-q"], check=True)
        (proj / "AGENTS.md").write_text(f"# {name}\n- Run `pytest` before shipping.\n", encoding="utf-8")
        if commit_initial:
            subprocess.run(["git", "-C", str(proj), "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-C", str(proj), "config", "user.name", "Tester"], check=True)
            subprocess.run(["git", "-C", str(proj), "add", "AGENTS.md"], check=True)
            subprocess.run(["git", "-C", str(proj), "commit", "-q", "-m", "init"], check=True)
        return proj

    def test_audit_detects_missing_pointers_zones_and_clutter_then_adopt_heals_cleanly(self) -> None:
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

        # Verify `git status --porcelain` stays 100% clean (pointers & work/ excluded locally)
        st = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True, check=True)
        self.assertEqual(st.stdout.strip(), "")

        rep_after = ag.run_audit(ns_audit)
        self.assertTrue(rep_after["summary"]["healthy"], msg=str(rep_after["summary"]))

    def test_scratch_and_test_dir_allocate_and_disambiguate_parallel_leases(self) -> None:
        proj = self._make_git_project("Beta", commit_initial=True)
        ns_s1 = ag.build_parser().parse_args(
            ["scratch", "--project", str(proj), "--agent", "codex", "--topic", "API Probe", "--ttl-hours", "48"]
        )
        sdir1 = ag.allocate_ephemeral_dir("scratch", ns_s1)
        self.assertTrue(sdir1.is_dir())
        self.assertEqual(sdir1.parent.name, "_scratch")
        lease1 = json.loads((sdir1 / ".agent-lease.json").read_text(encoding="utf-8"))
        self.assertEqual(lease1["agent"], "codex")
        self.assertEqual(lease1["ttl_hours"], 48.0)

        # Simulate a second live agent session on the same day with a different live PID (init/launchd PID 1 or current pid)
        other_live_pid = os.getpid() if lease1["pid"] != os.getpid() else os.getppid()
        ns_s2 = ag.build_parser().parse_args(
            ["scratch", "--project", str(proj), "--agent", "codex", "--topic", "API Probe"]
        )
        setattr(ns_s2, "pid", other_live_pid)
        with mock.patch.object(ag, "pid_alive", return_value=True):
            sdir2 = ag.allocate_ephemeral_dir("scratch", ns_s2)
        self.assertNotEqual(sdir1, sdir2)
        # First agent's lease was NOT clobbered
        self.assertEqual(json.loads((sdir1 / ".agent-lease.json").read_text(encoding="utf-8"))["pid"], lease1["pid"])

        # Allocating scratch also shielded `/work/` in `.git/info/exclude`
        st = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True, check=True)
        self.assertEqual(st.stdout.strip(), "")

    def test_nested_worktrees_depth_1_2_3_discovered_for_gc_audit_and_triage(self) -> None:
        self._make_git_project("Core")
        wt_d1 = self.ai_root / "Claude/Worktrees/flat-wt"
        wt_d2 = self.ai_root / "Orca/Worktrees/Game/scylla"
        wt_d3 = self.ai_root / "Orca/Worktrees/Phayen/NEGA/flatback"
        old_ts = time.time() - (6 * 86400)

        for wt in (wt_d1, wt_d2, wt_d3):
            wt.mkdir(parents=True)
            (wt / ".git").write_text("gitdir: /tmp/fake-gitdir\n", encoding="utf-8")
            s = wt / "work/_scratch/20261101-agent-stale"
            s.mkdir(parents=True)
            f = s / "trace.log"
            f.write_text("log", encoding="utf-8")
            os.utime(f, (old_ts, old_ts))
            os.utime(s, (old_ts, old_ts))

        # Add root clutter to depth-3 worktree and stranded legacy work/ data to depth-2 worktree
        (wt_d3 / "tmp").mkdir()
        (wt_d2 / "work/retained/dataset").mkdir(parents=True)
        (wt_d2 / "work/retained/dataset/weights.bin").write_bytes(b"123456")

        discovered = ag.discover_worktree_checkouts(self.ai_root)
        self.assertEqual(set(discovered), {wt_d1, wt_d2, wt_d3})

        # Audit catches root clutter inside depth-3 worktree
        ns_audit = ag.build_parser().parse_args(["audit", "--root", str(self.projects), "--home", str(self.home)])
        audit_rep = ag.run_audit(ns_audit)
        self.assertEqual(len(audit_rep["worktrees_clutter"]), 1)
        self.assertIn("Orca/Worktrees/Phayen/NEGA/flatback", audit_rep["worktrees_clutter"][0]["worktree"])

        # Triage reports stranded work/ data inside worktrees (`--output` flag alias)
        md_out = self.root / "wt-triage.md"
        ns_tr = ag.build_parser().parse_args(
            ["triage", "--root", str(self.projects), "--home", str(self.home), "--output", str(md_out)]
        )
        tr_rep = ag.run_triage(ns_tr)
        self.assertTrue(any("Game/scylla" in w["worktree"] for w in tr_rep["worktree_stranded_work"]))
        self.assertTrue(md_out.is_file())

        # GC sweeps expired _scratch across all 3 nested worktree depths while preserving work/retained
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

        old_ts = time.time() - (10 * 86400)
        mid_ts = time.time() - (36 * 3600)  # 1.5 days old: kept by daily (3d), swept by deep (1d)

        # 1. Expired _scratch (>3d) -> reclaimed in daily
        exp_scratch = proj / "work/_scratch/20261101-codex-old"
        exp_scratch.mkdir()
        f1 = exp_scratch / "dump.json"
        f1.write_text("{}", encoding="utf-8")
        os.utime(f1, (old_ts, old_ts))
        os.utime(exp_scratch, (old_ts, old_ts))

        # 2. `.pinned` marker -> protected by Gate 02
        pinned = proj / "work/_scratch/20261101-codex-pinned"
        pinned.mkdir()
        (pinned / ".pinned").write_text("", encoding="utf-8")
        os.utime(pinned / ".pinned", (old_ts, old_ts))
        os.utime(pinned, (old_ts, old_ts))

        # 3. Open file descriptor (`lsof`) -> protected by Gate 01
        lsof_busy = proj / "work/_scratch/20261101-codex-openfd"
        lsof_busy.mkdir()
        f_busy = lsof_busy / "live.log"
        f_busy.write_text("streaming", encoding="utf-8")
        os.utime(f_busy, (old_ts, old_ts))
        os.utime(lsof_busy, (old_ts, old_ts))

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
        lf = legacy / "rootfs.cpio"
        lf.write_text("precious", encoding="utf-8")
        os.utime(lf, (old_ts, old_ts))
        os.utime(legacy, (old_ts, old_ts))

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

        # Daily GC with mocked lsof reporting `lsof_busy`
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
                "--apply",
            ]
        )
        with mock.patch.object(ag, "find_lsof_busy_paths", return_value={lsof_busy}):
            daily_rep = ag.run_gc(ns_daily)

        self.assertFalse(exp_scratch.exists())
        self.assertTrue(pinned.exists())
        self.assertTrue(lsof_busy.exists())
        self.assertTrue(mid_scratch.exists())
        self.assertTrue(legacy.exists())
        self.assertTrue(any(s["gate"] == "Gate01_OpenFileDescriptor" for s in daily_rep["protected_skips"]))

        # Deep GC sweeps `mid_scratch` (36h >= 24h deep TTL)
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
                "--apply",
            ]
        )
        with mock.patch.object(ag, "find_lsof_busy_paths", return_value={lsof_busy}):
            deep_rep = ag.run_gc(ns_deep)
        self.assertFalse(mid_scratch.exists())
        self.assertIsNotNone(deep_rep["manifest"])

        # Restore `--latest --dry-run` previews without moving
        ns_dry = ag.build_parser().parse_args(["restore", "--latest", "--trash-dir", str(self.trash), "--dry-run"])
        dry_res = ag.run_restore(ns_dry)
        self.assertEqual(dry_res["mode"], "dry-run")
        self.assertEqual(dry_res["restored_count"], 1)
        self.assertFalse(mid_scratch.exists())

        # Restore `--latest --apply` restores `mid_scratch` and marks manifest `restored_at`
        ns_apply1 = ag.build_parser().parse_args(["restore", "--latest", "--trash-dir", str(self.trash), "--apply"])
        res1 = ag.run_restore(ns_apply1)
        self.assertEqual(res1["restored_count"], 1)
        self.assertTrue(mid_scratch.exists())

        # Second `restore --latest --apply` skips already-restored deep manifest and restores `exp_scratch`!
        res2 = ag.run_restore(ns_apply1)
        self.assertEqual(res2["restored_count"], 1)
        self.assertTrue(exp_scratch.exists())

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

        # Put fake links & ${VAR} inside fenced/inline code blocks -> should NOT trigger false positives!
        (proj / "AGENTS.md").write_text(
            "# Epsilon\n"
            "- Example inline `[example](does-not-exist.md)` and `${EXAMPLE_ENV}`.\n"
            "```bash\n"
            "echo ${UNSET_VAR} # [code-link](missing-in-code.md)\n"
            "```\n",
            encoding="utf-8",
        )

        # Create a skill with `description: >-` block scalar and >500 lines
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

        # Now add a REAL broken link outside code blocks -> MUST trigger attention!
        with (proj / "AGENTS.md").open("a", encoding="utf-8") as fh:
            fh.write("\nSee [Broken Spec](docs/missing-spec.md) and {{UNRESOLVED_TOKEN}}.\n")
        rep_bad = ag.run_audit(ns_audit)
        self.assertFalse(rep_bad["summary"]["healthy"])
        issues = rep_bad["projects"][0]["issues"]
        self.assertTrue(any("broken_doc_link" in x for x in issues))
        self.assertTrue(any("unresolved_placeholders" in x for x in issues))


if __name__ == "__main__":
    unittest.main()
