# Contributing

Thanks for helping. Small, focused pull requests are easiest to review.

## Before you start

- Open an issue first for anything larger than a bug fix, so we can agree on the approach.
- Everything must keep running on the system Python of macOS (3.9) with the standard library only.
- Shell scripts are zsh (`#!/usr/bin/env zsh` or `#!/bin/zsh`).

## Develop

```bash
git clone https://github.com/Rem021/orca-multi-agent-blueprint.git
cd orca-multi-agent-blueprint
./deploy.sh --dry-run                     # preview; writes nothing
python3 -B -m unittest discover -s tests  # or: just test
```

The test suites run in temporary directories; they never touch your agents, Keychain or backups.

## Pull requests

- Add or update a test for every behaviour change, especially in `scripts/guard.py`.
- Keep personal paths, machine names, UDIDs, account names and project names out of code, tests and docs.
- Update `README.md` and `README.zh-CN.md` together when user-facing behaviour changes, and add a line to
  `CHANGELOG.md` under *Unreleased*.
- Security problems go through [SECURITY.md](SECURITY.md), not a public issue.
