#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export TMPDIR="$ROOT/.cache/tmp" PIP_CACHE_DIR="$ROOT/.cache/pip"
export PYINSTALLER_CONFIG_DIR="$ROOT/.cache/pyinstaller" XDG_CACHE_HOME="$ROOT/.cache"
export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR"
PYTHON="$ROOT/engine/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  printf '%s\n' 'Create engine/.venv and install engine/requirements-cuda.txt first.' >&2
  exit 1
fi
if [[ "${1:-}" == --build ]]; then
  cd "$ROOT/engine"
  # CUDA bundling is opt-in: cuDNN+cuBLAS add ~1.4 GB to a onefile binary and slow every launch.
  NV_ARGS=()
  if [[ "${2:-}" == --cuda ]]; then
    for pkg in nvidia.cudnn nvidia.cublas nvidia.cuda_nvrtc; do
      if "$PYTHON" -c "import $pkg" >/dev/null 2>&1; then NV_ARGS+=(--collect-all "$pkg"); fi
    done
  fi
  "$PYTHON" -m PyInstaller --noconfirm --clean --onefile --name murmur-engine \
    --paths . --runtime-hook runtime_hook_cuda.py "${NV_ARGS[@]}" \
    --collect-all faster_whisper --collect-all ctranslate2 \
    --collect-all tokenizers --collect-all onnxruntime murmur_engine/__main__.py
  TARGET="$(rustc -vV | while IFS=' ' read -r key value; do [[ "$key" != host: ]] || printf '%s' "$value"; done)"
  mkdir -p "$ROOT/src-tauri/binaries"
  cp dist/murmur-engine "$ROOT/src-tauri/binaries/murmur-engine-$TARGET"
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
