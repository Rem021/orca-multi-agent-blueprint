# Tier-1 Supervisor Playbook (Muse / Pluggable Front-End Commander)

This playbook defines how the **Tier-1 Supervisor** (default: **Muse** on iPhone/Mac; swappable with **Orca Mobile**, **Claude Desktop**, **OpenClaw**, or a custom webhook/cron agent) coordinates **Orca IDE (Tier-2 Control Plane)** and the **Worker Fleet (Tier-3 Agents)** without creating conflicting schedulers.

---

## 1. Pluggable Role Separation

| Tier | Default Component | Drop-In Replacements | Core Responsibility |
| :--- | :--- | :--- | :--- |
| **Tier 1: Front-End Supervisor** | **Muse** (iPhone + Mac via Tailscale / File & Orca CLI channel) | Orca Mobile, Claude Desktop, OpenClaw, Telegram/Slack Agent | Natural-language intake, hourly patrol (`orca-patrol`), cross-project status synthesis, morning digest, quota-failover approval |
| **Tier 2: Local Control Plane** | **Orca IDE (`1.4.250+`)** | Native `tmux` + `git worktree` + `just` (fallback mode) | Single source of truth for Git worktrees, PTY terminals, native chats, Agent Hooks, and Run/Task/Dispatch DAGs |
| **Tier 3: Worker & Reviewer Fleet** | **Claude Code**, **OpenAI Codex**, **Google Antigravity (Gemini)**, **DeepSeek Harness (`dsh`)** | OpenCode, Cursor CLI, Aider, Kiro | Scoped implementation in isolated worktrees + independent cross-model code/hash verification |

---

## 2. Core Operating Principles for the Supervisor

### A. Single Control Plane (No "Double Scheduler" Collisions)
- **Orca** owns local terminal sessions, worktree lifecycle, and the `orchestration` mailbox (`orca orchestration ...`).
- External scripts and the Tier-1 Supervisor **must never** blindly consume `orca orchestration check` mailboxes belonging to an active Coordinator session, nor send blind timer-based `"continue"` messages into a running terminal.
- Instead, the Supervisor reads **non-destructive, timestamped JSON snapshots**:
  - `python3 scripts/doctor.py` (runtime status, worktree count, hook health, profile parity, simulator leases);
  - `python3 scripts/handoff.py` (per-task git SHA, changed file SHA-256 digests, verification receipts);
  - Project `reports/<YYYY-MM-DD-topic>/README.md`.

### B. Hourly Patrol Loop (`orca-patrol`)
1. **Channel & Volume Self-Check**:
   - Verify `orca status --json` is `ready` and the external volume (`AI_VOLUME_PATH`) matches `AI_VOLUME_UUID`.
   - If the remote relay (`ts-relay`) disconnected, restart the relay before inspecting workspaces; never treat an unreachable relay as worker failure.
2. **Worktree & Receipt Delta Check**:
   - Compare active `working` / `waiting` / `blocked` sessions against the previous patrol checkpoint.
   - Verify completed tasks by reading their `reports/<YYYY-MM-DD-topic>/` receipt and `HANDOFF.json` SHA-256 hashes.
3. **Decision Gate Escalation**:
   - Routine reversible progress continues automatically.
   - Irreversible actions (`main` branch merge, production release, paid resource allocation, architecture changes, or deleting unarchived data) are queued into the user's approval digest.

### C. Quota Exhaustion & Multi-Account Failover Protocol
When a Worker or Coordinator hits a model rate limit or quota ceiling:
1. **Freeze & Record**: Capture the current worktree path, `HEAD` SHA, uncommitted `git status`, and completed test receipts via `scripts/handoff.py`.
2. **Verify Old Worker Termination**: Confirm via two independent signals (Orca session state + process table) that the previous worker is no longer writing to the worktree, and increment the task fencing token.
3. **Seamless Profile Switch**: Because `scripts/sync_profiles.py` keeps all Orca-managed Claude Profiles and Codex Accounts 100% aligned (`AGENTS.md`, `CLAUDE.md`, `hooks.json`, `xcode` MCP, and `skills/`), the backup account or alternate model family (e.g., Claude -> Codex or Antigravity -> DeepSeek) resumes directly from the `HANDOFF.json` packet without losing rules or tool access.
4. **Hard Retry Cap**: Maximum 2 retries per error class; never retry a quota-exhausted account on the same quota pool.

---

## 3. Copy-Paste Supervisor System Prompt (for Muse / Custom Commander)

```markdown
You are the Tier-1 Supervisor managing software engineering projects on the host Mac through Orca IDE.

1. Default to Chinese, lead with the conclusion, and strictly separate verified receipt data from unverified assumptions.
2. Treat Orca as the single local execution control plane:
   - Never send blind "continue" prompts to active worker terminals.
   - Read `doctor.py` and `HANDOFF.json` receipts to verify task progress, Git SHAs, and SHA-256 deliverable hashes.
3. Enforce workspace & storage redlines:
   - All projects live under `${AI_VOLUME_PATH}/AI/Projects/<Project>` (single main clone on `main`).
   - All parallel tasks run in isolated worktrees under `${AI_VOLUME_PATH}/AI/{Orca,Codex,Claude}/Worktrees/` using the display name format `ProjectName｜SpecificTopic`.
   - Never authorize `rm -rf`, `git reset --hard`, `git clean -fdx`, or emptying Trash. (Parallel and extra iOS Simulators are fine since 2026-11-13.)
4. For complex features or firmware/rendering changes, use the Hub-and-Spoke Cross-Review pattern:
   - Assign Agent A (e.g. Claude or Codex) to implement in an isolated worktree and produce a `HANDOFF.json` receipt.
   - Assign Agent B (a different model family, e.g. Codex, Antigravity/Gemini, or DeepSeek) to independently audit the diff, verify SHA-256 hashes, and run counterexample tests before marking the task ready for merge.
```
