#!/usr/bin/env python3
"""Unit tests for agent_governor.py covering audit, adopt, lease allocation, 5-gate GC, triage, and restore."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agent_governor as ag  # noqa: E402


class AgentGovernorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.projects = self.root / "AI/Projects"
        self.trash = self.root / "Trash"
        self.home.mkdir(parents=True)
        self.projects.mkdir(parents=True)
        self.trash.mkdir(parents=True)

        # Global rules in sync
        for rel in (".codex/AGENTS.md", ".claude/CLAUDE.md", ".gemini/GEMINI.md"):
            p = self.home / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# Global Rules\n- Keep rules concise.\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_git_project(self, name: str) -> Path:
        proj = self.projects / name
        proj.mkdir(parents=True)
        subprocess.run(["git", "-C", str(proj), "init", "-q"], check=True)
        (proj / "AGENTS.md").write_text(f"# {name}\n- Run `pytest` before shipping.\n", encoding="utf-8")
        return proj

    def test_audit_detects_missing_pointers_zones_and_clutter_then_adopt_heals(self) -> None:
        proj = self._make_git_project("Alpha")
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

        # Run adopt --apply --relocate-clutter
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
        # Empty `.worktrees` trashed, non-empty `tmp/` relocated into `work/retained/from-root-tmp-*`
        self.assertFalse((proj / ".worktrees").exists())
        self.assertFalse((proj / "tmp").exists())
        relocated = list((proj / "work/retained").glob("from-root-tmp-*/probe.pdf"))
        self.assertEqual(len(relocated), 1)

        # Re-audit should now be completely healthy
        rep_after = ag.run_audit(ns_audit)
        self.assertTrue(rep_after["summary"]["healthy"], msg=str(rep_after["summary"]))

    def test_scratch_and_test_dir_allocate_lease(self) -> None:
        proj = self._make_git_project("Beta")
        ns_s = ag.build_parser().parse_args(
            ["scratch", "--project", str(proj), "--agent", "codex", "--topic", "API Probe", "--ttl-hours", "48"]
        )
        sdir = ag.allocate_ephemeral_dir("scratch", ns_s)
        self.assertTrue(sdir.is_dir())
        self.assertEqual(sdir.parent.name, "_scratch")
        lease = json.loads((sdir / ".agent-lease.json").read_text(encoding="utf-8"))
        self.assertEqual(lease["agent"], "codex")
        self.assertEqual(lease["ttl_hours"], 48.0)

        ns_t = ag.build_parser().parse_args(
            ["test-dir", "--project", str(proj), "--agent", "claude", "--topic", "ui-smoke"]
        )
        tdir = ag.allocate_ephemeral_dir("tests", ns_t)
        self.assertTrue(tdir.is_dir())
        self.assertEqual(tdir.parent.name, "_tests")

    def test_gc_five_gates_grandfathering_and_restore(self) -> None:
        proj = self._make_git_project("Gamma")
        for sub in ("_scratch", "_tests", "retained"):
            (proj / "work" / sub).mkdir(parents=True, exist_ok=True)

        old_ts = time.time() - (10 * 86400)

        # 1. Expired _scratch (>3d) -> SHOULD BE RECLAIMED
        exp_scratch = proj / "work/_scratch/20261101-codex-old"
        exp_scratch.mkdir()
        f1 = exp_scratch / "dump.json"
        f1.write_text("{}", encoding="utf-8")
        os.utime(f1, (old_ts, old_ts))
        os.utime(exp_scratch, (old_ts, old_ts))

        # 2. Pinned _scratch with .keep -> MUST BE PROTECTED (Gate 02)
        pinned = proj / "work/_scratch/20261101-codex-pinned"
        pinned.mkdir()
        (pinned / ".keep").write_text("", encoding="utf-8")
        os.utime(pinned / ".keep", (old_ts, old_ts))
        os.utime(pinned, (old_ts, old_ts))

        # 3. Active PID lease in _scratch -> MUST BE PROTECTED (Gate 01)
        active = proj / "work/_scratch/20261101-codex-active"
        active.mkdir()
        lease_file = active / ".agent-lease.json"
        lease_file.write_text(
            json.dumps(
                {
                    "pid": os.getppid(),
                    "expires_at": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        os.utime(lease_file, (old_ts, old_ts))
        os.utime(active, (old_ts, old_ts))

        # 4. Legacy untagged folder directly under work/ -> MUST BE PROTECTED (Grandfathering)
        legacy = proj / "work/sony-firmware-20261024"
        legacy.mkdir()
        lf = legacy / "rootfs.cpio"
        lf.write_text("precious", encoding="utf-8")
        os.utime(lf, (old_ts, old_ts))
        os.utime(legacy, (old_ts, old_ts))

        # 5. Excess .bak files in ~/.codex -> keep newest 2, stage older 2 (Gate 04)
        codex_dir = self.home / ".codex"
        for idx in range(4):
            bf = codex_dir / f"config.toml.bak-{idx}"
            bf.write_text(f"v{idx}", encoding="utf-8")
            ts = time.time() - ((idx + 1) * 86400)
            os.utime(bf, (ts, ts))

        ns_gc = ag.build_parser().parse_args(
            [
                "gc",
                "--root",
                str(self.projects),
                "--home",
                str(self.home),
                "--trash-dir",
                str(self.trash),
                "--keep-backups",
                "2",
                "--skip-simulators",
                "--apply",
            ]
        )
        gc_rep = ag.run_gc(ns_gc)
        self.assertFalse(exp_scratch.exists())
        self.assertTrue(pinned.exists())
        self.assertTrue(active.exists())
        self.assertTrue(legacy.exists())
        self.assertTrue((codex_dir / "config.toml.bak-0").exists())
        self.assertTrue((codex_dir / "config.toml.bak-1").exists())
        self.assertFalse((codex_dir / "config.toml.bak-2").exists())
        self.assertFalse((codex_dir / "config.toml.bak-3").exists())
        self.assertIsNotNone(gc_rep["manifest"])

        # Test atomic restore
        ns_res = ag.build_parser().parse_args(["restore", gc_rep["manifest"]])
        res_rep = ag.run_restore(ns_res)
        self.assertEqual(res_rep["restored_count"], 3)
        self.assertTrue(exp_scratch.exists())
        self.assertTrue((codex_dir / "config.toml.bak-2").exists())
        self.assertTrue((codex_dir / "config.toml.bak-3").exists())

    def test_triage_groups_iterative_legacy_series_readonly(self) -> None:
        proj = self._make_git_project("Delta")
        work = proj / "work"
        for beta in ("beta31", "beta32", "beta33"):
            d = work / f"ui-motion-install-{beta}-20261106"
            d.mkdir(parents=True)
            (d / "bundle.bin").write_bytes(b"x" * 1024)

        md_out = self.root / "triage.md"
        ns_tr = ag.build_parser().parse_args(
            ["triage", "--root", str(self.projects), "--home", str(self.home), "--markdown", str(md_out)]
        )
        rep = ag.run_triage(ns_tr)
        self.assertEqual(rep["total_legacy_items"], 3)
        fams = rep["projects"][0]["iterative_families"]
        self.assertEqual(len(fams), 1)
        self.assertEqual(fams[0]["count"], 3)
        self.assertTrue(md_out.is_file())


if __name__ == "__main__":
    unittest.main()
