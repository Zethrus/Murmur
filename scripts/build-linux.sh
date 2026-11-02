#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export TMPDIR="$ROOT/.cache/tmp" PIP_CACHE_DIR="$ROOT/.cache/pip"
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR"
if [[ ! -x engine/.venv/bin/python ]]; then
  /usr/bin/python3 -m venv --system-site-packages engine/.venv
fi
engine/.venv/bin/python -m pip install -r engine/requirements.txt pyinstaller
npm ci
npm run build
scripts/engine-sidecar.sh --build
# Use the project-pinned Tauri CLI; no global cargo-tauri install required.
npx tauri build --bundles deb
if ! npx tauri bundle --bundles appimage; then
  printf '%s\n' 'Optional AppImage bundling failed; the binary and .deb remain available.' >&2
fi
