---
name: orca-workspace
description: >-
  Operate the multi-agent Mac workspace set up by orca-multi-agent-blueprint: health check and
  profile sync (deploy.sh, doctor.py), scratch/test folders and reversible TTL cleanup
  (agent-governor), disposable iOS simulators and QEMU VMs (agent-test-device), and quiet Xcode
  logs / structural search (xcbeautify, ast-grep). Use for workspace cleanup, leftover test
  devices, "清理工作区 / 清理闲置测试虚拟机", or when a doctor report needs action.
---

# orca-workspace

Blueprint checkout: `{{BLUEPRINT_DIR}}`

## Health and sync

```bash
python3 "{{BLUEPRINT_DIR}}/scripts/doctor.py" --summary   # exit 0 healthy · 1 attention · 2 check failed
"{{BLUEPRINT_DIR}}/deploy.sh" --sync-profiles             # skills, MCP servers, rules, hooks → every agent
"{{BLUEPRINT_DIR}}/deploy.sh" --apply                     # re-deploy from config.env
```

Report `summary.attention` word for word; do not claim success while it lists items.

## Folders and cleanup (`agent-governor`)

```bash
SCRATCH_DIR="$(agent-governor scratch --topic <topic>)"   # work/_scratch, 3-day TTL
TEST_DIR="$(agent-governor test-dir --topic <topic>)"     # work/_tests, 7-day TTL
agent-governor audit                                      # read-only rules/skills/clutter audit
agent-governor gc                                         # dry run: what would move to the Trash
agent-governor gc --skip-simulators --apply               # move eligible items to the Trash
```

`gc` never deletes: it moves items to the Trash and skips anything tracked by Git, open by a process,
marked `.keep`/`.pinned`, or under `work/retained/`.

## Disposable test devices (`agent-test-device`)

```bash
agent-test-device simulator --type com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro -- ./test-ios
agent-test-device qemu qemu-system-aarch64 -M virt -display none \
  -drive file=/path/to/base.qcow2,format=qcow2 -- ./test-vm
```

Each run owns one instance (`AGENT_TEST_UDID`, `AGENT_TEST_QMP`) and removes only that instance on exit.

Reclaiming leftovers: a temporary name, a count or an idle state never proves an instance is finished.
Match it to its creation record and confirm no active owner or lease first; list uncertain ones for the user.

- **iOS simulators**: take candidates from `ephemeral_simulators` in `agent-governor gc --json` (the shared
  primary/secondary UDIDs are excluded) → claim the lease and re-check the owner → `xcrun simctl shutdown`
  → `xcrun simctl delete` → release the lease.
- **QEMU**: match the QMP socket and runtime folder to the launch record → stop through QMP → confirm the
  process exited and nothing holds the disks → remove the disposable folder.
- **Other VM managers**: match the instance ID you created → stop and wait → delete the instance and its
  private disk → list again and report what was removed or skipped.

## CLI patterns

- Quiet Xcode output (errors and warnings only): `set -o pipefail && xcodebuild test … 2>&1 | xcbeautify --quiet`
- Structural search / rewrite: `ast-grep run -p '$OBJ.perform($$$ARGS)' -l swift .` (add `-r '…' -U` to rewrite)
