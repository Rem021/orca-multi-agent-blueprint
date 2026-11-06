#!/bin/zsh
# 60-Second Storage Volume & USB Session Watchdog (volume-guard.sh)
# In external SSD mode: checks mount status, hardware VolumeUUID & USB re-enumeration between polls.
# In internal disk mode: verifies local workspace directory and reports ok without false USB alerts.
source "$(dirname "$0")/lib.sh"
N=volume-guard
prev=$(python3 -c "import json;print(json.load(open('$ST/$N.json'))['state'])" 2>/dev/null || echo "")

if [[ "$STORAGE_MODE" == "internal" || "$VOL" != /Volumes/* ]]; then
  if [[ -d "$VOL/AI" ]]; then
    cur="ok"
    msg="本机内置硬盘存储模式已就绪 ($VOL/AI)"
  else
    cur="absent"
    msg="本机内置工作区目录缺失 ($VOL/AI)"
  fi
  if [[ "$cur" != "$prev" ]]; then
    status_write "$N" "$cur" "$msg"
    log "$N" "$cur ($msg)"
  fi
  exit 0
fi

force=no
[[ ! -f "$ST/volume-usb-observation.json" ]] && force=yes
sample=$(python3 "$AG/volume-observer.py" --volume "$VOL" --uuid "$VOL_UUID" --state "$ST/volume-usb-observation.json" --events "$LOGD/volume-usb-events.jsonl") || {
  log "$N" "USB 观察失败；保留上次状态，等待下次采样"
  exit 1
}
vol_label="$(basename "$VOL")"
case "$sample" in
  busy) exit 0 ;;
  ok) cur=ok; msg="${vol_label} 已挂载，卷身份核对通过" ;;
  reconnected)
    cur=ok; force=yes
    msg="${vol_label} 已挂载；检测到两次采样之间 USB 重新枚举，原因待核查"
    log "$N" "$msg"
    ;;
  absent) cur=absent; msg="${vol_label} 未挂载，项目/素材备份会跳过" ;;
  wrong_volume) cur=absent; msg="${vol_label} 挂载点卷 UUID 不匹配，不能视作目标工作盘" ;;
  *) log "$N" "无法识别 USB 观察结果"; exit 1 ;;
esac
if [[ "$cur" != "$prev" || "$force" = yes ]]; then
  status_write "$N" "$cur" "$msg"
  [[ "$cur" = absent ]] && notify "外置盘不在" "$msg"
  log "$N" "$cur"
fi
exit 0
