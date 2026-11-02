#!/usr/bin/env bash
# Configuration, downloaded models, history and logs survive unless --purge is explicit.
set -Eeuo pipefail
trap 'printf "Murmur uninstall failed at line %s.\n" "$LINENO" >&2' ERR
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
case "${1:-}" in
  --help|-h) printf 'Usage: ./uninstall.sh [--purge]\nQuit Murmur first. --purge deletes config, models, history and logs.\n'; exit 0 ;;
  ''|--purge) ;;
  *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { printf 'Unexpected arguments. Use --help.\n' >&2; exit 2; }
[[ "$(uname -s)" == Linux && $EUID -ne 0 ]] || {
  printf 'Run as your regular Linux user, without sudo.\n' >&2; exit 1;
}
command -v python3 >/dev/null || { printf 'Python 3 is required to uninstall safely.\n' >&2; exit 1; }
printf 'Removing user installation. Quit Murmur first so it cannot recreate data.\n'
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -s "$RUNTIME/murmur-inject.pid" ]]; then
  kill "$(cat "$RUNTIME/murmur-inject.pid")" 2>/dev/null || true
fi
rm -f "$RUNTIME"/murmur-inject.fifo "$RUNTIME"/murmur-inject.txt "$RUNTIME"/murmur-inject.pid "$RUNTIME"/murmur-inject.done
python3 "$ROOT/scripts/linux-user-install.py" uninstall "$@"
