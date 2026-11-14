#!/usr/bin/env zsh
# ==============================================================================
# Orca Multi-Agent Blueprint — One-Click Deployment & Auto-Discovery Engine
# Supports: --auto (zero-config full setup), --apply, --dry-run, --sync-profiles, --doctor,
#           --pack-to-ssd / --restore-from-ssd (cross-Mac migration), --setup-backups
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/config.env"

MODE="dry-run"
RENDER_RULES="false"
FAST_CONFIG_ONLY="false"
FORCE_APPS="false"
FORCE="false"
ENABLE_AGENTS_ARG=""
HOLD_AGENTS_ARG=""
PRE_PUSH_REPO=""
VOLUME_OVERRIDE=""
STORAGE_MODE_OVERRIDE=""
RESTORED="false"

need_value() {
  if [[ $# -lt 2 || -z "$2" || "$2" == --* ]]; then
    echo "Option $1 needs a value (use --help)" >&2
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --auto) MODE="auto"; shift ;;
    --apply) MODE="apply"; shift ;;
    --dry-run) MODE="dry-run"; shift ;;
    --pack-to-ssd) MODE="pack-to-ssd"; shift ;;
    --pack-dry-run) MODE="pack-dry-run"; shift ;;
    --restore-from-ssd) MODE="restore-from-ssd"; shift ;;
    --restore-dry-run) MODE="restore-dry-run"; shift ;;
    --migration-status) MODE="migration-status"; shift ;;
    --refresh-guide) MODE="refresh-guide"; shift ;;
    --agents) MODE="agents"; shift ;;
    --setup-backups) MODE="setup-backups"; shift ;;
    --backup-status) MODE="backup-status"; shift ;;
    --install-pre-push) need_value "$@"; MODE="install-pre-push"; PRE_PUSH_REPO="$2"; shift 2 ;;
    --storage) need_value "$@"; STORAGE_MODE_OVERRIDE="$2"; shift 2 ;;
    --internal) STORAGE_MODE_OVERRIDE="internal"; shift ;;
    --external) STORAGE_MODE_OVERRIDE="external"; shift ;;
    --volume) need_value "$@"; VOLUME_OVERRIDE="$2"; shift 2 ;;
    --fast-config-only) FAST_CONFIG_ONLY="true"; shift ;;
    --force-apps) FORCE_APPS="true"; shift ;;
    --force) FORCE="true"; shift ;;
    --enable-agents) need_value "$@"; ENABLE_AGENTS_ARG="$2"; shift 2 ;;
    --hold-agents) need_value "$@"; HOLD_AGENTS_ARG="$2"; MODE="agents"; shift 2 ;;
    --sync-profiles) MODE="sync-profiles"; shift ;;
    --doctor) MODE="doctor"; shift ;;
    --render-rules) RENDER_RULES="true"; shift ;;
    --config) need_value "$@"; CONFIG_FILE="$2"; shift 2 ;;
    -h|--help)
      cat <<'EOF'
Usage: ./deploy.sh [OPTIONS]

Deploy
  --auto                 Zero-config full deployment: auto-discovers volume, UUID, simulators,
                         existing projects & agents, generates config.env, installs CLI tools,
                         configures automated backups, and syncs all Orca profiles
  --apply                Execute full deployment using ./config.env (or ./config.example.env)
  --dry-run              Preview directories, tools, symlinks, backups & project adoptions (default)
  --sync-profiles        Re-synchronize skills, MCPs, AGENTS.md/CLAUDE.md and hooks across all profiles
  --render-rules         Force re-render ~/.codex/AGENTS.md, ~/.claude/CLAUDE.md, ~/.gemini/GEMINI.md
                         (backs up the existing files)
  --doctor               Read-only health audit; `summary.attention` lists what needs action
  --install-pre-push <repo>
                         Install the agent-guard pre-push hook into one Git repository

Storage
  --storage <mode>       external : everything under /Volumes/<SSD>/AI
                         internal : everything under ~/AI-Workspace/AI
                         auto     : external SSD when mounted, otherwise internal (default)
  --external | --internal  Shorthand for --storage external | internal
  --volume <path>        Volume to use for pack / restore / deploy
  --config <path>        Path to a custom config.env

