#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# Keep Cargo and the audit bound to the same absolute target directory.
CARGO_TARGET_DIR="$(realpath -m -- "${CARGO_TARGET_DIR:-$ROOT/src-tauri/target}")"
export CARGO_TARGET_DIR
export TMPDIR="${TMPDIR:-$ROOT/.cache/tmp}" PIP_CACHE_DIR="${PIP_CACHE_DIR:-$ROOT/.cache/pip}"
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR"
export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-2}"
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"
# Baseline Rust + AVX2 llama.cpp, shared with CI so release artifacts cannot drift.
. "$ROOT/scripts/cpu-baseline.sh"
# bindgen needs libclang (checked below); the C/C++ compiler is g++, not the clang driver.
for tool in cmake g++ xdotool xprop mksquashfs readelf xvfb-run xauth dbus-run-session; do
  command -v "$tool" >/dev/null || { printf 'Missing Enhance build prerequisite: %s\n' "$tool" >&2; exit 1; }
done
if ! compgen -G '/usr/lib/llvm-*/lib/libclang.so*' >/dev/null &&
   ! compgen -G '/usr/lib/*/libclang.so*' >/dev/null &&
   [[ ! -f "${LIBCLANG_PATH:-/nonexistent}/libclang.so" ]]; then
  printf '%s\n' 'Missing libclang shared library (install libclang-dev or set LIBCLANG_PATH).' >&2
  exit 1
fi
scripts/prepare-linux-release.sh "${MURMUR_BUILD_PYTHON:-python3}"
npm ci
npm run build
scripts/engine-sidecar.sh --build "$ROOT/engine/.release-venv/bin/python"
# Murmur ships cuBLAS only; stop if the frozen engine could load cuDNN or NVRTC.
python3 scripts/check-engine-imports.py "$ROOT/engine/dist/murmur-engine" \
  --site-packages "$("$ROOT/engine/.release-venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
# Downloadable components: engine and CUDA add-on zips, then the signed manifest.
VERSION="$(node -p "require('./package.json').version")"
COMPONENTS="$ROOT/.cache/artifacts/components"
rm -rf "$COMPONENTS"
engine/.release-venv/bin/python scripts/bundle-linux-runtime.py components \
  --engine "$ROOT/engine/dist/murmur-engine" --version "$VERSION" --output "$COMPONENTS"
# The signing key must already be valid when the manifest is issued, so a throwaway
# key (no release key configured) is created before the manifest.
if [[ -z "${MURMUR_SIGNING_SUBKEY:-}" ]]; then
  KEYHOME="$(mktemp -d "${TMPDIR:-/tmp}/murmur-dev-key.XXXXXX")"
  trap 'gpgconf --homedir "$KEYHOME" --kill all >/dev/null 2>&1 || true; rm -rf "$KEYHOME"' EXIT
  scripts/components/dev-key.sh "$KEYHOME" >/dev/null
  sleep 1
fi
# A release with Windows enabled signs the Windows zips too: MURMUR_WINDOWS_COMPONENTS names a
# folder with the Windows zips and components-windows-inputs.json from windows-build.yml.
WINDOWS_ARGS=()
if [[ -n "${MURMUR_WINDOWS_COMPONENTS:-}" ]]; then
  cp "$MURMUR_WINDOWS_COMPONENTS"/murmur-*-windows-x86_64-"$VERSION".zip "$COMPONENTS/"
  WINDOWS_ARGS=(--windows-inputs "$MURMUR_WINDOWS_COMPONENTS/components-windows-inputs.json")
fi
python3 scripts/components/make-manifest.py --components "$COMPONENTS" --version "$VERSION" \
  --output "$COMPONENTS/components-manifest.json" "${WINDOWS_ARGS[@]}"
if [[ -n "${MURMUR_SIGNING_SUBKEY:-}" ]]; then
  scripts/components/sign-manifest.sh "$COMPONENTS/components-manifest.json" "$COMPONENTS/murmur-release.asc"
else
  # These artifacts are for testing only: the app refuses them unless
  # MURMUR_DEV_TRUST_ROOT(+ACK) names this certificate.
  GNUPGHOME="$KEYHOME" scripts/components/sign-manifest.sh "$COMPONENTS/components-manifest.json" "$COMPONENTS/murmur-release.asc"
  echo "WARNING: components signed with a throwaway key (untrusted-dev-signed)." >&2
fi
# Release builds embed the signed manifest so setup shows real sizes offline.
export MURMUR_EMBED_MANIFEST="$COMPONENTS/components-manifest.json"
# Use the project-pinned Tauri CLI; no global cargo-tauri install required.
npx tauri build --bundles deb
python3 scripts/audit-llama-cpu.py "$CARGO_TARGET_DIR/release" --artifact "$CARGO_TARGET_DIR/release/murmur"
npx tauri bundle --bundles appimage
. "$ROOT/scripts/select-linux-bundles.sh"
select_linux_bundles "$CARGO_TARGET_DIR/release" "$VERSION"
rm -rf "$ROOT/.cache/artifacts/linux"
engine/.release-venv/bin/python scripts/bundle-linux-runtime.py package \
  --appimage "$LINUX_APPIMAGE" \
  --deb "$LINUX_DEB" \
  --output "$ROOT/.cache/artifacts/linux"
python3 scripts/test-linux-bundle.py "$ROOT/.cache/artifacts/linux" "$COMPONENTS"
python3 scripts/test-linux-startup.py "$ROOT"/.cache/artifacts/linux/*.AppImage --components "$COMPONENTS"
python3 scripts/check-release.py sizes "$ROOT"/.cache/artifacts/linux/* "$COMPONENTS"/*.zip "$ROOT/engine/dist/murmur-engine"
