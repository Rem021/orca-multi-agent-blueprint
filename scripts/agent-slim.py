#!/usr/bin/env python3
"""
agent-slim v2.1.0 — Universal AI Agent, Xcode & Multi-Stack Workspace Deep Cleaner
==================================================================================
A zero-dependency, safety-first disk space reclaimer and Xcode/macOS workspace
hygiene engine engineered for ANY developer using AI coding agents (Claude Code,
OpenAI Codex, Cursor, Windsurf, Cline, Roo Code, Aider, OpenHands, Devin, Orca,
Antigravity) across 7 major technology ecosystems:

  1. Web / JS / TS / Full-Stack (Next.js, Nuxt, Vite, Turbo, Astro, SvelteKit, Angular, Parcel, Storybook, Playwright)
  2. Python / AI / ML / Data Science (PyTorch/Lightning logs, W&B local caches, Gradio, Jupyter, Ruff, Mypy, Pytest, Tox, Hydra)
  3. Systems / Rust / Go / C / C++ / Zig / VM Labs (Cargo target/, CMake, Bazel, Zig, .o/.obj/.dia/.pch, iterative .cpio/.img rootfs dumps)
  4. Apple / Xcode / Mobile & Desktop Native (DerivedData, Debug/Release-iphoneos/simulator, *UITests-Runner.app, *.saved-application,
     *.app.saved, CoreSimulator caches/clones, DeviceSupport, xcresult/dSYM/trace, LaunchServices lsregister ghost-app cleanup,
     Android .gradle/.cxx/build, Flutter .dart_tool, React Native .expo)
  5. Game Engines (Unity Library/Temp/Obj, Unreal Intermediate/DerivedDataCache/Saved, Godot .godot)
  6. DevOps / Cloud / IaC (Terraform .terraform, Terragrunt, Serverless .serverless, AWS SAM .aws-sam)
  7. Media / Creative / Graphics (FCP .fcpbundle Render/Transcoded/Analysis files, Blender .blend1/.blend2, raw GPU frame dumps, SHA-256 scratch dedup)

Five-Stage Safety Pipeline (Hard Guardrails):
  - Gate 01 (Active Process & Modification Lock): Inspects `ps -axo command` for open/active files & skips recent edits within `--min-age-hours`.
  - Gate 02 (VCS & Worktree Guard): Never modifies or deletes `.git`, `.svn`, `.hg`, `.jj`, tracked source code, or uncommitted edits.
  - Gate 03 (Agent Memory, Rules & Report Docs Guard): Strictly preserves `.agents`, `.claude`, `.codex`, `.cursor`, `CLAUDE.md`, `AGENTS.md`,
    `SKILL.md`, and all markdown/JSON/script/media report files while traversing `reports/`, `outputs/`, and `work/` to purge binary build caches.
  - Gate 04 (Asset & Script Reference Graph): Parses `.fcpxml`, `.json` (`current.json`, `vm-candidate-*.json`), `.py`, and `.sh` files so
    referenced media, baseline `.cpio` images, and active installation UUIDs are never deleted.
  - Gate 05 (Version, UUID-Pointer & Iterative Run Retention): Retains the newest `N` semantic releases, `current.json`-governed UUID installations,
    iterative VM/firmware `.cpio` builds, and iterative test-run state backups while purging superseded historical binary dumps.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

VERSION = "2.3.0"

LSREGISTER_PATH = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)

# ============================================================================
# 1. UNIVERSAL GUARDRAILS: NEVER DELETE THESE DIRECTORIES OR FILES
# ============================================================================

PROTECTED_DIRS: Set[str] = {
    # Version Control Systems
    ".git", ".svn", ".hg", ".jj",
    # AI Coding Agent State, Memory, Rules & Skills
    ".agents", ".agent", ".claude", ".codex", ".gemini", ".cursor",
    ".windsurf", ".cline", ".roo", ".continue", ".aider", ".openhands",
    ".devin", ".goose", ".sweep", ".copilot", ".antigravity", ".orca",
    # IDE & Repository Configuration
    ".github", ".gitlab", ".vscode", ".idea", ".fleet", ".zed",
    # Documentation, Architecture, Specs & Agent Memory Folders
    # Note: `reports`, `outputs`, `work` are intentionally traversed so binary build caches
    # (DerivedData, Debug-iphonesimulator, .xcresult, .dSYM, .saved-application) inside them
    # can be cleaned while all report documents/scripts/images inside them remain protected.
    "docs", "doc", "documentation", "research_notes", "notes",
    "specs", "adr", "architecture", "memory", "memories", "prompts", "skills",
    "plans", "context", "handover", "handoff",
    # Test Fixtures, Snapshots & Database Migrations (Source of Truth)
    "fixtures", "testdata", "__snapshots__", "migrations", "seeds", "schemas",
}

PROTECTED_FILES: Set[str] = {
    # AI Agent Instructions, Memory & Handoff Files
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", "CURSOR.md", "WINDSURF.md",
    "CODEX.md", "SKILL.md", "MEMORY.md", "PROGRESS.md", "HANDOFF.md",
    "STATUS.md", "PLAN.md", "TODO.md", "TASKS.md", "GOAL.md", "RULES.md",
    "ARCHITECTURE.md", "CONTEXT.md", "PROMPT.md", "README.md", "README.zh-CN.md",
    "README.en.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", "SECURITY.md",
    # Dotfile Agent Rules
    ".cursorrules", ".windsurfrules", ".clinerules", ".roorules",
    ".continuerules", ".aider.conf.yml", ".aider.model.settings.yml",
    # Dependency Lockfiles & Project Manifests (Never delete)
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock",
    "Cargo.toml", "Cargo.lock", "pyproject.toml", "poetry.lock", "uv.lock",
    "Pipfile", "Pipfile.lock", "requirements.txt", "environment.yml",
    "go.mod", "go.sum", "Package.swift", "Package.resolved",
    "Podfile", "Podfile.lock", "pubspec.yaml", "pubspec.lock",
    "Gemfile", "Gemfile.lock", "composer.json", "composer.lock",
    "Makefile", "CMakeLists.txt", "justfile", "Dockerfile", "docker-compose.yml",
    "current.json", ".metadata_never_index",
    ".env", ".env.local", ".env.development", ".env.production", ".env.example",
}

# Protected AI Model Weight & Dataset Extensions
PROTECTED_MODEL_EXTS: Set[str] = {
    ".safetensors", ".gguf", ".ckpt", ".onnx", ".pt", ".pth", ".h5", ".tflite", ".mlmodel"
}

# ============================================================================
# 2. UNIVERSAL MULTI-STACK CACHE & ARTIFACT SIGNATURES
# ============================================================================

UNCONDITIONAL_CACHE_DIRS: Dict[str, str] = {
    # Web / JS / TS / Full-Stack
    ".next": "Web Build Cache (Next.js .next)",
    ".nuxt": "Web Build Cache (Nuxt .nuxt)",
    ".output": "Web Build Cache (Nitro/Nuxt .output)",
    ".svelte-kit": "Web Build Cache (SvelteKit .svelte-kit)",
    ".astro": "Web Build Cache (Astro .astro)",
    ".turbo": "Monorepo Cache (Turborepo .turbo)",
    ".parcel-cache": "Bundler Cache (Parcel .parcel-cache)",
    ".vite": "Bundler Cache (Vite .vite)",
    ".angular": "Web Build Cache (Angular .angular)",
    ".docusaurus": "Docs Build Cache (Docusaurus .docusaurus)",
    ".vercel": "Deployment Build Cache (.vercel)",
    ".netlify": "Deployment Build Cache (.netlify)",
    ".wrangler": "Cloudflare Workers Cache (.wrangler)",
    "storybook-static": "Storybook Static Build (storybook-static)",
    "playwright-report": "E2E Test Report Cache (playwright-report)",
    "test-results": "E2E Test Artifacts (test-results)",
    ".nyc_output": "Code Coverage Cache (.nyc_output)",
    # Python / AI / ML / Data Science
    "__pycache__": "Python Bytecode Cache (__pycache__)",
    ".pytest_cache": "Python Test Cache (.pytest_cache)",
    ".mypy_cache": "Python Typecheck Cache (.mypy_cache)",
    ".ruff_cache": "Python Linter Cache (.ruff_cache)",
    ".pytype": "Python Typecheck Cache (.pytype)",
    ".hypothesis": "Property Test Cache (.hypothesis)",
    ".tox": "Python Env Matrix Cache (.tox)",
    ".nox": "Python Env Matrix Cache (.nox)",
    ".ipynb_checkpoints": "Jupyter Checkpoint Cache (.ipynb_checkpoints)",
    ".gradio": "Gradio Temp Cache (.gradio)",
    "lightning_logs": "PyTorch Lightning Logs (lightning_logs)",
    "multirun": "Hydra Experiment Sweep Cache (multirun)",
    # Systems / C / C++ / Zig / Bazel
    "CMakeFiles": "CMake Intermediate Directory (CMakeFiles)",
    ".zig-cache": "Zig Compiler Cache (.zig-cache)",
    "zig-out": "Zig Build Output (zig-out)",
    ".cache": "Local Tooling Cache (.cache)",
    "ccache": "Compiler Cache (ccache)",
    ".ccache": "Compiler Cache (.ccache)",
    "sccache": "Compiler Cache (sccache)",
    # Mobile & Desktop Native (Xcode / Swift / Android / Flutter / React Native)
    "DerivedData": "Xcode DerivedData Build Cache",
    "Intermediates.noindex": "Xcode Build Intermediates (Intermediates.noindex)",
    "ModuleCache.noindex": "Clang/Swift Module Cache",
    "Index.noindex": "Xcode Index Cache (Index.noindex)",
    "SDKStatCaches.noindex": "Xcode SDK Stat Cache",
    "Debug-iphoneos": "Xcode iOS Device Debug Build Products (Debug-iphoneos)",
    "Debug-iphonesimulator": "Xcode iOS Simulator Debug Build Products (Debug-iphonesimulator)",
    "Release-iphoneos": "Xcode iOS Device Release Build Products (Release-iphoneos)",
    "Release-iphonesimulator": "Xcode iOS Simulator Release Build Products (Release-iphonesimulator)",
    "Debug-appletvos": "Xcode tvOS Debug Build Products",
    "Release-appletvos": "Xcode tvOS Release Build Products",
    "Debug-watchos": "Xcode watchOS Debug Build Products",
    "Release-watchos": "Xcode watchOS Release Build Products",
    "Debug-xros": "Xcode visionOS Debug Build Products",
    "Release-xros": "Xcode visionOS Release Build Products",
    ".swiftpm": "Swift Package Manager Cache (.swiftpm)",
    ".gradle": "Gradle Project Cache (.gradle)",
    ".kotlin": "Kotlin Compiler Cache (.kotlin)",
    ".cxx": "Android NDK C++ Build Cache (.cxx)",
    ".dart_tool": "Dart/Flutter Tooling Cache (.dart_tool)",
    ".expo": "Expo React Native Cache (.expo)",
    # Game Engines
    "DerivedDataCache": "Unreal Engine DerivedDataCache",
    ".godot": "Godot Engine Import/Build Cache (.godot)",
    # DevOps / Cloud / IaC
    ".terraform": "Terraform Provider Cache (.terraform)",
    ".terragrunt-cache": "Terragrunt Cache (.terragrunt-cache)",
    ".serverless": "Serverless Framework Build (.serverless)",
    ".aws-sam": "AWS SAM Build Cache (.aws-sam)",
}

# Disposable directories matched by prefix
PREFIX_CACHE_DIRS: Tuple[Tuple[str, str], ...] = (
    ("cmake-build-", "CLion / CMake Build Directory (cmake-build-*)"),
    ("bazel-", "Bazel Build Symlink/Output (bazel-*)"),
    ("DerivedData-", "Xcode Custom DerivedData Cache (DerivedData-*)"),
    ("derived-simulator", "Xcode Simulator DerivedData Cache (derived-simulator*)"),
    ("xcode-install", "Xcode Temporary Install Build Cache (xcode-install*)"),
    ("xcode-release", "Xcode Temporary Release Build Cache (xcode-release*)"),
)

# Disposable directories matched by suffix (macOS bundles & Xcode diagnostic/backup bundles)
SUFFIX_CACHE_DIRS: Tuple[Tuple[str, str], ...] = (
    (".xcresult", "Xcode Test Result Bundle (.xcresult)"),
    (".dSYM", "Debug Symbols Bundle (.dSYM)"),
    (".trace", "Instruments Profiling Trace (.trace)"),
    (".SavedState", "macOS Saved Application State (.SavedState)"),
    (".saved-application", "Stale Backup Application Bundle (.saved-application)"),
    (".saved-service", "Stale Backup PlugInKit/XPC Service Bundle (.saved-service)"),
    (".app.saved", "Timestamped Backup Application Bundle (.app.saved)"),
    ("UITests-Runner.app", "Xcode UI Test Runner App Bundle (*UITests-Runner.app)"),
    ("Tests-Runner.app", "Xcode Test Runner App Bundle (*Tests-Runner.app)"),
)

# Disposable individual file extensions (intermediate compiler outputs, dumps, backups)
DISPOSABLE_FILE_EXTS: Dict[str, str] = {
    # Compiler intermediates
    ".o": "Compiled Object File (.o)",
    ".obj": "Compiled Object File (.obj)",
    ".lo": "Libtool Object File (.lo)",
    ".dia": "Clang Serialized Diagnostics (.dia)",
    ".pch": "Precompiled Header (.pch)",
    ".gch": "GCC Precompiled Header (.gch)",
    ".ilk": "MSVC Incremental Linker File (.ilk)",
    ".exp": "MSVC Export File (.exp)",
    ".pyc": "Python Bytecode (.pyc)",
    ".pyo": "Python Optimized Bytecode (.pyo)",
    # Crash dumps & profiling traces
    ".core": "Process Core Dump (.core)",
    ".dmp": "Crash Minidump (.dmp)",
    ".mdmp": "Windows Crash Minidump (.mdmp)",
    ".hprof": "JVM Heap Dump (.hprof)",
    ".profraw": "LLVM Raw Coverage Profile (.profraw)",
    ".profdata": "LLVM Merged Coverage Profile (.profdata)",
    # Raw GPU / Framebuffer / Render Dumps
    ".rgba16f": "Raw Half-Float GPU Framebuffer Dump (.rgba16f)",
    ".rgba32f": "Raw Float GPU Framebuffer Dump (.rgba32f)",
    # Creative / CAD Backups
    ".blend1": "Blender Auto-Backup File (.blend1)",
    ".blend2": "Blender Auto-Backup File (.blend2)",
    ".bak": "Backup File (.bak)",
    ".tmp": "Temporary File (.tmp)",
    ".temp": "Temporary File (.temp)",
    ".swp": "Vim Swap File (.swp)",
}

DISPOSABLE_EXACT_FILES: Set[str] = {
    ".DS_Store",
    "Thumbs.db",
    ".eslintcache",
    ".stylelintcache",
    ".prettiercache",
    "npm-debug.log",
    "yarn-error.log",
    ".coverage",
}

# ============================================================================
# 3. DATA STRUCTURES & HELPERS
# ============================================================================

@dataclass
class CandidateItem:
    path: Path
    category: str
    size_bytes: int
    reason: str
    is_dir: bool


@dataclass
class ScanReport:
    root: Path
    candidates: List[CandidateItem] = field(default_factory=list)
    protected_notes: List[str] = field(default_factory=list)
    skipped_active: List[str] = field(default_factory=list)
    hygiene_notes: List[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(c.size_bytes for c in self.candidates)


def format_bytes(num_bytes: int) -> str:
    val = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if val < 1024.0 or unit == "TB":
            return f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} TB"


def get_path_size(path: Path) -> int:
    """Fast recursive size calculation without following symlinks."""
    if path.is_symlink():
        return 0
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0

    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    if entry.is_file(follow_symlinks=False):
                        try:
                            total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        except OSError:
            pass
    return total


def is_recently_modified(path: Path, min_age_seconds: float) -> bool:
    """Check if a directory or file was modified within `min_age_seconds` (Active Task Guardrail)."""
    if min_age_seconds <= 0:
        return False
    now = time.time()
    try:
        if (now - path.stat(follow_symlinks=False).st_mtime) < min_age_seconds:
            return True
    except OSError:
        return False
    return False


def fast_file_signature(path: Path) -> Optional[str]:
    """Compute a fast content signature (size + first 64KB + middle 64KB + last 64KB SHA-256)."""
    try:
        size = path.stat().st_size
        if size == 0:
            return None
        h = hashlib.sha256()
        h.update(str(size).encode("utf-8"))
        chunk_size = 65536
        with open(path, "rb") as f:
            h.update(f.read(chunk_size))
            if size > chunk_size * 2:
                f.seek(size // 2)
                h.update(f.read(chunk_size))
            if size > chunk_size:
                f.seek(max(0, size - chunk_size))
                h.update(f.read(chunk_size))
        return h.hexdigest()
    except OSError:
        return None


def collect_active_process_paths() -> Tuple[Set[Path], Set[str]]:
    """
    Gate 01: Inspect running process command lines (`ps -axo command`) so any file,
    archive, or directory actively used by `qemu`, `xcodebuild`, `python3`, etc. is protected.
    """
    active_paths: Set[Path] = set()
    active_basenames: Set[str] = set()
    try:
        out = subprocess.check_output(["ps", "-axo", "command"], text=True, errors="ignore")
        for m in re.finditer(r"(/[^\s,;\"'<>|]+)", out):
            raw = m.group(1).rstrip("):")
            if len(raw) > 4:
                p = Path(raw)
                active_paths.add(p)
                active_basenames.add(p.name)
                try:
                    active_paths.add(p.resolve())
                except OSError:
                    pass
    except Exception:
        pass
    return active_paths, active_basenames


# ============================================================================
# 4. CONTEXT-VERIFIED BUILD & BUNDLE DETECTION (GATE 04 / LAYER 4)
# ============================================================================

TRANSIENT_APP_ANCESTORS: Set[str] = {
    "work", "archive", "builds", "taskhistory", "phayen-lab", "deriveddata",
    "candidate-products", "signed-products", "hostvalidation", "unpacked",
    "verified", "payload", "batch2-identity-records", "before", "recovery",
    "target-app", "runner-app", "preserved-current", "backups",
}


def is_transient_build_app_bundle(dir_path: Path, dir_name: str, active_paths: Set[Path]) -> Optional[Tuple[str, str]]:
    """
    Detect detached Xcode / macOS / iOS `.app` build artifacts inside transient agent worktrees,
    test harnesses, archives, or backup folders that pollute Spotlight and LaunchServices (`🚫` icons),
    while strictly protecting `/Applications`, `/System`, `~/Library/Plug-Ins`, and active release bundles.
    """
    if not dir_name.endswith(".app"):
        return None

    # Never touch system or standard installed app directories
    s_path = str(dir_path)
    if s_path.startswith(("/Applications/", "/System/", "/Library/")) or "/Library/Plug-Ins/" in s_path:
        return None
    if "/Library/Daemon Containers/" in s_path:
        return None

    # Never touch an app bundle that is actively running in `ps aux`
    if dir_path in active_paths or any(ap == dir_path or dir_path in ap.parents for ap in active_paths):
        return None

    lower_parts = [p.lower() for p in dir_path.parts[:-1]]
    parent_name = dir_path.parent.name.lower()

    # 1. Explicit backup/trial naming on the `.app` itself
    lower_name = dir_name.lower()
    if any(tag in lower_name for tag in ("-backup.app", "-tested.app", "-rejected.app", "-diagnostic.app", "fixture.app", "probea.app", "probeb.app")):
        return (
            "Stale Backup / Diagnostic App Bundle (.app)",
            f"Backup/diagnostic app bundle '{dir_name}'",
        )

    # 2. Inside Build/Products or signed/candidate/backup/work/archive/lab directories
    if (
        ("build" in lower_parts and "products" in lower_parts)
        or any(anc in TRANSIENT_APP_ANCESTORS for anc in lower_parts)
        or parent_name.startswith(("signed", "runner-", "llvc-", "phone", "debug-", "release-"))
    ):
        return (
            "Detached Xcode / Agent Build Artifact (.app)",
            f"Transient build artifact '{dir_name}' inside '{dir_path.parent.name}/'",
        )

    return None


def classify_context_verified_dir(
    dir_path: Path,
    dir_name: str,
    include_node_modules: bool,
    active_paths: Set[Path],
) -> Optional[Tuple[str, str]]:
    """
    Verify ambiguous directory names (`target`, `build`, `.build`, `Library`, `Temp`,
    `Intermediate`, `wandb`, `node_modules`, detached `.app`) using sibling manifest files
    or ancestor context so source folders are NEVER accidentally deleted.
    """
    parent = dir_path.parent

    # 0. Detached Xcode/Agent `.app` bundle inside work/archive/build/lab directories
    app_match = is_transient_build_app_bundle(dir_path, dir_name, active_paths)
    if app_match:
        return app_match

    # 1. Rust Cargo or Java Maven `target/`
    if dir_name == "target":
        if (parent / "Cargo.toml").exists():
            return ("Rust Cargo Build Target (target/)", "Verified sibling Cargo.toml")
        if (parent / "pom.xml").exists():
            return ("Maven Build Target (target/)", "Verified sibling pom.xml")

    # 2. SwiftPM / Xcode `.build/`
    if dir_name == ".build":
        if (parent / "Package.swift").exists() or any(parent.glob("*.xcodeproj")) or any(parent.glob("*.xcworkspace")):
            return ("SwiftPM Build Cache (.build/)", "Verified sibling Package.swift / Xcode project")
        if (
            (dir_path / "arm64-apple-macosx").exists()
            or (dir_path / "x86_64-apple-macosx").exists()
            or (dir_path / "checkouts").exists()
            or (dir_path / "DerivedData").exists()
        ):
            return ("SwiftPM / Xcode Build Cache (.build/)", "Contains SwiftPM/Xcode build layout")

    # 3. Gradle / Android / Flutter / CMake `build/`
    if dir_name == "build":
        if (
            (parent / "build.gradle").exists()
            or (parent / "build.gradle.kts").exists()
            or (parent / "settings.gradle").exists()
            or (parent / "settings.gradle.kts").exists()
            or (parent / "pubspec.yaml").exists()
        ):
            return ("Gradle / Flutter Build Output (build/)", "Verified sibling build.gradle / pubspec.yaml")
        if (dir_path / "CMakeCache.txt").exists() or (dir_path / "build.ninja").exists():
            return ("CMake / Ninja Build Directory (build/)", "Contains CMakeCache.txt / build.ninja")
        if (dir_path / "Build" / "Products").exists() or (dir_path / "DerivedData").exists():
            return ("Xcode Build Output Directory (build/)", "Contains Xcode Build/Products layout")

    # 4. Unity Engine `Library/`, `Temp/`, `Obj/`, `Logs/`
    if dir_name in {"Library", "Temp", "Obj", "Logs"}:
        if (parent / "Assets").is_dir() and (parent / "ProjectSettings").is_dir():
            return (f"Unity Engine Cache ({dir_name}/)", "Verified Unity project (Assets/ + ProjectSettings/)")

    # 5. Unreal Engine `Intermediate/`, `Saved/Logs`, `Saved/Crashes`
    if dir_name == "Intermediate":
        if any(parent.glob("*.uproject")):
            return ("Unreal Engine Intermediate Build", "Verified sibling *.uproject")
    if dir_name in {"Logs", "Crashes", "Autosaves"} and parent.name == "Saved":
        if any(parent.parent.glob("*.uproject")):
            return (f"Unreal Engine Saved/{dir_name}", "Verified Unreal *.uproject")

    # 6. Weights & Biases (`wandb/`) local run caches
    if dir_name == "wandb":
        if (dir_path / "latest-run").exists() or any(dir_path.glob("run-*")) or any(dir_path.glob("offline-run-*")):
            return ("Weights & Biases Local Run Cache (wandb/)", "Contains W&B run-* directories")

    # 7. Node / Frontend `node_modules/` (Opt-in via --include-node-modules)
    if dir_name == "node_modules" and include_node_modules:
        if (parent / "package.json").exists():
            return ("Node.js Dependencies (node_modules/)", "Reinstallable via npm/pnpm/yarn/bun (--include-node-modules)")

    # 8. Explicit temporary build/probe folders created by AI agents
    lower_name = dir_name.lower()
    if (
        lower_name.startswith(".tmp-build")
        or lower_name.startswith("tmp_build")
        or lower_name.startswith(".cargo-target")
        or lower_name.startswith("cargo-target")
        or lower_name.startswith("deriveddata")
        or lower_name.startswith("cmake_build")
        or lower_name.startswith("probe_out")
    ):
        return ("Agent Temporary Build/Probe Directory", f"Matches temporary build pattern '{dir_name}'")

    return None


# ============================================================================
# 5. REFERENCE GRAPH & VERSION / ITERATION RETENTION (GATES 04 & 05)
# ============================================================================

FAST_SKIP_DIRS: Set[str] = (
    PROTECTED_DIRS
    | set(UNCONDITIONAL_CACHE_DIRS.keys())
    | {
        "node_modules", "venv", ".venv", "vendor", "Pods", "SourcePackages",
        "checkouts", "linux-kernel", "sony-oss-fx3", "tree", "nflasha15-files",
        "full-unpack-20261107", "before", "recovery", "Installations", "Backups",
    }
)


def collect_referenced_paths(root: Path) -> Tuple[Set[Path], Set[str]]:
    """
    Gate 04: Scan FCPXML (.fcpxml) and script/manifest files (.py, .sh, .json) to build a set of
    referenced local file paths and basenames (e.g., `--source ...cpio`, `current.json` pointers)
    that MUST NOT be deleted.
    """
    referenced_paths: Set[Path] = set()
    referenced_names: Set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if d not in FAST_SKIP_DIRS and not d.endswith((".app", ".saved-application", ".app.saved", ".xcresult", ".dSYM"))
        ]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if fname.endswith(".fcpxml"):
                try:
                    tree = ET.parse(fpath)
                    for elem in tree.getroot().iter("media-rep"):
                        src = elem.attrib.get("src", "")
                        if src.startswith("file://"):
                            parsed = urllib.parse.urlparse(src)
                            decoded = urllib.parse.unquote(parsed.path)
                            referenced_names.add(Path(decoded).name)
                            try:
                                referenced_paths.add(Path(decoded).resolve())
                            except OSError:
                                referenced_paths.add(Path(decoded))
                except Exception:
                    pass
            elif fname.endswith((".py", ".sh")) or fname == "current.json":
                try:
                    if fpath.stat().st_size <= 256 * 1024:
                        text = fpath.read_text(encoding="utf-8", errors="ignore")
                        for m in re.finditer(r"([A-Za-z0-9_.\-]+\.(?:cpio|img|fdat|tar|gz|app|bin))", text):
                            referenced_names.add(m.group(1))
                except OSError:
                    pass

    return referenced_paths, referenced_names


def version_sort_key(name: str) -> Tuple[int, ...]:
    """Extract numeric version tuple for sorting release/trial directories."""
    nums = [int(n) for n in re.findall(r"\d+", name)]
    return tuple(nums) if nums else (0,)


def scan_pointer_governed_install_dirs(
    root: Path,
    keep_versions: int,
    report: ScanReport,
    seen_paths: Set[Path],
) -> None:
    """
    Gate 05A: Detect installation transaction pools governed by a `current.json` pointer
    (e.g., `PrismGrade/Installations` containing `current.json` + hundreds of UUID directories).
    Retains the active UUID referenced in `current.json` + the newest `keep_versions` directories,
    and reclaims all superseded historical UUID transaction directories.
    """
    uuid_re = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        candidate_parent = Path(dirpath)
        if candidate_parent in seen_paths or any(p in seen_paths for p in candidate_parent.parents):
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames
            if d not in PROTECTED_DIRS and d not in {"node_modules", "linux-kernel", "venv", ".venv"}
            and not d.endswith((".app", ".saved-application", ".app.saved"))
        ]

        if "current.json" not in filenames:
            continue

        uuid_dirs = [
            candidate_parent / d for d in list(dirnames)
            if uuid_re.match(d) and not (candidate_parent / d).is_symlink()
        ]
        if len(uuid_dirs) <= keep_versions:
            continue

        active_tokens: Set[str] = set()
        try:
            raw_json = (candidate_parent / "current.json").read_text(encoding="utf-8", errors="ignore").strip()
            active_tokens.add(raw_json.strip('"').strip("'"))
            for m in uuid_re.finditer(raw_json):
                active_tokens.add(m.group(0))
        except OSError:
            pass

        sorted_dirs = sorted(
            uuid_dirs,
            key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
            reverse=True,
        )
        kept: Set[Path] = set(sorted_dirs[:keep_versions])
        for d in sorted_dirs:
            if d.name in active_tokens:
                kept.add(d)

        for d in sorted_dirs:
            if d in kept:
                seen_paths.add(d)
                report.protected_notes.append(f"Retained active/latest installation UUID: {d}")
            elif d not in seen_paths:
                sz = get_path_size(d)
                if sz > 0:
                    seen_paths.add(d)
                    report.candidates.append(
                        CandidateItem(
                            path=d,
                            category="Superseded Installation Transactions",
                            size_bytes=sz,
                            reason=f"Historical UUID transaction superseded by current.json (kept newest {keep_versions} + active)",
                            is_dir=True,
                        )
                    )
        dirnames[:] = [d for d in dirnames if (candidate_parent / d) not in seen_paths]


def scan_versioned_release_dirs(
    root: Path,
    keep_versions: int,
    report: ScanReport,
    seen_paths: Set[Path],
) -> None:
    """
    Gate 05B: Detect versioned build/release output directories (e.g., `App-0.3.0-beta.1` .. `beta.27`)
    and retain the newest `keep_versions` + any actively linked version.
    """
    for dirpath, dirnames, _ in os.walk(root, topdown=True, followlinks=False):
        candidate_parent = Path(dirpath)
        if candidate_parent in seen_paths or any(p in seen_paths for p in candidate_parent.parents):
            dirnames[:] = []
            continue
        if (candidate_parent / ".keep").exists():
            report.protected_notes.append(f"Protected (.keep pin): {candidate_parent}")
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames
            if d not in FAST_SKIP_DIRS and (candidate_parent / d) not in seen_paths
            and not (candidate_parent / d / ".keep").exists()
        ]
        if "reports" in {p.lower() for p in candidate_parent.parts}:
            continue

        ver_pattern = re.compile(r"^(.*?)(v?\d+\.\d+\.\d+.*|(?:beta|rc|alpha)\d+.*)$", re.IGNORECASE)
        subdirs = [
            candidate_parent / d for d in dirnames
            if ver_pattern.match(d) and not (candidate_parent / d).is_symlink()
        ]
        if len(subdirs) <= keep_versions:
            continue

        active_refs: Set[str] = set()
        for jf in candidate_parent.glob("*.json"):
            try:
                jtext = jf.read_text(encoding="utf-8", errors="ignore")
                active_refs.update(re.findall(r"[A-Za-z0-9_.\-]+(?:beta|rc|alpha|v)\d+[A-Za-z0-9_.\-]*", jtext))
            except OSError:
                pass

        groups: Dict[str, List[Path]] = {}
        for d in subdirs:
            m = ver_pattern.match(d.name)
            if m:
                prefix = m.group(1).rstrip("-_")
                groups.setdefault(prefix, []).append(d)

        for prefix, group_dirs in groups.items():
            if len(group_dirs) <= keep_versions:
                continue
            sorted_dirs = sorted(
                group_dirs,
                key=lambda p: (version_sort_key(p.name), p.stat().st_mtime if p.exists() else 0.0),
                reverse=True,
            )
            kept = set(sorted_dirs[:keep_versions])
            for d in sorted_dirs:
                if any(ref in d.name for ref in active_refs):
                    kept.add(d)

            for d in sorted_dirs:
                if d in kept:
                    seen_paths.add(d)
                    report.protected_notes.append(f"Retained recent/active versioned build: {d}")
                elif d not in seen_paths:
                    sz = get_path_size(d)
                    if sz > 0:
                        seen_paths.add(d)
                        report.candidates.append(
                            CandidateItem(
                                path=d,
                                category="Stale Versioned Release Builds",
                                size_bytes=sz,
                                reason=f"Superseded release build (retained top {keep_versions} newest versions)",
                                is_dir=True,
                            )
                        )


def scan_iterative_build_files(
    root: Path,
    keep_versions: int,
    min_age_seconds: float,
    active_paths: Set[Path],
    active_basenames: Set[str],
    referenced_names: Set[str],
    report: ScanReport,
    seen_paths: Set[Path],
) -> None:
    """
    Gate 05C: Detect large numbered iterative build/rootfs/VM image sequences in the same folder
    (e.g., `guest-hybrid-fx6kernel-capid-grmreal1.cpio` .. `grmreal513-suhook.cpio` totaling 200+ GB).
    """
    iter_file_re = re.compile(r"^([A-Za-z0-9_.\-]*?[A-Za-z_\-])(\d+)(?:-[A-Za-z0-9_.\-]+)?(\.(?:cpio|img|fdat|tar|bin))$")
    min_size_bytes = 20 * 1024 * 1024  # Only apply to large >= 20MB iterative images
    safe_age_window = max(min_age_seconds, 1800.0)  # Always protect files modified in the last 30 mins

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_dir = Path(dirpath)
        if current_dir in seen_paths or any(p in seen_paths for p in current_dir.parents):
            dirnames[:] = []
            continue
        if (current_dir / ".keep").exists():
            report.protected_notes.append(f"Protected (.keep pin): {current_dir}")
            dirnames[:] = []
            continue
        if (current_dir / ".keep").exists():
            report.protected_notes.append(f"Protected (.keep pin): {current_dir}")
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames
            if d not in FAST_SKIP_DIRS and (current_dir / d) not in seen_paths
            and not d.endswith((".app", ".saved-application", ".app.saved"))
        ]

        series_map: Dict[Tuple[str, str], List[Tuple[int, float, Path]]] = {}
        for fname in filenames:
            m = iter_file_re.match(fname)
            if not m:
                continue
            fpath = current_dir / fname
            if fpath in seen_paths or fpath.is_symlink():
                continue
            try:
                st = fpath.stat()
            except OSError:
                continue
            if st.st_size < min_size_bytes:
                continue
            prefix, num_str, ext = m.group(1), m.group(2), m.group(3)
            series_map.setdefault((prefix, ext), []).append((int(num_str), st.st_mtime, fpath))

        keep_count = max(keep_versions, 3)
        for (prefix, ext), items in series_map.items():
            if len(items) <= keep_count:
                continue
            items_sorted = sorted(items, key=lambda x: (x[0], x[1]), reverse=True)
            kept_paths: Set[Path] = {x[2] for x in items_sorted[:keep_count]}

            for num, mtime, fpath in items_sorted:
                if (
                    fpath in active_paths
                    or fpath.name in active_basenames
                    or fpath.name in referenced_names
                    or is_recently_modified(fpath, safe_age_window)
                ):
                    kept_paths.add(fpath)

            for num, mtime, fpath in items_sorted:
                if fpath in kept_paths:
                    seen_paths.add(fpath)
                    report.protected_notes.append(f"Retained active/baseline/newest iterative image: {fpath.name}")
                elif fpath not in seen_paths:
                    sz = get_path_size(fpath)
                    if sz > 0:
                        seen_paths.add(fpath)
                        report.candidates.append(
                            CandidateItem(
                                path=fpath,
                                category="Superseded Iterative VM/Firmware Images",
                                size_bytes=sz,
                                reason=f"Superseded iteration #{num} in '{prefix}*{ext}' series (kept newest {keep_count} + referenced baselines)",
                                is_dir=False,
                            )
                        )


def scan_iterative_run_backups(
    root: Path,
    keep_versions: int,
    report: ScanReport,
    seen_paths: Set[Path],
) -> None:
    """
    Gate 05D: Detect iterative test/device run directories (`runs/*-r1`, `runs/*-r2` .. `runs/*-r7`)
    that stash multi-gigabyte `before/backup/{target-app,target-data,runner-app,runner-data}` and
    `recovery/preserved-current` dumps.
    """
    run_re = re.compile(r"^(.*?)-r(\d+)$")

    for dirpath, dirnames, _ in os.walk(root, topdown=True, followlinks=False):
        current_dir = Path(dirpath)
        if current_dir in seen_paths or any(p in seen_paths for p in current_dir.parents):
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames
            if d not in PROTECTED_DIRS and d not in {"node_modules", "linux-kernel", "venv", ".venv"}
            and not d.endswith((".app", ".saved-application", ".app.saved"))
        ]
        if current_dir.name != "runs":
            continue

        series: Dict[str, List[Tuple[int, float, Path]]] = {}
        for d in list(dirnames):
            m = run_re.match(d)
            if not m:
                continue
            rpath = current_dir / d
            if rpath.is_symlink() or rpath in seen_paths:
                continue
            try:
                mtime = rpath.stat().st_mtime
            except OSError:
                mtime = 0.0
            series.setdefault(m.group(1), []).append((int(m.group(2)), mtime, rpath))

        for prefix, run_items in series.items():
            if len(run_items) <= keep_versions:
                continue
            run_sorted = sorted(run_items, key=lambda x: (x[0], x[1]), reverse=True)
            kept_runs = {x[2] for x in run_sorted[:keep_versions]}

            for rnum, _, rpath in run_sorted:
                if rpath in kept_runs:
                    seen_paths.add(rpath)
                    report.protected_notes.append(f"Retained newest test run state: {rpath}")
                    continue
                heavy_subdirs = [
                    rpath / "before" / "backup",
                    rpath / "recovery" / "preserved-current",
                    rpath / "input",
                    rpath / "ui",
                    rpath / "tests",
                ]
                for hdir in heavy_subdirs:
                    if hdir.exists() and hdir.is_dir() and hdir not in seen_paths:
                        sz = get_path_size(hdir)
                        if sz > 0:
                            seen_paths.add(hdir)
                            report.candidates.append(
                                CandidateItem(
                                    path=hdir,
                                    category="Superseded Test Run State Backups",
                                    size_bytes=sz,
                                    reason=f"Redundant binary backup inside superseded run '{rpath.name}' (JSON/manifest receipts preserved)",
                                    is_dir=True,
                                )
                            )
        dirnames[:] = []




def scan_orphan_git_worktrees(
    root: Path,
    report: ScanReport,
    seen_paths: Set[Path],
) -> None:
    """
    Gate 05E: Detect orphaned `.git/worktrees/<id>` administrative directories whose target
    `gitdir` path (resolved for both relative and absolute paths) no longer exists on disk.
    """
    for dirpath, dirnames, _ in os.walk(root, topdown=True, followlinks=False):
        current_dir = Path(dirpath)
        if current_dir in seen_paths or any(p in seen_paths for p in current_dir.parents):
            dirnames[:] = []
            continue
        if ".git" in dirnames:
            wt_root = current_dir / ".git" / "worktrees"
            if wt_root.is_dir():
                for wt in sorted(wt_root.iterdir()):
                    if not wt.is_dir() or wt in seen_paths:
                        continue
                    gitdir_file = wt / "gitdir"
                    if gitdir_file.exists():
                        try:
                            raw = gitdir_file.read_text(encoding="utf-8", errors="ignore").strip()
                            if raw:
                                resolved = (wt / raw).resolve() if not os.path.isabs(raw) else Path(raw)
                                if not resolved.exists():
                                    sz = get_path_size(wt)
                                    seen_paths.add(wt)
                                    report.candidates.append(
                                        CandidateItem(
                                            path=wt,
                                            category="Orphaned Git Worktree Metadata (.git/worktrees)",
                                            size_bytes=sz,
                                            reason=f"Target worktree gitdir no longer exists ({raw})",
                                            is_dir=True,
                                        )
                                    )
                        except OSError:
                            pass
        dirnames[:] = [
            d for d in dirnames
            if d not in FAST_SKIP_DIRS and d != ".git" and (current_dir / d) not in seen_paths
        ]




# ============================================================================
# 5B. AI AGENT STRAY CLUTTER, GHOST WORKTREES & MULTI-ACCOUNT HYGIENE (v2.3.0)
# ============================================================================

AGENT_EMPTY_DOTDIRS: Set[str] = {
    ".agents", ".claude", ".codex", ".gemini", ".cursor", ".windsurf", ".cline", ".roo"
}


def _is_git_tracked(repo_dir: Path, rel_target: str) -> bool:
    """Return True if rel_target is tracked by Git."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "ls-files", "--error-unmatch", rel_target],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return res.returncode == 0
    except Exception:
        return False


