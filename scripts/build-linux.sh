#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# Keep Cargo and the audit bound to the same absolute target directory.
export CARGO_TARGET_DIR="$(realpath -m -- "${CARGO_TARGET_DIR:-$ROOT/src-tauri/target}")"
export TMPDIR="$ROOT/.cache/tmp" PIP_CACHE_DIR="$ROOT/.cache/pip"
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR"
export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-2}"
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"
# Baseline Rust + AVX2 llama.cpp, shared with CI so release artifacts cannot drift.
. "$ROOT/scripts/cpu-baseline.sh"
# bindgen needs libclang (checked below); the C/C++ compiler is g++, not the clang driver.
for tool in cmake g++; do
  command -v "$tool" >/dev/null || { printf 'Missing Enhance build prerequisite: %s\n' "$tool" >&2; exit 1; }
done
if ! compgen -G '/usr/lib/llvm-*/lib/libclang.so*' >/dev/null &&
   ! compgen -G '/usr/lib/*/libclang.so*' >/dev/null &&
   [[ ! -f "${LIBCLANG_PATH:-/nonexistent}/libclang.so" ]]; then
  printf '%s\n' 'Missing libclang shared library (install libclang-dev or set LIBCLANG_PATH).' >&2
  exit 1
fi
if [[ ! -x engine/.venv/bin/python ]]; then
  /usr/bin/python3 -m venv --system-site-packages engine/.venv
fi
engine/.venv/bin/python -m pip install -r engine/requirements-cuda.txt pyinstaller
npm ci
npm run build
scripts/engine-sidecar.sh --build
# Use the project-pinned Tauri CLI; no global cargo-tauri install required.
npx tauri build --bundles deb
python3 scripts/audit-llama-cpu.py "$CARGO_TARGET_DIR/release" --artifact "$CARGO_TARGET_DIR/release/murmur"
if ! npx tauri bundle --bundles appimage; then
  printf '%s\n' 'Optional AppImage bundling failed; the binary and .deb remain available.' >&2
fi
