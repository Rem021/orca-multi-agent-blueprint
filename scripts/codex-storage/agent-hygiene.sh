#!/bin/zsh
# Periodic Multi-Agent Ephemeral Scratch/Test, Xcode Runner & LaunchServices Ghost GC (agent-hygiene.sh)
# LaunchAgent runs every 1h: polls iCloud backup package uploads (`icloud_prune.py`, immediate while `warn`),
# and executes `agent-governor gc --mode daily` on a throttled cadence (AGENT_HYGIENE_GC_INTERVAL_SEC, default 4h)
# to stage expired work/_scratch (>3d), work/_tests (>7d), stale DerivedData (>12h), idle XCTest runners,
# profile .tmp/snapshots, excess .bak files, and idle ephemeral simulators into ~/.Trash/agent-governor-*.
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
GC_STAMP="$ST/.agent-gc.stamp"
gc_interval="${AGENT_HYGIENE_GC_INTERVAL_SEC:-14400}"
[[ "$gc_interval" == <-> ]] || gc_interval=14400
now_ep="$(date +%s)"
last_gc_ep=0
[[ -f "$GC_STAMP" ]] && last_gc_ep="$(cat "$GC_STAMP" 2>/dev/null || echo 0)"
[[ "$last_gc_ep" == <-> ]] || last_gc_ep=0

ran_gc=0
rc=0
raw_json=""
if (( ${#extra_gc_args[@]} > 0 )) || (( gc_interval == 0 )) || (( now_ep - last_gc_ep >= gc_interval )); then
  ran_gc=1
  raw_json="$(python3 "$GOV_BIN" gc --mode daily --root "$VOL/AI/Projects" --home "$HOME" "${extra_gc_args[@]}" --apply --json 2>>"$LOGD/$N.log")"
  rc=$?
else
  log "$N" "gc throttled (elapsed=$(( now_ep - last_gc_ep ))s < ${gc_interval}s)"
fi

# Periodic iCloud backup package polling & rolling pruner:
#   - When icloud-package is in `warn` state (delayed upload), polls on every wakeup until upload completes,
#     then evicts local copy, prunes superseded packages, and reconciles icloud-package state to `ok`.
#   - Otherwise runs on a throttled cadence (ICLOUD_PRUNE_INTERVAL_SEC, default 3600s) to enforce
#     rolling retention across all hosts and legacy packages.
ICLOUD_PRUNE_NOTE=""
ICLOUD_PRUNE_SCRIPT="$AG/icloud_prune.py"
ICLOUD_PRUNE_STAMP="$ST/.icloud-prune.stamp"
if [[ $rc -eq 0 && "${AGENT_HYGIENE_SKIP_ICLOUD_PRUNE:-0}" != "1" && -f "$ICLOUD_PRUNE_SCRIPT" ]]; then
  icloud_state=""
  if [[ -f "$ST/icloud-package.json" ]]; then
    icloud_state="$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("state", ""))
except Exception:
    pass' "$ST/icloud-package.json" 2>/dev/null || true)"
  fi
  now_ep="$(date +%s)"
  last_prune_ep=0
  [[ -f "$ICLOUD_PRUNE_STAMP" ]] && last_prune_ep="$(cat "$ICLOUD_PRUNE_STAMP" 2>/dev/null || echo 0)"
  prune_interval="${ICLOUD_PRUNE_INTERVAL_SEC:-3600}"
  [[ "$prune_interval" == <-> ]] || prune_interval=3600
  if [[ "$icloud_state" == "warn" ]] || (( now_ep - last_prune_ep >= prune_interval )); then
    SAFE_HOST="${HOSTTAG//[^A-Za-z0-9._-]/_}"
    [[ -n "$SAFE_HOST" ]] || SAFE_HOST="Mac"
    icloud_prune_json="$(python3 "$ICLOUD_PRUNE_SCRIPT" --apply --evict-kept --json \
      --icloud-dir "$ICLOUD" --storage-dir "$S" --host "$SAFE_HOST" \
      --keep-latest "$ICLOUD_KEEP_LATEST" --max-age-days "$ICLOUD_MAX_AGE_DAYS" \
      --scope "$ICLOUD_PRUNE_SCOPE" --stale-host-days "$ICLOUD_STALE_HOST_DAYS" 2>>"$LOGD/$N.log")"
    prune_rc=$?
    if (( prune_rc != 0 )); then
      status_write "$N" fail "iCloud 清理失败 (rc=$prune_rc)，看 logs/$N.log"
      exit "$prune_rc"
    fi
    if eval_out="$(python3 -c '
import json, sys
d = json.load(sys.stdin)
pruned_cnt = int(d["pruned_count"])
if d["failed_count"]:
    sys.exit(1)
reclaimed = d["reclaimed_human"]
pending = d["pending_own_uploads"]
latest = d["latest_own_uploaded"]
if pruned_cnt > 0:
    print(f"NOTE=，轮询清理 iCloud 旧包 {pruned_cnt} 份 ({reclaimed})")
if sys.argv[1] == "warn" and not pending and latest:
    with open(sys.argv[2], encoding="utf-8") as receipt:
        previous = json.load(receipt)
    if previous.get("state") != "warn" or latest["name"] not in previous.get("message", ""):
        sys.exit(0)
    print("RECONCILE_NAME=" + latest["name"])
    print("RECONCILE_SIZE=" + latest["size_human"])
' "$icloud_state" "$ST/icloud-package.json" <<< "$icloud_prune_json")"; then
      echo "$now_ep" > "$ICLOUD_PRUNE_STAMP"
      reconcile_name=""
      reconcile_size=""
      while IFS= read -r kv; do
        case "$kv" in
          NOTE=*) ICLOUD_PRUNE_NOTE="${kv#NOTE=}" ;;
          RECONCILE_NAME=*) reconcile_name="${kv#RECONCILE_NAME=}" ;;
          RECONCILE_SIZE=*) reconcile_size="${kv#RECONCILE_SIZE=}" ;;
        esac
      done <<< "$eval_out"
      if [[ -n "$ICLOUD_PRUNE_NOTE" ]]; then
        log "$N" "icloud-prune${ICLOUD_PRUNE_NOTE}"
      fi
      if [[ -n "$reconcile_name" ]]; then
        status_write "icloud-package" ok "后台轮询确认上传通过并完成旧包清理 (保留最新 ${ICLOUD_KEEP_LATEST} 份): ${reconcile_name} ${reconcile_size}"
        log "$N" "reconciled icloud-package warn->ok (${reconcile_name})"
      fi
    else
      status_write "$N" fail "iCloud 清理结果无效"
      exit 1
    fi
  fi
