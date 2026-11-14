#!/bin/zsh
# Nightly Encrypted Compact Backup to iCloud Drive (icloud-package.sh)
# Covers all multi-agent configs, memories, multi-account pools, Muse tokens,
# Xcode signing, iOS pairing, LaunchAgents, and APFS CoW SQLite snapshots.
# Encrypts with AES-256-CBC PBKDF2, verifies decryption before atomic publish,
# and requests brctl evict after iCloud upload completes.
umask 077
setopt PIPE_FAIL NULL_GLOB
source "$(dirname "$0")/lib.sh"
N=icloud-package
take_lock "$N"
status_write "$N" running "开始生成并验证全量 Agent 加密备份小包"
log "$N" "start verified-package"
WORKDIR=""
SQLITE_STAGE="$HOME/.orca/sqlite-backups"

cleanup_package() {
  if [[ -n "$WORKDIR" && "$WORKDIR" == "$HOME/Library/Caches/icloud-package."* ]]; then
    rm -f -- "$WORKDIR/archive.tar.gz" "$WORKDIR/archive.tar.gz.enc"
    rmdir -- "$WORKDIR" 2>/dev/null || true
  fi
  if [[ -d "$SQLITE_STAGE" ]]; then
    rm -rf -- "$SQLITE_STAGE" 2>/dev/null || true
  fi
}
_EXIT_HOOKS+=(cleanup_package)
trap 'status_write "$N" fail "备份任务被中断；历史版本保留"; exit 130' INT TERM HUP
fail_package() { log "$N" "failed: $1"; status_write "$N" fail "$1"; exit 1; }

free_kb=$(df -Pk "$HOME/Library/Caches" | awk 'END {print $4}')
[[ "$free_kb" == <-> && "$free_kb" -ge 8388608 ]] || fail_package "内置盘可用空间少于 8 GiB，保留历史备份并停止"
mkdir -p "$ICLOUD" || fail_package "iCloud 备份目录不可写"
WORKDIR=$(mktemp -d "$HOME/Library/Caches/icloud-package.XXXXXX") || fail_package "无法建立临时目录"
TMP="$WORKDIR/archive.tar.gz"
ENC="$WORKDIR/archive.tar.gz.enc"
ts=$(date +%Y%m%d-%H%M%S)
SAFE_HOST="${HOSTTAG//[^A-Za-z0-9._-]/_}"
[[ -n "$SAFE_HOST" ]] || SAFE_HOST="Mac"
F="$ICLOUD/mac-agents-${SAFE_HOST}-${ts}.tar.gz.enc"
[[ ! -e "$F" ]] || fail_package "目标文件已存在，拒绝覆盖"

# Fail-closed if Keychain encryption key is missing (never silently generate a new random key on a new Mac!)
if ! security find-generic-password -s "$KC_ICLOUD_SVC" -a "$KC_ACCOUNT" -w >/dev/null 2>&1; then
  notify "iCloud 备份失败" "缺少钥匙串密钥 $KC_ICLOUD_SVC，请先导入旧机密钥"
  fail_package "钥匙串缺少加密密钥 $KC_ICLOUD_SVC (account=$KC_ACCOUNT)；已拒绝自动生成新密钥以保护历史备份"
fi

