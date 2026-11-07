# Test ledger

One row per check and platform. **executed** = run for this change with the
result shown; **blocked** = cannot run with the available machines or tools;
**not run** = could run but was not run for this change. Evidence is a command,
log excerpt, or file in this repository. A build or DOM test passing is not a
hardware result.

Machine for every Linux row unless noted: einstein-probe, Ubuntu 24.04.5 LTS,
x86_64, RTX 4060 Laptop GPU, NVIDIA driver 580.178.04, Python 3.12, Node 22.

## PR-B (component-manager), 2026-11-07

| Check | Platform | Status | Evidence |
| --- | --- | --- | --- |
| `npm run build && npm run test:ui` | Linux | executed | 48/48, including new DOM tests: setup step order, "Set up later" persists and never reopens, window-close postpone hides the wizard, consent gates the download button, size totals, GPU option hidden without an NVIDIA GPU and recommended with one, language filters models and keeps translate for multilingual only, Components page states/actions (available, installing with live progress, paused, failed, unverified, corrupt, in use, depended on), missing trust root disables installs |
| `python3 -m unittest discover -s scripts/tests -p 'test_*.py'` | Linux | executed | 76 OK, including new: deterministic LZMA zips that drop internal links and refuse escaping/dangling ones, `.deb` holds only the app, new budgets, manifest URLs must point at the same release with matching size/SHA-256, model files only from huggingface.co, missing signature/certificate blocks release, packaged-CUDA GPU evidence rejected |
| `engine/.release-venv/bin/python -m pytest engine/tests -q` | Linux | executed | 53 passed; `models.install` refused, `model_path` from the app overrides the legacy lookup and `null` never falls back |
| `xvfb-run -a cargo test` | Linux | executed | 244 passed, 2 ignored (+ 11 + 2 in other targets). Components: trust (tampered manifest, bad signature, unknown key, primary-key signature, expired subkey, revoked subkey, revoked primary, SHA-1, DSA, trust-root fingerprint, certificate update rule), manifest schema, anti-rollback (lower serial, same serial different bytes, expired, clock earlier than issued), all-or-nothing fetch of manifest+sig+cert, fetch (every disallowed redirect class, Range resume with re-hash, retry budget 1+3, cancellation mid-body, stalled transfer), archive (traversal, absolute, symlink, device, FIFO, duplicate, case collision, reserved names, ADS, trailing dot/space, long paths, ratio bomb, entry-count bomb, oversize, Python LZMA), store (failed smoke test keeps previous version, atomic `active.json`, ELF `NEEDED` parsing, engine `--help`), service (end-to-end install via local HTTP server, removal refused when depended on / in use, interrupted job persists as paused and never auto-resumes, pause keeps the `.part` and resume uses Range, insufficient disk names the amounts, migration of matching and mismatching models, missing trust root refuses installs), engine lookup order and `model_path` |
| `xvfb-run -a cargo test --no-default-features` | Linux | executed | 244 passed, 1 ignored |
| `cargo test -- --ignored real_hosts` | Linux, internet | executed | Real Hugging Face LFS download of `tiny.en` `model.bin` through the CDN redirect, cancelled at 10 MB and resumed with Range, SHA-256 verified; GitHub release asset through its redirect |
| `cargo check --release --no-default-features` and `cargo test --no-run --no-default-features` for `x86_64-pc-windows-gnu` | Windows (cross-compile) | executed | Both pass; compiles the Windows branches (rename retry, `MURMUR_CUDA_DIR`, memory detection); nothing run |
| `scripts/build-linux.sh` end to end | Linux | executed | exit 0 (final run). Freezes the engine, cuDNN gate, builds both zips, creates a throwaway key, generates and signs the manifest, embeds it, builds the app, llama.cpp CPU audit, packages, then the bundle and startup tests and the size table. Earlier runs found two problems now fixed: the throwaway key was created after `issued_at` (rejected by the verifier, as it must be) and stale GPU evidence in a reused output folder |
| `scripts/test-linux-bundle.py` with components | Linux | executed | App packages contain no engine/CUDA; engine and CUDA zips installed through the AppImage's `murmur --install-component` (throwaway trust root + acknowledgement); engine cuDNN gate, RPC, `models.install` refused; add-on files, hashes and library resolution; tampered zip, missing trust root, and a release binary without the acknowledgement install nothing |
| `scripts/test-linux-startup.py --components` | Linux, Xvfb | executed | No engine: Settings opens, the app stays up, "Speech engine not installed" logged once, no engine process; with the engine component installed: that engine starts; login/autostart/relaunch pass |
| `scripts/test-linux-inference.py --components` (GPU) | Linux, RTX 4060, driver 580.178.04 | executed | Passed on the final build. Engine, CUDA add-on and `distil-small.en` installed through the app into a throwaway profile; forced CUDA with only the add-on directory on the loader path; all four precisions × VAD on/off gave the same transcript hash; Auto on GPU; CPU and GPU-hidden Auto on CPU. Loaded: `libcublas.so.12`, `libcublasLt.so.12` from `components/cuda/<version>/`, host `libcuda.so.580.178.04`; no cuDNN/NVRTC. Warm median 65.0 ms, cold 1.03 s. Evidence: `evidence/gpu-validation-components-distil-small-en-rtx4060.json` |
| `scripts/test-clean-install.sh` | Linux, Docker `ubuntu:24.04`, `--network none` | executed | Passed. Image = `ubuntu:24.04` + the `.deb`'s Depends resolved by apt (package removed again); test container offline. `dpkg -i`; Murmur ran 25 s on Xvfb without an engine and stayed up; strace saw 17 `connect()` calls, all `AF_UNIX`, none `AF_INET`/`AF_INET6`; "Speech engine not installed" logged; engine zip and the staged model installed offline through `--install-component`; CPU transcription "And so my fellow Americans." The first run found a real bug: apt satisfied `libasound2 \| libasound2t64` with the OSS shim `liboss4-salsa-asound2` and Murmur failed with `undefined symbol: snd_pcm_get_params`; fixed by depending on `libasound2t64 \| libasound2` |
| Memory while installing the CUDA add-on | Linux | executed | Release CLI: 20.8 s, peak RSS 785 MB, traced to the ELF smoke test reading `libcublasLt.so.12` whole; after the fix (debug CLI) peak RSS 78 MB. The packaged artifacts above were built just before this fix; CI rebuilds them from the final commit |
| `--install-component` with a model folder | Linux | executed | Staged `distil-small.en` matched the pinned manifest hashes and installed as `speech-distil-small.en` |
| gpg-made signature verified by the app | Linux | executed | Manifest signed by `sign-manifest.sh` (gpg, ed25519 subkey) verified by Sequoia in the app |
| Real release key, `release-signing` environment, tag build | GitHub | blocked | Owner has not created the key (docs/signing.md); release builds report "Release trust root missing" |
| In-app downloads of every model from Hugging Face | Linux | not run | ~8 GB; covered by the real-host test (one model) and pinned hashes from the Hub API |
| Screenshots of setup and Components | Linux | not run | DOM tests only |
| Real dictation with a microphone after setup | Linux | not run | Manual check before a release |
| Any Windows runtime check (component install, rename retry, engine stop before activation, CUDA) | Windows | blocked | No Windows machine or runner |

