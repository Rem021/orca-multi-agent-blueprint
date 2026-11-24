# Orca Multi-Agent Blueprint

**Run several coding agents on one Mac without them getting in each other's way.**

One Git worktree per task, a guardrail that checks every agent command before it runs, shared rules and
skills for every agent, scheduled encrypted backups, and a one-drive move to a new Mac. Works with
Claude Code, Codex, Gemini CLI and Antigravity; the [Orca](https://orca.dev) desktop app is optional.

[简体中文](README.zh-CN.md) · [Quick start](#quick-start) · [How it works](#how-it-works) ·
[Guardrail](#command-guardrail) · [Backups](#backups) · [New Mac](#moving-to-a-new-mac) · [Troubleshooting](#troubleshooting)

[![CI](https://github.com/Rem021/orca-multi-agent-blueprint/actions/workflows/ci.yml/badge.svg)](https://github.com/Rem021/orca-multi-agent-blueprint/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Rem021/orca-multi-agent-blueprint?style=flat-square)](https://github.com/Rem021/orca-multi-agent-blueprint/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

## What you get

| | |
| :--- | :--- |
| **One layout for every project** | `AI/Projects` holds one clone per repository, parallel tasks live in `AI/<Agent>/Worktrees`, retired work is archived to `AI/Archive` with a Git bundle and checksums first. |
| **A guardrail in front of every command** | `agent-guard` runs as the pre-command hook of each agent and refuses destructive Git, delete and push operations before they happen. |
| **The same rules and skills everywhere** | One template becomes `~/.codex/AGENTS.md`, `~/.claude/CLAUDE.md` and `~/.gemini/GEMINI.md` (English or Chinese); skills, MCP servers and hooks are synced to every installed agent and account. |
| **An optional supervisor** | Work with the agents directly (default), or let a personal assistant take your requests and dispatch them. You choose once during setup. |
| **Delivery without babysitting** | `agent-ship` rebases, scans for secrets, runs checks, opens the PR, waits for CI and merges the exact commit that was checked; risky changes stop for you. |
| **Clean disks** | `agent-governor` hands out scratch and test folders with a TTL and later moves expired ones to the Trash, never deleting outright. `agent-test-device` gives each test its own simulator or VM and removes it afterwards. |
| **Backups and migration** | Hourly/daily/weekly Restic snapshots, a nightly encrypted package in iCloud Drive, a weekly restore drill, and `--pack-to-ssd` → double-click restore on a new Mac. |
| **A health check that says what to do** | `./deploy.sh --doctor` ends with a numbered list of what needs attention. |

## Quick start

**Requirements:** macOS (Apple silicon or Intel), Python 3.9+ and Git (`xcode-select --install`). Optional:
the Orca app, and [`just`](https://github.com/casey/just) for the shortcuts below. The other CLI tools are
installed into `~/.local/bin` for you, with pinned SHA-256 checksums.

```bash
git clone https://github.com/Rem021/orca-multi-agent-blueprint.git
cd orca-multi-agent-blueprint
./deploy.sh --dry-run     # preview: prints every step, writes nothing
./deploy.sh --auto        # detect this Mac, write config.env, install
```

`--auto` asks one question when it runs in a terminal: whether a personal assistant should act as the
[supervisor](#optional-supervisor). Press Enter to skip it. To decide up front, pass `--no-supervisor` or
`--supervisor <name>`.

| You want to… | Run |
| :--- | :--- |
| See what would happen | `./deploy.sh --dry-run` |
| Set up this Mac (new, or already full of projects) | `./deploy.sh --auto` (add `--external` or `--internal` to choose the disk) |
| Change the supervisor later | `./deploy.sh --apply --supervisor <name>` or `--no-supervisor` |
| Pack the old Mac onto an external drive | `./deploy.sh --pack-dry-run`, then `./deploy.sh --pack-to-ssd` |
| Restore onto a new Mac | Double-click `Restore on New Mac.command` on the drive |

<details>
<summary><b>No checkout yet: one-line bootstrap</b></summary>

The bootstrapper clones the repository into the workspace and passes every argument on to `deploy.sh`.
The whole script is wrapped in a function, so a cut-off download runs nothing.

```bash
# Preview only
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh -s -- --dry-run

# Install; uses an external drive that already has an AI/ folder, otherwise ~/AI-Workspace
curl -fsSL https://raw.githubusercontent.com/Rem021/orca-multi-agent-blueprint/main/bootstrap.sh | zsh
```

</details>

<details>
<summary><b>Let an agent install it</b></summary>

Send this to Claude Code, Codex, Gemini CLI or Antigravity:

```text
Read https://github.com/Rem021/orca-multi-agent-blueprint/blob/main/AGENT_INSTALL.md.
Ask me which scenario I want, where the AI files should live and whether I want a supervisor,
show me the dry run, then carry it out.
```

[`AGENT_INSTALL.md`](AGENT_INSTALL.md) tells the agent what to ask, what to run and what it must never do.

</details>

<details>
<summary><b>What <code>--auto</code> does</b></summary>

1. Creates the `AI/` layout on the chosen disk and writes `config.env` (git-ignored, mode `0600`).
2. Adds `~/.local/bin` to `~/.zshenv`, makes sure background jobs have a `python3`, enables Git settings that
   help with many worktrees (`feature.manyFiles`, `rerere`, relative worktree paths).
3. Installs `rg`, `fd`, `ast-grep`, `just`, `xcbeautify`, `restic` and `gh` (checksum-verified, skipped when present).
4. Installs `agent-guard`, `agent-governor`, `agent-ship` and `agent-test-device` into `~/.local/bin`, plus the
   `orca-workspace` skill.
5. Adopts existing projects: adds `docs/ reports/ outputs/ work/` and excludes `work/` through
   `.git/info/exclude`. Nothing is moved or deleted.
6. Renders the global rules for every agent that has none yet (existing files are kept; `--render-rules`
   replaces them after a backup).
7. Installs the backup LaunchAgents (skip with `ENABLE_AUTOMATED_BACKUPS="false"`).
8. Syncs skills, MCP servers, rules and hooks to every installed agent and account, then self-tests the guardrail.

</details>

## How it works

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/architecture-en-dark.svg?v=4">
    <source media="(prefers-color-scheme: light)" srcset="./assets/architecture-en-light.svg?v=4">
    <img alt="Three tiers: optional supervisor, control plane, workers" src="./assets/architecture-en-light.svg?v=4" width="100%">
  </picture>
</p>

| Tier | Role | Default | Alternatives |
| :--- | :--- | :--- | :--- |
| **1 · Supervisor (optional)** | Takes a request, splits it into tasks, collects results | None: you direct the agents | Any personal assistant, Orca mobile, your own script |
| **2 · Control plane** | Worktree lifecycle, account switching, rules and hook sync, guardrail | Orca desktop + CLI | `just` + `git worktree` |
| **3 · Workers** | Each agent codes in its own worktree | Claude Code, Codex | Gemini CLI, Antigravity, Cursor, OpenCode, Aider |

### Optional supervisor

By default there is no supervisor: you talk to the coding agents yourself, in Orca or a terminal.

If you already use a personal assistant that can drive the `orca` CLI, name it during `--auto`, or later
with `./deploy.sh --apply --supervisor <name>`. The name is saved in `config.env` (`SUPERVISOR_AGENT`) and
written into the global rules, so every worker knows that tasks may come from it, reports results back to
it, and still asks **you** directly about the decisions listed in the rules (data migrations, releases,
credentials, breaking changes). `--no-supervisor` removes it again. The blueprint does not install or
configure the assistant itself.

### Task lifecycle

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/workflow-en-dark.svg?v=4">
    <source media="(prefers-color-scheme: light)" srcset="./assets/workflow-en-light.svg?v=4">
    <img alt="Branch, build, ship, review, archive" src="./assets/workflow-en-light.svg?v=4" width="100%">
  </picture>
</p>

1. **Branch** — a new worktree and task branch (`<prefix>/<topic>`) from a clean `main`.
2. **Build** — every shell command and file write passes `agent-guard` first.
3. **Ship** — `agent-ship` scans for secrets and new test skips, runs checks, opens the PR, waits for CI, merges.
4. **Review** — optionally a different model reviews the diff read-only.
5. **Archive** — `just archive-worktree` writes a Git bundle, a snapshot and `SHA256SUMS.txt` before anything is removed.

## Workspace layout

| | External drive (`--external`) | Internal disk (`--internal`) |
| :--- | :--- | :--- |
| Root | `/Volumes/<drive>` | `~/AI-Workspace` (`INTERNAL_WORKSPACE_PATH`) |
| Identity check | Mount point **and** volume UUID before every write | Not needed (`AI_VOLUME_UUID="LOCAL"`) |
| Good for | Keeping large repos and caches off the internal disk; moving between Macs by unplugging | Laptops without a drive |

With `--storage auto` (the default) an external drive is used only when it already has an `AI/` folder.
Disk images, Time Machine disks, network shares and read-only volumes are never picked.

```text
<root>/AI/
├── Projects/<project>/        one clone per repository, parked on a clean main
│   ├── docs/  reports/<date-topic>/  outputs/<topic>/      tracked
│   └── work/{_scratch,_tests,retained}/                    git-excluded, not indexed
├── Orca/Worktrees/  Codex/Worktrees/  Claude/Worktrees/    one directory per parallel task
├── Archive/<date-topic>/      repo.bundle + worktree-snapshot.tar.gz + SHA256SUMS.txt
├── Data/                      shared build caches
├── Backups/restic/            local Restic repository when no NAS is configured
└── Migration/latest/          snapshot written by --pack-to-ssd
```

## Command guardrail

[`scripts/guard.py`](scripts/guard.py) is installed as `~/.local/bin/agent-guard` and registered as the
pre-command hook of Claude Code (`PreToolUse`), Codex (`PreToolUse`), Gemini CLI (`BeforeTool`) and
Antigravity. It parses each command (quoting, `sudo`/`env` wrappers, `sh -c`, here-strings, `git -C`,
`git -c`, chained and piped commands) and refuses:

| Refused | Do this instead |
| :--- | :--- |
| Writing to `/Volumes/<drive>/AI` while the drive is missing or has the wrong UUID | Plug the drive in; no look-alike folder is created at the mount point |
| Recursive delete of a project, worktree, archive, `.git`, the workspace or the home folder (case-insensitive, symlinks resolved) | `just archive-worktree`, then move it to the Trash |
| `git reset --hard`, `git clean -f`, `git checkout -f` / `git switch --discard-changes`, `git restore .`, `git stash clear`, including abbreviations and `-c` overrides | Commit, stash or bundle first |
| `git worktree prune` without `--dry-run` | Preview first |
| Emptying the Trash | Archived copies stay recoverable |
| Force-pushing or deleting a protected branch (`GUARD_PROTECTED_BRANCHES`, default `main master`) | Push a new commit or open a pull request |
| Pushing a restricted branch (`GUARD_RESTRICTED_BRANCHES`, default none) | Keep that history local |
| Reading private keys and credential files (`~/.ssh/id_*`, `.p12`, …) | Ask the user |
| Creating `tmp/`, `scratch/`, `.worktrees/` in a project or worktree root | `agent-governor scratch --topic <topic>` |
| Quitting the host Orca app, `launchctl submit` | — |

> [!NOTE]
> This is a guardrail against accidents, not a sandbox. Commands inside a script file, `python -c`, a remote
> shell or a downloaded installer (`curl … | sh`) are not analysed, nor is variable indirection such as
> `R=rm; $R …`. Backups and the archive step are the second line of defence.

**Codex** only runs a hook after you approve it: open Codex once after installing and run `/hooks`.
To cover pushes made outside an agent too, add the Git hook to the repositories that matter:
`./deploy.sh --install-pre-push /path/to/repo` (never replaces an existing `pre-push` hook).

## Shipping with `agent-ship`

Run inside a task worktree after committing. It refuses dirty trees, default branches and repositories listed
in `~/.orca/ship.json` (`_deny_repos`), then:

1. scans every commit of the branch (as text, ignoring `.gitattributes` tricks) and the commit messages for
   secrets, and the diff for newly added test skips;
2. rebases on the latest `main`, runs the checks from `~/.orca/ship.json` plus any `--check`, and scans again
   if that changed anything;
3. pushes, opens the PR, waits for CI and merges with `--match-head-commit`, so only the checked commit lands.

Changes to signing, billing, migrations or CI workflows stop with the PR open (`--no-merge` does that on
purpose). Exit codes: `0` merged · `2` refused · `3` conflict · `4` check failed · `5` secret found ·
`6` CI failed · `7` merge failed · `10` waiting for you.

## Folders, cleanup and test devices

```bash
agent-governor scratch --topic login-fix      # work/_scratch/…  (3-day TTL)
agent-governor test-dir --topic login-fix     # work/_tests/…    (7-day TTL)
agent-governor gc                             # dry run
agent-governor gc --apply                     # move expired items to the Trash, with a restore manifest
agent-governor audit                          # rule-file budgets, skills, project layout (read-only)
```

`gc` skips anything tracked by Git, open by a process, leased by a live task, marked `.keep`/`.pinned`, or under
`work/retained/`. It runs every few hours from the `agent-hygiene` job.

```bash
agent-test-device simulator --type com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro -- ./run-ui-tests
agent-test-device qemu qemu-system-aarch64 -M virt -display none -drive file=base.qcow2,format=qcow2 -- ./run-vm-tests
```

Each run creates one simulator or VM (`AGENT_TEST_UDID`, `AGENT_TEST_QMP`), runs the command, and deletes that
instance on success, failure or Ctrl-C. QEMU runs with `-snapshot`, so base images are never modified.

## Backups

`./deploy.sh --setup-backups` (part of `--auto`) stores two random keys in the login Keychain, installs the
jobs from [`scripts/codex-storage/`](scripts/codex-storage) and registers these LaunchAgents:

| Job | When | Target | What |
| :--- | :--- | :--- | :--- |
| `volume-guard` | every 60 s (external) / 10 min (internal) | status file | Drive mounted with the right UUID |
| `backup-hot` | hourly | Restic | Agent state on the internal disk (`~/.codex`, `~/.claude`, `~/.gemini`, `~/.orca`, `~/.ssh`, …) and consistent SQLite copies |
| `icloud-package` | daily 02:30 | iCloud Drive | Small encrypted package of configuration, rules, scripts and databases |
| `backup-projects` | daily 03:00 | Restic | `<root>/AI` without build products |
| `backup-media` | Sunday 04:00 | Restic | `BACKUP_MEDIA_PATH`, bandwidth-capped (off when empty) |
| `maintenance` | Sunday 05:30 | Restic | `forget --prune` (24 hourly, 30 daily, 12 weekly, 24 monthly) and `check` |
| `restore-drill` | Sunday 07:00 | scratch folder | Restores part of the newest snapshot and verifies it |
| `agent-hygiene` | hourly | — | iCloud package retention; `agent-governor gc` every 4 h |

```bash
./deploy.sh --backup-status     # or: just backup-status
```

<details>
<summary><b>How the jobs protect your data</b></summary>

- **SQLite** files are copied with SQLite's online backup API and checked with `PRAGMA quick_check`, so a
  database that an agent is writing to is never captured half-way.
- **The iCloud package** is streamed through `openssl enc -aes-256-cbc -pbkdf2 -iter 600000` and authenticated
  with an HMAC-SHA256 (encrypt-then-MAC) using keys from the Keychain. It is verified and test-decrypted before
  it is published; no unencrypted archive is written to disk. To open one by hand:

  ```bash
  security find-generic-password -s icloud-package-key -w \
    | python3 "$HOME/Library/Application Support/Codex Storage/agents/icloud_crypto.py" decrypt PKG \
    | tar -xzf - -C /path/to/restore
  ```

  `decrypt` checks the HMAC first and refuses a modified package; packages made before 3.0 still open,
  with a warning that they carry no integrity tag.
- **Retention** keeps each Mac's newest confirmed-uploaded packages (`BACKUP_ICLOUD_KEEP_LATEST`) and never
  removes another Mac's newest one; packages dated in the future never displace current ones.
- **Each Mac has its own backup name** (`BACKUP_HOST_TAG`); a name inherited through migration is replaced.
- **Jobs never overlap** and a job that cannot run says so: it records `skipped`, turns `stale` after
  `BACKUP_STALE_ALERT_HOURS`, shows a notification, and `--doctor` reports it.

</details>

<details>
<summary><b>Using a NAS for Restic</b></summary>

Set `BACKUP_RESTIC_REPO` to a folder on the share, plus `BACKUP_NAS_MOUNT` and `BACKUP_NAS_SMB_MATCH`.
Connect once in Finder (⌘K) and tick *Remember this password in my keychain*. With
`BACKUP_NAS_AUTOMOUNT="auto"` the jobs mount the share silently when the NAS answers and skip quietly
elsewhere. Without a NAS the repository lives in `<root>/AI/Backups/restic`.

</details>

## Moving to a new Mac

```text
 old Mac                           external drive                     new Mac
 ./deploy.sh --pack-to-ssd  ──►  AI/Migration/latest  ──►  Restore on New Mac.command (double-click)
```

```bash
./deploy.sh --pack-dry-run       # what would be packed, and how large it is
./deploy.sh --pack-to-ssd        # incremental; safe to run again
./deploy.sh --migration-status   # origin, age and integrity of the snapshot
```

On the new Mac, plug the drive in and double-click `Restore on New Mac.command` in its root folder (or run
`zsh "/Volumes/<drive>/AI/Migration/restore-on-new-mac.sh"`). It works offline. `RESTORE-GUIDE.md` next to it
explains each step, and `AI/Migration/AGENT_RESTORE.md` is written for an agent doing the restore.
Quit Orca, Claude, Codex and other agent apps and CLIs first, and run the restore from Terminal.

| Carried over | Not carried over |
| :--- | :--- |
| Agent state and history (`~/.codex`, `~/.claude`, `~/.gemini`, `~/.orca`, …) | VM images and iOS Simulator devices |
| SQLite databases (consistent, verified copies) | Build caches and anything re-downloadable |
| SSH keys, Git and Zsh configuration | The login keychain itself |
| Keychain items the backups and agents need | Files outside `AI/` on the drive (they stay there) |
| LaunchAgents and the folders they use; core agent apps | |

The restore keeps every file it replaces in `~/.migration-preexisting/<timestamp>`, rewrites `/Users/<old>` to
`/Users/<new>`, never overwrites a different Keychain value, gives the new Mac its own backup name, and keeps
third-party LaunchAgents from the old Mac **held** until you enable them
(`./deploy.sh --agents`, `--agents --enable-agents <label,…>`). Extra paths, apps and Keychain items can be
added with `EXTRA_MIGRATION_PATHS`, `EXTRA_RESTIC_PATHS`, `EXTRA_BUNDLED_APPS` and `EXTRA_KEYCHAIN_SERVICES`.

> [!WARNING]
> The snapshot contains SSH private keys and exported Keychain passwords (files are mode `0600`). Anyone with
> the drive can read them unless it is encrypted: use an encrypted APFS volume, only restore from a drive you
> trust, and delete `AI/Migration/latest` once the new Mac works if the drive leaves your hands.

## Health check

```bash
./deploy.sh --doctor        # full JSON report
just doctor-summary         # verdict only
```

```text
Health check: 2 item(s) need attention
  1. Backup job backup-hot has no successful run on record (last run skipped: NAS not mounted)
  2. 8 installed backup script(s) differ from this checkout; run ./deploy.sh --setup-backups.
```

Exit code `0` nothing to do · `1` items need attention (`summary.attention`) · `2` a check could not run.
It is read-only and looks at backup results, the NAS, the migration snapshot, installed tools that lag behind
the checkout, guardrail hooks per agent, background Python, third-party LaunchAgents, and whether every
agent and account has the same skills and rules.

## Configuration

`./deploy.sh --auto` writes `config.env`. [`config.example.env`](config.example.env) lists every key with its
default; copy a line into `config.env`, edit it, and run `./deploy.sh --apply`. Most used:

| Key | Default | Meaning |
| :--- | :--- | :--- |
| `STORAGE_MODE` | `auto` | `external`, `internal` or `auto` |
| `SUPERVISOR_AGENT` | `none` | Name of the optional personal-assistant supervisor |
| `RULES_LANGUAGE` | `en` (`zh-CN` on a Chinese macOS) | Language of the generated global rules |
| `DEFAULT_BRANCH_PREFIX` | `codex` | Prefix of task branches |
| `GUARD_PROTECTED_BRANCHES` | `main master` | Never force-pushed or deleted |
| `GUARD_RESTRICTED_BRANCHES` | `none` | Never pushed at all |
| `PRIMARY_SIMULATOR_UDID` / `SECONDARY_SIMULATOR_UDID` | detected | Long-lived shared simulators, never cleaned up |
| `ENABLE_AUTOMATED_BACKUPS` | `true` | Install the backup jobs |
| `BACKUP_RESTIC_REPO` | `<root>/AI/Backups/restic` | Local folder or NAS path |
| `BACKUP_ICLOUD_DIR` | `iCloud Drive/Agent Backups/Nightly` | Where the nightly package goes |
| `BACKUP_MEDIA_PATH` | empty | Large folder for the weekly bandwidth-capped backup |
| `BACKUP_ICLOUD_KEEP_LATEST` | `1` | Packages kept per Mac |

## Commands

`just --list` shows every recipe. The most used ones:

| Group | Recipes |
| :--- | :--- |
| Health | `doctor` · `doctor-summary` · `test` · `dry-run` · `verify-volume` |
| Backups | `setup-backups` · `backup-status` · `backup-now-hot` · `backup-now-icloud` · `prune-icloud-dry-run` |
| Migration | `pack-dry-run` · `pack-to-ssd` · `restore-dry-run` · `restore-from-ssd` · `migration-status` · `agents` |
| Daily | `archive-worktree <path> <topic>` · `install-pre-push <repo>` · `governor-audit` · `govern-gc` |

<details>
<summary><b>All <code>deploy.sh</code> options</b></summary>

| Option | Purpose |
| :--- | :--- |
| `--dry-run` | Preview everything (default when no mode is given) |
| `--auto` | Detect this Mac, write `config.env`, deploy |
| `--apply` | Deploy from the existing `config.env` |
| `--supervisor <name>`, `--no-supervisor` | Set or clear the optional supervisor (re-renders the rules) |
| `--sync-profiles` | Re-sync skills, MCP servers, rules and hooks to every agent and account |
| `--render-rules` | Rewrite the global rule files from the template (existing files are backed up) |
| `--doctor` | Read-only health check |
| `--install-pre-push <repo>` | Install the guardrail's `pre-push` hook into one repository |
| `--storage <auto\|external\|internal>`, `--external`, `--internal` | Where the workspace lives |
| `--volume <path>`, `--config <path>` | Use a specific drive / configuration file |
| `--setup-backups`, `--backup-status` | Install or update the backup jobs; show their state |
| `--pack-dry-run`, `--pack-to-ssd` | Preview / write the migration snapshot |
| `--restore-dry-run`, `--restore-from-ssd` | Preview / restore the snapshot, then deploy |
| `--migration-status`, `--refresh-guide` | Snapshot status; rewrite the restore guide and launcher |
| `--fast-config-only`, `--force-apps`, `--force` | Pack/restore modifiers (see `--help`) |
| `--agents`, `--hold-agents <labels>`, `--enable-agents <labels>` | List, stop or start third-party LaunchAgents |

</details>

## Troubleshooting

| Symptom | Cause and fix |
| :--- | :--- |
| `--doctor`: a backup has *no successful run on record* | The job keeps skipping, usually because the NAS or the drive is not mounted. Mount it, then `just backup-now-hot`. |
| A NAS login dialog appears every hour | No password saved for the share. Connect once in Finder with *Remember this password*, or set `BACKUP_NAS_AUTOMOUNT="never"`. |
| `--doctor`: the backup name was inherited from another Mac | Run `./deploy.sh --setup-backups`. |
| iCloud Drive fills up with packages | `just prune-icloud-dry-run`, then `just prune-icloud`. |
| Codex ignores the guardrail | Approve the hook once in Codex with `/hooks`. |
| `--pack-to-ssd` refuses: the snapshot belongs to another Mac | Intended. Pass `--force` once the other snapshot is no longer needed. |
| A LaunchAgent from the old Mac does not run | It is held on purpose: `./deploy.sh --agents`, then `--agents --enable-agents <label>`. |
| A dialog offers to install the Command Line Tools | No usable `python3`/`git` yet. Install them, or set `ORCA_PYTHON=/path/to/python3`. |
| The guardrail blocks every command that mentions the drive | The drive is unplugged or was replaced. Plug it in, or run `./deploy.sh --auto` to adopt the new one. |

## Repository layout

```text
bootstrap.sh            one-line installer (clone, then deploy.sh)
deploy.sh               entry point for every mode
config.example.env      every setting with its default
Justfile                everyday shortcuts
AGENT_INSTALL.md        instructions for an agent that installs this for a user
templates/              global rule templates (English, Chinese)
skills/orca-workspace/  the workspace skill installed for every agent
scripts/
  guard.py              agent-guard        command guardrail
  ship.py               agent-ship         rebase, scan, PR, CI, merge
  agent_governor.py     agent-governor     scratch/test folders, TTL cleanup, audits
  test_device.py        agent-test-device  disposable simulators and VMs
  discover_and_adopt.py detection, config.env, project adoption, rule rendering
  sync_profiles.py      skills, MCP servers, rules and hooks → every agent and account
  doctor.py             health check
  setup_backups.py      Keychain keys and LaunchAgents
  migrate.py            pack / restore / status / third-party LaunchAgents
  install_cli_tools.sh  pinned CLI tools into ~/.local/bin
  codex-storage/        the backup jobs
tests/                  unittest suites (run in temporary folders)
```

## Tests

```bash
just test      # or: python3 -B -m unittest discover -s tests
```

The suites never touch your agents, Keychain or backups, and run on Python 3.9 (the macOS system Python) and
the newest Python in CI.

## Related

- [Workspace Naming Guard](https://github.com/Rem021/workspace-naming-guard): an Orca plugin that names
  workspaces `Project｜Topic` automatically, without spending model tokens.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
