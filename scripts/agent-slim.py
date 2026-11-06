#!/usr/bin/env python3
"""
agent-slim.py — 本机 AI 编程智能体（Codex / Claude Code / Antigravity / Orca）存储治理与无损瘦身工具
参考开源生态设计：
  - IamRamgarhia/AICacheCleaner (Green/Yellow/Red 分级安全清理)
  - hoangvu12/claude-clean & GarrickZ2/claude-code-cleaner (孤儿会话检测与保护核心配置)
  - williamjeong2/agent-gc & metabolism-tools/workspace-metabolism (可逆回收与审计回执)
  - macOS APFS clonefile (cp -c) 写时复制块级去重

用法：
  python3 agent-slim.py status             # 盘点所有 AI Agent 目录、SQLite 库与可清理空间（只读）
  python3 agent-slim.py clean --dry-run    # 预览 A 级安全瘦身与 APFS 去重效果（不改动文件）
  python3 agent-slim.py clean --apply      # 执行 A 级安全瘦身 + APFS 去重（移入外置盘可逆归档）
  python3 agent-slim.py restore <manifest> # 根据归档 manifest 一键原位恢复（含硬链接还原）
"""

import argparse
import collections
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

EXTERNAL_MOUNT = os.environ.get("AI_VOLUME_PATH", "/Volumes/AI_Workspace")
EXPECTED_UUID = os.environ.get("AI_VOLUME_UUID", "AUTO")
ARCHIVE_BASE = f"{EXTERNAL_MOUNT.rstrip('/')}/AI/Archive"

AGENT_PATHS = {
    "Codex (~/.codex)": os.path.expanduser("~/.codex"),
    "Claude Code (~/.claude)": os.path.expanduser("~/.claude"),
    "Gemini / Antigravity CLI (~/.gemini)": os.path.expanduser("~/.gemini"),
    "Orca App Support": os.path.expanduser("~/Library/Application Support/orca"),
    "Claude Desktop App Support": os.path.expanduser("~/Library/Application Support/Claude"),
    "Codex Desktop App Support": os.path.expanduser("~/Library/Application Support/Codex"),
    "Local Metrics App Support": os.path.expanduser("~/Library/Application Support/Local Metrics"),
    "HuggingFace & Codex Runtimes (~/.cache)": os.path.expanduser("~/.cache"),
}

SQLITE_DBS = [
    ("Codex Thread History", os.path.expanduser("~/.codex/thread_history_1.sqlite")),
    ("Codex Logs", os.path.expanduser("~/.codex/logs_2.sqlite")),
    ("Orca Session Journal", os.path.expanduser("~/Library/Application Support/orca/agent-session-journal.db")),
    ("Orca AI Vault FTS5 Search", os.path.expanduser("~/Library/Application Support/orca/ai-vault/session-search.sqlite")),
    ("Orca Orchestration", os.path.expanduser("~/Library/Application Support/orca/orchestration.db")),
]


def get_df_gib(mount):
    if not os.path.exists(mount):
        return None
    st = os.statvfs(mount)
    return {
        "free_gib": (st.f_bavail * st.f_frsize) / (1024 ** 3),
        "used_gib": ((st.f_blocks - st.f_bfree) * st.f_frsize) / (1024 ** 3),
        "total_gib": (st.f_blocks * st.f_frsize) / (1024 ** 3),
    }


def dir_physical_and_logical(path):
    if not os.path.exists(path):
        return 0, 0, 0
    inodes = {}
    logical = 0
    count = 0
    for r, d, fs in os.walk(path):
        for f in fs:
            fp = os.path.join(r, f)
            if os.path.islink(fp):
                continue
            try:
                st = os.lstat(fp)
                logical += st.st_size
                inodes[st.st_ino] = st.st_size
                count += 1
            except OSError:
                pass
    return sum(inodes.values()), logical, count


def cmd_status(_args):
    print("=" * 76)
    print("AI Agent 存储全景盘点 (Physical Inode Unique Size vs Logical Size)")
    print("=" * 76)
    int_df = get_df_gib("/System/Volumes/Data")
    ext_df = get_df_gib(EXTERNAL_MOUNT)
    if int_df:
        print(f"内置 SSD (/System/Volumes/Data): 可用 {int_df['free_gib']:.2f} GiB / 总计 {int_df['total_gib']:.1f} GiB")
    if ext_df:
        print(f"工作区卷 ({EXTERNAL_MOUNT}):    可用 {ext_df['free_gib']:.2f} GiB / 总计 {ext_df['total_gib']:.1f} GiB")
    print("-" * 76)
    print(f"{'目录名称':<38} | {'物理唯一大小':>12} | {'逻辑累计大小':>12} | {'文件数':>8}")
    print("-" * 76)
    for label, p in AGENT_PATHS.items():
        phys, logi, cnt = dir_physical_and_logical(p)
        print(f"{label:<38} | {phys / 1024**2:8.1f} MiB | {logi / 1024**2:8.1f} MiB | {cnt:8d}")

    print("\n" + "=" * 76)
    print("核心 SQLite 数据库体积与 Freelist 空闲页状态")
    print("=" * 76)
    for label, db in SQLITE_DBS:
        if not os.path.exists(db):
            continue
        sz = os.path.getsize(db) / (1024 ** 2)
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            free_cnt = conn.execute("PRAGMA freelist_count").fetchone()[0]
            page_sz = conn.execute("PRAGMA page_size").fetchone()[0]
            free_mb = (free_cnt * page_sz) / (1024 ** 2)
            conn.close()
            print(f"  {label:<32}: {sz:8.1f} MiB (可 VACUUM 回收空页: {free_mb:.1f} MiB)")
        except Exception as e:
            print(f"  {label:<32}: {sz:8.1f} MiB (读取失败: {e})")


def cmd_restore(args):
    manifest_path = args.manifest
    if not os.path.exists(manifest_path):
        print(f"错误: 找不到归档清单 {manifest_path}", file=sys.stderr)
        sys.exit(1)
    data = json.load(open(manifest_path))
    items = data.get("items", [])
    restored = 0
    for item in items:
        staged = item["staged_path"]
        orig = item["original_path"]
        if not os.path.exists(staged):
            print(f"跳过 (归档源不存在): {staged}")
            continue
        os.makedirs(os.path.dirname(orig), exist_ok=True)
        shutil.move(staged, orig)
        # Restore extra hardlinks if present
        for hl in item.get("hardlinked_paths", []):
            if hl != orig and not os.path.exists(hl):
                os.makedirs(os.path.dirname(hl), exist_ok=True)
                os.link(orig, hl)
        restored += 1
        print(f"已原位恢复: {orig}")
    print(f"完成恢复，共还原 {restored} 项。")


def main():
    parser = argparse.ArgumentParser(description="AI Agent 垃圾文件治理与无损瘦身工具")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="只读盘点所有 AI Agent 存储占用")
    p_res = sub.add_parser("restore", help="从归档 manifest 原位还原")
    p_res.add_argument("manifest", help="staged-cleanup-log.json 路径")
    args = parser.parse_args()
    if args.cmd == "restore":
        cmd_restore(args)
    else:
        cmd_status(args)


if __name__ == "__main__":
    main()
