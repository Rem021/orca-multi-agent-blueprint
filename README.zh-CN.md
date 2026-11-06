# Orca 多智能体协同与工作树治理蓝图

[![English](https://img.shields.io/badge/Language-English-0969da?style=flat)](./README.md)
[![智能体自动部署](https://img.shields.io/badge/智能体协议-AGENT__INSTALL.md-8250df?style=flat)](./AGENT_INSTALL.md)
[![系统平台](https://img.shields.io/badge/支持平台-macOS_(Apple_Silicon_%26_Intel)-1a7f37?style=flat)](https://github.com/Rem021/orca-multi-agent-blueprint)
[![调度中枢](https://img.shields.io/badge/Orca_中枢-v1.4%2B-0969da?style=flat)](https://github.com/Rem021/orca-multi-agent-blueprint)
[![存储拓扑](https://img.shields.io/badge/双模存储-外置硬盘_%7C_内置硬盘-0969da?style=flat)](#四双模存储方案外置固态硬盘-vs-本机内置硬盘与文件四分法体系)
[![自动备份](https://img.shields.io/badge/全自动备份-7_项_LaunchAgents_守护-1a7f37?style=flat)](#五全自动每日每小时每周分级备份与硬件巡检体系)
[![安全硬护栏](https://img.shields.io/badge/PreToolUse-确定性安全拦截-cf222e?style=flat)](./scripts/guard.py)
[![开源协议](https://img.shields.io/badge/开源协议-MIT-656d76?style=flat)](./LICENSE)

> **语言切换**：[**👉 Click here for the English Version (`README.md`)**](./README.md)

面向 macOS 重度多智能体开发者的生产级**三层可插拔架构**、**内置/外置双模存储与文件四分法治理标准**、**7 项全自动每日/每小时/每周分级备份守护系统**与**跨 Mac 1:1 无损迁移方案**。支持在**全新 Mac** 或**已有大量存量项目与多个 AI 智能体的主力开发机**上无损落地。

---

## 一、快速部署、存储模式选择与跨机迁移（支持任何 Mac 开发者一键复用）

### 方式 A：直接将本仓库链接发给任意 AI 智能体（智能询问「操作模式」与「内置/外置存储方案」）

任何开发者都可以复制以下文本，发送给 **Claude Code、OpenAI Codex、Google Antigravity、Cursor 或 Muse**（无论在旧电脑还是新电脑上）：

```text
请阅读 https://github.com/Rem021/orca-multi-agent-blueprint/blob/main/AGENT_INSTALL.md ，先询问我：（1）是要本机部署/优化、旧 Mac 换机打包、还是新 Mac 无损还原；（2）要把所有 AI 文件存入外置固态硬盘（--external）还是本机内置硬盘（--internal），并把详细步骤列出来。
```

智能体读取 [`AGENT_INSTALL.md`](./AGENT_INSTALL.md) 后，会**主动向你询问两项选择（操作模式 `1/2/3` + 存储位置 `A: 外置固态硬盘 / B: 本机内置硬盘`），并详细说明每一步动作**：
1. **场景 1：【本机直接部署 / 存量环境无损优化 + 全自动每日备份】（`--auto`）**：根据你选择的**外置盘（`/Volumes/<外置盘>/AI`）**或**本机内置盘（`~/AI-Workspace/AI`）**创建标准目录体系，无损纳管存量项目（补齐 `docs/reports/outputs/work` 四分法目录），免依赖安装 7 款原生加速工具，自动配置 **7 项每日/每小时/每周自动备份守护进程**，并将全局规则、`PreToolUse` 安全硬护栏、Xcode 桥接与技能同步到所有智能体账号。
2. **场景 2：【旧 Mac 换机打包 — 准备迁移到新电脑】（`--pack-to-ssd`）**：零私有路径硬编码，动态扫描你本机 `$HOME` 下的智能体状态、并发执行 APFS 写时复制（`cp -c`）SQLite 数据库热备与完整性校验、提取 `LaunchAgents` 关联目录与钥匙串密钥、通过指纹缓存增量打包核心 `.app` 与独立 `Python.framework` 至外置盘（自动跳过几十 GB 的 `claudevm.bundle` 与 `CoreSimulator` 虚拟机镜像），并在外置盘生成 `AGENT_RESTORE.md` 供新电脑读取。
3. **场景 3：【新 Mac 无损还原 + 全量部署 — 从外置盘同步到新电脑】（`--restore-from-ssd`）**：在新电脑上读取外置盘快照，1:1 还原所有文件、硬链接与数据库；若新旧 Mac 系统用户名不同，**自动重写软链接、`LaunchAgents` 与配置文件中的 `/Users/<旧用户名>` -> `/Users/<新用户名>`**，清理旧机残留锁、恢复钥匙串与守护任务，并自动串联 `--auto` 完成新机对齐。

### 方式 B：终端一行命令直接部署（可自由指定外置盘或内置盘）

在终端（Terminal / Ghostty / iTerm2）中执行：

```bash
# 自动识别模式（若挂载了外置盘则优先使用 /Volumes/<外置盘>/AI，否则使用本机 ~/AI-Workspace/AI）
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh

# 显式指定方案 A：将所有 AI 文件存入【外置固态硬盘】(/Volumes/<外置盘>/AI)
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --auto --external

# 显式指定方案 B：将所有 AI 文件存入【本机内置硬盘】(~/AI-Workspace/AI)
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --auto --internal

# 只读预检模式（预览将要执行的动作，不修改任何文件）
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --dry-run
```

### 方式 C：只带一块外置硬盘跨 Mac 无损换机（终端一行命令）

1. **在旧 Mac 出发前执行（先预览，再将内置盘智能体状态、SQLite 热备、账号池、SSH 密钥与本地仓库无损打包至外置盘）**：
   ```bash
   curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --pack-dry-run
   curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --pack-to-ssd
   ```
2. **到达新 Mac 插上外置盘一键还原并自动部署（支持纯离线执行，也可直接让新机 Agent 读取 `/Volumes/<外置盘>/AI/Migration/AGENT_RESTORE.md`）**：
   ```bash
   zsh /Volumes/<外置盘名称>/AI/Migration/restore-on-new-mac.sh
   ```

---

## 二、三层可插拔架构设计

为解决单一智能体常见的**上下文窗口污染、单账号限流中断、多任务代码互相覆盖、后台子进程找不到工具链**四大痛点，本方案将整套系统解耦为可随时替换的三层结构：

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/architecture-zh-dark.svg?v=3">
    <source media="(prefers-color-scheme: light)" srcset="./assets/architecture-zh-light.svg?v=3">
    <img alt="三层可插拔架构图" src="./assets/architecture-zh-light.svg?v=3" width="100%">
  </picture>
</p>

| 层级 | 架构角色 | 默认配置 | 可直接替换的备选方案（修改 `config.env`） | 核心职责 |
| :--- | :--- | :--- | :--- | :--- |
| **第一层** | **前端总指挥** | **Muse** | Orca 手机端、Claude 桌面端、自定义脚本 | 接收自然语言需求、拆解跨项目任务、向中枢下发工作树指令、汇总验收结果 |
| **第二层** | **调度与护栏中枢** | **Orca 桌面端与 CLI** | `just` + `projectctl.py` 纯命令行调度 | 管理上百个工作树生命周期、多账号配额自动切换、运行命令安全硬护栏与中文命名守护插件 |
| **第三层** | **执行特工池与双模存储** | **Claude + Codex + Antigravity + DeepSeek** | OpenCode、Cursor、Windsurf、Aider | 在各自独立的 Git 工作树沙盒中并行编码、语法树重构、模拟器测试，配合双模存储与全自动备份 |

> [!NOTE]
> 如需更换前端总指挥、底层执行智能体或存储模式（`external` 外置盘 / `internal` 内置盘），只需修改 [`config.env`](./config.example.env) 中的配置项并运行 `./deploy.sh --apply` 即可一键切换。

---

## 三、五步确定性研发与归档流程

从下发任务到最终合入主干并归档工作树，全流程遵循五步标准闭环：

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/workflow-zh-dark.svg?v=3">
    <source media="(prefers-color-scheme: light)" srcset="./assets/workflow-zh-light.svg?v=3">
    <img alt="五步确定性研发与归档闭环" src="./assets/workflow-zh-light.svg?v=3" width="100%">
  </picture>
</p>

1. **核验存储卷并拉取分支**：通过 `projectctl.py` 核验存储卷状态（外置盘校验硬件 `VolumeUUID`，内置盘校验 `~/AI-Workspace`），从干净的 `main` 主仓拉取独立工作树与任务分支。
2. **护栏下并行开发**：主攻智能体在独立工作树内编码，所有终端命令均经 `agent-guard` 事前校验，Xcode 编译输出经 `xcbeautify` 压缩降噪。
3. **生成紧凑交接包**：执行 `just handoff` 自动提取分支基准哈希、变更文件 `SHA-256` 摘要与验证回执，无需复制冗长聊天记录即可缩减约 80% 交接上下文。
4. **异构模型互审**：由另一模型（如 Claude 编码后交由 Codex 审查）读取交接包与代码差异进行只读审计，严格区分“已核验证据”与“推断假设”。
5. **安全打包归档**：任务合入后，通过 `just archive-worktree` 生成完整 Git 镜像包（`repo.bundle`）、目录快照（`.tar.gz`）与 `SHA256SUMS.txt` 校验单存入 `AI/Archive/`，再清理工作树。

---

## 四、双模存储方案（外置固态硬盘 vs 本机内置硬盘）与文件「四分法」体系

### 4.1 方案选择：把所有 AI 文件存入【外置固态硬盘】还是【本机内置硬盘】？

本框架支持通过 `--external` 或 `--internal`（以及 `config.env` 中的 `STORAGE_MODE`）自由选择或随时切换 AI 文件的物理落盘位置：

| 对比维度 | 方案 A：外置固态硬盘模式（`--external`） | 方案 B：本机内置硬盘模式（`--internal`） |
| :--- | :--- | :--- |
| **根目录路径 (`AI_VOLUME_PATH`)** | `/Volumes/<外置硬盘名>`（如 `/Volumes/AI_Workspace`） | `~/AI-Workspace`（可通过 `INTERNAL_WORKSPACE_PATH` 自定义） |
| **硬件防误写保护** | 每次写入前强制校验硬件 `VolumeUUID`，未挂载时立即阻断防止写入假挂载点 | 标记 `AI_VOLUME_UUID="INTERNAL"`，随系统内置盘全天候在线 |
| **60 秒守护巡检 (`volume-guard`)** | 每 60 秒监测外置盘挂载状态与 USB 总线 `sessionID` 掉盘重连事件 | 每 60 秒校验本地工作区目录与备份目录可用性 |
| **本机磁盘空间占用** | 数百 GB 的主仓库、180+ 工作树与编译缓存全部置于外置盘，极致释放内置盘 | 全量收敛于本机 `~/AI-Workspace/AI`，无需携带任何外接硬盘 |
| **跨 Mac 换机体验** | 拔下硬盘插到新 Mac 即可直接复用全部 `AI/Projects` 与工作树，零拷贝等待 | 换机时可随时插上移动硬盘执行 `--pack-to-ssd` 导出 |
| **一键切换指令** | `just use-external-storage` 或 `./deploy.sh --auto --external` | `just use-internal-storage` 或 `./deploy.sh --auto --internal` |

### 4.2 统一目录结构与项目内「四分法」标准

无论你选择外置固态硬盘（`/Volumes/AI_Workspace`）还是本机内置硬盘（`~/AI-Workspace`），底层的 `AI/` 目录拓扑与项目内「四分法」文件管理规范完全一致：

```text
${AI_VOLUME_PATH}/                   # 外置盘 /Volumes/<SSD> (--external) 或 本机内置盘 ~/AI-Workspace (--internal)
└── AI/
    ├── Projects/                    # 【唯一主入口层】每个项目仅保留一个主克隆，长期停靠 main 分支且保持干净
    │   ├── Mac/                     # 系统维护、多智能体编排工具与全局治理主仓
    │   ├── NovaCam/                 # 虚拟示例项目 A：专业相机应用主仓
    │   ├── FlowWeave/               # 虚拟示例项目 B：跨平台工作流引擎主仓
    │   └── <任意项目>/               # 统一实行项目内“四分法”标准结构：
    │       ├── docs/                #   1. 长期文档：架构规范、设计说明、运行手册（纳入版本控制）
    │       ├── reports/             #   2. 审计回执：按“日期-主题”存放阶段报告与验收证据（纳入版本控制）
    │       ├── outputs/             #   3. 正式产物：可复用插件、工具脚本、发布包（纳入版本控制）
    │       └── work/                #   4. 临时试验场：日志、草稿、参考代码（写入 .git/info/exclude，严禁入库）
    │
    ├── Orca/Worktrees/              # 【Orca 隔离工作树池】每个并行任务独立一个目录，绝不污染主入口
    ├── Codex/Worktrees/             # 【Codex 隔离工作树池】例如 NovaCam-raw-color-fix
    ├── Claude/Worktrees/            # 【Claude 隔离工作树池】
    │
    ├── Archive/                     # 【可逆冷归档区】退役工作树必先打包至此，校验通过后才允许移出工作树
    │   └── 2026-11-06-raw-fix/      #   内含 repo.bundle + worktree-snapshot.tar.gz + SHA256SUMS.txt
    │
    ├── Data/                        # 【共享缓存与硬件协调锁】
    │   ├── BuildCache/              #   跨工作树共享编译包缓存（各工作树构建产物仍保持隔离）
    │   └── SimulatorCoordination/   #   iOS 模拟器租约锁目录，防止多智能体并发抢占同一台模拟器
    │
    ├── Backups/ResticRepo/          # 【本地自动备份仓库】未配置外部 NAS 时自动作为 Restic 增量备份库
    └── Migration/latest/            # 【跨机无损迁移快照】由 --pack-to-ssd 生成，含内置盘全量状态与恢复引导器
```

---

## 五、全自动每日/每小时/每周分级备份与硬件巡检体系

执行 `./deploy.sh --auto`（或 `just setup-backups`）时，[`scripts/setup_backups.py`](./scripts/setup_backups.py) 会自动在 macOS 钥匙串中生成高强度加密密钥（`icloud-package-key` 与 `restic-mac-agents`），将 [`scripts/codex-storage/`](./scripts/codex-storage) 安装至 `~/Library/Application Support/Codex Storage/agents/`，并向系统 `launchd` 注册 **7 项全自动后台守护任务**：

| 守护任务 (`LaunchAgent`) | 自动触发频率 | 备份目标位置 | 核心工程机制与防损坏设计 |
| :--- | :--- | :--- | :--- |
| **`icloud-package`** | **每天凌晨 02:30** | iCloud Drive (`AI备份/每晚小包`) | 对所有活跃 SQLite 数据库执行 **APFS 写时复制（`cp -c`）+ `PRAGMA wal_checkpoint(TRUNCATE)`**，打包核心配置、规则与脚本，使用钥匙串密码进行 **AES-256-CBC PBKDF2 加密**，当场执行**解密 + `tar -tzf` 完整性校验**，保留最近 30 份，并调用 **`brctl evict`** 卸载本地缓存实现**零本地磁盘占用** |
| **`backup-hot`** | **每小时整点 (`:00`)** | Restic 仓库（NAS SMB 或 `${AI_VOLUME_PATH}/AI/Backups/ResticRepo`） | 将内置盘所有智能体状态（`.codex`、`.claude`、`.orca`、`.gemini`、`.config`、`.ssh`、`.agents`）及无锁截断后的 SQLite 快照增量去重备份至 Restic 仓库 |
| **`backup-projects`** | **每天凌晨 03:00** | Restic 仓库（NAS SMB 或本地） | 每日全量扫描 `${AI_VOLUME_PATH}/AI`（含 `Projects`、`Worktrees`、`Archive`、`Data`），自动排除 `DerivedData`、`.build`、`node_modules` 等瞬态编译缓存 |
| **`backup-media`** | **每周日 04:00** | Restic 仓库（NAS SMB 或本地） | 支持对扩展媒体盘（`BACKUP_MEDIA_SOURCE_PATH`）执行带宽限流（`45 MB/s`）的周期增量备份 |
| **`maintenance`** | **每周日 05:30** | Restic 仓库 + 本地 SQLite | 自动执行 `restic forget --keep-hourly 24 --keep-daily 30 --keep-weekly 12 --keep-monthly 24 --prune` 清理过期快照，执行 `restic check`（每月首个周日额外抽检 5% 数据块），并检测本地 SQLite 空闲页比例 |
| **`restore-drill`** | **每周日 07:00** | 临时隔离沙盒 (`mktemp -d`) | **全自动灾难恢复演练**：每周自动从 Restic 最新 `hot` 快照抽取核心文件还原至临时目录并校验可读性，确保备份 100% 可恢复 |
| **`volume-guard`** | **每 60 秒** | 状态账本 (`volume-state.json`) | 外置盘模式下每分钟核验硬件 `VolumeUUID` 并监测 USB 总线 `sessionID` 掉盘重连事件；内置盘模式下每分钟校验本地工作区就绪状态 |

> [!TIP]
> - **零 NAS 亦可开箱即用**：若未配置 `BACKUP_NAS_SMB_URL`，系统会自动在 `${AI_VOLUME_PATH}/AI/Backups/ResticRepo` 初始化本地加密 Restic 仓库；若配置了局域网 NAS（如 SMB 挂载），则自动挂载 NAS 并写入远端仓库。
> - **随时查看备份健康面板**：运行 `./deploy.sh --backup-status` 或 `just backup-status`，即可实时生成并查看 `~/Library/Application Support/Codex Storage/backup-status.md` 状态报告。

---

## 六、工作区、聊天与分支命名规范

在同时管理上百个工作区与数十个会话标签时，统一命名规范配合本地 [`plugins/orca-naming-guard`](./plugins/orca-naming-guard) 插件可实现**零模型额度消耗的自动规整**：

| 对象类型 | 命名格式规则 | 规范示例（虚拟项目名称） |
| :--- | :--- | :--- |
| **主项目入口** | `正式产品名或领域名` | `NovaCam`、`流织`、`星际前线`、`ChromaStudio`、`Mac` |
| **专项项目入口** | `项目名｜用途` | `NovaCam｜色彩引擎开发` |
| **工作树与有主题聊天** | `项目名｜具体主题` | `Mac｜多智能体设置与命名整理`、`ChromaStudio｜色轮并发渲染修复` |
| **尚无任务的空闲工作区** | `项目名｜待命工作区 <稳定短编号>` | `NovaCam｜待命工作区 888c8500` |
| **导入资料工作区** | `项目名｜导入资料`（必要时追加编号） | `NovaCam｜导入资料 A` |
| **失效旧路径历史登记** | `项目名｜原用途（历史登记）` | `NovaCam｜单次拍摄实验（历史登记）` |
| **新 Git 任务分支** | `<前缀>/<2至4个英文单词连字符分隔>` | `codex/cloud-rule-save`、`claude/hdr-highlight-fix` |

---

## 七、全套技能矩阵与命令安全硬护栏

### 7.1 核心技能列表（全账号自动对齐）

| 技能类别 | 技能名称 | 核心工程用途 |
| :--- | :--- | :--- |
| **中枢编排与控制** | `orchestration` | 跨智能体派发子任务（例如在主会话中派发独立审查工作树并回收结论） |
| **中枢编排与控制** | `orca-cli` | 以命令行方式管理工作树、受管终端读写等待、仓库配置与内置浏览器 |
| **模拟器与界面交互** | `orca-emulator` | 控制已启动的 iOS 模拟器（点击、手势滑动、读取无障碍节点树与截图） |
| **模拟器与界面交互** | `orca-computer-use` | 当命令行无法完成任务时，通过无障碍树驱动本地桌面窗口完成界面校验 |
| **语法树检索与工具链** | [`ast-grep`](./skills/ast-grep/SKILL.md) | 语法树级精准搜索与批量重构，配合 `rg`、`fd`、`xcbeautify`、`repomix`、`just` |
| **存储瘦身与治理** | `agent-slim` / `mole` | 盘点智能体缓存、清理数据库空闲页，利用文件系统写时复制机制无损去重 |

### 7.2 命令执行前硬护栏（[`scripts/guard.py`](./scripts/guard.py)）

安装在 `~/.local/bin/agent-guard` 的硬护栏挂载于所有智能体的命令执行前钩子（`PreToolUse`），在危险命令运行前直接物理阻断：

| 规则编号 | 严禁执行的危险操作 | 硬护栏强制要求的安全替代路径 |
| :--- | :--- | :--- |
| **规则 01** | 外置工作盘未挂载时读写 `/Volumes/<盘名>/AI` | 立即中止命令，严禁在未挂载的空目录桩上创建假目录 |
| **规则 02** | 对项目、工作树或归档目录直接执行 `rm -rf` | 先执行 `just archive-worktree` 归档并移入同卷废纸篓，再执行工作树移除指令 |
| **规则 03** | 执行 `git reset --hard` 或 `git clean -fdx` | 先通过 `git bundle` 与压缩快照将未提交修改备份至 `AI/Archive/` |
| **规则 04** | 执行不带 `--dry-run` 的 `git worktree prune` | 必须先加 `--dry-run` 预览，确认外置盘在线后再处理失效工作树记录 |
| **规则 05** | 执行 `xcrun simctl create` 或 `simctl clone` | 严禁无限新建或克隆模拟器占满磁盘；必须复用指定主/副模拟器并检查租约锁 |
| **规则 06** | 清空系统或外置盘废纸篓 | 移入废纸篓的工作树与归档副本必须保留可恢复性 |
| **规则 07** | 推送受保护的历史敏感分支（`develop` / `feat/C-*`） | 命令钩子与 Git `pre-push` 双重拦截，防止含历史敏感配置的分支推送到远端 |

---

## 八、日常标准运维指令（`Justfile`）

```bash
just --list                                  # 查看所有 17 项标准运维、备份与迁移指令
just verify-volume                           # 1. 核验工作区存储卷挂载状态与硬件 UUID
just doctor                                  # 2. 运行全景只读健康诊断与多账号对齐检查
just slim-status                             # 3. 盘点所有智能体存储占用与数据库可回收空间
just sim-leases                              # 4. 查看当前 iOS 模拟器协调租约与运行中的测试进程
just test-guardrails                         # 5. 运行命令安全硬护栏单元测试
just handoff repo="." goal="阶段开发完成"      # 6. 生成高密度跨智能体交接回执包 (HANDOFF.json)
just archive-worktree <路径> <主题>           # 7. 安全打包归档工作树至 AI/Archive/ (含 SHA-256 校验单)
just pack-dry-run                            # 8. 预演跨 Mac 离线换机打包（不写文件，不含虚拟机）
just pack-to-ssd                             # 9. 一键将旧 Mac 所有智能体状态、SQLite 与仓库无损打包进外置盘
just restore-from-ssd                        # 10. 在新 Mac 上从外置盘一键无损还原并自动完成全量对齐部署
just migration-status                        # 11. 查看外置盘最新跨机迁移快照状态与完整性回执
just setup-backups                           # 12. 安装/更新 7 项全自动每日/每小时/每周分级备份与存储巡检守护进程
just backup-status                           # 13. 查看每日/每小时/每周自动备份与存储健康状态报告
just backup-now-icloud                       # 14. 立即触发一次 iCloud 每晚加密小包备份（含 CoW 快照与解密自检）
just backup-now-hot                          # 15. 立即触发一次每小时 Restic 热状态备份（内置盘 Agent 配置与状态库）
just use-external-storage                    # 16. 切换/部署为「外置固态硬盘模式」(/Volumes/<SSD>/AI)
just use-internal-storage                    # 17. 切换/部署为「本机内置硬盘模式」(~/AI-Workspace/AI)
```

---

## 九、核心痛点修复与收益对比

| 工程维度 | 优化前（原生分散使用状态） | 优化后（部署本套蓝图） | 实际工程收益 |
| :--- | :--- | :--- | :--- |
| **多账号限流切换** | 主账号限流切换到备用账号后，丢失全局规则、编译桥接与技能 | `sync_profiles.py` 自动保持所有账号配置 100% 一致 | **账号切换零断层、能力零降级** |
| **内置/外置双模存储** | 路径写死导致要么撑爆内置盘，要么不插移动硬盘就无法工作 | 支持 `--external`（外置盘）与 `--internal`（内置盘）一键切换 | **桌面工作站与外出轻薄本均可无缝适配** |
| **全自动分级备份** | 手工备份易遗漏，直接拷贝活跃 SQLite 易导致 `-wal` 锁损坏 | 7 项 `LaunchAgents` 自动执行 CoW 截断热备、iCloud 加密与每周演练 | **每小时/每日全自动无感备份 + 100% 可恢复验证** |
| **后台进程工具链** | 非交互式子进程找不到加速工具，退化为慢速全盘扫描 `work/` 目录 | `~/.zshenv` 注入原生工具链，`.git/info/exclude` 自动忽略 `/work/` | **代码检索提速 5~10 倍，杜绝临时文件刷爆上下文** |
| **编译与测试日志** | 数万行原始编译输出直接塞满上下文窗口，挤占核心代码记忆 | 统一经 `xcbeautify` 过滤降噪，配合单并发标志与模拟器租约锁 | **编译日志上下文占用减少约 90%，模拟器零死锁** |
| **高危命令防御** | 仅靠提示词叮嘱，智能体偶发执行 `git reset --hard` 或误删目录 | `agent-guard` 在命令执行前毫秒级物理拦截 | **100% 阻断未归档删改操作** |
| **海量工作区命名** | 满屏随机英文鱼名，靠人工肉眼分辨或额外消耗模型额度改名 | 本地命名守护插件自动分配待命编号并同步首个合规中文标题 | **零模型额度消耗，3 秒内自动规整工作区名称** |

---

## 十、仓库目录结构

```text
orca-multi-agent-blueprint/
├── assets/                          # 自动适配深浅色模式的单层圆角矢量图（中英分版，已修复垂直箭头指向）
├── bootstrap.sh                     # 一行命令全自动引导脚本（支持 --external / --internal）
├── deploy.sh                        # 核心部署、存储切换、自动备份安装与跨机迁移引擎
├── AGENT_INSTALL.md                 # 供 AI 智能体自动阅读并交互询问用户的安装协议
├── config.example.env               # 参数化配置模板（含双模存储、智能体池与自动备份参数）
├── Justfile                         # 17 个日常标准运维、自动备份、双模存储切换与换机配方
├── scripts/
│   ├── discover_and_adopt.py        # 智能环境探测与存量项目「四分法」无损纳管脚本
│   ├── setup_backups.py             # 自动生成钥匙串加密密钥并安装 7 项备份 LaunchAgents 守护进程
│   ├── codex-storage/               # 7 项全自动每日/每小时/每周分级备份与存储巡检脚本套件
│   │   ├── lib.sh                   #   双模存储路径解析、钥匙串读取、APFS CoW SQLite 热备与并发锁公共库
│   │   ├── icloud-package.sh        #   每晚 02:30 AES-256-CBC 加密 iCloud 小包备份 + 解密自检 + brctl evict
│   │   ├── backup-restic.sh         #   每小时 hot、每日 03:00 projects、每周日 04:00 media Restic 增量备份
│   │   ├── maintenance.sh           #   每周日 05:30 Restic 快照修剪、完整性校验与 SQLite 空闲页巡检
│   │   ├── restore-drill.sh         #   每周日 07:00 全自动灾难恢复演练（沙盒还原 + 校验）
│   │   ├── volume-guard.sh          #   每 60 秒硬件 VolumeUUID 校验与 USB 掉盘重连监测守护脚本
│   │   ├── volume-observer.py       #   存储卷挂载事件与状态变化观察器
│   │   └── status.sh                #   统一备份与存储健康状态报告生成器 (backup-status.md)
│   ├── migrate.py                   # 通用跨 Mac 外置盘无损打包与还原引擎（动态探测 + 零虚拟机冗余）
│   ├── install_cli_tools.sh         # 原生命令行加速工具链自动安装脚本（含 0.0 秒极速跳过通道）
│   ├── sync_profiles.py             # 多智能体与多账号技能、规则、钩子同步脚本（自动修复断链与跨机路径）
│   ├── guard.py                     # 命令执行前安全硬护栏脚本
│   ├── doctor.py                    # 全景只读健康审计工具
│   ├── handoff.py                   # 跨智能体紧凑交接包生成工具
│   ├── projectctl.py                # 受保护的工作树创建与存储卷核验工具
│   └── agent-slim.py                # 智能体存储占用盘点与无损瘦身工具
├── plugins/
│   └── orca-naming-guard/           # 零额度消耗的工作区中文命名守护插件（含完整测试）
├── skills/
│   └── ast-grep/SKILL.md            # 结构化语法树检索与原生工具链技能说明
├── templates/
│   ├── AGENTS.md.tmpl               # 全局规则参数化模板
│   └── MUSE_SUPERVISOR_PLAYBOOK.md  # 前端总指挥调度手册
└── tests/
    ├── test_guard.py                # 安全硬护栏单元测试套件
    └── test_migrate.py              # 跨机无损迁移、SQLite 热备与跨用户路径重定向单元测试
```
