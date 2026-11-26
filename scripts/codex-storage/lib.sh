#!/bin/zsh
# Shared library of the scheduled backup jobs (lib.sh)
# Sourced by icloud-package.sh, backup-restic.sh, maintenance.sh, restore-drill.sh, volume-guard.sh and
# agent-hygiene.sh. All settings come from ~/.orca/backup-config.env (written by setup_backups.py).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG="en_US.UTF-8" LC_ALL="en_US.UTF-8"
setopt NULL_GLOB
zmodload zsh/system

if [[ -f "$HOME/.orca/backup-config.env" ]]; then
  source "$HOME/.orca/backup-config.env"
fi
if [[ -f "$HOME/.orca/hygiene.env" ]]; then
  source "$HOME/.orca/hygiene.env"
fi

# The state folder keeps its historical name: renaming it would orphan every installed setup
S="$HOME/Library/Application Support/Codex Storage"
AG="$S/agents"
ST="$S/status"
LOGD="$S/logs"
mkdir -p "$ST" "$LOGD"

STORAGE_MODE="${STORAGE_MODE:-auto}"
VOL="${AI_VOLUME_PATH:-$HOME/AI-Workspace}"
# AI_VOLUME_PATH may name a folder inside a volume (/Volumes/SSD/Workspace): checks that need the
# mount point use the volume root
if [[ "$VOL" == /Volumes/?* ]]; then
  VOL_ROOT="/Volumes/${${VOL#/Volumes/}%%/*}"
else
  VOL_ROOT="$VOL"
fi
VOL_UUID="${AI_VOLUME_UUID:-AUTO}"
NAS="${BACKUP_NAS_MOUNT:-}"
NAS_SMB_MATCH="${BACKUP_NAS_SMB_MATCH:-}"
NAS_AUTOMOUNT="${BACKUP_NAS_AUTOMOUNT:-auto}"
REPO="${BACKUP_RESTIC_REPO:-${VOL}/AI/Backups/restic}"
MEDIA_PATH="${BACKUP_MEDIA_PATH:-${VOL}/Work}"
# iCloud folder for the nightly packages. Installs that already publish to the legacy folder keep it.
ICLOUD_DRIVE="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
ICLOUD_LEGACY_DIR="$ICLOUD_DRIVE/AI备份/每晚小包"
if [[ -n "${BACKUP_ICLOUD_DIR:-}" ]]; then
  ICLOUD="$BACKUP_ICLOUD_DIR"
elif [[ -d "$ICLOUD_LEGACY_DIR" ]]; then
  ICLOUD="$ICLOUD_LEGACY_DIR"
else
  ICLOUD="$ICLOUD_DRIVE/Agent Backups/Nightly"
fi
ICLOUD_KEEP_LATEST="${BACKUP_ICLOUD_KEEP_LATEST:-1}"
# Raw Claude Code transcripts (~/.claude/projects) grow by gigabytes; Restic backs them up every hour, so the
# nightly iCloud package leaves them out unless this is "true".
ICLOUD_INCLUDE_TRANSCRIPTS="${BACKUP_ICLOUD_INCLUDE_TRANSCRIPTS:-false}"
ICLOUD_MAX_AGE_DAYS="${BACKUP_ICLOUD_MAX_AGE_DAYS:-7}"
ICLOUD_PRUNE_SCOPE="${BACKUP_ICLOUD_PRUNE_SCOPE:-all-hosts}"
ICLOUD_STALE_HOST_DAYS="${BACKUP_ICLOUD_STALE_HOST_DAYS:-0}"
ICLOUD_PRUNE_INTERVAL_SEC="${BACKUP_ICLOUD_PRUNE_INTERVAL_SEC:-3600}"
KC_ACCOUNT="${BACKUP_KEYCHAIN_ACCOUNT:-${USER:-$(id -un)}}"
KC_ICLOUD_SVC="${BACKUP_KEYCHAIN_ICLOUD:-icloud-package-key}"
KC_RESTIC_SVC="${BACKUP_KEYCHAIN_RESTIC:-restic-mac-agents}"
# Absolute path so a `security` earlier on PATH can never see the keys (override exists for the tests)
SECURITY_BIN="${BACKUP_SECURITY_BIN:-/usr/bin/security}"
HOSTTAG="${BACKUP_HOST_TAG:-$(scutil --get LocalHostName 2>/dev/null || hostname -s)}"
SAFE_HOST="${HOSTTAG//[^A-Za-z0-9._-]/_}"
[[ -n "$SAFE_HOST" ]] || SAFE_HOST="Mac"
EXTRA_MIGRATION_PATHS="${EXTRA_MIGRATION_PATHS:-}"
EXTRA_RESTIC_PATHS="${EXTRA_RESTIC_PATHS:-}"
# Hours without a successful run before a repeatedly skipped job is escalated to `stale` (0 disables)
STALE_ALERT_HOURS="${BACKUP_STALE_ALERT_HOURS:-24}"
RESTIC_RETRY_LOCK="${BACKUP_RESTIC_RETRY_LOCK:-5m}"

