#!/usr/bin/env bash
# Runs inside the offline container started by scripts/test-clean-install.sh (as root first).
set -euo pipefail
dpkg -i /in/murmur.deb >/dev/null
test -x /usr/bin/murmur
test ! -e /usr/lib/murmur/engine && test ! -e /usr/lib/murmur/cuda
if ip -o link 2>/dev/null | grep -v ' lo:' | grep -q .; then echo "container is not offline" >&2; exit 1; fi
exec su tester -c 'bash -s' <<'USER'
set -euo pipefail
export HOME=/home/tester XDG_RUNTIME_DIR=/tmp/runtime && mkdir -m 700 -p "$XDG_RUNTIME_DIR"
cd "$HOME"
# 1) No engine: the app must start, stay up, open Settings, and never try the network.
xvfb-run -a -s "-screen 0 1280x800x24" dbus-run-session -- \
  strace -f -qq -e trace=connect -o /tmp/connect.log timeout 25 /usr/bin/murmur > /tmp/app.log 2>&1 || status=$?
if [ "${status:-0}" != 124 ]; then echo "Murmur exited early (${status:-0})"; cat /tmp/app.log; exit 1; fi
if grep -E 'sa_family=AF_INET6?' /tmp/connect.log; then echo "network connection attempted at startup" >&2; exit 1; fi
# The trace must have seen something (X11/D-Bus sockets), or it proves nothing.
calls=$(grep -c 'connect(' /tmp/connect.log || true)
[ "$calls" -gt 0 ] || { echo "strace recorded no connect() calls" >&2; exit 1; }
echo "connect() calls during startup: $calls, all non-IP ($(grep -o 'sa_family=AF_[A-Z0-9]*' /tmp/connect.log | sort | uniq -c | tr -s ' ' | tr '\n' ' '))"
grep -q 'Speech engine not installed' ~/.local/share/murmur/murmur.log || { echo "missing-engine report absent"; cat ~/.local/share/murmur/murmur.log; exit 1; }
test -f ~/.config/murmur/config.toml
echo "startup without engine: no network, app stayed up"
# 2) Install the engine and the model from local files through the app's verifier.
export MURMUR_DEV_TRUST_ROOT=/in/components/murmur-release.asc MURMUR_DEV_TRUST_ROOT_ACK=I-understand-this-disables-release-trust
args=(--manifest /in/components/components-manifest.json --manifest-sig /in/components/components-manifest.json.sig)
/usr/bin/murmur --install-component /in/components/murmur-engine-linux-x86_64-*.zip "${args[@]}"
/usr/bin/murmur --install-component /in/model "${args[@]}"
python3 /in/test/transcribe.py ~/.local/share/murmur/components /in/test/jfk-3s.wav
USER
