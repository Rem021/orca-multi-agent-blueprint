---
name: agent-slim
description: Universal AI Agent, Xcode & Multi-Stack Workspace Deep Cleaner. Use when the user asks to clean up bloated projects, free disk space across AI agent worktrees (Claude Code, OpenAI Codex, Cursor, Windsurf, Cline, Roo Code, Aider, OpenHands, Devin, Orca, Antigravity), remove ghost/prohibited (🚫) app icons from macOS Launchpad/Applications (`lsregister`), clean Xcode system caches (`DerivedData`, `CoreSimulator`, `DeviceSupport`, `XCTestDevices`), or purge build caches, compiler intermediates, GPU/frame dumps, iterative VM/firmware `.cpio` images, test-run backups, and stale release builds without losing Git state, uncommitted code, active tasks, referenced assets, or AI agent memory/rules.
---

# `agent-slim` — Universal AI Agent, Xcode & Multi-Stack Workspace Deep Cleaner

AI coding agents frequently spawn parallel git worktrees, run isolated compiler/test matrices, dump intermediate debug probes, and accumulate hundreds of gigabytes of hidden build caches (`target/`, `DerivedData/`, `Debug-iphonesimulator/`, `.saved-application`, `.next/`, `wandb/`, `Library/`, `.gradle/`, `.terraform/`, iterative `.cpio` rootfs images) — while also polluting macOS `LaunchServices` (`lsregister`) and Launchpad with broken `🚫` build-artifact `.app` icons.

`agent-slim` (`v2.3.0`) is a **zero-dependency, 5-gate safety disk space reclaimer and Xcode/macOS hygiene engine** designed for **any developer's AI Agent projects** across **7 major technology ecosystems**.

---

## 1. Five-Stage Safety Pipeline (Iron Rules)

Before deleting a single byte, `agent-slim` enforces five non-negotiable protection gates:

1. **Gate 01 — Active Process (`ps aux`) & Modification-Age Lock (`--min-age-hours`)**:
   - Inspects `ps -axo command` to detect any file, `.cpio` image, `.app` bundle, or directory actively used by running processes (`qemu`, `xcodebuild`, `clang`, `python3`, etc.) and automatically skips them.
   - Skips any build directory or cache modified within the last `N` hours so active background compilations or test runs are never interrupted.
2. **Gate 02 — VCS & Uncommitted Work Protection**:
   - Never modifies or deletes `.git`, `.svn`, `.hg`, `.jj`, tracked source files, or uncommitted working tree edits.
3. **Gate 03 — Universal AI Agent Memory, Rules & Report Docs Protection**:
   - Automatically protects agent state, skills, rules, and handoff documentation across **all mainstream AI coding tools**:
     - **Directories**: `.agents/`, `.claude/`, `.codex/`, `.gemini/`, `.cursor/`, `.windsurf/`, `.cline/`, `.roo/`, `.continue/`, `.aider/`, `.openhands/`, `.devin/`, `.orca/`, `.antigravity/`, `docs/`, `research_notes/`, `specs/`, `adr/`, `memory/`, `memories/`, `prompts/`, `skills/`, `plans/`, `fixtures/`, `testdata/`, `__snapshots__/`, `migrations/`.
     - **Report & Output Traversal**: Traverses `reports/`, `outputs/`, and `work/` to purge hidden binary build caches (`DerivedData`, `derived-simulator`, `.xcresult`, `.dSYM`, `*UITests-Runner.app`, `*.saved-application`), while strictly preserving all markdown, JSON, YAML, CSV, HTML, PDF, script, and image files inside them.
     - **Files**: `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `CURSOR.md`, `WINDSURF.md`, `CODEX.md`, `SKILL.md`, `MEMORY.md`, `PROGRESS.md`, `HANDOFF.md`, `STATUS.md`, `PLAN.md`, `TODO.md`, `RULES.md`, `.cursorrules`, `.windsurfrules`, `.clinerules`, `.roorules`, `.continuerules`, `current.json`, `.metadata_never_index`, and all package lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lockb`, `Cargo.lock`, `uv.lock`, `poetry.lock`, `Podfile.lock`, `Package.resolved`).
