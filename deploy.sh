#!/usr/bin/env zsh
# ==============================================================================
# Orca Multi-Agent Blueprint — One-Click Deployment & Auto-Discovery Engine
# Supports: --auto (zero-config full setup), --apply, --dry-run, --sync-profiles, --doctor
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/config.env"

MODE="dry-run"
RENDER_RULES="false"
FAST_CONFIG_ONLY="false"
STORAGE_MODE_OVERRIDE=""

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
    --setup-backups) MODE="setup-backups"; shift ;;
    --backup-status) MODE="backup-status"; shift ;;
    --storage) STORAGE_MODE_OVERRIDE="$2"; shift 2 ;;
    --internal) STORAGE_MODE_OVERRIDE="internal"; shift ;;
    --external) STORAGE_MODE_OVERRIDE="external"; shift ;;
    --fast-config-only) FAST_CONFIG_ONLY="true"; shift ;;
    --sync-profiles) MODE="sync-profiles"; shift ;;
    --doctor) MODE="doctor"; shift ;;
    --render-rules) RENDER_RULES="true"; shift ;;
    --config) CONFIG_FILE="$2"; shift 2 ;;
    -h|--help)
      cat <<'EOF'
Usage: ./deploy.sh [OPTIONS]

Options:
  --auto                 Zero-config full deployment: auto-discovers volume, UUID, simulators,
                         existing projects & agents, generates config.env, installs CLI tools,
                         configures automated daily/hourly backups, and syncs all Orca profiles
  --storage <mode>       Choose AI file storage location:
                           external : Store all AI projects/worktrees/backups on External SSD (/Volumes/<SSD>/AI)
                           internal : Store all AI projects/worktrees/backups on Mac Internal Disk (~/AI-Workspace/AI)
                           auto     : Prefer mounted External SSD if present, otherwise Mac Internal Disk (Default)
  --external             Shorthand for --storage external
  --internal             Shorthand for --storage internal
  --setup-backups        Install/update the 7-job automated daily/hourly/weekly backup & volume watchdog system
  --backup-status        Render and display the unified backup & storage health report
  --pack-to-ssd          Losslessly pack all internal-disk Agent states, SQLite DBs, accounts,
                         SSH/Git/Zsh configs, LaunchAgents & local repos onto target workspace
                         (Zero-VM: skips claudevm.bundle & CoreSimulator devices)
  --pack-dry-run         Preview what --pack-to-ssd will archive without writing files
  --restore-from-ssd     One-click restore onto a new Mac from workspace + full --auto deploy
  --restore-dry-run      Preview what --restore-from-ssd will restore on the target Mac
  --migration-status     Show metadata & integrity status of the latest migration snapshot
  --fast-config-only     Combine with --pack-to-ssd to skip multi-GB raw session logs
  --apply                Execute full deployment using ./config.env (or ./config.example.env)
  --dry-run              Preview all directories, tools, symlinks, backups & project adoptions (Default)
  --sync-profiles        Re-synchronize skills, MCPs, AGENTS.md/CLAUDE.md, and hooks across all Orca profiles
  --doctor               Run read-only multi-agent health and parity audit
  --render-rules         Force re-render ~/.codex/AGENTS.md, ~/.claude/CLAUDE.md, ~/.gemini/GEMINI.md (backs up existing)
  --config <path>        Path to custom config.env
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

if [[ ! -f "$CONFIG_FILE" ]]; then
  if [[ "$STORAGE_MODE_OVERRIDE" == "internal" ]]; then
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

# Bootstrap Python 3.14 from /Library/Frameworks or from the SSD migration bundle
# so a brand-new Mac without Xcode Command Line Tools never triggers the /usr/bin/python3 stub dialog
if [[ -x "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3" ]]; then
  export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:$PATH"
elif [[ -n "${AI_VOLUME_PATH:-}" && -d "${AI_VOLUME_PATH}/AI/Migration/latest/system/Python.framework" ]]; then
  SSD_PY_FW="${AI_VOLUME_PATH}/AI/Migration/latest/system/Python.framework"
  export DYLD_FRAMEWORK_PATH="${AI_VOLUME_PATH}/AI/Migration/latest/system"
  export PYTHONHOME="${SSD_PY_FW}/Versions/3.14"
  export PATH="${SSD_PY_FW}/Versions/3.14/bin:$PATH"
fi

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

source "$CONFIG_FILE"

if [[ -n "$STORAGE_MODE_OVERRIDE" ]]; then
  export STORAGE_MODE="$STORAGE_MODE_OVERRIDE"
else
  export STORAGE_MODE="${STORAGE_MODE:-auto}"
fi
export INTERNAL_WORKSPACE_PATH="${INTERNAL_WORKSPACE_PATH:-$HOME/AI-Workspace}"

if [[ "$STORAGE_MODE" == "internal" ]]; then
  export AI_VOLUME_PATH="$INTERNAL_WORKSPACE_PATH"
  export AI_VOLUME_UUID="INTERNAL"
elif [[ "$STORAGE_MODE" == "auto" && ( -z "${AI_VOLUME_PATH:-}" || "${AI_VOLUME_PATH}" == "/Volumes/AI_Workspace" ) ]]; then
  if [[ ! -d "/Volumes/AI_Workspace" ]]; then
    export AI_VOLUME_PATH="$INTERNAL_WORKSPACE_PATH"
    export AI_VOLUME_UUID="INTERNAL"
  fi
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
export EXTRA_MIGRATION_PATHS="${EXTRA_MIGRATION_PATHS:-}"
export EXTRA_BUNDLED_APPS="${EXTRA_BUNDLED_APPS:-}"
export EXTRA_KEYCHAIN_SERVICES="${EXTRA_KEYCHAIN_SERVICES:-}"

