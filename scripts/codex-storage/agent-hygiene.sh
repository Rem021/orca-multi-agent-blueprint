#!/bin/zsh
# Periodic Multi-Agent Ephemeral Scratch/Test, Xcode Runner & LaunchServices Ghost GC (agent-hygiene.sh)
# Runs every 30m: stages expired work/_scratch (>3d), work/_tests (>7d), stale DerivedData (>72h),
# idle *UITests-Runner.app / *.xctrunner (>=1h), profile .tmp/snapshots, excess .bak files, and idle
# ephemeral test simulators into ~/.Trash/agent-governor-* with manifest.json, ensures .metadata_never_index
# shields, and unregisters ghost/transient build `.app` bundles from macOS LaunchServices (`lsregister -u`).
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
extra_gc_args=( ${(z)AGENT_GOVERNOR_GC_ARGS:-} )
raw_json="$(python3 "$GOV_BIN" gc --mode daily --root "$VOL/AI/Projects" --home "$HOME" "${extra_gc_args[@]}" --apply --json 2>>"$LOGD/$N.log")"
rc=$?

# Once per 24h (when agent-slim is installed in $HOME), sweep retained lab-run runner bundles & LaunchServices
SLIM_SCRIPT="$HOME/.agents/skills/agent-slim/scripts/agent_slim.py"
SLIM_STAMP="$ST/.agent-slim-daily.stamp"
if [[ $rc -eq 0 && "${AGENT_HYGIENE_SKIP_SLIM:-0}" != "1" && -f "$SLIM_SCRIPT" ]]; then
  now_ep="$(date +%s)"
  last_ep=0
  [[ -f "$SLIM_STAMP" ]] && last_ep="$(cat "$SLIM_STAMP" 2>/dev/null || echo 0)"
  if (( now_ep - last_ep >= 86400 )); then
    python3 "$SLIM_SCRIPT" clean "$VOL/AI" --min-age-hours 1 --clean-launchservices --shield-spotlight --json >>"$LOGD/$N.log" 2>&1 \
      && echo "$now_ep" > "$SLIM_STAMP"
  fi
fi

if [[ $rc -eq 0 && -n "$raw_json" ]]; then
  summary="$(python3 - "$raw_json" <<'PYSUM'
import json, sys
d = json.loads(sys.argv[1])
cnt = d.get("total_candidates", 0)
hum = d.get("reclaimable_human", "0 B")
skp = d.get("protected_skips_count", 0)
ls_cnt = len(d.get("launchservices_ghosts") or [])
fs_cnt = max(0, cnt - ls_cnt)
extra = f"，注销幽灵应用 {ls_cnt} 项" if ls_cnt else ""
print("5-gate TTL GC 完成：回收 {} 项 ({}){}，跳过保护项 {} 项".format(fs_cnt, hum, extra, skp))
PYSUM
)"
  status_write "$N" ok "${summary:-5-gate TTL GC 完成}"
else
  status_write "$N" fail "agent-governor gc 执行异常 (rc=$rc)，看 logs/$N.log"
  notify "Agent 缓存巡检异常" "看 logs/agent-hygiene.log"
fi
log "$N" "done rc=$rc"
