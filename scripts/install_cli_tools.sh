#!/usr/bin/env zsh
# ==============================================================================
# CLI toolchain installer (Apple Silicon & Intel Macs, no Homebrew needed)
# Installs rg, fd, ast-grep (+ sg), just, xcbeautify, restic and gh into ~/.local/bin.
#
# Every download is pinned: scripts/cli-tools.lock names the exact release, asset and SHA-256 per
# architecture, and the file is checked with `shasum -a 256 -c` before anything is installed.
#
#   install_cli_tools.sh               install the tools that are missing (fast path when all exist)
#   install_cli_tools.sh --upgrade     reinstall every tool at the version in the lock file
#   install_cli_tools.sh --print-lock  (maintainers) print a lock file for the latest releases, built
#                                      from the SHA-256 digests GitHub publishes for each asset:
#                                      scripts/install_cli_tools.sh --print-lock > scripts/cli-tools.lock
# ==============================================================================
set -euo pipefail
setopt extended_glob

SCRIPT_DIR="${0:A:h}"
LOCK_FILE="${CLI_TOOLS_LOCK:-$SCRIPT_DIR/cli-tools.lock}"

# tool  GitHub repository  asset suffix (arm64)  asset suffix (x86_64)
TOOL_SPECS=(
  "gh cli/cli macOS_arm64.zip macOS_amd64.zip"
  "rg BurntSushi/ripgrep aarch64-apple-darwin.tar.gz x86_64-apple-darwin.tar.gz"
  "fd sharkdp/fd aarch64-apple-darwin.tar.gz x86_64-apple-darwin.tar.gz"
  "ast-grep ast-grep/ast-grep app-aarch64-apple-darwin.zip app-x86_64-apple-darwin.zip"
  "just casey/just aarch64-apple-darwin.tar.gz x86_64-apple-darwin.tar.gz"
  "xcbeautify cpisciotta/xcbeautify arm64-apple-macosx.zip x86_64-apple-macosx.zip"
  "restic restic/restic darwin_arm64.bz2 darwin_amd64.bz2"
)

UPGRADE=false
PRINT_LOCK=false
for arg in "$@"; do
  case "$arg" in
    --upgrade|-u) UPGRADE=true ;;
    --print-lock) PRINT_LOCK=true ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
  esac
done

export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# --- Maintainer: print a refreshed lock file --------------------------------------------------
release_json() {
  local repo="$1" out="$2"
  if ! command -v gh >/dev/null 2>&1 || ! gh api "repos/$repo/releases/latest" > "$out" 2>/dev/null; then
    curl --proto '=https' --proto-redir '=https' -fsSL "https://api.github.com/repos/$repo/releases/latest" -o "$out"
  fi
}

if [[ "$PRINT_LOCK" == "true" ]]; then
  print -r -- "# Pinned CLI tools for scripts/install_cli_tools.sh (generated $(date -u +%Y-%m-%d))."
  print -r -- "# Refresh: scripts/install_cli_tools.sh --print-lock > scripts/cli-tools.lock, then review the diff."
  print -r -- "# tool  repository  tag  arch  asset  sha256"
  for spec in "${TOOL_SPECS[@]}"; do
    fields=(${=spec})
    release_json "${fields[2]}" "$TMP_DIR/release.json"
    python3 - "$TMP_DIR/release.json" "${fields[1]}" "${fields[2]}" "${fields[3]}" "${fields[4]}" <<'PYLOCK'
import json, re, sys
path, tool, repo, suffix_arm, suffix_x86 = sys.argv[1:6]
with open(path) as stream:
    release = json.load(stream)
for arch, suffix in (("arm64", suffix_arm), ("x86_64", suffix_x86)):
    assets = [a for a in release["assets"] if a["name"].endswith(suffix)]
    if len(assets) != 1:
        sys.exit(f"{repo}: expected exactly one asset ending in {suffix}, found {len(assets)}")
    digest = assets[0].get("digest") or ""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        sys.exit(f"{repo}: asset {assets[0]['name']} has no published SHA-256 digest")
    print(f"{tool} {repo} {release['tag_name']} {arch} {assets[0]['name']} {digest[7:]}")
PYLOCK
  done
  exit 0
fi

# --- Install ----------------------------------------------------------------------------------
mkdir -p ~/.local/bin

# Link existing restic from Codex Storage/bin if present and not upgrading
if [[ "$UPGRADE" == "false" && ! -x "$HOME/.local/bin/restic" && -x "$HOME/Library/Application Support/Codex Storage/bin/restic" ]]; then
  ln -sf "$HOME/Library/Application Support/Codex Storage/bin/restic" "$HOME/.local/bin/restic"
fi

