#!/bin/zsh
# Weekly Disaster-Recovery Restore Verification Drill (restore-drill.sh)
# Restores ~/.codex/memories and ~/.claude/projects from the latest hot snapshot into a temp directory and verifies file checksums.
source "$(dirname "$0")/lib.sh"
N=restore-drill
take_lock "$N"
[[ -x "$RESTIC" ]] || { status_write "$N" skipped "未安装 restic"; exit 0; }
nas_mounted || { status_write "$N" skipped "备份目标或 NAS 未挂载"; exit 0; }

T="$HOME/Library/Caches/restic-drill"
rm -rf "$T"
mkdir -p "$T"
log "$N" "start"
OUT=$(caffeinate -i "$RESTIC" -r "$REPO" restore latest --tag hot --host "$HOSTTAG" --include "$HOME/.codex/memories" --include "$HOME/.claude/projects" --target "$T" --verify 2>&1)
rc=$?
echo "$OUT" | tail -5 >> "$LOGD/$N.log"
n=$(find "$T" -type f | wc -l | tr -d ' ')
if [[ $rc -eq 0 && "$n" -gt 0 ]]; then
  status_write "$N" ok "恢复并校验 $n 个文件通过"
else
  status_write "$N" fail "rc=$rc, files=$n: $(echo "$OUT" | tail -1)"
  notify "恢复演练失败" "rc=$rc, 恢复文件数=$n，看 logs/restore-drill.log"
fi
rm -rf "$T"
log "$N" "done rc=$rc files=$n"
