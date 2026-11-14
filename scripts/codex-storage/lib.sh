#!/bin/zsh
# Universal Backup & Storage Watchdog Shared Library (lib.sh)
# Sourced by icloud-package.sh, backup-restic.sh, maintenance.sh, restore-drill.sh, volume-guard.sh
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG="en_US.UTF-8" LC_ALL="en_US.UTF-8"
setopt NULL_GLOB

# Load user/machine-specific configuration from ~/.orca/backup-config.env (written by setup_backups.py)
if [[ -f "$HOME/.orca/backup-config.env" ]]; then
  source "$HOME/.orca/backup-config.env"
fi

S="$HOME/Library/Application Support/Codex Storage"
AG="$S/agents"
ST="$S/status"
LOGD="$S/logs"
mkdir -p "$ST" "$LOGD"

STORAGE_MODE="${STORAGE_MODE:-auto}"
VOL="${AI_VOLUME_PATH:-$HOME/AI-Workspace}"
VOL_UUID="${AI_VOLUME_UUID:-AUTO}"
NAS="${BACKUP_NAS_MOUNT:-}"
NAS_SMB_MATCH="${BACKUP_NAS_SMB_MATCH:-}"
NAS_AUTOMOUNT="${BACKUP_NAS_AUTOMOUNT:-auto}"
REPO="${BACKUP_RESTIC_REPO:-${VOL}/AI/Backups/restic}"
MEDIA_PATH="${BACKUP_MEDIA_PATH:-${VOL}/Work}"
ICLOUD="${BACKUP_ICLOUD_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/AI备份/每晚小包}"
KC_ACCOUNT="${BACKUP_KEYCHAIN_ACCOUNT:-${USER:-$(id -un)}}"
KC_ICLOUD_SVC="${BACKUP_KEYCHAIN_ICLOUD:-icloud-package-key}"
KC_RESTIC_SVC="${BACKUP_KEYCHAIN_RESTIC:-restic-mac-agents}"
HOSTTAG="${BACKUP_HOST_TAG:-$(scutil --get LocalHostName 2>/dev/null || hostname -s)}"
EXTRA_MIGRATION_PATHS="${EXTRA_MIGRATION_PATHS:-}"
EXTRA_RESTIC_PATHS="${EXTRA_RESTIC_PATHS:-}"
# Hours without a successful run before a repeatedly skipped job is escalated to `stale` (0 disables)
STALE_ALERT_HOURS="${BACKUP_STALE_ALERT_HOURS:-24}"

if [[ -x "$S/bin/restic" ]]; then
  RESTIC="$S/bin/restic"
else
  RESTIC="$(command -v restic 2>/dev/null || echo "$S/bin/restic")"
fi

export RESTIC_PASSWORD_COMMAND="/usr/bin/security find-generic-password -s '${KC_RESTIC_SVC}' -a '${KC_ACCOUNT}' -w"
export RESTIC_CACHE_DIR="$HOME/Library/Caches/restic"

# --- Exit handling -----------------------------------------------------------------------------
# zsh runs an EXIT trap that was set *inside a function* as soon as that function returns, so the
# trap must live at file scope. Scripts register cleanups with `_EXIT_HOOKS+=(fn)` instead of
# overriding the trap; the job lock is always released last.
typeset -ga _EXIT_HOOKS
LOCK_NAME=""
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

