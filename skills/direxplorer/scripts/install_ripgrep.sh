#!/usr/bin/env bash
# Install ripgrep (the `rg` binary) that direxplorer's Grep tool depends on.
#
# Run by `evonic doctor --fix` when `rg` is missing. Strategy:
#   - macOS with Homebrew  -> `brew install ripgrep`
#   - otherwise            -> download the matching release from
#                             https://github.com/BurntSushi/ripgrep/releases
#                             and install `rg` into a PATH directory.
#
# Best-effort and noisy on purpose; exits non-zero on failure so the doctor
# reports it.
set -euo pipefail

REPO="${RIPGREP_RELEASE_REPO:-BurntSushi/ripgrep}"

log() { echo "[install_ripgrep] $*"; }

if command -v rg >/dev/null 2>&1; then
  log "rg already installed at $(command -v rg) ($(rg --version | head -n1))"
  exit 0
fi

os="$(uname -s)"
arch="$(uname -m)"

# Prefer Homebrew on macOS.
if [ "$os" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
  log "Installing ripgrep via Homebrew…"
  brew install ripgrep
  command -v rg >/dev/null 2>&1 && { log "Installed $(rg --version | head -n1)"; exit 0; }
  log "brew install completed but rg not on PATH; falling back to release download."
fi

# Map host to a ripgrep release target triple.
target=""
case "$os/$arch" in
  Darwin/arm64|Darwin/aarch64) target="aarch64-apple-darwin" ;;
  Darwin/x86_64)               target="x86_64-apple-darwin" ;;
  Linux/x86_64|Linux/amd64)    target="x86_64-unknown-linux-musl" ;;
  Linux/aarch64|Linux/arm64)   target="aarch64-unknown-linux-gnu" ;;
  *) log "Unsupported platform: $os/$arch. Install ripgrep manually from https://github.com/$REPO/releases"; exit 1 ;;
esac
log "Host target: $target"

# Resolve the latest release tag from the GitHub API.
api="https://api.github.com/repos/$REPO/releases/latest"
log "Querying latest release…"
tag="$(curl -fsSL "$api" | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')"
[ -n "$tag" ] || { log "Could not determine latest ripgrep version."; exit 1; }
log "Latest version: $tag"

asset="ripgrep-${tag}-${target}.tar.gz"
url="https://github.com/$REPO/releases/download/${tag}/${asset}"

# Pick an install directory that is on PATH and writable (no sudo).
dest_dir=""
for cand in /usr/local/bin "$HOME/.local/bin"; do
  if [ -d "$cand" ] && [ -w "$cand" ]; then dest_dir="$cand"; break; fi
done
if [ -z "$dest_dir" ]; then
  dest_dir="$HOME/.local/bin"
  mkdir -p "$dest_dir"
fi
log "Install directory: $dest_dir"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
log "Downloading $url"
curl -fsSL "$url" -o "$tmp/$asset"
tar -xzf "$tmp/$asset" -C "$tmp"

rg_path="$(find "$tmp" -type f -name rg -perm -u+x | head -n1)"
[ -n "$rg_path" ] || rg_path="$(find "$tmp" -type f -name rg | head -n1)"
[ -n "$rg_path" ] || { log "rg binary not found in archive."; exit 1; }

install -m 0755 "$rg_path" "$dest_dir/rg"
log "Installed rg to $dest_dir/rg ($("$dest_dir/rg" --version | head -n1))"

case ":$PATH:" in
  *":$dest_dir:"*) ;;
  *) log "NOTE: $dest_dir is not on your PATH. Add it: export PATH=\"$dest_dir:\$PATH\"" ;;
esac
