# Instructions for AI agents

You are reading this because a user asked you to set up, pack or restore their Mac with
[`orca-multi-agent-blueprint`](https://github.com/Rem021/orca-multi-agent-blueprint).
Human documentation: [README](README.md) · [中文说明](README.zh-CN.md).

**Short version:** ask three questions, show a dry run, get a yes, run one command, report the health check.

## Ground rules

| Do | Do not |
| :--- | :--- |
| Ask for the scenario, the storage location and the supervisor unless the user already said | Choose a scenario, a drive or a supervisor for the user |
| Run the preview first and show what will change | Start `--auto`, `--pack-to-ssd` or a restore without the user's go-ahead |
| Pass `--supervisor <name>` or `--no-supervisor` yourself, so the installer does not stop to ask | Leave the installer waiting for an answer in a terminal you cannot see |
| Let the user run a restore in **Terminal**, with Orca, Claude, Codex and other agent apps and CLIs closed | Run a restore from inside an agent that is being restored (you included), or add `--force` to get past a check |
| Report `summary.attention` from the health check word for word | Report success while it still lists items |
| Treat `AI/Migration/latest` as read-only | Print or copy `keychain-secrets.json`, `.ssh` keys or token files |
| Ask before enabling a LaunchAgent that came from another Mac | Create a folder under `/Volumes/<name>` for a drive that is not mounted |

## Step 1 — Look, then ask

```bash
ls /Volumes
```

Then ask, in the user's language:

> **1. What do you want to do?**
> - **Set up this Mac**: directory layout, CLI tools, command guardrail, scheduled backups, rules and skills for
>   every agent. Works on a new Mac and on one that already has projects; nothing is moved or deleted.
> - **Pack this (old) Mac onto an external drive**, to move to a new Mac.
> - **Restore onto this (new) Mac from the drive**, then set it up.
>
> **2. Where should the AI files live?**
> - **External drive** (`/Volumes/<drive>/AI`): keeps the internal disk free; every write checks the mount and
>   UUID; unplug the drive to move to another Mac.
> - **Internal disk** (`~/AI-Workspace/AI`): nothing to carry.
>
> **3. Should a personal assistant act as supervisor?** (optional)
> - **No** (default): you give tasks to the coding agents directly.
> - **Yes**: name the assistant (for example `orca-mobile` or your own assistant). Workers will accept tasks from
>   it and report back to it; important decisions still come to you.

## Step 2 — Run the matching commands

`BOOT` below stands for:

```bash
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s --
```

When a checkout already exists, run `./deploy.sh` from it with the same flags instead.

### Set up this Mac

```bash
BOOT --dry-run --external --no-supervisor     # or --internal / --supervisor <name>; changes nothing
BOOT --auto --external --no-supervisor        # after the user agrees
```

It needs Python 3.9+ and `git`. If a dialog offers to install the Command Line Tools, let the user accept it,
wait, and run the command again. If Codex is installed, tell the user to approve the new hook once with
`/hooks` inside Codex.

### Pack the old Mac

```bash
BOOT --pack-dry-run            # sizes and contents; changes nothing
BOOT --pack-to-ssd             # incremental, safe to repeat
```

- Add `--volume /Volumes/<drive>` when more than one external drive is connected.
- The pack refuses to replace a snapshot from a different Mac. Tell the user; pass `--force` only when they
  confirm the other snapshot is no longer needed.
- Tell the user that the snapshot holds SSH keys and Keychain passwords, and recommend an encrypted drive.

### Restore onto the new Mac

The drive carries everything; no network or checkout is needed. You may run the preview; the restore itself
must run in Terminal with agent apps and CLIs closed, so hand the second command to the user.

```bash
zsh "/Volumes/<drive>/AI/Migration/restore-on-new-mac.sh" --restore-dry-run   # preview (you may run this)
zsh "/Volumes/<drive>/AI/Migration/restore-on-new-mac.sh"                     # the user runs this in Terminal
```

- The user can also double-click `Restore on New Mac.command` in the drive's root folder.
- `/Volumes/<drive>/AI/Migration/AGENT_RESTORE.md` is generated from the actual snapshot and is more specific
  than this page; read it first when it exists.
- Add `--internal` to put the workspace on the new Mac's internal disk.
- Third-party LaunchAgents from the old Mac are restored but **held**. List them with `./deploy.sh --agents`
  and enable only what the user asks for: `./deploy.sh --agents --enable-agents <label,…>`.
- NAS passwords and network logins (VPN, Tailscale) are not migrated; the user signs in again.

## Step 3 — Verify and report

```bash
./deploy.sh --doctor           # exit 0: healthy · 1: items need attention · 2: a check could not run
./deploy.sh --backup-status
```

Show the user `summary.attention` and `summary.advice`, and help with each entry. Typical first-run entries:
the NAS share is not mounted, a backup has not had its first successful run yet, or the drive is not
encrypted. The README's *Troubleshooting* table maps each message to its fix.