Backups
  --setup-backups        Install/update the 7 backup & volume-watchdog LaunchAgents
  --backup-status        Render and print the backup & storage health report

Migration between Macs
  --pack-to-ssd          Pack agent state, SQLite DBs, accounts, SSH/Git/Zsh configs, LaunchAgents
                         and core apps onto the volume (skips VM bundles & CoreSimulator devices)
  --pack-dry-run         Preview what --pack-to-ssd would archive
  --restore-from-ssd     Restore the snapshot onto this Mac, then run the full deployment
  --restore-dry-run      Preview what --restore-from-ssd would restore
  --migration-status     Show metadata & integrity status of the latest snapshot
  --refresh-guide        Rewrite the restore guide/launchers on the volume from the existing snapshot
                         (no re-pack; snapshot data is not touched)
  --fast-config-only     With --pack-to-ssd: skip multi-GB raw session logs and .app bundles
  --force-apps           With --pack-to-ssd: resync .app bundles even when unchanged
  --force                With --pack-to-ssd: replace a snapshot packed on a different Mac
                         With --restore-from-ssd: bypass preflight blocks (running apps, macOS
                         version, pack state, free space)
  --enable-agents <list> With --restore-from-ssd: comma-separated third-party LaunchAgent labels to
                         start instead of holding

Third-party LaunchAgents
  --agents               List third-party LaunchAgents and whether they run, are held, or start at
                         the next login
  --agents --enable-agents <list>   Enable and load the listed labels
  --hold-agents <list>   Disable and unload the listed labels

Environment
  ORCA_PYTHON            Python 3.9+ interpreter to use instead of the auto-detected one

  -h, --help             Show this help message
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1 (use --help)" >&2
      exit 1
      ;;
  esac
done

if [[ -n "$VOLUME_OVERRIDE" ]]; then
  export AI_VOLUME_PATH="$VOLUME_OVERRIDE"
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
  if [[ "$STORAGE_MODE_OVERRIDE" == "internal" && -z "$VOLUME_OVERRIDE" ]]; then
    export AI_VOLUME_PATH="${INTERNAL_WORKSPACE_PATH:-$HOME/AI-Workspace}"
  elif [[ -z "${AI_VOLUME_PATH:-}" ]]; then
    for v in /Volumes/*(N/); do
      vname="${v:t}"
      [[ "$vname" == "Macintosh HD"* || "$vname" == "Preboot" || "$vname" == "Recovery" || "$vname" == "VM" || "$vname" == "Update" || "$vname" == "com.apple."* ]] && continue
      if [[ -d "${v}/AI" ]]; then
        export AI_VOLUME_PATH="$v"
        break
      fi
    done
  fi
fi

export PATH="$HOME/.local/bin:$PATH"

if [[ -f "$CONFIG_FILE" && -z "${AI_VOLUME_PATH:-}" ]]; then
  AI_VOLUME_PATH="$(zsh -c "source ${(q)CONFIG_FILE} >/dev/null 2>&1; printf '%s' \"\${AI_VOLUME_PATH:-}\"")"
  [[ -n "$AI_VOLUME_PATH" ]] && export AI_VOLUME_PATH
fi

# ------------------------------------------------------------------------------
# Python 3 resolution
# A brand-new Mac only has the /usr/bin/python3 stub, which opens the Command Line Tools installer
# instead of running. Everything below therefore calls whichever interpreter passes python_ok:
# one that is already installed, or the relocatable uv-managed CPython inside the migration snapshot
# (it runs straight from the SSD, no installation and no password needed).
# ------------------------------------------------------------------------------
PYTHON_BIN=""
PY_SHIM_DIR=""

have_clt() {
  /usr/bin/xcode-select -p >/dev/null 2>&1
}

python_ok() {
  local py="$1"
  [[ -n "$py" && -x "$py" ]] || return 1
  if [[ "${py:A}" == /usr/bin/* ]] && ! have_clt; then
    return 1
  fi
  "$py" -c 'import sys, ssl, sqlite3, plistlib; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

resolve_python() {
  local c
  local -a candidates
  candidates=(
    "${ORCA_PYTHON:-}"
    "${commands[python3]:-}"
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3
    /opt/homebrew/bin/python3
    /usr/local/bin/python3
    /usr/bin/python3
    "$HOME"/.local/share/uv/python/cpython-3.*/bin/python3(NnOn)
  )
  if [[ -n "${AI_VOLUME_PATH:-}" ]]; then
    candidates+=("${AI_VOLUME_PATH}"/AI/Migration/latest/home/.local/share/uv/python/cpython-3.*/bin/python3(NnOn))
  fi
  for c in "${candidates[@]}"; do
    if python_ok "$c"; then
      PYTHON_BIN="$c"
      return 0
    fi
  done
  return 1
}

