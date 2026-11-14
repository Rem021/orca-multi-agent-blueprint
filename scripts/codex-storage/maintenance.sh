#!/bin/zsh
# Weekly Retention Policy + Prune + Integrity Check (maintenance.sh)
# `restic forget --prune` + `restic check`; the first run of each month also reads back 5% of raw data.
source "$(dirname "$0")/lib.sh"
N=maintenance
take_lock "$N"
STALE_H=$(( STALE_ALERT_HOURS * 14 ))
[[ -x "$RESTIC" ]] || { skip_job "$N" "未安装 restic" "$STALE_H"; exit 0; }
nas_mounted || { skip_job "$N" "${NAS_REASON:-备份目标或 NAS 未挂载}" "$STALE_H"; exit 0; }

log "$N" "start"
ok=1
"$RESTIC" -r "$REPO" unlock >> "$LOGD/$N.log" 2>&1
caffeinate -i "$RESTIC" -r "$REPO" --retry-lock "$RESTIC_RETRY_LOCK" forget --group-by host,tags --keep-hourly 24 --keep-daily 30 --keep-weekly 12 --keep-monthly 24 --prune >> "$LOGD/$N.log" 2>&1 || ok=0
if [[ "$(date +%d)" -le 7 ]]; then
  caffeinate -i "$RESTIC" -r "$REPO" --retry-lock "$RESTIC_RETRY_LOCK" check --read-data-subset=5% >> "$LOGD/$N.log" 2>&1 || ok=0
else
  caffeinate -i "$RESTIC" -r "$REPO" --retry-lock "$RESTIC_RETRY_LOCK" check >> "$LOGD/$N.log" 2>&1 || ok=0
fi
stats=$("$RESTIC" -r "$REPO" --retry-lock "$RESTIC_RETRY_LOCK" stats --mode raw-data 2>/dev/null | grep -E "Total Size|Snapshots" | tr '\n' ' ')
[[ $ok == 1 ]] && status_write "$N" ok "forget/prune/check 通过。$stats" || {
  status_write "$N" fail "有步骤失败，看 logs/$N.log"
  notify "备份巡检失败" "看 logs/maintenance.log"
}
log "$N" "done ok=$ok"
