---
name: agent-governor
description: Multi-Agent Workspace File Norm Governance, Startup Context/Token Optimizer, and 5-Gate Ephemeral Scratch/Test Directory Garbage Collector. Use when auditing or optimizing `AGENTS.md`/`CLAUDE.md`/`GEMINI.md` context bloat and 4-zone directory rules (`docs`, `reports`, `outputs`, `work`), allocating isolated per-agent scratch or test folders (`work/_scratch`, `work/_tests`), triaging historical `work/` folders, or running safe TTL-based cleanup of agent temporary caches, `.bak` backups, and ephemeral test simulators.
---

# `agent-governor` — Multi-Agent File Governance, Context Optimizer & 5-Gate TTL GC

`agent-governor` (`v1.2.0`) enforces three pillars across Claude Code, OpenAI Codex, Google Gemini / Antigravity, Cursor, Windsurf, and Orca:

1. **Startup Context & File Norm Governance (`audit` / `adopt`)**: Keeps global and project `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` within empirical adherence budgets (`arXiv:2602.11988`, `arXiv:2601.20404`), checks `SKILL.md` YAML block-scalar frontmatter & 500-line body caps, verifies `@AGENTS.md` pointer parity without dirtying `git status`, ensures `/work/` is excluded via `.git/info/exclude` (preventing IDE/agent `.gitignore` search blindness), and blocks unauthorized root clutter (`tmp/`, `scratch/`, `.worktrees/`) across projects and nested Git worktrees.
2. **Per-Agent Collision-Safe Lease Sandboxes (`scratch` / `test-dir`)**: Replaces flat `work/` dumping with a strict 3-tier lifecycle contract and automatic parallel-agent lease disambiguation:
   - `work/_scratch/<YYYYMMDD>-<agent>-<topic>/` — Throwaway probes, logs, screenshots (**3-day TTL** in `daily`, **1-day** in `deep`).
   - `work/_tests/<YYYYMMDD>-<agent>-<topic>/` — Ephemeral test runs, beta bundles, `.xcresult` (**7-day TTL** in `daily`, **3-day** in `deep`).
   - `work/retained/<topic>/` — Long-lived datasets, firmware baselines, reference media (**Never auto-deleted**).
3. **5-Gate Reversible TTL Garbage Collection (`gc` / `triage` / `restore`)**: Sweeps expired `_scratch`, `_tests` (across `AI/Projects` and depth 1–4 `AI/{Orca,Codex,Claude}/Worktrees`), multi-account agent internal junk (`.tmp`, `shell-snapshots`, `paste-cache`, `telemetry`, `log`, `tmpclaude-*-cwd`, excess `.bak` files), and idle ephemeral test iOS Simulators into Trash with a `manifest.json` receipt — **never running `rm -rf` and never deleting grandfathered legacy `work/<dir>` data**.

---

## Standard Workflows

### 1. Allocate an Isolated Scratch or Test Directory (Instead of Creating `tmp/` in Repo Root)

```bash
# Allocate a 3-day TTL scratch directory (auto-disambiguates if another live agent holds the same topic)
SCRATCH_DIR="$(agent-governor scratch --topic api-probe)"

# Allocate a 7-day TTL test output directory for test bundles or verification artifacts
TEST_OUT_DIR="$(agent-governor test-dir --topic ui-smoke)"
```

### 2. Audit Startup Context Budgets, Pointers, Nested Worktrees & 4-Zone File Norms

```bash
# Read-only human-readable audit across all projects, active worktrees, and installed skills
agent-governor audit

# Strict JSON output for CI or doctor scripts
agent-governor audit --strict --json
```

### 3. Heal Missing 4-Zone Directories, `@AGENTS.md` Pointers & Root Clutter

```bash
# Preview changes first (dry-run default)
agent-governor adopt --relocate-clutter

# Apply non-destructive healing (keeps git status clean; relocates non-empty root tmp/ into work/retained/)
agent-governor adopt --relocate-clutter --apply
```

### 4. Run 5-Gate TTL Garbage Collection (Dry-Run First, Reversible via Trash)

```bash
# Preview reclaimable scratch/test dirs, multi-account .tmp caches & idle test simulators
agent-governor gc

# Execute daily cleanup into Trash with manifest.json receipt (serialized via fcntl.flock)
agent-governor gc --apply

# Tighter deep cleanup (scratch=1d, tests=3d, caches=3d, tmp=6h, sim=1d)
agent-governor gc --mode deep --apply

# Ultra-fast SessionEnd hook sweep for dead tmpclaude-*-cwd & empty scratch dirs
agent-governor gc --mode session-end --apply
```

### 5. Triage Legacy `work/` + Stranded Worktree Data & Restore Any Cleanup Batch

```bash
# Generate a read-only family grouping & worktree hold-data report
agent-governor triage --output /tmp/work-triage.md

# Preview or atomically restore the latest un-restored GC/adopt manifest
agent-governor restore --latest --dry-run
agent-governor restore --latest --apply
```

---

## Five Non-Negotiable Safety Gates

1. **Gate 01 — `flock` Mutex, Active Process, Lease PID & `lsof` Open-FD Lock**: Serializes GC runs via `fcntl.flock`, checks `ps -axo pid,command`, `.agent-lease.json` creator PID liveness, `lsof -Fn` open file descriptors / working directories, and `--min-idle-hours`.
2. **Gate 02 — Grandfathering & `.keep`/`.pinned` Guard**: Never auto-deletes `work/retained/`, any folder containing `.keep` / `.pin` / `.pinned` / `HOLD`, or any pre-existing untagged folder directly under `work/<legacy>`.
3. **Gate 03 — Batched Git Tracked & Symlink Boundary Guard**: Never follows symlinks (`os.scandir` `follow_symlinks=False`) and verifies per-repo batched `git ls-files` is empty before staging any item.
4. **Gate 04 — Multi-Account Harness Isolation**: Safely cleans stale `.tmp`, `shell-snapshots`, `paste-cache`, `telemetry`, runtime `log`, and excess `.bak` backups (retaining newest 2) across `~/.codex`, `~/.claude`, `~/.gemini`, and `orca/{codex-accounts,claude-profiles,antigravity-chat}` without touching active sessions, transcripts, or SQLite databases.
5. **Gate 05 — Primary Simulator Guard & Same-Volume Reversible Trash Receipt**: Protects primary/secondary and booted iOS Simulators, deletes only idle `Shutdown` simulators with anchored ephemeral prefixes (`agent-tmp`, `ephemeral-`, `scratch-sim`, `temp-sim`; logged in `manifest.json` as non-Trash external receipts — pass `--skip-simulators` to disable), and atomically stages filesystem items via same-volume `os.rename` into `.Trashes/<uid>` or `~/.Trash` (refusing cross-device `copy+rmtree` or mounted subvolumes) with `manifest.json`.
