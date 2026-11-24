#!/bin/zsh
# Hourly upkeep job (agent-hygiene.sh). Two independent parts:
# 1. iCloud package poll and rolling prune (icloud_prune.py): on every wake-up while the nightly
#    package still waits for its upload (receipt `warn`), otherwise every
#    BACKUP_ICLOUD_PRUNE_INTERVAL_SEC (default 1 h). It runs whatever the state of the workspace
#    volume or of the cache GC.
# 2. `agent-governor gc --mode daily` at most every AGENT_HYGIENE_GC_INTERVAL_SEC (default 4 h; 0 runs
#    it every hour), and only while the workspace volume is ready. It moves expired work/_scratch
#    (>3 d), work/_tests (>7 d), stale DerivedData (>12 h), idle XCTest runners, profile .tmp/snapshots,
#    excess .bak files and idle ephemeral simulators into ~/.Trash/agent-governor-*.
#    AGENT_GOVERNOR_GC_ARGS adds arguments; it does not bypass the interval.
source "$(dirname "$0")/lib.sh"
N=agent-hygiene
take_lock "$N"
STALE_H=$(( STALE_ALERT_HOURS * 2 ))
log "$N" "start"

prev_state="$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("state", ""))
except Exception:
    pass' "$ST/$N.json" 2>/dev/null)"

# Receipt of this run: the most severe part wins (ok < warn < fail); notes are joined
STATE=ok
NOTES=()
raise_state() {
  case "$1:$STATE" in
    fail:*|warn:ok) STATE="$1" ;;
  esac
}
alert() {
  # One notification per failure streak, not one per hourly run
  [[ "$prev_state" == fail ]] || notify "Agent upkeep problem" "$1"
}

# --- 1. iCloud package poll and prune -----------------------------------------------------------
ICLOUD_PRUNE_SCRIPT="$AG/icloud_prune.py"
ICLOUD_PRUNE_STAMP="$ST/.icloud-prune.stamp"
if [[ "${AGENT_HYGIENE_SKIP_ICLOUD_PRUNE:-0}" != "1" && -f "$ICLOUD_PRUNE_SCRIPT" ]]; then
  icloud_state="$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("state", ""))
except Exception:
    pass' "$ST/icloud-package.json" 2>/dev/null)"
  now_ep="$(date +%s)"
  last_prune_ep="$(cat "$ICLOUD_PRUNE_STAMP" 2>/dev/null)"
  [[ "$last_prune_ep" == <-> ]] || last_prune_ep=0
  prune_interval="$ICLOUD_PRUNE_INTERVAL_SEC"
  [[ "$prune_interval" == <-> ]] || prune_interval=3600
  if [[ "$icloud_state" == "warn" ]] || (( now_ep - last_prune_ep >= prune_interval )); then
    icloud_prune_json="$(python3 "$ICLOUD_PRUNE_SCRIPT" --apply --evict-kept --json \
      --icloud-dir "$ICLOUD" --storage-dir "$S" --host "$SAFE_HOST" \
      --keep-latest "$ICLOUD_KEEP_LATEST" --max-age-days "$ICLOUD_MAX_AGE_DAYS" \
      --scope "$ICLOUD_PRUNE_SCOPE" --stale-host-days "$ICLOUD_STALE_HOST_DAYS" 2>>"$LOGD/$N.log")"
    prune_rc=$?
    print -r -- "$now_ep" > "$ICLOUD_PRUNE_STAMP"
    prune_eval="$(python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    pruned_cnt = int(d["pruned_count"])
except Exception:
    sys.exit(2)
reclaimed, failed_cnt = d.get("reclaimed_human", "0 B"), d.get("failed_count", 0)
if pruned_cnt > 0:
    print(f"NOTE=pruned {pruned_cnt} old iCloud package(s) ({reclaimed})")
if failed_cnt:
    print(f"WARN=could not prune {failed_cnt} iCloud package(s), see the log")
for name in d.get("future_dated") or []:
    print(f"WARN={name} is dated more than a day in the future (wrong clock?); kept, delete it by hand")
latest = d.get("latest_own_uploaded")
if sys.argv[1] == "warn" and not d.get("pending_own_uploads") and latest:
    try:
        with open(sys.argv[2], encoding="utf-8") as receipt:
            previous = json.load(receipt)
    except Exception:
        previous = {}
    if previous.get("state") == "warn" and latest["name"] in previous.get("message", ""):
        print("RECONCILE_NAME=" + latest["name"])
        print("RECONCILE_SIZE=" + latest["size_human"])
' "$icloud_state" "$ST/icloud-package.json" <<< "$icloud_prune_json")"
    if (( $? != 0 )); then
      raise_state fail
      NOTES+=("iCloud prune returned no valid report (rc=$prune_rc), see logs/$N.log")
      alert "iCloud prune failed, see logs/$N.log"
    else
      reconcile_name=""
      reconcile_size=""
      while IFS= read -r kv; do
        case "$kv" in
          NOTE=*) NOTES+=("${kv#NOTE=}"); log "$N" "icloud-prune: ${kv#NOTE=}" ;;
          WARN=*) raise_state warn; NOTES+=("${kv#WARN=}"); log "$N" "icloud-prune: ${kv#WARN=}" ;;
          RECONCILE_NAME=*) reconcile_name="${kv#RECONCILE_NAME=}" ;;
          RECONCILE_SIZE=*) reconcile_size="${kv#RECONCILE_SIZE=}" ;;
        esac
      done <<< "$prune_eval"
      if [[ -n "$reconcile_name" ]]; then
        status_write "icloud-package" ok "Upload confirmed by the hourly poll; older packages pruned (keeping the newest ${ICLOUD_KEEP_LATEST}): ${reconcile_name} ${reconcile_size}"
        log "$N" "reconciled icloud-package warn->ok (${reconcile_name})"
      fi
    fi
  fi
