# Test ledger

One row per check and platform. **executed** = run for this change with the
result shown; **blocked** = cannot run with the available machines or tools;
**not run** = could run but was not run for this change. Evidence is a command,
log excerpt, or file in this repository. A build or DOM test passing is not a
hardware result.

Machine for every Linux row unless noted: einstein-probe, Ubuntu 24.04.5 LTS,
x86_64, RTX 4060 Laptop GPU, NVIDIA driver 580.178.04, Python 3.12, Node 22.

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
