#!/bin/zsh
# Universal Backup & Storage Watchdog Shared Library (lib.sh)
# Sourced by icloud-package.sh, backup-restic.sh, maintenance.sh, restore-drill.sh, volume-guard.sh
export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
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
REPO="${BACKUP_RESTIC_REPO:-${VOL}/AI/Backups/restic}"
MEDIA_PATH="${BACKUP_MEDIA_PATH:-${VOL}/Work}"
ICLOUD="${BACKUP_ICLOUD_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/AI备份/每晚小包}"
KC_ACCOUNT="${BACKUP_KEYCHAIN_ACCOUNT:-${USER:-$(id -un)}}"
KC_ICLOUD_SVC="${BACKUP_KEYCHAIN_ICLOUD:-icloud-package-key}"
KC_RESTIC_SVC="${BACKUP_KEYCHAIN_RESTIC:-restic-mac-agents}"
HOSTTAG="${BACKUP_HOST_TAG:-$(scutil --get LocalHostName 2>/dev/null || hostname -s)}"
EXTRA_MIGRATION_PATHS="${EXTRA_MIGRATION_PATHS:-}"

if [[ -x "$S/bin/restic" ]]; then
  RESTIC="$S/bin/restic"
else
  RESTIC="$(command -v restic 2>/dev/null || echo "$S/bin/restic")"
fi

export RESTIC_PASSWORD_COMMAND="security find-generic-password -s ${KC_RESTIC_SVC} -a ${KC_ACCOUNT} -w"
export RESTIC_CACHE_DIR="$HOME/Library/Caches/restic"

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

nas_mounted() {
  if [[ -n "$NAS_SMB_MATCH" ]]; then
    if ! /sbin/mount | /usr/bin/grep -Fq "$NAS_SMB_MATCH"; then
      local smb_spec="${NAS_SMB_MATCH%% on *}"
      local host_part="${smb_spec#*@}"
      host_part="${host_part#//}"
      local nas_host="${host_part%%/*}"
      if [[ -n "$nas_host" ]] && /sbin/ping -c 1 -W 1000 "$nas_host" >/dev/null 2>&1; then
        /usr/bin/osascript -e "mount volume \"smb:${smb_spec}\"" >/dev/null 2>&1 || true
        sleep 2
      fi
    fi
    /sbin/mount | /usr/bin/grep -Fq "$NAS_SMB_MATCH" && [[ -f "$REPO/config" ]]
  elif [[ -n "$NAS" ]]; then
    /sbin/mount | /usr/bin/grep -Fq " on $NAS (" && [[ -f "$REPO/config" ]]
  else
    [[ -f "$REPO/config" ]]
  fi
}

log() {
  local name="$1"; shift
  echo "$(date '+%F %T') $*" >> "$LOGD/$name.log"
}

notify() {
  osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true
}

status_write() {
  python3 - "$ST/$1.json" "$1" "$2" "$3" "${4:-{\}}" <<'PY'
import json, sys, time, os, tempfile
p, name, state, msg, extra = sys.argv[1:6]
d = {"name": name, "state": state, "message": msg, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
try:
    d.update(json.loads(extra))
except Exception:
    pass
fd, tmp = tempfile.mkstemp(prefix=".status-", dir=os.path.dirname(p))
try:
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
  "$AG/status.sh" >/dev/null 2>&1 || true
}

take_lock() {
  mkdir "$ST/.lock-$1" 2>/dev/null || { log "$1" "另一个实例在跑，跳过"; exit 0; }
  trap "rmdir '$ST/.lock-$1' 2>/dev/null" EXIT
}
