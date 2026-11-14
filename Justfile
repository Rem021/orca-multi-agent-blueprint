# Orca 多 Agent 蓝图 · 常用指令入口
# 用法：在本目录运行 `just`（列出全部指令）或 `just <指令>`。所有指令都只是 deploy.sh / scripts 的薄封装。

set shell := ["zsh", "-cu"]
set dotenv-load := true
set dotenv-filename := "config.env"

blueprint_dir := justfile_directory()
# 工作区根目录：STORAGE_MODE=internal 时使用内置盘；否则环境变量 / config.env 优先，其次 ~/.orca/backup-config.env
storage_mode := env_var_or_default("STORAGE_MODE", `zsh -c '[[ -f "$HOME/.orca/backup-config.env" ]] && source "$HOME/.orca/backup-config.env"; print -r -- "${STORAGE_MODE:-auto}"'`)
volume_path := if storage_mode == "internal" {
    env_var_or_default("INTERNAL_WORKSPACE_PATH", env_var("HOME") + "/AI-Workspace")
} else {
    env_var_or_default("AI_VOLUME_PATH", `zsh -c '[[ -f "$HOME/.orca/backup-config.env" ]] && source "$HOME/.orca/backup-config.env"; print -r -- "${AI_VOLUME_PATH:-$HOME/AI-Workspace}"'`)
}
expected_uuid := if storage_mode == "internal" {
    "LOCAL"
} else {
    env_var_or_default("AI_VOLUME_UUID", `zsh -c '[[ -f "$HOME/.orca/backup-config.env" ]] && source "$HOME/.orca/backup-config.env"; print -r -- "${AI_VOLUME_UUID:-AUTO}"'`)
}

# 列出全部指令
default:
    @just --list --unsorted

# ───────────── 体检与测试 ─────────────

