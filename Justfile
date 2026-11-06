# Mac 多 Agent 协作与系统维护标准入口 (Justfile)
# 用法: 在工作区根目录下运行 `just --list` 或 `just <recipe>`

set shell := ["zsh", "-cu"]

volume_path := env_var_or_default("AI_VOLUME_PATH", "/Volumes/AI_Workspace")
expected_uuid := env_var_or_default("AI_VOLUME_UUID", "AUTO")
blueprint_dir := justfile_directory()

# 默认显示所有可用标准指令
default:
    @just --list

# 1. 核验外置工作盘挂载状态与卷 UUID
verify-volume:
    #!/usr/bin/env zsh
    set -euo pipefail
    if [[ "{{volume_path}}" == /Volumes/* ]]; then
        actual_uuid=$(diskutil info -plist "{{volume_path}}" | plutil -extract VolumeUUID raw -)
        if [[ "{{expected_uuid}}" != "AUTO" && -n "{{expected_uuid}}" && "$actual_uuid" != "{{expected_uuid}}" ]]; then
            echo "❌ 卷 UUID 不匹配: 期望 {{expected_uuid}}, 实际 ${actual_uuid:-未挂载}" >&2
            exit 1
        fi
        echo "✅ 外置工作盘 {{volume_path}} 已挂载且 UUID 核验通过 ($actual_uuid)"
    else
        echo "✅ 本地工作区目录 {{volume_path}} 可用"
    fi

# 2. 运行全面只读健康诊断 (doctor.py)
doctor: verify-volume
    python3 "{{blueprint_dir}}/scripts/doctor.py"

# 3. 查看本机 AI Agent 存储瘦身与 SQLite 状态 (agent-slim.py)
slim-status: verify-volume
    python3 "{{blueprint_dir}}/scripts/agent-slim.py" status

# 4. 查看 iOS 模拟器协调租约与当前运行中的 xcodebuild 测试
sim-leases: verify-volume
    #!/usr/bin/env zsh
    set -euo pipefail
    echo "=== 活跃租约目录 ==="
    ls -la "{{volume_path}}/AI/Data/SimulatorCoordination/active-"* 2>/dev/null || true
    for f in "{{volume_path}}"/AI/Data/SimulatorCoordination/active-*/lease.json(N); do
        [[ -f "$f" ]] && echo "--- $f ---" && cat "$f"
    done
    echo "=== 当前运行中的 xcodebuild / simctl 进程 ==="
    ps aux | rg -i "xcodebuild|simctl" | rg -v "rg -i" || echo "无活跃测试进程"

# 5. 自检命令安全硬护栏 (guard.py)
test-guardrails:
    python3 "{{blueprint_dir}}/tests/test_guard.py"

# 6. 生成跨 Agent 交接回执包 (HANDOFF.json)
handoff repo="." goal="阶段交接快照": verify-volume
    python3 "{{blueprint_dir}}/scripts/handoff.py" --repo "{{repo}}" --goal "{{goal}}"

# 7. 安全归档工作树到 ${AI_VOLUME_PATH}/AI/Archive/<日期>-<主题>（不执行删除）
archive-worktree worktree_path topic: verify-volume
    #!/usr/bin/env zsh
    set -euo pipefail
    wt=$(cd "{{worktree_path}}" && pwd)
    date_str=$(date +%Y-%m-%d)
    dest="{{volume_path}}/AI/Archive/${date_str}-{{topic}}"
    mkdir -p "$dest"
    if git -C "$wt" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "$wt" bundle create "$dest/repo.bundle" --all
        git -C "$wt" status --porcelain=v1 > "$dest/git-status.txt"
    fi
    tar -czf "$dest/worktree-snapshot.tar.gz" -C "$(dirname "$wt")" "$(basename "$wt")"
    shasum -a 256 "$dest"/* > "$dest/SHA256SUMS.txt"
    echo "✅ 已归档至 $dest (含 bundle, tar.gz 与 SHA256SUMS.txt)"

# 8. 预演跨 Mac 离线换机打包 (不写文件，不含虚拟机)
pack-dry-run: verify-volume
    "{{blueprint_dir}}/deploy.sh" --pack-dry-run

# 9. 一键将旧 Mac 内置盘所有 Agent 状态、SQLite 会话库、密钥与本地仓库无损打包进外置盘 (不含虚拟机)
pack-to-ssd: verify-volume
    "{{blueprint_dir}}/deploy.sh" --pack-to-ssd

# 10. 在新 Mac 上从外置盘一键无损还原所有 Agent 状态并自动执行全量环境对齐部署
restore-from-ssd: verify-volume
    "{{blueprint_dir}}/deploy.sh" --restore-from-ssd

# 11. 查看外置盘最新跨机迁移快照状态与完整性回执
migration-status: verify-volume
    "{{blueprint_dir}}/deploy.sh" --migration-status