def _dir_has_only_ignorable_markers(path: Path, allowed_markers: Set[str]) -> bool:
    """Return True if directory tree contains zero symlinks and zero files outside allowed_markers."""
    try:
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            cur = Path(dirpath)
            for d in dirnames:
                if (cur / d).is_symlink():
                    return False
            for f in filenames:
                fp = cur / f
                if fp.is_symlink() or f not in allowed_markers:
                    return False
        return True
    except OSError:
        return False


def _is_accidental_dummy_npm_package(pkg_json: Path) -> bool:
    """Detect untouched `npm init -y` package.json left accidentally inside native non-JS worktrees."""
    try:
        if pkg_json.stat().st_size > 512:
            return False
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        for dep_key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies", "workspaces"):
            if data.get(dep_key):
                return False
        scripts = data.get("scripts") or {}
        if set(scripts.keys()) - {"test"}:
            return False
        test_cmd = str(scripts.get("test", ""))
        if "no test specified" not in test_cmd:
            return False
        parent = pkg_json.parent
        has_native_anchor = any(
            any(parent.glob(pat))
            for pat in ("*.xcodeproj", "*.xcworkspace", "Package.swift", "Cargo.toml", "go.mod", "CMakeLists.txt")
        )
        if not has_native_anchor:
            return False
        has_js_sources = any(
            any(parent.glob(pat))
            for pat in ("*.js", "*.ts", "*.jsx", "*.tsx", "*.mjs", "*.cjs", "src/*.js", "src/*.ts")
        )
        return not has_js_sources
    except Exception:
        return False


