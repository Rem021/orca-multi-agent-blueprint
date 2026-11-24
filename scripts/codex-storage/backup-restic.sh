#!/bin/zsh
# Restic backup runner (backup-restic.sh)
# Usage: backup-restic.sh hot | projects | media
# Works with both the external SSD and the internal disk storage mode.
umask 077
source "$(dirname "$0")/lib.sh"
P="${1:?profile}"
N="backup-$P"
take_lock "$N"

# Hours without a successful run before repeated skips are escalated to `stale`
case "$P" in
  hot) STALE_H="$STALE_ALERT_HOURS" ;;
  projects) STALE_H=$(( STALE_ALERT_HOURS * 3 )) ;;
  *) STALE_H=$(( STALE_ALERT_HOURS * 14 )) ;;
esac

[[ -x "$RESTIC" ]] || { skip_job "$N" "restic is not installed" "$STALE_H"; exit 0; }

# If using a local Restic repository (no NAS configured) and the target workspace volume is online,
# automatically initialize the Restic repository on first run.
if [[ -z "$NAS" && -z "$NAS_SMB_MATCH" && ! -f "$REPO/config" ]] && vol_mounted; then
  mkdir -p "$REPO" 2>/dev/null || true
  "$RESTIC" -r "$REPO" init >> "$LOGD/$N.log" 2>&1 || true
fi

nas_mounted || { skip_job "$N" "${NAS_REASON:-Backup target or NAS is not mounted}" "$STALE_H"; exit 0; }

SQLITE_HOT_STAGE="$HOME/.orca/sqlite-hot-stage"
cleanup_hot_stage() {
  [[ "$P" == hot && -d "$SQLITE_HOT_STAGE" ]] && rm -rf -- "$SQLITE_HOT_STAGE" 2>/dev/null || true
}
_EXIT_HOOKS+=(cleanup_hot_stage)

case "$P" in
  hot)
    # Consistent point-in-time copies of live SQLite databases (see sqlite_snapshot.py)
    snapshot_agent_databases "$SQLITE_HOT_STAGE"

    CANDIDATES=(
      "$HOME/.claude"
      "$HOME/.claude.json"
      "$HOME/.codex"
      "$HOME/.gemini"
      "$HOME/.agents"
      "$HOME/.orca"
      "$HOME/.ssh"
      "$HOME/.gitconfig"
      "$HOME/.zshrc"
      "$HOME/.zshenv"
      "$HOME/.zprofile"
      "$HOME/.zsh_history"
      "$HOME/Developer"
      "$HOME/Library/LaunchAgents"
      "$HOME/Library/Developer/Xcode/UserData"
      "$HOME/Library/Application Support/orca"
      "$HOME/Library/Application Support/Claude"
      "$HOME/Library/Application Support/Codex"
      "$HOME/Library/Application Support/Codex Storage/agents"
      "$HOME/Library/Application Support/Codex Storage/bin"
      "$HOME/Library/Application Support/Antigravity"
    )
    # Your own additions: EXTRA_MIGRATION_PATHS (also in the iCloud package) and EXTRA_RESTIC_PATHS
    extra_paths "$EXTRA_MIGRATION_PATHS" "$EXTRA_RESTIC_PATHS"
    for cleaned in "${reply[@]}"; do
      CANDIDATES+=("$HOME/$cleaned")
    done
    typeset -U CANDIDATES
    PATHS=()
    for c in "${CANDIDATES[@]}"; do
      [[ -e "$c" || -L "$c" ]] && PATHS+=("$c")
    done
    EXC=(
      --exclude "$HOME/Library/Group Containers"
      --exclude "$HOME/.codex/cache"
      --exclude "$HOME/.codex/.tmp"
      --exclude "$HOME/.codex/tmp"
      --exclude "$HOME/.codex/node_repl"
      --exclude "$HOME/.codex/worktrees"
      --exclude "$HOME/.claude/shell-snapshots"
      --exclude "$HOME/.claude/debug"
      --exclude "$HOME/Library/Application Support/Claude/vm_bundles"
      --exclude "$HOME/Library/Application Support/Claude/claude-code-vm"
      --exclude "$HOME/Library/Application Support/Claude/Cache"
      --exclude "$HOME/Library/Application Support/Claude/Code Cache"
      --exclude "$HOME/Library/Application Support/Claude/GPUCache"
      --exclude "$HOME/Library/Application Support/orca/Cache"
      --exclude "$HOME/Library/Application Support/orca/Code Cache"
      --exclude "$HOME/Library/Application Support/orca/GPUCache"
      --exclude "$HOME/Library/Application Support/orca/DawnGraphiteCache"
      --exclude "$HOME/Library/Application Support/orca/DawnWebGPUCache"
      --exclude "$HOME/Library/Application Support/orca/Crashpad"
      --exclude "$HOME/Library/Application Support/orca/acp-temp"
      --exclude "$HOME/Library/Application Support/orca/Singleton*"
      --exclude "$HOME/Library/Application Support/orca/orcad.lock"
      --exclude "node_modules"
      --exclude "DerivedData"
      --exclude ".DS_Store"
    )
    ;;
  projects)
    vol_mounted || { skip_job "$N" "Workspace volume is not ready ($VOL)" "$STALE_H"; exit 0; }
    PATHS=("$VOL/AI")
    EXC=(
      --exclude node_modules
      --exclude .venv
      --exclude venv
      --exclude __pycache__
      --exclude .mypy_cache
      --exclude .pytest_cache
      --exclude DerivedData
      --exclude .next
      --exclude .build
      --exclude "$VOL/AI/Data/BuildCache"
      --exclude .DS_Store
      --exclude "$VOL/AI/Codex/Worktrees"
      --exclude "$VOL/AI/Claude/Worktrees"
      --exclude "$VOL/AI/Orca/Worktrees"
      --exclude "$VOL/AI/Backups/restic"
      --exclude "$VOL/AI/Migration"
    )
    ;;
  media)
    vol_mounted || { skip_job "$N" "Workspace volume is not ready ($VOL)" "$STALE_H"; exit 0; }
    [[ -d "$MEDIA_PATH" ]] || { status_write "$N" skipped "Media folder is not configured or missing ($MEDIA_PATH)"; exit 0; }
    PATHS=("$MEDIA_PATH")
    EXC=(--exclude "Render Files" --exclude "Transcoded Media" --exclude "Analysis Files" --exclude .DS_Store --limit-upload 45000)
    ;;
  *)
    echo "unknown profile: $P"
    exit 2
    ;;