fi

# --- 2. Cache GC ----------------------------------------------------------------------------------
GOV_BIN="$HOME/.local/bin/agent-governor"
if [[ ! -x "$GOV_BIN" ]]; then
  SCRIPT_FALLBACK="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)/agent_governor.py"
  [[ -f "$SCRIPT_FALLBACK" ]] && GOV_BIN="$SCRIPT_FALLBACK" || GOV_BIN=""
fi
GC_STAMP="$ST/.agent-gc.stamp"
GC_LAST="$ST/.agent-gc.last"
gc_interval="${AGENT_HYGIENE_GC_INTERVAL_SEC:-14400}"
[[ "$gc_interval" == <-> ]] || gc_interval=14400
now_ep="$(date +%s)"
last_gc_ep="$(cat "$GC_STAMP" 2>/dev/null)"
[[ "$last_gc_ep" == <-> ]] || last_gc_ep=0
gc_skip_reason=""

if [[ -z "$GOV_BIN" ]]; then
  gc_skip_reason="agent-governor is not installed ($HOME/.local/bin/agent-governor)"
elif ! vol_mounted; then
  gc_skip_reason="Workspace is not ready ($VOL/AI); cache GC skipped"
elif (( gc_interval > 0 && now_ep - last_gc_ep < gc_interval )); then
  # Throttled: repeat the result of the last GC run
  gc_state="ok"
  gc_msg="Cache GC ran $(( (now_ep - last_gc_ep) / 60 )) min ago"
  if [[ -f "$GC_LAST" ]]; then
    gc_lines=("${(@f)$(<"$GC_LAST")}")
    [[ "${gc_lines[1]}" == (ok|warn|fail) ]] && gc_state="${gc_lines[1]}"
    [[ -n "${gc_lines[2]:-}" ]] && gc_msg="${gc_lines[2]} (${gc_msg#Cache GC })"
  fi
  log "$N" "gc throttled (elapsed=$(( now_ep - last_gc_ep ))s < ${gc_interval}s)"
  raise_state "$gc_state"
  NOTES=("$gc_msg; next GC within $(( (gc_interval - (now_ep - last_gc_ep) + 59) / 60 )) min" "${NOTES[@]}")
else
  extra_gc_args=( ${(Q)${(z)AGENT_GOVERNOR_GC_ARGS:-}} )
  raw_json="$(python3 "$GOV_BIN" gc --mode daily --root "$VOL/AI/Projects" --home "$HOME" "${extra_gc_args[@]}" --apply --json 2>>"$LOGD/$N.log")"
  gc_rc=$?
  # Stamp every attempt: a GC that keeps failing must not turn into an hourly full scan
  print -r -- "$now_ep" > "$GC_STAMP"
  gc_eval="$(python3 -c '
import json, sys
rc = sys.argv[1]
try:
    d = json.loads(sys.stdin.read())
    execution = d["execution"]
except Exception:
    print("fail"); print(f"agent-governor gc returned no valid report (rc={rc}), see the log"); sys.exit(0)
errors = d.get("errors") or []
cnt = int(d.get("total_candidates", 0))
ls_cnt = len(d.get("launchservices_ghosts") or [])
done = "Cache GC: reclaimed {} item(s) ({}){}, {} protected item(s) skipped".format(
    max(0, cnt - ls_cnt), d.get("reclaimable_human", "0 B"),
    f", unregistered {ls_cnt} ghost app(s)" if ls_cnt else "", d.get("protected_skips_count", 0))
if execution == "applied" and not errors:
    print("ok"); print(done)
elif execution == "applied":
    print("warn"); print(f"{done}; {len(errors)} item(s) failed, see the log")
elif execution == "skipped-locked":
    print("ok"); print("Cache GC skipped: another GC run holds the lock")
elif execution == "dry-run":
    print("ok"); print(f"Cache GC dry run: {cnt} candidate(s)")
else:
    first = errors[0].get("error", "") if errors and isinstance(errors[0], dict) else ""
    print("fail"); print(f"Cache GC aborted ({execution})" + (": " + first if first else ""))
' "$gc_rc" <<< "$raw_json")"
  gc_lines=("${(@f)gc_eval}")
  gc_state="${gc_lines[1]:-fail}"
  gc_msg="${gc_lines[2]:-agent-governor gc failed (rc=$gc_rc)}"
  print -r -- "$gc_state"$'\n'"$gc_msg" > "$GC_LAST"
  log "$N" "gc rc=$gc_rc state=$gc_state: $gc_msg"
  raise_state "$gc_state"
  NOTES=("$gc_msg" "${NOTES[@]}")
  [[ "$gc_state" == fail ]] && alert "$gc_msg"
fi

message="${(j:; :)NOTES}"
if [[ -n "$gc_skip_reason" && "$STATE" == ok ]]; then
  skip_job "$N" "$gc_skip_reason${message:+; $message}" "$STALE_H"
else
  [[ -n "$gc_skip_reason" ]] && message="$gc_skip_reason${message:+; $message}"
  status_write "$N" "$STATE" "${message:-Upkeep done}"
fi
log "$N" "done state=$STATE"
[[ "$STATE" != fail ]]
