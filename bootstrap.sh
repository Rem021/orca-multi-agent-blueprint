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
#   zsh "/Volumes/<SSD>/Restore on New Mac.command"
#
# Everything runs from main(), called on the last line: if the download stops half-way, the shell
# reads an unfinished function and runs nothing.
# ==============================================================================

REPO_URL="${ORCA_BLUEPRINT_REPO_URL:-https://github.com/Rem021/orca-multi-agent-blueprint.git}"

need_value() {
  if [[ $# -lt 2 || "$2" == --* ]]; then
    echo "❌ Error: $1 needs a value." >&2
    exit 2
  fi
}

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

# /usr/bin/git is a stub that opens the Command Line Tools installer when they are missing
have_git() {
  local git_bin="${commands[git]:-}"
  [[ -n "$git_bin" ]] || return 1
  if [[ "${git_bin:A}" == /usr/bin/* ]] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

# owner/repo of a GitHub URL in https, ssh or scp form, lower-cased; "" for anything else
github_slug() {
  local url="${1%/}"
  url="${url%.git}"
  case "$url" in
    https://github.com/*) url="${url#https://github.com/}" ;;
    ssh://git@github.com/*) url="${url#ssh://git@github.com/}" ;;
    git@github.com:*) url="${url#git@github.com:}" ;;
    *) return 0 ;;
  esac
  [[ "$url" == ?*/?* && "$url" != */*/* ]] && print -r -- "${(L)url}"
}

# The origin URL of a checkout; read from .git/config when git itself is not usable yet
origin_url() {
  local dir="$1"
  if have_git; then
    git -C "$dir" config --get remote.origin.url 2>/dev/null || true
  elif [[ -f "$dir/.git/config" ]]; then
    /usr/bin/awk '/^\[remote "origin"\]/ { sect = 1; next } /^\[/ { sect = 0 }
      sect && $1 == "url" { sub(/^[^=]*=[ \t]*/, ""); print; exit }' "$dir/.git/config"
  fi
}

# Only a git checkout of the official repository is ever run. A folder at the install path that is
# anything else (a copy on someone's drive, a fork, a stray deploy.sh) is refused, not executed.
verify_checkout() {
  local dir="$1" origin
  if [[ ! -e "$dir/.git" ]]; then
    echo "❌ Error: ${dir} exists but is not a git checkout, so it cannot be verified." >&2
    echo "   Move it aside (or delete it) and run this command again to get a fresh copy of ${REPO_URL}." >&2
    exit 1
  fi
  origin="$(origin_url "$dir")"
  if [[ -z "$(github_slug "$REPO_URL")" || "$(github_slug "$origin")" != "$(github_slug "$REPO_URL")" ]]; then
    echo "❌ Error: ${dir} is a checkout of '${origin:-<no origin>}', not of ${REPO_URL}." >&2
    echo "   Refusing to run it. Move it aside, or set ORCA_BLUEPRINT_REPO_URL if you deploy from your own fork." >&2
    exit 1
  fi
}

main() {
  setopt localoptions err_exit no_unset pipe_fail
  local custom_volume="" storage_mode_arg="${STORAGE_MODE:-auto}" has_mode="false" base_vol v
  local found_vol="" first_ext="" vol_root install_dir legacy_dir
  local -a args
  args=("$@")

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --storage) need_value "$@"; storage_mode_arg="$2"; shift 2 ;;
      --internal) storage_mode_arg="internal"; shift ;;
      --external) storage_mode_arg="external"; shift ;;
      --volume) need_value "$@"; custom_volume="$2"; shift 2 ;;
      # Options with a value that are not modes: skip the value so it is never read as a flag
      --supervisor|--config|--enable-agents) need_value "$@"; shift 2 ;;
      --hold-agents|--install-pre-push)
        need_value "$@"; has_mode="true"; shift 2 ;;
      --dry-run|--apply|--auto|--doctor|--sync-profiles|--setup-backups|--backup-status|--pack-to-ssd|--pack-dry-run|--restore-from-ssd|--restore-dry-run|--migration-status|--refresh-guide|--agents|--help|-h)
        has_mode="true"; shift ;;
      *) shift ;;  # --no-supervisor, --force, --render-rules, ... pass through to deploy.sh
    esac
  done
  [[ "$has_mode" == "true" ]] || args=(--auto "${args[@]}")

  # 1. Where the workspace lives: an external SSD under /Volumes, or ~/AI-Workspace on the internal disk
  if [[ -n "$custom_volume" ]]; then
    base_vol="$custom_volume"
  elif [[ "$storage_mode_arg" == "internal" ]]; then
    base_vol="${INTERNAL_WORKSPACE_PATH:-$HOME/AI-Workspace}"
  elif [[ -n "${AI_VOLUME_PATH:-}" && -d "${AI_VOLUME_PATH}" ]]; then
    base_vol="${AI_VOLUME_PATH}"
  else
    # A volume that already has an AI/ folder is adopted; a blank drive only with --external
    for v in /Volumes/*(N/); do
      usable_volume "$v" || continue
      [[ -z "$first_ext" ]] && first_ext="$v"
      if [[ -d "${v}/AI" ]]; then
        found_vol="$v"
        break
      fi
    done
    if [[ "$storage_mode_arg" == "external" && -z "$found_vol" ]]; then
      found_vol="$first_ext"
      if [[ -z "$found_vol" ]]; then
        echo "❌ Error: --external was requested but no writable external volume is mounted." >&2
        exit 1
      fi
    fi
    base_vol="${found_vol:-${INTERNAL_WORKSPACE_PATH:-$HOME/AI-Workspace}}"
  fi

  # Never create a look-alike folder at the mount point of a drive that is not plugged in
  if [[ "$base_vol" == /Volumes/* ]]; then
    vol_root="/Volumes/${${base_vol#/Volumes/}%%/*}"
    if ! /sbin/mount | /usr/bin/grep -qF " on ${vol_root} ("; then
      echo "❌ Error: ${vol_root} is not a mounted volume. Plug the drive in, or pass --internal." >&2
      exit 1
    fi
  fi

  export AI_VOLUME_PATH="$base_vol"
  export STORAGE_MODE="$storage_mode_arg"
  # New installs live in AI/Projects/; a checkout at the earlier location keeps being used
  install_dir="${AI_VOLUME_PATH}/AI/Projects/orca-multi-agent-blueprint"
  legacy_dir="${AI_VOLUME_PATH}/AI/Projects/Mac/outputs/orca-multi-agent-blueprint"
  [[ ! -e "$install_dir" && -e "$legacy_dir" ]] && install_dir="$legacy_dir"

  echo "=================================================================="
  echo " 🚀 Orca Multi-Agent Blueprint — bootstrap"
  echo " Storage mode   : ${STORAGE_MODE}"
  echo " Workspace root : ${AI_VOLUME_PATH}"
  echo " Blueprint dir  : ${install_dir}"
  echo " deploy.sh args : ${args[*]}"
  echo "=================================================================="

  if [[ -e "$install_dir" ]]; then
    verify_checkout "$install_dir"
    if have_git; then
      if [[ -z "$(git -C "$install_dir" status --porcelain 2>/dev/null)" ]]; then
        echo "--> Updating the blueprint checkout (fast-forward only)..."
        git -C "$install_dir" pull --ff-only --quiet origin main \
          || echo "    ⚠️ Could not update (offline or diverged); continuing with the existing checkout."
      else
        echo "--> The blueprint checkout has local changes; not updating it."
      fi
    else
      echo "--> Using the existing blueprint checkout at ${install_dir} (git is not installed, so it is not updated)"
    fi
    [[ -f "$install_dir/deploy.sh" ]] || { echo "❌ Error: ${install_dir} has no deploy.sh." >&2; exit 1; }
  else
    if ! have_git; then
      echo "❌ Error: git is needed to download the blueprint and it is not installed yet." >&2
      echo "   Run: xcode-select --install   (then run this command again)" >&2
      exit 1
    fi
    echo "--> Cloning ${REPO_URL} into ${install_dir}..."
    mkdir -p "${install_dir:h}"
    git clone --depth 1 "$REPO_URL" "$install_dir"
  fi

  cd "$install_dir"
  exec /bin/zsh ./deploy.sh "${args[@]}"
}

main "$@"
