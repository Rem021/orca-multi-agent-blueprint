# Orca 多智能体协同蓝图

[![English](https://img.shields.io/badge/docs-English-0969da?style=flat)](./README.md)
[![支持平台](https://img.shields.io/badge/平台-macOS_(Apple_Silicon_%26_Intel)-1a7f37?style=flat)](#环境要求)
[![智能体安装协议](https://img.shields.io/badge/给智能体-AGENT__INSTALL.md-8250df?style=flat)](./AGENT_INSTALL.md)
[![开源协议](https://img.shields.io/badge/协议-MIT-656d76?style=flat)](./LICENSE)

让多个编程智能体（Claude Code、Codex、Antigravity 等）在同一台 Mac 上并行工作的一套可复现方案：统一的目录标准、
相互隔离的 Git 工作树、命令执行前的安全护栏、定时加密备份，以及只带一块外置硬盘就能把整套环境搬到另一台 Mac 的迁移流程。

**[快速开始](#快速开始)** · **[架构](#架构)** · **[存储布局](#存储布局)** · **[自动备份](#自动备份)** ·
**[换机迁移](#换机迁移)** · **[命令护栏](#命令护栏)** · **[健康检查](#健康检查)** · **[常用命令](#常用命令)** ·
**[配置](#配置)** · **[常见问题](#常见问题)**

---

> **配套上下文预算审计与缓存 TTL 治理引擎**：已内置并同步开源至 **[`Rem021/agent-governor`](https://github.com/Rem021/agent-governor)**（`work/_scratch` 3 天回收 · `work/_tests` 7 天回收 · `work/retained` 永久豁免 · 每日 04:15 `agent-hygiene` 废纸篓暂存回执）。

## 你会得到什么

| | |
| :--- | :--- |
| **所有项目同一套目录** | `AI/Projects` 每个仓库只留一个干净的主克隆；并行任务放在 `AI/<智能体>/Worktrees`；退役的工作先打包进 `AI/Archive` 再移除。 |
| **外置硬盘或内置硬盘均可** | 同一套目录既能放在 `/Volumes/<硬盘>`，也能放在 `~/AI-Workspace`。使用外置盘时，每次写入前都会核对挂载状态与卷 UUID。 |
| **在命令执行前拦截的护栏** | `agent-guard` 挂在各智能体的命令前置钩子上，拒绝八类破坏性命令。 |
| **七项定时备份任务** | 每晚加密小包上传 iCloud 云盘，每小时/每天/每周 Restic 快照，每周清理、校验并做一次恢复演练。 |
| **整套环境迁移** | 旧电脑执行 `--pack-to-ssd`，新电脑双击一次即可。智能体状态、SQLite 数据库、账号、SSH/Git/Zsh 配置与核心应用一并带走。 |
| **会告诉你该做什么的体检** | `./deploy.sh --doctor` 末尾给出带编号的待处理事项，有事项时以非零状态退出。 |

## 快速开始

### 环境要求

- Apple Silicon 或 Intel 的 macOS。
- Python 3.9 及以上。`deploy.sh` 会自行查找（命令行工具、python.org、Homebrew、uv）；
  也可用 `ORCA_PYTHON=/path/to/python3` 指定。
- `git`（随 Xcode 命令行工具提供：`xcode-select --install`）。
- 可选：用于工作树编排的 Orca 桌面端；运行下文配方需要 `just` ≥ 1.27。蓝图依赖的命令行工具会自动装进 `~/.local/bin`。

### 按你的情况选择

| 你想要… | 执行 |
| :--- | :--- |
| **先看看会发生什么**（不做任何改动） | `./deploy.sh --dry-run` |
| **部署这台 Mac**（全新或已有大量项目均可） | `./deploy.sh --auto` |
| **把旧 Mac 打包到硬盘** | 先 `./deploy.sh --pack-dry-run`，再 `./deploy.sh --pack-to-ssd` |
| **在新 Mac 上恢复** | 双击硬盘根目录的 `新Mac一键恢复.command` |

还没有检出仓库？引导脚本会把仓库克隆到工作区，并把所有参数原样交给 `deploy.sh`：

```bash
# 只预览
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --dry-run

# 部署；已有 AI/ 目录的外置盘优先，否则使用 ~/AI-Workspace
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh

# 明确指定
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --auto --external
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --auto --internal
```

<details>
<summary><b>想让智能体代劳？</b></summary>

把下面这段话发给 Claude Code、Codex、Antigravity 或 Cursor：

```text
请阅读 https://github.com/Rem021/orca-multi-agent-blueprint/blob/main/AGENT_INSTALL.md 。
先问我要哪种场景（部署本机 / 打包旧 Mac / 在新 Mac 上恢复），以及 AI 文件放外置硬盘还是内置硬盘；
把预演结果给我看过之后再执行。
```

[`AGENT_INSTALL.md`](./AGENT_INSTALL.md) 写明了智能体该问什么、执行哪些命令、哪些事不能做
（例如：不能在正被恢复的桌面应用内部执行恢复）。

</details>

<details>
<summary><b><code>--auto</code> 的八个步骤</b></summary>

1. 在选定的卷上创建 `AI/` 目录结构。
2. 把 `~/.local/bin` 加入 `~/.zshenv`，为后台任务链接一个固定可用的 `python3`，开启适合大量工作树的 Git 加速选项。
3. 把 `rg`、`fd`、`ast-grep`、`just`、`xcbeautify`、`scc`、`repomix`、`restic` 安装到 `~/.local/bin`（已存在则跳过）。
4. 把护栏安装为 `~/.local/bin/agent-guard`，将其设置写入 `~/.orca/guard.env`，并安装 `ast-grep` 技能（已安装 Orca 时一并安装 Orca 自带技能）。
5. 纳管已有项目：补齐 `docs / reports / outputs / work` 目录，并通过 `.git/info/exclude` 忽略 `work/`。不移动、不删除任何文件。
6. 安装七项备份 LaunchAgent（`ENABLE_AUTOMATED_BACKUPS=false` 时跳过）。
7. 把技能、MCP 服务、全局规则与钩子同步到每个智能体账号。
8. 运行测试。

已有的 `~/.codex/AGENTS.md`、`~/.claude/CLAUDE.md`、`~/.gemini/GEMINI.md` 会保留；
`--render-rules` 会先备份再用模板重写。

</details>

## 架构

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/architecture-zh-dark.svg?v=3">
    <source media="(prefers-color-scheme: light)" srcset="./assets/architecture-zh-light.svg?v=3">
    <img alt="三层架构图" src="./assets/architecture-zh-light.svg?v=3" width="100%">
  </picture>
</p>

| 层级 | 职责 | 默认 | 可替换为 |
| :--- | :--- | :--- | :--- |
| **第一层** | 总指挥：接收需求、拆分任务、汇总结果 | Muse | Orca 手机端、Claude 桌面端、自定义脚本 |
| **第二层** | 调度与护栏：工作树生命周期、账号切换、命令检查 | Orca 桌面端与 CLI | `just` + `scripts/projectctl.py` |
| **第三层** | 执行者：各自在独立工作树内编码 | Claude、Codex、Antigravity、DeepSeek | OpenCode、Cursor、Aider |

每一层都在 [`config.env`](./config.example.env) 中选择，修改后执行 `./deploy.sh --apply` 生效。

### 任务生命周期

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/workflow-zh-dark.svg?v=3">
    <source media="(prefers-color-scheme: light)" srcset="./assets/workflow-zh-light.svg?v=3">
    <img alt="五步任务生命周期" src="./assets/workflow-zh-light.svg?v=3" width="100%">
  </picture>
</p>

1. **核验并拉分支**：检查存储卷，从干净的 `main` 创建工作树与任务分支。
2. **在护栏下开发**：每条终端命令先经过 `agent-guard`。
3. **交接**：`just handoff` 生成紧凑的 `HANDOFF.json`（基准提交、变更文件及 SHA-256、验证记录），下一个智能体不必读聊天记录。
4. **交叉审查**：换一个模型只读审查交接包与代码差异。
5. **归档**：`just archive-worktree` 把 `repo.bundle`、`.tar.gz` 快照与 `SHA256SUMS.txt` 写入 `AI/Archive/`，然后才移除工作树。

## 存储布局

| | 外置硬盘（`--external`） | 内置硬盘（`--internal`） |
| :--- | :--- | :--- |
| 工作区根目录 | `/Volumes/<硬盘>` | `~/AI-Workspace`（`INTERNAL_WORKSPACE_PATH`） |
| 身份校验 | 每次写入前核对挂载点**和**卷 UUID | 无需校验，记录为 `AI_VOLUME_UUID="LOCAL"` |
| 巡检（每 60 秒） | 挂载状态、UUID、USB 掉盘重连 | 工作区与备份目录是否存在 |
| 适合 | 把几百 GB 的仓库与编译缓存移出内置盘；拔下硬盘即可换机 | 不带硬盘出门的笔记本 |
| 切换 | `just use-external-storage` | `just use-internal-storage` |

`--storage auto`（默认）只会选用已经含有 `AI/` 目录的外置盘。安装器磁盘映像、时间机器磁盘、网络共享与只读卷永远不会被选中。

```text
<工作区根目录>/AI/
├── Projects/<项目>/           每个仓库一个主克隆，平时停在干净的 main
│   ├── docs/                  长期文档                                 （入库）
│   ├── reports/<日期-主题>/   阶段报告与回执                           （入库）
│   ├── outputs/<主题>/        正式交付物                               （入库）
│   └── work/                  临时草稿，经 .git/info/exclude 忽略      （不入库）
├── Orca/Worktrees/            每个并行任务一个目录
├── Codex/Worktrees/
├── Claude/Worktrees/
├── Archive/<日期-主题>/       repo.bundle + worktree-snapshot.tar.gz + SHA256SUMS.txt
├── Data/
│   ├── BuildCache/            跨工作树共享的依赖包缓存
│   └── SimulatorCoordination/ 租约文件，避免两个智能体同时操作同一台 iOS 模拟器
├── Backups/restic/            未配置 NAS 时的本地 Restic 仓库
└── Migration/latest/          --pack-to-ssd 生成的迁移快照
```

## 自动备份

`./deploy.sh --setup-backups`（`--auto` 已包含）会在登录钥匙串中生成两个随机密码
（`icloud-package-key`、`restic-mac-agents`），安装 [`scripts/codex-storage/`](./scripts/codex-storage)
中的脚本，并注册七个 LaunchAgent：

| 任务 | 时间 | 备份到 | 内容 |
| :--- | :--- | :--- | :--- |
| `volume-guard` | 每 60 秒 | 状态文件 | 工作区卷是否挂载、UUID 是否正确、USB 是否重连 |
| `backup-hot` | 每 60 分钟 | Restic | 内置盘上的智能体状态：`~/.codex`、`~/.claude`、`~/.gemini`、`~/.orca`、`~/.ssh` 等，以及一致性的 SQLite 快照 |
| `icloud-package` | 每天 02:30 | iCloud 云盘 | 配置、规则、脚本与数据库的加密小包 |
| `backup-projects` | 每天 03:00 | Restic | `<工作区>/AI`，不含编译产物（`DerivedData`、`.build`、`node_modules` 等） |
| `backup-media` | 周日 04:00 | Restic | `BACKUP_MEDIA_PATH`，上传限速约 45 MB/s |
| `maintenance` | 周日 05:30 | Restic | `forget --prune`（按主机与标签保留 24 个小时级、30 个日级、12 个周级、24 个月级快照）与 `check`；每月首次运行抽读 5% 数据 |
| `restore-drill` | 周日 07:00 | 临时目录 | 用 `--verify` 恢复最新 hot 快照的一部分，校验后删除 |

```bash
./deploy.sh --backup-status     # 或：just backup-status
```

<details>
<summary><b>这些任务如何保护数据</b></summary>

- **SQLite 数据库**通过 SQLite 在线备份接口复制，并用 `PRAGMA quick_check` 校验后才进入备份，
  智能体正在写入的数据库不会被复制到一半。
- **iCloud 小包**用钥匙串密码经 `openssl enc -aes-256-cbc -pbkdf2` 加密，发布前再解密并列出内容；
  校验失败的包直接丢弃。只保留**本机**最新的 `BACKUP_ICLOUD_KEEP_LATEST` 份（默认 1 份），其他 Mac 的包不会被动。
- **每台 Mac 有自己的名字**（`BACKUP_HOST_TAG`，取自本机的本地主机名），用于快照和小包文件名。
  经迁移从另一台 Mac 继承来的名字会被自动替换，两台 Mac 不会互相清理对方的历史。
- **任务不会重叠**：每个任务在整个运行期间持有一把锁。
- **跑不了会明说**：NAS 或硬盘不在时任务记为 `skipped`；连续 `BACKUP_STALE_ALERT_HOURS` 小时没有成功
  则变为 `stale`，弹出通知，并在 `--doctor` 中列出。

</details>

<details>
<summary><b>把 Restic 仓库放到 NAS</b></summary>

把 `BACKUP_RESTIC_REPO` 设为共享中的目录，并把 `BACKUP_NAS_MOUNT` / `BACKUP_NAS_SMB_MATCH` 设为它的挂载点与
`mount` 输出特征。先在访达里连接一次（⌘K），勾选「在我的钥匙串中记住此密码」。
`BACKUP_NAS_AUTOMOUNT="auto"` 时，NAS 可达就静默挂载，换到别的网络则安静跳过。
没有 NAS 时，仓库位于 `<工作区>/AI/Backups/restic`。

</details>

## 换机迁移

```text
 旧 Mac                              外置硬盘                             新 Mac
 ./deploy.sh --pack-to-ssd   ──►   AI/Migration/latest   ──►   新Mac一键恢复.command（双击）
```

**在旧 Mac 上**

```bash
./deploy.sh --pack-dry-run      # 会打包什么、有多大
./deploy.sh --pack-to-ssd       # 增量打包，可重复执行
./deploy.sh --migration-status  # 快照的来源、时间与完整性
```

**在新 Mac 上**：插上硬盘，双击根目录的 `新Mac一键恢复.command`
（或在终端执行 `zsh "/Volumes/<硬盘>/新Mac一键恢复.command"`），无需联网。
同一目录下的 `新电脑恢复指南.md` 是根据快照生成的分步指南；`AI/Migration/AGENT_RESTORE.md` 供代你执行恢复的智能体阅读。

恢复前请退出 Orca、Claude、Codex 与 Antigravity，并在「终端」里执行恢复，不要在这些应用内部执行。

| 会带走 | 不会带走 |
| :--- | :--- |
| 智能体状态与历史（`~/.codex`、`~/.claude`、`~/.gemini`、`~/.orca` 等） | 虚拟机镜像（`claudevm.bundle`）与 iOS 模拟器设备 |
| SQLite 数据库（一致性副本，已校验） | 编译缓存及一切可重新下载的内容 |
| 账号、SSH 密钥、Git 与 Zsh 配置 | 登录钥匙串本身，只会暂存到 `~/Migration-Imports/` |
| 备份任务与智能体需要的钥匙串密码 | `AI/` 之外的素材与工程文件（它们原样留在硬盘上） |
| LaunchAgent 及其用到的目录 | |
| 核心应用与一份 `Brewfile` | |

<details>
<summary><b>恢复过程如何保护新 Mac</b></summary>

- **预检**：智能体应用仍在运行、快照不完整、新 Mac 的 macOS 比旧 Mac 旧、磁盘空间不足时拒绝开始。`--force` 可强制继续。
- **不会悄悄覆盖**：被替换的文件保存在 `~/.migration-preexisting/<时间戳>`。
- **用户名不同**：plist、配置文件与软链接里的 `/Users/<旧>` 会改写为 `/Users/<新>`。
- **第三方 LaunchAgent 保持关闭**：从旧 Mac 带来的任务（交易机器人、同步工具等）用 `launchctl disable` 暂停，
  当前不会启动，下次登录也不会。用 `./deploy.sh --agents` 查看，
  用 `./deploy.sh --agents --enable-agents <标签,…>` 启用需要的，用 `./deploy.sh --hold-agents <标签,…>` 再次暂停。
- **备份任务**只有在钥匙串密码到位后才会加载，并且新 Mac 会使用自己的备份名。
- **一台 Mac 一份快照**：硬盘上已有别的 Mac 的快照时，拒绝从另一台 Mac 打包，除非加 `--force`。

</details>

> [!WARNING]
> 快照以可读形式包含 SSH 私钥和导出的钥匙串密码（`AI/Migration/latest`，文件权限 `0600`）。
> 硬盘没有加密时，任何拿到硬盘的人都能读取。请使用加密的 APFS 卷（访达 → 右键点按硬盘 →「加密」）；
> 如果硬盘会离开你的手，请在确认新 Mac 正常后删除 `AI/Migration/latest`。

## 命令护栏

[`scripts/guard.py`](./scripts/guard.py) 安装为 `~/.local/bin/agent-guard`，并注册为 Claude Code、Codex 与
Antigravity 的命令前置钩子。它会解析每条终端命令（引号、`sudo`/`env` 包装、`sh -c`、`git -C <目录>`、串联命令），并拒绝：

| # | 拒绝的操作 | 正确做法 |
| :-: | :--- | :--- |
| 1 | 硬盘未挂载或 UUID 不符时读写 `/Volumes/<硬盘>/AI` | 插上硬盘；绝不会在挂载点创建同名假目录 |
| 2 | 对项目、工作树或归档目录递归 `rm` | 先 `just archive-worktree`，再移入废纸篓 |
| 3 | `git reset --hard`、`git clean -f` | 先提交、暂存或打包 |
| 4 | 不带 `--dry-run` 的 `git worktree prune` | 在硬盘已挂载时先预览 |
| 5 | 清空废纸篓 | 归档副本必须保持可恢复 |
| 6 | `xcrun simctl create` / `clone` | 复用指定的两台模拟器及其租约 |
| 7 | 推送受限分支（`GUARD_RESTRICTED_BRANCHES`，默认 `develop`、`feat/C-*`） | 含密钥的历史只留在本地 |
| 8 | 强制推送或删除共享分支（`GUARD_PROTECTED_BRANCHES`，默认 `main`、`master`） | 推送新提交或发起合并请求 |

把分支列表设为 `none` 即可关闭对应规则。想让智能体之外的推送也受保护，可在重要仓库里安装 Git 钩子：

```bash
./deploy.sh --install-pre-push /path/to/repo     # 不会覆盖已有的 pre-push 钩子
```

> [!NOTE]
> 这是防止误操作的护栏，不是沙盒。藏在 `python -c`、脚本文件或远程 shell 里的命令不会被分析。
> 备份与归档步骤是第二道防线。

## 健康检查

```bash
./deploy.sh --doctor            # 完整 JSON 报告，重点看 summary
just doctor-summary             # 只看结论
```

```text
Health check: 2 item(s) need attention
  1. Backup job backup-hot has no successful run on record (last run skipped: NAS not mounted)
  2. 8 installed backup script(s) differ from this checkout; run ./deploy.sh --setup-backups.
Advice:
  - The volume holding the migration snapshot is not encrypted.
```

| 退出码 | 含义 |
| :-: | :--- |
| `0` | 没有需要处理的事项 |
| `1` | 至少有一项需要处理（见 `summary.attention`） |
| `2` | 有检查项无法执行 |

体检只读不写。检查内容包括：备份的实际结果（而不只是任务是否已安装）、NAS、本机的备份名、迁移快照的时间与来源、
落后于仓库的已安装脚本、各智能体的护栏钩子、后台任务使用的 Python、第三方 LaunchAgent，以及各智能体账号的技能与规则是否一致。

## 常用命令

`just --list` 按分组列出全部配方。

| 分组 | 配方 |
| :--- | :--- |
| 体检与测试 | `verify-volume` · `doctor` · `doctor-summary` · `test` · `test-guardrails` · `dry-run` |
| 备份 | `setup-backups` · `backup-status` · `backup-now-icloud` · `backup-now-hot` |
| 换机迁移 | `pack-dry-run` · `pack-to-ssd` · `restore-dry-run` · `restore-from-ssd` · `migration-status` · `refresh-guide` · `agents` · `hold-agents <标签>` · `enable-agents <标签>` |
| 存储模式 | `use-external-storage` · `use-internal-storage` |
| 日常协作 | `handoff` · `archive-worktree <路径> <主题>` · `install-pre-push <仓库>` · `slim-status` · `sim-leases` |

<details>
<summary><b><code>deploy.sh</code> 全部选项</b></summary>

| 选项 | 作用 |
| :--- | :--- |
| `--dry-run` | 预览全部动作；未指定模式时的默认行为 |
| `--auto` | 探测卷、模拟器、项目与智能体，写出 `config.env` 并部署 |
| `--apply` | 按现有 `config.env` 部署 |
| `--sync-profiles` | 重新把技能、MCP 服务、规则与钩子同步到所有智能体账号 |
| `--render-rules` | 用模板重写全局规则文件（先备份现有文件） |
| `--doctor` | 只读健康检查 |
| `--install-pre-push <仓库>` | 在指定仓库安装护栏的 `pre-push` 钩子 |
| `--storage <auto\|external\|internal>`、`--external`、`--internal` | 工作区放在哪里 |
| `--volume <路径>`、`--config <路径>` | 指定卷 / 配置文件 |
| `--setup-backups`、`--backup-status` | 安装或更新备份任务；查看其状态 |
| `--pack-dry-run`、`--pack-to-ssd` | 预览 / 写入迁移快照 |
| `--restore-dry-run`、`--restore-from-ssd` | 预览 / 恢复快照并随后部署 |
| `--migration-status` | 快照的来源、时间与完整性 |
| `--refresh-guide` | 基于现有快照重写恢复指南与启动器 |
| `--fast-config-only` | 打包时略过原始会话日志与应用 |
| `--force-apps` | 打包时即使应用未变化也重新复制 |
| `--force` | 打包：替换另一台 Mac 的快照；恢复：跳过预检拦截 |
| `--agents`、`--hold-agents <标签>`、`--enable-agents <标签>` | 查看、暂停或启用第三方 LaunchAgent |

</details>

## 配置

`./deploy.sh --auto` 会根据探测结果写出 `config.env`（已被 Git 忽略）；
[`config.example.env`](./config.example.env) 为每个键都写了说明。最常改的几个：

| 键 | 默认值 | 含义 |
| :--- | :--- | :--- |
| `STORAGE_MODE` | `auto` | `external`、`internal` 或 `auto` |
| `AI_VOLUME_PATH` / `AI_VOLUME_UUID` | 自动探测 | 工作区根目录及其应有的 UUID |
| `WORKER_AGENTS` | `claude,codex,antigravity,dsh` | 接收技能、规则与钩子的智能体 |
| `PRIMARY_SIMULATOR_UDID` / `SECONDARY_SIMULATOR_UDID` | 自动探测 | 智能体唯一可用的两台 iOS 模拟器 |
| `GUARD_PROTECTED_BRANCHES` | `main master` | 禁止强制推送或删除 |
| `GUARD_RESTRICTED_BRANCHES` | `develop feat/C-*` | 禁止推送；`none` 关闭 |
| `ENABLE_AUTOMATED_BACKUPS` | `true` | 是否安装七项备份任务 |
| `BACKUP_RESTIC_REPO` | `<工作区>/AI/Backups/restic` | Restic 仓库（本地目录或 NAS 路径） |
| `BACKUP_NAS_AUTOMOUNT` | `auto` | `auto`、`always` 或 `never` |
| `BACKUP_MEDIA_PATH` | `<工作区>/Work` | 每周素材备份的目录 |
| `BACKUP_HOST_TAG` | 本机名称 | 本机在快照与小包中的名字 |
| `BACKUP_ICLOUD_KEEP_LATEST` | `1` | 每台 Mac 保留的每晚小包数量 |
| `BACKUP_STALE_ALERT_HOURS` | `24` | 多少小时没有成功后报警 |
| `EXTRA_MIGRATION_PATHS` / `EXTRA_BUNDLED_APPS` / `EXTRA_KEYCHAIN_SERVICES` | 空 | 迁移快照的额外内容 |

## 常见问题

| 现象 | 原因与处理 |
| :--- | :--- |
| `--doctor` 提示某个备份 *no successful run on record* | 任务在跑，但每次都跳过。看括号里的原因：通常是 NAS 共享或硬盘没挂载。挂载后执行 `just backup-now-hot`。 |
| 换了网络后备份一直跳过 | 那个网络访问不到 NAS。数据不会丢，NAS 恢复可达后任务自动继续。用 `BACKUP_STALE_ALERT_HOURS=0` 可关闭提醒。 |
| 每小时弹出一次 NAS 登录窗口 | 这个共享没有保存密码。在访达里连接一次并勾选「记住此密码」，或设 `BACKUP_NAS_AUTOMOUNT="never"`。 |
| `--doctor` 提示备份名继承自另一台 Mac | 执行 `./deploy.sh --setup-backups`；只有想延续旧 Mac 的历史时才设 `BACKUP_HOST_TAG_KEEP="true"`。 |
| iCloud 云盘里每晚小包越积越多 | 以其他 Mac 命名的包不会被本机删除。那台 Mac 退役后手动删除即可。 |
| `--pack-to-ssd` 拒绝执行：快照属于另一台 Mac | 这是有意的保护。确认不再需要旧 Mac 的快照后加一次 `--force`。 |
| 从旧 Mac 带来的 LaunchAgent 在新 Mac 上不运行 | 它被有意暂停。先 `./deploy.sh --agents`，再 `--agents --enable-agents <标签>`。 |
| 弹窗要求安装命令行工具 | 还没有可用的 `python3`/`git`。安装（`xcode-select --install`）或设置 `ORCA_PYTHON`。 |
| 护栏拒绝推送 `develop` | 这是规则 7。在 `config.env` 里设 `GUARD_RESTRICTED_BRANCHES="none"`，再执行 `./deploy.sh --apply`。 |
| 凡是提到硬盘的命令都被护栏拦截 | 硬盘没插或换过（UUID 不符）。插上硬盘，或执行 `./deploy.sh --auto` 接管新硬盘。 |

## 仓库结构

```text
orca-multi-agent-blueprint/
├── bootstrap.sh               一行命令安装器：克隆后运行 deploy.sh
├── deploy.sh                  所有模式的统一入口
├── config.example.env         带说明的配置模板
├── Justfile                   日常配方
├── AGENT_INSTALL.md           写给代用户部署的智能体的说明
├── scripts/
│   ├── discover_and_adopt.py  探测卷、模拟器、智能体与项目，写出 config.env
│   ├── sync_profiles.py       技能、MCP 服务、规则、钩子 → 每个智能体账号
│   ├── guard.py               命令护栏（安装为 agent-guard）
│   ├── doctor.py              健康检查
│   ├── migrate.py             pack / restore / status / agents / guide
│   ├── setup_backups.py       钥匙串密码 + 七个 LaunchAgent
│   ├── codex-storage/         备份任务本体（lib.sh、backup-restic.sh、icloud-package.sh、
│   │                          maintenance.sh、restore-drill.sh、volume-guard.sh、status.sh、
│   │                          sqlite_snapshot.py、volume-observer.py）
│   ├── projectctl.py          带校验的工作树创建
│   ├── handoff.py             HANDOFF.json 生成器
│   ├── agent-slim.py          智能体缓存占用盘点
│   └── install_cli_tools.sh   把命令行工具装进 ~/.local/bin
├── plugins/orca-naming-guard/ 让工作区名称保持「项目名｜主题」格式的 Orca 插件
├── skills/ast-grep/           语法树级搜索与重构技能
├── templates/                 全局规则模板、总指挥手册
├── tests/                     护栏、迁移、备份脚本、体检四套测试
└── assets/                    架构图（深浅色、中英文）
```

### 命名约定

工作区、工作树与聊天标签统一命名为 `项目名｜具体主题`（全角竖线，两侧无空格）；空闲工作区为
`项目名｜待命工作区 <8 位编号>`；任务分支为 `<智能体>/<两到四个英文单词>`，例如 `codex/cloud-rule-save`。
[`orca-naming-guard`](./plugins/orca-naming-guard) 插件在本地完成这件事，不消耗模型额度。

## 测试

```bash
just test     # 护栏、迁移、备份脚本、体检（Python）+ 命名插件（Node）
```

测试只在临时目录中运行，不会碰你的智能体、钥匙串或备份。在 Python 3.9（macOS 自带版本）与当前 Python 3 上均通过。

## 开源协议

[MIT](./LICENSE)
