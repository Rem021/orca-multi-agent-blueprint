# Autonomous AI Agent Installation & Cross-Mac Migration Guide (`AGENT_INSTALL.md`)

> **Mandatory Instruction for All AI Coding Agents (Claude Code, OpenAI Codex, Google Antigravity, DeepSeek, Cursor, Windsurf, or Muse)**:
> When a user gives you the link `https://github.com/Rem021/orca-multi-agent-blueprint` (or asks you to read this file), **DO NOT blindly run a single mode without checking the user's intent**. Follow **Step 0** below first!

---

## Step 0 (Mandatory): Ask the User Whether They Want **Deployment** or **Cross-Mac Migration**, and Explain the Detailed Steps!

Unless the user has already explicitly stated which exact mode they want in their prompt, you **MUST** first inspect `/Volumes` briefly:

```bash
ls -la /Volumes
```

Then **ask the user which of the following 3 workflows they want to execute**, and **present the detailed step-by-step breakdown** in the user's language (Chinese template below, followed by English):

---

### 🇨🇳 中文交互模板（当用户使用中文时，请向用户展示以下 3 个选项与详细步骤并询问其选择）

> 我已读取 `orca-multi-agent-blueprint` 蓝图协议。本工具包支持**任何 Mac 开发者**进行**本机多 Agent 架构一键部署**，或通过外置硬盘实现**旧 Mac -> 新 Mac 的 1:1 无损跨机迁移**（零私有路径绑定，自动跳过数十 GB 的虚拟机镜像）。
>
> 请问您当前需要执行以下哪种操作？（请回复 **1**、**2** 或 **3**）：
>
> #### 选项 1：【本机直接部署 / 存量环境无损优化】（在当前 Mac 上直接配置）
> - **适用场景**：在当前这台 Mac（全新 Mac，或已有大量 Git 项目与多个 AI 智能体的开发机）上直接部署三层多智能体协同架构、安全硬护栏与多账号同步。
> - **详细执行步骤（共 4 步）**：
>   1. **只读环境预检（`--dry-run`）**：自动识别外置工作盘（校验硬件 `VolumeUUID`）或回退至 `~/AI-Workspace`，探测可用 iOS 模拟器、已安装智能体与存量仓库，输出完整变更预览（不修改任何文件）；
>   2. **零配置自动部署（`--auto`）**：创建标准目录体系（`AI/Projects`、`Worktrees`、`Archive`、`Data`），免 Homebrew 安装 7 款原生加速工具（`rg`、`fd`、`ast-grep`、`just`、`xcbeautify`、`scc`、`repomix`）至 `~/.local/bin` 并写入 `~/.zshenv`，开启 Git 大仓多工作树加速；
>   3. **存量项目无损纳管与多账号对齐**：在不改动任何源码的前提下为项目补齐 `docs/reports/outputs/work` 四分法目录并排除 `/work/`；将全局规则、`PreToolUse` 命令硬护栏（`agent-guard`）、Xcode MCP 与技能软链接 100% 同步到所有 Claude、Codex、Antigravity 账号；
>   4. **全景健康审计（`doctor.py`）**：运行护栏与迁移引擎单元测试，输出只读健康体检报告。
>
> #### 选项 2：【旧 Mac 换机打包 — 准备迁移到新电脑】（在旧 Mac 上执行，打包至外置硬盘）
> - **适用场景**：准备更换新 Mac，希望将当前这台旧 Mac 内置盘上的所有 Agent 会话历史、SQLite 数据库、多账号池、CLI 工具、核心 App、SSH 密钥、守护进程（LaunchAgents）及本地项目无损打包到外置硬盘（自动跳过 `claudevm.bundle` 与 `CoreSimulator` 虚拟机镜像）。
> - **详细执行步骤（共 5 步）**：
>   1. **打包清单预检（`--pack-dry-run`）**：动态扫描 `$HOME` 下的智能体状态目录、活跃 SQLite 数据库（通过 16 字节魔数自动识别并跳过非标准加密文件）、`LaunchAgents` 关联目录与钥匙串服务名、`/Applications` 核心应用，向您汇报完整打包清单；
>   2. **硬链接无损镜像（`rsync -aHR`）**：将 `$HOME` 下的智能体配置、多账号登录态、SSH/Git/Zsh 配置及本地仓库增量同步至外置盘 `${AI_VOLUME_PATH}/AI/Migration/latest/home/`，完整保留硬链接去重；
>   3. **SQLite 并发写时复制热备（APFS `cp -c` + WAL 截断校验）**：4 线程并发对所有活跃 SQLite 数据库执行毫秒级 APFS 写时复制克隆、离线 `PRAGMA wal_checkpoint(TRUNCATE)` 与完整性校验（`integrity=ok`），确保热备零锁冲突；
>   4. **离线运行环境与密钥封装**：通过指纹缓存极速增量打包核心 `.app` 应用、独立 `Python.framework`（避免新机无 Xcode CLT 时弹窗阻塞）、`Brewfile`、Keychain 密钥（`0600` 权限）及蓝图仓库本身至外置盘；
>   5. **生成新机一键还原引导器**：在外置盘生成 `/Volumes/<外置盘>/AI/Migration/restore-on-new-mac.sh` 与 `/Volumes/<外置盘>/AI/Migration/AGENT_RESTORE.md`，到新电脑插上硬盘即可由新电脑的 Agent 读取并无损同步。
>
> #### 选项 3：【新 Mac 无损还原 + 全量部署 — 从外置盘同步到新电脑】（在新 Mac 上执行）
> - **适用场景**：已在旧 Mac 上完成打包并将外置硬盘插到了这台新 Mac 上，希望将旧电脑的所有 Agent 状态、会话记忆、密钥与项目 1:1 无损同步到新电脑并完成新机配置。
> - **详细执行步骤（共 5 步）**：
>   1. **快照核验与还原预检（`--restore-dry-run`）**：读取外置盘 `AI/Migration/latest/metadata/MANIFEST.json`，核对快照完整性、源机器信息及新旧电脑用户名差异；
>   2. **全量文件与核心应用 1:1 还原**：优先引导外置盘内置的 `Python.framework`，通过 `rsync -aHR` 将所有 `$HOME` 智能体目录、SQLite 数据库及缺失的 `/Applications/*.app` 还原到位；
>   3. **跨机用户名路径自动重定向与旧锁清理**：若新旧 Mac 用户名不同，自动将恢复后的软链接、`LaunchAgents` 及智能体配置文件中的 `/Users/<旧用户名>` 重写为 `/Users/<新用户名>`；清除旧机器残留的 `SingletonLock`/`orcad.lock`/`-wal` 锁文件，修复 `~/.ssh` 权限（`700`/`600`）并挂载 `node`/`npm`/`dsh` 软链接；
>   4. **钥匙串与守护进程恢复**：将密钥导入新机登录钥匙串，并向新机 `launchd`（`gui/$UID`）注册所有 `LaunchAgents`；
>   5. **自动串联全量部署与自检**：无缝衔接执行 `./deploy.sh --auto`（已还原的 CLI 工具 0.0 秒极速通过）与 `doctor.py`，输出新机就绪报告。