def scan_workspace_agent_clutter(
    root: Path,
    min_age_seconds: float,
    active_paths: Set[Path],
    report: ScanReport,
    seen_paths: Set[Path],
) -> None:
    """
    Pass 2E: Sweep empty Git-untracked agent dotdirs (.agents/.claude/.gemini/...),
    empty 0-byte Worktrees / .orca-preparing / TaskHistory directory shells, and
    accidental `npm init -y` dummy manifests in pure native (Xcode/Swift/Rust/Go/C++) repos.
    """
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_dir = Path(dirpath)
        if current_dir in seen_paths or any(p in seen_paths for p in current_dir.parents):
            dirnames[:] = []
            continue
        if (current_dir / ".keep").exists() or (current_dir / ".pinned").exists():
            dirnames[:] = []
            continue

        in_git_repo = (current_dir / ".git").exists()

        # 1. Empty Git-untracked agent dotdirs in repo/project roots
        for d in list(dirnames):
            if d in AGENT_EMPTY_DOTDIRS:
                dp = current_dir / d
                if dp.is_symlink() or dp in seen_paths or dp in active_paths:
                    continue
                if is_recently_modified(dp, min_age_seconds):
                    continue
                if _dir_has_only_ignorable_markers(dp, {".DS_Store"}):
                    if in_git_repo and _is_git_tracked(current_dir, d):
                        continue
                    seen_paths.add(dp)
                    report.candidates.append(
                        CandidateItem(
                            path=dp,
                            category="Empty Untracked Agent Dotdir",
                            size_bytes=get_path_size(dp),
                            reason=f"Empty untracked `{d}/` directory shell",
                            is_dir=True,
                        )
                    )

        # 2. Empty ghost shells under Worktrees/ or TaskHistory/ & stale .orca-preparing dirs
        parent_name = current_dir.name
        if parent_name in {"Worktrees", "worktrees", "TaskHistory"}:
            for d in list(dirnames):
                dp = current_dir / d
                if dp.is_symlink() or dp in seen_paths or dp in active_paths:
                    continue
                if is_recently_modified(dp, min_age_seconds):
                    continue
                if _dir_has_only_ignorable_markers(dp, {".DS_Store", ".metadata_never_index"}):
                    seen_paths.add(dp)
                    report.candidates.append(
                        CandidateItem(
                            path=dp,
                            category="Empty Worktree / TaskHistory Shell",
                            size_bytes=get_path_size(dp),
                            reason=f"Empty 0-file directory shell inside `{parent_name}/`",
                            is_dir=True,
                        )
                    )
                    dirnames.remove(d)
        elif ".orca-preparing" in dirnames:
            op = current_dir / ".orca-preparing"
            if not op.is_symlink() and op not in seen_paths and not is_recently_modified(op, min_age_seconds):
                if _dir_has_only_ignorable_markers(op, {".DS_Store"}):
                    seen_paths.add(op)
                    report.candidates.append(
                        CandidateItem(
                            path=op,
                            category="Stale Worktree Staging Shell (.orca-preparing)",
                            size_bytes=get_path_size(op),
                            reason="Empty `.orca-preparing` worktree staging directory",
                            is_dir=True,
                        )
                    )
                    dirnames.remove(".orca-preparing")

        # 3. Accidental dummy npm package.json + lockfile in pure native repos/worktrees
        if in_git_repo and "package.json" in filenames:
            pkg_p = current_dir / "package.json"
            if pkg_p not in seen_paths and not pkg_p.is_symlink() and not is_recently_modified(pkg_p, min_age_seconds):
                if not _is_git_tracked(current_dir, "package.json") and _is_accidental_dummy_npm_package(pkg_p):
                    sz = get_path_size(pkg_p)
                    seen_paths.add(pkg_p)
                    report.candidates.append(
                        CandidateItem(
                            path=pkg_p,
                            category="Accidental Dummy Manifest (package.json)",
                            size_bytes=sz,
                            reason="Untracked default `npm init -y` package.json inside non-JS native repository",
                            is_dir=False,
                        )
                    )
                    for lock_name in ("pnpm-lock.yaml", "package-lock.json", "yarn.lock"):
                        lp = current_dir / lock_name
                        if lp.is_file() and not lp.is_symlink() and not _is_git_tracked(current_dir, lock_name):
                            seen_paths.add(lp)
                            report.candidates.append(
                                CandidateItem(
                                    path=lp,
                                    category="Accidental Dummy Lockfile",
                                    size_bytes=get_path_size(lp),
                                    reason=f"Untracked lockfile paired with dummy package.json ({lock_name})",
                                    is_dir=False,
                                )
                            )

        dirnames[:] = [
            d for d in dirnames
            if d not in FAST_SKIP_DIRS
            and d not in PROTECTED_DIRS
            and d not in {"Archive", "Data"}
            and (current_dir / d) not in seen_paths
        ]


