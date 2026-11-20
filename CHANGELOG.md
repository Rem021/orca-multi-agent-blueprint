# Changelog

All notable changes to `orca-multi-agent-blueprint` are documented in this file.

## [2.2.0] - 2026-11-20

### Changed
- **Unified Single Workspace Skill (`skills/orca-workspace/SKILL.md`)**: Consolidated the three split custom skills (`agent-governor`, `agent-slim`, `ast-grep`) into one lean `orca-workspace` skill (~105 tokens frontmatter, -75% startup tax), while keeping `~/.local/bin/agent-governor` and `~/.local/bin/agent-slim` CLIs intact. `sync_profiles.py` automatically retires legacy split skill mounts across all clients safely (unlinking symlinks and moving legacy copies to `~/.Trash`).
- **Disable Global `core.fsmonitor` (`deploy.sh`, `doctor.py`)**: Stopped enabling `core.fsmonitor=true` globally because multi-worktree setups spawn hundreds of persistent `git fsmonitor--daemon` processes; `doctor.py` now advises unsetting global `core.fsmonitor` when detected.
- **Throttle `agent-hygiene` Background GC (`setup_backups.py`, `agent-hygiene.sh`)**: Relaxed `local.kafu.agent-hygiene` LaunchAgent wakeup from 30m to 1h (preserving timely iCloud upload polling) while throttling `agent-governor gc` to every 4h (`AGENT_HYGIENE_GC_INTERVAL_SEC=14400`) and removing the redundant 24h `agent-slim` overlap call.

### Fixed
- **Synced `agent-governor v1.4.1` & `agent-slim v2.3.2`**: Fixed LaunchServices `lsregister -dump` false positives on unmounted DMG `container id:` volume paths and `@~/.claude/CLAUDE.md` global pointer drift in `audit`.

## [Unreleased]

### Added
- **iCloud Package Rolling & Polling Pruner (`scripts/codex-storage/icloud_prune.py`)**:
  - Per-host rolling retention (`BACKUP_ICLOUD_KEEP_LATEST`, `BACKUP_ICLOUD_MAX_AGE_DAYS`, `BACKUP_ICLOUD_PRUNE_SCOPE=all-hosts|self`, `BACKUP_ICLOUD_STALE_HOST_DAYS`) with fail-closed `icloud_status` upload verification so each active Mac keeps its newest confirmed-uploaded archive while superseded multi-host and legacy hostless (`mac-agents-YYYYMMDD-HHMMSS.tar.gz.enc`) archives are reclaimed automatically.
  - Periodic polling in `agent-hygiene.sh` (`BACKUP_ICLOUD_PRUNE_INTERVAL_SEC`, default 3600s; 30m fast-poll whenever `icloud-package` is in `warn` waiting for upload confirmation) that automatically evicts, prunes, and reconciles `icloud-package` receipts back to `ok` once iCloud finishes syncing.
  - Standalone `--prune-only` mode on `icloud-package.sh` and `just prune-icloud` / `just prune-icloud-dry-run` recipes, plus shared per-host `.published-<host>.list` ledgers for graceful fallback under macOS LaunchAgent TCC directory-listing restrictions.

## [2.1.1] - 2026-11-14

### Changed
- **Embedded [`agent-governor` v1.3.2](https://github.com/Rem021/agent-governor) & [`agent-slim` v2.3.0](https://github.com/Rem021/agent-slim)**: Synced 13-entrypoint multi-account global rule parity audit, broken local MCP binary & Claude memory bloat checks, `--clean-agent-clutter` workspace orphan sweep, and reversible Trash `batch-<ts>/manifest.json` staging.
- **Streamlined 22-Line `templates/AGENTS.md.tmpl` & Full Profile Parity (`sync_profiles.py`)**: Trimmed global rule template token footprint while preserving all guardrails/decision gates, and synced full SHA-256 content across Orca Claude profiles, Codex runtime home, and Gemini Antigravity entrypoints.

## [2.1.0] - 2026-11-14

### Added
- **Embedded [`agent-governor` v1.3.1](https://github.com/Rem021/agent-governor)**: Context-budget linter (`AGENTS.md <= 60` lines, `SKILL.md <= 500` lines), 4-zone + 3-tier `work/{_scratch,_tests,retained}` standardizer, recursive `lsof +D` open-handle guard, `journal.jsonl` Write-Ahead Log crash-safe Trash staging, and daily `04:15` `agent-hygiene` LaunchAgent.
- **GitHub Actions CI (`.github/workflows/ci.yml`)**: Automated verification across Python `3.9` & `3.12` on `macos-latest` (`129` unit tests).

### Security & Hardening
- **`guard.py` (`agent-guard`)**: Subshell `(...)` & command-substitution `$(...)` CWD isolation, conservative multi-branch `possible_cwds` tracking across `;`/`||`/`&`/`|`/`if..fi`/`while..done`, `Write|Edit|MultiEdit` file-target protection, dynamic variable fail-closed blocking, and depth 1–3 Git worktree protection.
- **`ship.py` (`agent-ship`)**: Full `<base>..HEAD` commit-history secret scanning with `<REDACTED>` output, 9 secret classes & 17 high-risk path patterns (including rename sources), untracked-file & non-`origin` blocking, and strict fail-closed `Stop(6)` when GitHub CI reports `no checks reported` after 3 retries.
- **Backup Continuity (`scripts/codex-storage/*`)**: `icloud-package.sh` verifies `isUploaded == true` via `icloud_status` before pruning older host archives or writing an `ok` receipt; `backup-restic.sh`, `maintenance.sh`, and `restore-drill.sh` enforce `--retry-lock "${BACKUP_RESTIC_RETRY_LOCK:-5m}"`.

## [2.0.0] - 2026-11-06

### Added
- Initial 3-tier multi-agent orchestration blueprint, dual external/internal storage engine, lossless cross-Mac migration (`migrate.py`), Restic + iCloud encrypted backup suite, and `orca-naming-guard` plugin.