---

### 🌐 English Interactive Template (If the user communicates in English)

> I have read the `orca-multi-agent-blueprint` protocol. This toolkit supports **both in-place multi-agent deployment** and **1:1 lossless cross-Mac hardware migration via an external SSD** for any macOS developer (with zero hardcoded personal paths and automatic VM exclusion).
>
> Which workflow would you like me to run? (Please reply **1**, **2**, or **3**):
>
> 1. **Option 1: In-Place Deployment & Optimization (Current Mac)**
>    - **Step 1**: Run `--dry-run` to preview volume detection, iOS simulators, installed agents, and non-destructive project adoption without modifying files.
>    - **Step 2**: Run `--auto` to initialize the directory taxonomy, install the 7 native CLI tools (`rg`, `fd`, `ast-grep`, `just`, `xcbeautify`, `scc`, `repomix`) into `~/.local/bin`, and configure `~/.zshenv` + Git multi-worktree acceleration.
>    - **Step 3**: Synchronize `AGENTS.md`/`CLAUDE.md`/`GEMINI.md`, `PreToolUse` safety guardrails (`agent-guard`), Xcode MCP, and skills across all Orca-managed Claude, Codex, and Antigravity profiles.
>    - **Step 4**: Run unit tests and `doctor.py` to output a read-only health report.
> 2. **Option 2: Old Mac — Pack All Agent States & Projects to External SSD (Prepare for New Mac)**
>    - **Step 1**: Run `--pack-dry-run` to auto-discover `$HOME` agent directories, live SQLite databases, custom `LaunchAgents` dependencies, and `/Applications` bundles.
>    - **Step 2**: Mirror `$HOME` paths via `rsync -aHR` (preserving hardlinks across session stores while skipping `claudevm.bundle` and `CoreSimulator`).
>    - **Step 3**: Run parallel APFS Copy-on-Write (`cp -c`) + offline `wal_checkpoint(TRUNCATE)` snapshots and integrity checks across all live SQLite databases.
>    - **Step 4**: Mirror core `.app` bundles, standalone `Python.framework`, `Brewfile`, Keychain secrets (`0600`), and a self-contained copy of this blueprint onto `${AI_VOLUME_PATH}/AI/Migration/latest`.
>    - **Step 5**: Generate `/Volumes/<SSD>/AI/Migration/restore-on-new-mac.sh` and `AGENT_RESTORE.md` so the new Mac's Agent can restore everything in one step.
> 3. **Option 3: New Mac — Lossless Restore from External SSD + Full Auto-Deploy**
>    - **Step 1**: Run `--restore-dry-run` to verify `MANIFEST.json` and preview all paths and `.app` bundles to restore.
>    - **Step 2**: Restore all `$HOME` files, hardlinks, SQLite databases, and missing `/Applications/*.app` bundles from the external SSD.
>    - **Step 3**: Automatically relocate `/Users/<old_user>` -> `/Users/<new_user>` across symlinks, `LaunchAgents`, and agent configs if the macOS username changed; clean stale host locks (`SingletonLock`, `orcad.lock`, `-wal`) and enforce `~/.ssh` permissions (`700`/`600`).
>    - **Step 4**: Import Keychain secrets and bootstrap `LaunchAgents` into `launchd` (`gui/$UID`).
>    - **Step 5**: Automatically chain into `./deploy.sh --auto` and `doctor.py` to verify 100% parity on the new Mac.

