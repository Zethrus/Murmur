<p align="center">
  <img src="docs/branding/readme-banner.svg" alt="Murmur. Your voice, on your machine. Local dictation for Linux, with a Flipper Zero remote." width="100%">
</p>

<p align="center">
  <a href="https://github.com/Zethrus/Murmur/releases/latest">Download for Linux</a> ·
  <a href="companion/README.md">Flipper Zero setup</a> ·
  <a href="docs/building.md">Build from source</a> ·
  <a href="docs/roadmap.md">What's next</a>
</p>

<p align="center">
  <a href="https://github.com/Zethrus/Murmur/actions/workflows/release.yml"><img src="https://github.com/Zethrus/Murmur/actions/workflows/release.yml/badge.svg" alt="Release workflow"></a>
  <a href="https://github.com/Zethrus/Murmur/releases/latest"><img src="https://img.shields.io/github/v/release/Zethrus/Murmur?color=7aa2f7" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-7aa2f7" alt="GPL version 3"></a>
</p>

Murmur turns speech into text in the application you're using. Hold **Ctrl+Alt+D**,
speak, and release. Recognition runs on your computer. There is no account,
subscription, or cloud transcription service.

Use it for messages, notes, editor prompts, or code. A small overlay shows the
transcript as it arrives. The optional Flipper Zero app gives you a physical
push-to-talk button and a screen for live text.

## What ships today

**Linux x86_64 and the Flipper Zero companion.** Current prebuilt downloads use
Ubuntu 24.04 as their build baseline and require **glibc 2.39 or newer**, a working Linux desktop, and hardware drivers. On older distributions, build from source. Use an X11
desktop with a working microphone and system tray. Native Wayland input is not verified.

| Download | Download size | Use it for |
| --- | ---: | --- |
| `.deb` | 11 MB | Debian/Ubuntu installation; uses the distribution's WebKitGTK, GTK and xdotool (`apt` installs them) |
| `.AppImage` | 91 MB | Recommended portable download, with runtime and typing tools included |
| `murmur-linux-x86_64.tar.gz` | 90 MB | The same AppImage, named `murmur`, in a tarball |
| `murmur-companion-vX.Y.Z.fap` | < 0.1 MB | The Flipper Zero remote, matched to the desktop version |
| `SHA256SUMS` | — | Checking that the downloaded files match the release |