4. **Gate 04 — Context-Verified Build Matching & Script Reference Graph**:
   - Ambiguous directory names (`target/`, `build/`, `.build/`, `Library/`, `Temp/`, `Intermediate/`, `node_modules/`) are verified against sibling project manifests (`Cargo.toml`, `Package.swift`, `*.xcodeproj`, `build.gradle`, `Assets/` + `ProjectSettings/`, `*.uproject`, `package.json`) before cleanup.
   - Parses `.fcpxml`, `.json` (`current.json`), `.py`, and `.sh` files so referenced external media, baseline `--source` `.cpio` images, and active UUID installations are never orphaned.
   - Automatically protects AI model weights (`.safetensors`, `.gguf`, `.ckpt`, `.onnx`, `.pt`, `.pth`, `.h5`, `.tflite`, `.mlmodel`).
5. **Gate 05 — Multi-Pattern Version, UUID-Pointer & Iterative Run Retention**:
   - **Semantic Releases**: Retains the newest `N` versioned release builds (`--keep-versions 2`) while purging superseded historical builds.
   - **Pointer-Governed UUID Pools**: Detects installation directories governed by `current.json` (e.g., `Installations/<UUID>`), preserving the active UUID + newest `N` transactions while reclaiming hundreds of obsolete UUID backups.
   - **Iterative VM/Firmware Images**: Retains the newest `N` numbered `.cpio`/`.img`/`.fdat` iterations + any script/process-referenced baseline image while purging hundreds of gigabytes of intermediate trial dumps.
   - **Iterative Test-Run Backups**: Preserves all `.json`/`.manifest`/`.py` receipts across every test run (`runs/*-r1..rN`), retains full binary state for the newest `N` runs, and purges redundant multi-GB `before/backup` and `recovery/preserved-current` dumps from older runs.

---

## 2. Supported Technology Stacks (7 Ecosystems Out-of-the-Box)

| Ecosystem | Reclaimable Caches & Artifacts Detected Automatically |
| :--- | :--- |
| **1. Web / JS / TS / Full-Stack** | `.next`, `.nuxt`, `.output`, `.svelte-kit`, `.astro`, `.turbo`, `.parcel-cache`, `.vite`, `.angular`, `.docusaurus`, `.vercel`, `.netlify`, `.wrangler`, `storybook-static`, `playwright-report`, `test-results`, `.nyc_output`, `.eslintcache`, optional `node_modules` (`--include-node-modules`) |
| **2. Python / AI / ML / Data Science** | `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.pytype`, `.hypothesis`, `.tox`, `.nox`, `.ipynb_checkpoints`, `.gradio`, `lightning_logs`, `multirun` (Hydra), `wandb/` (local run caches), `.pyc`/`.pyo` *(while protecting `.safetensors`, `.gguf`, `.pt`, `.ckpt`, `.onnx`)* |
| **3. Systems / Rust / Go / C / C++ / Zig / VM Labs** | Cargo `target/` (verified by `Cargo.toml`), `CMakeFiles`, `cmake-build-*`, `bazel-*`, `.zig-cache`, `zig-out`, `ccache`/`sccache`, `.o`, `.obj`, `.lo`, `.dia`, `.pch`, `.gch`, `.core`/`.dmp` crash dumps, `.profraw`/`.profdata`, superseded iterative `.cpio`/`.img`/`.fdat` rootfs images |
| **4. Apple / Xcode / Mobile & Desktop Native** | Xcode `DerivedData`, `DerivedData-*`, `derived-simulator*`, `xcode-install*`, `xcode-release*`, `Intermediates.noindex`, `Debug/Release-iphoneos/iphonesimulator`, `*UITests-Runner.app`, `*Tests-Runner.app`, `*.saved-application`, `*.saved-service`, `*.app.saved`, detached build `.app` bundles, SwiftPM `.build`, `ModuleCache.noindex`, `Index.noindex`, `.xcresult`, `.dSYM`, `.trace`, system-level Xcode/Simulator caches (`--clean-xcode`), `DeviceSupport` symbols (`--clean-device-support`), macOS `LaunchServices` ghost-app unregistration (`--clean-launchservices`), Android `.gradle`, `.kotlin`, `.cxx`, Gradle/Flutter `build/`, `.dart_tool`, React Native `.expo` |
| **5. Game Engines** | **Unity** (`Library/`, `Temp/`, `Obj/`, `Logs/` verified by `Assets/` + `ProjectSettings/`), **Unreal Engine** (`Intermediate/`, `DerivedDataCache/`, `Saved/Logs`, `Saved/Crashes` verified by `*.uproject`), **Godot** (`.godot/`) |
| **6. DevOps / Cloud / IaC** | Terraform `.terraform`, `.terragrunt-cache`, Serverless `.serverless`, AWS SAM `.aws-sam` |
| **7. Media / Creative / Graphics** | Final Cut Pro `.fcpbundle` (`Render Files`, `Transcoded Media`, `Analysis Files`), Blender `.blend1`/`.blend2`, uncompressed GPU framebuffer dumps (`.rgba16f`, `.rgba32f`, debug `.raw`), and SHA-256 content-deduplicated scratch copies (`--dedup-scratch-mb`) |

