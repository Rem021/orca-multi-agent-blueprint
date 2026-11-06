# Autonomous AI Agent Installation, Storage Choice & Cross-Mac Migration Guide (`AGENT_INSTALL.md`)

> **Mandatory Instruction for All AI Coding Agents (Claude Code, OpenAI Codex, Google Antigravity, DeepSeek, Cursor, Windsurf, or Muse)**:
> When a user gives you the link `https://github.com/Rem021/orca-multi-agent-blueprint` (or asks you to read this file), **DO NOT blindly run a single mode without checking the user's intent**. Follow **Step 0** below first!

---

## Step 0 (Mandatory): Ask the User for (1) Workflow Mode and (2) AI Storage Location (External SSD vs. Internal Disk), and Explain the Detailed Steps!

Unless the user has already explicitly stated both their workflow mode and storage preference in their prompt, you **MUST** first inspect `/Volumes` briefly:

```bash
ls -la /Volumes
```

Then **ask the user which workflow (1 / 2 / 3) and which storage mode (A: External SSD / B: Mac Internal Disk) they want to execute**, and **present the detailed step-by-step breakdown** in the user's language (Chinese template below, followed by English):

---

### 🇨🇳 中文交互模板（当用户使用中文时，请向用户展示以下选项与详细步骤并询问其选择）

