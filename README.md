# Murmur

[![Linux build](https://github.com/Zethrus/Murmur/actions/workflows/linux-build.yml/badge.svg)](https://github.com/Zethrus/Murmur/actions/workflows/linux-build.yml)
[![Windows build](https://github.com/Zethrus/Murmur/actions/workflows/windows-build.yml/badge.svg)](https://github.com/Zethrus/Murmur/actions/workflows/windows-build.yml)
[![Release](https://img.shields.io/github/v/release/Zethrus/Murmur)](https://github.com/Zethrus/Murmur/releases)

Local, private dictation for Linux/X11 and Windows 10/11 x64. Hold a global hotkey,
speak, and Murmur inserts the transcript into the focused application. A small
interactive, draggable overlay shows recording, preview and completion; the tray opens
Settings. Coding mode handles technical vocabulary and spoken punctuation for
editor prompts, chat boxes and terminals.

Tauri v2 + TypeScript provide the UI; Rust handles audio, hotkeys and injection;
a separate frozen Python engine runs faster-whisper/CTranslate2. Models load on
first use and remain warm. Linux has been verified end-to-end. Windows builds
and runtime are **unverified on real Windows hardware**; CI compilation is not a
substitute for microphone, GPU, hotkey and desktop testing.

Screenshots: placeholders pending a matched Linux/Windows capture set. See the
[design-parity checklist and capture instructions](docs/parity/README.md).
The icon mark, palette and regeneration steps are documented in
[docs/branding](docs/branding/README.md).

## Linux installation

Use a regular user account, not sudo. Prerequisites must already be installed:

- Node.js 22+, npm, Rust stable/Cargo (1.92+ recommended), Python 3.10+ with venv/pip.
- C/C++ build tools, CMake, Clang and libclang development libraries,
  pkg-config, WebKitGTK 4.1, libsoup 3, GTK3,
  Ayatana AppIndicator, librsvg, ALSA, X11 and XKB development libraries.
- `patchelf`, `file` and librsvg tools for bundles; `xprop` for focused-window detection.
- An X11 desktop, working microphone/audio stack and tray support. Native Wayland
  injection is unverified; use an X11 session if compositor restrictions interfere.

From the repository root:

```sh
./install.sh
~/.local/bin/murmur
```

The installer builds the Python engine in place at `~/.local/share/murmur/engine`
(a venv from `engine/requirements-cuda.txt`, including the CUDA wheels so GPU inference
works on NVIDIA hardware), runs `npm ci`, then `npx tauri build` (which also builds
the frontend). Linux does **not** ship a frozen interpreter: `murmur-engine` is a small
launcher that execs the venv with the CUDA libraries on `LD_LIBRARY_PATH`, which keeps
the install ~30 MB instead of ~1.6 GB. Allow a few GB of build space for the toolchain.
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
engine/.venv/bin/python -m pip install -r engine/requirements-cuda.txt pyinstaller
npm ci
npm run tauri dev                # desktop app; source Python engine from the venv
# npm run dev                   # browser UI only: no Rust IPC
```

For a release build without installing into your home directory:

```sh
scripts/build-linux.sh          # sidecar launcher + app + deb; AppImage best-effort
# Keep the engine launcher beside the raw release binary for manual execution:
cp .cache/murmur-engine src-tauri/target/release/murmur-engine
src-tauri/target/release/murmur
```

The default build directory is `src-tauri/target`; manual build-linux.sh honors
Cargo target overrides. The installer and CI deliberately pin that directory.
An extracted portable Linux archive includes the executable pair. A raw CI
artifact download can lose executable bits: run `chmod +x murmur murmur-engine`
or use the `.tar.gz`, which preserves permissions. Linux bundles need compatible
system webview/audio libraries; portable does not mean a fully static application.

## Windows build

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

## Enhance (optional)

Release builds keep Rust codegen on the original x86-64/SSE2 baseline and enable AVX, AVX2, FMA, F16C, BMI2 and SSE4.2 for the pinned llama.cpp backend, with native tuning, AVX-512 and all-variant builds disabled. CI audits the generated CMake compiler flags and fails on unexpected flags. Local rewriting requires AVX2 + FMA + F16C (for example, Intel Haswell from 2013 or newer); CPUs without those features report Enhance as unavailable instead of entering the native backend and crashing. A minimum-CPU hardware run is still pending. The optional real-model evaluation can be run locally with `MURMUR_ENHANCE_MODEL=/path/to/model.gguf cargo test --manifest-path src-tauri/Cargo.toml real_model_rewrite_quality -- --ignored --nocapture`; it requires the installed model and makes no download request.

Settings → Behavior offers local CPU rewriting after the original dictation is delivered. It is off by default. Only an explicit Install action downloads the pinned Qwen3-1.7B Q4_K_M model (~1.1 GB) from Hugging Face; subsequently rewriting runs offline. The model is not bundled. A rewrite preview appears in the overlay; Accept copies the proposal to the clipboard, but does not paste or replace previously typed text. Discard leaves the original unchanged. Rewrite and Accept hotkeys start unassigned with suggested bindings in Settings. Code and terminal dictation is not rewritten by default; it requires the explicit opt-in. A rewrite that exceeds its deadline stops with "Enhance stopped: generation exceeded the configured time limit. Original dictation is unchanged." See [the design](docs/enhance-design.md) and [validation limits](docs/enhance-validation.md). Native Windows overlay interaction and real-model rewrite quality are not verified yet; the 1.7B model mainly fixes punctuation, casing and fillers rather than expanding a prompt.

## Hotkeys

| Shortcut / mode | Action |
| --- | --- |
| `Ctrl+Alt+D`, Hold (default) | Hold to record; release to transcribe and insert |
| Main shortcut, Toggle | Press to start; press again to stop; silence also auto-stops |
| Code shortcut (`general.hotkey_code`, unset by default) | Same hold/toggle behavior, forcing code style for this dictation |
| `Esc` while listening | Cancel without insertion or a cancellation cue |

Very short recordings are ignored. Rebind shortcuts in General; the main and
code shortcuts must differ. Escape is reserved for cancellation, not dictation.

## Coding mode / vibecoding workflow

1. In Engine, explicitly install **large-v3-turbo**, then choose **Use model** for
   technical prompts. Use `distil-small.en` for a smaller English-only option.
2. In General, choose toggle mode; Audio defaults allow up to 90 seconds. Increase
   the silence timeout if you pause often while composing prompts.
3. Keep Behavior → Dictation style **Auto** and **Prefer paste** enabled. Code-app
   matching selects code style for editors/terminals; edit the list for your apps.
4. Keep Live preview enabled to catch misheard identifiers; press Esc to cancel
   before the result is inserted. Preview is not a code validator.
5. Add project-specific tokens to Engine → Dictionary or deterministic corrections
   to Replacements. Optionally bind the Code shortcut for other focused apps.

Say “foo underscore bar”, “double equals”, “fat arrow”, or “quote hello unquote”.
Code style avoids sentence capitalisation and preserves code-like/mixed-case
identifiers. Filler removal is opt-in. “Newline” becomes a space unless explicitly
enabled; detected terminals always flatten newlines to avoid executing a line.
Check the destination before dictating commands; app detection is not a security boundary.

Prefer paste uses the clipboard for code style or text longer than 200 characters,
even when Method is Type. Disable it to use the legacy method selection (Auto
uses typing for ASCII up to 1500 bytes, otherwise paste). Linux terminals use
Ctrl+Shift+V; other targets use Ctrl+V. Previous clipboard text is restored after
300 ms unless it changed. Non-text clipboard data cannot be restored; when there
was no text snapshot, the transcript remains available. Clipboard-only/excluded
apps receive no keystrokes, and Murmur's own windows are skipped.

## Configuration reference

Linux: `${XDG_CONFIG_HOME:-~/.config}/murmur/config.toml`, data/models/history/logs
under `${XDG_DATA_HOME:-~/.local/share}/murmur`. Windows:
`%APPDATA%\Murmur\config.toml` and `%LOCALAPPDATA%\Murmur` for data.
Use Settings or edit TOML; writes are atomic, unknown keys are preserved, and
invalid edits report errors while retaining the last valid configuration.
All supported keys are listed below. Defaults are application defaults; a fresh
Linux user install explicitly enables autostart without changing the app default.

| TOML key | Default | Meaning / accepted values |
| --- | --- | --- |
| `general.hotkey` | `"Ctrl+Alt+D"` | Global dictation accelerator; not modifier-only |
| `general.hotkey_mode` | `"hold"` | `hold` or `toggle` |
| `general.hotkey_code` | `""` | Optional distinct accelerator forcing code style |
| `general.language` | `"en"` | Language code; empty string enables autodetection |
| `general.translate` | `false` | Translate speech to English; use a multilingual model |
| `general.autostart` | `false` | Start on login; true in a fresh Linux installer-created config |
| `general.theme` | `"auto"` | `auto`, `light`, `dark`; shared by settings and overlay |
| `overlay.enabled` | `true` | Show overlay |
| `overlay.position` | `"bottom-center"` | `bottom-center` or `top-center` |
| `overlay.scale` | `1.0` | Scale, 0.8–1.6 |
| `overlay.show_preview` | `true` | Live recognition preview |
| `overlay.idle_hide` | `true` | Hide after returning to idle |
| `sounds.enabled` | `true` | Enable synthesized start/stop/error cues |
| `sounds.volume` | `0.6` | Cue volume, 0–1 |
| `sounds.muted` | `false` | Mute without changing volume |
| `engine.model` | `"distil-small.en"` | Catalog model name; empty means none; selection is not installation |
| `engine.device` | `"auto"` | `auto`, `cuda`, `cpu`; auto allows sticky CPU fallback |
| `engine.compute_type` | `"auto"` | `auto`, `int8`, `int8_float16`, `float16`, `float32`, `int8_float32`; auto resolves per device |
| `engine.beam_size` | `1` | Decoder beam size, 1–10 |
| `engine.vad_filter` | `true` | Filter nonspeech during inference |
| `engine.dictionary` | Seeded coding vocabulary below | String array used as decoder prompt bias, not guaranteed corrections |
| `audio.device` | `"default"` | System default or enumerated input-device name |
| `audio.gain` | `1.0` | Input multiplier, 0–10 |
| `audio.silence_threshold` | `0.012` | RMS silence threshold, 0–1 |
| `audio.silence_timeout_ms` | `1200` | Toggle-mode silence endpoint, 100–60000 ms |
| `audio.max_utterance_s` | `90` | Maximum recording length, 1–90 seconds |
| `audio.min_utterance_ms` | `250` | Ignore shorter recordings; positive and no greater than maximum duration |
| `injection.method` | `"auto"` | `auto`, `type`, `clipboard`; prefer_paste can override |
| `injection.clipboard_only` | `false` | Copy transcript without sending paste/type keystrokes |
| `injection.trailing_space` | `true` | Append one space |
| `injection.capitalize` | `true` | Capitalise prose sentence starts, not code |
| `injection.remove_fillers` | `false` | Remove words listed in postproc.fillers |
| `injection.exclude_apps` | `[]` | Focused-app matches receive clipboard-only output |
| `injection.prefer_paste` | `true` | Prefer atomic paste for code style or long text |
| `postproc.replacements` | `{ "mur mur" = "Murmur" }` | Case-insensitive whole-word corrections, longest phrase first |
| `postproc.fillers` | `["um", "uh", "er", "hmm"]` | Filler list; no empty entries |
| `postproc.style` | `"auto"` | `prose`, `code`, `auto` (resolve by focused app) |
| `postproc.code_apps` | Editor/terminal list below | App-name/class matches that select code style |
| `postproc.spoken_punctuation` | `true` | Convert punctuation phrases to symbols |
| `postproc.allow_newlines` | `false` | Allow spoken newline outside detected terminals |
| `privacy.history_enabled` | `false` | Opt-in local SQLite transcript history |
| `privacy.history_retention_days` | `7` | Retention, 1–3650 days |

Default dictionary: `Tauri`, `faster-whisper`, `CTranslate2`, `WebKitGTK`, `WebView2`,
`serde`, `tokio`, `enigo`, `cpal`, `rodio`, `Murmur`, `zethrus`, `Python`, `TypeScript`,
`npm`, `cargo`, `git`, `stdin`, `stdout`, `JSON-RPC`, `hotkey`, `overlay`, `systemd`,
`Wayland`, `X11`, `pip`, `venv`, `regex`, `null`, `boolean`.

Default code apps: `code`, `cursor`, `vscode`, `code-oss`, `jetbrains`, `zed`,
`sublime`, `gnome-terminal`, `konsole`, `alacritty`, `kitty`, `wezterm`,
`windowsterminal`, `mintty`. Linux matches X11 window classes; Windows uses
foreground executable names. Explicit lists, including empty ones, are preserved.

## Privacy

Speech recognition runs locally: no account, telemetry, cloud transcription or
saved microphone audio. The engine disables Hugging Face telemetry and is offline
except for an explicit model installation, which downloads weights from Hugging
Face without uploading speech/transcripts. Dependency installation/builds also
require network access; “local” describes dictation, not software distribution.

History is off by default. Enabling it stores local transcripts in `history.sqlite`;
retention pruning occurs at startup and daily. Disabling it stops new entries but
does not delete old ones; use Privacy → Clear history or uninstall with `--purge`.
Diagnostic logs rotate at 2 MiB with two backups and are not intended to contain
transcripts. Treat debug logs as sensitive when sharing. Clipboard managers,
destination applications and filesystem backups may independently retain text.

## Performance

Measured on Linux with an **11.0 s clip** (78 characters of transcript), model **distil-small.en**:

| Platform / device | Compute type | Inference | Key-to-text | VRAM |
| --- | --- | --- | --- | --- |
| Linux / GPU RTX 4060 | int8_float16 | 103-124 ms | 1.5 s | 359 MiB |
| Linux / CPU | int8 | 1505 ms | 2.9 s | — |
| Windows / GPU RTX 4060 | int8_float16 | Pending measurement | Pending measurement | Pending measurement |
| Windows / CPU | int8 | Pending measurement | Pending measurement | — |

These are measured results for that clip, not general latency guarantees or
Windows measurements. Key-to-text is post-recording turnaround, not the time
spent speaking. Model loading, model size, hardware and injection method affect
latency; the first inference can be slower while the engine loads the model.

On Linux the transcript is typed with real XTEST keystrokes (~6 ms per
character), which dominates key-to-text: 78 characters take roughly 0.5 s to
type, plus a short wait for the hotkey chord to be released. Speech recognition
itself is ~0.1 s on the GPU. A clipboard-paste path exists for long transcripts
but is not the default because synthetic paste chords are unreliable on X11.

Memory (measured, idle with `distil-small.en` loaded): app process **203 MB**
(settings + overlay webviews), engine **779 MB** with the CUDA context resident
(CTranslate2 + cuDNN + model); CPU-only runs omit the CUDA context and are
noticeably smaller. Memory scales with model size, not utterance length.

## Troubleshooting / QA

- **No model:** open Settings → Engine, Install a catalog model, then Use model.
  A model name in config is not a download; incomplete downloads are not installed.
- **Hotkey conflict:** choose another non-modifier combination in General; avoid
  OS/window-manager bindings. Escape is reserved. Check both hotkeys are distinct.
- **No microphone:** select/refresh an available input in Audio; check OS microphone
  permissions, PipeWire/ALSA or Windows input settings, mute and gain. Unplugged
  device errors rebuild the capture stream on the next recording attempt.
- **Sidecar crash:** keep `murmur-engine[.exe]` next to the release app. A bare app
  copied alone is insufficient. The supervisor restarts once, then requires an app
  restart after another failure. Check `<data>/murmur.log`; for source runs verify
  the venv imports. For extra diagnostics, quit and launch with `MURMUR_LOG=debug`.
  Frozen onefile builds need writable temporary storage and permission to execute
  extracted libraries; restrictive/noexec temp mounts can prevent startup.
- **X11 typing drops or mangles the first word:** the transcript is typed by a
  long-lived helper process (`XDG_RUNTIME_DIR/murmur-inject.*`) that waits for the
  hotkey chord to be physically released before typing. If words are lost, check
  that no synthetic modifier release is being sent (fake Ctrl/Alt key-ups
  desynchronise the global hotkey grab, after which chords are swallowed silently),
  that the typing delay stays at 6 ms or more per character (GTK drops characters
  below that), and that only one Murmur instance owns the hotkey. The helper exits
  with the app; a stale `murmur-inject.pid` can be deleted after quitting.
- **CUDA fallback:** use Device Auto / Compute Auto for CUDA `int8_float16` or CPU
  `int8`. Auto sticks to CPU for that engine process if CUDA loading/inference
  fails; restart after fixing compatible drivers/runtime libraries. Forced CUDA
  reports errors rather than silently falling back. NVIDIA runtime wheels are
  included when available, but do not replace the host driver. Check the resolved
  device in Settings after inference; Windows CUDA remains unverified.
- **Injection fails:** check X11 `DISPLAY` and `xprop`, destination focus and Windows
  privilege levels. Try Clipboard only or disable Prefer paste for explicit Type.
- **Build fails:** check native dependencies and free disk space. Re-run only after
  resolving the reported stage; local install requires both Linux bundles to succeed.

Optional synthetic audio test (not a benchmark of speech accuracy):

```sh
mkdir -p .cache/qa
espeak-ng -w .cache/qa/speech.wav 'Murmur local speech test'
aplay .cache/qa/speech.wav       # or paplay
MURMUR_QA_WAV="$PWD/.cache/qa/speech.wav" npm run tauri dev
```

QA WAV accepts PCM/float WAV, downmixes/resamples, bypasses the microphone, rewinds
at each dictation and stops at EOF. Test Engine uses a silent three-second probe;
VAD may return empty text. Do not confuse that probe with the measured clip above.

## CI, releases and design parity

`linux-build.yml` runs on a self-hosted Linux runner (labels from the
`LINUX_RUNNER` repository variable, hosted `ubuntu-22.04` when unset), Node 22,
Rust stable with an isolated `RUSTUP_HOME`/`CARGO_HOME`, and a frozen engine; it
produces the deb, AppImage, unsigned app/sidecar pair and a portable tarball.
`windows-build.yml` expects a Windows runner via `WINDOWS_RUNNER` (hosted
`windows-latest` when unset) and produces unsigned EXEs, the NSIS installer and
a portable zip; tag builds skip it when no Windows runner exists.
`release.yml` is the only entry point for tags `v*`: each build job attaches its
own assets to the release, then a final job verifies the asset set and attaches
`SHA256SUMS`. Release publishing deliberately does not depend on Actions
artifact storage, because that quota is small on this account.
Fork pull requests never execute on the self-hosted runner. No signing or
notarization is configured. The Linux release pipeline has been verified end to
end (`v0.1.0`); Windows artefacts remain unbuilt until a Windows runner is
registered, so desktop Windows compatibility is still unverified. See
[docs/parity](docs/parity/README.md) for the manual Linux/Windows acceptance
matrix and screenshot protocol.
