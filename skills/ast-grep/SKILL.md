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
  ast-grep run -p '$OBJ.perform($$$ARGS)' -l swift ~/AI-Workspace/AI/Projects/<Project>
  ast-grep run -p 'ioctl($FD, $REQ, $$$REST)' -l c .
  ```
- Preview structural rewrites safely (dry-run diff by default; add `-U` to apply):
  ```bash
  ast-grep run -p 'oldFunc($A, $B)' -r 'newFunc($A, options: $B)' -l swift
  ```

## 2. `rg` (ripgrep) & `fd` — Fast Ignore-Aware Search
- Automatically respects `.gitignore` and `.git/info/exclude` (skips heavy `work/` directories):
  ```bash
  rg -n "Pattern" .
  fd -e swift -e metal .
  ```

## 3. `xcbeautify` — Token-Compact Xcode Build & Test Output
- Pipe `xcodebuild` output through `xcbeautify` to shrink build logs by ~90% while preserving compiler errors, warnings, and failing tests:
  ```bash
  set -o pipefail && xcodebuild test ... 2>&1 | xcbeautify
  ```

## 4. `repomix` — Context Packing for Cross-Agent Review
- Pack a module or subdirectory into a compressed AI-ready summary (`--compress` extracts Tree-sitter signatures):
  ```bash
  repomix --include "src/**/*.swift" --compress --output /tmp/module-context.xml
  ```

## 5. `just` & `scc` — Task Recipes & Codebase Metrics
- Run `just --list` in a repo root to inspect available recipes (`doctor`, `slim`, `sync-profiles`, `test`).
- Run `scc .` to audit language breakdown, lines of code, and cyclomatic complexity before refactoring.
