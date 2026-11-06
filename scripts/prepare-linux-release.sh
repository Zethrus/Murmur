#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_PYTHON="${1:-python3}"
"$BUILD_PYTHON" -m venv --clear "$ROOT/engine/.release-venv"
"$ROOT/engine/.release-venv/bin/python" -m pip install --require-hashes \
  -r "$ROOT/engine/requirements-linux-release.txt"
"$ROOT/engine/.release-venv/bin/python" -m pip check
