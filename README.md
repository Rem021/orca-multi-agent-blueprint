# Orca Multi-Agent Orchestration Blueprint

[![简体中文](https://img.shields.io/badge/Language-简体中文-0969da?style=flat)](./README.zh-CN.md)
[![Agent Setup](https://img.shields.io/badge/Agent_Protocol-AGENT__INSTALL.md-8250df?style=flat)](./AGENT_INSTALL.md)
[![Platform](https://img.shields.io/badge/Platform-macOS_(Apple_Silicon_%26_Intel)-1a7f37?style=flat)](https://github.com/Rem021/orca-multi-agent-blueprint)
[![Orca](https://img.shields.io/badge/Orca_Engine-v1.4%2B-0969da?style=flat)](https://github.com/Rem021/orca-multi-agent-blueprint)
[![Storage](https://img.shields.io/badge/Storage-External_SSD_%7C_Internal_Disk-0969da?style=flat)](#4-dual-mode-storage-external-ssd-vs-internal-disk--4-zone-file-governance)
[![Backups](https://img.shields.io/badge/Backups-7_Automated_LaunchAgents-1a7f37?style=flat)](#5-automated-multi-tier-dailyhourlyweekly-backup--watchdog-system)
[![Guardrails](https://img.shields.io/badge/PreToolUse-Deterministic_Guard-cf222e?style=flat)](./scripts/guard.py)
[![License](https://img.shields.io/badge/License-MIT-656d76?style=flat)](./LICENSE)

> **Switch Language**: [**👉 点击切换至简体中文完整版 (`README.zh-CN.md`)**](./README.zh-CN.md)

A production-grade 3-tier pluggable multi-agent architecture, **dual-mode storage governance (External SSD or Mac Internal Disk)**, **7-job automated daily/hourly/weekly backup suite**, and **1:1 lossless cross-Mac migration engine** for macOS. Designed to run non-destructively on **both brand-new Macs and existing Macs with dozens of repositories and multiple AI agents**.

---

## 1. Quick Start (Universal for Any Mac Developer: Deploy, Choose Storage, or Migrate)

### Option A: Send This Repository Link to Any AI Agent (Interactive Assistant)

Any developer can copy and paste the following prompt into **Claude Code, OpenAI Codex, Google Antigravity, Cursor, or Muse** on either their current Mac or a brand-new Mac:

```text
Please read https://github.com/Rem021/orca-multi-agent-blueprint/blob/main/AGENT_INSTALL.md and ask me (1) whether I want in-place deployment, old-Mac SSD packing, or new-Mac SSD restore, and (2) whether I want to store all AI files on an External SSD (--external) or Mac Internal Disk (--internal), explaining the exact steps first.
```

When the AI Agent reads [`AGENT_INSTALL.md`](./AGENT_INSTALL.md), it will **first ask you for your workflow choice (1 / 2 / 3) and storage preference (A: External SSD / B: Mac Internal Disk)** and explain every step before executing:
1. **Mode 1 — In-Place Deployment, File Governance & Automated Backups (`--auto`)**: Sets up the 3-tier multi-agent architecture, 4-zone file taxonomy (`docs/reports/outputs/work`), `PreToolUse` safety guardrails, native CLI toolchain, 7-job automated daily/hourly/weekly backup suite, and multi-account profile parity on your current Mac.
2. **Mode 2 — Source (Old) Mac Lossless Pack to External SSD (`--pack-to-ssd`)**: Dynamically discovers and packs your `$HOME` agent states, parallel APFS Copy-on-Write SQLite database snapshots, multi-account pools, `LaunchAgents`, Keychain secrets, core `.app` bundles, standalone `Python.framework`, and local repos onto an external SSD (skipping multi-GB VMs like `claudevm.bundle` and `CoreSimulator`).
3. **Mode 3 — Target (New) Mac Lossless Restore + Auto-Deploy (`--restore-from-ssd`)**: Reads the external SSD on your new Mac, restores all files and hardlinks, automatically rewrites `/Users/<old_user>` -> `/Users/<new_user>` if your macOS username changed, cleans stale host locks, restores Keychain & `LaunchAgents`, and chains into `--auto` deployment.

### Option B: One-Line Terminal Bootstrap (Choose External SSD or Internal Disk)

Run in your macOS terminal (`Terminal`, `Ghostty`, or `iTerm2`):

```bash
# Auto-detect (prefers mounted External SSD /Volumes/<SSD>/AI if present, else ~/AI-Workspace/AI)
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh

# Explicitly store all AI files on an External SSD (/Volumes/<SSD>/AI)
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --auto --external

# Explicitly store all AI files on Mac Internal Disk (~/AI-Workspace/AI)
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --auto --internal

# Preview all planned actions in read-only mode without modifying any files
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --dry-run
```

### Option C: One-Line Cross-Mac Hardware Migration (Carry Only Your External SSD)

1. **On your Old Mac (preview first, then pack all internal-disk Agent states, SQLite DBs, accounts, SSH keys & local repos to SSD)**:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --pack-dry-run
   curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --pack-to-ssd
   ```
2. **On your New Mac (plug in the SSD and run one command — 100% offline-capable)**:
   ```bash
   zsh /Volumes/<YOUR_SSD>/AI/Migration/restore-on-new-mac.sh
   ```

---

## 2. 3-Tier Pluggable Architecture

Running multiple coding agents concurrently on a single machine often suffers from **context window pollution**, **single-account rate limits**, **working tree collisions**, and **broken PATHs in non-interactive shells**. This blueprint decouples the stack into three hot-swappable tiers:

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/architecture-en-dark.svg?v=3">
    <source media="(prefers-color-scheme: light)" srcset="./assets/architecture-en-light.svg?v=3">
    <img alt="3-Tier Pluggable Architecture" src="./assets/architecture-en-light.svg?v=3" width="100%">
  </picture>
</p>

| Tier | Architectural Role | Default Component | Pluggable Alternatives (`config.env`) | Core Responsibility |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 1** | **Supervisor** | **Muse** | Orca Mobile, Claude Desktop, Custom CLI | Captures natural-language intent, decomposes cross-repo tasks, dispatches worktrees, and summarizes results |
| **Tier 2** | **Control Plane** | **Orca Desktop & CLI** | `just` + `projectctl.py` CLI Runner | Governs 180+ Git worktrees, handles multi-account quota failover, runs `PreToolUse` safety hooks and naming plugin |
| **Tier 3** | **Worker Fleet & Storage** | **Claude + Codex + Antigravity + DeepSeek** | OpenCode, Cursor, Windsurf, Aider | Executes parallel coding, AST refactoring, simulator testing, dual-mode storage governance, and automated backups |

> [!NOTE]
> To swap your front-end supervisor, worker agents, or storage mode (`external` vs `internal`), update [`config.env`](./config.example.env) and run `./deploy.sh --apply`.

---

## 3. 5-Stage Deterministic Task Lifecycle

Every engineering task follows a strict 5-stage pipeline from worktree creation to cryptographic archival:

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/workflow-en-dark.svg?v=3">
    <source media="(prefers-color-scheme: light)" srcset="./assets/workflow-en-light.svg?v=3">
    <img alt="5-Stage Deterministic Task Lifecycle" src="./assets/workflow-en-light.svg?v=3" width="100%">
  </picture>
</p>

1. **Worktree Spawn**: `projectctl.py` verifies the workspace root (External SSD hardware `VolumeUUID` or Internal Disk `~/AI-Workspace`) and creates an isolated Git worktree branched cleanly off `main`.
2. **Guarded Coding**: The primary agent codes inside the isolated worktree while `agent-guard` intercepts dangerous shell commands pre-execution and `xcbeautify` compresses Xcode logs by ~90%.
3. **Compact Handoff**: Running `just handoff` emits a concise `HANDOFF.json` receipt containing Git Base/Head SHAs, changed file SHA-256 digests, and verification artifacts—reducing handoff context tokens by ~80%.
4. **Cross-Review**: A heterogeneous reviewer agent (e.g., Codex reviewing Claude's patch) performs a read-only audit of `HANDOFF.json` and `git diff`, explicitly separating verified evidence from inference.
5. **Safe Archive**: Before retiring a worktree, `just archive-worktree` writes a full `repo.bundle`, `.tar.gz` snapshot, and `SHA256SUMS.txt` into `AI/Archive/`.

---

## 4. Dual-Mode Storage (External SSD vs. Internal Disk) & 4-Zone File Governance

### 4.1 Choosing Between External SSD (`--external`) and Mac Internal Disk (`--internal`)

You can store your entire `AI/` workspace tree either on a dedicated **External SSD** or directly on your **Mac's Internal SSD**—and switch at any time:

| Dimension | Option A: External SSD Mode (`--external`) | Option B: Mac Internal Disk Mode (`--internal`) |
| :--- | :--- | :--- |
| **Root Path (`AI_VOLUME_PATH`)** | `/Volumes/<Your_SSD>` (e.g. `/Volumes/AI_Workspace`) | `~/AI-Workspace` (customizable via `INTERNAL_WORKSPACE_PATH`) |
| **Hardware UUID Guard** | Verifies `VolumeUUID` before every write; blocks writes if unmounted | Uses `AI_VOLUME_UUID="INTERNAL"`; always online |
| **USB Watchdog (`volume-guard`)** | Checks mount state & USB `sessionID` re-enumeration every 60s | Verifies local workspace & backup directory availability every 60s |
| **Internal Disk Footprint** | Keeps multi-hundred-GB repos, worktrees & caches off internal SSD | Self-contained on internal SSD; zero external hardware needed |
| **Cross-Mac Portability** | Unplug SSD -> plug into new Mac -> instant restore & zero-copy repos | Pack to external drive or NAS when migrating machines |
| **How to Switch** | `just use-external-storage` or `./deploy.sh --auto --external` | `just use-internal-storage` or `./deploy.sh --auto --internal` |

### 4.2 Standardized Workspace & 4-Folder Project Taxonomy

Whether `AI_VOLUME_PATH` points to `/Volumes/AI_Workspace` (External SSD) or `~/AI-Workspace` (Internal Disk), the exact same directory hierarchy and 4-folder project standard apply:

```text
${AI_VOLUME_PATH}/                   # Either /Volumes/<SSD> (--external) or ~/AI-Workspace (--internal)
└── AI/
    ├── Projects/                    # [Canonical Entrypoint Layer] Exactly 1 main clone per repo, parked on clean main
    │   ├── Mac/                     # System maintenance & multi-agent orchestration repository
    │   ├── NovaCam/                 # Fictional Example A: Pro iOS Camera & RAW Engine
    │   ├── FlowWeave/               # Fictional Example B: Cross-Platform Rule & Workflow Engine
    │   └── <AnyProject>/            # Standardized 4-Folder Internal Structure per project:
    │       ├── docs/                #   1. Architecture specs, design docs & operating runbooks (Tracked in Git)
    │       ├── reports/             #   2. Stage audit reports & JSON receipts by YYYY-MM-DD-topic (Tracked in Git)
    │       ├── outputs/             #   3. Reusable tools, plugins & release deliverables (Tracked in Git)
    │       └── work/                #   4. Scratchpad, logs & temporary scripts (Ignored via .git/info/exclude)
    │
    ├── Orca/Worktrees/              # [Orca Isolated Worktree Pool] 1 directory per parallel task
    ├── Codex/Worktrees/             # [Codex Isolated Worktree Pool] e.g., NovaCam-raw-color-fix
    ├── Claude/Worktrees/            # [Claude Isolated Worktree Pool]
    │
    ├── Archive/                     # [Reversible Cold Archive] Retired worktrees bundled here before removal
    │   └── 2026-11-06-raw-fix/      #   Contains repo.bundle + worktree-snapshot.tar.gz + SHA256SUMS.txt
    │
    ├── Data/                        # [Shared Caches & Hardware Leases]
    │   ├── BuildCache/              #   Shared Xcode SourcePackages (SPM) cache across worktrees
    │   └── SimulatorCoordination/   #   iOS Simulator lease locks (active-*/lease.json) to prevent test collisions
    │
    ├── Backups/ResticRepo/          # [Local Fallback Restic Repository] Used automatically when NAS is not configured
    └── Migration/latest/            # [Cross-Mac Lossless Migration Snapshot] Created by --pack-to-ssd
```

---

## 5. Automated Multi-Tier Daily/Hourly/Weekly Backup & Watchdog System

Deployed automatically via [`scripts/setup_backups.py`](./scripts/setup_backups.py) and [`scripts/codex-storage/`](./scripts/codex-storage) into `~/Library/Application Support/Codex Storage/agents/` and `~/Library/LaunchAgents/`, this 7-job `launchd` suite protects every AI agent state, SQLite database, and project repository with **zero manual intervention**:

| Job Name (`LaunchAgent`) | Schedule | Target Destination | Core Engineering Mechanism |
| :--- | :--- | :--- | :--- |
| **`icloud-package`** | **Daily 02:30** | iCloud Drive (`AI备份/每晚小包`) | Clones live SQLite DBs via APFS CoW (`cp -c`) + `wal_checkpoint(TRUNCATE)`, packages core agent configs & scripts, encrypts with **AES-256-CBC PBKDF2** (password auto-stored in macOS Keychain `icloud-package-key`), verifies decryption + `tar -tzf`, prunes to latest 30 packages, and calls `brctl evict` for **zero local disk footprint** |
| **`backup-hot`** | **Hourly (`:00`)** | Restic Repo (NAS SMB or `${AI_VOLUME_PATH}/AI/Backups/ResticRepo`) | Stages consistent SQLite snapshots (`state_5.sqlite`, `orca.db`, `memory.db`, `opencode.db`) + `$HOME` agent profiles (`.codex`, `.claude`, `.orca`, `.gemini`, `.config`, `.ssh`, `.agents`) into deduplicated Restic snapshots |
| **`backup-projects`** | **Daily 03:00** | Restic Repo (NAS SMB or Local) | Backs up `${AI_VOLUME_PATH}/AI` (`Projects`, `Worktrees`, `Archive`, `Data`) while automatically excluding transient build caches (`DerivedData`, `.build`, `node_modules`, `Migration`) |
| **`backup-media`** | **Weekly Sun 04:00** | Restic Repo (NAS SMB or Local) | Optional rate-limited (`--limit-upload 46080` = 45 MB/s) backup of external media volumes (`BACKUP_MEDIA_SOURCE_PATH`) |
| **`maintenance`** | **Weekly Sun 05:30** | Restic Repo + Local SQLite | Runs `restic forget --keep-hourly 24 --keep-daily 30 --keep-weekly 12 --keep-monthly 24 --prune`, `restic check` (with `--read-data-subset=5%` on the 1st Sunday of each month), and checks SQLite freelist ratio |
| **`restore-drill`** | **Weekly Sun 07:00** | Temp Sandbox (`mktemp -d`) | Automated disaster-recovery verification: runs `restic restore latest --tag hot --verify` into an isolated temp directory and asserts restored files are readable |
| **`volume-guard`** | **Every 60s** | Local Telemetry (`volume-state.json`) | In `external` mode, verifies hardware `VolumeUUID` and detects USB bus `sessionID` re-enumeration (logging drop/reconnect events); in `internal` mode, verifies `~/AI-Workspace/AI` readiness |

> [!TIP]
> - **Zero NAS Required Out of the Box**: If you do not configure `BACKUP_NAS_SMB_URL` in `~/.orca/backup-config.env` (or `config.env`), Restic automatically initializes and backs up to `${AI_VOLUME_PATH}/AI/Backups/ResticRepo` using an auto-generated 256-bit Keychain password (`restic-mac-agents`).
> - **Check Live Backup Health Anytime**: Run `./deploy.sh --backup-status` or `just backup-status` to generate and view `~/Library/Application Support/Codex Storage/backup-status.md`.

---

## 6. Workspace, Chat & Branch Naming Convention

When managing 180+ workspaces concurrently, default random fish names (`lancetfish`, `soldierfish`) make navigation difficult. The bundled [`plugins/orca-naming-guard`](./plugins/orca-naming-guard) plugin enforces a clean naming standard with **zero LLM token overhead**:

| Object Type | Naming Pattern | Canonical Example (Fictional Projects) |
| :--- | :--- | :--- |
| **Main Repository Entry** | `Canonical Product Name` | `NovaCam`, `FlowWeave`, `StarFront`, `ChromaStudio`, `Mac` |
| **Dedicated Sub-Entry** | `ProjectName｜Purpose` | `NovaCam｜Color Engine Lab` |
| **Active Worktree & Chat** | `ProjectName｜SpecificTopic` | `Mac｜Multi-Agent Setup & Naming`, `ChromaStudio｜Color Wheel Concurrency Fix` |
| **Idle Standby Workspace** | `ProjectName｜Standby <8-char-hash>` | `NovaCam｜Standby 888c8500` |
| **Imported Reference Dir** | `ProjectName｜Reference Data` | `NovaCam｜Reference Data A` |
| **Git Task Branch** | `<prefix>/<2-4-word-kebab-case>` | `codex/cloud-rule-save`, `claude/hdr-highlight-fix` |

---

## 7. Skill Matrix & `PreToolUse` Safety Guardrails

### 7.1 Unified Skill Matrix (Synced Across All Profiles)

| Category | Skill Name | Engineering Purpose |
| :--- | :--- | :--- |
| **Orchestration** | `orchestration` | Spawn heterogeneous worker agents (e.g., dispatch Codex from Claude) and collect results |
| **Orchestration** | `orca-cli` | Programmatically control Orca worktrees, managed terminals, repository settings, and browser |
| **Simulator & GUI** | `orca-emulator` | Drive booted iOS Simulators inside Orca's emulator pane (taps, gestures, accessibility tree) |
| **Simulator & GUI** | `orca-computer-use` | GUI automation for visible macOS app windows via accessibility tree when CLI is unavailable |
| **Code Intelligence** | [`ast-grep`](./skills/ast-grep/SKILL.md) | Syntax-aware AST search & rewrite across Swift/C/C++/Rust/TS/Python + `rg`, `fd`, `repomix`, `xcbeautify` |
| **Storage Hygiene** | `agent-slim` / `mole` | Audit AI session bloat, reclaim SQLite freelist pages, and deduplicate files via APFS `cp -c` |

### 7.2 Deterministic `PreToolUse` Safety Guardrail ([`scripts/guard.py`](./scripts/guard.py))

Installed at `~/.local/bin/agent-guard` and wired into every agent's `PreToolUse` hook, this guardrail blocks destructive commands **before execution**:

| Rule ID | Blocked Command Pattern | Enforced Safe Alternative |
| :--- | :--- | :--- |
| **Rule 01** | Accessing `/Volumes/<Drive>/AI` when unmounted | Aborts immediately so agents never create fake directories on an unmounted stub |
| **Rule 02** | `rm -rf` on `AI/Projects`, `Worktrees`, or `Archive` | Archive via `just archive-worktree`, move to Trash, then run `git worktree remove` |
| **Rule 03** | `git reset --hard` or `git clean -f / -fdx` | Bundle uncommitted work to `AI/Archive/` via `git bundle` + `.tar.gz` first |
| **Rule 04** | `git worktree prune` without `--dry-run` | Always run `git worktree prune --dry-run` first to verify external drive state |
| **Rule 05** | `xcrun simctl create` or `xcrun simctl clone` | Reuse designated primary/secondary simulators and check `SimulatorCoordination` leases |
| **Rule 06** | Emptying Trash (`rm -rf ~/.Trash/*` / `EmptyTrash`) | Preserve Trash contents for emergency recovery |
| **Rule 07** | Pushing restricted branches (`develop`, `feat/C-*`) | Blocked at both `PreToolUse` and Git `pre-push` hook layers |

---

## 8. Daily Operational Recipes (`Justfile`)

```bash
just --list                                  # List all 17 standardized operational recipes
just verify-volume                           # 1. Verify workspace volume mount & hardware UUID
just doctor                                  # 2. Run read-only health & profile parity audit
just slim-status                             # 3. Inspect physical vs. logical storage & SQLite freelist pages
just sim-leases                              # 4. Inspect active iOS Simulator leases & running xcodebuild jobs
just test-guardrails                         # 5. Run unit & integration tests for PreToolUse guardrails
just handoff repo="." goal="Stage complete"  # 6. Generate compact cross-agent HANDOFF.json receipt
just archive-worktree <path> <topic>         # 7. Safely bundle & checksum a worktree into AI/Archive/
just pack-dry-run                            # 8. Preview cross-Mac SSD migration bundle (Zero-VM)
just pack-to-ssd                             # 9. Losslessly pack all Agent states, SQLite DBs & repos to SSD
just restore-from-ssd                        # 10. Losslessly restore onto a new Mac + run full --auto deploy
just migration-status                        # 11. Inspect latest SSD migration snapshot manifest & integrity
just setup-backups                           # 12. Install/update the 7-job automated daily/hourly/weekly backup suite
just backup-status                           # 13. Render & view unified backup & storage health report
just backup-now-icloud                       # 14. Trigger an immediate encrypted iCloud nightly package backup
just backup-now-hot                          # 15. Trigger an immediate hourly Restic hot-state snapshot
just use-external-storage                    # 16. Switch/deploy workspace in External SSD mode (/Volumes/<SSD>/AI)
just use-internal-storage                    # 17. Switch/deploy workspace in Mac Internal Disk mode (~/AI-Workspace/AI)
```

---

## 9. Quantified Engineering Gains

| Dimension | Before (Ad-Hoc Multi-Agent Setup) | After (Orca Multi-Agent Blueprint) | Measured Engineering Gain |
| :--- | :--- | :--- | :--- |
| **Multi-Account Failover** | Switching profiles after rate-limiting loses skills, rules, and Xcode MCP | `sync_profiles.py` keeps all Claude/Codex/Antigravity profiles 100% identical | **Zero capability drop on account failover** |
| **Storage Flexibility** | Hardcoded paths force either internal disk bloat or mandatory external drive | `--external` (`/Volumes/<SSD>`) vs `--internal` (`~/AI-Workspace`) dual-mode switch | **Works on both desktop rigs & travel laptops** |
| **Automated Daily Backups** | Manual copies or uncheckpointed SQLite backups corrupt live `-wal` journals | 7 `LaunchAgents` run APFS CoW + `wal_checkpoint(TRUNCATE)`, AES-256 iCloud & Restic | **Zero-touch hourly/daily backups + weekly restore drill** |
| **Non-Interactive Shell PATH** | Subshells miss `rg`/`fd`/`ast-grep` and fall back to slow `grep -R` across `work/` | `~/.zshenv` injects native `~/.local/bin` tools + `.git/info/exclude` skips `/work/` | **5–10x faster code search & zero scratch pollution** |
| **Xcode Build & Test Logs** | Raw 50,000-line `xcodebuild` output floods context windows | Piped through `xcbeautify` + single-destination flags + simulator lease locks | **~90% fewer build log tokens & zero simulator deadlocks** |
| **Destructive Command Safety** | Prompt-only rules occasionally fail to stop `git reset --hard` or `rm -rf` | `PreToolUse` hook (`agent-guard`) blocks dangerous commands pre-execution | **100% deterministic interception of destructive commands** |
| **180+ Worktree Naming** | Unreadable fish names (`soldierfish`) or LLM-billed renaming | `orca-naming-guard` local state machine assigns standby IDs & syncs valid titles | **Zero LLM tokens used; auto-organized within 3 seconds** |

---

## 10. Repository Layout

```text
orca-multi-agent-blueprint/
├── assets/                          # Theme-adaptive single-layer SVG diagrams (Light & Dark modes)
├── bootstrap.sh                     # Universal one-line bootstrapper (supports --external / --internal)
├── deploy.sh                        # Deployment, storage switch, backup setup & migration engine
├── AGENT_INSTALL.md                 # Interactive installation & migration protocol for AI coding agents
├── config.example.env               # Parameterized configuration template (storage mode, agents, backups)
├── Justfile                         # 17 daily operational, backup, storage & migration recipes
├── scripts/
│   ├── discover_and_adopt.py        # Smart environment auto-discovery & 4-zone project adoption
│   ├── setup_backups.py             # Automated installer for Keychain secrets & 7 backup LaunchAgents
│   ├── codex-storage/               # 7-job automated backup & hardware watchdog suite
│   │   ├── lib.sh                   #   Shared dual-mode storage, Keychain, APFS CoW SQLite & lock library
│   │   ├── icloud-package.sh        #   Nightly 02:30 AES-256-CBC encrypted iCloud backup + brctl evict
│   │   ├── backup-restic.sh         #   Hourly hot-state, daily 03:00 projects & weekly media Restic backups
│   │   ├── maintenance.sh           #   Weekly Sunday 05:30 retention pruning & integrity verification
│   │   ├── restore-drill.sh         #   Weekly Sunday 07:00 automated restore verification drill
│   │   ├── volume-guard.sh          #   60-second hardware VolumeUUID & USB bus re-enumeration watchdog
│   │   ├── volume-observer.py       #   DiskArbitration / workspace event observer daemon
│   │   └── status.sh                #   Unified Markdown backup & storage health report generator
│   ├── migrate.py                   # Universal lossless cross-Mac SSD pack & restore engine (Zero-VM)
│   ├── install_cli_tools.sh         # Zero-dependency native CLI binary installer (with 0.0s fast-path)
│   ├── sync_profiles.py             # Multi-agent & multi-account skill, MCP, rule & hook synchronizer
│   ├── guard.py                     # Deterministic PreToolUse & Git pre-push safety guardrail
│   ├── doctor.py                    # Read-only health & profile parity auditor
│   ├── handoff.py                   # Token-compact cross-agent handoff packet generator
│   ├── projectctl.py                # Guarded worktree creator with hardware VolumeUUID verification
│   └── agent-slim.py                # AI agent storage auditor & APFS clonefile deduplicator
├── plugins/
│   └── orca-naming-guard/           # Zero-token Orca workspace naming guard plugin (with unit tests)
├── skills/
│   └── ast-grep/SKILL.md            # Structural AST search & native CLI toolchain skill definition
├── templates/
│   ├── AGENTS.md.tmpl               # Parameterized global rules template
│   └── MUSE_SUPERVISOR_PLAYBOOK.md  # Tier-1 supervisor playbook
└── tests/
    ├── test_guard.py                # Unit & integration test suite for safety guardrails
    └── test_migrate.py              # Unit tests for SQLite CoW backup, LaunchAgent discovery & path relocation
```
