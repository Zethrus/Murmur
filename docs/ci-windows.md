# Windows CI and the Edison runner

Status, November 2026: written and reviewed, **never run**. Hosted GitHub runners
are billing-locked for this account, and the owner's Windows PC "Edison" is not
registered as a runner yet. Until both workflows below have passed once, treat
every Windows statement in the docs as "verified in source, not run on hardware".

## The runner

Edison is the owner's Windows 10/11 x64 PC with an NVIDIA GPU. It runs the GitHub
Actions runner as a **Windows service** under a dedicated standard account,
`murmur-ci` (not an administrator). Labels:

```
self-hosted, Windows, X64, gpu, nvidia
```

Provisioning is described step by step in the prompt handed to the owner's agent
on Edison, and implemented by `scripts/windows/provision-runner.ps1` (elevated
PowerShell; idempotent):

```powershell
.\scripts\windows\provision-runner.ps1                       # checks, account, tools, staged model
.\scripts\windows\provision-runner.ps1 -RunnerToken <token>  # + register the runner service
.\scripts\windows\provision-runner.ps1 -VerifyOnly           # report only
```

It checks Windows (21H2+), the NVIDIA driver (R528.33+, the Windows floor for
CUDA 12.x; never upgraded by the script) and 60 GB free; creates `murmur-ci` with "Log on as a service"; installs
Git, GitHub CLI, PowerShell 7, VS 2022 Build Tools (C++ and Windows SDK), CMake,
LLVM, rustup (machine-wide in `C:\rust`), Node 22, Python 3.12, NSIS, WebView2
and 7-Zip with winget; stages `distil-small.en` offline in
`C:\murmur-release-models`; downloads the runner (SHA-256 from the release notes)
and registers it with the one-time token the owner creates at
<https://github.com/Zethrus/Murmur/settings/actions/runners/new>. It never
installs the CUDA Toolkit or cuDNN: only the driver comes from the host, the app
brings cuBLAS. It does not run the "smoke test as `murmur-ci`" step (that needs
an interactive logon with the account's password); do that by hand, or let the
first workflow run prove the toolchain.

**The service session cannot run interactive tests.** Jobs run in session 0:
no visible desktop, no global hotkeys, no foreground window, no microphone
prompt. Overlay, hotkeys, injection, tray, autostart and DPI checks are done by
a person in the logged-in session with
[parity/windows-checklist.md](parity/windows-checklist.md) and
`scripts/parity/capture-windows.ps1`.

## Repository settings (owner)

| Kind | Name | Value | Effect |
| --- | --- | --- | --- |
| Variable | `WINDOWS_RUNNER` | `["self-hosted","Windows","X64","gpu"]` | `windows-build.yml` runs on Edison. Unset: `windows-latest` (hosted, needs billing) |
| Variable | `ENABLE_WINDOWS` | `true` | Windows workflow and the Windows part of `release.yml` run at all |
| Variable | `WINDOWS_GPU_TEST_MODELS` | `C:\murmur-release-models` | Enables GPU validation (manual runs with `gpu=true`, and release tags) |
| Secret | `WINDOWS_CERT_PFX_BASE64`, `WINDOWS_CERT_PASSWORD` | base64 PFX and its password | Optional Authenticode signing; absent → unsigned build, labelled as such |

Fork pull requests never run on the self-hosted runner: the job runs for them
only when `WINDOWS_RUNNER` is unset (hosted). Tags never fall back to a hosted
runner. The release jobs that use Edison's GPU hard-code the labels
`self-hosted, Windows, X64, gpu, nvidia`.

## What `windows-build.yml` does

Pull requests and manual runs (`workflow_dispatch`, input `gpu`):

1. Python 3.12 (`actions/setup-python` on hosted runners; the machine-wide install on Edison, because setup-python runs the MSI and edits HKLM, which the non-admin runner user cannot), Node 22, Rust (isolated toolchain), MSVC + CMake + libclang
   (`scripts/prepare-windows-native.ps1`).
2. Freeze the engine (`scripts/windows/freeze-engine.ps1`): onedir PyInstaller
   from `engine/requirements-windows-release.txt` with `--require-hashes`, prune
   CTranslate2's unused `cudnn64_9.dll` shim, `murmur-engine.exe --help`, then the
   **cuDNN/NVRTC gate**: `scripts/check-engine-imports.py` on the frozen folder
   (string scan + PE import and delay-import tables, pefile required on Windows)
   and on the unfrozen site-packages.
3. UI build and DOM tests, the Windows-relevant script tests, engine pytest,
   `cargo test` with and without the Enhance backend.
4. Authenticode certificate import (**release tags only**, dormant until the
   secrets exist), `tauri build` for the MSVC target (signing through
   `bundle.windows.signCommand` only on tags with an imported certificate),
   certificate removal (always), llama.cpp CPU-flag audit. Pull-request and
   manual builds never receive the certificate secrets, never hold the key in
   `Cert:\CurrentUser\My` and never produce Authenticode-signed artifacts;
   `scripts/tests/test_windows_ci.py` checks this in the workflow text.
5. `scripts/windows/package.ps1`: NSIS installer and portable zip (app only),
   engine zip, cuBLAS-only CUDA zip from the pinned wheel, the manifest input
   `components-windows-inputs.json`, size budgets.
6. Startup smoke test: `murmur.exe --version` and `--help` on every runner.
   On **hosted** runners only (`vars.WINDOWS_RUNNER` unset), also a launch with
   `--background` in a throwaway profile for 10 seconds; it must not exit with an
   error, must write its config, and must not install an engine by itself. The
   self-hosted runner is a Windows service in session 0, where the tray
   (`Shell_NotifyIcon` needs a shell) and WebView2 are unavailable, so the launch
   would fail for reasons unrelated to Murmur; on Edison the interactive
   checklist in the logged-in session covers launch, tray and Settings.
7. A Windows-only manifest signed with a throwaway key (never a release asset).
8. With `gpu=true`: `scripts/test-windows-inference.py` (see below).
9. Upload the artifacts (best effort, 3 days, marked untrusted-dev-signed).

Release tags (called from `release.yml`): steps 1–6 (on Edison, so without the
launch smoke), then the installer, portable
zip and both component zips are attached to the **draft** release with `gh`
(no third-party publish action), and the component inputs are passed to the
Linux job as a workflow output.

## Release flow with Windows enabled

```
prepare ─┬─ windows (Edison) ── linux (signs ONE manifest: Linux + Windows) ── windows-gpu (Edison) ─┐
         └─ flipper ─────────────────────────────────────────────────────────────────────────────────┴─ validate
```

- The Linux release job downloads the two Windows zips from the draft, and
  `make-manifest.py --windows-inputs` refuses them unless size and SHA-256 equal
  what the Windows job recorded. One signed manifest lists both platforms.
- `windows-gpu` downloads the final signed assets and runs the GPU matrix.
- `validate` runs `check-release.py assets <tag> <dir> --windows [--windows-gpu]`:
  exact asset set, budgets, portable zip = `murmur.exe` + `README.txt`, manifest
  entries for both platforms pointing at this release, and the GPU evidence.
- Nothing is ever published; the draft stays a draft.

## The GPU gate

`scripts/test-windows-inference.py` (shares `scripts/inference_common.py` with
the Linux script) extracts `murmur.exe` from the portable zip and installs the
engine, the CUDA add-on and the staged model into a throwaway `%LOCALAPPDATA%`
through the app's own verifier (`--install-component`). Then, with
`MURMUR_CUDA_DIR` pointing at the add-on (as the app passes it) and all host CUDA
removed from `PATH`/`CUDA_PATH`:

- forced CUDA: 1 cold + 10 warm runs, all four precisions × VAD on/off, device
  must stay `cuda` without fallback;
- loaded modules: `cublas64_12.dll` and `cublasLt64_12.dll` only from the add-on,
  `nvcuda.dll` from the driver, **no `cudnn*` or `nvrtc*` DLL**;
- Auto on GPU; explicit CPU and Auto with the GPU hidden stay on CPU.

It writes `GPU-VALIDATION-windows.json` with the SHA-256 of the installer,
portable zip, both Windows zips and the manifest. Decision 5 of the plan: the
GPU option stays hidden on Windows until this evidence exists for a release.
Flipping `WINDOWS_GPU_OFFERED` in `src-tauri/src/components/detect.rs` to `true`
is the only code change needed afterwards.

Static evidence gathered while writing this (not a substitute for the gate):
CTranslate2 4.6.3's `ctranslate2.dll` (win_amd64) has no cuDNN import, symbol or
string and loads `cublas64_12.dll` at run time; its wheel nevertheless ships the
266 KB cuDNN 9 dispatcher `cudnn64_9.dll`, which the freeze removes.
`cublasLt64_12.dll` 12.9.2.10 imports only `KERNEL32.dll` but names
`nvrtc64_120_0.dll` for an optional JIT path, exactly like Linux
`libcublasLt.so.12` (which never loaded NVRTC in the Linux GPU matrix).

PyInstaller caveat: CTranslate2 loads `cublas64_12.dll` at run time today, so
PyInstaller's dependency analysis does not see it. If a future CTranslate2
gains a *static* import of a CUDA DLL and a CUDA Toolkit is on the build
machine's `PATH`, PyInstaller may copy the host's `cublas64_12.dll` into
`_internal/`, silently bundling (and preferring) a host copy over the add-on.
The engine-zip budget would probably catch a ~100 MB jump, but the decisive
check is the Windows inference test: its module evidence fails unless cuBLAS
is loaded from the add-on directory. Edison has no CUDA Toolkit installed
(the provisioning script never installs one), which keeps this latent.

## Authenticode

There is no certificate. Builds are unsigned and say so in the job summary and
release notes; asset names do not change. When the owner adds
`WINDOWS_CERT_PFX_BASE64`/`WINDOWS_CERT_PASSWORD`, `scripts/windows/authenticode.ps1`
imports the PFX into the runner user's certificate store (the temporary file is
deleted at once), Tauri signs `murmur.exe` and the installer with `signtool`
(SHA-256, RFC 3161 timestamp) and verifies them, and the certificate and its key
are removed from the store in an `always()` step.
