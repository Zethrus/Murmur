# Build and install from source

Prebuilt downloads use an Ubuntu 24.04 / glibc 2.39 baseline. Building locally
links against your distribution's libraries instead.

Use a regular user account, not sudo. Prerequisites must already be installed:

- Node.js 22+, npm, Rust stable/Cargo (1.92+ recommended), Python 3.11+ with venv/pip.
- C/C++ build tools, CMake, Clang and libclang development libraries,
  pkg-config, WebKitGTK 4.1, libsoup 3, GTK3,
  Ayatana AppIndicator, librsvg, ALSA, X11 and XKB development libraries.
- `patchelf`, `file`, `squashfs-tools` and librsvg tools for bundles; `xvfb`, `xauth` and `dbus-run-session` for startup tests; `xprop` for focused-window detection, `xdotool` for typing and paste, and a running D-Bus session for Bluetooth.
- An X11 desktop, working microphone/audio stack and tray support. Native Wayland
  injection is unverified; use an X11 session if compositor restrictions interfere.

From the repository root:

```sh
./install.sh
~/.local/bin/murmur
```

The installer builds the Python engine in place at `${XDG_DATA_HOME:-~/.local/share}/murmur/engine`
(a venv from `engine/requirements-cuda.txt`, including the CUDA wheels so GPU inference
works on NVIDIA hardware), runs `npm ci`, then `npx tauri build` (which also builds
the frontend). This source installer does **not** freeze the interpreter: `murmur-engine` is a small
launcher that execs the venv with the CUDA libraries on `LD_LIBRARY_PATH`, which avoids packing CUDA into a single executable. The venv still needs several GB of disk space. Allow additional space for the Rust toolchain and build outputs.
It fails on missing prerequisites or build/copy verification errors. It does not
install system packages, fetch models, launch the app, or elevate privileges.

Installed layout (default XDG directories):

| Path | Purpose |
| --- | --- |
| `~/.local/bin/murmur` | Launcher; add `~/.local/bin` to PATH if needed |
| `~/.local/share/murmur/bin/murmur` | App binary |
| `~/.local/share/murmur/lib/murmur/engine/murmur-engine` | Launcher that runs the engine venv (CUDA-aware) |
| `~/.local/share/murmur/engine/.venv` | Local inference runtime (faster-whisper + CUDA wheels) |
| `~/.local/share/murmur/icons/<size>/murmur.png` | Private icon copies |
| `~/.local/share/icons/hicolor/*/apps/murmur.png` | Desktop-theme icons |
| `~/.local/share/applications/murmur.desktop` | Application-menu entry |
| `~/.config/autostart/murmur.desktop` | Login startup entry |