---

## 3. Standard Agent Workflow

### Step 1: Safe Dry-Run Audit (Default)
Always run `agent-slim` in dry-run mode first to inspect the categorized space breakdown and verify which safety guardrails triggered:

```bash
python3 ~/.agents/skills/agent-slim/scripts/agent_slim.py /path/to/workspace --clean-xcode --clean-launchservices
```

### Step 2: Execute Cleanup & Reclaim Space
Once the dry-run report is confirmed, pass `--execute` (or use the `clean` subcommand) to permanently reclaim disk space and unregister ghost apps from macOS LaunchServices:

```bash
python3 ~/.agents/skills/agent-slim/scripts/agent_slim.py /path/to/workspace --clean-xcode --clean-launchservices --shield-spotlight --execute
```

### Step 3: Advanced Multi-Worktree & Full-System Xcode Cleanup
For sweeping an entire parent directory of AI agent worktrees plus Xcode system caches and `DeviceSupport`:

```bash
python3 ~/.agents/skills/agent-slim/scripts/agent_slim.py ~/Projects \
  --include-node-modules \
  --min-age-hours 2 \
  --keep-versions 2 \
  --dedup-scratch-mb 25 \
  --clean-xcode \
  --clean-device-support \
  --clean-orphans \
  --clean-launchservices \
  --shield-spotlight \
  --report-json /tmp/agent-slim-audit.json \
  --execute
```

---

## 4. CLI Reference

