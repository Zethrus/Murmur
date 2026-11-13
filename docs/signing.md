# Release signing key

Murmur verifies its component manifest with one OpenPGP key: a **primary key** that only certifies, and a **signing subkey** that CI uses.
The production primary fingerprint is
`F9616DB91C05AB407EBF608DAB733B91896418DF`, independently verified by the owner.
The primary is passphrase-protected on Hinton, a network-connected host; it is
not an offline key. An AES-256-encrypted backup including the revocation
certificate is retained on the owner's external drive. CI receives only the
signing subkey, which expires on 2027-11-13. Keep the backup passphrase in the
owner's password manager. Offline primary-key custody remains the recommended
long-term arrangement.

No private key material is ever committed; the public certificate is committed. CI pull-request builds use throwaway keys
(`scripts/components/dev-key.sh`); unit tests generate keys at runtime.

## 1. Create the key (reference for future provisioning)

The production key is already provisioned; do not generate a replacement for
each release. The following describes the preferred offline procedure.

On an offline machine (live USB, no network), with GnuPG 2.2 or newer:

```sh
export GNUPGHOME="$(mktemp -d)"   # or a directory on an encrypted USB stick
chmod 700 "$GNUPGHOME"

# Primary: ed25519, certify only, no expiry. Use a strong passphrase.
gpg --quick-gen-key "Murmur Release <release@zethr.us>" ed25519 cert never
FPR=$(gpg --with-colons --list-keys | awk -F: '/^fpr/ {print $10; exit}')

# Signing subkey: ed25519, valid for one year.
gpg --quick-add-key "$FPR" ed25519 sign 1y

# Revocation certificate for the primary key (gpg also writes one to openpgp-revocs.d).
gpg --output murmur-release-revoke.asc --gen-revoke "$FPR"

# Public certificate for the app and the release.
gpg --armor --export "$FPR" > murmur-release.asc

# The signing subkey alone, for CI. `--export-secret-subkeys` never includes the
# primary's secret part; the trailing ! selects exactly this subkey.
SUB=$(gpg --with-colons --list-keys "$FPR" | awk -F: '/^sub/ {s=1} s && /^fpr/ {print $10; exit}')
gpg --armor --export-secret-subkeys "$SUB"! > murmur-signing-subkey.asc
echo "$FPR"
```

Check the result: `gpg --list-packets murmur-signing-subkey.asc` must show the
primary as `gnu-dummy` (no secret) and one secret subkey with usage `S`.

Keep the primary secret key, its passphrase and the revocation certificate
offline, in two places. Record the fingerprint `$FPR`.

## 2. Pin it in the app

1. Commit `murmur-release.asc` as `src-tauri/trust/murmur-release.asc`.
2. Set `RELEASE_PRIMARY_FINGERPRINT` in `src-tauri/src/components/trust.rs` to
   the 40-character fingerprint (uppercase hex, no spaces) and commit.
3. Run `cargo test` and check that `load_root()` succeeds in a release build:
   `murmur` logs no "Release trust root missing".

## 3. Configure GitHub

- Environment **`release-signing`** (Settings → Environments):
  - required reviewer: the owner;
  - deployment branches and tags: only tags matching `v*`;
  - secret **`MURMUR_SIGNING_SUBKEY`**: the contents of
    `murmur-signing-subkey.asc`;
  - secret `MURMUR_SIGNING_PASSPHRASE` if the subkey has a passphrase.
- Repository variable **`MURMUR_RELEASE_TRUST_FINGERPRINT`**: the fingerprint.
- Tag protection (rulesets): only maintainers may create `v*` tags.

Delete `murmur-signing-subkey.asc` from the offline machine's working directory
once stored.

## What CI does with it

- Pull requests and branch builds sign the manifest with a throwaway key, embed
  it, run every test with `MURMUR_DEV_TRUST_ROOT`, and upload artifacts named
  `*-untrusted-dev-signed*`. Those artifacts install nothing without the
  development override.
- Tags: the `release` job (environment `release-signing`, so it waits for the
  reviewer) checks that the committed certificate and the constant match
  `MURMUR_RELEASE_TRUST_FINGERPRINT`, imports the subkey into a temporary
  `GNUPGHOME` that is deleted afterwards (`sign-manifest.sh`), rebuilds the app
  with the release-signed manifest embedded, runs the tests and GPU validation,
  and attaches the assets to the draft. `release.yml`'s validate job checks the
  signature against the fingerprint again. Nothing is published automatically.
- Without the variable, tag builds stop after the throwaway-signed validation
  and upload nothing.

## Rotation

Every year, before the subkey expires (Murmur refuses manifests issued after
its expiry):

```sh
gpg --quick-add-key "$FPR" ed25519 sign 1y        # on the offline machine
gpg --armor --export "$FPR" > murmur-release.asc
SUB=<new subkey fingerprint>
gpg --armor --export-secret-subkeys "$SUB"! > murmur-signing-subkey.asc
```

Replace the `MURMUR_SIGNING_SUBKEY` secret, commit the new certificate to
`src-tauri/trust/` and attach it to the next release as `murmur-release.asc`.
Installed apps accept the newer certificate because the same primary certifies it
and it is strictly newer. Optionally revoke the old subkey with reason
"superseded" (`--quick-revoke-sig` revokes *signatures*, not subkeys; do not use
it here):

```sh
gpg --edit-key "$FPR"      # on the offline machine
gpg> key N                 # N = position of the old subkey in the listing
gpg> revkey                # pick reason 3 "Key is superseded" (2 for compromised)
gpg> save
gpg --armor --export "$FPR" > murmur-release.asc
```

or with Sequoia, `sq key subkey revoke` (flags differ between `sq` versions; see
its `--help`), then export the certificate.

## Compromise and revocation

- **Subkey compromised**: revoke it with reason "key compromised" on the
  offline machine (`revkey` above, reason 2), create a new subkey as above,
  publish the updated certificate with the next manifest. Apps reject every
  signature from a revoked subkey, including on manifests they already cached.
  If no new subkey is ready yet, publish the revocation-only certificate anyway:
  apps store any acceptable, strictly newer certificate as soon as they fetch it,
  even when the manifest next to it no longer verifies, so the revocation takes
  effect at once. An app never replaces a stored certificate with an older one,
  so an attacker cannot undo the revocation by serving the previous certificate.
- **Primary compromised or lost**: publish the revocation certificate as
  `murmur-release.asc` with a new release. Apps that fetch it refuse all
  component installs permanently and tell the user to update Murmur. A new
  primary key requires a new app release with a new pinned fingerprint.

## Failure behavior when signing configuration is missing

- Release builds cannot install components (by design: "Release trust root
  missing").
- Tag builds do not upload release assets.
- `release.yml`'s validate job fails at the signature check.

[Back to the README](../README.md)
