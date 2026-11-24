# Orca Multi-Agent Blueprint — everyday recipes
# Run `just` in this folder to list them. Every recipe is a thin wrapper around deploy.sh or scripts/.

set shell := ["zsh", "-cu"]

blueprint_dir := justfile_directory()
backup_agents := env_var("HOME") + "/Library/Application Support/Codex Storage/agents"
# Settings come from the environment, then ./config.env, then ~/.orca/backup-config.env.
# Both files are sourced by zsh (not parsed as dotenv) so values mean the same as in deploy.sh.
storage_mode := env_var_or_default("STORAGE_MODE", `zsh -c 'for f in "$HOME/.orca/backup-config.env" ./config.env; do [[ -f "$f" ]] && source "$f"; done >/dev/null 2>&1; print -r -- "${STORAGE_MODE:-auto}"'`)
volume_path := if storage_mode == "internal" {
    env_var_or_default("INTERNAL_WORKSPACE_PATH", `zsh -c 'for f in "$HOME/.orca/backup-config.env" ./config.env; do [[ -f "$f" ]] && source "$f"; done >/dev/null 2>&1; print -r -- "${INTERNAL_WORKSPACE_PATH:-$HOME/AI-Workspace}"'`)
} else {
    env_var_or_default("AI_VOLUME_PATH", `zsh -c 'for f in "$HOME/.orca/backup-config.env" ./config.env; do [[ -f "$f" ]] && source "$f"; done >/dev/null 2>&1; print -r -- "${AI_VOLUME_PATH:-$HOME/AI-Workspace}"'`)
}
expected_uuid := if storage_mode == "internal" {
    "LOCAL"
} else {
    env_var_or_default("AI_VOLUME_UUID", `zsh -c 'for f in "$HOME/.orca/backup-config.env" ./config.env; do [[ -f "$f" ]] && source "$f"; done >/dev/null 2>&1; print -r -- "${AI_VOLUME_UUID:-AUTO}"'`)
}

# List all recipes
default:
    @just --list --unsorted

# ───────────── Health and tests ─────────────

