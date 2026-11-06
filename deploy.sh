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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --auto) MODE="auto"; shift ;;
    --apply) MODE="apply"; shift ;;
    --dry-run) MODE="dry-run"; shift ;;
    --sync-profiles) MODE="sync-profiles"; shift ;;
    --doctor) MODE="doctor"; shift ;;
    --render-rules) RENDER_RULES="true"; shift ;;
    --config) CONFIG_FILE="$2"; shift 2 ;;
    -h|--help)
      cat <<'EOF'
Usage: ./deploy.sh [OPTIONS]

Options:
  --auto            Zero-config full deployment: auto-discovers volume, UUID, simulators,
                    existing projects & agents, generates config.env, installs CLI tools,
                    syncs all Orca profiles, and verifies health (Recommended!)
  --apply           Execute full deployment using ./config.env (or ./config.example.env)
  --dry-run         Preview all directories, tools, symlinks, and project adoptions (Default)
  --sync-profiles   Re-synchronize skills, MCPs, AGENTS.md/CLAUDE.md, and hooks across all Orca profiles
  --doctor          Run read-only multi-agent health and parity audit
  --render-rules    Force re-render ~/.codex/AGENTS.md, ~/.claude/CLAUDE.md, ~/.gemini/GEMINI.md (backs up existing)
  --config <path>   Path to custom config.env
  -h, --help        Show this help message
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1 (use --help)" >&2
      exit 1
      ;;
  esac
done

# Auto-discovery step: generate config.env if in --auto mode and config.env does not exist
if [[ "$MODE" == "auto" && ! -f "$CONFIG_FILE" ]]; then
  echo "--> Auto-discovering host volume, iOS simulators, existing projects & agent fleet..."
  python3 "${SCRIPT_DIR}/scripts/discover_and_adopt.py" --apply --write-config "$CONFIG_FILE" >/dev/null
  MODE="apply"
