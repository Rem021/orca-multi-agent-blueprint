---
name: orca-workspace
description: >-
  Mac workspace ops (`deploy.sh`, `doctor.py`), 4-zone TTL cleanup (`agent-governor`),
  disposable test VMs/simulators (`agent-test-device`, 清理闲置测试虚拟机), and
  `xcbeautify --quiet` / `ast-grep`.
---

# `orca-workspace` — Workspace Ops, Cleanup & Test Devices

## 1. Environment Health & Sync (`orca-multi-agent-blueprint`)

```bash
python3 ~/AI-Workspace/AI/Projects/Mac/outputs/orca-multi-agent-blueprint/scripts/doctor.py --summary
~/AI-Workspace/AI/Projects/Mac/outputs/orca-multi-agent-blueprint/deploy.sh --sync-profiles
~/AI-Workspace/AI/Projects/Mac/outputs/orca-multi-agent-blueprint/deploy.sh --apply
```

## 2. File Governance & Reversible Cleanup (`agent-governor`)

```bash
SCRATCH_DIR="$(agent-governor scratch --topic <topic>)"   # 3d TTL
TEST_DIR="$(agent-governor test-dir --topic <topic>)"      # 7d TTL
agent-governor audit                                       # read-only parity & clutter audit
agent-governor gc                                          # dry-run preview
agent-governor gc --skip-simulators --apply                # stage eligible files to Trash
```

Never `rm -rf`; `gc` skips active PIDs, open `lsof +D` handles, `.keep`/`.pinned`, `work/retained/`, and tracked Git files.

## 3. Disposable VMs/Simulators & Reclaiming Leftovers (`清理闲置测试虚拟机`)

```sh
agent-test-device simulator --type com.apple.CoreSimulator.SimDeviceType.iPhone-18-Pro -- ./test-ios
agent-test-device qemu qemu-system-aarch64 -M virt -display none -drive file=/path/to/base.qcow2,format=qcow2 -- ./test-vm
```

Each invocation owns one temporary instance (`AGENT_TEST_UDID` / `AGENT_TEST_QMP`) and cleans up only its own instance on exit. Preserve shared/permanent devices, base images, and test reports.

**Reclaiming leftover test instances** (count, temporary name, or idle state alone never proves completion — always match creation record + verify no active owner/lease; list uncertain candidates for review):
- **iOS simulators**: inspect `ephemeral_simulators` from `agent-governor gc --dry-run --json` (exclude protected primary/secondary UDIDs) -> claim lease & recheck owner -> `xcrun simctl shutdown "$udid"` -> `xcrun simctl delete "$udid"` -> release lease.
- **QEMU**: match private QMP socket & runtime dir to launch record -> stop via QMP -> verify exit & no open handles -> remove disposable dir/disks.
- **Other VM managers**: match created instance ID -> stop & wait for shutdown -> delete instance + private test disk -> re-list inventory and report removed/skipped IDs.

## 4. High-Leverage Native CLI Patterns

- **Quiet Xcode logs (~90% smaller, errors/warnings only)**: `set -o pipefail && xcodebuild test ... 2>&1 | xcbeautify --quiet`
- **Structural AST search/rewrite**: `ast-grep run -p '$OBJ.perform($$$ARGS)' -l swift .` (add `-r '...' -U` to rewrite)
