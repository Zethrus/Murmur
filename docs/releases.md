# Publishing a release

The public release currently requires Linux x86_64 and Flipper Zero. Windows joins
the same draft when `ENABLE_WINDOWS` is `true` and the Edison runner is
registered ([ci-windows.md](ci-windows.md)); Android is deferred in
[the roadmap](roadmap.md).

## Prepare

1. Update versions in `package.json`, `package-lock.json`, `src-tauri/Cargo.toml`,
   `src-tauri/Cargo.lock`, `src-tauri/tauri.conf.json`, and `engine/pyproject.toml`.
2. Run `python3 companion/flipper/scripts/sync_version.py`.
3. Update `docs/release-notes.md` with the changes and any known limitations.
4. Run the [build and test checks](building.md), including real GPU inference
   on a trusted NVIDIA runner and manual X11 delivery checks. Review final
   asset hashes and validation evidence before tagging.
5. Merge the reviewed commit. Create and push its `vMAJOR.MINOR.PATCH` tag.

Do not reuse a published version or move its tag. Fixes need a new version.

## What CI does

`release.yml` validates versions and creates a draft. Linux and Flipper tag-only
jobs upload directly to it. The Windows build stages four assets on Edison;
a separate trusted tag-only job verifies their hashes and uploads them from the
same runner. Publication does not depend on Actions artifact storage. All
enabled builds must succeed. The Linux job freezes the onedir engine, fails if it
references cuDNN or NVRTC, and builds two **component zips**: the engine
(`murmur-engine-linux-x86_64-<ver>.zip`) and the cuBLAS-only GPU add-on
(`murmur-cuda-linux-x86_64-<ver>.zip`). `scripts/components/make-manifest.py`
writes `components-manifest.json` from those zips and the pinned Hugging Face
model files in `scripts/components/provenance.toml`. The first job signs it with a
throwaway key and runs every test: the app packages (AppImage, `.deb`, tar.gz,
none of which contain the engine or CUDA), component installs through the app's
own verifier, startup with and without an engine, the offline Docker clean
install, and the size budgets.

On tags, and only when the release key is configured
([signing.md](signing.md)), the `release` job runs in the protected
`release-signing` environment: it rebuilds, signs the manifest with the release
subkey, embeds it, repeats the tests, and requires a trusted compatible NVIDIA GPU
and an offline staged `distil-small.en` model at `MURMUR_GPU_TEST_MODELS`. Real
inference with the installed engine and GPU add-on produces `GPU-VALIDATION.json`
tied to the final asset hashes. Missing hardware or model fails the tag job before
upload. Without the release key nothing is uploaded.

The final job downloads every asset and runs `scripts/check-release.py`. It
requires the portable archive, `.deb`, AppImage, version-matched `.fap`,
`GPU-VALIDATION.json`, both component zips, `components-manifest.json`, its
`.sig` and `murmur-release.asc`. Loose app and engine files are not release assets.
The manifest's zip URLs must point at this same release, with matching sizes and
SHA-256; model files must come from `huggingface.co`. The signature must verify
against the certificate and its primary fingerprint must equal
`MURMUR_RELEASE_TRUST_FINGERPRINT`. The final job also checks the archive layout,
AppImage match, GPU evidence hashes (cuBLAS loaded from the installed add-on), and
per-asset size budgets: AppImage 125 MiB, `.deb` 125 MiB, tar.gz 130 MiB, engine zip
130 MiB, CUDA zip 520 MiB, manifest/signature/certificate 1 MiB. It generates and
verifies `SHA256SUMS`, uploads it, and leaves the release as a draft for testing. Unexpected or missing
files block publication. Record three short deliveries for each terminal/editor
and overlay on/off combination, with hotkey and companion triggers where
available. Report the original X11 focus symptom separately if it recurs.

After the workflow succeeds and manual checks pass, publish the validated draft
from GitHub Releases, or run `gh release edit vMAJOR.MINOR.PATCH --draft=false --latest`.

A failed build leaves a draft. Fix the failure and rerun the workflow while it is
still a draft. A published release cannot be overwritten by the prepare job.

## Runner settings

`LINUX_RUNNER` is a JSON array of runner labels. When unset, builds use
`ubuntu-22.04`. The machine needs the native Linux build dependencies, Python,
Node, Rust, GitHub CLI, and sufficient disk space. Linux engine builds use
CPython 3.12 and the checked-in wheel hash lock; release validation uses Python
3.11 or later. Set `MURMUR_GPU_TEST_MODELS` only on the trusted GPU runner, and
`MURMUR_CLEAN_TEST_MODEL` (a staged `distil-small.en` folder) where Docker is
available for the offline clean-install test. `MURMUR_RELEASE_TRUST_FINGERPRINT`
and the `release-signing` environment are described in [signing.md](signing.md).

Flipper is required and no longer gated by `ENABLE_FLIPPER`.

## Windows assets

With `ENABLE_WINDOWS=true` and `WINDOWS_RUNNER` set, `release.yml` also calls
`windows-build.yml` on the Edison runner. It attaches to the **draft**:

| Asset | Budget | Notes |
| --- | ---: | --- |
| `Murmur_<ver>_x64-setup.exe` | 35 MiB | Per-user NSIS installer, app only, embedded WebView2 bootstrapper |
| `murmur-windows-x86_64.zip` | 35 MiB | `murmur.exe` + `README.txt` only |
| `murmur-engine-windows-x86_64-<ver>.zip` | 130 MiB | Onedir engine component |
| `murmur-cuda-windows-x86_64-<ver>.zip` | 520 MiB | cuBLAS-only GPU add-on; 429.9 MiB when built from the pinned wheel on Linux, to be re-measured on Edison |
| `GPU-VALIDATION-windows.json` | 1 MiB | Only when `WINDOWS_GPU_TEST_MODELS` is set (the Edison GPU gate) |

The Linux release job signs **one** manifest for both platforms, after checking
that the Windows zips it downloads from the draft match the hashes the Windows job
recorded. The `windows-gpu` job then runs the full GPU matrix on Edison against
the final signed assets; `validate` requires the Windows set (`--windows`) and,
when the GPU job ran, its evidence (`--windows-gpu`): forced CUDA and Auto on the
GPU, cuBLAS loaded from the installed add-on, `nvcuda.dll` from the driver, no
cuDNN/NVRTC. Without that evidence the Windows GPU option must stay hidden
(`WINDOWS_GPU_OFFERED = false`).

**Authenticode.** Windows builds are unsigned until the owner adds
`WINDOWS_CERT_PFX_BASE64` and `WINDOWS_CERT_PASSWORD`; the job summary says
"Unsigned", asset names stay the same, and the release notes must say so.
Never invent or borrow a certificate.

Before publishing a release with Windows assets, run
[the Windows checklist](parity/windows-checklist.md) on Edison in the logged-in
session; CI cannot test hotkeys, injection, the overlay or the tray.
The standalone Flipper workflow ignores tag pushes because `release.yml` owns
tag builds. Fork pull requests are skipped when a self-hosted runner is configured.

## After publishing

Download the public assets and verify their checksums. Install on a clean Linux
account, test recording and injection into an editor and terminal, and load the
`.fap` on real hardware. Keep the matching source tag available alongside the
GPL license. Record any hardware checks that were not performed.

For a public announcement, link to `/releases/latest` and state Linux/X11 support.
Do not advertise Windows or an Android APK until their downloads and device checks
(for Windows: [the checklist](parity/windows-checklist.md)) are complete.