vol_mounted() {
  # In internal disk mode (or non-/Volumes path), verify local AI directory exists
  if [[ "$STORAGE_MODE" == "internal" || "$VOL" != /Volumes/* ]]; then
    [[ -d "$VOL/AI" ]] && return 0 || return 1
  fi
  python3 - "$VOL" "$VOL_UUID" <<'PYVOL'
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
        NAS_REASON="NAS 共享未挂载（BACKUP_NAS_AUTOMOUNT=never，需手动挂载 smb:${smb_spec}）"
        return 1
      fi
      if [[ -z "$nas_host" ]] || ! _smb_host_alive "$nas_host"; then
        NAS_REASON="NAS ${nas_host:-?} 不可达（SMB 无响应：不在同一网络、未连 VPN 或 NAS 未开机）"
        return 1
      fi
      # Only auto-mount when macOS already holds a saved SMB credential for this host; otherwise
      # `mount volume` pops an authentication dialog on every scheduled run.
      if [[ "$NAS_AUTOMOUNT" != "always" ]] && ! /usr/bin/security find-internet-password -s "$nas_host" >/dev/null 2>&1; then
        NAS_REASON="NAS 可达但共享未挂载，且钥匙串没有保存的 SMB 凭据：请在 Finder ⌘K 连接 smb:${smb_spec} 并勾选“在我的钥匙串中记住此密码”"
        return 1
      fi
      /usr/bin/osascript -e 'on run argv' -e 'mount volume (item 1 of argv)' -e 'end run' -- "smb:${smb_spec}" >/dev/null 2>&1 || true
      sleep 2
      if ! /sbin/mount | /usr/bin/grep -Fq "$NAS_SMB_MATCH"; then
        NAS_REASON="NAS 共享自动挂载失败（保存的凭据可能已失效）：请在 Finder ⌘K 重新连接 smb:${smb_spec}"
        return 1
      fi
    fi
  elif [[ -n "$NAS" ]]; then
    if ! /sbin/mount | /usr/bin/grep -Fq " on $NAS ("; then
      NAS_REASON="备份卷未挂载 ($NAS)"
      return 1
    fi
  fi
  if [[ ! -f "$REPO/config" ]]; then
    NAS_REASON="restic 仓库不存在或尚未初始化 ($REPO)"
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
        d["message"] = f"已 {int(hours)} 小时没有成功运行：{msg}"
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
  [[ -n "$alert" ]] && notify "备份长时间未成功: $1" "$alert"
  "$AG/status.sh" >/dev/null 2>&1 || true
}

# skip_job <job> <reason> [stale-hours] — record a skipped run, escalate when it keeps happening
skip_job() {
  log "$1" "skip: $2"
  status_write "$1" skipped "$2" '{}' "${3:-$STALE_ALERT_HOURS}"
}

# sqlite_snapshot <src.db> <dst.db> — consistent point-in-time copy of a live SQLite database
sqlite_snapshot() {
  [[ -f "$1" ]] || return 0
  python3 "$AG/sqlite_snapshot.py" "$1" "$2" >/dev/null 2>>"$LOGD/sqlite-snapshot.log" \
    || log sqlite-snapshot "failed: $1"
  return 0
}

# --- Job lock ----------------------------------------------------------------------------------
# status/.lock-<job>/pid holds the owner PID. A lock whose owner is gone (crash, SIGKILL, reboot)
# is reclaimed instead of blocking the job forever.
_lock_owner_alive() {
  local pid="$1"
  [[ "$pid" == <-> ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # Guard against PID reuse: the owner must still be one of these backup scripts
  /bin/ps -p "$pid" -o command= 2>/dev/null | /usr/bin/grep -Fq -- "${ZSH_ARGZERO:t}"
}

take_lock() {
  local name="$1" lock="$ST/.lock-$1" pid age
  if ! mkdir "$lock" 2>/dev/null; then
    pid="$(cat "$lock/pid" 2>/dev/null)"
    if _lock_owner_alive "$pid"; then
      log "$name" "另一个实例在跑 (pid=$pid)，跳过"
      exit 0
    fi
    if [[ -z "$pid" ]]; then
      # No PID file: a lock written by an older script version, or an owner still starting up.
      age=$(( $(date +%s) - $(stat -f %m "$lock" 2>/dev/null || date +%s) ))
      if (( age < 43200 )); then
        log "$name" "锁没有 PID 记录且不足 12 小时，按运行中处理，跳过"
        exit 0
      fi
    fi
    log "$name" "回收失效的锁 (pid=${pid:-unknown})"
    rm -f "$lock/pid" 2>/dev/null
    rmdir "$lock" 2>/dev/null
    mkdir "$lock" 2>/dev/null || { log "$name" "锁被其他实例抢占，跳过"; exit 0; }
  fi
  print -r -- "$$" > "$lock/pid"
  LOCK_NAME="$name"
}

release_lock() {
  [[ -n "$LOCK_NAME" ]] || return 0
  local lock="$ST/.lock-$LOCK_NAME"
  if [[ "$(cat "$lock/pid" 2>/dev/null)" == "$$" ]]; then
    rm -f "$lock/pid" 2>/dev/null
    rmdir "$lock" 2>/dev/null
  fi
  LOCK_NAME=""
}
