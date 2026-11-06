#!/bin/zsh
# Weekly Retention Policy + Prune + Integrity Check (maintenance.sh)
# Reads back 5% of raw data on the first Sunday of each month.
source "$(dirname "$0")/lib.sh"
N=maintenance
take_lock "$N"
[[ -x "$RESTIC" ]] || { status_write "$N" skipped "未安装 restic"; exit 0; }
nas_mounted || { status_write "$N" skipped "备份目标或 NAS 未挂载"; exit 0; }

log "$N" "start"
ok=1
"$RESTIC" -r "$REPO" unlock >> "$LOGD/$N.log" 2>&1
caffeinate -i "$RESTIC" -r "$REPO" forget --group-by host,tags --keep-hourly 24 --keep-daily 30 --keep-weekly 12 --keep-monthly 24 --prune >> "$LOGD/$N.log" 2>&1 || ok=0
if [[ "$(date +%d)" -le 7 ]]; then
  caffeinate -i "$RESTIC" -r "$REPO" check --read-data-subset=5% >> "$LOGD/$N.log" 2>&1 || ok=0
else
  caffeinate -i "$RESTIC" -r "$REPO" check >> "$LOGD/$N.log" 2>&1 || ok=0
fi
stats=$("$RESTIC" -r "$REPO" stats --mode raw-data 2>/dev/null | grep -E "Total Size|Snapshots" | tr '\n' ' ')
[[ $ok == 1 ]] && status_write "$N" ok "forget/prune/check 通过。$stats" || {
  status_write "$N" fail "有步骤失败，看 logs/$N.log"
  notify "备份巡检失败" "看 logs/maintenance.log"
}
log "$N" "done ok=$ok"
