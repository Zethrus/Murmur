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
use tray → Quit to stop it. Install and select a model in Settings → Engine.

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
scripts/build-linux.sh          # frozen engine + bundled app, deb and AppImage
# Run the raw release binary against the onedir engine it was built with:
MURMUR_ENGINE=engine/dist/murmur-engine src-tauri/target/release/murmur
```

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
The final downloads are in `.cache/artifacts/linux`. AppImage creation is required.
`scripts/bundle-linux-runtime.py` adds typing tools, missing client libraries,
the onedir engine under `usr/lib/murmur/engine`, cuBLAS under
`usr/lib/murmur/cuda`, and notices to Tauri's AppImage. It replaces duplicate
library copies with symlinks and repacks the SquashFS with zstd. The portable
archive holds only that AppImage, named `murmur`. The `.deb` is Tauri's package
(app in `/usr/bin`, system `Depends`) plus the same engine and cuBLAS folders
under `/usr/lib/murmur`. `scripts/test-linux-bundle.py` checks the CUDA
manifest, library resolution, the engine and its cuDNN gate, Debian/AppImage
parity of the engine and cuBLAS, stripped binaries, symlinked libraries, the
archive layout, and the size budgets; it also starts the extracted `.deb` app.
Models download inside Settings.

`python3 scripts/check-release.py sizes <files or folders>` prints download and
unpacked sizes; `budgets <dir>` enforces the per-asset budgets. Current
measurements are in [the size report](size-report.md).

`scripts/test-linux-startup.py` launches the AppImage on an isolated X11 display
and D-Bus session to check manual startup, that the packaged engine starts,
background startup, autostart migration, and manual reactivation.

These bundles still use the host's glibc, desktop services, and hardware drivers.
Build on the oldest supported distribution; the current release baseline is
Ubuntu 24.04 with glibc 2.39. GPU inference needs a compatible host NVIDIA
driver. [NVIDIA lists](https://docs.nvidia.com/cuda/archive/12.9.0/cuda-toolkit-release-notes/index.html)
575.51.03 as the minimum for CUDA 12.9 GA; the tested RTX 4060 Laptop GPU uses
driver 580.178.04. Other hardware and driver combinations require validation.
The build copies the cuBLAS wheel's `License.txt` into
`usr/share/doc/murmur/bundled-runtime`. Before public distribution, review the
exact wheel notice and the [CUDA](https://docs.nvidia.com/cuda/eula/index.html)
distribution terms for the intended package; this build check does not make a
legal determination.

For a trusted GPU host with an already staged model, run
`python3 scripts/test-linux-inference.py --artifacts .cache/artifacts/linux
--models-dir /path/to/staged/models`. It runs the cuDNN gate on the packaged
engine, then performs offline VAD-disabled forced CUDA (with only the shipped
cuBLAS folder on the loader path, plus every GPU precision with VAD on and off),
Auto, explicit CPU, and GPU-hidden Auto transcription. It checks that cuBLAS
loads from the package and that no cuDNN or NVRTC library loads, and writes
`GPU-VALIDATION.json` tied to the built asset hashes. The
fixture is a three-second excerpt of [OpenAI Whisper's `tests/jfk.flac`](https://github.com/openai/whisper/blob/main/tests/jfk.flac). Pass
`--baseline-engine` and `--baseline-libraries` to compare ten warmed runs with
a known working manual-path engine. The first inference is recorded separately.
Timings for the current build are recorded in [the size report](size-report.md).

## Windows development build (deferred)

Windows downloads are not part of the Linux launch. See [the release schedule](roadmap.md).

**Unverified on real Windows hardware.** Use x64 Windows 10/11 with Python 3.12
(recommended), Node 22, Rust stable with the MSVC toolchain, Visual Studio C++ Build
Tools/Windows SDK, CMake, LLVM libclang (set `LIBCLANG_PATH` if needed),
and WebView2. Those prerequisites must already be available;
the script itself requires no administrator rights. Use a developer PowerShell
if the linker/SDK is not discoverable from a normal shell.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-windows.ps1
```

This process-scoped execution-policy override does not change the machine policy.
The script creates `engine\.venv`, installs requirements + PyInstaller, freezes
the onedir engine into `engine\dist\murmur-engine\` (no CUDA inside; set
`MURMUR_CUDA_DIR` to a folder with the CUDA DLLs), runs `npm ci`,
`npm run build`, installs the Cargo Tauri CLI matching the locked npm CLI version,
and runs `cargo tauri build --target x86_64-pc-windows-msvc`. The existing
`tauri.windows.conf.json` selects NSIS automatically. It then copies the engine
folder to `engine\` beside `murmur.exe`, where the app looks for it.

Printed outputs are under `src-tauri\target\x86_64-pc-windows-msvc\release`:

- `murmur.exe` and `engine\murmur-engine.exe`: portable layout; keep the `engine`
  folder beside `murmur.exe`.
- `bundle\nsis\*-setup.exe`: unsigned NSIS installer (per-user default). It does
  not include the engine yet.

Python is not required on the destination PC. Models are not bundled. WebView2
and compatible NVIDIA drivers (for CUDA) remain runtime prerequisites. Unsigned
builds may trigger SmartScreen; inspect the build source rather than disabling
system protections. Run Murmur and its target apps at the same privilege level;
Windows can block injection into elevated windows.

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
