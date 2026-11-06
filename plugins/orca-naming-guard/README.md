# 工作区中文命名插件 (`orca-naming-guard`)

版本 `0.1.0` — 零 Token 消耗的本地 Orca `trusted-worker` 插件。

## 核心功能

新建本地文件夹工作区后约 3 秒自动校正显示名，并每 60 秒例行补查一次：
- **待命占位**：尚无主题时自动使用 `项目名｜待命工作区 <稳定短编号>`（基于工作区 ID 的 8 位 SHA-256 摘要）；
- **自动采纳**：首个合规的原生聊天标题出现后自动采纳为工作区标题；
- **保护 Git 分支首次重命名**：Git 工作树须先等 Orca 完成首次英文分支命名（`pendingFirstAgentMessageRename === false`），避免写显示名时提前清掉原生分支改名标记；
- **尊重人工覆盖**：用户在插件改名后一旦手动修改工作区名称，插件立即停止接管该工作区，绝不抢回；
- **零模型开销**：纯本地状态机对比，无需发起模型请求，不消耗任何 LLM 额度。

仅处理 `${AI_VOLUME_PATH}/AI/` 下、`host=local`、非主工作树、非归档工作区。未知项目、版本或接口失败时自动跳过。

## 权限与安全边界

这是 `trusted-worker` 代码插件：
- 声明 `workspace:read` 用于监听创建事件，`storage:own` 用于保存自身状态与最多 1000 条名称变更记录；
- 通过 Node 读取 Orca 本地运行时连接信息，经 Unix socket 只读查询 `session.tabs.listAll`，并调用固定路径的 `orca worktree list`、`repo list`、`worktree set --display-name`（使用参数数组，不拼接 shell）；
- 不访问云服务、不调用模型、不启动/停止 Agent、不发送消息、不修改 Git 分支或目录、不操作账号密钥/钥匙串。

## 验证与启用

```sh
node --test plugins/orca-naming-guard/*.test.mjs
node plugins/orca-naming-guard/preview.mjs
orca plugin validate ./plugins/orca-naming-guard --json
orca plugin inspect ./plugins/orca-naming-guard --json
ORCA_NAMING_READ_ONLY=1 orca plugin check ./plugins/orca-naming-guard --json
```

加载并启用：
```sh
orca plugin dev load ./plugins/orca-naming-guard --json
# 读取返回的 approvalFingerprint 后执行：
orca plugin approve local.workspace-naming-guard --fingerprint <FINGERPRINT> --json
orca plugin enable local.workspace-naming-guard --json
```

## 停用与恢复

- 停用：`orca plugin disable local.workspace-naming-guard --json`
- 卸载开发加载：`orca plugin dev unload local.workspace-naming-guard --json`
- 恢复单个名称：`orca worktree set --worktree 'id:<ID>' --display-name '<原名>' --json`
