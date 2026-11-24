#!/bin/zsh
# Storage volume and USB session watchdog (volume-guard.sh; every 60 s in external mode, 10 min internal)
# External SSD mode: checks the mount, the VolumeUUID and USB re-enumeration between polls.
# Internal disk mode: checks the local workspace folder and never raises USB alerts.
source "$(dirname "$0")/lib.sh"
N=volume-guard
prev=""
prev_msg=""
if [[ -f "$ST/$N.json" ]]; then
  IFS=$'\t' read -r prev prev_msg < <(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("state",""),d.get("message",""),sep="\t")' "$ST/$N.json" 2>/dev/null || echo "")
fi

if [[ "$STORAGE_MODE" == "internal" || "$VOL" != /Volumes/* ]]; then
  if [[ -d "$VOL/AI" ]]; then
    cur="ok"
    ext_note=""
    if [[ "$MEDIA_PATH" == /Volumes/*/* ]]; then
      ext_vol="/Volumes/${${MEDIA_PATH#/Volumes/}%%/*}"
      if [[ -d "$MEDIA_PATH" ]]; then
        ext_note="; external media drive ${ext_vol:t} is online"
      else
        ext_note="; external media drive ${ext_vol:t} is not connected (local work is not affected)"
      fi
    fi
    msg="Internal disk workspace is ready ($VOL/AI)${ext_note}"
  else
    cur="absent"
    msg="Internal workspace folder is missing ($VOL/AI)"
  fi
  if [[ "$cur" != "$prev" || "$msg" != "$prev_msg" ]]; then
    status_write "$N" "$cur" "$msg"
    log "$N" "$cur ($msg)"
  fi
  exit 0
fi

force=no
[[ ! -f "$ST/volume-usb-observation.json" ]] && force=yes
sample=$(python3 "$AG/volume-observer.py" --volume "$VOL" --uuid "$VOL_UUID" --state "$ST/volume-usb-observation.json" --events "$LOGD/volume-usb-events.jsonl") || {
  log "$N" "USB observation failed; keeping the previous state until the next sample"
  exit 1
}
vol_label="${VOL_ROOT:t}"
case "$sample" in
  busy) exit 0 ;;
  ok) cur=ok; msg="${vol_label} is mounted and its identity matches" ;;
  reconnected)
    cur=ok; force=yes
    msg="${vol_label} is mounted; USB re-enumerated between two samples (cause unknown)"
    log "$N" "$msg"
    ;;
  absent) cur=absent; msg="${vol_label} is not mounted; project and media backups are skipped" ;;
  wrong_volume) cur=absent; msg="The volume mounted at ${VOL_ROOT} has a different UUID; it is not the workspace drive" ;;
  missing_folder) cur=absent; msg="${vol_label} is mounted but the workspace folder $VOL is missing" ;;
  *) log "$N" "unrecognised USB observation result: $sample"; exit 1 ;;
esac
if [[ "$cur" != "$prev" || "$msg" != "$prev_msg" || "$force" = yes ]]; then
  status_write "$N" "$cur" "$msg"
  [[ "$cur" = absent && "$cur" != "$prev" ]] && notify "Workspace drive unavailable" "$msg"
  log "$N" "$cur"
fi
exit 0
