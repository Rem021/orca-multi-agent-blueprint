# Instructions for AI agents

You are reading this because a user asked you to set up, pack or restore their Mac with
[`orca-multi-agent-blueprint`](https://github.com/Rem021/orca-multi-agent-blueprint).
Human-facing documentation: [README](./README.md) · [中文说明](./README.zh-CN.md).

**The short version:** ask two questions, show a dry run, get a yes, run one command, report the health check.

## Ground rules

| Do | Do not |
| :--- | :--- |
| Ask for the scenario and the storage location unless the user already stated both | Pick a scenario or a drive on the user's behalf |
| Run the preview first and show the user what will change | Start `--auto`, `--pack-to-ssd` or a restore without the user's go-ahead |
| Run a restore from **Terminal**, with Orca / Claude / Codex / Antigravity closed | Run a restore from inside one of the apps being restored, or add `--force` to get past the check |
| Report `summary.attention` from the health check, word for word | Report success while it still lists items |
| Treat `AI/Migration/latest` as read-only | Print or copy `keychain-secrets.json`, `.ssh` keys or token files |
| Ask before enabling a LaunchAgent that came from another Mac | Create a folder under `/Volumes/<name>` for a drive that is not mounted |

## Step 1 — Look, then ask

```bash
ls /Volumes
```

Then ask, in the user's language:

> **Which of these do you want?**
> 1. **Set up this Mac** — directory layout, CLI tools, command guardrail, scheduled backups, rules and skills
>    synced to every agent account. Works on a new Mac and on one that already has projects; nothing is moved
>    or deleted.
> 2. **Pack this (old) Mac onto an external SSD** — to move to a new Mac.
> 3. **Restore onto this (new) Mac from the SSD** — then set it up as in 1.
>
> **Where should the AI files live?**
> - **A. External SSD** (`/Volumes/<SSD>/AI`) — keeps the internal disk free; every write is preceded by a
>   mount and UUID check; unplug the drive to move to another Mac.
> - **B. Internal disk** (`~/AI-Workspace/AI`) — nothing to carry.

<details>
<summary>中文问法</summary>

> **你想做哪一件事？**
> 1. **部署这台 Mac**：目录结构、命令行工具、命令护栏、定时备份，并把规则与技能同步到每个智能体账号。
>    全新 Mac 或已有项目的 Mac 都可以，不移动、不删除任何文件。
> 2. **把这台（旧）Mac 打包到外置硬盘**：为换机做准备。
> 3. **从硬盘恢复到这台（新）Mac**：恢复后按第 1 项完成部署。
>
> **AI 文件放在哪里？**
> - **A. 外置硬盘**（`/Volumes/<硬盘>/AI`）：不占内置盘；每次写入前核对挂载与 UUID；拔下硬盘即可换机。
> - **B. 内置硬盘**（`~/AI-Workspace/AI`）：不需要携带任何设备。

</details>

## Step 2 — Run the matching commands

`BOOT` below stands for:

```bash
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s --
```

When a checkout already exists, use `./deploy.sh` with the same flags instead.

### 1 · Set up this Mac

```bash
BOOT --dry-run --external      # or --internal; changes nothing — show the output to the user
BOOT --auto --external         # after the user agrees
```

What it does is listed in the README under *What `--auto` does*. It needs Python 3.9+ and `git`; if a dialog
offers to install the Command Line Tools, let the user accept it, wait, and run the command again.

### 2 · Pack the old Mac

```bash
BOOT --pack-dry-run            # sizes and contents; changes nothing
BOOT --pack-to-ssd             # incremental, safe to repeat
```

- Add `--volume /Volumes/<SSD>` when more than one external drive is connected.
- The pack refuses to replace a snapshot that belongs to a different Mac. Tell the user; pass `--force` only
  when they confirm the other snapshot is no longer needed.
- Tell the user that the snapshot holds SSH keys and Keychain passwords in readable form, and recommend an
  encrypted drive.

### 3 · Restore onto the new Mac

The drive carries everything needed; no network or checkout is required.

```bash
zsh "/Volumes/<SSD>/AI/Migration/restore-on-new-mac.sh" --restore-dry-run   # preview
zsh "/Volumes/<SSD>/AI/Migration/restore-on-new-mac.sh"                     # restore, then deploy
```

- The user can also double-click `新Mac一键恢复.command` in the drive's root folder.
- `/Volumes/<SSD>/AI/Migration/AGENT_RESTORE.md` is generated from the actual snapshot and is more specific
  than this page — read it first when it exists.
- Add `--internal` to move the workspace to the new Mac's internal disk instead of keeping it on the drive.
- LaunchAgents from the old Mac that are not part of the blueprint are restored but **held**. List them with
  `./deploy.sh --agents` and enable only what the user asks for: `./deploy.sh --agents --enable-agents <label,…>`.
- NAS passwords and network logins (Tailscale, VPN) are not migrated; the user has to sign in again.

## Step 3 — Verify and report

```bash
./deploy.sh --doctor           # exit 0: healthy · 1: items need attention · 2: a check could not run
./deploy.sh --backup-status
```

Show the user `summary.attention` and `summary.advice`, and help with each entry. Typical first-run entries:
the NAS share is not mounted, a backup has not had its first successful run yet, or the drive is not
encrypted. The README's *Troubleshooting* table maps each message to its fix.
