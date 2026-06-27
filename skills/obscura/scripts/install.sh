#!/usr/bin/env bash
# Install the `obscura` headless-browser binary this skill depends on.
#
# Run by `evonic doctor --fix` when `obscura` is missing. Downloads the matching
# asset from https://github.com/h4ckf0r0day/obscura/releases and installs it as
# a single binary on PATH (obscura ships as one dependency-free executable).
#
# Best-effort and noisy on purpose; exits non-zero on failure so the doctor
# reports it.
set -euo pipefail

REPO="${OBSCURA_RELEASE_REPO:-h4ckf0r0day/obscura}"

log() { echo "[install_obscura] $*"; }

if command -v obscura >/dev/null 2>&1; then
  log "obscura already installed at $(command -v obscura) ($(obscura --version 2>/dev/null | head -n1))"
  exit 0
fi

os="$(uname -s)"
arch="$(uname -m)"

# Build the list of OS/arch keywords we expect in a release asset name.
case "$os" in
  Darwin) os_keys="darwin macos apple osx" ;;
  Linux)  os_keys="linux" ;;
  *) log "Unsupported OS: $os. Install obscura manually from https://github.com/$REPO/releases"; exit 1 ;;
esac
case "$arch" in
  arm64|aarch64) arch_keys="arm64 aarch64" ;;
  x86_64|amd64)  arch_keys="x86_64 amd64 x64" ;;
  *) arch_keys="$arch" ;;
esac
log "Host: $os/$arch"

# Fetch the latest release's asset download URLs.
api="https://api.github.com/repos/$REPO/releases/latest"
log "Querying latest release…"
urls="$(curl -fsSL "$api" | grep -o '"browser_download_url": *"[^"]*"' | sed -E 's/.*"(https[^"]+)".*/\1/')"
[ -n "$urls" ] || { log "No release assets found for $REPO."; exit 1; }

# Choose the first asset matching both an OS keyword and an arch keyword;
# fall back to OS-only if no arch-tagged asset exists.
pick=""
for u in $urls; do
  lc="$(echo "$u" | tr '[:upper:]' '[:lower:]')"
  os_match=0; for k in $os_keys; do case "$lc" in *"$k"*) os_match=1;; esac; done
  arch_match=0; for k in $arch_keys; do case "$lc" in *"$k"*) arch_match=1;; esac; done
  if [ "$os_match" = 1 ] && [ "$arch_match" = 1 ]; then pick="$u"; break; fi
done
if [ -z "$pick" ]; then
  for u in $urls; do
    lc="$(echo "$u" | tr '[:upper:]' '[:lower:]')"
    for k in $os_keys; do case "$lc" in *"$k"*) pick="$u"; break 2;; esac; done
  done
fi
[ -n "$pick" ] || { log "No matching asset for $os/$arch. Install manually from https://github.com/$REPO/releases"; exit 1; }
log "Selected asset: $pick"

# Pick a PATH directory that is writable without sudo.
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
file="$tmp/$(basename "$pick")"
log "Downloading…"
curl -fsSL "$pick" -o "$file"

# Extract if it's an archive; otherwise treat the download as the binary itself.
bin_path=""
case "$file" in
  *.tar.gz|*.tgz) tar -xzf "$file" -C "$tmp" ;;
  *.tar)          tar -xf  "$file" -C "$tmp" ;;
  *.zip)          unzip -q "$file" -d "$tmp" ;;
  *)              bin_path="$file" ;;
esac
if [ -z "$bin_path" ]; then
  bin_path="$(find "$tmp" -type f -name obscura | head -n1)"
fi
[ -n "$bin_path" ] || { log "obscura binary not found in download."; exit 1; }

install -m 0755 "$bin_path" "$dest_dir/obscura"
log "Installed obscura to $dest_dir/obscura ($("$dest_dir/obscura" --version 2>/dev/null | head -n1))"

case ":$PATH:" in
  *":$dest_dir:"*) ;;
  *) log "NOTE: $dest_dir is not on your PATH. Add it: export PATH=\"$dest_dir:\$PATH\"" ;;
esac