---

## Execution Commands by Chosen Option

### If the User Chooses Option 1: In-Place Deployment / Optimization

1. **Preview (`--dry-run`)**:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --dry-run
   ```
2. **Execute (`--auto`)**:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh
   ```
3. **Health Audit (`doctor.py`)**:
   ```bash
   python3 "${AI_VOLUME_PATH:-$HOME/AI-Workspace}/AI/Projects/Mac/outputs/orca-multi-agent-blueprint/scripts/doctor.py"
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
3. **Report to User**:
   Show the generated `MANIFEST.json` summary and tell the user:
   - Everything is packed under `/Volumes/<SSD>/AI/Migration/latest`.
   - On the new Mac, they can simply plug in the SSD and tell any AI Agent on the new Mac to read `/Volumes/<SSD>/AI/Migration/AGENT_RESTORE.md` (or give the new Mac's Agent this GitHub URL and choose **Option 3**), or run `zsh /Volumes/<SSD>/AI/Migration/restore-on-new-mac.sh`.

---

### If the User Chooses Option 3: New Mac — Restore from External SSD + Auto-Deploy

1. **Preview Restore (`--restore-dry-run`)**:
   If the external SSD is mounted at `/Volumes/<SSD>`:
   ```bash
   zsh /Volumes/<SSD>/AI/Migration/restore-on-new-mac.sh --restore-dry-run
   ```
   *(Or via GitHub bootstrapper: `curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --restore-dry-run`)*
2. **Execute Full Lossless Restore + Auto-Deploy (`--restore-from-ssd`)**:
   ```bash
   zsh /Volumes/<SSD>/AI/Migration/restore-on-new-mac.sh
   ```
   *(Or via GitHub bootstrapper: `curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --restore-from-ssd`)*
3. **Health Audit (`doctor.py`)**:
   ```bash
   python3 "${AI_VOLUME_PATH:-$HOME/AI-Workspace}/AI/Projects/Mac/outputs/orca-multi-agent-blueprint/scripts/doctor.py"
   ```