# Last resort: the snapshot's Python.framework is built for /Library/Frameworks and cannot run from
# the SSD, so it has to be copied there first (needs an administrator password).
install_bundled_python_framework() {
  local src="${AI_VOLUME_PATH:-}/AI/Migration/latest/system/Python.framework"
  [[ -n "${AI_VOLUME_PATH:-}" && -d "${src}/Versions" ]] || return 1
  [[ "$MODE" == "restore-from-ssd" || "$MODE" == "apply" || "$MODE" == "auto" ]] || return 1
  [[ -t 0 && ! -e /Library/Frameworks/Python.framework ]] || return 1
  echo "--> No usable Python 3 found. Installing the bundled Python.framework into /Library/Frameworks"
  echo "    (macOS asks for your login password once)..."
  sudo /bin/mkdir -p /Library/Frameworks && sudo /usr/bin/rsync -aH "$src" /Library/Frameworks/
}

drop_python_shim() {
  if [[ -n "$PY_SHIM_DIR" ]]; then
    path=("${(@)path:#$PY_SHIM_DIR}")
    /bin/rm -f "${PY_SHIM_DIR}/python3"
    /bin/rmdir "$PY_SHIM_DIR" 2>/dev/null || true
    PY_SHIM_DIR=""
    rehash
  fi
}
trap drop_python_shim EXIT

# Make the bare `python3` used by every script below resolve to the chosen interpreter
activate_python() {
  drop_python_shim
  if [[ "${commands[python3]:-}" != "$PYTHON_BIN" ]]; then
    PY_SHIM_DIR="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/orca-blueprint-py.XXXXXX")"
    /bin/ln -s "$PYTHON_BIN" "${PY_SHIM_DIR}/python3"
    path=("$PY_SHIM_DIR" "${path[@]}")
    rehash
  fi
}