# 1. Consistent point-in-time copies of core SQLite databases (see sqlite_snapshot.py)
mkdir -p "$SQLITE_STAGE"
sqlite_snapshot "$HOME/Library/Application Support/orca/orchestration.db" "$SQLITE_STAGE/orca-orchestration.db"
sqlite_snapshot "$HOME/Library/Application Support/orca/profiles/local-default/profile-state.db" "$SQLITE_STAGE/orca-profile-state.db"
sqlite_snapshot "$HOME/Library/Application Support/orca/ai-vault/session-parse-cache.sqlite" "$SQLITE_STAGE/orca-session-parse-cache.sqlite"
sqlite_snapshot "$HOME/.codex/memories_1.sqlite" "$SQLITE_STAGE/codex-memories_1.sqlite"
sqlite_snapshot "$HOME/.codex/memories_v2_1.sqlite" "$SQLITE_STAGE/codex-memories_v2_1.sqlite"
sqlite_snapshot "$HOME/.codex/state_5.sqlite" "$SQLITE_STAGE/codex-state_5.sqlite"
sqlite_snapshot "$HOME/.gemini/antigravity/conversation_summaries.db" "$SQLITE_STAGE/antigravity-conversation_summaries.db"
for acct_home in "$HOME/Library/Application Support/orca/codex-accounts"/*/home; do
  acct_id="$(basename "$(dirname "$acct_home")")"
  for db_name in state_5.sqlite memories_1.sqlite memories_v2_1.sqlite goals_1.sqlite queue_1.sqlite; do
    sqlite_snapshot "$acct_home/$db_name" "$SQLITE_STAGE/codex-${acct_id}-${db_name}"
  done
done

# 2. Universal multi-agent state paths (excludes VMs & multi-GB raw logs for compact iCloud size)
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
  ".dsh/.credentials.yaml"
  ".dsh/.anonymous-user-id"
  ".dsh/cordis.patch.yml"
  ".dsh/llm-deepseek"
  ".dsh/sessions"
  ".dsh/storages"
  ".dsh-tui"
  ".grokbot"
  ".ssh"
  ".gitconfig"
  ".zshrc"
  ".zshenv"
  ".zprofile"
  ".zsh_history"
  ".python_history"
  ".pymobiledevice3"
  ".local/bin/agent-guard"
  "Library/LaunchAgents"
  "Library/Developer/Xcode/UserData"
  "Library/Application Support/Codex Storage/agents"
  "Library/Application Support/Codex Storage/bin"
  "Library/Application Support/com.meta.endo"
  "Library/Application Support/com.meta.prototypes.endo.tokens"
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

# Dynamically include any EXTRA_MIGRATION_PATHS from config.env (excluding TCC-blocked Group Containers)
for extra_item in ${(s:,:)EXTRA_MIGRATION_PATHS}; do
  cleaned="${extra_item## }"
  cleaned="${cleaned%% }"
  [[ -n "$cleaned" && "$cleaned" != "Library/Group Containers"* ]] && CANDIDATES+=("$cleaned")
done

# Include all Orca-managed Codex account configs, memories & skills
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

( cd "$HOME" && COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs -czf "$TMP" \
    --exclude ".claude/shell-snapshots" \
    --exclude ".claude/debug" \
    --exclude ".dsh-tui/compile-cache" \
    --exclude ".dsh-tui/inject" \
    --exclude ".ssh/agent" \
    --exclude "Library/Group Containers" \
    --exclude "node_modules" \
    --exclude ".venv" \
    --exclude "venv" \
    --exclude "__pycache__" \
    --exclude "*.sock" \
    --exclude "*.ipc" \
    --exclude "Singleton*" \
    --exclude "fsmonitor--daemon*" \
    --exclude "*.fcpbundle" \
    --exclude "Render Files" \
    --exclude "Transcoded Media" \
    --exclude "Analysis Files" \
    --exclude "*.sqlite-wal" \
    --exclude "*.sqlite-shm" \
    --exclude "*.db-wal" \
    --exclude "*.db-shm" \
    --exclude ".DS_Store" \
    "${EXISTING_ITEMS[@]}" ) 2>>"$LOGD/$N.log" || fail_package "tar 失败；没有发布不完整备份"

security find-generic-password -s "$KC_ICLOUD_SVC" -a "$KC_ACCOUNT" -w | openssl enc -aes-256-cbc -pbkdf2 -salt -in "$TMP" -out "$ENC" -pass stdin 2>>"$LOGD/$N.log" || fail_package "加密失败；没有发布不完整备份"
security find-generic-password -s "$KC_ICLOUD_SVC" -a "$KC_ACCOUNT" -w | openssl enc -d -aes-256-cbc -pbkdf2 -in "$ENC" -pass stdin 2>>"$LOGD/$N.log" | tar -tzf - >/dev/null 2>>"$LOGD/$N.log" || fail_package "解密归档校验失败；没有发布不完整备份"
[[ "$(stat -f %d "$ENC")" == "$(stat -f %d "$ICLOUD")" ]] || fail_package "临时目录与 iCloud 不在同一文件系统，停止发布"
mv -n "$ENC" "$F" || fail_package "发布失败"
[[ ! -e "$ENC" && -s "$F" ]] || fail_package "未能完整发布备份"
sz=$(du -h "$F" | cut -f1)
digest=$(shasum -a 256 "$F" | awk '{print $1}')
log "$N" "archive verified: $(basename "$F") sha256=$digest items=${#EXISTING_ITEMS[@]}"

# Record published package in THIS host's ledger, and only prune older packages AFTER iCloud upload
# is confirmed (when `$S/bin/icloud_status` is available). A LaunchAgent usually has no TCC permission
# to list iCloud Drive, so this host tracks its own published filenames in `$LEDGER`. Packages of other
# host tags are never touched, and if iCloud sync stalls while away from home, previously uploaded
# packages remain intact in iCloud.
KEEP_LATEST="${BACKUP_ICLOUD_KEEP_LATEST:-1}"
LEDGER="$S/icloud-published-${SAFE_HOST}.list"
print -r -- "${F:t}" >> "$LEDGER"

prune_own_packages() {
  [[ "$KEEP_LATEST" =~ ^[0-9]+$ && "$KEEP_LATEST" -ge 1 ]] || return 0
  [[ -f "$LEDGER" ]] || return 0
  typeset -aU own_pkgs
  own_pkgs=( ${(f)"$(<"$LEDGER")"} "$ICLOUD"/mac-agents-${SAFE_HOST}-*.tar.gz.enc(N.:t) )
  /bin/ls "$ICLOUD" >/dev/null 2>&1 || log "$N" "note: 无权列出 iCloud 目录（macOS 隐私限制），仅按本机发布记录清理旧包"
  local -a kept=()
  local pkg_name
  for pkg_name in ${(On)own_pkgs}; do
    [[ "$pkg_name" == mac-agents-${SAFE_HOST}-<->-<->.tar.gz.enc ]] || continue
    [[ -e "$ICLOUD/$pkg_name" ]] || continue
    if (( ${#kept[@]} < KEEP_LATEST )); then
      kept+=("$pkg_name")
    elif rm -f -- "$ICLOUD/$pkg_name" 2>>"$LOGD/$N.log" && [[ ! -e "$ICLOUD/$pkg_name" ]]; then
      log "$N" "pruned superseded package: $pkg_name"
    else
      kept+=("$pkg_name")
      log "$N" "could not prune $pkg_name (kept in ledger for the next run)"
    fi
  done
  print -rl -- "${kept[@]}" > "$LEDGER.tmp" && mv -f "$LEDGER.tmp" "$LEDGER"
}

uploaded_ok() {
  [[ ! -x "$S/bin/icloud_status" ]] && return 1
  "$S/bin/icloud_status" "$1" 2>/dev/null | head -1 | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("isUploaded") is True and not d.get("hasConflicts") and not d.get("uploadingError") and not d.get("uploadError") and not d.get("errorCode") else 1)' 2>/dev/null
}

finalize_uploaded_package() {
  prune_own_packages
  if brctl evict "$F" >/dev/null 2>&1; then
    status_write "$N" ok "解密校验及上传通过，已请求驱逐本地副本 (保留最新 ${KEEP_LATEST} 份): $(basename "$F") $sz"
  else
    status_write "$N" ok "解密校验及上传通过，本地副本保留 (保留最新 ${KEEP_LATEST} 份): $(basename "$F") $sz"
  fi
  log "$N" "done uploaded=1; retained latest ${KEEP_LATEST} package(s)"
}

if [[ -x "$S/bin/icloud_status" ]]; then
  if uploaded_ok "$F"; then
    finalize_uploaded_package
  else
    status_write "$N" warn "本地解密校验通过，等待 iCloud 上传确认后再清理旧包: $(basename "$F") ($sz)"
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
        status_write "$N" warn "本地包已就绪但等待 iCloud 上传超时，已保留历史云端旧包: $(basename "$F") ($sz)"
        log "$N" "done uploaded=0; kept historical packages without pruning"
      fi
    }
    if [[ "${ICLOUD_UPLOAD_SYNC_WAIT:-0}" == "1" ]]; then
      watch_upload
    else
      ( watch_upload ) >/dev/null 2>&1 &!
    fi
  fi
else
  prune_own_packages
  status_write "$N" ok "解密校验通过并已发布至 iCloud 云盘 (保留最新 ${KEEP_LATEST} 份): $(basename "$F") ($sz)"
  log "$N" "done uploaded=1 (no icloud_status binary); retained latest ${KEEP_LATEST} package(s)"
fi
exit 0
