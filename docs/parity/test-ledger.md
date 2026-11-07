# Test ledger

One row per check and platform. **executed** = run for this change with the
result shown; **blocked** = cannot run with the available machines or tools;
**not run** = could run but was not run for this change. Evidence is a command,
log excerpt, or file in this repository. A build or DOM test passing is not a
hardware result.

Machine for every Linux row unless noted: einstein-probe, Ubuntu 24.04.5 LTS,
x86_64, RTX 4060 Laptop GPU, NVIDIA driver 580.178.04, Python 3.12, Node 22.

## PR-C (windows-parity), 2026-11-07

**Nothing in this section ran on Windows.** There is no Windows machine, VM or
runner: hosted runners are billing-locked and Edison is not registered yet.
Every Windows behaviour is "verified in source, not run on hardware".

| Check | Platform | Status | Evidence |
| --- | --- | --- | --- |
| `xvfb-run -a cargo test --manifest-path src-tauri/Cargo.toml` | Linux | executed | 275 passed, 3 ignored (+ 11 + 2 in other targets); 255 before PR-C. New: focus identity with PID/start time (reused HWND, reused PID, unreadable start time), blocked targets never typed into in any mode, Insert mode holds an unknown target, integrity-level and secure-desktop decisions, microphone consent decisions and non-Windows behaviour, modifier wait (release and 600 ms bound), hotkey edges and press-during-delivery, overlay MA_NOACTIVATE / foreground hand-back / subclass ownership decisions, Windows rename retry schedule (≤ 5 s), engine files never switched or removed while the engine runs, Windows GPU hidden flag and System32 `nvidia-smi` lookup, `--version`/`--help` |
| `xvfb-run -a cargo test … --no-default-features` | Linux | executed | 275 passed, 2 ignored |
| `cargo check --no-default-features --target x86_64-pc-windows-gnu` | Windows (cross-compile) | executed | Passes; compiles every new `cfg(windows)` path (token integrity, input desktop, process times, subclassing, registry, ShellExecuteW, GetAsyncKeyState); only pre-existing warnings. Type check only: GNU ≠ MSVC, nothing runs |
| `cargo test --no-run --no-default-features --target x86_64-pc-windows-gnu` | Windows (cross-compile) | executed | Builds all four test executables (lib, bin, companion client, network); includes the Windows-only tests (`components::detect`, `platform` on Windows). Not executed: no Windows/Wine |
| `cargo check` / `cargo test --no-run` for `x86_64-pc-windows-gnu` **with default features** | Windows (cross-compile) | blocked | llama.cpp (`llama-cpp-sys-2`) does not compile against this machine's mingw-w64 headers (`unknown type name 'THREAD_POWER_THROTTLING_STATE'`, also with `-D_WIN32_WINNT=0x0A00`); not Murmur code. The MSVC build on Edison covers it |
| `npm run build && npm run test:ui` | Linux | executed | 53/53 (50 before): overlay buttons never take focus on mouse down but still act on click; Audio explains a Windows microphone privacy block and opens its Settings page; no-device explanation on Windows and none on Linux |
| `npm run test:tokens` | Linux | executed | 20 passed |
| `python3 -m unittest discover -s scripts/tests -p 'test_*.py'` | Linux | executed | 109 OK (76 before): built-in PE import/delay-import parser on fake PE32/PE32+ images, Windows engine with a cuDNN delay-import fails on any host, pefile required on Windows and its tables merged (fake module); Windows component builder (shim pruned, cuBLAS-only zip, wrong wheel hash, linked NVRTC fails, lazy NVRTC name recorded), Windows locks are hash-pinned win_amd64; manifest signs Windows entries only with matching inputs; Windows release assets, portable layout, manifest entries, GPU evidence (add-on cuBLAS, host `nvcuda.dll`, no cuDNN/NVRTC, stale hashes) and budgets; shared inference core and the Windows script's pure parts (module origin, clean environment, evidence asset set) |
| `engine/.release-venv/bin/python -m pytest engine/tests -q` | Linux | executed | 53 passed (unchanged) |
| Real `nvidia-cublas-cu12` 12.9.2.10 win_amd64 wheel → `windows_components.py download-cuda` + `cuda` | Linux (Windows artifact) | executed | Downloaded with `pip --require-hashes --platform win_amd64`; add-on zip 450,766,521 bytes (429.9 MiB), 771 MB unpacked; `cublas64_12.dll` imports KERNEL32 + `cublasLt64_12.dll`; `cublasLt64_12.dll` imports KERNEL32 only and names `nvrtc64_120_0.dll` (lazy, as on Linux). First version of the builder rejected that string; changed to reject linked imports only and record lazy names |
| Real CTranslate2 4.6.3 win_amd64 wheel, static inspection | Linux (Windows artifact) | executed | `ctranslate2.dll`: no cuDNN import, symbol or string (ASCII or UTF-16); names `cublas64_12.dll` (run-time load). Wheel bundles `cudnn64_9.dll` (266 KB dispatcher, imports KERNEL32 only) → pruned from the freeze; the gate would otherwise fail |
| `bash -n scripts/build-linux.sh`; YAML parse of the three workflows | Linux | executed | OK |
| `cargo clippy --all-targets --no-default-features` (Linux and `x86_64-pc-windows-gnu`) | Linux / Windows (cross) | executed | No warnings in code added by PR-C (Windows-only decision helpers marked `allow(dead_code)` off Windows); remaining warnings pre-existing (`inject.rs` imports, `store.rs` CUDA smoke test, `detect.rs` memory query) |
| PowerShell syntax check of `build-windows.ps1`, `scripts/windows/*.ps1`, `scripts/parity/capture-windows.ps1` | — | blocked | No `pwsh` on this machine; read-only review only |
| Windows engine freeze, MSVC build, NSIS installer, portable zip, `windows-build.yml`, `release.yml` with Windows | Windows | blocked | No Windows machine or runner; never executed |
| Startup smoke (`--version`, `--help`, 10 s no-engine launch) on Windows | Windows | blocked | Same; `--version`/`--help` unit-tested on Linux |
| GPU inference on Windows (`test-windows-inference.py`) | Windows + NVIDIA | blocked | Edison not registered; `WINDOWS_GPU_OFFERED` stays `false` |
| B7 overlay focus, B8 modifiers, B9 elevated/secure desktop, B10 identity, microphone privacy, tray left-click, refocus refusal, rename retry, CUDA DLL loading | Windows | blocked | Hardware procedure: [windows-checklist.md](windows-checklist.md) (O3/O4, H1–H3, F9/F10, F3, M1–M4, T1, F5, C2) |
| Settings screenshots on Windows (`capture-windows.ps1`) | Windows | blocked | Logged-in Windows session needed |
| `provision-runner.ps1` on Edison | Windows | blocked | Owner's agent provisions Edison separately; script not run |
| Linux packaging build (`scripts/build-linux.sh`) after PR-C | Linux | not run | Only the manifest call changed (Windows inputs optional, off by default) and `write_zip` moved to `component_zip.py` (its tests pass); ~16 GB free disk; CI rebuilds |

