#!/usr/bin/env bash
# Detached-sign the component manifest with the release signing subkey.
#
#   sign-manifest.sh <manifest.json> [public-cert-out.asc]
#
# Key material comes from $GNUPGHOME, or from MURMUR_SIGNING_SUBKEY (an armored export made
# with `gpg --export-secret-subkeys`, stored only as a protected-environment secret). That
# export is imported into a private temporary GNUPGHOME which is destroyed on exit. Nothing
# here writes key material anywhere else; never commit keys.
set -euo pipefail
manifest=${1:?usage: sign-manifest.sh <manifest.json> [cert.asc]}
cert=${2:-}
if [[ -n "${MURMUR_SIGNING_SUBKEY:-}" ]]; then
  home=$(mktemp -d)
  trap 'gpgconf --homedir "$home" --kill all >/dev/null 2>&1 || true; rm -rf "$home"' EXIT
  chmod 700 "$home"
  export GNUPGHOME=$home
  printf '%s\n' "$MURMUR_SIGNING_SUBKEY" | gpg --batch --quiet --import
fi
fingerprint=${MURMUR_SIGNING_FINGERPRINT:-$(gpg --batch --with-colons --list-secret-keys | awk -F: '/^fpr/ {print $10; exit}')}
[[ -n $fingerprint ]] || { echo "No signing key available" >&2; exit 1; }
gpg --batch --yes --quiet --pinentry-mode loopback --passphrase "${MURMUR_SIGNING_PASSPHRASE:-}" \
  --local-user "$fingerprint" --digest-algo SHA512 --armor --detach-sign --output "$manifest.sig" "$manifest"
if [[ -n $cert ]]; then
  gpg --batch --armor --export "$fingerprint" > "$cert"
fi
gpg --batch --quiet --verify "$manifest.sig" "$manifest"
echo "Signed $manifest with $fingerprint"
