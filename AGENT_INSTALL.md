# Autonomous AI Agent Installation Guide (`AGENT_INSTALL.md`)

> **If a user gave you (Claude Code, OpenAI Codex, Google Antigravity, DeepSeek, Cursor, or Muse) the link to `https://github.com/Rem021/orca-multi-agent-blueprint` and asked you to deploy or optimize their Mac environment, follow the exact steps below.**

---

## Autonomous Execution Protocol (For AI Agents)

### Step 1: Run the One-Line Bootstrap in Preview Mode (`--dry-run`)
First, run the bootstrap script in `--dry-run` mode to inspect the host Mac's storage volumes, existing projects, iOS simulators, and installed agent profiles without mutating any files:

```bash
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --dry-run
```

### Step 2: Execute Full Zero-Config Auto-Deployment (`--auto`)
Once `--dry-run` succeeds (or if the user asked for immediate deployment), run:

```bash
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh
```

What this command automatically accomplishes on **both brand-new Macs and existing multi-project/multi-agent Macs**:
1. **Storage & Volume Detection**:
   - Detects any external AI SSD under `/Volumes/*` (and extracts its hardware `VolumeUUID` via `diskutil`), or falls back cleanly to `~/AI-Workspace` on a single-disk Mac.
   - Initializes the directory taxonomy (`AI/Projects`, `AI/{Orca,Codex,Claude}/Worktrees`, `AI/Archive`, `AI/Data/BuildCache/SourcePackages`, `AI/Data/SimulatorCoordination`).
2. **Non-Destructive Adoption of Existing Projects**:
   - Scans existing projects in `${AI_VOLUME_PATH}/AI/Projects/*` and repositories registered in Orca (`orca repo list --json`).
   - Ensures each project under `AI/Projects/` has `docs/`, `reports/`, `outputs/`, and `work/` directories without altering any existing code.
   - Appends `/work/` to `.git/info/exclude` so temporary scratch files never pollute Git status.
3. **Zero-Dependency Native CLI Toolchain**:
   - Installs `rg` (ripgrep), `fd`, `ast-grep` (`sg`), `just`, `xcbeautify`, `scc`, and `repomix` into `~/.local/bin` (auto-detecting `arm64` vs `x86_64`, without requiring Homebrew or `gh` login).
   - Configures `~/.zshenv` so non-interactive agent subshells always include `~/.local/bin` in `PATH`.
   - Enables Git large-repo and multi-worktree acceleration (`feature.manyFiles`, `core.fsmonitor`, `rerere.enabled`, `worktree.useRelativePaths`, `branch.sort`).
4. **Multi-Agent & Multi-Account Failover Alignment**:
   - Installs Orca's built-in skills (`orca-cli`, `orca-computer-use`, `orca-emulator`, `orchestration`) and the bundled `ast-grep` skill.
   - Synchronizes `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `hooks.json`, `xcode` MCP (`/usr/bin/xcrun mcpbridge` with `120s/600s` timeouts), and skill symlinks across **every** Orca-managed Claude Profile, Codex Account, and Antigravity Chat directory.
5. **Deterministic `PreToolUse` Safety Guardrails**:
   - Installs `~/.local/bin/agent-guard` (`scripts/guard.py`) and wires it into Claude, Codex, and Antigravity `PreToolUse` hooks to block destructive commands (`rm -rf` on projects/worktrees, `git reset --hard`, `git clean -fdx`, unflagged `git worktree prune`, `xcrun simctl create/clone`, and emptying Trash).

### Step 3: Verify Health & Report Back
Run the read-only health auditor and present the summary to the user:

```bash
python3 "${AI_VOLUME_PATH:-$HOME/AI-Workspace}/AI/Projects/Mac/outputs/orca-multi-agent-blueprint/scripts/doctor.py"
```

---

## Lossless Cross-Mac Hardware Migration Protocol (External SSD Only)

When migrating to a brand-new Mac using only the external SSD (without bringing the old Mac or copying bulky VMs like `claudevm.bundle` / `CoreSimulator/Devices`):

### 1. On the Old Mac (Before Unplugging the SSD)
Pack all internal-disk AI Agent states, online-checkpointed SQLite databases (`agent-session-journal.db`, `orchestration.db`, `thread_history_1.sqlite`, etc.), multi-account pools, SSH/Git/Zsh configs, `LaunchAgents`, Keychain secrets, and `~/Developer` local repos onto `${AI_VOLUME_PATH}/AI/Migration/latest`:

```bash
# Preview first
./deploy.sh --pack-dry-run

# Execute lossless pack to external SSD
./deploy.sh --pack-to-ssd
```

### 2. On the New Mac (Plug in the SSD & Run One Command)
Either run directly from the external SSD (100% offline-capable):

```bash
zsh /Volumes/<YOUR_SSD>/AI/Migration/restore-on-new-mac.sh
```

Or via the one-line GitHub bootstrapper:

```bash
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --restore-from-ssd
```
This restores all internal-disk agent states, cleans stale host locks, restores Keychain entries, fixes `~/.ssh` permissions, and automatically chains into `./deploy.sh --auto` + `doctor.py`.
