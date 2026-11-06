#!/usr/bin/env zsh
# ==============================================================================
# Orca Multi-Agent Blueprint — Universal One-Line Bootstrapper
#
# Run on ANY new Mac or existing multi-project Mac via:
#   curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh
#
# Or preview in dry-run mode:
#   curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --dry-run
# ==============================================================================
set -euo pipefail

REPO_URL="https://github.com/Rem021/orca-multi-agent-blueprint.git"
DEPLOY_MODE="--auto"
CUSTOM_VOLUME=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DEPLOY_MODE="--dry-run"; shift ;;
    --apply|--auto) DEPLOY_MODE="--auto"; shift ;;
    --volume) CUSTOM_VOLUME="$2"; shift 2 ;;
    *) shift ;;
  esac
done

# 1. Auto-detect target workspace volume (External SSD under /Volumes/* or ~/AI-Workspace)
if [[ -n "$CUSTOM_VOLUME" ]]; then
  BASE_VOL="$CUSTOM_VOLUME"
elif [[ -n "${AI_VOLUME_PATH:-}" && -d "${AI_VOLUME_PATH}" ]]; then
  BASE_VOL="${AI_VOLUME_PATH}"
else
  # Scan /Volumes for any external drive that already has an AI/ folder
  FOUND_VOL=""
  for v in /Volumes/*(N/); do
    local vname="${v:t}"
    [[ "$vname" == "Macintosh HD"* || "$vname" == "Preboot" || "$vname" == "Recovery" || "$vname" == "VM" || "$vname" == "Update" ]] && continue
    if [[ -d "${v}/AI" ]]; then
      FOUND_VOL="$v"
      break
    fi
  done
  BASE_VOL="${FOUND_VOL:-$HOME/AI-Workspace}"
fi

export AI_VOLUME_PATH="$BASE_VOL"
INSTALL_DIR="${AI_VOLUME_PATH}/AI/Projects/Mac/outputs/orca-multi-agent-blueprint"

echo "=================================================================="
echo " 🚀 Orca Multi-Agent Blueprint — One-Line Bootstrap"
echo " Target Workspace Root : ${AI_VOLUME_PATH}"
echo " Blueprint Install Dir : ${INSTALL_DIR}"
echo " Execution Mode        : ${DEPLOY_MODE}"
echo "=================================================================="

mkdir -p "$(dirname "$INSTALL_DIR")"

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  echo "--> Updating existing blueprint checkout at ${INSTALL_DIR}..."
  git -C "$INSTALL_DIR" fetch origin main --quiet || true
  git -C "$INSTALL_DIR" pull --ff-only origin main --quiet || true
else
  if [[ -d "$INSTALL_DIR" ]]; then
    echo "--> Existing blueprint directory found at ${INSTALL_DIR}"
  else
    echo "--> Cloning ${REPO_URL} into ${INSTALL_DIR}..."
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
fi

cd "$INSTALL_DIR"
chmod +x ./deploy.sh ./scripts/*.sh ./scripts/*.py ./tests/*.py 2>/dev/null || true
exec ./deploy.sh "$DEPLOY_MODE"
