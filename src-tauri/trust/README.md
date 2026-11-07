# Release trust root

`murmur-release.asc` (not yet present) is the armored **public** certificate of Murmur's
offline release primary key. `build.rs` compiles it into the app; its full fingerprint must
equal `RELEASE_PRIMARY_FINGERPRINT` in `src/components/trust.rs`. Until both exist, release
builds refuse every component install with "Release trust root missing".

Never put secret key material in this directory. See `docs/signing.md`.
