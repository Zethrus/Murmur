#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export TMPDIR="${TMPDIR:-$ROOT/.cache/tmp}" PIP_CACHE_DIR="${PIP_CACHE_DIR:-$ROOT/.cache/pip}"
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$ROOT/.cache/pyinstaller}" XDG_CACHE_HOME="$ROOT/.cache"
export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR"
PYTHON="$ROOT/engine/.venv/bin/python"
if [[ "${1:-}" == --build ]]; then
  if [[ "${2:-}" == --cuda ]]; then
    printf '%s\n' 'Linux --cuda onefile mode is retired; desktop CUDA is staged in the AppDir.' >&2
    exit 1
  fi
  PYTHON="${2:-$ROOT/engine/.release-venv/bin/python}"
fi
if [[ ! -x "$PYTHON" ]]; then
  printf 'Missing selected engine Python: %s\n' "$PYTHON" >&2
  exit 1
fi
if [[ "${1:-}" == --build ]]; then
  cd "$ROOT/engine"
  # One directory, not onefile: no per-launch unpack into TMPDIR. PyAV (FFmpeg) and
  # hf_xet are left out: Murmur sends decoded PCM and downloads over plain HTTPS
  # (murmur_engine/runtime.py). CUDA userspace is staged by the desktop packaging.
  "$PYTHON" -m PyInstaller --noconfirm --clean --onedir --name murmur-engine \
    --paths . --exclude-module nvidia --exclude-module av --exclude-module hf_xet \
    --collect-all faster_whisper --collect-all ctranslate2 --collect-all tokenizers \
    --collect-binaries onnxruntime --collect-data onnxruntime \
    --hidden-import onnxruntime.capi._pybind_state murmur_engine/__main__.py
  printf 'Frozen engine: %s\n' "$ROOT/engine/dist/murmur-engine"
else
  export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
  # Dev runs use the venv's CUDA wheels directly; ctranslate2 dlopen()s them by soname.
  NV_LIBS=$(ls -d "$ROOT"/engine/.venv/lib/python*/site-packages/nvidia/*/lib 2>/dev/null | paste -sd: -)
  if [[ -n "$NV_LIBS" ]]; then
    export LD_LIBRARY_PATH="${NV_LIBS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  fi
  cd "$ROOT/engine"
  exec "$PYTHON" -m murmur_engine "$@"
fi