# 核验工作盘已挂载且卷 UUID 与配置一致
[group('体检与测试')]
verify-volume:
    #!/usr/bin/env zsh
    set -euo pipefail
    vol="{{volume_path}}"
    exp_uuid="{{expected_uuid}}"
    if [[ "{{storage_mode}}" == "internal" || "$vol" != /Volumes/* ]]; then
        echo "✅ 内置盘工作区 $vol"
        exit 0
    fi
    actual_uuid="$(/usr/sbin/diskutil info -plist "$vol" 2>/dev/null | /usr/bin/plutil -extract VolumeUUID raw - 2>/dev/null || true)"
    if [[ -z "$actual_uuid" ]]; then
        echo "❌ 外置工作盘 $vol 未挂载" >&2
        exit 1
    fi
    case "$exp_uuid" in
        ""|AUTO|LOCAL|INTERNAL) ;;
        "$actual_uuid") ;;
        *) echo "❌ 卷 UUID 不匹配：期望 $exp_uuid，实际 $actual_uuid" >&2; exit 1 ;;
    esac
    echo "✅ 外置工作盘 $vol 已挂载，UUID $actual_uuid"

# 只读体检，输出完整 JSON（末尾 summary.attention 是需要处理的事项）
[group('体检与测试')]
doctor: verify-volume
    "{{blueprint_dir}}/deploy.sh" --doctor

# 只读体检，只看结论
[group('体检与测试')]
doctor-summary:
    python3 "{{blueprint_dir}}/scripts/doctor.py" --summary

# 运行全部自测：命令护栏、迁移引擎、备份脚本、体检、Orca 命名插件
[group('体检与测试')]
test:
    #!/usr/bin/env zsh
    set -euo pipefail
    cd "{{blueprint_dir}}"
    for t in tests/test_guard.py tests/test_migrate.py tests/test_backup_scripts.py tests/test_doctor.py tests/test_agent_governor.py; do
        echo "=== $t ==="
        python3 "$t"
    done
    if [[ -x "/Applications/Orca.app/Contents/Resources/bin/orca" ]]; then
        echo "=== plugins/orca-naming-guard ==="
        /Applications/Orca.app/Contents/Resources/bin/orca plugin test "{{blueprint_dir}}/plugins/orca-naming-guard" --json >/dev/null && echo "OK"
    fi

# 只测命令护栏 (guard.py)
[group('体检与测试')]
test-guardrails:
    python3 "{{blueprint_dir}}/tests/test_guard.py"

# 预览一次完整部署会做什么，不写任何文件
[group('体检与测试')]
dry-run:
    "{{blueprint_dir}}/deploy.sh" --dry-run

# ───────────── 备份 ─────────────

# 安装或更新 8 个分级备份与卫生巡检任务（LaunchAgent）
[group('备份')]
setup-backups: verify-volume
    "{{blueprint_dir}}/deploy.sh" --setup-backups

# 查看各备份任务最近一次运行结果
[group('备份')]
backup-status:
    "{{blueprint_dir}}/deploy.sh" --backup-status

# 立即执行一次 iCloud 加密小包备份
[group('备份')]
backup-now-icloud:
    zsh "${HOME}/Library/Application Support/Codex Storage/agents/icloud-package.sh"

# 立即执行一次 Restic 热状态备份（需要 NAS 已挂载）
[group('备份')]
backup-now-hot:
    zsh "${HOME}/Library/Application Support/Codex Storage/agents/backup-restic.sh" hot

# ───────────── 换机迁移 ─────────────

# 预演打包：列出会写入外置盘的内容，不写文件
[group('换机迁移')]
pack-dry-run: verify-volume
    "{{blueprint_dir}}/deploy.sh" --pack-dry-run

# 旧 Mac：把 Agent 状态、会话库、密钥与本机仓库打包进外置盘（不含虚拟机）
[group('换机迁移')]
pack-to-ssd: verify-volume
    "{{blueprint_dir}}/deploy.sh" --pack-to-ssd

# 预演恢复：列出会写入本机的内容，不写文件
[group('换机迁移')]
restore-dry-run: verify-volume
    "{{blueprint_dir}}/deploy.sh" --restore-dry-run

# 新 Mac：从外置盘恢复并完成部署（先退出 Orca / Claude / Codex 等桌面应用）
[group('换机迁移')]
restore-from-ssd: verify-volume
    "{{blueprint_dir}}/deploy.sh" --restore-from-ssd

# 查看外置盘上迁移快照的来源、时间与完整性
[group('换机迁移')]
migration-status: verify-volume
    "{{blueprint_dir}}/deploy.sh" --migration-status

# 只重写盘根目录的恢复指南与启动器，不重新打包
[group('换机迁移')]
refresh-guide: verify-volume
    "{{blueprint_dir}}/deploy.sh" --refresh-guide

# 列出第三方后台任务（LaunchAgent）及其状态
[group('换机迁移')]
agents:
    "{{blueprint_dir}}/deploy.sh" --agents

# 暂停第三方后台任务，多个用逗号分隔（重新登录后也不会自动运行）
[group('换机迁移')]
hold-agents labels:
    "{{blueprint_dir}}/deploy.sh" --hold-agents "{{labels}}"

# 启用并加载第三方后台任务，多个用逗号分隔
[group('换机迁移')]
enable-agents labels:
    "{{blueprint_dir}}/deploy.sh" --agents --enable-agents "{{labels}}"

# ───────────── 存储模式 ─────────────

# 部署为外置固态硬盘模式 (/Volumes/<SSD>/AI)
[group('存储模式')]
use-external-storage:
    "{{blueprint_dir}}/deploy.sh" --auto --external

# 部署为内置硬盘模式 (~/AI-Workspace/AI)，不会搬运已有项目
[group('存储模式')]
use-internal-storage:
    "{{blueprint_dir}}/deploy.sh" --auto --internal

# ───────────── 日常协作 ─────────────

# 给某个仓库安装 pre-push 护栏（拦截强推共享分支与受限分支）
[group('日常协作')]
install-pre-push repo:
    "{{blueprint_dir}}/deploy.sh" --install-pre-push "{{repo}}"

# 查看 Agent 存储占用与 SQLite 状态
[group('日常协作')]
slim-status: verify-volume
    python3 "{{blueprint_dir}}/scripts/agent-slim.py" status

# 查看 iOS 模拟器租约与正在运行的 xcodebuild
[group('日常协作')]
sim-leases: verify-volume
    #!/usr/bin/env zsh
    set -euo pipefail
    dir="{{volume_path}}/AI/Data/SimulatorCoordination"
    echo "=== 活跃租约 ($dir) ==="
    found=0
    for f in "$dir"/active-*/lease.json(N); do
        found=1
        echo "--- $f ---"
        cat "$f"
        echo
    done
    (( found )) || echo "无"
    echo "=== 运行中的 xcodebuild / simctl ==="
    /usr/bin/pgrep -fl 'xcodebuild|simctl' || echo "无"

# 生成跨 Agent 交接回执 (HANDOFF.json)
[group('日常协作')]
handoff repo="." goal="阶段交接快照": verify-volume
    python3 "{{blueprint_dir}}/scripts/handoff.py" --repo "{{repo}}" --goal "{{goal}}"

# 把工作树归档到 <工作盘>/AI/Archive/<日期>-<主题>（bundle + tar.gz + SHA256，不删除原目录）
[group('日常协作')]
archive-worktree worktree_path topic: verify-volume
    #!/usr/bin/env zsh
    set -euo pipefail
    wt="$(cd "{{worktree_path}}" && pwd)"
    topic="{{topic}}"
    if [[ -z "$topic" || "$topic" == */* || "$topic" == .* ]]; then
        echo "❌ 主题不能为空、不能包含 / 或以 . 开头" >&2
        exit 1
    fi
    dest="{{volume_path}}/AI/Archive/$(date +%Y-%m-%d)-${topic}"
    if [[ -e "$dest" ]]; then
        echo "❌ $dest 已存在，换一个主题名，避免覆盖已有归档" >&2
        exit 1
    fi
    mkdir -p "$dest"
    if git -C "$wt" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "$wt" bundle create "$dest/repo.bundle" --all
        git -C "$wt" bundle verify "$dest/repo.bundle" >/dev/null
        git -C "$wt" status --porcelain=v1 --branch > "$dest/git-status.txt"
    fi
    tar -czf "$dest/worktree-snapshot.tar.gz" -C "${wt:h}" "${wt:t}"
    (cd "$dest" && shasum -a 256 -- *(.) > SHA256SUMS.txt)
    echo "✅ 已归档到 $dest"
    ls -lh "$dest"

# ───────────── 规范审计与缓存治理 (agent-governor) ─────────────

# 审计全局/项目规则文件预算、三端指针一致性与根目录杂物
[group('规范与治理')]
govern-audit:
    python3 "{{blueprint_dir}}/scripts/agent_governor.py" audit

# 预览到期缓存与临时测试目录 5 道门回收（加 --apply 移入废纸篓并写回执）
[group('规范与治理')]
govern-gc *args='':
    python3 "{{blueprint_dir}}/scripts/agent_governor.py" gc {{args}}

# 对历史存量 work/ 目录生成只读体积与陈旧度盘点报告
[group('规范与治理')]
govern-triage *args='':
    python3 "{{blueprint_dir}}/scripts/agent_governor.py" triage {{args}}