> 我已读取 `orca-multi-agent-blueprint` 蓝图协议。本工具包支持**任何 Mac 开发者**进行**本机多 Agent 架构与每日自动备份部署**，或通过外置硬盘实现**旧 Mac -> 新 Mac 的 1:1 无损跨机迁移**（零私有路径绑定，自动跳过数十 GB 的虚拟机镜像），并且支持自由选择**将所有 AI 文件存入外置硬盘还是本机内置硬盘**。
>
> 请告诉我您的两项选择（例如回复 **`1A`**、**`1B`**、**`2A`** 或 **`3A`**）：
>
> ---
> ### 第一部分：请选择操作模式（1 / 2 / 3）
>
> #### 选项 1：【本机直接部署 / 存量环境无损优化 + 全自动每日备份】（在当前 Mac 上直接配置）
> - **适用场景**：在当前这台 Mac（全新 Mac，或已有大量 Git 项目与多个 AI 智能体的开发机）上直接部署三层多智能体协同架构、文件四分法治理、安全硬护栏与 7 项全自动分级备份任务。
> - **详细执行步骤（共 5 步）**：
>   1. **只读环境预检（`--dry-run`）**：自动识别您选择的存储位置（外置固态硬盘 `/Volumes/<SSD>/AI` 并校验硬件 `VolumeUUID`，或本机内置硬盘 `~/AI-Workspace/AI`），探测可用 iOS 模拟器、已安装智能体与存量仓库，输出完整变更预览（不修改任何文件）；
>   2. **零配置标准目录与加速工具链部署（`--auto`）**：创建标准目录体系（`AI/Projects`、`Worktrees`、`Archive`、`Data`、`Backups`），免 Homebrew 安装 7 款原生加速工具（`rg`、`fd`、`ast-grep`、`just`、`xcbeautify`、`scc`、`repomix`）至 `~/.local/bin` 并写入 `~/.zshenv`，开启 Git 大仓多工作树加速；
>   3. **存量项目「四分法」无损纳管**：在不改动任何源码的前提下为项目补齐 `docs/`（文档）、`reports/`（审计回执）、`outputs/`（正式产物）、`work/`（临时试验场）四个标准目录并将 `/work/` 写入 `.git/info/exclude`；
>   4. **7 项全自动每日/每小时/每周备份与硬件巡检配置（`setup_backups.py`）**：自动生成加密密钥并存入 macOS 钥匙串，部署 7 项 `LaunchAgents` 守护任务：
>      - **每晚 02:30 iCloud 加密小包备份（`icloud-package`）**：对活跃 SQLite 执行 APFS 写时复制（`cp -c`）+ `wal_checkpoint(TRUNCATE)` 快照，打包核心配置并经 `AES-256-CBC PBKDF2` 加密写入 iCloud Drive，验证解密无误后调用 `brctl evict` 释放本地占用；
>      - **每小时整点热状态备份（`backup-hot`）**：将内置盘所有智能体状态与安全 SQLite 快照增量备份至 Restic 仓库；
>      - **每日 03:00 项目全量备份（`backup-projects`）**：将 `AI/Projects`、`Worktrees`、`Archive` 与 `Data` 增量备份至 Restic 仓库；
>      - **每周日 04:00 媒体备份（`backup-media`）**、**每周日 05:30 快照修剪与完整性校验（`maintenance`）**、**每周日 07:00 自动灾难恢复演练（`restore-drill`）** 以及 **每 60 秒存储卷硬件 UUID 与掉盘重连巡检（`volume-guard`）**；
>   5. **多账号对齐与全景健康审计（`sync_profiles.py` + `doctor.py`）**：将全局规则、`PreToolUse` 命令硬护栏（`agent-guard`）、Xcode MCP 与技能软链接 100% 同步到所有 Claude、Codex、Antigravity 账号，并输出只读体检报告。
>
> #### 选项 2：【旧 Mac 换机打包 — 准备迁移到新电脑】（在旧 Mac 上执行，打包至外置硬盘）
> - **适用场景**：准备更换新 Mac，希望将当前旧 Mac 内置盘上的所有 Agent 会话历史、SQLite 数据库、多账号池、CLI 工具、核心 App、SSH 密钥、自动备份守护进程（`LaunchAgents`）及本地项目无损打包到外置硬盘（自动跳过 `claudevm.bundle` 与 `CoreSimulator` 虚拟机镜像）。
> - **详细执行步骤（共 5 步）**：
>   1. **打包清单预检（`--pack-dry-run`）**：动态扫描 `$HOME` 下的智能体状态目录、活跃 SQLite 数据库、自动备份脚本与钥匙串服务名、`/Applications` 核心应用；
>   2. **硬链接无损镜像（`rsync -aHR`）**：将 `$HOME` 下的智能体配置、多账号登录态、SSH/Git/Zsh 配置及本地仓库增量同步至 `${AI_VOLUME_PATH}/AI/Migration/latest/home/`，完整保留硬链接去重；
>   3. **SQLite 并发写时复制热备（APFS `cp -c` + WAL 截断校验）**：4 线程并发对所有活跃 SQLite 数据库执行毫秒级 APFS 写时复制克隆、离线 `PRAGMA wal_checkpoint(TRUNCATE)` 与完整性校验（`integrity=ok`）；
>   4. **离线运行环境与密钥封装**：增量打包核心 `.app` 应用、独立 `Python.framework`、`Brewfile`、钥匙串密钥（`0600` 权限，含自动备份密码）及本蓝图仓库至外置盘；
>   5. **生成新机一键还原引导器**：在外置盘生成 `/Volumes/<外置盘>/AI/Migration/restore-on-new-mac.sh` 与 `AGENT_RESTORE.md`，到新电脑插上硬盘即可一键无损恢复。
>
> #### 选项 3：【新 Mac 无损还原 + 全量部署 — 从外置盘同步到新电脑】（在新 Mac 上执行）
> - **适用场景**：已在旧 Mac 完成打包并将外置硬盘插到了新 Mac 上，希望将旧电脑的所有 Agent 状态、会话记忆、自动备份体系、密钥与项目 1:1 无损同步到新电脑。
> - **详细执行步骤（共 5 步）**：
>   1. **快照核验与还原预检（`--restore-dry-run`）**：读取 `AI/Migration/latest/metadata/MANIFEST.json`，核对快照完整性及新旧电脑用户名差异；
>   2. **全量文件与核心应用 1:1 还原**：优先引导外置盘内置的 `Python.framework`，通过 `rsync -aHR` 将所有 `$HOME` 智能体目录、SQLite 数据库及缺失的 `/Applications/*.app` 还原到位；
>   3. **跨机用户名路径自动重定向与旧锁清理**：若新旧 Mac 用户名不同，自动将软链接、`LaunchAgents` 及智能体配置文件中的 `/Users/<旧用户名>` 重写为 `/Users/<新用户名>`；清除残留锁文件并修复 `~/.ssh` 权限；
>   4. **钥匙串与自动备份守护进程恢复**：将密钥导入新机登录钥匙串，并向新机 `launchd`（`gui/$UID`）注册所有 `LaunchAgents`；
>   5. **自动串联全量部署与自检**：无缝衔接执行 `./deploy.sh --auto`（支持继续使用外置盘，或加 `--internal` 切换为新机内置硬盘存储）与 `doctor.py`。
>
> ---
> ### 第二部分：请选择 AI 文件存储位置方案（A / B）
> - **方案 A：存入【外置固态硬盘】（`--external`，路径为 `/Volumes/<外置盘>/AI`）**
>   - **特点**：释放 Mac 内置盘空间，支持硬件 `VolumeUUID` 防误写保护、USB 掉盘重枚举监测，换机时拔下硬盘即可带走全部 `AI/Projects`、`Worktrees`、`Archive` 与备份仓库。
> - **方案 B：存入【Mac 本机内置硬盘】（`--internal`，路径为 `~/AI-Workspace/AI`）**
>   - **特点**：无需挂载任何外部移动硬盘，所有 `AI/Projects`、`Worktrees`、`Archive`、`Data` 与本地 Restic 备份库全部建立在本机内置盘 `~/AI-Workspace/AI` 下，开盖即用、移动办公零外设负担。

---

### 🌐 English Interactive Template (If the user communicates in English)

