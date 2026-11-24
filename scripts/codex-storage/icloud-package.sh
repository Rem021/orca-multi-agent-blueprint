#!/bin/zsh
# Nightly encrypted compact backup to iCloud Drive (icloud-package.sh)
# Covers the agent configs, memories, account pools, SSH/Git/shell settings, Xcode user data,
# LaunchAgents and consistent SQLite snapshots. tar streams straight into OpenSSL (no plaintext
# archive on disk); the package is AES-256-CBC (PBKDF2-SHA256, 600000 iterations) with an
# HMAC-SHA256 trailer (see icloud_crypto.py), is verified before an atomic publish, and the local
# copy is evicted once iCloud has uploaded it.
umask 077
setopt PIPE_FAIL NULL_GLOB
source "$(dirname "$0")/lib.sh"
N=icloud-package
CRYPTO="$AG/icloud_crypto.py"

PRUNE_ARGS=(
  --icloud-dir "$ICLOUD"
  --storage-dir "$S"
  --host "$SAFE_HOST"
  --keep-latest "$ICLOUD_KEEP_LATEST"
  --max-age-days "$ICLOUD_MAX_AGE_DAYS"
  --scope "$ICLOUD_PRUNE_SCOPE"
  --stale-host-days "$ICLOUD_STALE_HOST_DAYS"
)

if [[ "${1:-}" == "--prune-only" ]]; then
  shift
  has_mode=0
  for a in "$@"; do
    [[ "$a" == "--dry-run" || "$a" == "--apply" ]] && has_mode=1
  done
  (( has_mode )) || PRUNE_ARGS+=(--apply)
  exec python3 "$AG/icloud_prune.py" "${PRUNE_ARGS[@]}" "$@"
fi

take_lock "$N"
status_write "$N" running "Building and verifying the encrypted agent package"
log "$N" "start verified-package"
WORKDIR=""
SQLITE_STAGE="$HOME/.orca/sqlite-backups"
WORK_PREFIX="$HOME/Library/Caches/icloud-package."

cleanup_package() {
  if [[ -n "$WORKDIR" && "$WORKDIR" == "$WORK_PREFIX"* ]]; then
    rm -rf -- "$WORKDIR" 2>/dev/null || true
  fi
  if [[ -d "$SQLITE_STAGE" ]]; then
    rm -rf -- "$SQLITE_STAGE" 2>/dev/null || true
  fi
}
_EXIT_HOOKS+=(cleanup_package)
trap 'status_write "$N" fail "Package job was interrupted; earlier packages are kept"; exit 130' INT TERM HUP
fail_package() { log "$N" "failed: $1"; status_write "$N" fail "$1"; exit 1; }

# Work folders of runs that were killed before their cleanup ran. Versions before the streaming
# pipeline left a plaintext archive.tar.gz in them.
for stale_dir in "$WORK_PREFIX"*(N/); do
  log "$N" "removing leftover work folder ${stale_dir:t}"
  rm -rf -- "$stale_dir" 2>/dev/null || true
done