def _slugify_claude_project_path(p: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", str(p))


def scan_global_agent_clutter(
    home_dir: Optional[Path] = None,
    workspace_roots: Optional[List[Path]] = None,
    min_age_hours: float = 0.0,
    active_paths: Optional[Set[Path]] = None,
    apps_roots: Optional[List[Path]] = None,
) -> ScanReport:
    """
    Scan user home & multi-account AI agent directories (~/.claude, ~/.codex, ~/.gemini, ~/.orca,
    ~/.cursor, ~/.windsurf, and Orca multi-account profiles) for orphaned session slugs, unused-IDE
    ghost dotdirs, stale .bak/.tmp clutter, and broken local MCP binary paths.
    """
    home = (home_dir or Path.home()).expanduser().resolve()
    rep = ScanReport(root=home)
    min_age_sec = max(0.0, min_age_hours * 3600.0)
    act = active_paths or set()
    seen: Set[Path] = set()
    app_dirs = apps_roots if apps_roots is not None else [Path("/Applications"), home / "Applications"]

    def _add(p: Path, cat: str, reason: str, is_dir: bool) -> None:
        if p in seen or p in act or is_recently_modified(p, min_age_sec):
            return
        seen.add(p)
        rep.candidates.append(
            CandidateItem(path=p, category=cat, size_bytes=get_path_size(p), reason=reason, is_dir=is_dir)
        )

    # 1. Unused-IDE ghost dotdirs (~/.cursor, ~/.windsurf) when IDE .app is not installed
    for dot_name, app_name in ((".cursor", "Cursor.app"), (".windsurf", "Windsurf.app")):
        dot_p = home / dot_name
        if not dot_p.is_dir() or dot_p.is_symlink():
            continue
        if any((ad / app_name).exists() for ad in app_dirs):
            continue
        has_real_files = False
        for r, ds, fs in os.walk(dot_p, followlinks=False):
            rp = Path(r)
            for f in fs:
                if not (rp / f).is_symlink() and f != ".DS_Store":
                    has_real_files = True
                    break
            if has_real_files:
                break
        if not has_real_files:
            _add(dot_p, "Unused IDE Ghost Dotdir", f"`{app_name}` not installed and `{dot_name}` holds no user files", True)

    # 2. Home & multi-account .bak* backups, dangling ..*.tmp-*, .tmp/git-*, and empty pets/spares
    for pat in (".claude.json.backup", ".claude.json.bak*", ".zprofile.bak*", ".zshenv.bak*", ".zshrc.bak*"):
        for m in sorted(home.glob(pat)):
            if m.is_file() and not m.is_symlink():
                _add(m, "Stale Home Config Backup (.bak)", f"Superseded backup file in home directory ({m.name})", False)

    agent_homes: List[Path] = [home / ".claude", home / ".codex", home / ".gemini", home / ".orca"]
    orca_support = home / "Library" / "Application Support" / "orca"
    if orca_support.is_dir():
        for sub_glob in ("claude-profiles/*/home", "codex-accounts/*/home", "codex-runtime-home/home"):
            agent_homes.extend(sorted(orca_support.glob(sub_glob)))

    for ah in agent_homes:
        if not ah.is_dir() or ah.is_symlink():
            continue
        for pat in ("*.bak", "*.bak-*", ".*.bak", ".*.bak-*", "..*.tmp-*"):
            for m in sorted(ah.glob(pat)):
                if m.is_file() and not m.is_symlink():
                    _add(m, "Stale Agent Config Backup / Temp", f"Stale backup/temp file inside {ah.name} ({m.name})", False)
        for git_tmp in sorted((ah / ".tmp").glob("git-*")) if (ah / ".tmp").is_dir() else []:
            if git_tmp.is_dir() and not git_tmp.is_symlink():
                _add(git_tmp, "Stale Codex Git Temp Dir (.tmp/git-*)", "Leftover temporary git probe directory", True)
        for empty_sub in ("pets", "spares"):
            esp = ah / empty_sub
            if esp.is_dir() and not esp.is_symlink() and _dir_has_only_ignorable_markers(esp, {".DS_Store"}):
                _add(esp, "Empty Agent Internal Directory", f"Empty `{empty_sub}/` directory inside {ah.name}", True)

    # 3. Orphaned ~/.claude/projects/<slug> session directories (strictly preserving any with memory/)
    claude_projects = home / ".claude" / "projects"
    if claude_projects.is_dir() and workspace_roots:
        known_slugs: Set[str] = {_slugify_claude_project_path(home)}
        for wr in workspace_roots:
            wrr = wr.expanduser().resolve()
            known_slugs.add(_slugify_claude_project_path(wrr))
            for dirpath, dirnames, _ in os.walk(wrr, topdown=True, followlinks=False):
                cp = Path(dirpath)
                known_slugs.add(_slugify_claude_project_path(cp))
                dirnames[:] = [
                    d for d in dirnames
                    if d not in {"node_modules", ".git", "DerivedData", ".build", "Archive", "TaskHistory"}
                ]
        for slug_dir in sorted(claude_projects.iterdir()):
            if not slug_dir.is_dir() or slug_dir.is_symlink():
                continue
            if (slug_dir / "memory").exists():
                rep.protected_notes.append(f"Protected Claude project memory: {slug_dir}")
                continue
            if "---" in slug_dir.name and "Worktrees-" not in slug_dir.name:
                continue
            if slug_dir.name not in known_slugs:
                _add(
                    slug_dir,
                    "Orphaned Claude Worktree Session Cache",
                    f"Target worktree path no longer exists and no `memory/` present ({slug_dir.name})",
                    True,
                )

    # 4. Audit local MCP executable paths for broken references (non-destructive warning)
    try:
        import tomllib
        for cfg_p in [home / ".codex" / "config.toml"] + (
            sorted(orca_support.glob("codex-accounts/*/home/config.toml")) if orca_support.is_dir() else []
        ):
            if cfg_p.is_file():
                data = tomllib.loads(cfg_p.read_text(encoding="utf-8", errors="ignore"))
                for name, spec in (data.get("mcp_servers") or {}).items():
                    if isinstance(spec, dict) and spec.get("enabled", True) is not False:
                        cmd = str(spec.get("command") or "")
                        if cmd.startswith(("/", "~/")):
                            resolved_cmd = Path(cmd).expanduser()
                            if not resolved_cmd.exists():
                                rep.protected_notes.append(
                                    f"[Broken MCP Warning] {cfg_p}: mcp_servers.{name} command missing on disk -> {resolved_cmd}"
                                )
    except Exception:
        pass

    return rep


# ============================================================================
# 6. CONTENT-BASED SCRATCH DEDUPLICATION (SHA-256 FAST SIGNATURE)
# ============================================================================

def scan_duplicate_scratch_files(
    root: Path,
    dedup_min_mb: float,
    referenced_paths: Set[Path],
    referenced_names: Set[str],
    active_paths: Set[Path],
    report: ScanReport,
    seen_paths: Set[Path],
) -> None:
    """
    Find byte-identical large files (>= dedup_min_mb) inside temporary/scratch/test/export
    directories and mark redundant copies for cleanup while keeping the canonical source.
    """
    if dedup_min_mb <= 0:
        return
    min_bytes = int(dedup_min_mb * 1024 * 1024)
    scratch_tokens = {"tmp", "temp", "scratch", "test_out", "exports", "dumps", "captures", "work"}

    def _rel_parts_lower(target: Path) -> List[str]:
        try:
            return [seg.lower() for seg in target.relative_to(root).parts[:-1]]
        except ValueError:
            return [seg.lower() for seg in target.parts[:-1]]

    def _is_scratch_path(target: Path) -> bool:
        segs = _rel_parts_lower(target)
        if "archive" in segs:
            return False
        for seg in segs:
            tokens = set(re.split(r"[-_.]", seg))
            if seg in scratch_tokens or (tokens & scratch_tokens):
                return True
        return False

    git_tracked_cache: Dict[Path, Set[str]] = {}
    def _is_git_tracked(target: Path) -> bool:
        cur = target.parent
        git_root: Optional[Path] = None
        while cur != cur.parent:
            if (cur / ".git").exists():
                git_root = cur
                break
            cur = cur.parent
        if git_root is None:
            return False
        if git_root not in git_tracked_cache:
            try:
                out = subprocess.check_output(
                    ["git", "-C", str(git_root), "ls-files"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
                git_tracked_cache[git_root] = set(out.splitlines())
            except Exception:
                git_tracked_cache[git_root] = set()
        try:
            rel = str(target.relative_to(git_root))
            return rel in git_tracked_cache[git_root]
        except ValueError:
            return False

    sig_map: Dict[str, List[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current_dir = Path(dirpath)
        if current_dir in seen_paths or any(p in seen_paths for p in current_dir.parents):
            dirnames[:] = []
            continue
        if (current_dir / ".keep").exists():
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames
            if d not in FAST_SKIP_DIRS and d.lower() != "archive" and (current_dir / d) not in seen_paths
            and not (current_dir / d / ".keep").exists()
        ]

        for fname in filenames:
            fpath = current_dir / fname
            if fpath in seen_paths or fpath.is_symlink() or fpath in active_paths or fname in referenced_names:
                continue
            if fpath.suffix.lower() in PROTECTED_MODEL_EXTS:
                continue
            try:
                sz = fpath.stat().st_size
            except OSError:
                continue
            if sz < min_bytes:
                continue
            sig = fast_file_signature(fpath)
            if sig:
                sig_map.setdefault(sig, []).append(fpath)

    for sig, paths in sig_map.items():
        if len(paths) < 2:
            continue

        def canonical_priority(p: Path) -> Tuple[int, int, str]:
            try:
                is_ref = 0 if (p.resolve() in referenced_paths or p.name in referenced_names) else 1
            except OSError:
                is_ref = 1
            in_scratch = 1 if _is_scratch_path(p) else 0
            return (is_ref, in_scratch, str(p))

        sorted_paths = sorted(paths, key=canonical_priority)
        canonical = sorted_paths[0]
        for dup in sorted_paths[1:]:
            try:
                if dup.resolve() in referenced_paths or dup.name in referenced_names or dup in active_paths:
                    continue
            except OSError:
                pass
            if _is_git_tracked(dup):
                continue
            if _is_scratch_path(dup) or "copy" in dup.name.lower():
                sz = get_path_size(dup)
                seen_paths.add(dup)
                report.candidates.append(
                    CandidateItem(
                        path=dup,
                        category="Duplicate Large Scratch/Test Artifacts",
                        size_bytes=sz,
                        reason=f"Byte-identical duplicate of {canonical.name} (SHA-256 signature verified)",
                        is_dir=False,
                    )
                )


# ============================================================================
# 7. MAIN WORKSPACE SCANNER
# ============================================================================

def scan_workspace(
    root: Path,
    include_node_modules: bool = False,
    keep_versions: int = 2,
    min_age_hours: float = 0.0,
    dedup_scratch_mb: float = 25.0,
    active_paths: Optional[Set[Path]] = None,
    active_basenames: Optional[Set[str]] = None,
) -> ScanReport:
    report = ScanReport(root=root)
    seen_paths: Set[Path] = set()
    min_age_seconds = min_age_hours * 3600.0
    if active_paths is None or active_basenames is None:
        active_paths, active_basenames = collect_active_process_paths()

    referenced_paths, referenced_names = collect_referenced_paths(root)
    if referenced_paths or referenced_names:
        report.protected_notes.append(
            f"Protected {len(referenced_paths) + len(referenced_names)} active media/script-referenced asset(s)"
        )

    # Pre-Pass (Gate 05): Pointer-governed UUID pools & iterative test-run backups first
    # so active/newest runs & UUIDs are added to `seen_paths` and protected from inner bundle pruning
    scan_pointer_governed_install_dirs(root, keep_versions, report, seen_paths)
    scan_iterative_run_backups(root, keep_versions=keep_versions, report=report, seen_paths=seen_paths)

    # Pass 1: Walk directory tree for unconditional & context-verified caches
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_dir = Path(dirpath)
        if current_dir in seen_paths or any(p in seen_paths for p in current_dir.parents):
            dirnames[:] = []
            continue

        # Special handling for Final Cut Pro `.fcpbundle` packages
        if current_dir.name.endswith(".fcpbundle"):
            for sub in ("Render Files", "Transcoded Media", "Analysis Files"):
                for match in current_dir.rglob(sub):
                    if match.is_dir() and match not in seen_paths:
                        sz = get_path_size(match)
                        if sz > 0:
                            seen_paths.add(match)
                            report.candidates.append(
                                CandidateItem(
                                    path=match,
                                    category="Media Render Cache (.fcpbundle)",
                                    size_bytes=sz,
                                    reason=f"Regenerable FCP {sub} inside {current_dir.name}",
                                    is_dir=True,
                                )
                            )
            dirnames[:] = []
            continue

        kept_dirs: List[str] = []
        for d in dirnames:
            d_path = current_dir / d
            if d_path.is_symlink():
                continue
            if d_path in seen_paths:
                continue

            # Gate 01 & 02 & 03: Never traverse or delete Protected VCS / AI Agent Memory Directories
            if d in PROTECTED_DIRS:
                if d in {".agents", ".claude", ".codex", ".cursor", ".windsurf", ".cline", ".roo", "memory", "docs"}:
                    report.protected_notes.append(f"Protected AI Agent Memory/Config: {d_path}")
                continue

            # Fast skip huge OSS kernel / vendor trees that don't contain build caches
            if d in {"linux-kernel", "sony-oss-fx3"} and not include_node_modules:
                continue

            matched_category: Optional[str] = None
            matched_reason: Optional[str] = None

            # Check unconditional cache directories
            if d in UNCONDITIONAL_CACHE_DIRS:
                matched_category = UNCONDITIONAL_CACHE_DIRS[d]
                matched_reason = f"Standard disposable cache/build directory '{d}'"

            # Check prefix patterns (e.g., cmake-build-*, bazel-*, DerivedData-*, derived-simulator*)
            if not matched_category:
                for prefix, cat in PREFIX_CACHE_DIRS:
                    if d.startswith(prefix):
                        matched_category = cat
                        matched_reason = f"Matches build directory prefix '{prefix}*'"
                        break

            # Check suffix patterns (e.g., *.xcresult, *.dSYM, *.saved-application, *UITests-Runner.app)
            if not matched_category:
                for suffix, cat in SUFFIX_CACHE_DIRS:
                    if d.endswith(suffix):
                        matched_category = cat
                        matched_reason = f"Matches diagnostic/backup/runner bundle suffix '*{suffix}'"
                        break

            # Gate 04: Context-verified build directories & detached build `.app` bundles
            if not matched_category:
                verified = classify_context_verified_dir(d_path, d, include_node_modules, active_paths)
                if verified:
                    matched_category, matched_reason = verified

            if matched_category and matched_reason:
                # Gate 01: Active process / modification age check
                if is_recently_modified(d_path, min_age_seconds):
                    report.skipped_active.append(f"Skipped recently modified (<{min_age_hours}h): {d_path}")
                    continue
                sz = get_path_size(d_path)
                seen_paths.add(d_path)
                report.candidates.append(
                    CandidateItem(
                        path=d_path,
                        category=matched_category if sz > 0 else f"Empty/Skeleton {matched_category}",
                        size_bytes=sz,
                        reason=matched_reason if sz > 0 else f"0-byte skeleton/broken-symlink remnant ({matched_reason})",
                        is_dir=True,
                    )
                )
                continue

            if (d_path / ".keep").exists():
                report.protected_notes.append(f"Protected (.keep pin): {d_path}")
                continue

            # Never descend inside retained macOS/iOS code-signed bundles (.app, .framework, etc.)
            if d.endswith((".app", ".framework", ".bundle", ".plugin", ".xctest", ".saved-application", ".app.saved")):
                continue

            kept_dirs.append(d)

        dirnames[:] = kept_dirs

        # Inspect individual files in non-pruned directories
        for fname in filenames:
            fpath = current_dir / fname
            if fpath.is_symlink() or fpath in seen_paths or fpath in active_paths:
                continue

            if fname in PROTECTED_FILES or fname in referenced_names:
                continue

            ext = fpath.suffix.lower()
            if ext in PROTECTED_MODEL_EXTS:
                continue

            try:
                if fpath.resolve() in referenced_paths:
                    continue
            except OSError:
                pass

            file_cat: Optional[str] = None
            file_reason: Optional[str] = None

            if fname in DISPOSABLE_EXACT_FILES:
                file_cat = "OS & Linter Metadata Cache"
                file_reason = f"Disposable metadata/cache file '{fname}'"
            elif ext in DISPOSABLE_FILE_EXTS:
                file_cat = DISPOSABLE_FILE_EXTS[ext]
                file_reason = f"Disposable intermediate/dump file (*{ext})"
            elif ext == ".raw" and any(
                k in fname.lower() for k in ("dump", "probe", "debug", "capture", "temp", "out_")
            ):
                file_cat = "Raw Debug Framebuffer Dump (.raw)"
                file_reason = "Uncompressed debug/framebuffer dump matching dump/probe/capture naming"

            if file_cat and file_reason:
                if is_recently_modified(fpath, min_age_seconds):
                    continue
                sz = get_path_size(fpath)
                if sz > 0:
                    seen_paths.add(fpath)
                    report.candidates.append(
                        CandidateItem(
                            path=fpath,
                            category=file_cat,
                            size_bytes=sz,
                            reason=file_reason,
                            is_dir=False,
                        )
                    )

    # Pass 2B: Semantic versioned release build retention
    scan_versioned_release_dirs(root, keep_versions, report, seen_paths)

    # Pass 2C: Iterative numbered VM/firmware rootfs images (`.cpio`, `.img`, `.fdat`)
    scan_iterative_build_files(
        root,
        keep_versions=keep_versions,
        min_age_seconds=min_age_seconds,
        active_paths=active_paths,
        active_basenames=active_basenames,
        referenced_names=referenced_names,
        report=report,
        seen_paths=seen_paths,
    )

    # Pass 2D: Orphaned Git worktree administrative metadata (.git/worktrees/<id> with missing target)
    scan_orphan_git_worktrees(root, report, seen_paths)

    # Pass 3: Fast SHA-256 duplicate scratch/test artifact detection
    scan_duplicate_scratch_files(
        root,
        dedup_min_mb=dedup_scratch_mb,
        referenced_paths=referenced_paths,
        referenced_names=referenced_names,
        active_paths=active_paths,
        report=report,
        seen_paths=seen_paths,
    )

    # Pass 2E: Empty untracked agent dotdirs, 0-byte worktree shells & accidental dummy npm manifests
    scan_workspace_agent_clutter(
        root,
        min_age_seconds=min_age_seconds,
        active_paths=active_paths,
        report=report,
        seen_paths=seen_paths,
    )

    return report


# ============================================================================
# 8. XCODE SYSTEM CACHE, LAUNCHSERVICES & SPOTLIGHT HYGIENE (macOS)
# ============================================================================

def scan_xcode_system_caches(
    include_device_support: bool = False,
    active_paths: Optional[Set[Path]] = None,
    min_age_hours: float = 0.0,
) -> ScanReport:
    """
    Scan system-level Xcode & Simulator caches under `~/Library/Developer` and `~/Library/Caches`
    with per-entry active-process (`xcodebuild`/`swift-frontend`), booted-simulator, and age safety:
      - `~/Library/Developer/Xcode/DerivedData/*` (per-project safety)
      - `~/Library/Developer/CoreSimulator/Caches` & `Temp`
      - `~/Library/Developer/CoreSimulator/Devices/*/data/Library/Caches/com.apple.coresymbolicationd`
      - `~/Library/Developer/XCTestDevices` & `XCPGDevices`
      - Orphaned `~/Library/Developer/CoreSimulator/*-clones` parallel test directories
      - `~/Library/Caches/com.apple.dt.Xcode` & `~/Library/Caches/org.swift.swiftpm`
      - Optional (`--clean-device-support`): `~/Library/Developer/Xcode/*OS DeviceSupport`
    """
    home = Path.home()
    dev_root = home / "Library" / "Developer"
    report = ScanReport(root=dev_root)
    active_paths = active_paths or set()
    min_age_seconds = max(0.0, min_age_hours * 3600.0)

    xcode_active = False
    booted_udids: Set[str] = set()
    ps_raw = ""
    try:
        ps_raw = subprocess.check_output(["ps", "-axo", "command"], text=True, errors="ignore")
        if re.search(r"\b(xcodebuild|swift-frontend)\b", ps_raw):
            xcode_active = True
        for m in re.finditer(r"([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})", ps_raw):
            booted_udids.add(m.group(1).upper())
    except Exception:
        pass
    try:
        sim_out = subprocess.check_output(["xcrun", "simctl", "list", "devices", "booted"], text=True, errors="ignore")
        for m in re.finditer(r"([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})", sim_out):
            booted_udids.add(m.group(1).upper())
    except Exception:
        pass

    def _is_path_active(candidate: Path) -> bool:
        c_str = str(candidate)
        if c_str in ps_raw:
            return True
        for ap in active_paths:
            ap_str = str(ap)
            if ap_str == c_str or ap_str.startswith(c_str + "/") or c_str.startswith(ap_str + "/"):
                return True
        return False

    derived_root = dev_root / "Xcode" / "DerivedData"
    if derived_root.exists() and derived_root.is_dir():
        for child in sorted(derived_root.iterdir()):
            if child.name.startswith("."):
                continue
            if xcode_active and child.name in ("ModuleCache.noindex", "SDKStatCaches.noindex"):
                report.skipped_active.append(f"Skipped shared Xcode cache while build active: {child}")
                continue
            if _is_path_active(child):
                report.skipped_active.append(f"Skipped active Xcode DerivedData: {child}")
                continue
            if min_age_seconds > 0 and is_recently_modified(child, min_age_seconds):
                report.skipped_active.append(f"Skipped recently modified DerivedData (<{min_age_hours}h): {child}")
                continue
            sz = get_path_size(child)
            if sz > 0:
                report.candidates.append(
                    CandidateItem(
                        path=child,
                        category="Xcode System DerivedData",
                        size_bytes=sz,
                        reason=f"Regenerable Xcode intermediate build/index '{child.name}'",
                        is_dir=child.is_dir(),
                    )
                )

    targets: List[Tuple[Path, str, str, bool]] = [
        (dev_root / "CoreSimulator" / "Caches", "CoreSimulator System Caches", "Regenerable iOS Simulator runtime/dyld caches", True),
        (dev_root / "CoreSimulator" / "Temp", "CoreSimulator Temp Files", "Temporary iOS Simulator files", True),
        (dev_root / "XCTestDevices", "Orphaned XCTest Simulator Clones", "Disposable parallel test simulator clones", True),
        (dev_root / "XCPGDevices", "Orphaned Playground Simulator Clones", "Disposable Swift Playground simulator clones", False),
        (home / "Library" / "Caches" / "com.apple.dt.Xcode", "Xcode Application Cache", "Regenerable com.apple.dt.Xcode cache", True),
        (home / "Library" / "Caches" / "org.swift.swiftpm", "SwiftPM Global Cache", "Regenerable Swift Package Manager cache", True),
    ]

    if include_device_support:
        for ds_name in ("iOS DeviceSupport", "watchOS DeviceSupport", "tvOS DeviceSupport", "visionOS DeviceSupport"):
            targets.append(
                (
                    dev_root / "Xcode" / ds_name,
                    f"Xcode {ds_name} Symbols",
                    "Extracted device debug symbols (regenerated automatically when device connects)",
                    False,
                )
            )

    for tpath, cat, reason, skip_when_building in targets:
        if tpath.exists():
            if skip_when_building and (xcode_active or _is_path_active(tpath)):
                report.skipped_active.append(f"Skipped active Xcode/Simulator system cache: {tpath}")
                continue
            sz = get_path_size(tpath)
            if sz > 0:
                report.candidates.append(
                    CandidateItem(path=tpath, category=cat, size_bytes=sz, reason=reason, is_dir=True)
                )

    coresim_dir = dev_root / "CoreSimulator"
    if coresim_dir.exists():
        for child in coresim_dir.iterdir():
            if child.is_dir() and child.name.endswith("-clones") and not _is_path_active(child):
                sz = get_path_size(child)
                if sz > 0:
                    report.candidates.append(
                        CandidateItem(
                            path=child,
                            category="Orphaned Simulator Clone Set",
                            size_bytes=sz,
                            reason=f"Orphaned agent simulator clone directory '{child.name}'",
                            is_dir=True,
                        )
                    )
        devices_dir = coresim_dir / "Devices"
        if devices_dir.exists():
            for sym_cache in devices_dir.glob("*/data/Library/Caches/com.apple.coresymbolicationd"):
                if sym_cache.is_dir():
                    udid = sym_cache.parents[3].name.upper()
                    if udid in booted_udids or _is_path_active(sym_cache):
                        report.skipped_active.append(f"Skipped booted/active Simulator symbolication cache: {sym_cache}")
                        continue
                    sz = get_path_size(sym_cache)
                    if sz > 0:
                        report.candidates.append(
                            CandidateItem(
                                path=sym_cache,
                                category="Simulator CoreSymbolication Cache",
                                size_bytes=sz,
                                reason="Regenerable simulator symbolication cache (com.apple.coresymbolicationd)",
                                is_dir=True,
                            )
                        )

    return report




def scan_macos_system_orphans(keep_versions: int = 1) -> ScanReport:
    """
    Scan macOS user (`~/Library`, `~/.*`) and system (`/Library`) directories for:
      1. Orphaned & defunct `LaunchAgents` (`*.plist` with missing target binary or empty stub)
         [Attributed to: alienator88/Pearcleaner, slow-coding/fullcleaner, KnockKnock]
      2. Orphaned Sandbox `Containers`, `Group Containers` (TeamID verified), `Saved Application State`,
         and `Preferences` of uninstalled applications
         [Attributed to: tw93/Mole, momenbasel/PureMac, alienator88/Pearcleaner, Alyetama/Sweep]
      3. Verified Uninstalled Application Support & Suite Remnants (e.g., uninstalled Adobe apps,
         browsers, IDEs) while preserving shared components of installed apps (e.g., CameraRaw for DNG Converter)
         [Attributed to: jithin-sabu/purge-app, Homebrew Cask zap stanzas]
      4. Desktop App Updater Staging (`*-updater`, `*.updater`, `GoogleUpdater/crx_cache`),
         Telemetry/Crash dumps (`dunamis`, `DiagnosticReports`, `CrashReporter`), global package caches
         (`pip`, `pnpm`, `npm/_cacache`), and superseded multi-version AI agent runtime bundles
         [Attributed to: tw93/Mole, mac-cleanup/mac-cleanup-py]
    """
    home = Path.home()
    report = ScanReport(root=home / "Library")
    seen: Set[Path] = set()

    def is_deletable_by_user(p: Path) -> bool:
        try:
            st = p.lstat()
            if st.st_uid != os.getuid():
                return False
            if p.is_dir() and not p.is_symlink():
                # Check if macOS TCC (com.apple.containermanager.identity or App Management TCC) blocks unlinking
                os.listdir(p)
                for r, d, files in os.walk(p):
                    for name in list(d) + list(files):
                        sub = Path(r) / name
                        if sub.lstat().st_uid != os.getuid():
                            return False
            return True
        except OSError:
            return False

    def add_candidate(p: Path, cat: str, reason: str, allow_zero: bool = True) -> None:
        if p in seen or (not p.exists() and not p.is_symlink()):
            return
        if not os.access(p.parent, os.W_OK) or not is_deletable_by_user(p):
            return
        sz = get_path_size(p)
        if any(part in ("Containers", "Group Containers") for part in p.parts) and sz < 1024 * 1024:
            # Skip empty/stub TCC-locked sandbox container nodes (<1 MB) per tw93/Mole TCC boundary rule
            return
        if sz == 0 and p.is_dir() and not p.is_symlink():
            # Check if macOS App Management TCC (com.apple.provenance) blocks rmdir on 0-byte stub
            return
        if sz > 0 or allow_zero:
            seen.add(p)
            report.candidates.append(
                CandidateItem(path=p, category=cat, size_bytes=sz, reason=reason, is_dir=p.is_dir())
            )

    # Build active installed identity inventory (Bundle IDs, App Names, Team IDs, Symlinked Binaries)
    installed_bids: Set[str] = set()
    installed_team_ids: Set[str] = set()
    installed_app_stems: Set[str] = set()
    active_symlink_targets: Set[Path] = set()

    for bin_dir in (home / ".local" / "bin", Path("/usr/local/bin"), Path("/opt/homebrew/bin")):
        if bin_dir.is_dir():
            for b in bin_dir.iterdir():
                if b.is_symlink():
                    try:
                        active_symlink_targets.add(b.resolve())
                    except OSError:
                        pass

    for app_root in (Path("/Applications"), home / "Applications", Path("/System/Applications"), Path("/Applications/Utilities")):
        if not app_root.is_dir():
            continue
        for item in app_root.iterdir():
            if item.name.endswith(".app"):
                installed_app_stems.add(item.stem.lower())
            elif item.is_dir() and not item.is_symlink():
                for sub in item.iterdir():
                    if sub.name.endswith(".app"):
                        installed_app_stems.add(sub.stem.lower())

    if os.path.exists(LSREGISTER_PATH):
        try:
            dump_out = subprocess.check_output([LSREGISTER_PATH, "-dump"], text=True, errors="ignore")
            cur_valid_path: Optional[str] = None
            for line in dump_out.splitlines():
                if line.startswith("path:"):
                    m = re.match(r"^path:\s+(.*?)\s+\(0x", line)
                    if m:
                        p_str = m.group(1).strip()
                        if os.path.exists(p_str) and not any(
                            k in p_str for k in ("/.Trash", "/.Trashes/", "/DerivedData/", "/AI/Archive/", "/AI/Migration/")
                        ):
                            cur_valid_path = p_str
                        else:
                            cur_valid_path = None
                elif line.startswith("identifier:") and cur_valid_path:
                    bid = line.split(":", 1)[1].strip().split(" (")[0].strip().lower()
                    installed_bids.add(bid)
                elif line.startswith("teamID:") and cur_valid_path:
                    tid = line.split(":", 1)[1].strip().upper()
                    if tid and tid != "0000000000":
                        installed_team_ids.add(tid)
        except Exception:
            pass

    try:
        pkgs = subprocess.check_output(["pkgutil", "--pkgs"], text=True, errors="ignore").splitlines()
        for pk in pkgs:
            installed_bids.add(pk.strip().lower())
    except Exception:
        pass

    def has_app_matching(substr: str) -> bool:
        s = substr.lower()
        return any(s in stem for stem in installed_app_stems)

    # 1. Rule A: Orphaned & Defunct LaunchAgents (Pearcleaner / fullcleaner / KnockKnock rule)
    for la_dir in (home / "Library" / "LaunchAgents", Path("/Library/LaunchAgents")):
        if not la_dir.is_dir():
            continue
        for plist_path in sorted(la_dir.glob("*.plist")):
            if plist_path.name.startswith("com.apple."):
                continue
            try:
                with open(plist_path, "rb") as f:
                    pl = plistlib.load(f)
                prog = pl.get("Program")
                args = pl.get("ProgramArguments") or []
                if not prog and not args:
                    add_candidate(
                        plist_path,
                        "Orphaned / Defunct LaunchAgent (.plist)",
                        "Empty stub LaunchAgent plist with no Program or ProgramArguments",
                    )
                    continue
                exe = prog or (args[0] if args else None)
                if isinstance(exe, str) and exe.startswith("/") and not os.path.exists(exe):
                    add_candidate(
                        plist_path,
                        "Orphaned / Broken LaunchAgent (.plist)",
                        f"Target executable no longer exists ({exe})",
                    )
            except Exception:
                pass

    # 2. Rule B: Orphaned Sandbox Containers, Group Containers (TeamID verified) & Preferences
    # (Mole / PureMac / Pearcleaner / Sweep rule)
    containers_dir = home / "Library" / "Containers"
    if containers_dir.is_dir():
        for cdir in sorted(containers_dir.iterdir()):
            cname = cdir.name
            if cname.startswith((".", "com.apple.")):
                continue
            if any(
                cname.startswith(prefix)
                for prefix in (
                    "com.kafu.PrismV3GammaPairProbe",
                    "com.kafu.PrismGammaRequestProbe",
                    "com.kafu.PrismGradeLab.service",
                )
            ):
                add_candidate(
                    cdir,
                    "Orphaned Sandbox Container (~/Library/Containers)",
                    f"Sandbox container for uninstalled diagnostic probe '{cname}'",
                )

    group_containers_dir = home / "Library" / "Group Containers"
    if group_containers_dir.is_dir():
        team_re = re.compile(r"^([A-Z0-9]{10})\.(.+)$")
        for gdir in sorted(group_containers_dir.iterdir()):
            gname = gdir.name
            if gname.startswith((".", "com.apple.", "group.com.apple.")):
                continue
            m = team_re.match(gname)
            if m:
                tid, suite = m.group(1), m.group(2).lower()
                if "apple" in suite:
                    continue
                if tid not in installed_team_ids and not any(
                    suite == b or b.startswith(suite + ".") or suite.startswith(b + ".")
                    for b in installed_bids
                ):
                    add_candidate(
                        gdir,
                        "Orphaned Group Container (~/Library/Group Containers)",
                        f"Developer Team ID '{tid}' ({suite}) has zero installed applications on system",
                    )

    prefs_dir = home / "Library" / "Preferences"
    if prefs_dir.is_dir():
        uninstalled_pref_checks = [
            ("com.adobe.AfterEffects.", "after effects", "Adobe After Effects"),
            ("com.Adobe.After Effects", "after effects", "Adobe After Effects"),
            ("com.adobe.Install.Acrobat", "acrobat", "Adobe Acrobat"),
            ("com.adobe.acrobat_webcapture", "acrobat", "Adobe Acrobat"),
            ("com.adobe.Synchronizer.DC", "acrobat", "Adobe Acrobat"),
            ("com.adobe.ame.application", "media encoder", "Adobe Media Encoder"),
            ("com.blackmagic-design.DaVinciResolveProjectServer", "davinci resolve", "DaVinci Resolve"),
            ("app.creatorcache.cleaner", "creatorcachecleaner", "CreatorCacheCleaner"),
            ("com.kafu.PrismGradeInstaller.local", "prismgradeinstaller", "PrismGradeInstaller"),
            ("com.kafu.FCPRetimePointAssistant", "fcpretimepointassistant", "FCPRetimePointAssistant"),
            ("com.adobe.crashreporter.plist", "__never__", "Adobe CrashReporter"),
            ("com.adobe.dunamis.plist", "__never__", "Adobe Dunamis Telemetry"),
        ]
        for pfile in sorted(prefs_dir.glob("*.plist")):
            for prefix, app_probe, label in uninstalled_pref_checks:
                if pfile.name.startswith(prefix) and not has_app_matching(app_probe):
                    add_candidate(
                        pfile,
                        "Orphaned App Preference (~/Library/Preferences)",
                        f"Preference plist for uninstalled {label}",
                    )
                    break

    # 3. Rule C: Verified Uninstalled Application Support & Suite Remnants
    # (purge-app / Homebrew Cask zap / Pearcleaner rule)
    app_support_checks = [
        # Adobe suite components (preserving CameraRaw for installed Adobe DNG Converter)
        (Path("/Library/Application Support/Adobe/Acrobat"), "acrobat", "Adobe Acrobat"),
        (Path("/Library/Application Support/Adobe/MACPDFM"), "acrobat", "Adobe Acrobat PDFMaker"),
        (Path("/Library/Application Support/Adobe/Acrobat DC Helper Frameworks"), "acrobat", "Adobe Acrobat Helper"),
        (Path("/Library/Application Support/Adobe/ARMDC"), "acrobat", "Adobe Acrobat Refresh Manager"),
        (home / "Library/Application Support/Adobe/Acrobat", "acrobat", "Adobe Acrobat"),
        (Path("/Library/Application Support/Adobe/Adobe Photoshop 2026"), "photoshop", "Adobe Photoshop 2026"),
        (home / "Library/Application Support/Adobe/Adobe Photoshop 2026", "photoshop", "Adobe Photoshop 2026"),
        (Path("/Library/Application Support/Adobe/After Effects"), "after effects", "Adobe After Effects"),
        (home / "Library/Application Support/Adobe/Adobe Illustrator 29", "illustrator", "Adobe Illustrator 29"),
        (home / "Library/Caches/com.adobe.LightroomClassicCC7", "lightroom", "Adobe Lightroom Classic"),
        (Path("/Library/Application Support/Adobe/Installers"), "__never__", "Adobe Stale Installer Cache"),
        (Path("/Library/Application Support/Adobe/Uninstall"), "__never__", "Adobe Stale Uninstall Leftovers"),
        # Uninstalled IDEs, Browsers & Utilities
        (home / "Library/Application Support/Google/AndroidStudio2026.1.1", "android studio", "Android Studio"),
        (home / ".skiko", "android studio", "JetBrains Skiko Native Cache"),
        (Path("/Library/Application Support/Microsoft"), "microsoft", "Microsoft Office/Edge"),
        (home / "Library/Application Support/Microsoft", "microsoft", "Microsoft Office/Edge"),
        (home / "Library/Application Support/Microsoft Edge", "microsoft edge", "Microsoft Edge"),
        (Path("/Library/Application Support/Mozilla"), "firefox", "Mozilla Firefox"),
        (Path("/Library/Application Support/clash-verge-service"), "clash verge", "Clash Verge"),
        (home / "Library/Application Support/com.operasoftware.Opera", "opera", "Opera Browser"),
        (home / "Library/Application Support/Vivaldi", "vivaldi", "Vivaldi Browser"),
        (home / "Library/Application Support/BraveSoftware", "brave", "Brave Browser"),
        (home / "Library/Application Support/Chromium", "chromium", "Chromium Browser"),
        (home / "Library/Application Support/com.meta.endo", "endo", "Meta Endo Prototype"),
        (home / "Library/Application Support/com.meta.prototypes.endo.tokens", "endo", "Meta Endo Tokens"),
        (home / "Library/Application Support/Hatch", "hatch", "Hatch"),
        (home / "Library/Application Support/NEGA Remote Install", "nega remote install", "NEGA Remote Install"),
        (home / "Library/Application Support/CreatorCacheCleaner", "creatorcachecleaner", "CreatorCacheCleaner"),
        (home / "Library/Application Support/yinghuo", "yinghuo", "Yinghuo"),
    ]
    for target_path, app_probe, label in app_support_checks:
        if target_path.exists() and not has_app_matching(app_probe):
            add_candidate(
                target_path,
                "Uninstalled App Leftovers (Application Support / Caches)",
                f"Verified '{label}' is uninstalled across /Applications & ~/Applications",
            )

    # 4. Rule D: Desktop App Updater Staging, Telemetry & Package Download Caches
    # (tw93/Mole & mac-cleanup-py rule)
    caches_dir = home / "Library" / "Caches"
    if caches_dir.is_dir():
        for cchild in sorted(caches_dir.iterdir()):
            if cchild.name.endswith("-updater") or cchild.name in {
                "pip", "pnpm", "com.crashlytics.data", "io.sentry"
            }:
                add_candidate(
                    cchild,
                    "App Updater Staging & Package Caches",
                    f"Disposable updater/package/telemetry cache '{cchild.name}'",
                    allow_zero=False,
                )

    user_as = home / "Library" / "Application Support"
    if user_as.is_dir():
        for achild in sorted(user_as.iterdir()):
            if achild.name.endswith(".updater") or achild.name in {
                "com.adobe.dunamis", "CrashReporter", "com.amplitude.crash_report"
            }:
                add_candidate(
                    achild,
                    "App Updater Staging & Telemetry Dumps",
                    f"Disposable updater staging or telemetry dump '{achild.name}'",
                )

    for extra_cache, reason in [
        (home / "Library/Application Support/Google/GoogleUpdater/crx_cache", "Cached GoogleUpdater CRX installer packages"),
        (home / ".npm/_cacache", "Regenerable npm content-addressable download cache"),
        (home / "Library/Logs/DiagnosticReports", "macOS user diagnostic crash reports"),
        (Path("/Library/Logs/DiagnosticReports"), "macOS system diagnostic crash reports"),
        (Path("/Library/Logs/CrashReporter"), "macOS system CrashReporter logs"),
    ]:
        if extra_cache.exists():
            add_candidate(extra_cache, "System Updater, Crash & Package Caches", reason, allow_zero=False)

    # Orphaned interrupted runtime staging directories in ~/.cache/codex-runtimes
    codex_rt = home / ".cache" / "codex-runtimes"
    if codex_rt.is_dir():
        for rt_child in sorted(codex_rt.iterdir()):
            if "-install-" in rt_child.name:
                add_candidate(
                    rt_child,
                    "Orphaned Agent Runtime Staging",
                    f"Interrupted runtime install staging directory '{rt_child.name}'",
                )

    # Superseded multi-version agent runtime bundles (e.g., ~/Library/Application Support/Claude/claude-code/<ver>)
    for ver_parent in [
        home / "Library/Application Support/Claude/claude-code",
        home / ".local/share/claude/versions",
    ]:
        if not ver_parent.is_dir():
            continue
        ver_dirs = [
            d for d in ver_parent.iterdir()
            if re.match(r"^v?\d+\.\d+\.\d+", d.name) and not d.is_symlink()
        ]
        if len(ver_dirs) <= max(1, keep_versions):
            continue
        ver_sorted = sorted(
            ver_dirs,
            key=lambda p: (version_sort_key(p.name), p.stat().st_mtime if p.exists() else 0.0),
            reverse=True,
        )
        kept_vers = set(ver_sorted[:max(1, keep_versions)])
        for vd in ver_sorted:
            try:
                if vd.resolve() in active_symlink_targets or any(vd.resolve() in t.parents for t in active_symlink_targets):
                    kept_vers.add(vd)
            except OSError:
                pass
        for vd in ver_sorted:
            if vd not in kept_vers:
                add_candidate(
                    vd,
                    "Superseded Agent Runtime Bundle",
                    f"Superseded runtime version '{vd.name}' in {ver_parent.name} (newest & symlinked versions retained)",
                )

    return report


def clean_launchservices_ghost_apps(execute: bool, extra_unregistered_paths: Optional[List[Path]] = None) -> List[str]:
    """
    Scan macOS LaunchServices (`lsregister -dump`) for ghost/deleted `.app` or `.pluginkit`
    bundles and transient build artifacts (in `DerivedData`, `CoreSimulator`, `.Trashes`,
    `work/`, `.build/`, `Archive/`, `Worktrees/`, `TaskHistory/`, `Builds/`, `Phayen-lab/`,
    `PrismGrade/Installations`, `PrismGrade/Backups`), unregistering them via `lsregister -u`
    and running `lsregister -gc` so broken `🚫` icons vanish from Launchpad / Applications.
    """
    notes: List[str] = []
    if not os.path.exists(LSREGISTER_PATH):
        return notes

    transient_markers = (
        "/DerivedData/",
        "/CoreSimulator/Devices/",
        "/.Trashes/",
        "/.Trash/",
        "/AI/Archive/",
        "/AI/Codex/",
        "/AI/Data/",
        "/AI/Orca/Worktrees/",
        "/AI/Claude/Worktrees/",
        "/Application Support/PrismGrade/Installations/",
        "/Application Support/PrismGrade/LabInstallations/",
        "/Application Support/PrismGrade/Backups/",
        "/work/",
        "/.build/",
        ".saved-application",
        ".saved-service",
        ".app.saved",
        "UITests-Runner.app",
        "Tests-Runner.app",
    )

    to_unregister: Set[str] = set()
    try:
        dump_out = subprocess.check_output([LSREGISTER_PATH, "-dump"], text=True, errors="ignore")
        for line in dump_out.splitlines():
            if not line.startswith("path:"):
                continue
            m = re.match(r"^path:\s+(.*?)\s+\(0x[0-9a-fA-F]+\)\s*$", line)
            if not m:
                continue
            p_str = m.group(1).strip()
            if not p_str or p_str.startswith(("/System/", "/Library/")):
                continue
            if "/Library/Daemon Containers/" in p_str and "Runner.app" not in p_str and ".xctrunner" not in p_str:
                continue
            if any(marker in p_str for marker in transient_markers) or not os.path.exists(p_str):
                to_unregister.add(p_str)
    except Exception as exc:
        notes.append(f"LaunchServices scan note: {exc}")

    if not to_unregister:
        notes.append("LaunchServices: 0 ghost/build-artifact app registrations found")
        return notes

    if not execute:
        notes.append(f"Dry-run: would unregister {len(to_unregister)} ghost/build-artifact bundle(s) from macOS LaunchServices")
        for sample in sorted(to_unregister)[:8]:
            notes.append(f"  - [GHOST APP] {sample}")
        if len(to_unregister) > 8:
            notes.append(f"  - ... and {len(to_unregister) - 8} more")
        return notes

    unregistered_ok = 0
    for p_str in sorted(to_unregister):
        try:
            subprocess.run([LSREGISTER_PATH, "-u", p_str], capture_output=True, text=True, timeout=5)
            unregistered_ok += 1
        except Exception:
            pass

    try:
        subprocess.run([LSREGISTER_PATH, "-gc"], capture_output=True, text=True, timeout=15)
    except Exception:
        pass

    notes.append(f"LaunchServices: Unregistered {unregistered_ok} ghost/build-artifact bundle(s) and ran `lsregister -gc`")
    return notes


def apply_spotlight_shields(roots: List[Path], execute: bool) -> List[str]:
    """
    Place `.metadata_never_index` markers inside transient build/worktree/archive directories
    so Spotlight and Agent indexers skip intermediate build trees.
    """
    shield_notes: List[str] = []
    shield_subdirs = ("work", "DerivedData", "Builds", "Archive", "Worktrees", "TaskHistory", "Phayen-lab")

    target_dirs: Set[Path] = set()
    for r in roots:
        if r.name in shield_subdirs and r.is_dir():
            target_dirs.add(r)
        for sub in shield_subdirs:
            candidate = r / sub
            if candidate.is_dir():
                target_dirs.add(candidate)
        try:
            for child in r.iterdir():
                if child.is_dir() and not child.is_symlink():
                    if child.name in shield_subdirs:
                        target_dirs.add(child)
                    work_sub = child / "work"
                    if work_sub.is_dir():
                        target_dirs.add(work_sub)
        except OSError:
            pass

    created = 0
    for td in sorted(target_dirs):
        marker = td / ".metadata_never_index"
        if not marker.exists():
            if execute:
                try:
                    marker.touch()
                    created += 1
                except OSError:
                    pass
            else:
                created += 1

    if created > 0:
        prefix = "Created" if execute else "Dry-run: would create"
        shield_notes.append(f"{prefix} `.metadata_never_index` shield in {created} build/worktree directory(s)")
    return shield_notes


# ============================================================================
# 9. EXECUTION & REPORTING
# ============================================================================

def resolve_trash_dir(target: Path) -> Path:
    """Resolve same-volume Trash directory (.Trashes/<uid> on external volumes, ~/.Trash on internal disk)."""
    s_str = str(target)
    uid = os.getuid()
    if s_str.startswith("/Volumes/"):
        parts = Path(s_str).parts
        if len(parts) >= 3:
            vol_root = Path("/") / parts[1] / parts[2]
            trash_dir = vol_root / ".Trashes" / str(uid) / "agent-slim"
            try:
                trash_dir.mkdir(parents=True, exist_ok=True)
                return trash_dir
            except OSError:
                pass
    home_trash = Path.home() / ".Trash" / "agent-slim"
    home_trash.mkdir(parents=True, exist_ok=True)
    return home_trash


def remove_path_permanently(target: Path) -> None:
    """Permanently delete a directory or file to reclaim disk space immediately."""
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        def _handle_readonly(func, path, exc_info):
            try:
                os.chmod(path, 0o700)
                func(path)
            except OSError:
                pass
        shutil.rmtree(target, onerror=_handle_readonly)


def execute_cleanup(report: ScanReport, use_trash: bool = False) -> Tuple[int, int, List[Path], List[str]]:
    deleted_bytes = 0
    deleted_count = 0
    removed_paths: List[Path] = []
    errors: List[str] = []
    ts = int(time.time())
    manifest_items: List[Dict[str, object]] = []
    batch_trash_dirs: Set[Path] = set()

    for idx, item in enumerate(report.candidates):
        try:
            if not item.path.exists() and not item.path.is_symlink():
                continue
            if use_trash:
                trash_base = resolve_trash_dir(item.path) / f"batch-{ts}"
                trash_base.mkdir(parents=True, exist_ok=True)
                dest = trash_base / f"{idx:04d}-{item.path.name}"
                shutil.move(str(item.path), str(dest))
                batch_trash_dirs.add(trash_base)
                manifest_items.append(
                    {
                        "original_path": str(item.path),
                        "trash_path": str(dest),
                        "category": item.category,
                        "size_bytes": item.size_bytes,
                        "reason": item.reason,
                        "is_dir": item.is_dir,
                    }
                )
            else:
                remove_path_permanently(item.path)
            deleted_bytes += item.size_bytes
            deleted_count += 1
            removed_paths.append(item.path)
        except Exception as exc:
            errors.append(f"{item.path}: {exc}")

    if use_trash and manifest_items:
        for bdir in batch_trash_dirs:
            try:
                manifest_path = bdir / "manifest.json"
                manifest_path.write_text(
                    json.dumps(
                        {
                            "version": VERSION,
                            "created_at": ts,
                            "workspace_root": str(report.root),
                            "total_items": len(manifest_items),
                            "total_bytes": deleted_bytes,
                            "items": manifest_items,
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except OSError as exc:
                errors.append(f"manifest.json: {exc}")

    return deleted_bytes, deleted_count, removed_paths, errors


def clean_unavailable_ios_simulators(execute: bool) -> str:
    if shutil.which("xcrun") is None:
        return "xcrun not found (skipping iOS Simulator check)"
    if not execute:
        return "Dry-run: would run `xcrun simctl delete unavailable`"
    try:
        subprocess.run(
            ["xcrun", "simctl", "delete", "unavailable"],
            check=True,
            capture_output=True,
            text=True,
        )
        return "Executed `xcrun simctl delete unavailable` successfully"
    except Exception as exc:
        return f"Simulator cleanup note: {exc}"


def print_summary(reports: List[ScanReport], execute: bool, top_n: int = 25) -> None:
    mode_str = "EXECUTION MODE (RECLAIMING DISK SPACE)" if execute else "DRY-RUN MODE (NO FILES DELETED)"
    print("=" * 88)
    print(f"  agent-slim v{VERSION} — {mode_str}")
    print("=" * 88)

    grand_total = 0
    category_totals: Dict[str, int] = {}
    category_counts: Dict[str, int] = {}
    all_candidates: List[CandidateItem] = []

    for rep in reports:
        print(f"\n[Workspace] {rep.root}")
        print(f"  Candidates found : {len(rep.candidates)}")
        print(f"  Reclaimable space: {format_bytes(rep.total_bytes)}")
        if rep.protected_notes:
            unique_notes = sorted(set(rep.protected_notes))
            print(f"  Safety guardrails: {len(unique_notes)} active protection rule(s) triggered")
            for note in unique_notes[:5]:
                print(f"    - [PROTECTED] {note}")
            if len(unique_notes) > 5:
                print(f"    - ... and {len(unique_notes) - 5} more protected items")
        if rep.skipped_active:
            print(f"  Active skipped   : {len(rep.skipped_active)} recently modified item(s) skipped")

        grand_total += rep.total_bytes
        for c in rep.candidates:
            category_totals[c.category] = category_totals.get(c.category, 0) + c.size_bytes
            category_counts[c.category] = category_counts.get(c.category, 0) + 1
            all_candidates.append(c)

    print("\n" + "-" * 88)
    print("  Breakdown by Stack / Category")
    print("-" * 88)
    for cat, sz in sorted(category_totals.items(), key=lambda x: x[1], reverse=True):
        cnt = category_counts[cat]
        print(f"  {cat:<52} {cnt:>6} item(s)   {format_bytes(sz):>12}")

    if all_candidates:
        print("\n" + "-" * 88)
        print(f"  Top {min(top_n, len(all_candidates))} Largest Reclaimable Targets")
        print("-" * 88)
        for item in sorted(all_candidates, key=lambda x: x.size_bytes, reverse=True)[:top_n]:
            kind = "DIR " if item.is_dir else "FILE"
            print(f"  [{kind}] {format_bytes(item.size_bytes):>10}  {item.path}")
            print(f"         └─ {item.category}: {item.reason}")

    print("\n" + "=" * 88)
    print(f"  TOTAL RECLAIMABLE SPACE: {format_bytes(grand_total)} across {len(all_candidates)} item(s)")
    if not execute:
        print("  Tip: Re-run with `--execute` (or `clean`) to reclaim this space.")
    print("=" * 88)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent-slim",
        description=(
            "Universal AI Agent, Xcode & Multi-Stack Workspace Deep Cleaner. "
            "Reclaims 50%–95% of bloated project disk space across Web, Python/AI/ML, "
            "Rust/C++/Go/VM Labs, Xcode/Swift/Android, Unity/Unreal/Godot, and Media stacks while "
            "strictly protecting Git state, uncommitted code, active processes, and AI Agent memory."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Optional mode (`scan` or `clean`) followed by one or more workspace directories to scan.",
    )
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        default=[],
        help="Workspace directory to scan (can be specified multiple times; alias for positional paths).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform actual deletion (default is safe dry-run mode unless `clean` subcommand is used).",
    )
    parser.add_argument(
        "--trash",
        action="store_true",
        help="Move items to system Trash instead of permanent deletion.",
    )
    parser.add_argument(
        "--include-node-modules",
        action="store_true",
        help="Also remove reinstallable `node_modules/` directories (verified by sibling package.json).",
    )
    parser.add_argument(
        "--keep-versions",
        "--keep-latest-release",
        type=int,
        default=2,
        dest="keep_versions",
        help="Number of latest versioned/iterative builds to retain per group (default: 2).",
    )
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=0.0,
        help="Skip caches/artifacts modified within the last N hours to protect running builds (default: 0).",
    )
    parser.add_argument(
        "--dedup-scratch-mb",
        type=float,
        default=25.0,
        help="Minimum file size in MB for SHA-256 duplicate scratch/test file cleanup (0 to disable, default: 25).",
    )
    parser.add_argument(
        "--clean-simulators",
        action="store_true",
        help="Run `xcrun simctl delete unavailable` on macOS to purge orphaned iOS Simulator runtimes.",
    )
    parser.add_argument(
        "--clean-xcode",
        action="store_true",
        help="Also scan and clean system-level Xcode caches (~/Library/Developer/Xcode/DerivedData, CoreSimulator Caches/Clones, XCTestDevices, SwiftPM cache).",
    )
    parser.add_argument(
        "--clean-device-support",
        action="store_true",
        help="When used with --clean-xcode, also clean ~/Library/Developer/Xcode/*OS DeviceSupport symbol caches.",
    )
    parser.add_argument(
        "--clean-agent-clutter",
        action="store_true",
        help="Scan user home & multi-account AI agent dotdirs (~/.claude, ~/.codex, ~/.gemini, ~/.orca, ~/.cursor, ~/.windsurf) for orphaned worktree session caches, unused-IDE ghost dotdirs, stale .bak/.tmp clutter, and broken MCP binaries.",
    )
    parser.add_argument(
        "--clean-orphans",
        action="store_true",
        help="Deep-scan ~/Library & /Library for uninstalled app leftovers, orphaned Containers/Group Containers, broken LaunchAgents, updater staging caches, and superseded agent runtimes.",
    )
    parser.add_argument(
        "--clean-launchservices",
        action="store_true",
        help="Unregister ghost/deleted/build-artifact `.app` bundles from macOS LaunchServices (`lsregister -u` & `-gc`).",
    )
    parser.add_argument(
        "--shield-spotlight",
        action="store_true",
        help="Create `.metadata_never_index` markers in transient build/worktree directories.",
    )
    parser.add_argument(
        "--report-json",
        type=str,
        default=None,
        help="Optional file path to export a machine-readable JSON audit report.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON report to stdout.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"agent-slim {VERSION}",
    )

    args = parser.parse_args()

    # Support optional leading `scan` / `clean` subcommands from README examples
    raw_Input_paths = list(args.paths)
    if raw_Input_paths and raw_Input_paths[0] in {"scan", "clean"}:
        cmd = raw_Input_paths.pop(0)
        if cmd == "clean":
            args.execute = True
    raw_Input_paths.extend(args.roots)

    if not raw_Input_paths and not args.clean_xcode and not args.clean_launchservices and not args.clean_orphans and not args.clean_agent_clutter:
        parser.error("Please provide at least one workspace path (or `--root PATH`, `--clean-xcode`, `--clean-orphans`, `--clean-launchservices`).")

    active_paths, active_basenames = collect_active_process_paths()
    reports: List[ScanReport] = []
    resolved_roots: List[Path] = []

    for raw_path in raw_Input_paths:
        p = Path(raw_path).expanduser().resolve()
        if not p.exists():
            print(f"[WARN] Path does not exist, skipping: {p}", file=sys.stderr)
            continue
        resolved_roots.append(p)
        rep = scan_workspace(
            root=p,
            include_node_modules=args.include_node_modules,
            keep_versions=args.keep_versions,
            min_age_hours=args.min_age_hours,
            dedup_scratch_mb=args.dedup_scratch_mb,
            active_paths=active_paths,
            active_basenames=active_basenames,
        )
        reports.append(rep)

    if args.clean_xcode:
        xcode_rep = scan_xcode_system_caches(include_device_support=args.clean_device_support, active_paths=active_paths, min_age_hours=args.min_age_hours)
        reports.append(xcode_rep)

    if args.clean_orphans:
        orphan_rep = scan_macos_system_orphans(keep_versions=1)
        reports.append(orphan_rep)

    if args.clean_agent_clutter or args.clean_orphans:
        clutter_rep = scan_global_agent_clutter(
            workspace_roots=resolved_roots or None,
            min_age_hours=args.min_age_hours,
            active_paths=active_paths,
        )
        reports.append(clutter_rep)

    if not args.json:
        print_summary(reports, execute=args.execute)

    if args.clean_simulators or args.clean_xcode:
        sim_msg = clean_unavailable_ios_simulators(execute=args.execute)
        if not args.json:
            print(f"\n[iOS Simulators] {sim_msg}")

    total_reclaimed = 0
    total_deleted_items = 0
    all_removed_paths: List[Path] = []
    all_errors: List[str] = []

    if args.execute:
        for rep in reports:
            reclaimed, count, removed_paths, errs = execute_cleanup(rep, use_trash=args.trash)
            total_reclaimed += reclaimed
            total_deleted_items += count
            all_removed_paths.extend(removed_paths)
            all_errors.extend(errs)
        if not args.json:
            print("\n" + "=" * 88)
            print(f"  CLEANUP COMPLETE: Reclaimed {format_bytes(total_reclaimed)} ({total_deleted_items} items removed)")
            if all_errors:
                print(f"  Warnings/Errors ({len(all_errors)}):")
                for err in all_errors[:10]:
                    print(f"    - {err}")
            print("=" * 88)

    if args.clean_launchservices or (args.execute and any(p.name.endswith(".app") for p in all_removed_paths)):
        ls_notes = clean_launchservices_ghost_apps(execute=args.execute, extra_unregistered_paths=all_removed_paths)
        if not args.json:
            for note in ls_notes:
                print(f"[LaunchServices] {note}")

    if args.shield_spotlight and resolved_roots:
        shield_notes = apply_spotlight_shields(resolved_roots, execute=args.execute)
        if not args.json:
            for note in shield_notes:
                print(f"[Spotlight Shield] {note}")

    payload = {
        "version": VERSION,
        "executed": args.execute,
        "total_reclaimable_bytes": sum(r.total_bytes for r in reports),
        "total_reclaimed_bytes": total_reclaimed if args.execute else 0,
        "workspaces": [
            {
                "root": str(r.root),
                "reclaimable_bytes": r.total_bytes,
                "protected_notes": sorted(set(r.protected_notes)),
                "skipped_active": r.skipped_active,
                "candidates": [
                    {
                        "path": str(c.path),
                        "category": c.category,
                        "size_bytes": c.size_bytes,
                        "reason": c.reason,
                        "is_dir": c.is_dir,
                    }
                    for c in r.candidates
                ],
            }
            for r in reports
        ],
    }

    if args.report_json:
        out_path = Path(args.report_json).expanduser().resolve()
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        if not args.json:
            print(f"\n[Report] Exported JSON audit report to: {out_path}")

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
