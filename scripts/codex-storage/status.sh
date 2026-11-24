#!/bin/zsh
# Aggregates status/*.json receipts into a single Markdown dashboard (backup-status.md)
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [[ -f "$HOME/.orca/backup-config.env" ]]; then
  source "$HOME/.orca/backup-config.env"
fi
S="$HOME/Library/Application Support/Codex Storage"
ST="$S/status"
OUT="$S/backup-status.md"
STORAGE_MODE="${STORAGE_MODE:-auto}"
VOL="${AI_VOLUME_PATH:-$HOME/AI-Workspace}"
# Same folder choice as lib.sh: an install that already publishes to the legacy folder keeps it
ICLOUD_DRIVE="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
if [[ -n "${BACKUP_ICLOUD_DIR:-}" ]]; then
  ICLOUD="$BACKUP_ICLOUD_DIR"
elif [[ -d "$ICLOUD_DRIVE/AI备份/每晚小包" ]]; then
  ICLOUD="$ICLOUD_DRIVE/AI备份/每晚小包"
else
  ICLOUD="$ICLOUD_DRIVE/Agent Backups/Nightly"
fi

python3 - "$ST" "$OUT" "$STORAGE_MODE" "$VOL" "$ICLOUD" <<'PY'
import glob, json, os, sys, tempfile, time
st, out, storage_mode, vol, icloud = sys.argv[1:6]
home = os.path.expanduser("~")
rows = []
for p in sorted(glob.glob(os.path.join(st, "*.json"))):
    try:
        d = json.load(open(p))
        name = d.get("name") or os.path.splitext(os.path.basename(p))[0]
        state = d.get("state", "?")
        tm = d.get("time") or d.get("observed_at", "")
        msg = d.get("message", "")
        if not msg and "missed_reconnects_observed" in d:
            msg = f"UUID={d.get('volume_uuid', '')} missed reconnects observed={d.get('missed_reconnects_observed', 0)}"
        rows.append((name, state, tm, d.get("last_ok", ""), msg))
    except Exception as e:
        rows.append((os.path.basename(p), "?", "", "", str(e)))
icon = {"running": "⏳", "ok": "✅", "warn": "⚠️", "fail": "❌", "skipped": "⏭", "stale": "🚨", "absent": "⏏️"}
attention = [n for n, s, *_ in rows if s in ("fail", "stale", "absent")]
mode_label = "internal disk" if (storage_mode == "internal" or not vol.startswith("/Volumes/")) else f"external SSD ({vol})"
L = [
    "# Backup and storage status",
    "",
    f"- **Updated**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
    f"- **Storage mode**: {mode_label}",
    f"- **Workspace root**: `{vol}`",
    f"- **Needs attention**: {', '.join(attention) if attention else 'nothing'}",
    "",
    "| Job | State | Last run | Last success | Details |",
    "|---|---|---|---|---|",
]
for n, s, t, ok_at, m in rows:
    cell = str(m).replace("|", "\\|").replace("\n", " ")[:200]
    L.append(f"| {n} | {icon.get(s, s)} {s} | {t} | {ok_at or '—'} | {cell} |")
L += [
    "",
    "- **States**: `skipped` = a precondition was not met this time; `stale` = no success for a long time, needs a look.",
    "- **Logs**: `~/Library/Application Support/Codex Storage/logs/`",
    f"- **Nightly encrypted packages**: `{icloud.replace(home, '~', 1)}/`",
]
fd, tmp = tempfile.mkstemp(prefix=".backup-status-", dir=os.path.dirname(out))
with os.fdopen(fd, "w") as f:
    f.write("\n".join(L) + "\n")
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, out)
PY
