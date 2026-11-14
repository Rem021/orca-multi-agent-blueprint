# Changelog

All notable changes to `orca-multi-agent-blueprint` are documented in this file.

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
