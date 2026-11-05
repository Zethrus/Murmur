# Build and install from source

Prebuilt downloads use an Ubuntu 24.04 / glibc 2.39 baseline. Building locally
links against your distribution's libraries instead.

Use a regular user account, not sudo. Prerequisites must already be installed:

- Node.js 22+, npm, Rust stable/Cargo (1.92+ recommended), Python 3.11+ with venv/pip.
- C/C++ build tools, CMake, Clang and libclang development libraries,
  pkg-config, WebKitGTK 4.1, libsoup 3, GTK3,
  Ayatana AppIndicator, librsvg, ALSA, X11 and XKB development libraries.
- `patchelf`, `file` and librsvg tools for bundles; `xprop` for focused-window detection, `xdotool` for typing and paste, and a running D-Bus session for Bluetooth.
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
| `~/.local/share/murmur/bin/murmur-engine` | Launcher that runs the engine venv (CUDA-aware) |
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
python3 -m venv --system-site-packages engine/.venv
engine/.venv/bin/python -m pip install -r engine/requirements-cuda.txt pyinstaller pytest
npm ci
npm run tauri dev                # desktop app; source Python engine from the venv
# npm run dev                   # browser UI only: no Rust IPC
```

For a release build without installing into your home directory:

```sh
scripts/build-linux.sh          # frozen engine + app + deb; AppImage best-effort
# Keep the frozen engine beside the raw release binary for manual execution:
cp src-tauri/binaries/murmur-engine-x86_64-unknown-linux-gnu src-tauri/target/release/murmur-engine
src-tauri/target/release/murmur
```

The default build directory is `src-tauri/target`; manual build-linux.sh honors
Cargo target overrides. The installer and CI deliberately pin that directory.
An extracted portable Linux archive includes the executable pair. A raw CI
artifact download can lose executable bits: run `chmod +x murmur murmur-engine`
or use the `.tar.gz`, which preserves permissions. Linux bundles need compatible
system webview/audio libraries; portable does not mean a fully static application.

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
`murmur-engine.exe` with the CUDA runtime hook and any installed NVIDIA modules,
and copies the target-suffixed sidecar into `src-tauri\binaries`. It runs `npm ci`,
`npm run build`, installs the Cargo Tauri CLI matching the locked npm CLI version,
and runs `cargo tauri build --target x86_64-pc-windows-msvc`. The existing
`tauri.windows.conf.json` selects NSIS automatically.

Printed outputs are under `src-tauri\target\x86_64-pc-windows-msvc\release`:

- `murmur.exe` and `murmur-engine.exe`: portable pair; keep them in the same folder.
- `bundle\nsis\*-setup.exe`: unsigned NSIS installer (per-user default).

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
