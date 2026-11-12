#!/usr/bin/env zsh
# ==============================================================================
# Orca Multi-Agent Blueprint — one-line bootstrapper
#
# Fetches (or updates) the blueprint checkout, then hands every argument to deploy.sh.
#
#   curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh
#   curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --dry-run
#
# Without a mode flag it runs `deploy.sh --auto`. Run `deploy.sh --help` for all modes.
# Restoring a new Mac from a migration SSD does not need this script or network access:
#   zsh "/Volumes/<SSD>/新Mac一键恢复.command"
# ==============================================================================
set -euo pipefail

REPO_URL="https://github.com/Rem021/orca-multi-agent-blueprint.git"
CUSTOM_VOLUME=""
STORAGE_MODE_ARG="${STORAGE_MODE:-auto}"
HAS_MODE="false"
ARGS=("$@")

need_value() {
  if [[ $# -lt 2 || "$2" == --* ]]; then
    echo "❌ Error: $1 needs a value." >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --storage) need_value "$@"; STORAGE_MODE_ARG="$2"; shift 2 ;;
    --internal) STORAGE_MODE_ARG="internal"; shift ;;
    --external) STORAGE_MODE_ARG="external"; shift ;;
    --volume) need_value "$@"; CUSTOM_VOLUME="$2"; shift 2 ;;
    --dry-run|--apply|--auto|--doctor|--sync-profiles|--setup-backups|--backup-status|--pack-to-ssd|--pack-dry-run|--restore-from-ssd|--restore-dry-run|--migration-status|--refresh-guide|--agents|--hold-agents|--install-pre-push|--help|-h)
      HAS_MODE="true"; shift ;;
    *) shift ;;
  esac
done
[[ "$HAS_MODE" == "true" ]] || ARGS=(--auto "${ARGS[@]}")

# A volume that is not the internal disk, an installer image, a system volume or read-only
usable_volume() {
  local v="$1" name="${1:t}"
  [[ -d "$v" && -w "$v" ]] || return 1
  [[ "$name" == "Macintosh HD"* || "$name" == "Preboot" || "$name" == "Recovery" || "$name" == "VM" \
     || "$name" == "Update" || "$name" == "com.apple."* ]] && return 1
  [[ -e "$v/Backups.backupdb" ]] && return 1
  local info
  info="$(/usr/sbin/diskutil info "$v" 2>/dev/null || true)"
  [[ "$info" == *"Protocol:"*"Disk Image"* ]] && return 1
  [[ "$info" == *"Device Location:"*"Internal"* ]] && return 1
  return 0
}

# 1. Where the workspace lives: an external SSD under /Volumes, or ~/AI-Workspace on the internal disk
if [[ -n "$CUSTOM_VOLUME" ]]; then
  BASE_VOL="$CUSTOM_VOLUME"
elif [[ "$STORAGE_MODE_ARG" == "internal" ]]; then
  BASE_VOL="${INTERNAL_WORKSPACE_PATH:-$HOME/AI-Workspace}"
elif [[ -n "${AI_VOLUME_PATH:-}" && -d "${AI_VOLUME_PATH}" ]]; then
  BASE_VOL="${AI_VOLUME_PATH}"
else
  # A volume that already has an AI/ folder is adopted; a blank drive only with --external
  FOUND_VOL=""
  FIRST_EXT=""
  for v in /Volumes/*(N/); do
    usable_volume "$v" || continue
    [[ -z "$FIRST_EXT" ]] && FIRST_EXT="$v"
    if [[ -d "${v}/AI" ]]; then
      FOUND_VOL="$v"
      break
    fi
  done
  if [[ "$STORAGE_MODE_ARG" == "external" && -z "$FOUND_VOL" ]]; then
    FOUND_VOL="$FIRST_EXT"
    if [[ -z "$FOUND_VOL" ]]; then
      echo "❌ Error: --external was requested but no writable external volume is mounted." >&2
      exit 1
    fi
  fi
  BASE_VOL="${FOUND_VOL:-${INTERNAL_WORKSPACE_PATH:-$HOME/AI-Workspace}}"
fi

# Never create a look-alike folder at the mount point of a drive that is not plugged in
if [[ "$BASE_VOL" == /Volumes/* ]]; then
  VOL_ROOT="/Volumes/${${BASE_VOL#/Volumes/}%%/*}"
  if ! /sbin/mount | /usr/bin/grep -qF " on ${VOL_ROOT} ("; then
    echo "❌ Error: ${VOL_ROOT} is not a mounted volume. Plug the drive in, or pass --internal." >&2
    exit 1
  fi
fi

export AI_VOLUME_PATH="$BASE_VOL"
export STORAGE_MODE="$STORAGE_MODE_ARG"
INSTALL_DIR="${AI_VOLUME_PATH}/AI/Projects/Mac/outputs/orca-multi-agent-blueprint"

echo "=================================================================="
echo " 🚀 Orca Multi-Agent Blueprint — bootstrap"
echo " Storage mode   : ${STORAGE_MODE}"
echo " Workspace root : ${AI_VOLUME_PATH}"
echo " Blueprint dir  : ${INSTALL_DIR}"
echo " deploy.sh args : ${ARGS[*]}"
echo "=================================================================="

# /usr/bin/git is a stub that opens the Command Line Tools installer when they are missing
have_git() {
  local git_bin="${commands[git]:-}"
  [[ -n "$git_bin" ]] || return 1
  if [[ "${git_bin:A}" == /usr/bin/* ]] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

if [[ -f "${INSTALL_DIR}/deploy.sh" ]]; then
  if [[ -d "${INSTALL_DIR}/.git" ]] && have_git; then
    if [[ -z "$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null)" ]]; then
      echo "--> Updating the blueprint checkout (fast-forward only)..."
      git -C "$INSTALL_DIR" pull --ff-only --quiet origin main \
        || echo "    ⚠️ Could not update (offline or diverged); continuing with the existing checkout."
    else
      echo "--> The blueprint checkout has local changes; not updating it."
    fi
  else
    echo "--> Using the existing blueprint at ${INSTALL_DIR}"
  fi
else
  if ! have_git; then
    echo "❌ Error: git is needed to download the blueprint and it is not installed yet." >&2
    echo "   Run: xcode-select --install   (then run this command again)" >&2
    exit 1
  fi
  echo "--> Cloning ${REPO_URL} into ${INSTALL_DIR}..."
  mkdir -p "${INSTALL_DIR:h}"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
exec /bin/zsh ./deploy.sh "${ARGS[@]}"
