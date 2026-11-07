#!/usr/bin/env bash
# Create a THROWAWAY signing key for local and pull-request builds (never for releases).
#
#   dev-key.sh <empty-directory>
#
# Mirrors the release layout: ed25519 certify-only primary plus an ed25519 signing subkey,
# both expiring in 7 days, no passphrase. Writes <dir>/murmur-dev-trust.asc (public cert).
# Use with MURMUR_DEV_TRUST_ROOT (+ MURMUR_DEV_TRUST_ROOT_ACK for release binaries).
set -euo pipefail
home=$(realpath -m "${1:?usage: dev-key.sh <dir>}")
mkdir -p "$home"
chmod 700 "$home"
export GNUPGHOME=$home
gpg --batch --quiet --passphrase '' --quick-gen-key "Murmur throwaway test key <throwaway@invalid>" ed25519 cert 7d
fingerprint=$(gpg --batch --with-colons --list-keys | awk -F: '/^fpr/ {print $10; exit}')
gpg --batch --quiet --passphrase '' --quick-add-key "$fingerprint" ed25519 sign 7d
gpg --batch --armor --export "$fingerprint" > "$home/murmur-dev-trust.asc"
echo "$fingerprint"