> I have read the `orca-multi-agent-blueprint` protocol. This toolkit supports **in-place multi-agent deployment with automated daily/hourly/weekly backups**, **dual-mode storage selection (External SSD vs. Mac Internal Disk)**, and **1:1 lossless cross-Mac hardware migration**.
>
> Please tell me your two choices (for example, reply **`1A`**, **`1B`**, **`2A`**, or **`3A`**):
>
> ### Part 1: Choose Workflow Mode (1 / 2 / 3)
> 1. **Option 1: In-Place Deployment, File Governance & Automated Daily Backups (Current Mac)**
>    - **Step 1**: Run `--dry-run` to preview storage location (`external` SSD vs. `internal` disk), iOS simulators, installed agents, and non-destructive project adoption.
>    - **Step 2**: Run `--auto` to initialize the 4-zone directory taxonomy (`docs/`, `reports/`, `outputs/`, `work/`), install the 7 native CLI tools into `~/.local/bin`, and configure `~/.zshenv` + Git multi-worktree acceleration.
>    - **Step 3**: Provision macOS Keychain secrets and bootstrap the **7-job automated backup & hardware watchdog suite** (`icloud-package` nightly at 02:30 with AES-256-CBC + `brctl evict`, `backup-hot` hourly, `backup-projects` daily at 03:00, `backup-media` weekly, `maintenance` weekly prune/check, `restore-drill` weekly verification, and `volume-guard` every 60s).
>    - **Step 4**: Synchronize `AGENTS.md`/`CLAUDE.md`/`GEMINI.md`, `PreToolUse` safety guardrails (`agent-guard`), Xcode MCP, and skills across all Orca profiles, then run `doctor.py`.
> 2. **Option 2: Old Mac — Pack All Agent States, Backups & Projects to External SSD**
>    - Mirrors `$HOME` agent states via `rsync -aHR` (skipping `claudevm.bundle` & `CoreSimulator`), snapshots live SQLite DBs via parallel APFS `cp -c` + `wal_checkpoint(TRUNCATE)`, packs core `.app` bundles, `Python.framework`, Keychain secrets, and generates `restore-on-new-mac.sh`.
> 3. **Option 3: New Mac — Lossless Restore from External SSD + Full Auto-Deploy**
>    - Restores `$HOME` files, hardlinks, SQLite DBs, and `.app` bundles from the SSD, automatically rewrites `/Users/<old_user>` -> `/Users/<new_user>` if the macOS username changed, imports Keychain secrets & `LaunchAgents`, and chains into `./deploy.sh --auto`.
>
> ### Part 2: Choose AI File Storage Location (A / B)
> - **Storage A: External SSD (`--external`, `/Volumes/<SSD>/AI`)** — Hardware `VolumeUUID` verified before every write, zero internal disk bloat, instant cross-Mac portability.
> - **Storage B: Mac Internal Disk (`--internal`, `~/AI-Workspace/AI`)** — Self-contained on your Mac's internal SSD (`~/AI-Workspace/AI`), no external drive required.

---

## Execution Commands by Chosen Option

### If the User Chooses Option 1: In-Place Deployment + Automated Backups

- **With Storage A (External SSD — `--external`)**:
  ```bash
  # 1. Preview (--dry-run)
  curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --dry-run --external
  # 2. Execute (--auto --external)
  curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --auto --external
  ```
- **With Storage B (Mac Internal Disk — `--internal`)**:
  ```bash
  # 1. Preview (--dry-run)
  curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --dry-run --internal
  # 2. Execute (--auto --internal)
  curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --auto --internal
  ```
- **Verify Health & Backup Status**:
  ```bash
  ./deploy.sh --doctor
  ./deploy.sh --backup-status
  ```

---

### If the User Chooses Option 2: Old Mac — Pack to External SSD

1. **Preview Pack (`--pack-dry-run`)**:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --pack-dry-run
   ```
2. **Execute Lossless Pack to SSD (`--pack-to-ssd`)**:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --pack-to-ssd
   ```
   *(Note: If the user specifies a custom external drive path such as `/Volumes/MySSD`, pass `--volume /Volumes/MySSD`.)*

---

### If the User Chooses Option 3: New Mac — Restore from External SSD + Auto-Deploy

1. **Preview Restore (`--restore-dry-run`)**:
   ```bash
   zsh /Volumes/<SSD>/AI/Migration/restore-on-new-mac.sh --restore-dry-run
   ```
2. **Execute Full Lossless Restore + Auto-Deploy (`--restore-from-ssd`)**:
   - Keep AI workspace on the External SSD (`--external`, default):
     ```bash
     zsh /Volumes/<SSD>/AI/Migration/restore-on-new-mac.sh --external
     ```
   - Or restore onto the New Mac's Internal Disk (`--internal`):
     ```bash
     zsh /Volumes/<SSD>/AI/Migration/restore-on-new-mac.sh --internal
     ```
