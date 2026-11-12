#!/bin/zsh
# Aggregates status/*.json receipts into a single Markdown dashboard (backup-status.md)
if [[ -f "$HOME/.orca/backup-config.env" ]]; then
  source "$HOME/.orca/backup-config.env"
fi
S="$HOME/Library/Application Support/Codex Storage"
ST="$S/status"
OUT="$S/backup-status.md"
STORAGE_MODE="${STORAGE_MODE:-auto}"
VOL="${AI_VOLUME_PATH:-$HOME/AI-Workspace}"
ICLOUD="${BACKUP_ICLOUD_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/AI备份/每晚小包}"

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
            msg = f"UUID={d.get('volume_uuid', '')} 重连观测={d.get('missed_reconnects_observed', 0)}次"
        rows.append((name, state, tm, d.get("last_ok", ""), msg))
    except Exception as e:
        rows.append((os.path.basename(p), "?", "", "", str(e)))
icon = {"running": "⏳", "ok": "✅", "warn": "⚠️", "fail": "❌", "skipped": "⏭", "stale": "🚨", "absent": "⏏️"}
attention = [n for n, s, *_ in rows if s in ("fail", "stale", "absent")]
mode_label = "本机内置硬盘 (Internal Disk)" if (storage_mode == "internal" or not vol.startswith("/Volumes/")) else f"外置固态硬盘 ({vol})"
L = [
    "# 备份与存储状态总览",
    "",
    f"- **更新时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}",
    f"- **当前存储模式**：{mode_label}",
    f"- **工作区根目录**：`{vol}`",
    f"- **需要处理**：{'、'.join(attention) if attention else '无'}",
    "",
    "| 任务 | 状态 | 本次时间 | 最近成功 | 说明 |",
    "|---|---|---|---|---|",
]
for n, s, t, ok_at, m in rows:
    cell = str(m).replace("|", "\\|").replace("\n", " ")[:200]
    L.append(f"| {n} | {icon.get(s, s)} {s} | {t} | {ok_at or '—'} | {cell} |")
L += [
    "",
    "- **状态说明**：`skipped` 本次条件不满足已跳过；`stale` 已长时间没有成功运行，需要人工处理。",
    "- **日志目录**：`~/Library/Application Support/Codex Storage/logs/`",
    f"- **每晚加密小包目录**：`{icloud.replace(home, '~', 1)}/`",
]
fd, tmp = tempfile.mkstemp(prefix=".backup-status-", dir=os.path.dirname(out))
with os.fdopen(fd, "w") as f:
    f.write("\n".join(L) + "\n")
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, out)
PY
