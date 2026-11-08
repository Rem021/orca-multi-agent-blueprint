---
name: ast-grep
description: >-
  Structural AST code search, linting, and deterministic refactoring across Swift, C/C++, Objective-C, Rust, TypeScript, and Python using ast-grep (`ast-grep`), plus fast repo context packing (`repomix`), Xcode log filtering (`xcbeautify`), and standardized task execution (`just`).
---

# Structural Code Search & Fast CLI Toolchain (`~/.local/bin`)

Use these pre-installed Apple Silicon native binaries in `~/.local/bin` instead of slow BSD `grep -R`, `find`, or ad-hoc regex scripts:

## 1. `ast-grep` — Syntax-Aware Structural Search & Rewrite
- Search Swift / C / C++ / Rust / TS / Python by AST pattern (`$VAR` matches any single AST node, `$$$ARGS` matches zero or more nodes):
  ```bash
  ast-grep run -p '$OBJ.perform($$$ARGS)' -l swift /Volumes/AI_Workspace/AI/Projects/<Project>
  ast-grep run -p 'ioctl($FD, $REQ, $$$REST)' -l c /Volumes/AI_Workspace/AI/Projects/CoreHardwareLab
  ```
- Preview structural rewrites safely (dry-run diff by default; add `-U` to apply):
  ```bash
  ast-grep run -p 'oldFunc($A, $B)' -r 'newFunc($A, options: $B)' -l swift
  ```

## 2. `rg` (ripgrep) & `fd` — Fast Ignore-Aware Search
- Automatically respects `.gitignore` and `.git/info/exclude` (skips heavy `work/` directories):
  ```bash
  rg -n "SimulatorCoordination" /Volumes/AI_Workspace/AI/Projects/Mac
  fd -e swift -e metal . /Volumes/AI_Workspace/AI/Projects/NovaCam
  ```

## 3. `xcbeautify` — Token-Compact Xcode Build & Test Output
- Always pipe `xcodebuild` output through `xcbeautify` so build logs shrink by ~90% while preserving all compiler errors, warnings, and failing tests:
  ```bash
  set -o pipefail && xcodebuild test ... 2>&1 | xcbeautify
  ```

## 4. `repomix` — Context Packing for Cross-Agent Review
- Pack a module or subdirectory into a compressed AI-ready summary (with `--compress` for Tree-sitter signature extraction):
  ```bash
  repomix --include "src/**/*.swift" --compress --output /tmp/module-context.xml
  ```

## 5. `just` — Deterministic Project Recipes
- Check `just --list` in your workspace root (e.g., `just verify-volume`, `just doctor`, `just slim-status`, `just sim-leases`, `just handoff`).

## 6. `scc` — Instant Codebase Complexity & SLOC Metrics
- Quickly audit language breakdown, lines of code, and cyclomatic complexity before refactoring:
  ```bash
  scc /Volumes/AI_Workspace/AI/Projects/NovaCam
  ```