elif [[ "$MODE" == "auto" ]]; then
  MODE="apply"
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
  if [[ -z "${AI_VOLUME_PATH:-}" ]]; then
    for v in /Volumes/*(N/); do
      vname="${v:t}"
      [[ "$vname" == "Macintosh HD"* || "$vname" == "Preboot" || "$vname" == "Recovery" || "$vname" == "VM" || "$vname" == "Update" ]] && continue
      if [[ -d "${v}/AI" ]]; then
        export AI_VOLUME_PATH="$v"
        break
      fi
    done
  fi
  CONFIG_FILE="${SCRIPT_DIR}/config.example.env"
fi

source "$CONFIG_FILE"

export AI_VOLUME_PATH="${AI_VOLUME_PATH:-/Volumes/AI_Workspace}"
export AI_VOLUME_UUID="${AI_VOLUME_UUID:-AUTO}"
export SUPERVISOR_AGENT="${SUPERVISOR_AGENT:-muse}"
export WORKER_AGENTS="${WORKER_AGENTS:-claude,codex,antigravity,dsh}"
export DEFAULT_BRANCH_PREFIX="${DEFAULT_BRANCH_PREFIX:-codex}"
export ENABLE_XCODE_MCP="${ENABLE_XCODE_MCP:-true}"
export PRIMARY_SIMULATOR_UDID="${PRIMARY_SIMULATOR_UDID:-11111111-2222-3333-4444-555555555555}"
export PRIMARY_SIMULATOR_NAME="${PRIMARY_SIMULATOR_NAME:-iPhone 18 Pro Max}"
export SECONDARY_SIMULATOR_UDID="${SECONDARY_SIMULATOR_UDID:-66666666-7777-8888-9999-AAAAAAAAAAAA}"
export SECONDARY_SIMULATOR_NAME="${SECONDARY_SIMULATOR_NAME:-iPhone 18 Pro Max (Secondary)}"

if [[ "$MODE" == "doctor" ]]; then
  exec python3 "${SCRIPT_DIR}/scripts/doctor.py"
fi

if [[ "$MODE" == "sync-profiles" ]]; then
  exec python3 "${SCRIPT_DIR}/scripts/sync_profiles.py" --apply
fi

echo "=================================================================="
echo " Orca Multi-Agent Blueprint Deployment"
echo " Mode              : ${MODE}"
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
  if [[ -n "$AI_VOLUME_UUID" && "$AI_VOLUME_UUID" != "AUTO" && "$DETECTED_UUID" != "$AI_VOLUME_UUID" ]]; then
    echo "❌ Error: Volume UUID mismatch on ${AI_VOLUME_PATH} (expected ${AI_VOLUME_UUID}, got ${DETECTED_UUID})." >&2
    exit 1
  fi
  export AI_VOLUME_UUID="${DETECTED_UUID:-$AI_VOLUME_UUID}"
  echo "✅ Volume verified: ${AI_VOLUME_PATH} (UUID: ${AI_VOLUME_UUID:-N/A})"
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

echo "--> Step 1/7: Ensuring standard directory taxonomy..."
for d in "${DIRS[@]}"; do
  run_step "mkdir -p ${(q)d}"
done

# 3. Non-Interactive Shell PATH & Git Global Optimizations
echo "--> Step 2/7: Configuring ~/.zshenv & Git multi-worktree acceleration..."
if [[ ! -f "${HOME}/.zshenv" ]] || ! grep -q '.local/bin' "${HOME}/.zshenv" 2>/dev/null; then
  run_step "printf '%s\n' 'export PATH=\"\$HOME/.local/bin:/Library/Frameworks/Python.framework/Versions/3.14/bin:\$PATH\"' >> ~/.zshenv"
fi

if [[ "${ENABLE_GIT_ACCELERATION:-true}" == "true" ]]; then
  run_step "git config --global feature.manyFiles true"
  run_step "git config --global core.fsmonitor true"
  run_step "git config --global rerere.enabled true"
  run_step "git config --global worktree.useRelativePaths true"
  run_step "git config --global branch.sort -committerdate"
fi

# 4. Native CLI Toolchain Installation (Zero-dependency curl + GitHub API)
if [[ "${INSTALL_CLI_TOOLCHAIN:-true}" == "true" && "$MODE" == "apply" ]]; then
  echo "--> Step 3/7: Checking & installing native CLI toolchain in ~/.local/bin..."
  "${SCRIPT_DIR}/scripts/install_cli_tools.sh"
elif [[ "$MODE" == "dry-run" ]]; then
  echo "--> Step 3/7: [DRY-RUN] Would check/install rg, fd, ast-grep, just, xcbeautify, scc, repomix into ~/.local/bin"
fi

# 5. Install Orca Built-in Skills, Guardrails & ast-grep Skill
echo "--> Step 4/7: Installing Orca built-in skills, PreToolUse guardrail & ast-grep skill..."
if [[ "$MODE" == "apply" && -x "/Applications/Orca.app/Contents/Resources/bin/orca" ]]; then
  /Applications/Orca.app/Contents/Resources/bin/orca skills install >/dev/null 2>&1 || true
fi
run_step "mkdir -p ~/.agents/skills/ast-grep"
run_step "cp -f ${(q)SCRIPT_DIR}/skills/ast-grep/SKILL.md ~/.agents/skills/ast-grep/SKILL.md"
run_step "cp -f ${(q)SCRIPT_DIR}/scripts/guard.py ~/.local/bin/agent-guard"
run_step "chmod +x ~/.local/bin/agent-guard"

# 6. Auto-Discover & Non-Destructively Adopt Existing Projects & Global Rules
echo "--> Step 5/7: Running smart project adoption & global rule check..."
DISCOVER_FLAGS=("--${MODE}")
[[ "$RENDER_RULES" == "true" ]] && DISCOVER_FLAGS+=("--render-rules")
python3 "${SCRIPT_DIR}/scripts/discover_and_adopt.py" "${DISCOVER_FLAGS[@]}"

# 7. Synchronize All Agent Profiles & Run Verification Tests
echo "--> Step 6/7: Synchronizing skills, MCPs, rules & hooks across all Orca profiles..."
python3 "${SCRIPT_DIR}/scripts/sync_profiles.py" "--${MODE}"

echo "--> Step 7/7: Running verification tests..."
python3 "${SCRIPT_DIR}/tests/test_guard.py"
if command -v node >/dev/null 2>&1; then
  node --test "${SCRIPT_DIR}/plugins/orca-naming-guard/core.test.mjs" "${SCRIPT_DIR}/plugins/orca-naming-guard/worker.test.mjs"
fi

echo "✅ Deployment (${MODE}) completed successfully."
