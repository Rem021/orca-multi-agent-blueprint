# Security policy

## Reporting a vulnerability

Please report security problems privately through
[GitHub private vulnerability reporting](https://github.com/Rem021/orca-multi-agent-blueprint/security/advisories/new).
Do not open a public issue for them. You can expect a first reply within a week.

Useful details: the affected script and version (`git rev-parse --short HEAD`), the macOS version, the
exact input or command, and what happened compared with what you expected.

## Scope

In scope:

- A command that `agent-guard` claims to block (see the README's guardrail table) but lets through.
- Secrets written with wrong permissions, printed to logs, or left behind in temporary files.
- Shell or path injection in `deploy.sh`, `bootstrap.sh`, the backup jobs, or the generated restore launcher.
- A restore or cleanup step that deletes or overwrites data it should keep.

Out of scope (documented limits):

- `agent-guard` is a guardrail against accidents, not a sandbox. Commands hidden inside a script file,
  `python -c`, a remote shell or a downloaded installer are not analysed.
- The migration snapshot on the external drive contains SSH keys and exported Keychain passwords by design.
  Use an encrypted drive.