# Check that the workspace drive is mounted and has the configured volume UUID
[group('health')]
verify-volume:
    #!/usr/bin/env zsh
    set -euo pipefail
    vol="{{volume_path}}"
    exp_uuid="{{expected_uuid}}"
    if [[ -z "$vol" ]]; then
        echo "❌ No workspace path configured (run ./deploy.sh --auto first)" >&2
        exit 1
    fi
    if [[ "{{storage_mode}}" == "internal" || "$vol" != /Volumes/* ]]; then
        echo "✅ Internal-disk workspace $vol"
        exit 0
    fi
    actual_uuid="$(/usr/sbin/diskutil info -plist "$vol" 2>/dev/null | /usr/bin/plutil -extract VolumeUUID raw - 2>/dev/null || true)"
    if [[ -z "$actual_uuid" ]]; then
        echo "❌ External workspace drive $vol is not mounted" >&2
        exit 1
    fi
    case "$exp_uuid" in
        ""|AUTO|LOCAL|INTERNAL) ;;
        "$actual_uuid") ;;
        *) echo "❌ Volume UUID mismatch: expected $exp_uuid, found $actual_uuid" >&2; exit 1 ;;
    esac
    echo "✅ External workspace drive $vol is mounted (UUID $actual_uuid)"

# Read-only health check, full JSON (summary.attention lists what needs action)
[group('health')]
doctor: verify-volume
    "{{blueprint_dir}}/deploy.sh" --doctor

# Read-only health check, verdict only
[group('health')]
doctor-summary:
    python3 "{{blueprint_dir}}/scripts/doctor.py" --summary

# Run every test suite
[group('health')]
test:
    cd "{{blueprint_dir}}" && python3 -B -m unittest discover -s tests

# Test only the command guardrail (guard.py)
[group('health')]
test-guardrails:
    python3 "{{blueprint_dir}}/tests/test_guard.py"

# Preview a full deployment without writing anything
[group('health')]
dry-run:
    "{{blueprint_dir}}/deploy.sh" --dry-run

# ───────────── Backups ─────────────

# Install or update the scheduled backup and hygiene LaunchAgents
[group('backups')]
setup-backups: verify-volume
    "{{blueprint_dir}}/deploy.sh" --setup-backups

# Show the last result of every backup job
[group('backups')]
backup-status:
    "{{blueprint_dir}}/deploy.sh" --backup-status

# Build and upload the encrypted iCloud package now
[group('backups')]
backup-now-icloud:
    zsh "{{backup_agents}}/icloud-package.sh"

# Preview iCloud package retention (deletes nothing)
[group('backups')]
prune-icloud-dry-run *args='':
    zsh "{{backup_agents}}/icloud-package.sh" --prune-only --dry-run {{args}}

# Apply iCloud package retention now (keeps each Mac's newest confirmed package)
[group('backups')]
prune-icloud *args='':
    zsh "{{backup_agents}}/icloud-package.sh" --prune-only --apply {{args}}

# Run the hourly Restic backup of agent state now
[group('backups')]
backup-now-hot:
    zsh "{{backup_agents}}/backup-restic.sh" hot

# ───────────── Moving to a new Mac ─────────────

# Preview what --pack-to-ssd would write to the drive
[group('migration')]
pack-dry-run: verify-volume
    "{{blueprint_dir}}/deploy.sh" --pack-dry-run

# Old Mac: pack agent state, databases, keys and local repos onto the drive (no VMs)
[group('migration')]
pack-to-ssd: verify-volume
    "{{blueprint_dir}}/deploy.sh" --pack-to-ssd

# Preview what a restore would write to this Mac
[group('migration')]
restore-dry-run: verify-volume
    "{{blueprint_dir}}/deploy.sh" --restore-dry-run

# New Mac: restore from the drive, then deploy (quit Orca, Claude, Codex and other agent apps first)
[group('migration')]
restore-from-ssd: verify-volume
    "{{blueprint_dir}}/deploy.sh" --restore-from-ssd

# Origin, age and integrity of the snapshot on the drive
[group('migration')]
migration-status: verify-volume
    "{{blueprint_dir}}/deploy.sh" --migration-status

# Rewrite the restore guide and launchers on the drive without packing again
[group('migration')]
refresh-guide: verify-volume
    "{{blueprint_dir}}/deploy.sh" --refresh-guide

# List third-party LaunchAgents and their state
[group('migration')]
agents:
    "{{blueprint_dir}}/deploy.sh" --agents

# Stop third-party LaunchAgents (comma-separated labels; they stay off after the next login)
[group('migration')]
hold-agents $labels:
    "{{blueprint_dir}}/deploy.sh" --hold-agents "$labels"

# Enable and load third-party LaunchAgents (comma-separated labels)
[group('migration')]
enable-agents $labels:
    "{{blueprint_dir}}/deploy.sh" --agents --enable-agents "$labels"

# ───────────── Storage mode ─────────────

# Deploy with the workspace on an external drive (/Volumes/<drive>/AI)
[group('storage')]
use-external-storage:
    "{{blueprint_dir}}/deploy.sh" --auto --external

# Deploy with the workspace on the internal disk (~/AI-Workspace/AI); existing projects are not moved
[group('storage')]
use-internal-storage:
    "{{blueprint_dir}}/deploy.sh" --auto --internal

# ───────────── Daily work ─────────────

# Install the guardrail's pre-push hook into a repository
[group('daily')]
install-pre-push $repo:
    "{{blueprint_dir}}/deploy.sh" --install-pre-push "$repo"

# Audit rule parity, context budgets and project layout (read-only)
[group('daily')]
governor-audit:
    python3 "{{blueprint_dir}}/scripts/agent_governor.py" audit

# Preview expired scratch/test/cache cleanup (add --apply to move items to the Trash)
[group('daily')]
govern-gc *args='':
    python3 "{{blueprint_dir}}/scripts/agent_governor.py" gc {{args}}

# Read-only size and age report for existing work/ folders
[group('daily')]
govern-triage *args='':
    python3 "{{blueprint_dir}}/scripts/agent_governor.py" triage {{args}}

# Show iOS simulator leases and running xcodebuild / simctl processes
[group('daily')]
sim-leases: verify-volume
    #!/usr/bin/env zsh
    set -euo pipefail
    dir="{{volume_path}}/AI/Data/SimulatorCoordination"
    echo "=== Active leases ($dir) ==="
    found=0
    for f in "$dir"/active-*/lease.json(N); do
        found=1
        echo "--- $f ---"
        cat "$f"
        echo
    done
    (( found )) || echo "none"
    echo "=== Running xcodebuild / simctl ==="
    /usr/bin/pgrep -fl 'xcodebuild|simctl' || echo "none"

# Archive a worktree to <workspace>/AI/Archive/<date>-<topic> (bundle + tar.gz + SHA256; the worktree is kept)
[group('daily')]
archive-worktree $worktree_path $topic: verify-volume
    #!/usr/bin/env zsh
    set -euo pipefail
    wt="$(cd "$worktree_path" && pwd)"
    if [[ -z "$topic" || "$topic" == */* || "$topic" == .* || "$topic" == *[[:cntrl:]]* ]]; then
        echo "❌ The topic must not be empty, contain / or start with ." >&2
        exit 1
    fi
    dest="{{volume_path}}/AI/Archive/$(date +%Y-%m-%d)-${topic}"
    if [[ -e "$dest" ]]; then
        echo "❌ $dest already exists; pick another topic so no archive is overwritten" >&2
        exit 1
    fi
    mkdir -p "$dest"
    if git -C "$wt" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "$wt" bundle create "$dest/repo.bundle" --all
        git -C "$wt" bundle verify "$dest/repo.bundle" >/dev/null
        git -C "$wt" status --porcelain=v1 --branch > "$dest/git-status.txt"
    fi
    tar -czf "$dest/worktree-snapshot.tar.gz" -C "${wt:h}" "${wt:t}"
    (cd "$dest" && shasum -a 256 -- *(.) > SHA256SUMS.txt)
    echo "✅ Archived to $dest"
    ls -lh "$dest"
