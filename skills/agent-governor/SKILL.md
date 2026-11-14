---
name: agent-governor
description: Multi-Agent Workspace File Norm Governance, Startup Context/Token Optimizer, and 5-Gate Ephemeral Scratch/Test Directory Garbage Collector. Use when auditing or optimizing `AGENTS.md`/`CLAUDE.md`/`GEMINI.md` context bloat and 4-zone directory rules (`docs`, `reports`, `outputs`, `work`), allocating isolated per-agent scratch or test folders (`work/_scratch`, `work/_tests`), triaging historical `work/` folders, or running safe TTL-based cleanup of agent temporary caches, `.bak` backups, and ephemeral test simulators.
---

# `agent-governor` — Multi-Agent File Governance, Context Optimizer & 5-Gate TTL GC

`agent-governor` (`v1.0.0`) enforces three pillars across Claude Code, OpenAI Codex, Google Gemini / Antigravity, Cursor, Windsurf, and Orca:

1. **Startup Context & File Norm Governance (`audit` / `adopt`)**: Keeps global and project `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` within empirical adherence budgets (`arXiv:2602.11988`, `arXiv:2601.20404`), checks `@AGENTS.md` pointer parity, ensures `/work/` is excluded via `.git/info/exclude` (preventing IDE/agent `.gitignore` search blindness), and blocks unauthorized root clutter (`tmp/`, `scratch/`, `.worktrees/`).
2. **Per-Agent Lease-Stamped Ephemeral Sandboxes (`scratch` / `test-dir`)**: Replaces flat `work/` dumping with a strict 3-tier lifecycle contract:
   - `work/_scratch/<YYYYMMDD>-<agent>-<topic>/` — Throwaway probes, logs, screenshots (**3-day TTL**).
   - `work/_tests/<YYYYMMDD>-<agent>-<topic>/` — Ephemeral test runs, beta bundles, `.xcresult` (**7-day TTL**).
   - `work/retained/<topic>/` — Long-lived datasets, firmware baselines, reference media (**Never auto-deleted**).
3. **5-Gate Reversible TTL Garbage Collection (`gc` / `triage` / `restore`)**: Sweeps expired `_scratch`, `_tests`, multi-account agent internal junk (`.tmp`, `shell-snapshots`, `paste-cache`, `tmpclaude-*-cwd`, excess `.bak` files), and idle ephemeral test iOS Simulators into `~/.Trash/agent-governor-<YYYY-MM-DD>/` with a `manifest.json` receipt — **never running `rm -rf` and never deleting grandfathered legacy `work/<dir>` data**.

---

## Standard Workflows

### 1. Allocate an Isolated Scratch or Test Directory (Instead of Creating `tmp/` in Repo Root)

```bash
# Allocate a 3-day TTL scratch directory for temporary scripts, dumps, or screenshots
SCRATCH_DIR="$(agent-governor scratch --topic api-probe)"

# Allocate a 7-day TTL test output directory for test bundles or verification artifacts
TEST_OUT_DIR="$(agent-governor test-dir --topic ui-smoke)"
```

### 2. Audit Startup Context Budgets, Pointers & 4-Zone File Norms

```bash
# Read-only human-readable audit across all projects and installed skills
agent-governor audit

# Strict JSON output for CI or doctor scripts
agent-governor audit --strict --json
```

### 3. Heal Missing 4-Zone Directories, `@AGENTS.md` Pointers & Root Clutter

```bash
# Preview changes first (dry-run default)
agent-governor adopt --relocate-clutter

# Apply non-destructive healing (relocates non-empty root tmp/ into work/retained/)
agent-governor adopt --relocate-clutter --apply
```

### 4. Run 5-Gate TTL Garbage Collection (Dry-Run First, Reversible via `~/.Trash`)

```bash
# Preview reclaimable scratch/test dirs, multi-account .tmp caches & idle test simulators
agent-governor gc

# Execute cleanup into ~/.Trash/agent-governor-<date>/ with manifest.json receipt
agent-governor gc --apply

# Ultra-fast (<300ms) SessionEnd hook sweep for dead tmpclaude-*-cwd & empty scratch dirs
agent-governor gc --mode session-end --apply
```

### 5. Triage Legacy `work/` Folders & Restore Any Cleanup Batch

```bash
# Generate a read-only family grouping report of grandfathered historical work/* folders
agent-governor triage --markdown /tmp/work-triage.md

# Atomically restore all items from a previous GC or adopt manifest
agent-governor restore ~/.Trash/agent-governor-2026-11-14/gc-daily-041500-1234/manifest.json
```

---

## Five Non-Negotiable Safety Gates

1. **Gate 01 — Active Process & Lease PID Lock**: Checks `ps -axo pid,command`, `.agent-lease.json` creator PID liveness, and `--min-idle-hours` (default `2h`).
2. **Gate 02 — Grandfathering & `.keep` Pin Guard**: Never auto-deletes `work/retained/`, any folder containing `.keep` / `.pin` / `HOLD`, or any pre-existing untagged folder directly under `work/<legacy>`.
3. **Gate 03 — Git Tracked & Symlink Boundary Guard**: Never follows symlinks and verifies `git ls-files` is empty before staging any item.
4. **Gate 04 — Multi-Account Harness Isolation**: Safely cleans stale `.tmp`, `shell-snapshots`, `paste-cache`, and excess `.bak` backups (retaining newest 2) across `~/.codex`, `~/.claude`, `~/.gemini`, and `orca/{codex-accounts,claude-profiles}` without touching active sessions, transcripts, or SQLite databases.
5. **Gate 05 — Primary Simulator Guard & Reversible Trash Receipt**: Protects primary/secondary and booted iOS Simulators, deletes only idle `Shutdown` simulators explicitly named with ephemeral test markers (`agent-tmp`, `scratch`, `测试`), and moves all filesystem items to `~/.Trash` with `manifest.json`.