free_kb=$(df -Pk "$HOME/Library/Caches" | awk 'END {print $4}')
[[ "$free_kb" == <-> && "$free_kb" -ge 8388608 ]] || fail_package "Less than 8 GiB free on the internal disk; earlier packages are kept and nothing was written"
if [[ "$ICLOUD" == "$ICLOUD_DRIVE"/* && ! -d "$ICLOUD_DRIVE" ]]; then
  fail_package "iCloud Drive is not enabled on this Mac ($ICLOUD_DRIVE is missing)"
fi
mkdir -p "$ICLOUD" || fail_package "The iCloud backup folder is not writable ($ICLOUD)"
WORKDIR=$(mktemp -d "${WORK_PREFIX}XXXXXX") || fail_package "Could not create a work folder"
ENC="$WORKDIR/package.tar.gz.enc"
ts=$(date +%Y%m%d-%H%M%S)
F="$ICLOUD/mac-agents-${SAFE_HOST}-${ts}.tar.gz.enc"
[[ ! -e "$F" ]] || fail_package "Target file already exists; refusing to overwrite it"

# Fail closed when the Keychain key is missing: a new random key would make it look as if backups
# work while the history can no longer be opened with the key the user saved
if ! kc_secret "$KC_ICLOUD_SVC" >/dev/null; then
  notify "iCloud backup failed" "Keychain key $KC_ICLOUD_SVC is missing; import the key of your previous Mac first"
  fail_package "Keychain has no encryption key $KC_ICLOUD_SVC (account=$KC_ACCOUNT); no new key was generated so existing packages stay readable"
fi

# 1. Consistent point-in-time copies of the agent databases (see sqlite_snapshot.py)
snapshot_agent_databases "$SQLITE_STAGE"
(( SQLITE_SNAPSHOT_FAILURES == 0 )) || fail_package "$SQLITE_SNAPSHOT_FAILURES database snapshot(s) failed; the previous package is kept"

# 2. Agent state paths relative to $HOME (VM images and multi-GB raw logs stay out of the package).
#    Add your own with EXTRA_MIGRATION_PATHS in config.env.
CANDIDATES=(
  ".claude"
  ".claude.json"
  ".codex/config.toml"
  ".codex/auth.json"
  ".codex/AGENTS.md"
  ".codex/memories"
  ".codex/memories_v2"
  ".codex/skills"
  ".codex/rules"
  ".codex/automations"
  ".codex/session_index.jsonl"
  ".agents"
  ".orca"
  ".gemini/GEMINI.md"
  ".gemini/settings.json"
  ".gemini/config"
  ".gemini/skills"
  ".gemini/antigravity/GEMINI.md"
  ".gemini/antigravity/brain"
  ".gemini/antigravity/conversations"
  ".ssh"
  ".gitconfig"
  ".zshrc"
  ".zshenv"
  ".zprofile"
  ".zsh_history"
  ".python_history"
  ".local/bin/agent-guard"
  "Library/LaunchAgents"
  "Library/Developer/Xcode/UserData"
  "Library/Application Support/Codex Storage/agents"
  "Library/Application Support/Codex Storage/bin"
  "Library/Application Support/Claude/claude_desktop_config.json"
  "Library/Application Support/Claude/config.json"
  "Library/Application Support/Claude/claude-code-sessions"
  "Library/Application Support/Claude/local-agent-mode-sessions"
  "Library/Application Support/orca/profiles"
  "Library/Application Support/orca/claude-profiles"
  "Library/Application Support/orca/claude-accounts"
  "Library/Application Support/orca/claude-runtime-auth"
  "Library/Application Support/orca/antigravity-accounts"
  "Library/Application Support/orca/antigravity-chat/config"
  "Library/Application Support/orca/antigravity-chat/antigravity-cli"
  "Library/Application Support/orca/antigravity-chat/GEMINI.md"
  "Library/Application Support/orca/antigravity-chat/antigravity-acp/GEMINI.md"
  "Library/Application Support/orca/antigravity-chat/antigravity-acp/settings.json"
  "Library/Application Support/orca/antigravity-chat/antigravity-acp/acp_token.json"
  "Library/Application Support/orca/plugins"
  "Library/Application Support/orca/plugins-data"
  "Library/Application Support/orca/agent-hooks"
  "Library/Application Support/orca/agent-session-authority.key"
  "Library/Application Support/orca/orca-e2ee-keypair.json"
  "Library/Application Support/orca/orca-devices.json"
  "Library/Application Support/orca/orca-profile-index.json"
  "Library/Application Support/orca/orca-relay-region-preference.json"
  "Library/Application Support/orca/codex-hook-trust.json"
  "Library/Application Support/orca/codex-pane-accounts.json"
)
extra_paths "$EXTRA_MIGRATION_PATHS"
CANDIDATES+=("${reply[@]}")

# Orca-managed Codex accounts: configs, memories and skills
for acct_home in "$HOME/Library/Application Support/orca/codex-accounts"/*/home; do
  rel_home="${acct_home#$HOME/}"
  for sub in config.toml auth.json AGENTS.md memories memories_v2 skills rules automations; do
    CANDIDATES+=("$rel_home/$sub")
  done
done

EXISTING_ITEMS=()
typeset -U CANDIDATES
for item in "${CANDIDATES[@]}"; do
  [[ -e "$HOME/$item" || -L "$HOME/$item" ]] && EXISTING_ITEMS+=("$item")
done

TAR_EXCLUDES=(
  --exclude ".claude/shell-snapshots"
  --exclude ".claude/debug"
  --exclude ".ssh/agent"
  --exclude "Library/Group Containers"
  --exclude "node_modules"
  --exclude ".venv"
  --exclude "venv"
  --exclude "__pycache__"
  --exclude "*.sock"
  --exclude "*.ipc"
  --exclude "Singleton*"
  --exclude "fsmonitor--daemon*"
  --exclude "*.fcpbundle"
  --exclude "Render Files"
  --exclude "Transcoded Media"
  --exclude "Analysis Files"
  --exclude "*.sqlite-wal"
  --exclude "*.sqlite-shm"
  --exclude "*.db-wal"
  --exclude "*.db-shm"
  --exclude ".DS_Store"
)

# 3. Archive -> encrypt -> MAC in one stream; the key reaches OpenSSL on a private pipe
( cd "$HOME" && COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs -czf - "${TAR_EXCLUDES[@]}" "${EXISTING_ITEMS[@]}" ) 2>>"$LOGD/$N.log" \
  | python3 "$CRYPTO" encrypt --out "$ENC" --password-fd 3 3< <(kc_secret "$KC_ICLOUD_SVC") 2>>"$LOGD/$N.log" \
  || fail_package "Archiving or encryption failed; no incomplete package was published"

# 4. Self-check: the MAC must verify, then the archive must decrypt and list completely
python3 "$CRYPTO" decrypt "$ENC" --require-mac --password-fd 3 3< <(kc_secret "$KC_ICLOUD_SVC") 2>>"$LOGD/$N.log" \
  | tar -tzf - >/dev/null 2>>"$LOGD/$N.log" \
  || fail_package "Package verification (MAC, decryption, archive listing) failed; nothing was published"
[[ "$(stat -f %d "$ENC")" == "$(stat -f %d "$ICLOUD")" ]] || fail_package "The work folder and iCloud Drive are on different file systems; not publishing"
mv -n "$ENC" "$F" || fail_package "Publishing failed"
[[ ! -e "$ENC" && -s "$F" ]] || fail_package "The package was not published completely"
sz=$(du -h "$F" | cut -f1)
digest=$(shasum -a 256 "$F" | awk '{print $1}')
log "$N" "package verified: ${F:t} sha256=$digest items=${#EXISTING_ITEMS[@]}"

# 5. Record the package in this host's ledgers, then prune older packages only once iCloud confirms
#    the upload (needs $S/bin/icloud_status). agent-hygiene.sh keeps polling hourly when the upload
#    takes longer than this job waits.
python3 "$AG/icloud_prune.py" "${PRUNE_ARGS[@]}" --record "${F:t}" 2>>"$LOGD/$N.log" \
  || log "$N" "could not record ${F:t} in the ledgers"

prune_own_packages() {
  local raw_json
  if [[ -f "$AG/icloud_prune.py" ]]; then
    raw_json="$(python3 "$AG/icloud_prune.py" --apply --json "${PRUNE_ARGS[@]}" 2>>"$LOGD/$N.log")" || true
    if [[ -n "$raw_json" ]]; then
      python3 - "$raw_json" <<'PYLOG' | while IFS= read -r line; do log "$N" "$line"; done
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
if d.get("listing_restricted"):
    print("note: macOS privacy protection blocked listing the iCloud folder; pruned by the publish ledgers only")
for item in d.get("pruned") or []:
    print(f"pruned superseded package: {item.get('name')} ({item.get('reason')}, {item.get('size_human')})")
for item in d.get("failed") or []:
    print(f"could not prune {item.get('name')} ({item.get('error')})")
for name in d.get("future_dated") or []:
    print(f"warning: {name} is dated more than a day in the future (wrong clock?); kept, delete it by hand")
PYLOG
      return 0
    fi
  fi
  log "$N" "pruner unavailable; earlier packages in iCloud are kept"
}

uploaded_ok() {
  [[ ! -x "$S/bin/icloud_status" ]] && return 1
  "$S/bin/icloud_status" "$1" 2>/dev/null | head -1 | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("isUploaded") is True and not d.get("hasConflicts") and not d.get("uploadingError") and not d.get("uploadError") and not d.get("errorCode") else 1)' 2>/dev/null
}

finalize_uploaded_package() {
  prune_own_packages
  if brctl evict "$F" >/dev/null 2>&1; then
    status_write "$N" ok "Verified and uploaded; local copy evicted (keeping the newest ${ICLOUD_KEEP_LATEST}): ${F:t} $sz"
  else
    status_write "$N" ok "Verified and uploaded; local copy kept (keeping the newest ${ICLOUD_KEEP_LATEST}): ${F:t} $sz"
  fi
  log "$N" "done uploaded=1; retained latest ${ICLOUD_KEEP_LATEST} package(s) scope=${ICLOUD_PRUNE_SCOPE}"
}

if [[ -x "$S/bin/icloud_status" ]]; then
  if uploaded_ok "$F"; then
    finalize_uploaded_package
  else
    status_write "$N" warn "Package verified locally; waiting for the iCloud upload before pruning older packages: ${F:t} ($sz)"
    POLL_ATTEMPTS="${ICLOUD_UPLOAD_POLL_ATTEMPTS:-80}"
    POLL_SECONDS="${ICLOUD_UPLOAD_POLL_SECONDS:-30}"
    watch_upload() {
      trap '' INT TERM HUP
      local up=0 i
      for i in $(seq 1 "$POLL_ATTEMPTS"); do
        if uploaded_ok "$F"; then up=1; break; fi
        sleep "$POLL_SECONDS"
      done
      if [[ "$up" == 1 ]]; then
        finalize_uploaded_package
      else
        status_write "$N" warn "Package is ready but the iCloud upload has not finished; older packages are kept and the hourly poll will finish up: ${F:t} ($sz)"
        log "$N" "done uploaded=0; kept older packages (agent-hygiene polling finishes up)"
      fi
    }
    if [[ "${ICLOUD_UPLOAD_SYNC_WAIT:-0}" == "1" ]]; then
      watch_upload
    else
      ( watch_upload ) >/dev/null 2>&1 &!
    fi
  fi
else
  status_write "$N" warn "Package verified locally; no upload status helper, so older packages in iCloud are kept: ${F:t} ($sz)"
  log "$N" "done uploaded=unknown; kept older packages"
fi
exit 0
