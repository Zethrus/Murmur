# Components

Since 0.5 the Linux packages contain only the app. Everything heavy is a
**component** that Murmur downloads when you ask for it, verifies, and installs
into your data folder:

| Component | ID | Needed for | Source |
| --- | --- | --- | --- |
| Speech engine | `engine` | Dictation | GitHub Releases (Murmur builds it) |
| NVIDIA GPU acceleration | `cuda` (needs `engine`) | Optional GPU inference | GitHub Releases (cuBLAS from NVIDIA's PyPI wheel) |
| Speech models (7) | `speech-<model>` | Dictation (one is enough) | Hugging Face, pinned commit |
| Enhance model | `enhance-qwen3-1.7b-q4_k_m` | Optional Enhance rewrites | Hugging Face, pinned commit |

Code: `src-tauri/src/components/`. Release tooling: `scripts/components/`.

## Lifecycle

```
manifest (signed) ──► install click ──► space check ──► download (.part, Range resume)
   ──► size + SHA-256 ──► extract/stage ──► smoke test ──► rename into place
   ──► active.json (atomic) ──► engine restart / status refresh
```

Nothing touches the network at startup. The only requests Murmur makes are:

- **Install / Update / Reinstall / Retry / Resume**: the component's files.
- **Check for updates**: `components-manifest.json`, its `.sig` and
  `murmur-release.asc` from the latest *published* GitHub release.

## Trust

`trust.rs`. One OpenPGP primary key (ed25519, certify-only, kept offline)
certifies a signing subkey that expires after a year. The subkey makes a
detached signature over the exact manifest bytes.

- The primary key's full fingerprint is a constant in the binary
  (`RELEASE_PRIMARY_FINGERPRINT`); its public certificate
  (`src-tauri/trust/murmur-release.asc`) is compiled in by `build.rs`. If either
  is missing or they disagree, every install is refused with **"Release trust
  root missing"**; the app itself keeps working. Until the owner creates the
  key ([signing.md](signing.md)) this is the state of every build.
- Verification: Sequoia OpenPGP (pure-Rust crypto backend) with its
  `StandardPolicy`, plus SHA-1 and all DSA sizes rejected. The signature must come
  from a *subkey* that is bound with a valid binding signature and back-signature,
  is not revoked, and was valid when the manifest was issued (`issued_at`).
- **Certificate updates** (`murmur-release.asc` fetched next to the manifest)
  are judged against the certificate in force (the pinned root merged with the
  stored update) and accepted only for the same primary fingerprint, only if
  strictly newer (newest self-signature) and valid under the policy, with an
  unrevoked primary. They do **not** need a usable signing subkey: an update that
  only revokes the sole subkey must take effect; whether a subkey may sign a given
  manifest is decided at verification (bound, alive and unrevoked at its
  `issued_at`). Updates are merged, so signatures (revocations included) are only
  ever added. The merged certificate is stored in `components/trust/` as soon as
  it is accepted, even if the manifest fetched with it is refused, and a stored
  certificate is never replaced by one that is not strictly newer — an older
  certificate cannot undo a subkey revocation.
- **Revoked primary**: installs are refused permanently with a message to
  update Murmur; the refusal is recorded in `state.json`.
- Manifest, signature and certificate are fetched together. The manifest is
  trusted all or nothing: any failure means no manifest from that fetch is used
  or stored (only an acceptable certificate update is kept, as above).

### Development trust roots

`MURMUR_DEV_TRUST_ROOT=/path/cert.asc` replaces the pinned key in **debug
builds**. Release builds ignore it unless
`MURMUR_DEV_TRUST_ROOT_ACK=I-understand-this-disables-release-trust` is also
set, and then log a warning on every start. Tests, CI and
`scripts/test-*.py` use this with throwaway keys from
`scripts/components/dev-key.sh`. Unit tests generate keys at runtime.

## Manifest (schema 1)

```json
{
  "schema_version": 1, "serial": 1794029201, "channel": "stable",
  "issued_at": "2026-11-07T05:26:41Z", "expires_at": "2027-05-06T05:26:41Z",
  "components": [{
    "id": "engine", "version": "0.5.0", "os": "linux", "arch": "x86_64", "kind": "engine",
    "archive": "zip", "urls": ["https://github.com/Zethrus/Murmur/releases/download/v0.5.0/murmur-engine-linux-x86_64-0.5.0.zip"],
    "size_bytes": 65416131, "sha256": "…", "installed_size_bytes": 297522531, "temp_size_bytes": 0,
    "depends": [], "license": {"name": "…", "url": "…"},
    "provenance": {"type": "murmur_attested_only", "source_url": "…", "source_revision": "<git commit>",
                   "source_sha256": "<sha256 of the wheel lock>", "note": "…"},
    "display_name": "Speech engine", "description": "…"
  }]
}
```

- `archive: "zip"` components have one archive (`urls`, `size_bytes`, `sha256`).
- `archive: "none"` components list `files[]`, each with `name`, `urls`,
  `size_bytes`, `sha256`. Speech models use this so Murmur downloads them from
  Hugging Face at a **pinned commit** instead of re-hosting gigabytes of weights.
  Speech models also carry `model`, `quality`, `speed`, `memory_mb` (an estimate:
  `model.bin` MiB × 1.2 + 200) and `languages` (`en` or `multilingual`).
- `os` is `linux`, `windows` or `any`.
- `provenance.type`: `upstream_signature_verified` (an upstream signature was
  checked) or `murmur_attested_only` (Murmur pinned the bytes; no upstream
  signature exists). Every current component is `murmur_attested_only`:
  PyPI wheels are pinned by hash, Hugging Face publishes no signatures, and
  `large-v3-turbo` is a third-party conversion (`dropbox-dash/…`), which its note
  says.
- Unknown `schema_version` → refused ("update Murmur").
- Built by `scripts/components/make-manifest.py` from the zips and
  `scripts/components/provenance.toml`; model pins come from
  `scripts/components/pin-models.py`. Signed by `sign-manifest.sh`.

## Anti-rollback and clock

`components/state.json` keeps, per channel, the highest serial and the SHA-256 of
the manifest seen with it, plus the newest `issued_at` seen:

- lower serial → refused; same serial with different bytes → refused;
- system clock earlier than the newest `issued_at` seen (or than the manifest's
  own `issued_at`) → refused with **"Clock error likely"**. The manifest built
  into the binary is allowed 24 hours of slack here, so a computer whose clock is
  slightly behind does not report a clock error on first run; fetched and cached
  manifests keep the strict rule;
- `expires_at` in the past (checked after the rules above) → the manifest is kept
  as a **stale** catalog: components stay listed, dependency rules still apply,
  Verify and Remove work, but nothing is downloaded or activated (Install,
  Update, Resume and Retry are refused) until Check for updates fetches a fresh
  list. The UI shows "Component list expired — Check for updates"; first-run
  setup still opens but its install button is disabled with the same
  explanation. Embedded manifests expire 180 days after the build, so every
  release binary reaches this state offline eventually. The status payload
  carries `stale`.

Release builds embed a signed manifest so the setup shows real sizes offline; it
is verified on every load like a downloaded one. The higher serial of the
embedded and the cached manifest wins.

**Threat model.** `state.json`, `jobs.json` and `active.json` are not signed.
They defend against a network or mirror attacker (who cannot forge the signature
and cannot replay an older one), not against code running as your user, which
could already replace Murmur itself.

## Downloads (`fetch.rs`)

- HTTPS only, port 443, no user info, no IP literals, at most 5 redirects; every
  redirect hop is checked against an exact host list: `github.com`,
  `objects.githubusercontent.com`, `release-assets.githubusercontent.com`,
  `huggingface.co`, `cdn-lfs.huggingface.co`, `cdn-lfs-us-1.huggingface.co`,
  `cdn-lfs-eu-1.huggingface.co`, and the Hugging Face CDN hosts observed today:
  `us.aws.cdn.hf.co`, `cas-bridge.xethub.hf.co`, `cdn-lfs-us-1.hf.co`. No
  wildcard (`*.hf.co` is refused). Logs never contain URLs with query strings.
- Resume with HTTP Range into `downloads/<id>-<version>.part`. On resume the
  bytes already on disk are hashed again from byte 0. A server that ignores Range
  restarts from zero.
- Size and SHA-256 must match at the end; a mismatch deletes the `.part`.
- Network errors retry 3 times with exponential backoff; verification failures
  never retry. A transfer with no data for 30 s counts as a network error.
- Pause/Cancel stop within ~100 ms. Pause keeps the `.part`; Cancel deletes it.
- Before a job starts: free space ≥ download + installed + temporary + 100 MiB,
  otherwise an error naming each amount.

## Archives (`archive.rs`)

Zip only (deflate or LZMA; zip64 allowed). Refused: absolute paths, `..`,
backslashes, drive letters, any `:` in a name (to rule out alternate data
streams; this also refuses names that are legitimate on Unix, which component
archives never need), symlinks, device and
other special entries, duplicate or case-colliding names, a file used as a
directory, Windows reserved names (`CON`, `PRN`, `AUX`, `NUL`, `COM1–9`,
`LPT1–9`), names ending in a dot or space, paths over 240 characters, more than
20,000 entries, a total uncompressed size above `installed_size_bytes` × 1.05,
and entries of 1 MiB or more compressing better than 200:1. Every entry is
checked before anything is written. The executable bit is kept on Unix.

Release zips are built by `scripts/bundle-linux-runtime.py components` with
LZMA (the CUDA add-on is 435 MiB this way against 554 MiB with deflate). PyInstaller's
convenience symlinks inside the engine are left out; the real files ship at their
own paths (the frozen engine was checked to start and transcribe without them).

## Store (`store.rs`) and data layout

```
~/.local/share/murmur/                (%LOCALAPPDATA%\Murmur on Windows)
├── config.toml, history.sqlite, murmur.log   never touched by the component store
└── components/
    ├── state.json            anti-rollback state
    ├── jobs.json             queued / paused / failed jobs
    ├── trust/                cached manifest, signature, accepted certificate update
    ├── downloads/            .part files
    ├── engine/0.5.0/…        murmur-engine + _internal/
    ├── engine/active.json    {"version": "0.5.0", …}
    ├── cuda/0.5.0/…          libcublas.so.12, libcublasLt.so.12, NVIDIA notice
    └── speech-distil-small.en/ef77d90526cc/…
```

Install: extract into `<id>/.staging-<version>`, smoke test, rename to
`<id>/<version>`, then atomically rewrite `<id>/active.json` (temporary file,
fsync, rename). Smoke tests: the engine must run `--help`; every CUDA library's
`NEEDED` CUDA entries must resolve inside the add-on (parsed from the ELF
headers; no `readelf` needed on users' machines); models must have their required
files (GGUF magic for Enhance). A failed smoke test leaves the previous version
active and untouched. The previous version is deleted only after the new one is
active, and only for updates. Before the engine or CUDA add-on is switched or
removed, the engine process is stopped; afterwards it is restarted, so a newly
installed engine works without restarting Murmur. If anything fails after the
engine was stopped (activation, removal), the engine is restarted with the files
still in place instead of staying held. A file component's verified downloads
are moved into staging and moved back to `downloads/` when the smoke test,
activation or a pause fails, so Retry does not download them again (they are
hashed again first). The engine must confirm it stopped (its process exited)
before engine or CUDA files are switched or removed; if it does not within 20 s,
the job fails with "The speech engine did not stop in time" and nothing is
deleted. On Windows, renames retry on sharing violations and access denied
(antivirus, indexer, a just-exited engine) with backoff from 50 ms, for at most
about 5 s in total (verified in source, not run on Windows). The Windows GPU
add-on's smoke test requires `cublas64_12.dll` and `cublasLt64_12.dll` as PE
images; the Windows GPU option stays hidden in setup until validated on hardware
(`WINDOWS_GPU_OFFERED`).

Component ids and versions become paths, so every entry point checks them
(`safe_id`, `safe_version`) and `Store::component_dir` refuses unsafe ones too.
`active.json` is unsigned: a record whose `version` is unsafe, or whose
`location` (a migrated model verified in place) is not an absolute path inside
the component's folder or a pre-0.5 model folder, is treated as absent.

Removal is refused, with the reason shown, when another installed component
depends on it (the CUDA add-on needs the engine), when it is the selected speech
model, or when Enhance uses it for automatic rewrites. Removal deletes only
`components/<id>`.

## States

| State | Meaning | Actions |
| --- | --- | --- |
| available | in the manifest, not installed | Install |
| queued / installing | job waiting or running (live progress, rate, ETA) | Pause, Cancel |
| paused | stopped by the user, or Murmur exited mid-job | Resume, Discard |
| failed | job failed (message names the reason) | Retry, Discard |
| active | installed and in use | Verify, Remove, Update |
| installed | installed, not selected (models) | Use model, Verify, Remove |
| unverified | an earlier download whose files do not match the manifest | Reinstall |
| corrupt | files missing, or Verify found a change | Reinstall, Remove |

Interrupted jobs become **Paused** after a restart and never resume on their own.

## Migration from 0.4.x

On first start with a trusted manifest, Murmur hashes speech models in
`~/.local/share/murmur/models/<name>/` and the Enhance model in
`enhance-models/<id>/<revision>/` against the manifest (no network). Matching
folders are moved into the store (same file system: a rename, no copy, no
download) and shown as *adopted*. Anything else stays where it is, is shown as
**unverified** and is never loaded; Reinstall downloads a verified copy. Nothing
is deleted automatically.

Debug builds without any trusted manifest keep loading pre-0.5 models in place
so development checkouts work; release builds never load unverified files.

## Engine lookup

`MURMUR_ENGINE` → development venv (debug builds) → active `engine` component →
packaged `../lib/murmur/engine` (source installs). CUDA: active `cuda` component
→ packaged `../lib/murmur/cuda`; passed via `LD_LIBRARY_PATH` (Linux) or
`MURMUR_CUDA_DIR` (Windows). Speech requests carry the verified model directory
(`model_path`); the engine itself never downloads.

## Testing hooks

`murmur --install-component <zip|model folder> --manifest <json>
--manifest-sig <sig> [--trust-root <asc>]` installs from local files through the
same verification (used by `scripts/test-linux-*.py` and
`scripts/test-clean-install.sh`). It is a test and packaging tool: it runs
without the app, passes no `before_activate` hook and does not stop a running
engine, so reinstalling the active engine version there moves its folder aside
while it may still run. The app always stops the engine before switching.

[Back to the README](../README.md)
