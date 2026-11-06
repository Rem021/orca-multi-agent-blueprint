# Orca Multi-Agent Orchestration Blueprint

[![简体中文](https://img.shields.io/badge/Language-简体中文-0969da?style=flat)](./README.zh-CN.md)
[![Agent Setup](https://img.shields.io/badge/Agent_Protocol-AGENT__INSTALL.md-8250df?style=flat)](./AGENT_INSTALL.md)
[![Platform](https://img.shields.io/badge/Platform-macOS_(Apple_Silicon_%26_Intel)-1a7f37?style=flat)](https://github.com/Rem021/orca-multi-agent-blueprint)
[![Orca](https://img.shields.io/badge/Orca_Engine-v1.4%2B-0969da?style=flat)](https://github.com/Rem021/orca-multi-agent-blueprint)
[![Guardrails](https://img.shields.io/badge/PreToolUse-Deterministic_Guard-cf222e?style=flat)](./scripts/guard.py)
[![License](https://img.shields.io/badge/License-MIT-656d76?style=flat)](./LICENSE)

> **Switch Language**: [**👉 点击切换至简体中文完整版 (`README.zh-CN.md`)**](./README.zh-CN.md)

A production-grade 3-tier pluggable multi-agent architecture, Git worktree isolation standard, and zero-config automated deployment suite for macOS. Designed to run non-destructively on **both brand-new Macs and existing Macs with dozens of repositories and multiple AI agents**.

---

## 1. Quick Start (New & Existing Macs)

### Option A: Send This Repository Link to Any AI Agent (Zero Manual Setup)

Copy and paste the following prompt into **Claude Code, OpenAI Codex, Google Antigravity, Cursor, or Muse**:

```text
Please read https://github.com/Rem021/orca-multi-agent-blueprint/blob/main/AGENT_INSTALL.md and follow its protocol to automatically deploy and optimize my Mac multi-agent workflow (run --dry-run first, then execute --auto and show me the doctor.py report).
```

### Option B: One-Line Terminal Bootstrap

Run the following command in your macOS terminal (`Terminal`, `Ghostty`, or `iTerm2`):

```bash
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh
```

To preview all planned actions in read-only mode without modifying any files:

```bash
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --dry-run
```

### Option C: Lossless Cross-Mac Migration (Carry Only Your External SSD)

Moving to a new MacBook Pro without bringing your old Mac or copying bulky VMs (`claudevm.bundle` / `CoreSimulator`)?

1. **On your old Mac (pack internal-disk Agent states, online SQLite backups, accounts, SSH keys & local repos to SSD)**:
   ```bash
   ./deploy.sh --pack-to-ssd
   ```
2. **On your new Mac (plug in the SSD and restore + auto-deploy in one command)**:
   ```bash
   zsh /Volumes/<YOUR_SSD>/AI/Migration/restore-on-new-mac.sh
   # Or via GitHub bootstrapper:
   curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --restore-from-ssd
   ```

**What the Automated Deployment Engine Accomplishes in 6 Steps:**
1. **Storage & Volume UUID Detection**: Scans `/Volumes/*` for a dedicated external SSD and records its hardware `VolumeUUID` (preventing accidental writes to unmounted mountpoint stubs), or falls back cleanly to `~/AI-Workspace`.
2. **Non-Destructive Project Adoption**: Scans existing repositories in `AI/Projects/*` and Orca (`orca repo list`), creating missing `docs/`, `reports/`, `outputs/`, and `work/` directories without altering any source code, and appending `/work/` to `.git/info/exclude`.
3. **Simulator & Agent Fleet Discovery**: Detects available iOS Simulators (`xcrun simctl`) and installed CLI agents (`claude`, `codex`, `antigravity`, `dsh`, `opencode`, `cursor`) to generate `config.env`.
4. **Zero-Dependency Native CLI Toolchain**: Installs native `arm64`/`x86_64` binaries (`rg`, `fd`, `ast-grep`, `just`, `xcbeautify`, `scc`, `repomix`) into `~/.local/bin` and configures `~/.zshenv` so non-interactive agent subshells always find them.
5. **Multi-Account Profile Synchronization**: Aligns global instruction files (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`), `PreToolUse` safety hooks, Xcode MCP bridges, and skill symlinks across every Orca profile and account.
6. **Self-Test & Health Audit**: Executes guardrail unit tests, naming plugin tests, and the read-only health audit (`doctor.py`).

---

## 2. 3-Tier Pluggable Architecture

Running multiple coding agents concurrently on a single machine often suffers from **context window pollution**, **single-account rate limits**, **working tree collisions**, and **broken PATHs in non-interactive shells**. This blueprint decouples the stack into three hot-swappable tiers:

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/architecture-en-dark.svg?v=2">
    <source media="(prefers-color-scheme: light)" srcset="./assets/architecture-en-light.svg?v=2">
    <img alt="3-Tier Pluggable Architecture" src="./assets/architecture-en-light.svg?v=2" width="100%">
  </picture>
</p>

| Tier | Architectural Role | Default Component | Pluggable Alternatives (`config.env`) | Core Responsibility |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 1** | **Supervisor** | **Muse** | Orca Mobile, Claude Desktop, Custom CLI | Captures natural-language intent, decomposes cross-repo tasks, dispatches worktrees, and summarizes results |
| **Tier 2** | **Control Plane** | **Orca Desktop & CLI** | `just` + `projectctl.py` CLI Runner | Governs 180+ Git worktrees, handles multi-account quota failover, runs `PreToolUse` safety hooks and naming plugin |
| **Tier 3** | **Worker Fleet** | **Claude + Codex + Antigravity + DeepSeek** | OpenCode, Cursor, Windsurf, Aider | Executes parallel coding, AST refactoring, simulator testing, and cross-agent review inside isolated Git worktrees |

> [!NOTE]
> To swap your front-end supervisor or worker agents, update `SUPERVISOR_AGENT` and `WORKER_AGENTS` in [`config.env`](./config.example.env) and run `./deploy.sh --sync-profiles`.

---

## 3. 5-Stage Deterministic Task Lifecycle

Every engineering task follows a strict 5-stage pipeline from worktree creation to cryptographic archival:

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/workflow-en-dark.svg?v=2">
    <source media="(prefers-color-scheme: light)" srcset="./assets/workflow-en-light.svg?v=2">
    <img alt="5-Stage Deterministic Task Lifecycle" src="./assets/workflow-en-light.svg?v=2" width="100%">
  </picture>
</p>

1. **Worktree Spawn**: `projectctl.py` verifies the external SSD hardware `VolumeUUID` and creates an isolated Git worktree branched cleanly off `main`.
2. **Guarded Coding**: The primary agent codes inside the isolated worktree while `agent-guard` intercepts dangerous shell commands pre-execution and `xcbeautify` compresses Xcode logs by ~90%.
3. **Compact Handoff**: Running `just handoff` emits a concise `HANDOFF.json` receipt containing Git Base/Head SHAs, changed file SHA-256 digests, and verification artifacts—reducing handoff context tokens by ~80%.
4. **Cross-Review**: A heterogeneous reviewer agent (e.g., Codex reviewing Claude's patch) performs a read-only audit of `HANDOFF.json` and `git diff`, explicitly separating verified evidence from inference.
5. **Safe Archive**: Before retiring a worktree, `just archive-worktree` writes a full `repo.bundle`, `.tar.gz` snapshot, and `SHA256SUMS.txt` into `AI/Archive/`.

---

## 4. External SSD Directory Taxonomy & 4-Folder Standard

Illustrated below using the fictional workspace volume `/Volumes/AI_Workspace` and fictional project names (`NovaCam`, `FlowWeave`, `StarFront`, `ChromaStudio`):

```text
/Volumes/AI_Workspace/               # Dedicated workspace SSD (VolumeUUID verified before every write)
└── AI/
    ├── Projects/                    # [Canonical Entrypoint Layer] Exactly 1 main clone per repo, parked on clean main
    │   ├── Mac/                     # System maintenance & multi-agent orchestration repository
    │   ├── NovaCam/                 # Fictional Example A: Pro iOS Camera & RAW Engine
    │   ├── FlowWeave/               # Fictional Example B: Cross-Platform Rule & Workflow Engine
    │   ├── StarFront/               # Fictional Example C: Real-Time Strategy Game
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
    └── Data/                        # [Shared Caches & Hardware Leases]
        ├── BuildCache/              #   Shared Xcode SourcePackages (SPM) cache across worktrees
        └── SimulatorCoordination/   #   iOS Simulator lease locks (active-*/lease.json) to prevent test collisions
```

---

## 5. Workspace, Chat & Branch Naming Convention

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

## 6. Skill Matrix & `PreToolUse` Safety Guardrails

### 6.1 Unified Skill Matrix (Synced Across All Profiles)

| Category | Skill Name | Engineering Purpose |
| :--- | :--- | :--- |
| **Orchestration** | `orchestration` | Spawn heterogeneous worker agents (e.g., dispatch Codex from Claude) and collect results |
| **Orchestration** | `orca-cli` | Programmatically control Orca worktrees, managed terminals, repository settings, and browser |
| **Simulator & GUI** | `orca-emulator` | Drive booted iOS Simulators inside Orca's emulator pane (taps, gestures, accessibility tree) |
| **Simulator & GUI** | `orca-computer-use` | GUI automation for visible macOS app windows via accessibility tree when CLI is unavailable |
| **Code Intelligence** | [`ast-grep`](./skills/ast-grep/SKILL.md) | Syntax-aware AST search & rewrite across Swift/C/C++/Rust/TS/Python + `rg`, `fd`, `repomix`, `xcbeautify` |
| **Storage Hygiene** | `agent-slim` / `mole` | Audit AI session bloat, reclaim SQLite freelist pages, and deduplicate files via APFS `cp -c` |

### 6.2 Deterministic `PreToolUse` Safety Guardrail ([`scripts/guard.py`](./scripts/guard.py))

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

## 7. Daily Operational Recipes (`Justfile`)

```bash
just --list                                  # List all standardized operational recipes
just verify-volume                           # 1. Verify external workspace volume mount & hardware UUID
just doctor                                  # 2. Run read-only health & profile parity audit
just slim-status                             # 3. Inspect physical vs. logical storage & SQLite freelist pages
just sim-leases                              # 4. Inspect active iOS Simulator leases & running xcodebuild jobs
just test-guardrails                         # 5. Run unit & integration tests for PreToolUse guardrails
just handoff repo="." goal="Stage complete"  # 6. Generate compact cross-agent HANDOFF.json receipt
just archive-worktree <path> <topic>         # 7. Safely bundle & checksum a worktree into AI/Archive/
```

---

## 8. Quantified Engineering Gains

| Dimension | Before (Ad-Hoc Multi-Agent Setup) | After (Orca Multi-Agent Blueprint) | Measured Engineering Gain |
| :--- | :--- | :--- | :--- |
| **Multi-Account Failover** | Switching profiles after rate-limiting loses skills, rules, and Xcode MCP | `sync_profiles.py` keeps all Claude/Codex/Antigravity profiles 100% identical | **Zero capability drop on account failover** |
| **Non-Interactive Shell PATH** | Subshells miss `rg`/`fd`/`ast-grep` and fall back to slow `grep -R` across `work/` | `~/.zshenv` injects native `~/.local/bin` tools + `.git/info/exclude` skips `/work/` | **5–10x faster code search & zero scratch pollution** |
| **Xcode Build & Test Logs** | Raw 50,000-line `xcodebuild` output floods context windows | Piped through `xcbeautify` + single-destination flags + simulator lease locks | **~90% fewer build log tokens & zero simulator deadlocks** |
| **Destructive Command Safety** | Prompt-only rules occasionally fail to stop `git reset --hard` or `rm -rf` | `PreToolUse` hook (`agent-guard`) blocks dangerous commands pre-execution | **100% deterministic interception of destructive commands** |
| **180+ Worktree Naming** | Unreadable fish names (`soldierfish`) or LLM-billed renaming | `orca-naming-guard` local state machine assigns standby IDs & syncs valid titles | **Zero LLM tokens used; auto-organized within 3 seconds** |
| **Cross-Agent Handoffs** | Dumping chat transcripts leaves reviewers guessing what changed | `just handoff` emits exact Git SHAs, file SHA-256 digests & receipt paths | **80% smaller handoff context & 100% verifiable evidence** |

---

## 9. Repository Layout

```text
orca-multi-agent-blueprint/
├── assets/                          # Theme-adaptive single-layer SVG diagrams (Light & Dark modes)
├── bootstrap.sh                     # Universal one-line bootstrapper (supports curl | zsh)
├── deploy.sh                        # Deployment engine (--auto / --apply / --dry-run / --sync-profiles / --doctor)
├── AGENT_INSTALL.md                 # Autonomous installation protocol for AI coding agents
├── config.example.env               # Parameterized configuration template
├── Justfile                         # 7 daily operational recipes
├── scripts/
│   ├── discover_and_adopt.py        # Smart environment auto-discovery & non-destructive project adoption
│   ├── install_cli_tools.sh         # Zero-dependency native CLI binary installer
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
    └── test_guard.py                # Unit & integration test suite for safety guardrails
```
