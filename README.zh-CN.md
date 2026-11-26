# Orca 多 Agent 蓝图

**在一台 Mac 上同时跑多个编码 Agent，互不干扰。**

每个任务一个 Git 工作树；每条 Agent 命令执行前先过护栏；所有 Agent 共用同一套规则与技能；定时加密备份；
换机时一块硬盘带走全部环境。支持 Claude Code、Codex、Gemini CLI 与 Antigravity，[Orca](https://orca.dev)
桌面端可选。

[English](README.md) · [快速开始](#快速开始) · [工作原理](#工作原理) · [命令护栏](#命令护栏) ·
[备份](#备份) · [换机迁移](#换机迁移) · [常见问题](#常见问题)

[![CI](https://github.com/Rem021/orca-multi-agent-blueprint/actions/workflows/ci.yml/badge.svg)](https://github.com/Rem021/orca-multi-agent-blueprint/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Rem021/orca-multi-agent-blueprint?style=flat-square)](https://github.com/Rem021/orca-multi-agent-blueprint/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

## 能得到什么

| | |
| :--- | :--- |
| **所有项目同一套目录** | `AI/Projects` 每个仓库一份主仓；并行任务放在 `AI/<Agent>/Worktrees`；退役的工作先打 Git bundle 和校验清单，再归档到 `AI/Archive`。 |
| **每条命令先过护栏** | `agent-guard` 挂在每个 Agent 的命令前置钩子上，在危险的 Git、删除、推送操作发生之前拦下来。 |
| **处处一致的规则与技能** | 一份模板生成 `~/.codex/AGENTS.md`、`~/.claude/CLAUDE.md`、`~/.gemini/GEMINI.md`（中文或英文）；技能、MCP、钩子同步到每个已安装的 Agent 和账号。 |
| **可选的总指挥** | 默认你直接指挥 Agent；也可以让一个个人助理接收需求、拆分并派发任务。安装时选一次即可。 |
| **交付不用盯** | `agent-ship` 负责变基、扫描密钥、跑检查、建 PR、等 CI，只合并检查过的那个提交；高风险改动会停下来等你拍板。 |
| **磁盘保持干净** | `agent-governor` 分配带有效期的临时和测试目录，到期后移进废纸篓，从不直接删除。`agent-test-device` 给每次测试单独建模拟器或虚拟机，用完即删。 |
| **备份与换机** | 每小时、每天、每周的 Restic 快照，每晚一个 iCloud 加密小包，每周一次恢复演练；`--pack-to-ssd` 打包，新 Mac 上双击恢复。 |
| **会告诉你怎么做的体检** | `./deploy.sh --doctor` 最后列出需要处理的事项。 |

## 快速开始

**环境要求：** macOS（Apple 芯片或 Intel）、Python 3.9+ 与 Git（`xcode-select --install`）。可选：Orca 桌面端，
以及用于下文快捷指令的 [`just`](https://github.com/casey/just)。其余命令行工具会自动装到 `~/.local/bin`，
版本和 SHA-256 都已固定。

```bash
git clone https://github.com/Rem021/orca-multi-agent-blueprint.git
cd orca-multi-agent-blueprint
./deploy.sh --dry-run     # 预览：列出每一步，不写任何文件
./deploy.sh --auto        # 探测本机、生成 config.env 并安装
```

在终端里运行 `--auto` 时只会问一个问题：要不要让个人助理担任[总指挥](#可选的总指挥)。直接回车表示不用。
也可以提前用 `--no-supervisor` 或 `--supervisor <名字>` 指定。

| 你想… | 运行 |
| :--- | :--- |
| 先看看会发生什么 | `./deploy.sh --dry-run` |
| 部署这台 Mac（新机或已有很多项目都行） | `./deploy.sh --auto`（加 `--external` / `--internal` 指定硬盘） |
| 之后更换总指挥 | `./deploy.sh --apply --supervisor <名字>` 或 `--no-supervisor` |
| 把旧 Mac 打包到外置硬盘 | `./deploy.sh --pack-dry-run`，再 `./deploy.sh --pack-to-ssd` |
| 恢复到新 Mac | 双击硬盘上的 `Restore on New Mac.command` |

<details>
<summary><b>还没有 checkout：一行命令引导安装</b></summary>

引导脚本会把仓库克隆进工作区，并把所有参数原样交给 `deploy.sh`。整个脚本包在一个函数里，下载中断时什么也不会执行。

```bash
# 只预览
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --dry-run

# 安装：外置盘上已有 AI/ 目录就用外置盘，否则用 ~/AI-Workspace
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh
```

</details>

<details>
<summary><b>让 Agent 帮你装</b></summary>

把下面这段发给 Claude Code、Codex、Gemini CLI 或 Antigravity：

```text
阅读 https://github.com/Rem021/orca-multi-agent-blueprint/blob/main/AGENT_INSTALL.md 。
问我要做哪件事、AI 文件放在哪里、要不要总指挥，先给我看预演结果，再执行。
```

[`AGENT_INSTALL.md`](AGENT_INSTALL.md) 写明了 Agent 该问什么、该运行什么、绝对不能做什么。

</details>

<details>
<summary><b><code>--auto</code> 具体做了什么</b></summary>

1. 在选定的硬盘上建立 `AI/` 目录结构，写入 `config.env`（已被 Git 忽略，权限 `0600`）。
2. 把 `~/.local/bin` 加进 `~/.zshenv`，确保后台任务有可用的 `python3`，开启适合大量工作树的 Git 设置
   （`feature.manyFiles`、`rerere`、工作树相对路径）。
3. 安装 `rg`、`fd`、`ast-grep`、`just`、`xcbeautify`、`restic`、`gh`（校验哈希，已存在则跳过）。
4. 把 `agent-guard`、`agent-governor`、`agent-ship`、`agent-test-device` 装进 `~/.local/bin`（只是很小的启动器，
   代码放在 `~/.local/lib/orca-blueprint` 并预编译），并安装 `orca-workspace` 技能。
5. 接管已有项目：补齐 `docs/ reports/ outputs/ work/`，通过 `.git/info/exclude` 忽略 `work/`。不移动、不删除任何文件。
6. 为还没有规则文件的 Agent 生成全局规则（已有文件保留；`--render-rules` 会先备份再替换）。
7. 安装备份 LaunchAgent（设 `ENABLE_AUTOMATED_BACKUPS="false"` 可跳过）。
8. 把技能、MCP、规则与钩子同步到每个已安装的 Agent 和账号，最后自检命令护栏。

</details>

## 工作原理

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/architecture-zh-dark.svg?v=4">
    <source media="(prefers-color-scheme: light)" srcset="./assets/architecture-zh-light.svg?v=4">
    <img alt="三层结构：可选总指挥、控制层、执行层" src="./assets/architecture-zh-light.svg?v=4" width="100%">
  </picture>
</p>

| 层 | 职责 | 默认 | 可替换为 |
| :--- | :--- | :--- | :--- |
| **第一层 · 总指挥（可选）** | 接收需求、拆分任务、汇总结果 | 不设，由你直接指挥 | 任意个人助理、Orca 手机端、自定义脚本 |
| **第二层 · 控制层** | 工作树生命周期、账号切换、规则与钩子同步、命令护栏 | Orca 桌面端 + CLI | `just` + `git worktree` |
| **第三层 · 执行层** | 每个 Agent 在自己的工作树里写代码 | Claude Code、Codex | Gemini CLI、Antigravity、Cursor、OpenCode、Aider |

### 可选的总指挥

默认不设总指挥：你在 Orca 或终端里直接和编码 Agent 对话。

如果你已经在用能调用 `orca` CLI 的个人助理，可以在 `--auto` 时填上它的名字，或之后运行
`./deploy.sh --apply --supervisor <名字>`。名字会存进 `config.env`（`SUPERVISOR_AGENT`）并写入全局规则：
每个执行 Agent 都知道任务可能来自它、完成后向它回报，而规则里列出的重大抉择（数据迁移、发布、凭据、
破坏性改动）仍然直接问**你本人**。`--no-supervisor` 可以取消。蓝图本身不安装、也不配置这个助理。

### 任务生命周期

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/workflow-zh-dark.svg?v=4">
    <source media="(prefers-color-scheme: light)" srcset="./assets/workflow-zh-light.svg?v=4">
    <img alt="开分支、开发、交付、互审、归档" src="./assets/workflow-zh-light.svg?v=4" width="100%">
  </picture>
</p>

1. **开分支**：从干净的 `main` 新建工作树和任务分支（`<前缀>/<主题>`）。
2. **开发**：每条 shell 命令、每次写文件都先经过 `agent-guard`。
3. **交付**：`agent-ship` 扫描密钥和新增的跳过测试、跑检查、建 PR、等 CI、合并。
4. **互审**：可选，换一个模型只读审查改动。
5. **归档**：`just archive-worktree` 先写 Git bundle、快照和 `SHA256SUMS.txt`，再移除任何东西。

## 目录结构

| | 外置硬盘（`--external`） | 内置硬盘（`--internal`） |
| :--- | :--- | :--- |
| 根目录 | `/Volumes/<硬盘>` | `~/AI-Workspace`（`INTERNAL_WORKSPACE_PATH`） |
| 身份核验 | 每次写入前核对挂载点**和**卷 UUID | 不需要（`AI_VOLUME_UUID="LOCAL"`） |
| 适合 | 大仓库和缓存不占内置盘；拔下硬盘即可换机 | 不带硬盘出门的笔记本 |

`--storage auto`（默认）只在外置盘上已有 `AI/` 目录时才使用它；磁盘映像、时间机器盘、网络共享和只读卷永远不会被选中。

```text
<根目录>/AI/
├── Projects/<项目>/           每个仓库一份主仓，停在干净的 main
│   ├── docs/  reports/<日期-主题>/  outputs/<主题>/       纳入版本管理
│   └── work/{_scratch,_tests,retained}/                  Git 排除，不建 Spotlight 索引
├── Orca/Worktrees/  Codex/Worktrees/  Claude/Worktrees/   每个并行任务一个目录
├── Archive/<日期-主题>/       repo.bundle + worktree-snapshot.tar.gz + SHA256SUMS.txt
├── Data/                      共享构建缓存
├── Backups/restic/            未配置 NAS 时的本地 Restic 仓库
└── Migration/latest/          --pack-to-ssd 写入的快照
```

## 命令护栏

[`scripts/guard.py`](scripts/guard.py) 安装为 `~/.local/bin/agent-guard`，注册为 Claude Code（`PreToolUse`）、
Codex（`PreToolUse`）、Gemini CLI（`BeforeTool`）和 Antigravity 的命令前置钩子。它会解析每条命令（引号、
`sudo`/`env` 包装、`sh -c`、here-string、`git -C`、`git -c`、串联与管道），并拒绝：

| 拦截 | 应该这样做 |
| :--- | :--- |
| 外置盘未挂载或 UUID 不符时写入 `/Volumes/<硬盘>/AI` | 插上硬盘；挂载点下不会被建出同名空目录 |
| 递归删除项目、工作树、归档、`.git`、工作区或主目录（不区分大小写，解析软链接） | 先 `just archive-worktree`，再移进废纸篓 |
| `git reset --hard`、`git clean -f`、`git checkout -f` / `git switch --discard-changes`、`git restore .`、`git stash clear`，包括缩写和 `-c` 覆盖 | 先提交、stash 或打 bundle |
| 不带 `--dry-run` 的 `git worktree prune` | 先预览 |
| 清空废纸篓 | 归档副本要保持可恢复 |
| 强推或删除受保护分支（`GUARD_PROTECTED_BRANCHES`，默认 `main master`） | 推新提交或开 PR |
| 推送受限分支（`GUARD_RESTRICTED_BRANCHES`，默认无） | 把这段历史留在本地 |
| 读取私钥和凭据文件（`~/.ssh/id_*`、`.p12` 等） | 先问用户 |
| 在项目或工作树根目录建 `tmp/`、`scratch/`、`.worktrees/` | `agent-governor scratch --topic <主题>` |
| 退出宿主 Orca 应用、`launchctl submit` | — |

> [!NOTE]
> 这是防误操作的护栏，不是沙箱。脚本文件、`python -c`、远程 shell、下载执行的安装脚本（`curl … | sh`）里的命令
> 不做分析，`R=rm; $R …` 这类变量间接调用也不做分析。备份和归档是第二道防线。

**Codex** 只会运行你批准过的钩子：安装后打开一次 Codex，运行 `/hooks` 批准。
如果也想拦住 Agent 之外的推送，可以给重要仓库装 Git 钩子：`./deploy.sh --install-pre-push /path/to/repo`
（不会覆盖已有的 `pre-push`）。

## 用 `agent-ship` 交付

在任务工作树里提交后运行。它会拒绝有未提交改动的目录、默认分支，以及 `~/.orca/ship.json`（`_deny_repos`）里列出的仓库，然后：

1. 以纯文本方式扫描分支上的每个提交（不受 `.gitattributes` 影响）和提交说明里的密钥，并检查新增的跳过测试；
2. 变基到最新的 `main`，跑 `~/.orca/ship.json` 里的检查加上 `--check` 指定的检查；如果检查改动了内容，再扫描一遍；
3. 推送、建 PR、等 CI，用 `--match-head-commit` 合并，确保落地的就是检查过的那个提交。

涉及签名、计费、数据迁移或 CI 工作流的改动会停在 PR 等你决定（`--no-merge` 就是主动这样做）。
退出码：`0` 已合并 · `2` 拒绝 · `3` 冲突 · `4` 检查失败 · `5` 发现密钥 · `6` CI 失败 · `7` 合并失败 · `10` 等你拍板。

## 临时目录、清理与测试设备

```bash
agent-governor scratch --topic login-fix      # work/_scratch/…（3 天回收）
agent-governor test-dir --topic login-fix     # work/_tests/…（7 天回收）
agent-governor gc                             # 预演
agent-governor gc --apply                     # 把到期内容移进废纸篓，并写恢复清单
agent-governor audit                          # 规则文件预算、技能、项目目录规范（只读）
```

`gc` 会跳过 Git 跟踪的文件、被进程打开的文件、仍在租期内的任务目录、带 `.keep`/`.pinned` 标记的内容和
`work/retained/`。`agent-hygiene` 任务每隔几小时自动运行它。

```bash
agent-test-device simulator --type com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro -- ./run-ui-tests
agent-test-device qemu qemu-system-aarch64 -M virt -display none -drive file=base.qcow2,format=qcow2 -- ./run-vm-tests
```

每次运行建一个模拟器或虚拟机（`AGENT_TEST_UDID`、`AGENT_TEST_QMP`），执行命令后无论成功、失败还是 Ctrl-C
都会删除这个实例。QEMU 以 `-snapshot` 运行，基础镜像不会被改动。

## 备份

`./deploy.sh --setup-backups`（`--auto` 会自动执行）在登录钥匙串里生成两把随机密钥，安装
[`scripts/codex-storage/`](scripts/codex-storage) 里的脚本，并注册以下 LaunchAgent：

| 任务 | 时间 | 目标 | 内容 |
| :--- | :--- | :--- | :--- |
| `volume-guard` | 每 60 秒（外置）/ 10 分钟（内置） | 状态文件 | 硬盘已挂载且 UUID 正确 |
| `backup-hot` | 每小时 | Restic | 内置盘上的 Agent 状态（`~/.codex`、`~/.claude`、`~/.gemini`、`~/.orca`、`~/.ssh` 等）与一致的 SQLite 副本 |
| `icloud-package` | 每天 02:30 | iCloud 云盘 | 配置、规则、脚本和数据库的小型加密包（Claude Code 原始会话记录只进 Restic，除非设 `BACKUP_ICLOUD_INCLUDE_TRANSCRIPTS="true"`） |
| `backup-projects` | 每天 03:00 | Restic | `<根目录>/AI`，不含构建产物 |
| `backup-media` | 周日 04:00 | Restic | `BACKUP_MEDIA_PATH`，限速（为空则不备份） |
| `maintenance` | 周日 05:30 | Restic | `forget --prune`（24 小时、30 天、12 周、24 月）与 `check` |
| `restore-drill` | 周日 07:00 | 临时目录 | 从最新快照恢复一部分并校验 |
| `agent-hygiene` | 每小时 | — | iCloud 包的保留策略；每 4 小时运行一次 `agent-governor gc` |

```bash
./deploy.sh --backup-status     # 或：just backup-status
```

<details>
<summary><b>这些任务如何保护数据</b></summary>

- **SQLite** 通过 SQLite 在线备份 API 复制，再用 `PRAGMA quick_check` 校验，Agent 正在写入的数据库不会被拷到一半。
- **iCloud 包** 以流式方式经 `openssl enc -aes-256-cbc -pbkdf2 -iter 600000` 加密，并用 HMAC-SHA256 认证
  （先加密后 MAC），密钥来自钥匙串。发布前会校验并试解密；磁盘上不会出现未加密的归档。手动打开方式：

  ```bash
  security find-generic-password -s icloud-package-key -w \
    | python3 "$HOME/Library/Application Support/Codex Storage/agents/icloud_crypto.py" decrypt PKG \
    | tar -xzf - -C /path/to/restore
  ```

  `decrypt` 会先校验 HMAC，被改动过的包会被拒绝；3.0 之前生成的旧包仍可打开，但会提示没有完整性校验。
- **保留策略** 每台 Mac 保留最新的若干个已确认上传的包（`BACKUP_ICLOUD_KEEP_LATEST`），绝不删除其他 Mac 的最新包；
  文件名日期在未来的包不会挤掉当前的包。
- **每台 Mac 有自己的备份名**（`BACKUP_HOST_TAG`），迁移带来的旧名字会被替换。
- **任务互不重叠**，跑不了的任务会说明原因：记录为 `skipped`，超过 `BACKUP_STALE_ALERT_HOURS` 变为 `stale`，
  弹出通知，`--doctor` 也会报告。

</details>

<details>
<summary><b>用 NAS 存放 Restic 仓库</b></summary>

把 `BACKUP_RESTIC_REPO` 设成共享盘上的目录，再设置 `BACKUP_NAS_MOUNT` 和 `BACKUP_NAS_SMB_MATCH`。先在访达里
连接一次（⌘K）并勾选*在我的钥匙串中记住此密码*。`BACKUP_NAS_AUTOMOUNT="auto"` 时，NAS 可达就静默挂载，
不在家时安静跳过。没有 NAS 时仓库位于 `<根目录>/AI/Backups/restic`。

</details>

## 换机迁移

```text
 旧 Mac                            外置硬盘                           新 Mac
 ./deploy.sh --pack-to-ssd  ──►  AI/Migration/latest  ──►  Restore on New Mac.command（双击）
```

```bash
./deploy.sh --pack-dry-run       # 会打包哪些内容、有多大
./deploy.sh --pack-to-ssd        # 增量打包，可重复运行
./deploy.sh --migration-status   # 快照的来源、时间与完整性
```

在新 Mac 上插上硬盘，双击根目录的 `Restore on New Mac.command`（或在终端运行
`zsh "/Volumes/<硬盘>/AI/Migration/restore-on-new-mac.sh"`），全程不需要联网。旁边的 `RESTORE-GUIDE.md`
逐步说明，`AI/Migration/AGENT_RESTORE.md` 是写给代为恢复的 Agent 看的。恢复前请退出 Orca、Claude、Codex
等 Agent 应用和命令行，并在“终端”里运行。

| 会迁移 | 不迁移 |
| :--- | :--- |
| Agent 状态与历史（`~/.codex`、`~/.claude`、`~/.gemini`、`~/.orca` 等） | 虚拟机映像和 iOS 模拟器设备 |
| SQLite 数据库（一致且校验过的副本） | 构建缓存及可重新下载的内容 |
| SSH 密钥、Git 与 Zsh 配置 | 登录钥匙串本身 |
| 备份和 Agent 需要的钥匙串条目 | 硬盘上 `AI/` 以外的文件（原样留在硬盘上） |
| LaunchAgent 及其使用的目录；核心 Agent 应用 | |

恢复时，所有被替换的文件都会保留在 `~/.migration-preexisting/<时间戳>`；`/Users/<旧用户名>` 会改写成
`/Users/<新用户名>`；不会覆盖钥匙串里已有的不同值；新 Mac 会得到自己的备份名；旧 Mac 的第三方 LaunchAgent
会先**暂停**，由你决定是否启用（`./deploy.sh --agents`、`--agents --enable-agents <标签,…>`）。额外的路径、
应用和钥匙串条目可以通过 `EXTRA_MIGRATION_PATHS`、`EXTRA_RESTIC_PATHS`、`EXTRA_BUNDLED_APPS`、
`EXTRA_KEYCHAIN_SERVICES` 添加。

> [!WARNING]
> 快照里包含 SSH 私钥和导出的钥匙串密码（文件权限 `0600`）。硬盘没加密时，拿到硬盘的人都能读取。请使用加密的
> APFS 卷，只从可信的硬盘恢复；新 Mac 确认正常后，如果硬盘要交给别人，请删除 `AI/Migration/latest`。

## 体检

```bash
./deploy.sh --doctor        # 完整 JSON 报告
just doctor-summary         # 只看结论
```

```text
Health check: 2 item(s) need attention
  1. Backup job backup-hot has no successful run on record (last run skipped: NAS not mounted)
  2. 8 installed backup script(s) differ from this checkout; run ./deploy.sh --setup-backups.
```

退出码 `0` 无需处理 · `1` 有待处理事项（见 `summary.attention`）· `2` 有检查无法执行。体检是只读的，
会检查备份结果、NAS、迁移快照、落后于 checkout 的已安装工具、每个 Agent 的护栏钩子、后台 Python、
第三方 LaunchAgent，以及各 Agent 和账号的技能、规则是否一致。

## 配置

`./deploy.sh --auto` 会生成 `config.env`。[`config.example.env`](config.example.env) 列出了每个配置项及其默认值：
把需要的那一行复制到 `config.env`、修改后运行 `./deploy.sh --apply`。常用项：

| 配置项 | 默认值 | 含义 |
| :--- | :--- | :--- |
| `STORAGE_MODE` | `auto` | `external`、`internal` 或 `auto` |
| `SUPERVISOR_AGENT` | `none` | 可选总指挥（个人助理）的名字 |
| `RULES_LANGUAGE` | `en`（中文系统为 `zh-CN`） | 生成的全局规则使用的语言 |
| `DEFAULT_BRANCH_PREFIX` | `codex` | 任务分支前缀 |
| `GUARD_PROTECTED_BRANCHES` | `main master` | 禁止强推或删除 |
| `GUARD_RESTRICTED_BRANCHES` | `none` | 禁止推送 |
| `PRIMARY_SIMULATOR_UDID` / `SECONDARY_SIMULATOR_UDID` | 自动探测 | 长期共享的模拟器，不会被清理 |
| `ENABLE_AUTOMATED_BACKUPS` | `true` | 是否安装备份任务 |
| `BACKUP_RESTIC_REPO` | `<根目录>/AI/Backups/restic` | 本地目录或 NAS 路径 |
| `BACKUP_ICLOUD_DIR` | `iCloud 云盘/Agent Backups/Nightly` | 每晚加密包的存放位置 |
| `BACKUP_MEDIA_PATH` | 空 | 每周限速备份的大文件夹 |
| `BACKUP_ICLOUD_KEEP_LATEST` | `1` | 每台 Mac 保留的包数量 |

## 常用指令

`just --list` 列出全部指令，最常用的有：

| 分组 | 指令 |
| :--- | :--- |
| 体检 | `doctor` · `doctor-summary` · `test` · `dry-run` · `verify-volume` |
| 备份 | `setup-backups` · `backup-status` · `backup-now-hot` · `backup-now-icloud` · `prune-icloud-dry-run` |
| 迁移 | `pack-dry-run` · `pack-to-ssd` · `restore-dry-run` · `restore-from-ssd` · `migration-status` · `agents` |
| 日常 | `archive-worktree <路径> <主题>` · `install-pre-push <仓库>` · `governor-audit` · `govern-gc` |

<details>
<summary><b><code>deploy.sh</code> 全部选项</b></summary>

| 选项 | 作用 |
| :--- | :--- |
| `--dry-run` | 预览全部步骤（未指定模式时的默认值） |
| `--auto` | 探测本机、生成 `config.env` 并部署 |
| `--apply` | 按现有 `config.env` 部署 |
| `--supervisor <名字>`、`--no-supervisor` | 设置或取消可选总指挥（会重新生成规则） |
| `--sync-profiles` | 重新同步技能、MCP、规则与钩子到每个 Agent 和账号 |
| `--render-rules` | 用模板重写全局规则文件（先备份现有文件） |
| `--doctor` | 只读体检 |
| `--install-pre-push <仓库>` | 给一个仓库安装护栏的 `pre-push` 钩子 |
| `--storage <auto\|external\|internal>`、`--external`、`--internal` | 工作区放在哪里 |
| `--volume <路径>`、`--config <路径>` | 指定硬盘 / 配置文件 |
| `--setup-backups`、`--backup-status` | 安装或更新备份任务；查看状态 |
| `--pack-dry-run`、`--pack-to-ssd` | 预览 / 写入迁移快照 |
| `--restore-dry-run`、`--restore-from-ssd` | 预览 / 恢复快照，然后部署 |
| `--migration-status`、`--refresh-guide` | 快照状态；重写恢复指南与启动器 |
| `--fast-config-only`、`--force-apps`、`--force` | 打包与恢复的修饰选项（见 `--help`） |
| `--agents`、`--hold-agents <标签>`、`--enable-agents <标签>` | 列出、暂停或启用第三方 LaunchAgent |

</details>

## 常见问题

| 现象 | 原因与解决 |
| :--- | :--- |
| `--doctor` 说某个备份*没有成功记录* | 任务一直在跳过，通常是 NAS 或硬盘没挂载。挂上后运行 `just backup-now-hot`。 |
| 每小时弹一次 NAS 登录框 | 共享盘没有保存密码。在访达里连接一次并勾选*记住此密码*，或设 `BACKUP_NAS_AUTOMOUNT="never"`。 |
| `--doctor` 说备份名继承自另一台 Mac | 运行 `./deploy.sh --setup-backups`。 |
| iCloud 云盘被备份包占满 | 先 `just prune-icloud-dry-run`，再 `just prune-icloud`。 |
| Codex 没有触发护栏 | 在 Codex 里运行 `/hooks` 批准一次钩子。 |
| `--pack-to-ssd` 拒绝：快照属于另一台 Mac | 这是有意为之。确认旧快照不再需要后加 `--force`。 |
| 旧 Mac 的某个 LaunchAgent 在新 Mac 上不运行 | 它被有意暂停：`./deploy.sh --agents`，再 `--agents --enable-agents <标签>`。 |
| 弹窗提示安装命令行工具 | 还没有可用的 `python3`/`git`。安装它们，或设置 `ORCA_PYTHON=/path/to/python3`。 |
| 护栏拦下所有涉及硬盘的命令 | 硬盘没插或换了一块。插上它，或运行 `./deploy.sh --auto` 接管新硬盘。 |

## 仓库结构

```text
bootstrap.sh            一行命令安装（克隆后运行 deploy.sh）
deploy.sh               所有模式的入口
config.example.env      全部配置项及默认值
Justfile                日常快捷指令
AGENT_INSTALL.md        写给代为安装的 Agent 的说明
templates/              全局规则模板（英文、中文）
skills/orca-workspace/  为每个 Agent 安装的工作区技能
plugins/workspace-ops/  Workspace Ops Radar — Orca 实时运维插件（清理倒计时、空间大户、Bots 与健康体检）
scripts/
  guard.py              agent-guard        命令护栏
  ship.py               agent-ship         变基、扫描、PR、CI、合并
  agent_governor.py     agent-governor     临时与测试目录、到期清理、审计
  test_device.py        agent-test-device  一次性模拟器与虚拟机
  discover_and_adopt.py 探测本机、生成 config.env、接管项目、生成规则
  sync_profiles.py      技能、MCP、规则与钩子 → 每个 Agent 和账号
  doctor.py             体检
  setup_backups.py      钥匙串密钥与 LaunchAgent
  migrate.py            打包 / 恢复 / 状态 / 第三方 LaunchAgent
  install_cli_tools.sh  固定版本的命令行工具 → ~/.local/bin
  codex-storage/        备份任务脚本
tests/                  unittest 测试（在临时目录中运行）
```

## 测试

```bash
just test      # 或：python3 -B -m unittest discover -s tests
```

测试不会触碰你的 Agent、钥匙串或备份；CI 在 Python 3.9（macOS 系统自带）和最新版 Python 上运行。

## 相关项目

- [Workspace Naming Guard](https://github.com/Rem021/workspace-naming-guard)：Orca 插件，自动把工作区命名为
  `项目｜主题`，不消耗模型额度。

## 参与贡献与安全

见 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

[MIT](LICENSE)
