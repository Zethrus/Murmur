# Publishing a release

The public release currently requires Linux x86_64 and Flipper Zero. Windows and
Android are deferred in [the roadmap](roadmap.md).

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

`release.yml` validates versions and creates a draft. Linux and Flipper builds
upload directly to that draft, so publication does not depend on Actions artifact
storage. Both builds must succeed. The Linux job bundles typing utilities,
client libraries, and pinned CUDA userspace, then verifies AppImage and Debian
runtime parity. Tag builds require a trusted compatible NVIDIA GPU and an offline
staged `distil-small.en` model at `MURMUR_GPU_TEST_MODELS`. Real VAD-disabled
inference produces `GPU-VALIDATION.json` tied to final Linux asset hashes. Missing
hardware or model fails the tag job before upload. Pull request and manual jobs
explicitly report the GPU gate skipped.

The final job downloads every asset and runs `scripts/check-release.py`. It
requires the portable archive, `.deb`, AppImage, version-matched `.fap`, and
`GPU-VALIDATION.json`. Loose app and engine files are not release assets.
The Linux build checks that the portable archive contains the same AppImage and
bundled engine, both executable. The final job checks the archive layout,
AppImage match, GPU evidence hashes, and 2 GiB per-asset limit. It generates and
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
3.11 or later. Set `MURMUR_GPU_TEST_MODELS` only on the trusted GPU runner.

Flipper is required and no longer gated by `ENABLE_FLIPPER`. Windows development
CI remains behind `ENABLE_WINDOWS`; it is not called by today's release workflow.
The standalone Flipper workflow ignores tag pushes because `release.yml` owns
tag builds. Fork pull requests are skipped when a self-hosted runner is configured.

## After publishing

Download the public assets and verify their checksums. Install on a clean Linux
account, test recording and injection into an editor and terminal, and load the
`.fap` on real hardware. Keep the matching source tag available alongside the
GPL license. Record any hardware checks that were not performed.

For a public announcement, link to `/releases/latest` and state Linux/X11 support.
Do not advertise Windows or an Android APK until their downloads and device checks
are complete.
