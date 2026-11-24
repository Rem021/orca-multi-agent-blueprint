#!/bin/zsh
# Weekly retention policy, prune and integrity check (maintenance.sh)
# `restic forget --prune` + `restic check`; the first run of each month also reads back 5% of raw data.
source "$(dirname "$0")/lib.sh"
N=maintenance
take_lock "$N"
STALE_H=$(( STALE_ALERT_HOURS * 14 ))
[[ -x "$RESTIC" ]] || { skip_job "$N" "restic is not installed" "$STALE_H"; exit 0; }
nas_mounted || { skip_job "$N" "${NAS_REASON:-Backup target or NAS is not mounted}" "$STALE_H"; exit 0; }

log "$N" "start"
ok=1
# Maintenance needs an exclusive lock and can coincide with another Mac's weekly job.
maintenance_wait="${BACKUP_MAINTENANCE_RETRY_LOCK:-${BACKUP_RESTIC_RETRY_LOCK:-30m}}"
"$RESTIC" -r "$REPO" unlock >> "$LOGD/$N.log" 2>&1
caffeinate -i "$RESTIC" -r "$REPO" --retry-lock "$maintenance_wait" forget --group-by host,tags --keep-hourly 24 --keep-daily 30 --keep-weekly 12 --keep-monthly 24 --prune >> "$LOGD/$N.log" 2>&1 || ok=0
if [[ "$(date +%d)" -le 7 ]]; then
  caffeinate -i "$RESTIC" -r "$REPO" --retry-lock "$maintenance_wait" check --read-data-subset=5% >> "$LOGD/$N.log" 2>&1 || ok=0
else
  caffeinate -i "$RESTIC" -r "$REPO" --retry-lock "$maintenance_wait" check >> "$LOGD/$N.log" 2>&1 || ok=0
fi
stats=$("$RESTIC" -r "$REPO" --retry-lock "$maintenance_wait" stats --mode raw-data 2>/dev/null | grep -E "Total Size|Snapshots" | tr '\n' ' ')
[[ $ok == 1 ]] && status_write "$N" ok "forget/prune/check passed. $stats" || {
  status_write "$N" fail "A maintenance step failed, see logs/$N.log"
  notify "Backup maintenance failed" "See logs/maintenance.log"
}
log "$N" "done ok=$ok"
[[ $ok == 1 ]]
