#!/usr/bin/env bash
# Build Tailwind CSS from input sources.
# Requires tailwindcss CLI (v4) installed at /workspace/.local/bin/tailwindcss
# or available in PATH.
#
# Usage:
#     ./scripts/build_tailwind.sh          # one-off development build
#     ./scripts/build_tailwind.sh --minify # production build (minified)
#     ./scripts/build_tailwind.sh --watch  # rebuild on every template/JS change (dev)
#
# In --watch mode, leave this running in a terminal next to Flask; any new
# Tailwind class you use in templates/ or static/js/ is compiled automatically
# on save (then hard-refresh the browser). No more "class silently missing".

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INPUT="$ROOT/static/css/tailwind-input.css"
OUTPUT="$ROOT/static/css/tailwind.css"

TW="tailwindcss"
if command -v tailwindcss &>/dev/null; then
    TW="tailwindcss"
elif [ -x "$ROOT/.local/bin/tailwindcss" ]; then
    TW="$ROOT/.local/bin/tailwindcss"
else
    echo "ERROR: tailwindcss CLI not found."
    echo "Install it from https://github.com/tailwindlabs/tailwindcss/releases"
    echo "Place the binary at /workspace/.local/bin/tailwindcss"
    exit 1
fi

MINIFY=""
WATCH=""
case "${1:-}" in
    --minify) MINIFY="-m" ;;
    --watch)  WATCH="--watch" ;;
esac

if [ -n "$WATCH" ]; then
    echo "Watching templates/ and static/js/ for Tailwind classes (Ctrl-C to stop)..."
    exec $TW -i "$INPUT" -o "$OUTPUT" --watch
fi

echo "Building Tailwind CSS..."
$TW -i "$INPUT" -o "$OUTPUT" $MINIFY

SIZE=$(wc -c < "$OUTPUT")
echo "Done. Output: $OUTPUT (${SIZE} bytes)"