## PR-B (component-manager), 2026-11-07

| Check | Platform | Status | Evidence |
| --- | --- | --- | --- |
| `npm run build && npm run test:ui` | Linux | executed | 50/50 (after the review follow-up; 48 before, +2: expired list stays listed, explains itself and gates Install/Resume while Verify/Remove work; setup opens with an expired list but cannot install), including new DOM tests: setup step order, "Set up later" persists and never reopens, window-close postpone hides the wizard, consent gates the download button, size totals, GPU option hidden without an NVIDIA GPU and recommended with one, language filters models and keeps translate for multilingual only, Components page states/actions (available, installing with live progress, paused, failed, unverified, corrupt, in use, depended on), missing trust root disables installs |
| `python3 -m unittest discover -s scripts/tests -p 'test_*.py'` | Linux | executed | 76 OK, including new: deterministic LZMA zips that drop internal links and refuse escaping/dangling ones, `.deb` holds only the app, new budgets, manifest URLs must point at the same release with matching size/SHA-256, model files only from huggingface.co, missing signature/certificate blocks release, packaged-CUDA GPU evidence rejected |
| `engine/.release-venv/bin/python -m pytest engine/tests -q` | Linux | executed | 53 passed; `models.install` refused, `model_path` from the app overrides the legacy lookup and `null` never falls back |
| `xvfb-run -a cargo test` | Linux | executed | 255 passed, 3 ignored after the review follow-up (244 before; see the next table) (the opt-in real-host test and two existing) (+ 11 + 2 in other targets). Components: trust (tampered manifest, bad signature, unknown key, primary-key signature, expired subkey, revoked subkey, revoked primary, SHA-1, DSA, trust-root fingerprint, certificate update rule), manifest schema, anti-rollback (lower serial, same serial different bytes, expired, clock earlier than issued), all-or-nothing fetch of manifest+sig+cert, fetch (every disallowed redirect class, Range resume with re-hash, retry budget 1+3, cancellation mid-body, stalled transfer), archive (traversal, absolute, symlink, device, FIFO, duplicate, case collision, reserved names, ADS, trailing dot/space, long paths, ratio bomb, entry-count bomb, oversize, Python LZMA), store (failed smoke test keeps previous version, atomic `active.json`, ELF `NEEDED` parsing, engine `--help`), service (end-to-end install via local HTTP server, unsafe versions in unsigned `jobs.json`/`active.json` refused, removal refused when depended on / in use, interrupted job persists as paused and never auto-resumes, pause keeps the `.part` and resume uses Range, insufficient disk names the amounts, migration of matching and mismatching models, missing trust root refuses installs), engine lookup order and `model_path` |
| `xvfb-run -a cargo test --no-default-features` | Linux | executed | 255 passed, 2 ignored (244 before the review follow-up) |
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

