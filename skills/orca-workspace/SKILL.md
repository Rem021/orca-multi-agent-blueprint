---
name: orca-workspace
description: >-
  Unified multi-agent Mac workspace toolkit: environment deployment & health checks (`deploy.sh`,
  `doctor.py`, `sync_profiles.py`, `agent-guard`, `agent-ship`), 4-zone file governance & 5-gate
  TTL cleanup (`agent-governor`), and fast native CLI tools (`ast-grep`, `rg`, `fd`,
  `xcbeautify`, `just`, `repomix`, `scc`).
---

# `orca-workspace` — Multi-Agent Deployment, File Governance & Fast CLI Toolkit

Unified single-skill entrypoint for the `orca-multi-agent-blueprint` environment.

## 1. Environment Setup, Sync & Health (`orca-multi-agent-blueprint`)

```bash
# Health check (exit 0 healthy, 1 needs attention)
python3 ~/AI-Workspace/AI/Projects/Mac/outputs/orca-multi-agent-blueprint/scripts/doctor.py --summary

# Sync skills, rules, MCPs, and PreToolUse/SessionEnd hooks across all agent profiles
~/AI-Workspace/AI/Projects/Mac/outputs/orca-multi-agent-blueprint/deploy.sh --sync-profiles

# Re-deploy scripts, LaunchAgents, and guardrails (preview first)
~/AI-Workspace/AI/Projects/Mac/outputs/orca-multi-agent-blueprint/deploy.sh --dry-run
~/AI-Workspace/AI/Projects/Mac/outputs/orca-multi-agent-blueprint/deploy.sh --apply
```

- **Guardrail**: `~/.local/bin/agent-guard` blocks destructive commands and root clutter via `PreToolUse`.
- **Delivery**: `~/.local/bin/agent-ship` rebases, scans secrets, runs `~/.orca/ship.json` checks, opens/merges PRs (`--no-merge` for major decisions).

## 2. 4-Zone File Governance & 5-Gate Reversible Cleanup (`agent-governor`)

All projects follow `docs/` · `reports/` · `outputs/` · `work/{_scratch,_tests,retained}` (isolated via `.git/info/exclude` + `.metadata_never_index`). Never create `tmp/`, `scratch/`, or `.worktrees/` in repo roots.

```bash
# Allocate isolated per-agent scratch (3d TTL) or test output (7d TTL) directory
SCRATCH_DIR="$(agent-governor scratch --topic api-probe)"
TEST_DIR="$(agent-governor test-dir --topic ui-smoke)"

# Read-only rule/pointer/MCP/clutter audit
agent-governor audit

# Heal missing 4-zone directories, @AGENTS.md pointers & Spotlight shields
agent-governor adopt --relocate-clutter --apply

# Preview filesystem cleanup, simulator deletion and LaunchServices actions
agent-governor gc                 # dry-run preview
agent-governor gc --skip-simulators --apply   # stage filesystem items in Trash

# Inventory legacy work/ or restore staged files from the latest batch
agent-governor triage --output /tmp/work-triage.md
agent-governor restore --latest --apply
```

- **Safety**: Never runs `rm -rf`; skips active PIDs, open `lsof +D` handles, `.keep`/`.pinned`, `work/retained/`, grandfathered `work/<legacy>`, and tracked Git files.

## 3. Fast Native CLI Toolchain (`~/.local/bin`)

Prefer these native Apple Silicon binaries over slow BSD `grep`/`find` or raw `xcodebuild` output:

- **AST search/rewrite**: `ast-grep run -p '$OBJ.perform($$$ARGS)' -l swift .` (add `-r '...' -U` to rewrite)
- **Ignore-aware search**: `rg -n "Pattern" .` · `fd -e swift -e metal .`
- **Compact Xcode logs (~90% smaller)**: `set -o pipefail && xcodebuild test ... 2>&1 | xcbeautify`
- **Context packing / metrics / tasks**: `repomix --include "src/**" --compress --output /tmp/ctx.xml` · `scc .` · `just --list`
