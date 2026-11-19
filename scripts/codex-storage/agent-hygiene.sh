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

# Periodic iCloud backup package polling & rolling pruner:
#   - When icloud-package is in `warn` state (delayed upload), polls every 30m until upload completes,
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
      --icloud-dir "$ICLOUD" \
      --storage-dir "$S" \
      --host "$SAFE_HOST" \
      --keep-latest "$ICLOUD_KEEP_LATEST" \
      --max-age-days "$ICLOUD_MAX_AGE_DAYS" \
      --scope "$ICLOUD_PRUNE_SCOPE" \
      --stale-host-days "$ICLOUD_STALE_HOST_DAYS" 2>>"$LOGD/$N.log")" || true
    if [[ -n "$icloud_prune_json" ]]; then
      echo "$now_ep" > "$ICLOUD_PRUNE_STAMP"
      eval_out="$(python3 - "$icloud_prune_json" "$icloud_state" <<'PYIC'
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
prev_state = sys.argv[2]
pruned_cnt = int(d.get("pruned_count") or 0)
reclaimed = str(d.get("reclaimed_human") or "0 B")
pending = d.get("pending_own_uploads") or []
latest = d.get("latest_own_uploaded") or None
if pruned_cnt > 0:
    print(f"NOTE=，轮询清理 iCloud 旧包 {pruned_cnt} 份 ({reclaimed})")
if prev_state == "warn" and not pending and isinstance(latest, dict) and latest.get("name"):
    print(f"RECONCILE_NAME={latest['name']}")
    print(f"RECONCILE_SIZE={latest.get('size_human', '')}")
PYIC
      )"
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
    fi
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
  status_write "$N" ok "${summary:-5-gate TTL GC 完成}${ICLOUD_PRUNE_NOTE}"
else
  status_write "$N" fail "agent-governor gc 执行异常 (rc=$rc)，看 logs/$N.log"
  notify "Agent 缓存巡检异常" "看 logs/agent-hygiene.log"
fi
log "$N" "done rc=$rc"
