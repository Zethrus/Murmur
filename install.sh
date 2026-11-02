#!/usr/bin/env bash
# Build and install Murmur for the current Linux user; never elevate privileges.
set -Eeuo pipefail
trap 'printf "Murmur install failed at line %s. No further steps were run.\n" "$LINENO" >&2' ERR
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == --uninstall ]]; then
  shift
  exec "$ROOT/uninstall.sh" "$@"
fi
case "${1:-}" in
  --help|-h) printf 'Usage: ./install.sh | ./install.sh --uninstall [--purge]\n'; exit 0 ;;
  '') ;;
  *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
esac
[[ $# -eq 0 ]] || { printf 'Unexpected arguments. Use --help.\n' >&2; exit 2; }
[[ "$(uname -s)" == Linux && $EUID -ne 0 ]] || {
  printf 'Run as your regular Linux user, without sudo.\n' >&2; exit 1;
}
cd "$ROOT"
for cmd in python3 node npm npx rustc cargo pkg-config cc; do
  command -v "$cmd" >/dev/null || { printf 'Missing prerequisite: %s\n' "$cmd" >&2; exit 1; }
done
node -e 'if (Number(process.versions.node.split(".")[0]) < 22) process.exit(1)'
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
# Dependencies must already be provisioned; this script never runs a package manager as root.
pkg-config --exists webkit2gtk-4.1 libsoup-3.0 ayatana-appindicator3-0.1 librsvg-2.0 alsa || {
  printf 'Missing Linux development libraries; see README prerequisites.\n' >&2; exit 1;
}
export TMPDIR="$ROOT/.cache/tmp" PIP_CACHE_DIR="$ROOT/.cache/pip"
# Make the artifact location deterministic even with an inherited Cargo target override.
export CARGO_TARGET_DIR="$ROOT/src-tauri/target"
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR"
printf '[1/5] Preparing engine/.venv (system site packages enabled)...\n'
python3 -m venv --system-site-packages engine/.venv
PYTHON="$ROOT/engine/.venv/bin/python"
"$PYTHON" -c 'import sys; assert sys.prefix != sys.base_prefix'
"$PYTHON" -m pip install -r engine/requirements.txt pyinstaller
"$PYTHON" -c 'import faster_whisper, ctranslate2, numpy, huggingface_hub, PyInstaller'
printf '[2/5] Installing locked frontend dependencies...\n'
npm ci
[[ -x node_modules/.bin/tauri ]] || { printf 'Tauri CLI missing after npm ci.\n' >&2; exit 1; }
printf '[3/5] Freezing the engine (including installed NVIDIA runtimes)...\n'
scripts/engine-sidecar.sh --build
TARGET="$(rustc -vV | while IFS=' ' read -r key value; do
  [[ "$key" != host: ]] || printf '%s' "$value"
done)"
[[ -n "$TARGET" ]] || { printf 'Cannot determine Rust host target.\n' >&2; exit 1; }
SIDECAR="$ROOT/src-tauri/binaries/murmur-engine-$TARGET"
[[ -s "$SIDECAR" && -x "$SIDECAR" ]]
"$PYTHON" -c 'import subprocess, sys; subprocess.run([sys.argv[1], "--help"], check=True, timeout=120)' "$SIDECAR"
printf '[4/5] Building Murmur and Linux bundles...\n'
# Tauri runs npm run build through beforeBuildCommand.
npx tauri build
BINARY="$CARGO_TARGET_DIR/release/murmur"
[[ -s "$BINARY" && -x "$BINARY" ]]
printf '[5/5] Installing and verifying user files...\n'
python3 scripts/linux-user-install.py install --binary "$BINARY" --sidecar "$SIDECAR"
printf 'Installed. Launch ~/.local/bin/murmur or use the application menu.\n'
printf 'Quit any older running instance before launching the new version.\n'
