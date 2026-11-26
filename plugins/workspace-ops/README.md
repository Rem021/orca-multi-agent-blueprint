# Workspace Ops Radar (Orca Plugin)

An official Orca `trusted-worker` + HTML sidebar dashboard plugin bundled inside `orca-multi-agent-blueprint`. It unifies real-time multi-agent workspace telemetry, reversible TTL cleanup visibility, storage top offenders, Orca Bots (`Ram`) alert feedback, and Blueprint health checks in one panel.

## What it shows & does

1. **Overview & Countdowns (`总览`)**
   - Live countdowns for both **`agent-governor` GC** (reads `~/.orca/backup-config.env` + `Codex Storage/status/.agent-gc.stamp`, default 4h interval) and **Orca Workspace Cleanup** (hourly worktree evaluation).
   - Workspace APFS disk usage gauge + 24h / 7d reclamation metrics + **7-day SVG bar trend chart** parsed from `agent-hygiene.log`.
   - One-click **Preview / Daily / Session-end / Deep GC** (all strictly enforced by `agent-governor`'s 5 safety gates, moving to `~/.Trash` with restore receipts).
   - Live status bar indicator (`Ops · 3h 18m` / warning / critical badge).
2. **Cleanup History & Undo (`清理记录`)**
   - Parses `~/.Trash/agent-governor-*/**/manifest.json` batches, highlighting total files, category breakdown, and the **largest cleaned file/directory** per batch.
   - **Preview Restore** and **Two-step Undo / Restore** (`agent-governor restore`).
   - Orca built-in worktree cleanup run history and rule preview.
3. **Storage Top Offenders & Triage (`空间大户`)**
   - Ranks largest folders across grandfathered `work/*` (`agent-governor triage`), stranded worktree `work/` trees, worktree sizes (`orca workspace cleanup preview`), reclaimable GC candidates, `AI/Data`, and `~/Library/Developer/Xcode/DerivedData`.
   - Search & category filter chips, one-click **`.keep` pin / unpin** protection, and direct **Open Workspace** jump.
4. **Bots & Live Monitoring (`Bots & 实时`)**
   - Displays Orca Bots (e.g. `Ram`) status, recent chats, and dispatch history.
   - **Alert Engine + Bot Feedback Loop**: automatically detects low disk, failed/stale backups, `doctor.py` attention items, agents stuck waiting/blocked, GC spikes, and leftover ephemeral simulators; supports desktop notifications, one-click **"Ask Ram to analyze & report back"** (`orca orchestration send --to bot:Ram --type handoff`), and optional deduplicated auto-handoff for critical alerts (6-hour cooldown per fingerprint).
   - Real-time workspace agent radar (`agent.status.changed` event-driven) + on-demand **`orca diagnostics memory`** CPU/RAM breakdown + simulator lease & QEMU process check.
5. **Health, Backups & Token Radar (`健康体检`)**
   - All 8 Blueprint LaunchAgent status cards (`agent-hygiene`, `backup-hot`, `backup-projects`, `backup-media`, `icloud-package`, `maintenance`, `restore-drill`, `volume-guard`).
   - `doctor.py` health summary and `agent-governor audit` **startup context token budget** (global rules sync, `SKILL.md` frontmatter tokens, MCP counts, flagged projects).

## Load into Orca (Local Development)

```bash
# 1. Generate Orca plugin typings (if not already present)
orca plugin types /path/to/orca-multi-agent-blueprint/plugins/workspace-ops

# 2. Inspect requested capabilities before approving
orca plugin inspect /path/to/orca-multi-agent-blueprint/plugins/workspace-ops --json

# 3. Load into Orca (Orca prints the exact --approve fingerprint on first load)
orca plugin dev load /path/to/orca-multi-agent-blueprint/plugins/workspace-ops

# 4. Open the sidebar panel
orca plugin open local.workspace-ops-radar --panel ops-radar
```

## Verify & Test

```bash
cd plugins/workspace-ops
orca plugin validate . --json
orca plugin test . --json
orca plugin check . --json
```