# Handle standalone backup & migration modes
MIGRATE_EXTRA_ARGS=()
[[ "$FAST_CONFIG_ONLY" == "true" ]] && MIGRATE_EXTRA_ARGS+=("--fast-config-only")

if [[ "$MODE" == "setup-backups" ]]; then
  exec python3 "${SCRIPT_DIR}/scripts/setup_backups.py" --apply
elif [[ "$MODE" == "backup-status" ]]; then
  zsh "${SCRIPT_DIR}/scripts/codex-storage/status.sh"
  cat "${HOME}/Library/Application Support/Codex Storage/backup-status.md"
  exit 0
elif [[ "$MODE" == "migration-status" ]]; then
  exec python3 "${SCRIPT_DIR}/scripts/migrate.py" status
elif [[ "$MODE" == "pack-dry-run" ]]; then
  exec python3 "${SCRIPT_DIR}/scripts/migrate.py" pack --dry-run "${MIGRATE_EXTRA_ARGS[@]}"
elif [[ "$MODE" == "pack-to-ssd" ]]; then
  exec python3 "${SCRIPT_DIR}/scripts/migrate.py" pack --apply "${MIGRATE_EXTRA_ARGS[@]}"
elif [[ "$MODE" == "restore-dry-run" ]]; then
  exec python3 "${SCRIPT_DIR}/scripts/migrate.py" restore --dry-run
elif [[ "$MODE" == "restore-from-ssd" ]]; then
  python3 "${SCRIPT_DIR}/scripts/migrate.py" restore --apply
  MODE="apply"
elif [[ "$MODE" == "auto" ]]; then
  MODE="apply"
fi

if [[ "$MODE" == "doctor" ]]; then
  exec python3 "${SCRIPT_DIR}/scripts/doctor.py"
fi

if [[ "$MODE" == "sync-profiles" ]]; then
  exec python3 "${SCRIPT_DIR}/scripts/sync_profiles.py" --apply
fi

echo "=================================================================="
echo " Orca Multi-Agent Blueprint Deployment"
echo " Mode              : ${MODE}"
echo " Storage Mode      : ${STORAGE_MODE}"
echo " Config File       : ${CONFIG_FILE}"
echo " Workspace Root    : ${AI_VOLUME_PATH}"
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
  if [[ -n "$AI_VOLUME_UUID" && "$AI_VOLUME_UUID" != "AUTO" && "$AI_VOLUME_UUID" != "INTERNAL" && "$DETECTED_UUID" != "$AI_VOLUME_UUID" ]]; then
    echo "❌ Error: Volume UUID mismatch on ${AI_VOLUME_PATH} (expected ${AI_VOLUME_UUID}, got ${DETECTED_UUID})." >&2
    exit 1
  fi
  export AI_VOLUME_UUID="${DETECTED_UUID:-$AI_VOLUME_UUID}"
  echo "✅ External SSD verified: ${AI_VOLUME_PATH} (UUID: ${AI_VOLUME_UUID:-N/A})"
elif [[ "$AI_VOLUME_PATH" != /Volumes/* ]]; then
  export AI_VOLUME_UUID="INTERNAL"
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
  "${AI_VOLUME_PATH}/AI/Backups/ResticRepo"
  "${HOME}/.local/bin"
  "${HOME}/.agents/skills"
)

echo "--> Step 1/8: Ensuring standard 4-zone directory taxonomy..."
for d in "${DIRS[@]}"; do
  run_step "mkdir -p ${(q)d}"
done

# 3. Non-Interactive Shell PATH & Git Global Optimizations
echo "--> Step 2/8: Configuring ~/.zshenv & Git multi-worktree acceleration..."
if [[ ! -f "${HOME}/.zshenv" ]] || ! grep -q '.local/bin' "${HOME}/.zshenv" 2>/dev/null; then
  run_step "printf '%s\n' 'export PATH=\"\$HOME/.local/bin:/Library/Frameworks/Python.framework/Versions/3.14/bin:\$PATH\"' >> ~/.zshenv"
fi

if [[ "${ENABLE_GIT_ACCELERATION:-true}" == "true" ]] && xcode-select -p >/dev/null 2>&1; then
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
run_step "cp -f ${(q)SCRIPT_DIR}/scripts/guard.py ~/.local/bin/agent-guard"
run_step "chmod +x ~/.local/bin/agent-guard"

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
echo "--> Step 6/8: Configuring automated daily/hourly/weekly backups & storage watchdog..."
python3 "${SCRIPT_DIR}/scripts/setup_backups.py" "--${MODE}"

# 8. Synchronize All Agent Profiles & Run Verification Tests
echo "--> Step 7/8: Synchronizing skills, MCPs, rules & hooks across all Orca profiles..."
python3 "${SCRIPT_DIR}/scripts/sync_profiles.py" "--${MODE}"

echo "--> Step 8/8: Running verification tests..."
python3 "${SCRIPT_DIR}/tests/test_guard.py"
python3 "${SCRIPT_DIR}/tests/test_migrate.py"
if command -v node >/dev/null 2>&1; then
  AI_VOLUME_PATH="/Volumes/AI_Workspace" node --test "${SCRIPT_DIR}/plugins/orca-naming-guard/core.test.mjs" "${SCRIPT_DIR}/plugins/orca-naming-guard/worker.test.mjs"
fi

echo "✅ Deployment (${MODE}) completed successfully."
