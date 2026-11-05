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
# Share the native CPU contract with local release builds and CI.
. "$ROOT/scripts/cpu-baseline.sh"
for cmd in python3 node npm npx rustc cargo pkg-config cc cmake g++ xdotool xprop; do
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
printf '[1/5] Installing the local engine runtime (venv + model inference)...\n'
ENGINE_DIR="$(python3 scripts/linux-user-install.py engine-path)"
mkdir -p "$ENGINE_DIR"
if [[ ! -x "$ENGINE_DIR/.venv/bin/python" ]]; then
  python3 -m venv --system-site-packages "$ENGINE_DIR/.venv"
fi
PYTHON="$ENGINE_DIR/.venv/bin/python"
"$PYTHON" -m pip install --upgrade pip >/dev/null
"$PYTHON" -m pip install -r engine/requirements-cuda.txt
"$PYTHON" -c 'import faster_whisper, ctranslate2, numpy, huggingface_hub'
rm -rf "$ENGINE_DIR/murmur_engine"
cp -r engine/murmur_engine "$ENGINE_DIR/murmur_engine"
printf '[2/5] Installing locked frontend dependencies...\n'
npm ci
[[ -x node_modules/.bin/tauri ]] || { printf 'Tauri CLI missing after npm ci.\n' >&2; exit 1; }
printf '[3/5] Writing the engine launcher (CUDA runtimes from the venv, no frozen bundle)...\n'
SIDECAR="$ROOT/.cache/murmur-engine"
mkdir -p "$ROOT/.cache"
cat > "$SIDECAR" <<'LAUNCH'
#!/usr/bin/env bash
# Launches the engine from its installed venv so CUDA wheels are found without bundling ~1.6 GB.
set -euo pipefail
ROOT="${MURMUR_ENGINE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/murmur/engine}"
NV_LIBS=$(ls -d "$ROOT"/.venv/lib/python*/site-packages/nvidia/*/lib 2>/dev/null | paste -sd: - || true)
if [[ -n "$NV_LIBS" ]]; then
  export LD_LIBRARY_PATH="$NV_LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
export HF_HUB_OFFLINE=0 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m murmur_engine "$@"
LAUNCH
chmod 755 "$SIDECAR"
bash -n "$SIDECAR"
TARGET="$(rustc -vV | while IFS=' ' read -r key value; do
  [[ "$key" != host: ]] || printf '%s' "$value"
done)"
[[ -n "$TARGET" ]] || { printf 'Cannot determine Rust host target.\n' >&2; exit 1; }
# tauri.conf declares externalBin, so the bundle needs a file with the target suffix; the launcher
# is that file (a few hundred bytes) instead of a ~1.6 GB frozen interpreter.
mkdir -p src-tauri/binaries
cp "$SIDECAR" "src-tauri/binaries/murmur-engine-$TARGET"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
printf '[4/5] Building Murmur and Linux bundles...\n'
# Tauri runs npm run build through beforeBuildCommand.
npx tauri build
BINARY="$CARGO_TARGET_DIR/release/murmur"
[[ -s "$BINARY" && -x "$BINARY" ]]
printf '[5/5] Installing and verifying user files...\n'
python3 scripts/linux-user-install.py install --binary "$BINARY" --sidecar "$SIDECAR"
printf 'Installed. Launch ~/.local/bin/murmur or use the application menu.\n'
printf 'Quit any older running instance before launching the new version.\n'