if [[ -x "$S/bin/restic" ]]; then
  RESTIC="$S/bin/restic"
else
  RESTIC="$(command -v restic 2>/dev/null || echo "$S/bin/restic")"
fi

_kc_svc_q="${KC_RESTIC_SVC//\'/\'\\\'\'}"
_kc_acct_q="${KC_ACCOUNT//\'/\'\\\'\'}"
export RESTIC_PASSWORD_COMMAND="/bin/sh -c '/usr/bin/security find-generic-password -s \"\$1\" -a \"\$2\" -w 2>/dev/null || /usr/bin/security find-generic-password -s \"\$1\" -w' _ '${_kc_svc_q}' '${_kc_acct_q}'"
export RESTIC_CACHE_DIR="$HOME/Library/Caches/restic"

# kc_secret <service> — print a Keychain password; fails when it is missing or empty
kc_secret() {
  local secret
  secret="$("$SECURITY_BIN" find-generic-password -s "$1" -a "$KC_ACCOUNT" -w 2>/dev/null \
    || "$SECURITY_BIN" find-generic-password -s "$1" -w 2>/dev/null)" || return 1
  [[ -n "$secret" ]] || return 1
  print -r -- "$secret"
}

# --- Exit handling -----------------------------------------------------------------------------
# zsh runs an EXIT trap that was set *inside a function* as soon as that function returns, so the
# trap must live at file scope. Scripts register cleanups with `_EXIT_HOOKS+=(fn)` instead of
# overriding the trap; the job lock is always released last.
typeset -ga _EXIT_HOOKS
LOCK_NAME=""
_LOCK_FD=""
_run_exit_hooks() {
  local hook
  for hook in "${_EXIT_HOOKS[@]}"; do
    "$hook" 2>/dev/null || true
  done
  release_lock
}
trap _run_exit_hooks EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# True when the workspace is usable: the folder exists and, on an external volume, the volume root is
# mounted and (when AI_VOLUME_UUID is set) is the expected volume
vol_mounted() {
  if [[ "$STORAGE_MODE" == "internal" || "$VOL" != /Volumes/* ]]; then
    [[ -d "$VOL/AI" ]] && return 0 || return 1
  fi
  python3 - "$VOL_ROOT" "$VOL_UUID" <<'PYVOL' || return 1
import os, plistlib, subprocess, sys
path, expected_uuid = sys.argv[1], sys.argv[2]
if not os.path.ismount(path):
    sys.exit(1)
try:
    info = plistlib.loads(subprocess.check_output(['/usr/sbin/diskutil', 'info', '-plist', path], stderr=subprocess.DEVNULL, timeout=12))
except Exception:
    sys.exit(1)
if info.get('MountPoint') != path:
    sys.exit(1)
if expected_uuid and expected_uuid not in ('AUTO', 'LOCAL') and info.get('VolumeUUID') != expected_uuid:
    sys.exit(1)
sys.exit(0)
PYVOL
  [[ -d "$VOL" ]]
}

# True when <host> answers an SMB negotiate. Ping and a bare TCP connect are not enough: a TUN /
# fake-IP proxy answers both for any address, including a NAS on a network this Mac is not on.
_smb_host_alive() {
  local host="$1" out pid i
  local ping_bin="${SMB_PROBE_PING:-/sbin/ping}" smbutil_bin="${SMB_PROBE_SMBUTIL:-/usr/bin/smbutil}"
  "$ping_bin" -c 1 -W 1000 "$host" >/dev/null 2>&1 || return 1
  [[ -x "$smbutil_bin" ]] || return 0
  out="$(mktemp "${TMPDIR:-/tmp}/smb-probe.XXXXXX")" || return 0
  "$smbutil_bin" view -g "//$host" >"$out" 2>&1 &
  pid=$!
  for i in {1..16}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null
    rm -f "$out"
    return 1
  fi
  # An authentication error still proves the server is there; only a failed connection does not.
  # smbutil words a refused guest login as "server connection failed: Authentication error",
  # so the reason after the colon decides.
  if /usr/bin/grep -q "server connection failed" "$out" 2>/dev/null \
     && ! /usr/bin/grep -qi "server connection failed: authentication" "$out" 2>/dev/null; then
    rm -f "$out"
    return 1
  fi
  rm -f "$out"
  return 0
}

# Sets NAS_REASON to a human-readable cause whenever it returns non-zero.
NAS_REASON=""
nas_mounted() {
  NAS_REASON=""
  if [[ -n "$NAS_SMB_MATCH" ]]; then
    if ! /sbin/mount | /usr/bin/grep -Fq "$NAS_SMB_MATCH"; then
      local smb_spec="${NAS_SMB_MATCH%% on *}"
      local host_part="${smb_spec#*@}"
      host_part="${host_part#//}"
      local nas_host="${host_part%%/*}"
      if [[ "$NAS_AUTOMOUNT" == "never" ]]; then
        NAS_REASON="NAS share is not mounted (BACKUP_NAS_AUTOMOUNT=never; mount smb:${smb_spec} by hand)"
        return 1
      fi
      if [[ -z "$nas_host" ]] || ! _smb_host_alive "$nas_host"; then
        NAS_REASON="NAS ${nas_host:-?} is unreachable (no SMB answer: other network, VPN down or NAS off)"
        return 1
      fi
      # Only auto-mount when macOS already holds a saved SMB credential for this host; otherwise
      # `mount volume` pops an authentication dialog on every scheduled run.
      if [[ "$NAS_AUTOMOUNT" != "always" ]] && ! /usr/bin/security find-internet-password -s "$nas_host" >/dev/null 2>&1; then
        NAS_REASON="NAS is reachable but the share is not mounted and no SMB password is saved in the Keychain: connect to smb:${smb_spec} once in Finder (Go > Connect to Server) and tick \"Remember this password in my keychain\""
        return 1
      fi
      /usr/bin/osascript -e 'on run argv' -e 'mount volume (item 1 of argv)' -e 'end run' -- "smb:${smb_spec}" >/dev/null 2>&1 || true
      sleep 2
      if ! /sbin/mount | /usr/bin/grep -Fq "$NAS_SMB_MATCH"; then
        NAS_REASON="Automatic mount of the NAS share failed (the saved password may be outdated): reconnect to smb:${smb_spec} in Finder"
        return 1
      fi
    fi
  elif [[ -n "$NAS" ]]; then
    if ! /sbin/mount | /usr/bin/grep -Fq " on $NAS ("; then
      NAS_REASON="Backup volume is not mounted ($NAS)"
      return 1
    fi
  fi
  if [[ ! -f "$REPO/config" ]]; then
    NAS_REASON="Restic repository is missing or not initialised ($REPO)"
    return 1
  fi
  return 0
}

log() {
  local name="$1"; shift
  echo "$(date '+%F %T') $*" >> "$LOGD/$name.log"
}

notify() {
  [[ "${BACKUP_NOTIFY:-on}" == "off" ]] && return 0
  # Title and body travel as argv so quotes/backslashes in messages can never break the AppleScript
  /usr/bin/osascript -e 'on run argv' \
    -e 'display notification (item 2 of argv) with title (item 1 of argv)' \
    -e 'end run' -- "$1" "$2" >/dev/null 2>&1 || true
}

# status_write <job> <state> <message> [extra-json] [stale-hours]
# Keeps `last_ok` (last ok/warn run) and `since` (start of the current non-success streak) across
# runs. With stale-hours > 0 a `skipped` receipt is escalated to `stale` once the job has gone that
# long without a success, and a notification is raised at most once every 24 h.
status_write() {
  local alert
  alert=$(python3 - "$ST/$1.json" "$1" "$2" "$3" "${4:-{\}}" "${5:-0}" <<'PY'
import json, os, sys, tempfile, time
p, name, state, msg, extra, stale_hours = sys.argv[1:7]
FMT = "%Y-%m-%d %H:%M:%S"
OK = ("ok", "warn")
now = time.time()
now_s = time.strftime(FMT, time.localtime(now))

def parse(value):
    try:
        return time.mktime(time.strptime(value, FMT))
    except Exception:
        return None

try:
    with open(p, encoding="utf-8") as f:
        prev = json.load(f)
    if not isinstance(prev, dict):
        prev = {}
except Exception:
    prev = {}

d = {"name": name, "state": state, "message": msg, "time": now_s}
prev_state = prev.get("state")
last_ok = prev.get("last_ok") or (prev.get("time") if prev_state in OK else None)
if state in OK:
    d["last_ok"] = now_s
else:
    if last_ok:
        d["last_ok"] = last_ok
    since = prev.get("since")
    if not since and prev_state not in OK and prev_state not in (None, "running"):
        since = prev.get("time")
    if state != "running":
        since = since or now_s
    if since:
        d["since"] = since
    if prev.get("last_alert"):
        d["last_alert"] = prev["last_alert"]
try:
    more = json.loads(extra)
    if isinstance(more, dict):
        d.update(more)
except Exception:
    pass

try:
    limit = float(stale_hours)
except ValueError:
    limit = 0.0
if state == "skipped" and limit > 0:
    ref = parse(d.get("last_ok")) or parse(d.get("since")) or now
    hours = (now - ref) / 3600
    if hours >= limit:
        d["state"] = "stale"
        d["message"] = f"No successful run for {int(hours)} hours: {msg}"
        last_alert = parse(d.get("last_alert"))
        if last_alert is None or now - last_alert >= 24 * 3600:
            d["last_alert"] = now_s
            print(d["message"])

fd, tmp = tempfile.mkstemp(prefix=".status-", dir=os.path.dirname(p))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
  )
  [[ -n "$alert" ]] && notify "Backup has not succeeded: $1" "$alert"
  "$AG/status.sh" >/dev/null 2>&1 || true
}

# skip_job <job> <reason> [stale-hours] — record a skipped run, escalate when it keeps happening
skip_job() {
  log "$1" "skip: $2"
  status_write "$1" skipped "$2" '{}' "${3:-$STALE_ALERT_HOURS}"
}

# sqlite_snapshot <src.db> <dst.db> — consistent point-in-time copy of a live SQLite database
SQLITE_SNAPSHOT_FAILURES=0
sqlite_snapshot() {
  [[ -f "$1" ]] || return 0
  python3 "$AG/sqlite_snapshot.py" "$1" "$2" >/dev/null 2>>"$LOGD/sqlite-snapshot.log" \
    || { log sqlite-snapshot "failed: $1"; (( SQLITE_SNAPSHOT_FAILURES += 1 )); }
  return 0
}

# snapshot_agent_databases <stage-dir> — snapshot the live agent databases that both the Restic hot
# backup and the iCloud package carry (missing databases are skipped)
snapshot_agent_databases() {
  local stage="$1" orca="$HOME/Library/Application Support/orca" acct_home acct_id db_name
  mkdir -p "$stage"
  sqlite_snapshot "$orca/orchestration.db" "$stage/orca-orchestration.db"
  sqlite_snapshot "$orca/profiles/local-default/profile-state.db" "$stage/orca-profile-state.db"
  sqlite_snapshot "$orca/ai-vault/session-parse-cache.sqlite" "$stage/orca-session-parse-cache.sqlite"
  sqlite_snapshot "$HOME/.codex/memories_1.sqlite" "$stage/codex-memories_1.sqlite"
  sqlite_snapshot "$HOME/.codex/memories_v2_1.sqlite" "$stage/codex-memories_v2_1.sqlite"
  sqlite_snapshot "$HOME/.codex/state_5.sqlite" "$stage/codex-state_5.sqlite"
  sqlite_snapshot "$HOME/.gemini/antigravity/conversation_summaries.db" "$stage/antigravity-conversation_summaries.db"
  for acct_home in "$orca/codex-accounts"/*/home; do
    acct_id="${acct_home:h:t}"
    for db_name in state_5.sqlite memories_1.sqlite memories_v2_1.sqlite goals_1.sqlite queue_1.sqlite; do
      sqlite_snapshot "$acct_home/$db_name" "$stage/codex-${acct_id}-${db_name}"
    done
  done
}

# extra_paths <comma-separated list>... — set $reply to the cleaned paths relative to $HOME.
# Group Containers are skipped: macOS privacy protection blocks background jobs from reading them.
extra_paths() {
  setopt localoptions extendedglob
  local item cleaned
  reply=()
  for item in ${(s:,:)${(j:,:)@}}; do
    cleaned="${${item##[[:space:]]#}%%[[:space:]]#}"
    cleaned="${${cleaned#\~/}#$HOME/}"
    [[ -n "$cleaned" && "$cleaned" != "Library/Group Containers"* ]] && reply+=("$cleaned")
  done
}

# --- Job lock ----------------------------------------------------------------------------------
# status/.lock-<job>.flock carries a kernel (fcntl) lock held by the job's shell. The kernel drops it
# when the shell exits for any reason (crash, SIGKILL, reboot), so a lock can never go stale and
# no instance ever has to reclaim one by hand. The owner PID goes to .lock-<job>.pid: the holding
# shell must never open and close the .flock file itself, because closing any descriptor of a file
# releases the process's fcntl locks on it.
_lock_owner_alive() {
  local pid="$1"
  [[ "$pid" == <-> ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # Guard against PID reuse: the owner must still be one of these backup scripts
  /bin/ps -p "$pid" -o command= 2>/dev/null | /usr/bin/grep -Fq -- "${ZSH_ARGZERO:t}"
}

take_lock() {
  local name="$1" lockfile="$ST/.lock-$1.flock" legacy="$ST/.lock-$1" pid age
  # A job still running from a previous script version holds a lock directory instead
  if [[ -d "$legacy" ]]; then
    pid="$(cat "$legacy/pid" 2>/dev/null)"
    if _lock_owner_alive "$pid"; then
      log "$name" "Another instance is running (pid=$pid, previous script version); skipping"
      exit 0
    fi
    age=$(( $(date +%s) - $(stat -f %m "$legacy" 2>/dev/null || date +%s) ))
    if [[ -z "$pid" ]] && (( age < 43200 )); then
      log "$name" "Lock directory without a PID is less than 12 hours old; treating it as running and skipping"
      exit 0
    fi
    rm -f "$legacy/pid" 2>/dev/null
    rmdir "$legacy" 2>/dev/null
  fi
  [[ -e "$lockfile" ]] || : >> "$lockfile" 2>/dev/null
  if ! zsystem flock -t 0 -f _LOCK_FD "$lockfile" 2>/dev/null; then
    log "$name" "Another instance is running (pid=$(cat "$ST/.lock-$name.pid" 2>/dev/null)); skipping"
    exit 0
  fi
  print -r -- "$$" > "$ST/.lock-$name.pid"
  LOCK_NAME="$name"
}

release_lock() {
  [[ -n "$LOCK_NAME" ]] || return 0
  if [[ -n "$_LOCK_FD" ]]; then
    [[ "$(cat "$ST/.lock-$LOCK_NAME.pid" 2>/dev/null)" == "$$" ]] && rm -f "$ST/.lock-$LOCK_NAME.pid"
    zsystem flock -u "$_LOCK_FD" 2>/dev/null
  fi
  _LOCK_FD=""
  LOCK_NAME=""
}