fi

if [[ $ran_gc -eq 0 ]]; then
  status_write "$N" ok "巡检正常（GC 节流窗口内已跳过，仅执行轻量化轮询）${ICLOUD_PRUNE_NOTE}"
elif [[ $rc -eq 0 && -n "$raw_json" ]]; then
  summary="$(python3 -c '
import json, sys
d = json.load(sys.stdin)
if d.get("execution") != "applied" or d.get("errors"):
    sys.exit(1)
cnt = d.get("total_candidates", 0)
hum = d.get("reclaimable_human", "0 B")
skp = d.get("protected_skips_count", 0)
ls_cnt = len(d.get("launchservices_ghosts") or [])
fs_cnt = max(0, cnt - ls_cnt)
extra = f"，注销幽灵应用 {ls_cnt} 项" if ls_cnt else ""
print("5-gate TTL GC 完成：回收 {} 项 ({}){}，跳过保护项 {} 项".format(fs_cnt, hum, extra, skp))
' <<< "$raw_json")" || { status_write "$N" fail "GC 未完成，看 logs/$N.log"; exit 1; }
  echo "$now_ep" > "$GC_STAMP"
  status_write "$N" ok "${summary:-5-gate TTL GC 完成}${ICLOUD_PRUNE_NOTE}"
else
  status_write "$N" fail "agent-governor gc 执行异常 (rc=$rc)，看 logs/$N.log"
  notify "Agent 缓存巡检异常" "看 logs/agent-hygiene.log"
fi
log "$N" "done rc=$rc ran_gc=$ran_gc"
exit "$rc"