[Get the latest release](https://github.com/Zethrus/Murmur/releases/latest).
The packages contain only the app. On first start, Murmur's setup downloads what
you choose: the speech engine (65 MB), one speech model (75 MB to 3.1 GB;
`distil-small.en` is 336 MB) and, on NVIDIA GPUs, an optional cuBLAS-only GPU
add-on (456 MB). Each file is checked against a list signed with Murmur's
release key before it is used ([components](docs/components.md)). A typical
CPU setup is about 490 MB in total instead of 2 GB with 0.4.7. Sizes and how they
were measured: [size report](docs/size-report.md).

The Android APK is planned for **November 6, 2026**. These are targets, subject to
build and device testing. See [the roadmap](docs/roadmap.md).

### Windows (preview, not yet tested on Windows hardware)

Windows support is implemented but **has not run on a Windows PC yet**: it was
compiled and reviewed, and the Windows CI runner that will build and test it is
still being set up ([CI and runner](docs/ci-windows.md)). Until a release lists
Windows assets, there is no Windows download. When it does:

| Download | Expected size | Use it for |
| --- | ---: | --- |
| `Murmur_<ver>_x64-setup.exe` | ~15–25 MB (estimate) | Per-user installer, no administrator rights; installs WebView2 if missing |
| `murmur-windows-x86_64.zip` | ~10–20 MB (estimate) | Portable: `murmur.exe` and a README |

- **Requirements:** Windows 10 1809 or later (x64) and the Microsoft Edge WebView2
  Runtime (included in Windows 11 and current Windows 10). The installer embeds
  Microsoft's WebView2 bootstrapper, which **needs an internet connection** when
  WebView2 is missing.
- **First run downloads the engine,** like Linux: setup shows the sizes first,
  and every component is verified against Murmur's signed list. The NVIDIA GPU
  option stays hidden on Windows until it has been validated on Windows hardware.
- **Unsigned.** There is no Authenticode certificate yet, so SmartScreen warns
  "Windows protected your PC". Check the SHA-256 against `SHA256SUMS` from the
  same release before choosing *More info → Run anyway*.
- Murmur cannot type into windows running as administrator unless Murmur is
  too; it holds the text with Copy/Discard instead
  ([troubleshooting](docs/troubleshooting.md#windows)).

## Install and dictate

Download a package and `SHA256SUMS` from the same release. In that directory:

```sh
sha256sum --check --ignore-missing SHA256SUMS
```

For Debian/Ubuntu, install the downloaded `.deb`:

```sh
sudo apt install ./Murmur_*_amd64.deb
murmur
```

For the AppImage:

```sh
chmod +x Murmur_*_amd64.AppImage
./Murmur_*_amd64.AppImage
```

If FUSE is unavailable, try `./Murmur_*_amd64.AppImage --appimage-extract-and-run`.
The AppImage bundles `xdotool`, `xprop`, WebKitGTK, GTK, and the audio client
libraries. The `.deb` declares them as package dependencies instead. Neither
needs a Python installation.

For the portable archive:

```sh
mkdir murmur-portable
tar -xzf murmur-linux-x86_64.tar.gz -C murmur-portable
./murmur-portable/murmur
```

The portable archive includes the same AppImage as `murmur`. If FUSE is
unavailable, run `./murmur-portable/murmur --appimage-extract-and-run`.
The host still provides glibc, the X11 desktop, audio services, and hardware drivers. On GNOME, enable a tray/AppIndicator extension if your
shell does not show tray icons.

1. Launch Murmur. The first time, **setup** asks for your languages, suggests a
   speech model for your hardware and offers the GPU add-on if you have an NVIDIA
   GPU; nothing downloads until you click **Download and install**. Choose **Set
   up later** to skip it; Settings → **Components** has the same choices.
2. Wait for the downloads to finish (they resume if interrupted).
3. In **Audio**, select your microphone.
4. Focus a text field. Hold **Ctrl+Alt+D**, speak, then release.

Press **Esc** while recording to cancel. For hands-free recording, switch the
hotkey to **Toggle** in General. Closing Settings keeps Murmur running; choose
**Quit** from the tray menu to exit.

Components download only when you click Install. After that, speech recognition
works offline. The engine includes its Python runtime, so Python, Rust and Node
are not needed. GPU inference needs the GPU add-on and a compatible host NVIDIA
driver; CPU inference works without a GPU. Models from 0.4.x are checked and
reused. For the source installation, see [building from source](docs/building.md).

> Until Murmur's release signing key is published, development builds cannot
> install components ("Release trust root missing"); see
> [signing](docs/signing.md).

## Built for everyday dictation

- **Local recognition.** faster-whisper handles speech on CPU or a configured NVIDIA GPU.
- **Coding vocabulary.** Add project names to the dictionary, use spoken punctuation, and bind a separate coding shortcut.
- **A small overlay.** Watch live text, drag the overlay where you want it, and leave Settings closed.
- **Optional local rewriting.** Enhance can clean up punctuation and phrasing. You review the suggestion; Accept copies it to the clipboard.
- **A physical remote.** The Flipper companion supports USB and Bluetooth, push-to-talk, live text, and configurable buttons.
- **History you control.** Transcript history is off by default. If enabled, it stays in a local SQLite database.

Enhance downloads its own model when you explicitly install it. It needs roughly
1.1 GB of model storage and an AVX2/FMA/F16C-capable CPU. It is off by default and
does not replace text already entered into another application. See
[Enhance validation](docs/enhance-validation.md) for current limits.

## Flipper Zero

Download the `.fap` from the **same release** as the desktop app. Use qFlipper to
copy it to `apps/Tools` on the Flipper's SD card, then close qFlipper. Open
**Murmur Companion** on the device and enable Companion in Murmur's Settings.
Start with USB and a data cable.

Press **OK** to start or stop dictation. Enable **Push-to-talk** in Settings if
you prefer holding OK. Up requests a rewrite, Down copies the current suggestion,
and Back dismisses it. Button mappings can be changed in Settings.

See [the companion guide](companion/README.md) for Bluetooth, firmware
compatibility, and troubleshooting. No Flipper is required to use Murmur.

## Privacy

Speech recognition and rewriting run locally. Murmur has no telemetry and does
not save microphone recordings. It contacts the network only when you click
Install, Update or Check for updates, and only GitHub Releases and Hugging Face.
Enabling a companion sends text to that device.

Clipboard managers, the application you dictate into, and backups may retain
text independently. Linux typing uses a temporary text payload in a private
runtime directory. Read [the privacy notes](docs/privacy.md) before sharing logs
or app data.

## Help and development

| Guide | Contents |
| --- | --- |
| [Troubleshooting](docs/troubleshooting.md) | Audio, hotkeys, GPU fallback, tray, and companion problems |
| [Configuration](docs/configuration.md) | TOML settings, paths, and defaults |
| [Build instructions](docs/building.md) | Prerequisites, source install, uninstall, and tests |
| [Contributing](CONTRIBUTING.md) | Bugs, patches, and review expectations |
| [Release audit](docs/release-audit.md) | Findings, test results, and remaining checks |
| [Release process](docs/releases.md) | Required downloads, checksums, and publishing |
| [Roadmap](docs/roadmap.md) | Android and Windows plans |
| [Focus contract](docs/focus-contract.md) | When dictation is typed, held or copied |
| [Windows CI](docs/ci-windows.md) | The Edison runner, variables and the GPU gate |
| [Security](SECURITY.md) | Reporting a vulnerability |

The desktop app uses Tauri 2, Rust, and TypeScript. A separate Python process runs
faster-whisper/CTranslate2. The Flipper app is written in C.

Murmur is licensed under [GNU GPL version 3](LICENSE). Third-party libraries,
runtimes and downloaded models retain their own licenses.
