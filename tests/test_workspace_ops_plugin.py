"""Integrity and hygiene checks for the bundled Workspace Ops Radar Orca plugin.

Runs in the standard Python unittest suite (macOS CI Python 3.9 & 3.x) with no third-party
dependencies; when Node.js is available, also verifies panel/app.js syntax.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugins" / "workspace-ops"


class WorkspaceOpsPluginTests(unittest.TestCase):
    def test_manifest_valid_and_entries_exist(self) -> None:
        manifest_path = PLUGIN_DIR / "orca-plugin.json"
        self.assertTrue(manifest_path.is_file(), "orca-plugin.json missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["manifestVersion"], 2)
        self.assertEqual(manifest["execution"], "trusted-worker")
        self.assertEqual(manifest["pluginApi"], 1)
        self.assertEqual(manifest["id"], "workspace-ops-radar")
        self.assertTrue((PLUGIN_DIR / manifest["main"]).is_file(), "worker main file missing")

        panels = manifest.get("contributes", {}).get("panels", [])
        self.assertGreaterEqual(len(panels), 1)
        for panel in panels:
            self.assertTrue((PLUGIN_DIR / panel["entry"]).is_file(), f"panel entry missing: {panel['entry']}")

        capabilities = {c["kind"] for c in manifest.get("capabilities", [])}
        for required in ("workspace:read", "workspace:write", "agent:read", "storage:own", "settings:own"):
            self.assertIn(required, capabilities)

    def test_panel_html_has_no_external_network_assets(self) -> None:
        html_path = PLUGIN_DIR / "panel" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        self.assertIn('./styles.css', html)
        self.assertIn('./app.js', html)
        self.assertTrue((PLUGIN_DIR / "panel" / "styles.css").is_file())
        self.assertTrue((PLUGIN_DIR / "panel" / "app.js").is_file())
        self.assertIsNone(
            re.search(r'(?:src|href)\s*=\s*["\']https?://', html, re.IGNORECASE),
            "Orca plugin HTML frames block external network requests",
        )

    def test_no_hardcoded_user_home_paths_in_runtime_sources(self) -> None:
        for rel in ("src/core.ts", "src/collectors.ts", "src/worker.ts", "panel/app.js"):
            text = (PLUGIN_DIR / rel).read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"/Users/[A-Za-z0-9_.-]+/", text),
                f"Hardcoded /Users/<name>/ path found in {rel}",
            )

    def test_panel_js_syntax_when_node_available(self) -> None:
        node = shutil.which("node")
        if not node:
            return
        res = subprocess.run(
            [node, "--check", str(PLUGIN_DIR / "panel" / "app.js")],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