esac

status_write "$N" running "Running the restic backup ($P)"
log "$N" "start"
t0=$(date +%s)
OUT=$(caffeinate -i "$RESTIC" -r "$REPO" --retry-lock "$RESTIC_RETRY_LOCK" backup --tag "$P" --host "$HOSTTAG" --exclude-caches --no-scan "${EXC[@]}" "${PATHS[@]}" 2>&1)
rc=$?
echo "$OUT" >> "$LOGD/$N.log"
dt=$(( $(date +%s) - t0 ))
summary=$(echo "$OUT" | grep -E "^(Files:|Added to the repo|processed)" | tr '\n' ' ')
if [[ $rc -eq 0 && $SQLITE_SNAPSHOT_FAILURES -gt 0 ]]; then
  status_write "$N" warn "$SQLITE_SNAPSHOT_FAILURES database(s) have no consistent snapshot: $summary"
elif [[ $rc -eq 0 ]]; then
  status_write "$N" ok "$summary" "{\"seconds\":$dt}"
elif [[ $rc -eq 3 ]]; then
  errors=$(print -r -- "$OUT" | grep -c '^error:' || true)
  protected=$(print -r -- "$OUT" | grep -Ec '^error: can not obtain extended attribute com\.apple\.security\.private\.scoped-bookmark-(key|token) for .*: xattr\.get .*operation not permitted$' || true)
  if [[ "$errors" -gt 0 && "$errors" == "$protected" ]] && print -r -- "$OUT" | grep -Eq '^snapshot [0-9a-f]+ saved$'; then
    status_write "$N" warn "Snapshot saved; $protected protected macOS extended attribute(s) were not backed up (rc=3, see the log)" "{\"seconds\":$dt,\"protected_xattr_warnings\":$protected}"
  else
    status_write "$N" warn "Snapshot saved, but some files or attributes could not be read (rc=3): $summary" "{\"seconds\":$dt}"
  fi
else
  status_write "$N" fail "restic rc=$rc: $(echo "$OUT" | tail -2 | tr '\n' ' ')" "{\"seconds\":$dt}"
  notify "Backup failed: $P" "restic rc=$rc, see logs/$N.log"
fi
log "$N" "done rc=$rc in ${dt}s"
exit "$rc"
