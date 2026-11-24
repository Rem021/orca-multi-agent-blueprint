#!/bin/zsh
# Weekly disaster-recovery restore drill (restore-drill.sh)
# Restores a few small paths from THIS host's newest hot snapshot into ~/Library/Caches/restic-drill
# with `restic restore --verify`, then removes the scratch copy. The paths are chosen from what the
# snapshot actually contains, so the drill also works on a Mac without Codex or Claude.
umask 077
source "$(dirname "$0")/lib.sh"
N=restore-drill
take_lock "$N"
STALE_H=$(( STALE_ALERT_HOURS * 14 ))
[[ -x "$RESTIC" ]] || { skip_job "$N" "restic is not installed" "$STALE_H"; exit 0; }
nas_mounted || { skip_job "$N" "${NAS_REASON:-Backup target or NAS is not mounted}" "$STALE_H"; exit 0; }

T="$HOME/Library/Caches/restic-drill"
_cleanup_drill() { rm -rf -- "$T"; }
_EXIT_HOOKS+=(_cleanup_drill)
rm -rf -- "$T"
mkdir -p "$T"
log "$N" "start"

drill_fail() {
  status_write "$N" fail "$1"
  notify "Restore drill failed" "$1"
  log "$N" "failed: $1"
  exit 1
}

snap_json="$(caffeinate -i "$RESTIC" -r "$REPO" --retry-lock "$RESTIC_RETRY_LOCK" snapshots --json --latest 1 --tag hot --host "$HOSTTAG" 2>>"$LOGD/$N.log")" \
  || drill_fail "Could not list the snapshots of host $HOSTTAG, see logs/$N.log"

# First line: snapshot id. Following lines: paths to restore (small ones first; a path that the
# snapshot does not contain is skipped)
plan="$(python3 - "$HOME" "$snap_json" <<'PY'
import json, sys
home, raw = sys.argv[1], sys.argv[2]
try:
    snaps = [s for s in (json.loads(raw) or []) if isinstance(s, dict)]
except ValueError:
    snaps = []
if not snaps:
    sys.exit(3)
snap = max(snaps, key=lambda s: str(s.get("time", "")))
roots = [str(p).rstrip("/") for p in snap.get("paths") or []]
PREFERRED = (".codex/memories", ".claude/projects", ".agents/skills", ".gitconfig", ".zshrc",
             ".zprofile", ".zshenv", ".claude.json")
def covered(path):
    return any(path == r or path.startswith(r + "/") for r in roots)
picked = [f"{home}/{rel}" for rel in PREFERRED if covered(f"{home}/{rel}")]
if not picked and roots:
    picked = sorted(roots)[:1]  # nothing familiar: restore one top-level path of the snapshot
print(snap.get("id") or snap.get("short_id") or "latest")
print("\n".join(picked))
PY
)"
case $? in
  0) ;;
  3) skip_job "$N" "No hot snapshot of host $HOSTTAG in the repository yet" "$STALE_H"; exit 0 ;;
  *) drill_fail "Could not read the snapshot list of host $HOSTTAG" ;;
esac
plan_lines=("${(@f)plan}")
snap_id="${plan_lines[1]}"
INCLUDES=()
for p in "${(@)plan_lines[2,-1]}"; do
  [[ -n "$p" ]] && INCLUDES+=(--include "$p")
done
(( ${#INCLUDES[@]} )) || drill_fail "Snapshot $snap_id of host $HOSTTAG contains no paths to restore"

OUT=$(caffeinate -i "$RESTIC" -r "$REPO" --retry-lock "$RESTIC_RETRY_LOCK" restore "$snap_id" "${INCLUDES[@]}" --target "$T" --verify 2>&1)
rc=$?
print -r -- "$OUT" | tail -5 >> "$LOGD/$N.log"
n=$(find "$T" -type f | wc -l | tr -d ' ')
if [[ $rc -eq 0 && "$n" -gt 0 ]]; then
  status_write "$N" ok "Restored and verified $n file(s) from snapshot ${snap_id[1,8]}"
else
  status_write "$N" fail "rc=$rc, files=$n: $(print -r -- "$OUT" | tail -1)"
  notify "Restore drill failed" "rc=$rc, restored files=$n, see logs/restore-drill.log"
fi
rm -rf -- "$T"
log "$N" "done rc=$rc files=$n snapshot=${snap_id[1,8]}"