## PR-B review follow-up, 2026-11-07

After rebasing on `slim-linux` (a27179b, the `.deb` ALSA fix, now part of PR-A).

| Check | Platform | Status | Evidence |
| --- | --- | --- | --- |
| `xvfb-run -a cargo test --manifest-path src-tauri/Cargo.toml` | Linux | executed | 255 passed, 3 ignored (+ 11 + 2 in other targets). New: older certificate cannot undo a subkey revocation (stored file unchanged, manifest signed by the revoked subkey refused online and offline); revocation-only certificate update accepted and stored even though its manifest is refused, cached manifest then refused; stored update still covers manifests issued before its new subkey; certificate update rule without the usable-subkey requirement; DSA signing subkey under an Ed25519 primary (control: a permissive policy accepts the binding); trust-root assertion with `MURMUR_DEV_TRUST_ROOT` cleared; embedded manifest 24 h clock slack vs strict cached copy; expired manifest kept as a stale catalog, still subject to rollback; service: expired list stays listed, install/resume refused without network, verify/remove/dependencies work; failed activation and failed removal request an engine restart; failed activation keeps verified downloads and Retry makes no network request, same-size tampering caught by the re-hash; `engine/1.0.0/..` refused by every entry point and the store; `active.json` locations outside the component or legacy folders treated as absent |
| `xvfb-run -a cargo test … --no-default-features` | Linux | executed | 255 passed, 2 ignored |
| `npm run build && npm run test:ui` | Linux | executed | 50/50 |
| `python3 -m unittest discover -s scripts/tests -p 'test_*.py'` | Linux | executed | 76 OK (unchanged) |
| `engine/.release-venv/bin/python -m pytest engine/tests -q` | Linux | executed | 53 passed (unchanged) |
| `gpg --edit-key` → `key N` → `revkey` → `save` (docs/signing.md) | Linux, gpg 2.4.4 | executed | Throwaway ed25519 key: the subkey shows `revoked` afterwards |
| `cargo clippy --all-targets` on the changed files | Linux | executed | No new warnings (one pre-existing in `store.rs` CUDA smoke test) |
| Packaged build (`scripts/build-linux.sh`) after the follow-up | Linux | not run | No packaging script changed; disk space (~16 GB free); CI rebuilds |
| Any Windows runtime check of the follow-up | Windows | blocked | No Windows machine or runner |

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