## PR-A (slim-linux), 2026-11-06

| Check | Platform | Status | Evidence |
| --- | --- | --- | --- |
| `npm ci && npm run build && npm run test:ui` | Linux | executed | 41/41 UI tests pass |
| `python3 -m unittest discover -s scripts/tests -p 'test_*.py'` | Linux | executed | 68 tests OK, including new: cuDNN/NVRTC gate fixtures (fake `.so` with `libcudnn` fails), size budgets and fail-closed unknown files, tar.gz layout, symlink dedupe, cuBLAS-only manifest `verify()`, `.deb` layout, inference origin checks |
| `engine/.release-venv/bin/python -m pytest engine/tests -q` | Linux | executed | 53 passed, including new `test_runtime.py` (`MURMUR_CUDA_DIR`, PyAV stub, offline/xet flags) |
| `xvfb-run -a cargo test --manifest-path src-tauri/Cargo.toml` | Linux | executed | 200 passed, 1 ignored (+ 11 + 2 in other targets); includes 8 new `engine::resolution_tests` |
| `xvfb-run -a cargo test … --no-default-features` | Linux | executed | 200 passed, 2 ignored |
| `scripts/build-linux.sh` end to end | Linux | executed | exit 0. First run stopped at the new budget gate (AppImage 703.7 MiB > 400 MiB); see [size report](../size-report.md#interim-budget-versus-the-plan) |
| cuDNN/NVRTC gate on frozen engine | Linux | executed | `check-engine-imports.py`: 169 files, no reference |
| llama.cpp CPU-flag audit on the stripped release binary | Linux | executed | "baseline Rust + AVX2 llama.cpp … clean" |
| `scripts/test-linux-bundle.py` | Linux | executed | AppImage tools/libraries, engine RPC, cuBLAS manifest, no duplicate libraries, stripped binaries, tar.gz = AppImage only, `.deb` engine/cuBLAS identical to AppImage, `.deb` depends, `.deb` app startup |
| `scripts/test-linux-startup.py` on the AppImage (extract-and-run) | Linux, Xvfb | executed | Manual launch opens Settings and the packaged engine starts; login/autostart migration/relaunch pass |
| Release app with no engine installed | Linux, Xvfb | executed | Raw `target/release/murmur` (no `../lib/murmur/engine`): app stays running, Settings opens, one log line `WARN murmur::engine: Speech engine is not installed`, no engine respawn after 8 s |
| AppImage launched normally (FUSE) | Linux, Xvfb | executed | zstd SquashFS mounted at `/tmp/.mount_Murmur…`; Settings visible; engine running from `usr/lib/murmur/engine` |
| `scripts/test-linux-inference.py` (GPU) | Linux, RTX 4060 | executed | Passed. Forced CUDA with only `usr/lib/murmur/cuda` on the loader path; Auto on GPU; CPU and GPU-hidden Auto on CPU; warm median 64.0 ms; cuBLAS from the package, host `libcuda.so.580.178.04`; no cuDNN/NVRTC loaded. Evidence `GPU-VALIDATION.json` (build artifact, not committed) |
| GPU precision × VAD matrix | Linux, RTX 4060 | executed | `int8_float16`, `float16`, `float32`, `int8`, VAD on/off: same transcript hash for all eight |
| GPU matrix, `large-v3-turbo` (1.6 GB) | Linux, RTX 4060 | executed | Same script and cuBLAS-only loader path: all eight precision × VAD combinations passed; warm median 412 ms; loaded CUDA libraries were only `libcublas.so.12`, `libcublasLt.so.12` and the host driver; no cuDNN/NVRTC. Evidence: `evidence/gpu-validation-large-v3-turbo-rtx4060.json` |
| GPU, `large-v3` (3.1 GB) | Linux, RTX 4060 (8 GB) | executed, partial | `int8_float16`, `float16`, `int8` transcribed correctly with cuBLAS only (warm 332–387 ms). `float32` fails with `CUDA failed with error out of memory`: 6 GB of weights plus activations exceed 8 GB VRAM. Hardware limit, not a missing library; unchanged from 0.4.7 |
| cuDNN/NVRTC gate on unfrozen site-packages | Linux | executed | `--site-packages` scan of ctranslate2, faster_whisper, onnxruntime, murmur_engine sources: only onnxruntime's never-called `preload_dlls()` name table, allowlisted while no CUDA provider ships |
| Frozen engine `models.install` over HTTPS without hf_xet | Linux | executed | `tiny.en` installed into a temporary directory (4 files, 75 MB), then deleted |
| Baseline screenshots, v0.4.7 AppImage | Linux, Xvfb | executed | 12 Settings pages + 2 overlay images in `captures/baseline-0.4.7/` |
| Screenshots of this branch | Linux | not run | PR-A changes no UI; pairs follow with PR-B's setup UI |
| `sudo apt install ./Murmur_*.deb` on a real system | Linux | not run | Would replace the 0.4.x package in use on this machine; the extracted package was started instead |
| Real dictation into X11 apps with a microphone | Linux | not run | Packaging-only change; required manual check before a release |
| `check-release.py assets` on a full tagged release | Linux | not run | Needs a tag and `.fap`; covered by unit tests |
| `linux-build.yml` on the self-hosted runner | Linux | not run | Runs on the pull request |
| Lock regeneration with `pip-compile --generate-hashes` | Linux | blocked | `pip-compile` not installed; lock hand-edited and checked against `uv pip compile` with the existing pins (identical package set), then installed with `--require-hashes` |
| `cargo check --release --no-default-features --target x86_64-pc-windows-gnu` and `cargo test --no-run` for the same target | Windows (cross-compile on Linux) | executed | Both finish; only pre-existing warnings. Compiles the Windows branches of `engine.rs`; does not run them (no Wine) and does not cover MSVC or the default Enhance backend |
| Windows MSVC build, PyInstaller freeze, NSIS installer, `windows-build.yml` | Windows | blocked | No Windows machine or runner; hosted runners billing-locked. `build-windows.ps1` and `windows-build.yml` were edited by reading only |
| Any Windows runtime check (engine resolution, `MURMUR_CUDA_DIR`, CUDA) | Windows | blocked | No Windows hardware attached; Edison runner not set up yet |