Application-menu and autostart entries follow `XDG_DATA_HOME` and `XDG_CONFIG_HOME`
when set; executable/icon files always use `~/.local`. Re-running updates files
without duplicating entries or replacing existing preferences/models. Atomic file
replacement supports updating installed binaries, but quit the old process before
launching the new version. Symlinked installation paths are rejected for safety.
Fresh installs create a minimal config with autostart enabled. Existing config is
preserved: its `general.autostart` preference takes precedence on app startup.
Use Settings → General to change it. Closing Settings leaves Murmur in the tray;
use tray → Quit to stop it. The source install ships its own engine (found as the
packaged engine); install a speech model in Settings → Components. Component
installs need a build with the release trust root or a development root
(`MURMUR_DEV_TRUST_ROOT`, see [components](components.md#development-trust-roots)).

### Uninstall

Quit Murmur first (otherwise it can recreate data), then from the checkout:

```sh
./uninstall.sh                    # binaries, icons, menu and startup entries only
./install.sh --uninstall          # same operation
./uninstall.sh --purge            # ALSO deletes config, models, history and logs
```

Uninstallation is idempotent. Purge covers the default and current XDG Murmur
config/data directories; former custom XDG locations cannot be discovered.
Neither form removes the source checkout, build caches, or `engine/.venv`.
Unrelated applications' files are not removed. No privileged install is supported.

### Manual run / development

```sh
mkdir -p .cache/tmp .cache/pip
export TMPDIR="$PWD/.cache/tmp" PIP_CACHE_DIR="$PWD/.cache/pip"
python3 -m venv engine/.venv
engine/.venv/bin/python -m pip install -r engine/requirements-cuda.txt pyinstaller pytest
npm ci
npm run tauri dev                # desktop app; source Python engine from the venv
# npm run dev                   # browser UI only: no Rust IPC
```

For a release build without installing into your home directory:

```sh
scripts/build-linux.sh          # frozen engine, component zips + signed manifest, app, deb, AppImage
# Run the raw release binary against the onedir engine it was built with:
MURMUR_ENGINE=engine/dist/murmur-engine src-tauri/target/release/murmur
# Or install the built components into a throwaway profile through the app itself:
export XDG_DATA_HOME=/tmp/murmur-try MURMUR_DEV_TRUST_ROOT=$PWD/.cache/artifacts/components/murmur-release.asc \
       MURMUR_DEV_TRUST_ROOT_ACK=I-understand-this-disables-release-trust
src-tauri/target/release/murmur --install-component .cache/artifacts/components/murmur-engine-linux-x86_64-*.zip \
  --manifest .cache/artifacts/components/components-manifest.json \
  --manifest-sig .cache/artifacts/components/components-manifest.json.sig
```

Without `MURMUR_SIGNING_SUBKEY` the build signs the manifest with a throwaway key
(`scripts/components/dev-key.sh`): such artifacts install nothing unless the
development trust root above is set. See [signing](signing.md).

Linux release builds use CPython 3.12 and `scripts/prepare-linux-release.sh` in
both local and CI builds. It creates a clean `engine/.release-venv` and installs
the SHA-256 locked wheel set in `engine/requirements-linux-release.txt`; the
development `engine/.venv` is separate. The lock pins faster-whisper 1.2.1,
CTranslate2 4.6.3, PyInstaller 6.22.3, and the CUDA 12.9 cuBLAS wheel. The
NVRTC wheel is locked only because the cuBLAS wheel depends on it; it is not
shipped. The CUDA wheel hashes were checked against PyPI's published files.
`engine/requirements-linux-release.in` records the resolution inputs.

`scripts/engine-sidecar.sh --build` freezes the engine with PyInstaller
`--onedir` into `engine/dist/murmur-engine/` (executable plus `_internal/`).
PyAV and hf_xet are excluded (`murmur_engine/runtime.py` stubs PyAV; Murmur
sends decoded PCM and downloads over plain HTTPS). `scripts/check-engine-imports.py`
then fails the build if any file in that folder references cuDNN or NVRTC.

The default build directory is `src-tauri/target`; manual build-linux.sh honors
Cargo target overrides. The installer and CI deliberately pin that directory.
The app downloads are in `.cache/artifacts/linux`, the components in
`.cache/artifacts/components`. AppImage creation is required.

`scripts/bundle-linux-runtime.py components` zips the onedir engine and the
pinned cuBLAS libraries with their NVIDIA notice (LZMA, deterministic, no
links). `scripts/components/make-manifest.py` writes the manifest;
`sign-manifest.sh` signs it. `scripts/bundle-linux-runtime.py package` adds
typing tools and missing client libraries to Tauri's AppImage, replaces duplicate
library copies with symlinks and repacks the SquashFS with zstd; the portable
archive holds only that AppImage, named `murmur`; the `.deb` is Tauri's package
(app in `/usr/bin`, system `Depends`), xz-compressed. Neither contains the
engine or CUDA. `scripts/test-linux-bundle.py <linux dir> <components dir>`
checks the packages, then installs the engine and CUDA add-on through the app's
`--install-component` path into a throwaway profile, checks the engine (cuDNN
gate, RPC, refuses `models.install`) and the add-on (exact files, hashes,
library resolution), and confirms that a tampered zip, a missing trust root, or a
release binary without the acknowledgement installs nothing. It also starts the
extracted `.deb` app without an engine.

`scripts/test-clean-install.sh <linux dir> <components dir> <staged model>`
installs the `.deb` into an offline `ubuntu:24.04` container (`--network none`;
only the image build with the package's `Depends` uses the network), checks that
Murmur starts on Xvfb without an engine and makes no IP connection (strace),
then installs the engine zip and the model folder and transcribes the fixture on
the CPU.

`python3 scripts/check-release.py sizes <files or folders>` prints download and
unpacked sizes; `budgets <dir>` enforces the per-asset budgets. Current
measurements are in [the size report](size-report.md).

`scripts/test-linux-startup.py [--components DIR]` launches the AppImage on an
isolated X11 display and D-Bus session to check manual startup without an engine
(stays up, reports it once, never respawns), with the engine component installed
(that engine starts), background startup, autostart migration, and manual
reactivation.

These bundles still use the host's glibc, desktop services, and hardware drivers.
Build on the oldest supported distribution; the current release baseline is
Ubuntu 24.04 with glibc 2.39. GPU inference needs a compatible host NVIDIA
driver. [NVIDIA lists](https://docs.nvidia.com/cuda/archive/12.9.0/cuda-toolkit-release-notes/index.html)
575.51.03 as the minimum for CUDA 12.9 GA; the tested RTX 4060 Laptop GPU uses
driver 580.178.04. Other hardware and driver combinations require validation.
The build copies the cuBLAS wheel's `License.txt` into the CUDA add-on zip. Before public distribution, review the
exact wheel notice and the [CUDA](https://docs.nvidia.com/cuda/eula/index.html)
distribution terms for the intended package; this build check does not make a
legal determination.

For a trusted GPU host with an already staged model, run
`python3 scripts/test-linux-inference.py --artifacts .cache/artifacts/linux
--components .cache/artifacts/components --models-dir /path/to/staged/models`.
It installs the engine, the CUDA add-on and the model (verified against the
manifest) into a throwaway profile through the app, runs the cuDNN gate on the
installed engine, then performs offline VAD-disabled forced CUDA (with only the
add-on's cuBLAS folder on the loader path, plus every GPU precision with VAD on
and off), Auto, explicit CPU, and GPU-hidden Auto transcription. It checks that
cuBLAS loads from the add-on and that no cuDNN or NVRTC library loads, and writes
`GPU-VALIDATION.json` tied to the asset hashes. The
fixture is a three-second excerpt of [OpenAI Whisper's `tests/jfk.flac`](https://github.com/openai/whisper/blob/main/tests/jfk.flac). Pass
`--baseline-engine` and `--baseline-libraries` to compare ten warmed runs with
a known working manual-path engine. The first inference is recorded separately.
Timings for the current build are recorded in [the size report](size-report.md).

## Windows build

**Not yet run on Windows hardware**: the scripts were written and reviewed without
a Windows machine; the Edison runner ([ci-windows.md](ci-windows.md)) will be the
first to execute them. Use x64 Windows 10/11 with Python 3.12 x64 (the engine lock
is for CPython 3.12), Node 22, Rust stable with the MSVC toolchain, Visual Studio
2022 C++ Build Tools and Windows SDK, CMake, LLVM (libclang; set `LIBCLANG_PATH`
if needed), NSIS and WebView2. No administrator rights are needed for the build.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-windows.ps1
```

The process-scoped execution-policy override does not change the machine policy.
The script:

1. `scripts\windows\freeze-engine.ps1`: creates `engine\.release-venv`, installs
   `engine\requirements-windows-release.txt` with `--require-hashes` (win_amd64
   wheels for the same versions as the Linux lock), freezes the onedir engine into
   `engine\dist\murmur-engine\`, removes CTranslate2's unused `cudnn64_9.dll`
   shim, runs `murmur-engine.exe --help`, and the cuDNN/NVRTC gate
   (`scripts\check-engine-imports.py`, with pefile and the unfrozen
   site-packages).
2. `npm ci` and `npm run build`.
3. `npx tauri build --target x86_64-pc-windows-msvc`; `tauri.windows.conf.json`
   selects the per-user NSIS installer with the embedded WebView2 bootstrapper.
   With `WINDOWS_CERT_PFX_BASE64`/`WINDOWS_CERT_PASSWORD` set, `murmur.exe` and the
   installer are Authenticode-signed (`scripts\windows\authenticode.ps1`);
   otherwise they stay unsigned. Then the llama.cpp CPU-flag audit.
4. `scripts\windows\package.ps1` writes:
   - `.cache\artifacts\windows\Murmur_<ver>_x64-setup.exe`: the app only;
   - `.cache\artifacts\windows\murmur-windows-x86_64.zip`: `murmur.exe` and
     `README.txt` (no engine folder);
   - `.cache\artifacts\components-windows\murmur-engine-windows-x86_64-<ver>.zip`
     and `murmur-cuda-windows-x86_64-<ver>.zip` (cublas64_12.dll,
     cublasLt64_12.dll, NVIDIA's licence and a provenance file, from the
     `nvidia-cublas-cu12` 12.9.2.10 win_amd64 wheel pinned in
     `engine\requirements-windows-cuda.txt`; no cuDNN, no NVRTC);
   - `components-windows-inputs.json` for the signing job,
   and enforces the size budgets. `-SkipComponents` builds only the app.

Python is not required on the destination PC. The app downloads the engine on
first run, verified against the signed manifest; a development build accepts a
throwaway manifest only with `MURMUR_DEV_TRUST_ROOT` (+ acknowledgement in release
builds), as on Linux. To try a local engine without the component manager, point
`MURMUR_ENGINE` at `engine\dist\murmur-engine` and, for CUDA, `MURMUR_CUDA_DIR`
at a folder with the two cuBLAS DLLs.

Cross-checking Windows code from Linux (what this repository's CI cannot run on
Windows yet):

```sh
rustup target add x86_64-pc-windows-gnu
cargo check --manifest-path src-tauri/Cargo.toml --no-default-features --target x86_64-pc-windows-gnu
cargo test --manifest-path src-tauri/Cargo.toml --no-default-features --target x86_64-pc-windows-gnu --no-run
```

This type-checks every `cfg(windows)` path with the GNU target (MSVC linking and
the default Enhance backend are not covered: llama.cpp does not build against
older mingw-w64 headers). Nothing runs; Windows behaviour still needs
[the Windows checklist](parity/windows-checklist.md).

## Checks before a pull request

```sh
npm ci
npm run build
npm run test:tokens
npm run test:ui
python3 -m unittest discover -s scripts/tests -p 'test_*.py'
(cd engine && .venv/bin/python -m pytest tests -q)
xvfb-run -a cargo test --locked --manifest-path src-tauri/Cargo.toml
xvfb-run -a cargo test --locked --manifest-path src-tauri/Cargo.toml --no-default-features
cc -std=c11 -Wall -Wextra -Werror -o /tmp/murmur-protocol-test companion/tests/protocol_test.c companion/flipper/src/protocol.c
/tmp/murmur-protocol-test
```

Use `xvfb-run` to keep GTK tests off your desktop. Install Xvfb if your system does
not provide it. Engine tests mock downloads and model inference; they do not
measure recognition quality. Real microphone, hotkey and Flipper checks are
listed in [the manual test checklist](parity/README.md).

[Back to the README](../README.md)
