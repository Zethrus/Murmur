#!/usr/bin/env bash
# Build the Murmur companion app for the Flipper Zero.
#
# Requires ufbt (pip install ufbt). Produces dist/murmur_companion.fap.
# Run from anywhere; paths are resolved relative to this script.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

python3 scripts/sync_version.py

if ! command -v ufbt >/dev/null 2>&1; then
    echo "ufbt not found. Install it with: pip install ufbt" >&2
    exit 1
fi

ufbt "$@"

echo
echo "Built: $here/dist/murmur_companion.fap"
