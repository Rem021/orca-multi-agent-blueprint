#!/bin/zsh
# Daily Multi-Agent Ephemeral Scratch/Test & Cache TTL Garbage Collector (agent-hygiene.sh)
# Stages expired work/_scratch (>3d), work/_tests (>7d), stale profile .tmp/snapshots, excess .bak
# files, and idle ephemeral test simulators into ~/.Trash/agent-governor-* with manifest.json.
source "$(dirname "$0")/lib.sh"
N=agent-hygiene
take_lock "$N"
STALE_H=$(( STALE_ALERT_HOURS * 2 ))

GOV_BIN="$HOME/.local/bin/agent-governor"
if [[ ! -x "$GOV_BIN" ]]; then
  SCRIPT_FALLBACK="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)/agent_governor.py"
  if [[ -f "$SCRIPT_FALLBACK" ]]; then
    GOV_BIN="$SCRIPT_FALLBACK"
  else
    skip_job "$N" "未安装 agent-governor ($HOME/.local/bin/agent-governor)" "$STALE_H"
    exit 0
  fi
fi
vol_mounted || { skip_job "$N" "工作区未就绪 ($VOL/AI)" "$STALE_H"; exit 0; }

log "$N" "start"
raw_json="$(python3 "$GOV_BIN" gc --mode daily --root "$VOL/AI/Projects" --home "$HOME" --apply --json 2>>"$LOGD/$N.log")"
rc=$?
if [[ $rc -eq 0 && -n "$raw_json" ]]; then
  summary="$(python3 - "$raw_json" <<'PYSUM'
import json, sys
d = json.loads(sys.argv[1])
cnt = d.get("total_candidates", 0)
hum = d.get("reclaimable_human", "0 B")
skp = d.get("protected_skips_count", 0)
print("5-gate TTL GC 完成：回收 {} 项 ({})，跳过保护项 {} 项".format(cnt, hum, skp))
PYSUM
)"
  status_write "$N" ok "${summary:-5-gate TTL GC 完成}"
else
  status_write "$N" fail "agent-governor gc 执行异常 (rc=$rc)，看 logs/$N.log"
  notify "Agent 缓存巡检异常" "看 logs/agent-hygiene.log"
fi
log "$N" "done rc=$rc"
