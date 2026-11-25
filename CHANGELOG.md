# Changelog

Notable changes to `orca-multi-agent-blueprint`. Versions follow [Semantic Versioning](https://semver.org).

## [Unreleased]

## [3.1.0] - 2026-11-25

Removes four slowdowns that had crept in. Run `./deploy.sh --apply` to pick them up.

### Changed

- **Faster hook on every agent tool call.** `agent-guard`, `agent-governor`, `agent-ship` and
  `agent-test-device` are now three-line launchers in `~/.local/bin` that run precompiled modules from
  `~/.local/lib/orca-blueprint`; before, Python recompiled the whole 3 000+ line file on every call. With
  lazy imports and an argparse-free hook path, a guard check drops from about 45 ms to about 30 ms.
- **`agent-governor gc` about 2.5× faster** (15 s → 6 s on a busy workspace): an unexpired lease or any recent
  file in a `work/_scratch` / `work/_tests` entry now ends the check early, so only entries that are actually
  due get the full walk.
- **The nightly iCloud package no longer carries raw Claude Code transcripts** (`~/.claude/projects`), which
  made the "small" package grow by gigabytes; they stay in the hourly Restic backup. Set
  `BACKUP_ICLOUD_INCLUDE_TRANSCRIPTS="true"` to include them again.
- **Session-exit cleanup is off by default** (`ENABLE_SESSION_HYGIENE`): it repeated the scheduled
  `agent-hygiene` cleanup and made every Claude Code session wait for it on exit.

### Fixed

- `agent-ship` no longer reports "CI failed" when `gh pr checks --watch` exits non-zero for a transient
  reason (API error, a check that registered late): it re-reads the check list and stops only on a check
  GitHub reports as failed or cancelled, or when the list cannot be read.

## [3.0.0] - 2026-11-24

A ground-up review: security fixes across every module, a generic setup with no personal defaults, and an
optional supervisor instead of a fixed one.

### Upgrading from 2.x

1. `git pull`, then `./deploy.sh --apply`. Existing `config.env`, rule files and backup settings are kept.
2. `GUARD_RESTRICTED_BRANCHES` now defaults to none. If you relied on the old default, add
   `GUARD_RESTRICTED_BRANCHES="develop feat/C-*"` to `config.env`.
3. Personal apps and folders are no longer built in (see *Removed*). Add the ones you use to
   `EXTRA_BUNDLED_APPS`, `EXTRA_MIGRATION_PATHS` or `EXTRA_RESTIC_PATHS`.
4. Codex: approve the guardrail hook once with `/hooks`.
5. Nightly iCloud packages now use an authenticated format; `--apply` (or `--setup-backups`) installs the
   new `icloud_crypto.py` that the job needs. Older packages still open with the same helper.

### Security

- **agent-guard**: protected paths match case-insensitively (APFS is case-insensitive) and are checked again
  after resolving symlinks; `~user` is expanded. Closed bypasses through here-strings and `echo … | sh`,
  `env -S`/`-C`, `sudo -D`, long wrapper options, `$'…'` and zsh `=cmd`, git abbreviations (`--h`, `--f`),
  `-c alias.*`, repository aliases, `GIT_CONFIG_*`, `find -ok`/`-exec sh -c`, `xargs rm -r`, `rsync --del*`,
  `git push -o…`, `--mirror`, `--prune --all`, `gh api` force updates, input redirection of secret files and
  `git switch --discard-changes`. Brace expansion is bounded (a crafted command could time the hook out);
  unexpected errors now deny shell and write tools instead of allowing them; the hook never answers
  `allow` or `ask`, so it can no longer approve a tool call on the user's behalf. Fewer false positives for
  filtered `find -delete`, `mktemp` folders and `.git/rebase-*`.
- **agent-ship**: diffs are scanned as text with `--no-textconv --no-ext-diff`, so `.gitattributes` or binary
  files cannot hide a secret; commit messages and merge commits are scanned; everything is scanned again on
  the final HEAD after rebase and checks, and only that SHA is pushed and merged. More token formats and
  credential file names; a secret in a key position is fully redacted.
- **Migration**: pack no longer writes through symlinks into live databases; restore holds third-party
  LaunchAgents before any step that can fail, rejects snapshot paths that escape through symlinks, puts `--`
  before every rsync source, never overwrites a different Keychain value, keeps the WAL of replaced
  databases, detects running agent CLIs, and quotes paths in the generated launcher.
- **Backups**: the iCloud package is streamed (no plaintext archive on disk), encrypted with PBKDF2 at 600 000
  iterations and authenticated with HMAC-SHA256; CLI tools are pinned to versions and SHA-256 sums in
  `scripts/cli-tools.lock`; `bootstrap.sh` runs from a function and only reuses a checkout of the official
  repository; `/usr/bin/security` and `/usr/bin/openssl` are called by absolute path.
- **config.env** written by `--auto` can no longer execute code through a crafted volume or simulator name.

### Fixed

- `config.example.env` copied to `config.env` made every Restic job skip forever (`${…}` defaults were stored
  literally); `setup_backups.py` now evaluates the file with zsh and refuses unexpanded values.
- A future-dated iCloud package caused every new nightly package to be deleted after upload.
- Two Macs with the same LocalHostName shared one backup name; new and migrated installs add a short machine id.
- `agent-governor`: the Xcode sweep could stage build output inside a leased or pinned test folder;
  simulators were deleted by loose name matching (now only ones created by `agent-test-device`, unless
  `--broad-simulator-names`); LaunchServices entries in folders the job could not read were unregistered;
  SwiftPM checkouts made test folders unreclaimable; leases now record the owner's start time; one `lsof`
  snapshot replaces per-folder scans. The system DerivedData TTL is 7 days (was 12 hours).
- `agent-hygiene`: iCloud pruning no longer stops when the drive is away or GC fails, and the GC throttle holds.
- `sync_profiles.py` wrote duplicate TOML tables for some Codex configs; it now parses them and writes atomically.
  Gemini CLI gets the guardrail through `BeforeTool`; agents that are not installed are left alone; turning
  the guardrail off removes the hooks. `doctor` reports whether Codex approved the hook and no longer fails
  on a Mac without `~/.gitconfig` or Xcode.
- Restore drill restores only paths that exist in the newest snapshot; jobs use a kernel lock (`flock`); SQLite
  copies are checked with `PRAGMA quick_check`; a workspace folder inside a volume is recognised.

### Changed

- **Supervisor is optional.** `SUPERVISOR_AGENT` defaults to `none`; `--auto` asks once in a terminal, and
  `--supervisor <name>` / `--no-supervisor` set it later. A configured supervisor is written into the global
  rules (tasks may come from it, results go back to it, key decisions still go to the user).
- **Global rules in two languages.** `templates/AGENTS.md.tmpl` (English) and `templates/AGENTS.zh-CN.md.tmpl`
  (Chinese), chosen by `RULES_LANGUAGE` (`--auto` detects a Chinese macOS). Supervisor, external-drive,
  restricted-branch and simulator lines are only rendered when they apply. Rule files are written atomically,
  through symlinks, and left alone when unchanged.
- **`config.example.env`** lists every key commented out with its default (copying it changes nothing and no
  parser can misread `${…}` defaults). `config.env` written by `--auto` quotes every value literally.
- **Justfile** in English; it sources `config.env` with zsh instead of loading it as dotenv. Recipe arguments
  are passed as environment variables, so labels and topics can no longer inject shell code.
- **Diagrams** redrawn: tier 1 shows "you, directly" (default) and an optional personal assistant; the
  lifecycle shows `agent-ship` instead of the removed hand-off step.
- `deploy.sh --apply` self-tests only the guardrail; the full suite runs in CI and with `just test`.
- The `orca-workspace` skill gets the checkout path at install time instead of a hard-coded path.
- Restore launcher and guide on the drive are now `Restore on New Mac.command` and `RESTORE-GUIDE.md`
  (English with a Chinese summary); old files are reported, not deleted.
- New installs from `bootstrap.sh` go to `AI/Projects/orca-multi-agent-blueprint`; an existing checkout in the
  old location keeps working. Default iCloud folder is `Agent Backups/Nightly` (an existing legacy folder is kept).
- All user-facing messages of the guardrail, backups and status board are in English.

### Removed

- Personal defaults: Muse as default supervisor, the `dsh` worker, the `develop feat/C-*` restricted
  branches, placeholder simulator UDIDs, personal project aliases, and unused keys `ORCA_RELAY_HOST`,
  `ORCA_RELAY_PORT`, `PROJECT_ALIASES_JSON`.
- `scripts/projects.json` (nothing read it).
- Personal apps and folders from the built-in backup and migration lists (Muse, DeepSeek Harness, Grok Bot,
  CC Same, Mole, Ghostty, Tailscale, `~/opt`, `.pymobiledevice3`, `~/Applications`, …).
- `agent-guard` no longer reads the checkout's `config.env`; it reads `~/.orca/guard.env` written by `deploy.sh`.

### Added

- `SECURITY.md` with private vulnerability reporting, `CONTRIBUTING.md`, issue and pull-request templates.
- CI: actions pinned to full commit SHAs, `persist-credentials: false`, `zsh -n` syntax check, concurrency
  limit, Python 3.9 and the newest 3.x; Dependabot for GitHub Actions.

## [2.4.0] - 2026-11-22

- Guardrail refuses reads of private keys and credential files; warns when two worktrees edit the same file.
- `agent-ship` refuses newly added test skips (`--allow-test-skip` to override).
- Unified `orca-workspace` skill slimmed by half; retired modules removed (supervisor playbook, `projectctl`,
  hand-off packs, bundled repomix/scc).

## [2.3.0] - 2026-11-20

- Guardrail: shared glob matching for protected branches, more `git clean` / `git checkout` / Trash bypasses
  closed, fail-closed volume and pre-push checks.
- `agent-governor gc` about 3× faster; LaunchServices sweep skips unmounted volumes.
- Backups: `volume-guard` every 10 min in internal mode, iCloud upload watcher survives job exit, config
  precedence fixes, cross-user Keychain import.
- `agent-ship`: CI detection scoped to workflow triggers, restricted branches and URL-encoded refs respected.

## [2.2.0] – [2.2.1] - 2026-11-20

- One unified `orca-workspace` skill replaces three; legacy copies are retired automatically.
- Global `core.fsmonitor` no longer enabled (it leaked a daemon per worktree).
- `agent-hygiene` hourly with GC throttled to every 4 h; standalone `agent-slim` retired.

## [2.1.0] – [2.1.2] - 2026-11-14

- `agent-governor` embedded: context budgets, `work/{_scratch,_tests,retained}` lifecycle, reversible Trash staging.
- GitHub Actions CI on macOS with Python 3.9 and 3.12.
- Guardrail: subshell and command-substitution tracking, file-tool protection, worktree-root protection.
- `agent-ship`: full-history secret scan, high-risk path gate, fail-closed CI wait.
- iCloud package pruning only after the upload is confirmed; Restic `--retry-lock`.
- Xcode runner and LaunchServices hygiene rule with automatic clean-up.

## [2.0.0] - 2026-11-06

- First public release: three-tier layout, external or internal storage, cross-Mac migration, Restic and
  iCloud backups.

[Unreleased]: https://github.com/Rem021/orca-multi-agent-blueprint/compare/v3.1.0...HEAD
[3.1.0]: https://github.com/Rem021/orca-multi-agent-blueprint/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/Rem021/orca-multi-agent-blueprint/compare/v2.1.2...v3.0.0