| Flag | Default | Description |
| :--- | :--- | :--- |
| `paths...` / `--root PATH` | *(Required unless `--clean-xcode`)* | Optional `scan`/`clean` subcommand followed by one or more workspace directories to scan |
| `--execute` | `False` (Dry-Run) | Perform permanent file/directory deletion to reclaim disk space immediately |
| `--trash` | `False` | Stage items reversibly into system Trash (`batch-<ts>/`) with a full `manifest.json` provenance receipt |
| `--include-node-modules` | `False` | Also remove reinstallable `node_modules/` (verified by sibling `package.json`) |
| `--keep-versions N` / `--keep-latest-release N` | `2` | Retain top `N` newest versioned releases, UUID installations, iterative `.cpio` builds, and test runs |
| `--min-age-hours H` | `0.0` | Skip any directory/file modified within the last `H` hours (protects active builds) |
| `--dedup-scratch-mb MB` | `25.0` | Fast SHA-256 deduplication of identical large files (`>= MB`) inside scratch/temp/test folders (`0` to disable) |
| `--clean-xcode` | `False` | Clean `~/Library/Developer/Xcode/DerivedData`, `CoreSimulator` caches/clones/`coresymbolicationd`, `XCTestDevices`, SwiftPM caches, and unavailable simulators |
| `--clean-device-support` | `False` | When used with `--clean-xcode`, also clean `~/Library/Developer/Xcode/*OS DeviceSupport` symbol caches |
| `--clean-agent-clutter` | `False` | Sweep empty untracked repo dotdirs (`.agents`/`.claude`/`.gemini`), empty `Worktrees`/`.orca-preparing`/`TaskHistory` shells, accidental dummy `npm init` manifests in native repos, unused-IDE ghost dotdirs (`~/.cursor`/`~/.windsurf`), orphaned `~/.claude/projects/<slug>` caches (preserving `memory/`), stale `.bak`/`.tmp` clutter, and audit broken local MCP binaries |
| `--clean-orphans` | `False` | Deep-scan `~/Library` & `/Library` for uninstalled app leftovers, orphaned `Containers`/`Group Containers` (TeamID verified), broken `LaunchAgents`, updater staging caches, and superseded agent runtimes |
| `--clean-launchservices` | `False` | Unregister ghost, deleted, and build-artifact `.app` bundles from macOS `LaunchServices` (`lsregister -u` & `-gc`) |
| `--shield-spotlight` | `False` | Create `.metadata_never_index` markers in transient build/worktree directories |
| `-- clean-simulators` | `False` | Run `xcrun simctl delete unavailable` on macOS to remove orphaned iOS Simulator runtimes |
| `--report-json PATH` / `--json` | `None` | Export a detailed JSON audit report of all candidates, sizes, and protected items |

---

## 5. Open-Source Rule Attributions & Credits

`agent-slim` v2.3.0 synthesizes and extends battle-tested orphan detection and developer cleanup rules from the open-source community:

- **[`alienator88/Pearcleaner`](https://github.com/alienator88/Pearcleaner)** (`Locations.swift` & Orphaned Files Scanner): Target directory taxonomy across `~/Library` and `/Library` (`Application Support`, `Containers`, `Group Containers`, `Saved Application State`, `HTTPStorages`, `Preferences`, `LaunchAgents`).
- **[`tw93/Mole`](https://github.com/tw93/Mole)** (`SECURITY_AUDIT.md`, `clean_app_caches`, `clean_user_core`): Strict Bundle Identifier attribution over fuzzy name guessing, macOS App Sandbox / TCC (`containermanagerd` / `com.apple.provenance`) boundary guards, shared `Group Containers` protection, and Electron/desktop `*-updater` staging cleanup.
- **[`momenbasel/PureMac`](https://github.com/momenbasel/PureMac)**, **[`slow-coding/fullcleaner`](https://github.com/slow-coding/fullcleaner)** & **[`Alyetama/Sweep`](https://github.com/Alyetama/Sweep)**: Multi-signal identity inventory combining `LaunchServices` (`lsregister -dump`), `pkgutil --pkgs` installer receipts, 10-character Apple Developer `TeamID` prefix verification (`<TEAMID>.<suite>`), and `LaunchAgents` `Program` / `ProgramArguments` binary existence checks.
- **[`jithin-sabu/purge-app`](https://github.com/jithin-sabu/purge-app)** & **Homebrew Cask `zap` Stanzas**: Deterministic app-to-support-directory verification (e.g., per-product Adobe suite verification that cleans uninstalled `Acrobat`/`Illustrator` leftovers while preserving `CameraRaw` for installed `Adobe DNG Converter`).
- **[`tbillington/kondo`](https://github.com/tbillington/kondo)**, **[`voidcosmos/npkill`](https://github.com/voidcosmos/npkill)**, **[`mac-cleanup/mac-cleanup-py`](https://github.com/mac-cleanup/mac-cleanup-py)** & **[`brege/dil`](https://github.com/brege/dil)**: Sibling-manifest anchor verification (`Cargo.toml`, `Package.swift`, `pom.xml`, `build.gradle`, `CMakeLists.txt`, `*.uproject`) before pruning ambiguous `target/` or `build/` directories, plus global toolchain cache purging (`pip`, `pnpm`, `npm/_cacache`).

