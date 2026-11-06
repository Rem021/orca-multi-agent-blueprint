#!/usr/bin/env zsh
# ==============================================================================
# Zero-Dependency Native CLI Toolchain Installer (Apple Silicon & Intel Mac)
# Works on brand-new Macs WITHOUT requiring Homebrew or `gh` CLI login!
# Uses direct GitHub Releases API via curl + python3, with `gh` fast-path.
# ==============================================================================
set -euo pipefail

mkdir -p ~/.local/bin
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

# Instant fast-path: if all 7 CLI binaries are already installed (e.g. after --restore-from-ssd), exit in 0.0s
ALL_PRESENT=true
for tool in rg fd ast-grep sg just xcbeautify scc repomix; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    ALL_PRESENT=false
    break
  fi
done
if [[ "$ALL_PRESENT" == "true" ]]; then
  echo "⚡ Fast-path: All 7 native CLI tools (rg, fd, ast-grep, just, xcbeautify, scc, repomix) already ready in ~/.local/bin (0.0s)"
  exit 0
fi

ARCH="$(uname -m)"
if [[ "$ARCH" == "arm64" ]]; then
  RUST_ARCH="aarch64-apple-darwin"
  SWIFT_ARCH="arm64-apple-macosx"
  GO_ARCH="arm64"
else
  RUST_ARCH="x86_64-apple-darwin"
  SWIFT_ARCH="x86_64-apple-macosx"
  GO_ARCH="x86_64"
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

need_install() {
  ! command -v "$1" >/dev/null 2>&1
}

download_release_asset() {
  local repo="$1"
  local substr="$2"
  local out_file="$3"

  # Method 1: Try `gh` CLI if authenticated
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    if gh release download -R "$repo" -p "*${substr}*" -O "$out_file" --clobber 2>/dev/null; then
      return 0
    fi
  fi

  # Method 2: Universal zero-dependency fallback via GitHub REST API + curl + python3
  local url
  url="$(curl -fsSL "https://api.github.com/repos/${repo}/releases/latest" | python3 -c "
import json, sys
data = json.load(sys.stdin)
substr = sys.argv[1]
for asset in data.get('assets', []):
    if substr in asset.get('name', ''):
        print(asset['browser_download_url'])
        sys.exit(0)
sys.exit(1)
" "$substr")"
  curl -fsSL -L "$url" -o "$out_file"
}

if need_install rg; then
  echo "Installing ripgrep (rg) for ${RUST_ARCH}..."
  download_release_asset "BurntSushi/ripgrep" "${RUST_ARCH}.tar.gz" "$TMP_DIR/rg.tar.gz"
  tar -xzf "$TMP_DIR/rg.tar.gz" -C "$TMP_DIR"
  cp -f "$TMP_DIR"/ripgrep-*/rg ~/.local/bin/rg
fi

if need_install fd; then
  echo "Installing fd for ${RUST_ARCH}..."
  download_release_asset "sharkdp/fd" "${RUST_ARCH}.tar.gz" "$TMP_DIR/fd.tar.gz"
  tar -xzf "$TMP_DIR/fd.tar.gz" -C "$TMP_DIR"
  cp -f "$TMP_DIR"/fd-*/fd ~/.local/bin/fd
fi

if need_install ast-grep; then
  echo "Installing ast-grep for ${RUST_ARCH}..."
  download_release_asset "ast-grep/ast-grep" "app-${RUST_ARCH}.zip" "$TMP_DIR/ast-grep.zip"
  unzip -oq "$TMP_DIR/ast-grep.zip" -d "$TMP_DIR/ast-grep-out"
  cp -f "$TMP_DIR/ast-grep-out/ast-grep" ~/.local/bin/ast-grep
  cp -f "$TMP_DIR/ast-grep-out/sg" ~/.local/bin/sg
fi

if need_install just; then
  echo "Installing just for ${RUST_ARCH}..."
  download_release_asset "casey/just" "${RUST_ARCH}.tar.gz" "$TMP_DIR/just.tar.gz"
  tar -xzf "$TMP_DIR/just.tar.gz" -C "$TMP_DIR"
  cp -f "$TMP_DIR/just" ~/.local/bin/just
fi

if need_install xcbeautify; then
  echo "Installing xcbeautify for ${SWIFT_ARCH}..."
  download_release_asset "cpisciotta/xcbeautify" "${SWIFT_ARCH}.zip" "$TMP_DIR/xcbeautify.zip"
  unzip -oq "$TMP_DIR/xcbeautify.zip" -d "$TMP_DIR"
  cp -f "$TMP_DIR/xcbeautify" ~/.local/bin/xcbeautify
fi

if need_install scc; then
  echo "Installing scc for Darwin_${GO_ARCH}..."
  download_release_asset "boyter/scc" "scc_Darwin_${GO_ARCH}.tar.gz" "$TMP_DIR/scc.tar.gz"
  tar -xzf "$TMP_DIR/scc.tar.gz" -C "$TMP_DIR"
  cp -f "$TMP_DIR/scc" ~/.local/bin/scc
fi

if need_install repomix && command -v npm >/dev/null 2>&1; then
  echo "Installing repomix..."
  mkdir -p ~/.local/share/repomix
  npm install --prefix ~/.local/share/repomix repomix --no-audit --no-fund --silent
  ln -sf ~/.local/share/repomix/node_modules/.bin/repomix ~/.local/bin/repomix
fi

chmod +x ~/.local/bin/rg ~/.local/bin/fd ~/.local/bin/ast-grep ~/.local/bin/sg ~/.local/bin/just ~/.local/bin/xcbeautify ~/.local/bin/scc 2>/dev/null || true
xattr -dr com.apple.quarantine ~/.local/bin/rg ~/.local/bin/fd ~/.local/bin/ast-grep ~/.local/bin/sg ~/.local/bin/just ~/.local/bin/xcbeautify ~/.local/bin/scc 2>/dev/null || true
echo "✅ CLI toolchain ready in ~/.local/bin"
