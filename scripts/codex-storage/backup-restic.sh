#!/bin/zsh
# Universal Restic Backup Runner (backup-restic.sh)
# Usage: backup-restic.sh hot | projects | media
# Works seamlessly with both External SSD and Internal Disk storage modes.
source "$(dirname "$0")/lib.sh"
P="${1:?profile}"
N="backup-$P"
take_lock "$N"

[[ -x "$RESTIC" ]] || { status_write "$N" skipped "未安装 restic 二进制"; exit 0; }
nas_mounted || { status_write "$N" skipped "备份目标或 NAS 未挂载"; log "$N" "skip: repo unavailable"; exit 0; }

case "$P" in
  hot)
    CANDIDATES=(
      "$HOME/.claude"
      "$HOME/.claude.json"
      "$HOME/.codex"
      "$HOME/.gemini"
      "$HOME/.agents"
      "$HOME/.orca"
      "$HOME/.dsh"
      "$HOME/.dsh-tui"
      "$HOME/.grokbot"
      "$HOME/.ssh"
      "$HOME/.gitconfig"
      "$HOME/.zshrc"
      "$HOME/.zshenv"
      "$HOME/.zprofile"
      "$HOME/.zsh_history"
      "$HOME/.pymobiledevice3"
      "$HOME/Developer"
      "$HOME/Library/LaunchAgents"
      "$HOME/Library/Developer/Xcode/UserData"
      "$HOME/Library/Application Support/orca"
      "$HOME/Library/Application Support/Claude"
      "$HOME/Library/Application Support/Codex"
      "$HOME/Library/Application Support/Codex Storage/agents"
      "$HOME/Library/Application Support/Antigravity"
      "$HOME/Library/Application Support/com.meta.endo"
      "$HOME/Library/Application Support/com.meta.prototypes.endo.tokens"
      "$HOME/Library/Application Support/cc-same"
    )
    for extra_item in ${(s:,:)EXTRA_MIGRATION_PATHS}; do
      cleaned="${extra_item## }"
      cleaned="${cleaned%% }"
      [[ -n "$cleaned" ]] && CANDIDATES+=("$HOME/$cleaned")
    done
    typeset -U CANDIDATES
    PATHS=()
    for c in "${CANDIDATES[@]}"; do
      [[ -e "$c" || -L "$c" ]] && PATHS+=("$c")
    done
    EXC=(
      --exclude "$HOME/.codex/cache"
      --exclude "$HOME/.codex/.tmp"
      --exclude "$HOME/.codex/tmp"
      --exclude "$HOME/.codex/node_repl"
      --exclude "$HOME/.codex/worktrees"
      --exclude "$HOME/.claude/shell-snapshots"
      --exclude "$HOME/.claude/debug"
      --exclude "$HOME/.dsh/cache"
      --exclude "$HOME/.dsh/profiles/*/node_modules"
      --exclude "$HOME/.dsh-tui/compile-cache"
      --exclude "$HOME/Library/Application Support/Claude/vm_bundles"
      --exclude "$HOME/Library/Application Support/Claude/claude-code-vm"
      --exclude "$HOME/Library/Application Support/Claude/Cache"
      --exclude "$HOME/Library/Application Support/Claude/Code Cache"
      --exclude "$HOME/Library/Application Support/Claude/GPUCache"
      --exclude "$HOME/Library/Application Support/orca/Cache"
      --exclude "$HOME/Library/Application Support/orca/Code Cache"
      --exclude "$HOME/Library/Application Support/orca/GPUCache"
      --exclude "$HOME/Library/Application Support/orca/DawnGraphiteCache"
      --exclude "$HOME/Library/Application Support/orca/DawnWebGPUCache"
      --exclude "$HOME/Library/Application Support/orca/Crashpad"
      --exclude "$HOME/Library/Application Support/orca/acp-temp"
      --exclude "$HOME/Library/Application Support/orca/Singleton*"
      --exclude "$HOME/Library/Application Support/orca/orcad.lock"
      --exclude "node_modules"
      --exclude "DerivedData"
      --exclude ".DS_Store"
    )
    ;;
  projects)
    vol_mounted || { status_write "$N" skipped "工作区存储卷未就绪"; exit 0; }
    PATHS=("$VOL/AI")
    EXC=(
      --exclude node_modules
      --exclude .venv
      --exclude venv
      --exclude __pycache__
      --exclude .mypy_cache
      --exclude .pytest_cache
      --exclude DerivedData
      --exclude .next
      --exclude build
      --exclude .build
      --exclude .DS_Store
      --exclude "$VOL/AI/Codex/Worktrees"
      --exclude "$VOL/AI/Claude/Worktrees"
      --exclude "$VOL/AI/Orca/Worktrees"
      --exclude "$VOL/AI/Backups/restic"
    )
    ;;
  media)
    vol_mounted || { status_write "$N" skipped "工作区存储卷未就绪"; exit 0; }
    [[ -d "$MEDIA_PATH" ]] || { status_write "$N" skipped "未配置或不存在素材目录 ($MEDIA_PATH)"; exit 0; }
    PATHS=("$MEDIA_PATH")
    EXC=(--exclude "Render Files" --exclude "Transcoded Media" --exclude "Analysis Files" --exclude .DS_Store --limit-upload 45000)
    ;;
  *)
    echo "unknown profile: $P"
    exit 2
    ;;
esac

status_write "$N" running "正在执行 restic 备份 ($P)"
log "$N" "start"
t0=$(date +%s)
OUT=$(caffeinate -i "$RESTIC" -r "$REPO" backup --tag "$P" --host "$HOSTTAG" --exclude-caches --no-scan "${EXC[@]}" "${PATHS[@]}" 2>&1)
rc=$?
echo "$OUT" >> "$LOGD/$N.log"
dt=$(( $(date +%s) - t0 ))
summary=$(echo "$OUT" | grep -E "^(Files:|Added to the repo|processed)" | tr '\n' ' ')
if [[ $rc -eq 0 ]]; then
  status_write "$N" ok "$summary" "{\"seconds\":$dt}"
elif [[ $rc -eq 3 ]]; then
  errors=$(print -r -- "$OUT" | grep -c '^error:' || true)
  protected=$(print -r -- "$OUT" | grep -Ec '^error: can not obtain extended attribute com\.apple\.security\.private\.scoped-bookmark-(key|token) for .*: xattr\.get .*operation not permitted$' || true)
  if [[ "$errors" -gt 0 && "$errors" == "$protected" ]] && print -r -- "$OUT" | grep -Eq '^snapshot [0-9a-f]+ saved$'; then
    status_write "$N" warn "快照已保存；$protected 项受保护的 macOS 扩展属性未备份（rc=3），详见日志" "{\"seconds\":$dt,\"protected_xattr_warnings\":$protected}"
  else
    status_write "$N" warn "部分文件或属性读不到（rc=3）: $summary" "{\"seconds\":$dt}"
  fi
else
  status_write "$N" fail "restic rc=$rc: $(echo "$OUT" | tail -2 | tr '\n' ' ')" "{\"seconds\":$dt}"
  notify "备份失败: $P" "restic rc=$rc，看 logs/$N.log"
fi
log "$N" "done rc=$rc in ${dt}s"
exit "$rc"