# The backup LaunchAgents and agent hooks call a bare `python3` on
# ~/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin. When nothing on that list works, link the
# interpreter this run uses (only if it lives on the internal disk) into ~/.local/bin.
ensure_persistent_python() {
  local c
  for c in "$HOME/.local/bin/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    python_ok "$c" && return 0
  done
  if [[ -n "$PYTHON_BIN" && "${PYTHON_BIN:A}" != /Volumes/* && ! -e "$HOME/.local/bin/python3" ]]; then
    /bin/mkdir -p "$HOME/.local/bin"
    /bin/ln -sfn "$PYTHON_BIN" "$HOME/.local/bin/python3"
    echo "    ✅ Linked ~/.local/bin/python3 -> ${PYTHON_BIN}"
    rehash
    return 0
  fi
  echo "⚠️  No persistent python3 for background jobs. Install one (xcode-select --install, python.org, or uv)." >&2
}

if ! resolve_python; then
  if install_bundled_python_framework; then
    resolve_python || true
  fi
fi
if [[ -z "$PYTHON_BIN" ]]; then
  cat >&2 <<'EOF'
❌ Error: no usable Python 3 (3.9 or newer) was found on this Mac or in the migration snapshot.
   Install one of the following, then run this command again:
     • Apple Command Line Tools :  xcode-select --install
     • python.org installer     :  https://www.python.org/downloads/macos/
     • uv                       :  curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install
   or point ORCA_PYTHON at an existing interpreter.
EOF
  exit 1
fi
activate_python

DISCOVERED_CACHE=""
if [[ ! -f "$CONFIG_FILE" ]]; then
  if [[ "$MODE" == "auto" ]]; then
    echo "--> Auto-discovering host volume, iOS simulators, existing projects & agent fleet..."
    DISCOVER_INIT_ARGS=(--apply --write-config "$CONFIG_FILE")
    [[ -n "$STORAGE_MODE_OVERRIDE" ]] && DISCOVER_INIT_ARGS+=(--storage-mode "$STORAGE_MODE_OVERRIDE")
    DISCOVERED_CACHE="$(python3 "${SCRIPT_DIR}/scripts/discover_and_adopt.py" "${DISCOVER_INIT_ARGS[@]}")"
  else
    CONFIG_FILE="${SCRIPT_DIR}/config.example.env"
  fi
fi

set -a
source "$CONFIG_FILE"
set +a

if [[ -n "$VOLUME_OVERRIDE" ]]; then
  export AI_VOLUME_PATH="$VOLUME_OVERRIDE"
fi

SNAPSHOT_VOLUME_PATH="${VOLUME_OVERRIDE:-${AI_VOLUME_PATH:-}}"

if [[ -n "$STORAGE_MODE_OVERRIDE" ]]; then
  export STORAGE_MODE="$STORAGE_MODE_OVERRIDE"
else
  export STORAGE_MODE="${STORAGE_MODE:-auto}"
fi
export INTERNAL_WORKSPACE_PATH="${INTERNAL_WORKSPACE_PATH:-$HOME/AI-Workspace}"

# AI_VOLUME_UUID sentinels: AUTO = accept whichever volume is mounted there, LOCAL = internal disk
if [[ "$STORAGE_MODE" == "internal" ]]; then
  if [[ "$MODE" == "restore-from-ssd" || "$MODE" == "restore-dry-run" ]]; then
    echo "⚠️  WARNING: --internal mode only creates empty ~/AI-Workspace/AI directories; it does NOT copy AI/Projects from the external SSD!" >&2
  fi
  export AI_VOLUME_PATH="$INTERNAL_WORKSPACE_PATH"
  export AI_VOLUME_UUID="LOCAL"
elif [[ "$STORAGE_MODE" == "auto" && ( -z "${AI_VOLUME_PATH:-}" || "${AI_VOLUME_PATH:-}" == "/Volumes/AI_Workspace" ) && ! -d "/Volumes/AI_Workspace" ]]; then
  export AI_VOLUME_PATH="$INTERNAL_WORKSPACE_PATH"
  export AI_VOLUME_UUID="LOCAL"
else
  export AI_VOLUME_PATH="${AI_VOLUME_PATH:-/Volumes/AI_Workspace}"
  export AI_VOLUME_UUID="${AI_VOLUME_UUID:-AUTO}"
fi

export SUPERVISOR_AGENT="${SUPERVISOR_AGENT:-muse}"
export WORKER_AGENTS="${WORKER_AGENTS:-claude,codex,antigravity,dsh}"
export DEFAULT_BRANCH_PREFIX="${DEFAULT_BRANCH_PREFIX:-codex}"
export ENABLE_XCODE_MCP="${ENABLE_XCODE_MCP:-true}"
export PRIMARY_SIMULATOR_UDID="${PRIMARY_SIMULATOR_UDID:-11111111-2222-3333-4444-555555555555}"
export PRIMARY_SIMULATOR_NAME="${PRIMARY_SIMULATOR_NAME:-iPhone 18 Pro Max}"
export SECONDARY_SIMULATOR_UDID="${SECONDARY_SIMULATOR_UDID:-66666666-7777-8888-9999-AAAAAAAAAAAA}"
export SECONDARY_SIMULATOR_NAME="${SECONDARY_SIMULATOR_NAME:-iPhone 18 Pro Max (Secondary)}"
export ENABLE_AUTOMATED_BACKUPS="${ENABLE_AUTOMATED_BACKUPS:-true}"
export BACKUP_ICLOUD_DIR="${BACKUP_ICLOUD_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/AI备份/每晚小包}"
export BACKUP_NAS_MOUNT="${BACKUP_NAS_MOUNT:-}"
export BACKUP_NAS_SMB_MATCH="${BACKUP_NAS_SMB_MATCH:-}"
export BACKUP_RESTIC_REPO="${BACKUP_RESTIC_REPO:-${AI_VOLUME_PATH}/AI/Backups/restic}"
export BACKUP_MEDIA_PATH="${BACKUP_MEDIA_PATH:-${AI_VOLUME_PATH}/Work}"
export BACKUP_HOST_TAG="${BACKUP_HOST_TAG:-}"
export EXTRA_MIGRATION_PATHS="${EXTRA_MIGRATION_PATHS:-}"
export EXTRA_RESTIC_PATHS="${EXTRA_RESTIC_PATHS:-}"
export EXTRA_BUNDLED_APPS="${EXTRA_BUNDLED_APPS:-}"
export EXTRA_KEYCHAIN_SERVICES="${EXTRA_KEYCHAIN_SERVICES:-}"

# Handle standalone backup & migration modes
VOLUME_ARGS=()
[[ -n "$SNAPSHOT_VOLUME_PATH" ]] && VOLUME_ARGS+=("--volume" "$SNAPSHOT_VOLUME_PATH")

PACK_EXTRA_ARGS=("${VOLUME_ARGS[@]}")
[[ "$FAST_CONFIG_ONLY" == "true" ]] && PACK_EXTRA_ARGS+=("--fast-config-only")
[[ "$FORCE_APPS" == "true" ]] && PACK_EXTRA_ARGS+=("--force-apps")
[[ "$FORCE" == "true" ]] && PACK_EXTRA_ARGS+=("--force")

RESTORE_EXTRA_ARGS=("${VOLUME_ARGS[@]}")
[[ "$FORCE" == "true" ]] && RESTORE_EXTRA_ARGS+=("--force")
[[ -n "$ENABLE_AGENTS_ARG" ]] && RESTORE_EXTRA_ARGS+=("--enable-agents" "$ENABLE_AGENTS_ARG")

MIGRATE_PY="${SCRIPT_DIR}/scripts/migrate.py"

case "$MODE" in
  setup-backups)
    python3 "${SCRIPT_DIR}/scripts/setup_backups.py" --apply
    exit 0
    ;;
  backup-status)
    zsh "${SCRIPT_DIR}/scripts/codex-storage/status.sh"
    cat "${HOME}/Library/Application Support/Codex Storage/backup-status.md"
    exit 0
    ;;
  migration-status)
    python3 "$MIGRATE_PY" status "${VOLUME_ARGS[@]}"
    exit 0
    ;;
  refresh-guide)
    python3 "$MIGRATE_PY" guide --apply "${VOLUME_ARGS[@]}"
    exit 0
    ;;
  agents)
    AGENTS_ARGS=()
    [[ -n "$ENABLE_AGENTS_ARG" ]] && AGENTS_ARGS+=("--enable" "$ENABLE_AGENTS_ARG")
    [[ -n "$HOLD_AGENTS_ARG" ]] && AGENTS_ARGS+=("--hold" "$HOLD_AGENTS_ARG")
    python3 "$MIGRATE_PY" agents "${AGENTS_ARGS[@]}"
    exit 0
    ;;
  install-pre-push)
    python3 "${SCRIPT_DIR}/scripts/guard.py" --mode install-pre-push --repo "$PRE_PUSH_REPO"
    exit 0
    ;;
  pack-dry-run)
    python3 "$MIGRATE_PY" pack --dry-run "${PACK_EXTRA_ARGS[@]}"
    exit 0
    ;;
  pack-to-ssd)
    python3 "$MIGRATE_PY" pack --apply "${PACK_EXTRA_ARGS[@]}"
    exit 0
    ;;
  restore-dry-run)
    python3 "$MIGRATE_PY" restore --dry-run "${RESTORE_EXTRA_ARGS[@]}"
    exit 0
    ;;
  restore-from-ssd)
    python3 "$MIGRATE_PY" restore --apply "${RESTORE_EXTRA_ARGS[@]}"
    RESTORED="true"
    MODE="apply"
    # The restore just put the interpreters of the old Mac in place: prefer one on the internal disk
    # over the copy on the SSD for the rest of this run
    drop_python_shim
    resolve_python
    activate_python
    ;;
  auto)
    MODE="apply"
    ;;
  doctor)
    python3 "${SCRIPT_DIR}/scripts/doctor.py"
    exit 0
    ;;
  sync-profiles)
    python3 "${SCRIPT_DIR}/scripts/sync_profiles.py" --apply
    exit 0
    ;;
esac

echo "=================================================================="
echo " Orca Multi-Agent Blueprint Deployment"
echo " Mode              : ${MODE}"
echo " Storage Mode      : ${STORAGE_MODE}"
echo " Config File       : ${CONFIG_FILE}"
echo " Workspace Root    : ${AI_VOLUME_PATH}"
echo " Python            : ${PYTHON_BIN}"
echo " Supervisor Agent  : ${SUPERVISOR_AGENT}"
echo " Worker Fleet      : ${WORKER_AGENTS}"
echo "=================================================================="

# 1. Verify Volume / Workspace Root
if [[ "$AI_VOLUME_PATH" == /Volumes/* && "$MODE" != "dry-run" ]]; then
  if [[ ! -d "$AI_VOLUME_PATH" ]]; then
    echo "❌ Error: External volume ${AI_VOLUME_PATH} is not mounted. Aborting to prevent mountpoint stub creation." >&2
    exit 1
  fi
  DETECTED_UUID="$(diskutil info -plist "$AI_VOLUME_PATH" 2>/dev/null | plutil -extract VolumeUUID raw - 2>/dev/null || true)"
  if [[ -n "$AI_VOLUME_UUID" && "$AI_VOLUME_UUID" != "AUTO" && "$AI_VOLUME_UUID" != "LOCAL" && "$AI_VOLUME_UUID" != "INTERNAL" && "$DETECTED_UUID" != "$AI_VOLUME_UUID" ]]; then
    echo "❌ Error: Volume UUID mismatch on ${AI_VOLUME_PATH} (expected ${AI_VOLUME_UUID}, got ${DETECTED_UUID})." >&2
    exit 1
  fi
  export AI_VOLUME_UUID="${DETECTED_UUID:-$AI_VOLUME_UUID}"
  echo "✅ External SSD verified: ${AI_VOLUME_PATH} (UUID: ${AI_VOLUME_UUID:-N/A})"
elif [[ "$AI_VOLUME_PATH" != /Volumes/* ]]; then
  export AI_VOLUME_UUID="LOCAL"
  echo "✅ Internal Disk Workspace verified: ${AI_VOLUME_PATH}"
fi

run_step() {
  if [[ "$MODE" == "dry-run" ]]; then
    echo "[DRY-RUN] $*"
  else
    eval "$@"
  fi
}

# 2. Directory Taxonomy Initialization
DIRS=(
  "${AI_VOLUME_PATH}/AI/Projects"
  "${AI_VOLUME_PATH}/AI/Orca/Worktrees"
  "${AI_VOLUME_PATH}/AI/Codex/Worktrees"
  "${AI_VOLUME_PATH}/AI/Claude/Worktrees"
  "${AI_VOLUME_PATH}/AI/Archive"
  "${AI_VOLUME_PATH}/AI/Data/BuildCache/SourcePackages"
  "${AI_VOLUME_PATH}/AI/Data/SimulatorCoordination"
  "${HOME}/.local/bin"
  "${HOME}/.agents/skills"
)
if [[ -z "${BACKUP_NAS_MOUNT:-}" && -z "${BACKUP_NAS_SMB_MATCH:-}" && "${BACKUP_RESTIC_REPO:-}" == "${AI_VOLUME_PATH}/AI/Backups/restic" ]]; then
  DIRS+=("${AI_VOLUME_PATH}/AI/Backups/restic")
fi

echo "--> Step 1/8: Ensuring standard 4-zone directory taxonomy..."
for d in "${DIRS[@]}"; do
  run_step "mkdir -p ${(q)d}"
done

# 3. Non-Interactive Shell PATH & Git Global Optimizations
echo "--> Step 2/8: Configuring ~/.zshenv, python3 & Git multi-worktree acceleration..."
if [[ ! -f "${HOME}/.zshenv" ]] || ! grep -q '.local/bin' "${HOME}/.zshenv" 2>/dev/null; then
  run_step "printf '%s\n' 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshenv"
fi
if [[ "$MODE" == "apply" ]]; then
  ensure_persistent_python
fi

if [[ "${ENABLE_GIT_ACCELERATION:-true}" == "true" ]] && have_clt; then
  run_step "git config --global feature.manyFiles true"
  run_step "git config --global core.fsmonitor true"
  run_step "git config --global rerere.enabled true"
  run_step "git config --global worktree.useRelativePaths true"
  run_step "git config --global branch.sort -committerdate"
fi

# 4. Native CLI Toolchain Installation (Zero-dependency curl + GitHub API)
if [[ "${INSTALL_CLI_TOOLCHAIN:-true}" == "true" && "$MODE" == "apply" ]]; then
  echo "--> Step 3/8: Checking & installing native CLI toolchain in ~/.local/bin..."
  "${SCRIPT_DIR}/scripts/install_cli_tools.sh"
elif [[ "$MODE" == "dry-run" ]]; then
  echo "--> Step 3/8: [DRY-RUN] Would check/install rg, fd, ast-grep, just, xcbeautify, scc, repomix into ~/.local/bin"
fi

# 5. Install Orca Built-in Skills, Guardrails & ast-grep Skill
echo "--> Step 4/8: Installing Orca built-in skills, PreToolUse guardrail & ast-grep skill..."
if [[ "$MODE" == "apply" && -x "/Applications/Orca.app/Contents/Resources/bin/orca" && ! -d "${HOME}/.agents/skills/orca-cli" ]]; then
  /Applications/Orca.app/Contents/Resources/bin/orca skills install >/dev/null 2>&1 || true
fi
run_step "mkdir -p ~/.agents/skills/ast-grep"
run_step "cp -f ${(q)SCRIPT_DIR}/skills/ast-grep/SKILL.md ~/.agents/skills/ast-grep/SKILL.md"
# The guard runs as a hook of every live agent session: swap it in with a rename so no session
# ever executes a half-written file
run_step "cp -f ${(q)SCRIPT_DIR}/scripts/guard.py ~/.local/bin/.agent-guard.new"
run_step "chmod +x ~/.local/bin/.agent-guard.new"
run_step "mv -f ~/.local/bin/.agent-guard.new ~/.local/bin/agent-guard"
run_step "mkdir -p ~/.agents/skills/agent-governor/scripts"
run_step "cp -f ${(q)SCRIPT_DIR}/skills/agent-governor/SKILL.md ~/.agents/skills/agent-governor/SKILL.md"
run_step "cp -f ${(q)SCRIPT_DIR}/scripts/agent_governor.py ~/.agents/skills/agent-governor/scripts/agent_governor.py"
run_step "chmod +x ~/.agents/skills/agent-governor/scripts/agent_governor.py"
run_step "cp -f ${(q)SCRIPT_DIR}/scripts/agent_governor.py ~/.local/bin/.agent-governor.new"
run_step "chmod +x ~/.local/bin/.agent-governor.new"
run_step "mv -f ~/.local/bin/.agent-governor.new ~/.local/bin/agent-governor"
run_step "cp -f ${(q)SCRIPT_DIR}/scripts/ship.py ~/.local/bin/.agent-ship.new"
run_step "chmod +x ~/.local/bin/.agent-ship.new"
run_step "mv -f ~/.local/bin/.agent-ship.new ~/.local/bin/agent-ship"
# The installed guard has no checkout next to it and cannot read config.env: hand it what it enforces
if [[ "$MODE" == "apply" ]]; then
  mkdir -p "${HOME}/.orca"
  GUARD_ENV_TMP="$(mktemp "${HOME}/.orca/.guard.env.XXXXXX")"
  {
    print -r -- "# Generated by deploy.sh from ${CONFIG_FILE:t}; read by ~/.local/bin/agent-guard. Edits are overwritten."
    for key in STORAGE_MODE AI_VOLUME_PATH AI_VOLUME_UUID BACKUP_MEDIA_PATH PRIMARY_SIMULATOR_UDID SECONDARY_SIMULATOR_UDID GUARD_PROTECTED_BRANCHES GUARD_RESTRICTED_BRANCHES; do
      value="${(P)key:-}"
      # Skip unset keys and the placeholder simulator IDs of config.example.env
      [[ -z "$value" || "$value" == 11111111-2222-* || "$value" == 66666666-7777-* ]] && continue
      print -r -- "${key}=\"${value}\""
    done
  } > "$GUARD_ENV_TMP"
  mv -f "$GUARD_ENV_TMP" "${HOME}/.orca/guard.env"
else
  echo "[DRY-RUN] write ~/.orca/guard.env (volume, simulators and branch rules for agent-guard)"
fi

# 6. Auto-Discover & Non-Destructively Adopt Existing Projects & Global Rules
echo "--> Step 5/8: Running smart project adoption & global rule check..."
if [[ -n "$DISCOVERED_CACHE" && "$RENDER_RULES" == "false" ]]; then
  echo "$DISCOVERED_CACHE"
else
  DISCOVER_FLAGS=("--${MODE}" "--storage-mode" "${STORAGE_MODE}")
  [[ "$RENDER_RULES" == "true" ]] && DISCOVER_FLAGS+=("--render-rules")
  python3 "${SCRIPT_DIR}/scripts/discover_and_adopt.py" "${DISCOVER_FLAGS[@]}"
fi

# 7. Install & Bootstrap Automated Multi-Tier Daily/Hourly/Weekly Backup & Storage Watchdog System
if [[ "$ENABLE_AUTOMATED_BACKUPS" == "true" ]]; then
  echo "--> Step 6/8: Configuring automated daily/hourly/weekly backups & storage watchdog..."
  python3 "${SCRIPT_DIR}/scripts/setup_backups.py" "--${MODE}"
else
  echo "--> Step 6/8: Skipped automated backups (ENABLE_AUTOMATED_BACKUPS=${ENABLE_AUTOMATED_BACKUPS}); run ./deploy.sh --setup-backups to install them later."
fi

# 8. Synchronize All Agent Profiles & Run Verification Tests
echo "--> Step 7/8: Synchronizing skills, MCPs, rules & hooks across all Orca profiles..."
python3 "${SCRIPT_DIR}/scripts/sync_profiles.py" "--${MODE}"

echo "--> Step 8/8: Running verification tests..."
python3 "${SCRIPT_DIR}/tests/test_guard.py"
python3 "${SCRIPT_DIR}/tests/test_migrate.py"
python3 "${SCRIPT_DIR}/tests/test_backup_scripts.py"
python3 "${SCRIPT_DIR}/tests/test_doctor.py"
python3 "${SCRIPT_DIR}/tests/test_ship.py"
python3 "${SCRIPT_DIR}/tests/test_agent_governor.py"
if [[ -x "/Applications/Orca.app/Contents/Resources/bin/orca" ]]; then
  /Applications/Orca.app/Contents/Resources/bin/orca plugin test "${SCRIPT_DIR}/plugins/orca-naming-guard" --json >/dev/null
fi

if [[ "$RESTORED" == "true" ]]; then
  echo "--> Post-restore health check (./deploy.sh --doctor prints the full report)..."
  python3 "${SCRIPT_DIR}/scripts/doctor.py" --summary || true
fi

echo "✅ Deployment (${MODE}) completed successfully."