# Fast path: every tool is already installed (e.g. after --restore-from-ssd) and no --upgrade
ALL_PRESENT=true
for spec in "${TOOL_SPECS[@]}"; do
  if ! command -v "${spec%% *}" >/dev/null 2>&1; then
    ALL_PRESENT=false
    break
  fi
done
if [[ "$UPGRADE" == "false" && "$ALL_PRESENT" == "true" ]]; then
  echo "⚡ All CLI tools (rg, fd, ast-grep, just, xcbeautify, restic, gh) are already installed"
  exit 0
fi

[[ -f "$LOCK_FILE" ]] || { print -u2 -- "❌ Lock file not found: $LOCK_FILE"; exit 1; }
LOCK_ARCH="$(uname -m)"
[[ "$LOCK_ARCH" == "arm64" ]] || LOCK_ARCH="x86_64"

need_install() {
  [[ "$UPGRADE" == "true" ]] || ! command -v "$1" >/dev/null 2>&1
}

install_bin() {
  local src="$1"
  local dst="$2"
  local staged
  staged="$(mktemp "${dst}.XXXXXX")"
  if cp "$src" "$staged" && chmod 755 "$staged" && mv -f "$staged" "$dst"; then
    return 0
  fi
  rm -f "$staged"
  return 1
}

# download_pinned <tool> <out-file> — fetch the locked asset and verify its SHA-256
download_pinned() {
  local tool="$1" out_file="$2" line
  local -a entry
  line="$(/usr/bin/awk -v t="$tool" -v a="$LOCK_ARCH" '$1 == t && $4 == a { print; exit }' "$LOCK_FILE")"
  entry=(${=line})
  if (( ${#entry[@]} != 6 )) || [[ "${entry[6]}" != [0-9a-f](#c64) ]]; then
    print -u2 -- "❌ No valid entry for $tool ($LOCK_ARCH) in $LOCK_FILE"
    return 1
  fi
  echo "Installing $tool ${entry[3]} (${entry[5]})..."
  curl --proto '=https' --proto-redir '=https' -fsSL \
    "https://github.com/${entry[2]}/releases/download/${entry[3]}/${entry[5]}" -o "$out_file"
  if ! print -r -- "${entry[6]}  $out_file" | /usr/bin/shasum -a 256 -c --status -; then
    print -u2 -- "SHA-256 mismatch for $tool (${entry[5]}): expected ${entry[6]}"
    return 1
  fi
}

if need_install gh; then
  download_pinned gh "$TMP_DIR/gh.zip"
  unzip -oq "$TMP_DIR/gh.zip" -d "$TMP_DIR/gh-out"
  install_bin "$(echo "$TMP_DIR"/gh-out/gh_*/bin/gh)" ~/.local/bin/gh
fi

if need_install rg; then
  download_pinned rg "$TMP_DIR/rg.tar.gz"
  tar -xzf "$TMP_DIR/rg.tar.gz" -C "$TMP_DIR"
  install_bin "$(echo "$TMP_DIR"/ripgrep-*/rg)" ~/.local/bin/rg
fi

if need_install fd; then
  download_pinned fd "$TMP_DIR/fd.tar.gz"
  tar -xzf "$TMP_DIR/fd.tar.gz" -C "$TMP_DIR"
  install_bin "$(echo "$TMP_DIR"/fd-*/fd)" ~/.local/bin/fd
fi

if need_install ast-grep; then
  download_pinned ast-grep "$TMP_DIR/ast-grep.zip"
  unzip -oq "$TMP_DIR/ast-grep.zip" -d "$TMP_DIR/ast-grep-out"
  install_bin "$TMP_DIR/ast-grep-out/ast-grep" ~/.local/bin/ast-grep
  install_bin "$TMP_DIR/ast-grep-out/sg" ~/.local/bin/sg
fi

if need_install just; then
  download_pinned just "$TMP_DIR/just.tar.gz"
  mkdir -p "$TMP_DIR/just-out"
  tar -xzf "$TMP_DIR/just.tar.gz" -C "$TMP_DIR/just-out"
  install_bin "$TMP_DIR/just-out/just" ~/.local/bin/just
fi

if need_install xcbeautify; then
  download_pinned xcbeautify "$TMP_DIR/xcbeautify.zip"
  unzip -oq "$TMP_DIR/xcbeautify.zip" -d "$TMP_DIR/xcbeautify-out"
  install_bin "$TMP_DIR/xcbeautify-out/xcbeautify" ~/.local/bin/xcbeautify
fi

if need_install restic; then
  download_pinned restic "$TMP_DIR/restic.bz2"
  bunzip2 -f "$TMP_DIR/restic.bz2"
  install_bin "$TMP_DIR/restic" ~/.local/bin/restic
fi

echo "✅ CLI toolchain ready in ~/.local/bin"
