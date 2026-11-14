# Orca Multi-Agent Blueprint

[![简体中文](https://img.shields.io/badge/文档-简体中文-0969da?style=flat)](./README.zh-CN.md)
[![Platform](https://img.shields.io/badge/platform-macOS_(Apple_Silicon_%26_Intel)-1a7f37?style=flat)](#requirements)
[![Agent protocol](https://img.shields.io/badge/for_agents-AGENT__INSTALL.md-8250df?style=flat)](./AGENT_INSTALL.md)
[![License](https://img.shields.io/badge/license-MIT-656d76?style=flat)](./LICENSE)

A reproducible setup for running several coding agents (Claude Code, Codex, Antigravity, …) side by side
on one Mac: one directory standard, isolated Git worktrees, a command guardrail, scheduled encrypted
backups, and a way to move the whole environment to another Mac on a single external SSD.

**[Quick start](#quick-start)** · **[Architecture](#architecture)** · **[Storage](#storage-layout)** ·
**[Backups](#automated-backups)** · **[Moving to a new Mac](#moving-to-a-new-mac)** ·
**[Guardrail](#command-guardrail)** · **[Health check](#health-check)** · **[Commands](#everyday-commands)** ·
**[Configuration](#configuration)** · **[Troubleshooting](#troubleshooting)**

---

## What you get

| | |
| :--- | :--- |
| **One layout for every project** | `AI/Projects` holds one clean clone per repo; parallel tasks live in `AI/<Agent>/Worktrees`; retired work is bundled into `AI/Archive` before it is removed. |
| **External SSD or internal disk** | The same layout works under `/Volumes/<SSD>` or `~/AI-Workspace`. On an external drive every write is preceded by a mount and volume-UUID check. |
| **A guardrail that runs before the command** | `agent-guard` is wired into each agent's pre-command hook and refuses nine classes of destructive or root-cluttering commands. |
| **Context & cache governance ([`agent-governor`](https://github.com/Rem021/agent-governor))** | Enforces lean `AGENTS.md` budgets, a 3-tier `work/{_scratch,_tests,retained}` lifecycle, and 5-gate reversible TTL garbage collection into `~/.Trash`. |
| **Eight scheduled backup & hygiene jobs** | Encrypted nightly package to iCloud Drive, hourly/daily/weekly Restic snapshots, daily 04:15 TTL cache hygiene, weekly prune, integrity check and restore drill. |
| **Whole-environment migration** | `--pack-to-ssd` on the old Mac, one double-click on the new one. Agent state, SQLite databases, accounts, SSH/Git/Zsh config and core apps come along. |
| **A health check that tells you what to do** | `./deploy.sh --doctor` ends with a numbered list of what needs attention and exits non-zero when anything does. |

## Quick start

### Requirements

- macOS on Apple Silicon or Intel.
- Python 3.9 or newer. `deploy.sh` looks for one itself (Command Line Tools, python.org, Homebrew, uv);
  set `ORCA_PYTHON=/path/to/python3` to choose.
- `git` (ships with the Xcode Command Line Tools: `xcode-select --install`).
- Optional: the Orca desktop app for worktree orchestration, and `just` ≥ 1.27 for the recipes below. The CLI tools the blueprint relies on are installed into `~/.local/bin` for you.

### Pick your situation

| You want to… | Run |
| :--- | :--- |
| **See what would happen** (changes nothing) | `./deploy.sh --dry-run` |
| **Set up this Mac** (new or already full of projects) | `./deploy.sh --auto` |
| **Pack the old Mac onto an SSD** | `./deploy.sh --pack-dry-run`, then `./deploy.sh --pack-to-ssd` |
| **Restore onto a new Mac** | double-click `新Mac一键恢复.command` on the SSD |

No checkout yet? The bootstrapper clones the repository into the workspace and passes every argument on:

```bash
# Preview only
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --dry-run

# Deploy; uses an external SSD that already has an AI/ folder, otherwise ~/AI-Workspace
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh

# Choose explicitly
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --auto --external
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --auto --internal
```

<details>
<summary><b>Prefer to let an agent do it?</b></summary>

Send this to Claude Code, Codex, Antigravity or Cursor:

```text
Read https://github.com/Rem021/orca-multi-agent-blueprint/blob/main/AGENT_INSTALL.md.
Ask me which scenario I want (set up this Mac / pack the old Mac / restore onto a new Mac) and where
the AI files should live (external SSD or internal disk), show me the dry run, then carry it out.
```

[`AGENT_INSTALL.md`](./AGENT_INSTALL.md) tells the agent what to ask, which commands to run and what it must
not do (for example: never run a restore from inside the desktop app that is being restored).

</details>

<details>
<summary><b>What <code>--auto</code> does, step by step</b></summary>

1. Creates the `AI/` directory layout on the chosen volume.
2. Adds `~/.local/bin` to `~/.zshenv`, links a persistent `python3` for background jobs, enables Git
   settings that speed up many worktrees.
3. Installs `rg`, `fd`, `ast-grep`, `just`, `xcbeautify`, `scc`, `repomix` and `restic` into `~/.local/bin`
   (skipped when they are already there).
4. Installs the guardrail as `~/.local/bin/agent-guard`, writes its settings to `~/.orca/guard.env`, and
   installs the `ast-grep` skill (plus Orca's own skills when Orca is installed).
5. Adopts existing projects: adds the `docs / reports / outputs / work` folders and ignores `work/` through
   `.git/info/exclude`. Nothing is moved or deleted.
6. Installs the seven backup LaunchAgents (unless `ENABLE_AUTOMATED_BACKUPS=false`).
7. Syncs skills, MCP servers, global rules and hooks to every agent account.
8. Runs the test suites.

Existing `~/.codex/AGENTS.md`, `~/.claude/CLAUDE.md` and `~/.gemini/GEMINI.md` are kept; `--render-rules`
replaces them from the template after backing them up.

</details>

## Architecture

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/architecture-en-dark.svg?v=3">
    <source media="(prefers-color-scheme: light)" srcset="./assets/architecture-en-light.svg?v=3">
    <img alt="Three-tier architecture" src="./assets/architecture-en-light.svg?v=3" width="100%">
  </picture>
</p>

| Tier | Role | Default | Can be replaced by |
| :--- | :--- | :--- | :--- |
| **1** | Supervisor: takes the request, splits it into tasks, collects results | Muse | Orca mobile, Claude Desktop, your own script |
| **2** | Orchestration and guardrail: worktree lifecycle, account switching, command checks | Orca desktop + CLI | `just` + `scripts/projectctl.py` |
| **3** | Workers: each codes inside its own worktree | Claude, Codex, Antigravity, DeepSeek | OpenCode, Cursor, Aider |

Each tier is chosen in [`config.env`](./config.example.env); `./deploy.sh --apply` applies a change.

### Task lifecycle

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/workflow-en-dark.svg?v=3">
    <source media="(prefers-color-scheme: light)" srcset="./assets/workflow-en-light.svg?v=3">
    <img alt="Five-stage task lifecycle" src="./assets/workflow-en-light.svg?v=3" width="100%">
  </picture>
</p>

1. **Verify and branch** — check the volume, then create a worktree and task branch from a clean `main`.
2. **Develop behind the guardrail** — every shell command passes `agent-guard` first.
3. **Hand off** — `just handoff` writes a compact `HANDOFF.json` (base commit, changed files with SHA-256,
   verification notes) so the next agent does not need the chat history.
4. **Cross-review** — a different model reviews the handoff and the diff, read-only.
5. **Archive** — `just archive-worktree` writes `repo.bundle`, a `.tar.gz` snapshot and `SHA256SUMS.txt` to
   `AI/Archive/` before the worktree is removed.

## Storage layout

| | External SSD (`--external`) | Internal disk (`--internal`) |
| :--- | :--- | :--- |
| Workspace root | `/Volumes/<SSD>` | `~/AI-Workspace` (`INTERNAL_WORKSPACE_PATH`) |
| Identity check | Mount point **and** volume UUID before every write | None needed; recorded as `AI_VOLUME_UUID="LOCAL"` |
| Watchdog (every 60 s) | Mount state, UUID, USB re-enumeration | Workspace and backup folders exist |
| Good for | Keeping hundreds of GB of repos and build caches off the internal disk; moving between Macs by unplugging | Laptops that travel without a drive |
| Switch | `just use-external-storage` | `just use-internal-storage` |

With `--storage auto` (the default) an external drive is used only when it already contains an `AI/` folder.
Installer disk images, Time Machine disks, network shares and read-only volumes are never picked.

```text
<workspace root>/AI/
├── Projects/<project>/        one clone per repository, parked on a clean main
│   ├── docs/                  long-lived documentation                      (tracked)
│   ├── reports/<date-topic>/  stage reports and receipts                    (tracked)
│   ├── outputs/<topic>/       deliverables                                  (tracked)
│   └── work/                  scratch space, ignored via .git/info/exclude  (never committed)
├── Orca/Worktrees/            one directory per parallel task
├── Codex/Worktrees/
├── Claude/Worktrees/
├── Archive/<date-topic>/      repo.bundle + worktree-snapshot.tar.gz + SHA256SUMS.txt
├── Data/
│   ├── BuildCache/            package caches shared across worktrees
│   └── SimulatorCoordination/ lease files so two agents never drive the same iOS Simulator
├── Backups/restic/            local Restic repository when no NAS is configured
└── Migration/latest/          snapshot written by --pack-to-ssd
```

## Automated backups

`./deploy.sh --setup-backups` (part of `--auto`) creates two random passwords in the login Keychain
(`icloud-package-key`, `restic-mac-agents`), installs the scripts from
[`scripts/codex-storage/`](./scripts/codex-storage) and registers seven LaunchAgents:

| Job | When | Where it goes | What it covers |
| :--- | :--- | :--- | :--- |
| `volume-guard` | every 60 s | status file | Workspace volume mounted, UUID correct, USB reconnects |
| `backup-hot` | every 60 min | Restic | Agent state on the internal disk: `~/.codex`, `~/.claude`, `~/.gemini`, `~/.orca`, `~/.ssh`, … plus consistent SQLite snapshots |
| `icloud-package` | daily 02:30 | iCloud Drive | Small encrypted package of configuration, rules, scripts and databases |
| `backup-projects` | daily 03:00 | Restic | `<workspace>/AI` without build products (`DerivedData`, `.build`, `node_modules`, …) |
| `backup-media` | Sunday 04:00 | Restic | `BACKUP_MEDIA_PATH`, upload capped at about 45 MB/s |
| `maintenance` | Sunday 05:30 | Restic | `forget --prune` (24 hourly, 30 daily, 12 weekly, 24 monthly per host and tag), `check`; reads 5 % of the data on the first run of each month |
| `restore-drill` | Sunday 07:00 | scratch folder | Restores part of the newest hot snapshot with `--verify`, then deletes it |

```bash
./deploy.sh --backup-status     # or: just backup-status
```

<details>
<summary><b>How the jobs protect your data</b></summary>

- **SQLite** databases are copied through SQLite's online backup API and checked with `PRAGMA quick_check`
  before they enter a backup, so a database that an agent is writing to is never copied half-way.
- **The iCloud package** is encrypted with `openssl enc -aes-256-cbc -pbkdf2` using the Keychain password,
  then decrypted and listed again before it is published. A package that fails this check is discarded.
  Only the newest `BACKUP_ICLOUD_KEEP_LATEST` packages **of this Mac** are kept (default 1); packages of
  other Macs are left alone.
- **Each Mac has its own name** in snapshots and package file names (`BACKUP_HOST_TAG`, derived from the
  Mac's local host name). A tag inherited from another Mac through a migration is replaced automatically,
  so two Macs never prune each other's history.
- **Jobs do not overlap**: every job holds a lock for its whole run.
- **A job that cannot run says so.** When the NAS or the drive is away the job records `skipped`; after
  `BACKUP_STALE_ALERT_HOURS` without a success it becomes `stale`, a notification is shown, and
  `--doctor` reports it.

</details>

<details>
<summary><b>Using a NAS for the Restic repository</b></summary>

Set `BACKUP_RESTIC_REPO` to a folder on the share, and `BACKUP_NAS_MOUNT` / `BACKUP_NAS_SMB_MATCH` to its
mount point and `mount` signature. Connect once in Finder (⌘K) and tick *Remember this password in my
keychain*. With `BACKUP_NAS_AUTOMOUNT="auto"` the jobs then mount the share silently when the NAS answers,
and skip quietly when you are on another network. Without a NAS the repository lives in
`<workspace>/AI/Backups/restic`.

</details>

## Moving to a new Mac

```text
 old Mac                             external SSD                         new Mac
 ./deploy.sh --pack-to-ssd   ──►   AI/Migration/latest   ──►   新Mac一键恢复.command (double-click)
```

**On the old Mac**

```bash
./deploy.sh --pack-dry-run      # what would be packed, and how large it is
./deploy.sh --pack-to-ssd       # incremental; safe to run again
./deploy.sh --migration-status  # origin, age and integrity of the snapshot
```

**On the new Mac** — plug the drive in and double-click `新Mac一键恢复.command` in its root folder (or run
`zsh "/Volumes/<SSD>/新Mac一键恢复.command"` in Terminal). It works offline. The same folder holds
`新电脑恢复指南.md`, a step-by-step guide generated from the snapshot, and `AI/Migration/AGENT_RESTORE.md`
for an agent that should do the restore for you.

Close Orca, Claude, Codex and Antigravity before restoring, and run the restore from Terminal — not from
inside one of those apps.

| Carried over | Not carried over |
| :--- | :--- |
| Agent state and history (`~/.codex`, `~/.claude`, `~/.gemini`, `~/.orca`, …) | Virtual machine images (`claudevm.bundle`) and iOS Simulator devices |
| SQLite databases (consistent copies, verified) | Build caches and anything that can be re-downloaded |
| Accounts, SSH keys, Git and Zsh configuration | The login keychain itself — it is only staged under `~/Migration-Imports/` |
| Keychain passwords the backup jobs and agents need | Media and project files outside `AI/` (they stay where they are on the drive) |
| LaunchAgents and the folders they use | |
| Core apps and a `Brewfile` | |

<details>
<summary><b>What the restore does to protect the new Mac</b></summary>

- **Preflight**: refuses to start while agent apps are running, when the snapshot is incomplete, when the
  new Mac runs an older macOS than the old one, or when the disk is too small. `--force` overrides.
- **Nothing is overwritten silently**: files it replaces are kept in `~/.migration-preexisting/<timestamp>`.
- **Different user name**: `/Users/<old>` is rewritten to `/Users/<new>` in plists, configuration files and
  symlinks.
- **Third-party LaunchAgents stay off**: jobs that came from the old Mac (trading bots, sync tools, …) are
  held with `launchctl disable`, so they do not start now or at the next login. Review them with
  `./deploy.sh --agents`, start the ones you want with `./deploy.sh --agents --enable-agents <label,…>`,
  stop one again with `./deploy.sh --hold-agents <label,…>`.
- **Backup jobs** are loaded only when their Keychain passwords arrived, and the new Mac gets its own
  backup name.
- **One snapshot per Mac**: packing from a different Mac onto a drive that holds someone else's snapshot is
  refused unless you pass `--force`.

</details>

> [!WARNING]
> The snapshot contains SSH private keys and exported Keychain passwords in readable form
> (`AI/Migration/latest`, files are mode `0600`). Anyone holding the drive can read them unless the drive is
> encrypted. Use an encrypted APFS volume (Finder → right-click the drive → *Encrypt*), and delete
> `AI/Migration/latest` once the new Mac is confirmed working if the drive leaves your hands.

## Command guardrail

[`scripts/guard.py`](./scripts/guard.py) is installed as `~/.local/bin/agent-guard` and registered as the
pre-command hook of Claude Code, Codex and Antigravity. It parses each shell command (quotes, `sudo`/`env`
wrappers, `sh -c`, `git -C <dir>`, chained commands) and refuses:

| # | Refused | Do this instead |
| :-: | :--- | :--- |
| 1 | Touching `/Volumes/<SSD>/AI` while the drive is not mounted or has the wrong UUID | Plug the drive in; a look-alike folder is never created at the mount point |
| 2 | Recursive `rm` of a project, worktree or archive directory | `just archive-worktree`, then move to the Trash |
| 3 | `git reset --hard`, `git clean -f` | Commit, stash, or bundle the work first |
| 4 | `git worktree prune` without `--dry-run` | Preview first, with the drive mounted |
| 5 | Emptying the Trash | Archived copies must stay recoverable |
| 6 | `xcrun simctl create` / `clone` | Reuse the two designated simulators and their leases |
| 7 | Pushing a restricted branch (`GUARD_RESTRICTED_BRANCHES`, default `develop`, `feat/C-*`) | Keep history that contains secrets local |
| 8 | Force-pushing or deleting a shared branch (`GUARD_PROTECTED_BRANCHES`, default `main`, `master`) | Push a new commit or open a pull request |

Set a branch list to `none` to switch that rule off. To cover pushes made outside an agent as well, install
the Git hook in the repositories that matter:

```bash
./deploy.sh --install-pre-push /path/to/repo     # never replaces an existing pre-push hook
```

> [!NOTE]
> This is a guardrail against accidents, not a sandbox. A command hidden inside `python -c`, a script file
> or a remote shell is not analysed. Backups and the archive step are the second line of defence.

## Health check

```bash
./deploy.sh --doctor            # full JSON report; `summary` is the part to read
just doctor-summary             # only the verdict
```

```text
Health check: 2 item(s) need attention
  1. Backup job backup-hot has no successful run on record (last run skipped: NAS not mounted)
  2. 8 installed backup script(s) differ from this checkout; run ./deploy.sh --setup-backups.
Advice:
  - The volume holding the migration snapshot is not encrypted.
```

| Exit code | Meaning |
| :-: | :--- |
| `0` | Nothing needs attention |
| `1` | At least one item needs attention (listed in `summary.attention`) |
| `2` | A check could not be carried out |

The check is read-only. It looks at backup results (not just whether jobs are installed), the NAS, the
backup name of this Mac, the age and origin of the migration snapshot, installed scripts that lag behind
the checkout, the guardrail hooks of each agent, the Python used by background jobs, third-party
LaunchAgents, and whether every agent account has the same skills and rules.

## Everyday commands

`just --list` shows all recipes by group.

| Group | Recipes |
| :--- | :--- |
| Health and tests | `verify-volume` · `doctor` · `doctor-summary` · `test` · `test-guardrails` · `dry-run` |
| Backups | `setup-backups` · `backup-status` · `backup-now-icloud` · `backup-now-hot` |
| Migration | `pack-dry-run` · `pack-to-ssd` · `restore-dry-run` · `restore-from-ssd` · `migration-status` · `refresh-guide` · `agents` · `hold-agents <labels>` · `enable-agents <labels>` |
| Storage mode | `use-external-storage` · `use-internal-storage` |
| Daily work | `handoff` · `archive-worktree <path> <topic>` · `install-pre-push <repo>` · `slim-status` · `sim-leases` |

<details>
<summary><b>All <code>deploy.sh</code> options</b></summary>

| Option | Purpose |
| :--- | :--- |
| `--dry-run` | Preview everything; the default when no mode is given |
| `--auto` | Detect volume, simulators, projects and agents, write `config.env`, deploy |
| `--apply` | Deploy using the existing `config.env` |
| `--sync-profiles` | Re-sync skills, MCP servers, rules and hooks to all agent accounts |
| `--render-rules` | Rewrite the global rule files from the template (existing files are backed up) |
| `--doctor` | Read-only health check |
| `--install-pre-push <repo>` | Install the guardrail's `pre-push` hook into one repository |
| `--storage <auto\|external\|internal>`, `--external`, `--internal` | Where the workspace lives |
| `--volume <path>`, `--config <path>` | Use a specific volume / configuration file |
| `--setup-backups`, `--backup-status` | Install or update the backup jobs; show their state |
| `--pack-dry-run`, `--pack-to-ssd` | Preview / write the migration snapshot |
| `--restore-dry-run`, `--restore-from-ssd` | Preview / restore the snapshot, then deploy |
| `--migration-status` | Origin, age and integrity of the snapshot |
| `--refresh-guide` | Rewrite the restore guide and launchers from the existing snapshot |
| `--fast-config-only` | With pack: leave out raw session logs and app bundles |
| `--force-apps` | With pack: copy app bundles again even when unchanged |
| `--force` | With pack: replace another Mac's snapshot. With restore: skip preflight blocks |
| `--agents`, `--hold-agents <labels>`, `--enable-agents <labels>` | List, stop or start third-party LaunchAgents |

</details>

## Configuration

`./deploy.sh --auto` writes `config.env` (git-ignored) from what it finds;
[`config.example.env`](./config.example.env) documents every key. The ones most often changed:

| Key | Default | Meaning |
| :--- | :--- | :--- |
| `STORAGE_MODE` | `auto` | `external`, `internal` or `auto` |
| `AI_VOLUME_PATH` / `AI_VOLUME_UUID` | detected | Workspace root and the UUID it must have |
| `WORKER_AGENTS` | `claude,codex,antigravity,dsh` | Agents that receive skills, rules and hooks |
| `PRIMARY_SIMULATOR_UDID` / `SECONDARY_SIMULATOR_UDID` | detected | The only iOS Simulators agents may use |
| `GUARD_PROTECTED_BRANCHES` | `main master` | Never force-pushed or deleted |
| `GUARD_RESTRICTED_BRANCHES` | `develop feat/C-*` | Never pushed; `none` disables |
| `ENABLE_AUTOMATED_BACKUPS` | `true` | Install the seven backup jobs |
| `BACKUP_RESTIC_REPO` | `<workspace>/AI/Backups/restic` | Restic repository (local folder or NAS path) |
| `BACKUP_NAS_AUTOMOUNT` | `auto` | `auto`, `always` or `never` |
| `BACKUP_MEDIA_PATH` | `<workspace>/Work` | Folder for the weekly media backup |
| `BACKUP_HOST_TAG` | this Mac's name | Name of this Mac in snapshots and packages |
| `BACKUP_ICLOUD_KEEP_LATEST` | `1` | Nightly packages kept per Mac |
| `BACKUP_STALE_ALERT_HOURS` | `24` | Hours without a success before an alert |
| `EXTRA_MIGRATION_PATHS` / `EXTRA_BUNDLED_APPS` / `EXTRA_KEYCHAIN_SERVICES` | empty | Additions to the migration snapshot |

## Troubleshooting

| Symptom | Cause and fix |
| :--- | :--- |
| `--doctor` says a backup has *no successful run on record* | The job runs but skips every time. Read the message: usually the NAS share or the drive is not mounted. Mount it, then `just backup-now-hot`. |
| Backups skip after moving to another network | The NAS is unreachable from there. Nothing is lost; jobs resume when it is back. Silence the alert with `BACKUP_STALE_ALERT_HOURS=0`. |
| A login dialog for the NAS appears every hour | No password is saved for the share. Connect once in Finder with *Remember this password*, or set `BACKUP_NAS_AUTOMOUNT="never"`. |
| `--doctor` says the backup name was inherited from another Mac | Run `./deploy.sh --setup-backups`; set `BACKUP_HOST_TAG_KEEP="true"` only to continue the old Mac's history on purpose. |
| iCloud Drive fills up with nightly packages | Packages named after another Mac are never deleted by this one. Remove them by hand once that Mac is retired. |
| `--pack-to-ssd` refuses: snapshot belongs to another Mac | Intended. Pass `--force` once the old Mac's snapshot is no longer needed. |
| A LaunchAgent from the old Mac does not run on the new one | It is held on purpose. `./deploy.sh --agents`, then `--agents --enable-agents <label>`. |
| A dialog asks to install the Command Line Tools | No usable `python3`/`git` yet. Install them (`xcode-select --install`) or set `ORCA_PYTHON`. |
| The guardrail refuses a push of `develop` | That is rule 7. Set `GUARD_RESTRICTED_BRANCHES="none"` in `config.env` and run `./deploy.sh --apply`. |
| The guardrail blocks every command that mentions the SSD | The drive is unplugged or was replaced (UUID mismatch). Plug it in, or run `./deploy.sh --auto` to adopt the new drive. |

## Repository layout

```text
orca-multi-agent-blueprint/
├── bootstrap.sh               one-line installer: clone, then run deploy.sh
├── deploy.sh                  entry point for every mode
├── config.example.env         documented configuration template
├── Justfile                   everyday recipes
├── AGENT_INSTALL.md           instructions for an agent that deploys this for a user
├── scripts/
│   ├── discover_and_adopt.py  finds the volume, simulators, agents, projects; writes config.env
│   ├── sync_profiles.py       skills, MCP servers, rules, hooks → every agent account
│   ├── guard.py               the command guardrail (installed as agent-guard)
│   ├── doctor.py              health check
│   ├── migrate.py             pack / restore / status / agents / guide
│   ├── setup_backups.py       Keychain passwords + seven LaunchAgents
│   ├── codex-storage/         the backup jobs themselves (lib.sh, backup-restic.sh, icloud-package.sh,
│   │                          maintenance.sh, restore-drill.sh, volume-guard.sh, status.sh,
│   │                          sqlite_snapshot.py, volume-observer.py)
│   ├── projectctl.py          guarded worktree creation
│   ├── handoff.py             HANDOFF.json generator
│   ├── agent-slim.py          storage inventory for agent caches
│   └── install_cli_tools.sh   native CLI tools into ~/.local/bin
├── plugins/orca-naming-guard/ Orca plugin that keeps workspace names in `Project｜Topic` form
├── skills/ast-grep/           syntax-aware search and refactoring skill
├── templates/                 global rule template, supervisor playbook
├── tests/                     guard, migration, backup-script and doctor suites
└── assets/                    diagrams (light/dark, English/Chinese)
```

### Naming convention

Workspaces, worktrees and chat tabs are named `Project｜Topic` (full-width bar, no spaces around it); an idle
workspace is `Project｜待命工作区 <8-char id>`; task branches are `<agent>/<two-to-four-words>` such as
`codex/cloud-rule-save`. The [`orca-naming-guard`](./plugins/orca-naming-guard) plugin applies this locally
without spending model quota.

## Tests

```bash
just test     # guard, migration, backup scripts, doctor (Python) + naming plugin (Node)
```

The suites run against throw-away directories; they do not touch your agents, Keychain or backups. They pass on Python 3.9 (the system Python of macOS) and current Python 3.

## License

[MIT](./LICENSE)
