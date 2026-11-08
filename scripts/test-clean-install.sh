#!/usr/bin/env bash
# Clean-machine smoke test of the .deb in an offline Ubuntu 24.04 container.
#
#   scripts/test-clean-install.sh <linux artifacts dir> <components dir> <staged model dir>
#
# 1. Build a throwaway image: ubuntu:24.04 plus exactly the .deb's declared Depends (resolved
#    by apt from the package, which is then removed) and the test tools (Xvfb, strace). This
#    is the only step with network access.
# 2. Run it with --network none: install the .deb, start Murmur on Xvfb with no engine,
#    require it to stay up and open Settings, and record every connect() with strace:
#    any AF_INET/AF_INET6 attempt fails the test (startup must not touch the network).
# 3. Install the engine from the local zip and the model from the staged folder through the
#    app's --install-component path (throwaway trust root + acknowledgement), then
#    transcribe the fixture on the CPU with the installed engine.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACTS="$(realpath "${1:?artifacts dir}")"
COMPONENTS="$(realpath "${2:?components dir}")"
MODEL="$(realpath "${3:?staged model dir, e.g. distil-small.en}")"
DEB="$(ls "$ARTIFACTS"/*.deb)"
IMAGE="murmur-clean-test:$(sha256sum "$DEB" | cut -c1-12)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/murmur-clean.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
# Let apt resolve the package's own Depends (alternatives such as libasound2 | libasound2t64
# included), then remove Murmur again so the offline step installs it from scratch.
cp "$DEB" "$WORK/murmur.deb"
cat > "$WORK/Dockerfile" <<'DOCKER'
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
COPY murmur.deb /tmp/murmur.deb
RUN apt-get update && apt-get install -y --no-install-recommends /tmp/murmur.deb \
    xvfb xauth dbus dbus-x11 strace python3 procps iproute2 \
 && dpkg -r murmur && rm -rf /var/lib/apt/lists/* /tmp/murmur.deb
RUN useradd -m tester
DOCKER
docker build -q -t "$IMAGE" "$WORK" >/dev/null
cp "$ROOT/scripts/clean-install/inside.sh" "$ROOT/scripts/clean-install/transcribe.py" "$ROOT/scripts/tests/jfk-3s.wav" "$WORK/"
# The container's unprivileged user reads these through a read-only mount.
chmod 755 "$WORK" && chmod 644 "$WORK"/*
docker run --rm --network none \
  -v "$DEB:/in/murmur.deb:ro" -v "$COMPONENTS:/in/components:ro" -v "$MODEL:/in/model:ro" -v "$WORK:/in/test:ro" \
  "$IMAGE" bash /in/test/inside.sh
docker image rm -f "$IMAGE" >/dev/null
echo "Clean offline install: passed"
