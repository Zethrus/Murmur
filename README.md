# Murmur — phase 1

Local, private dictation for Linux/X11 and Windows 10/11 x64. This implementation targets
the committed SPEC.md contract at 4de9f8a; later spec additions are not implied complete.
This phase includes the Rust backend, isolated Python engine, live overlay, typed IPC,
full design tokens, and build plumbing. Settings intentionally contains only status JSON,
a missing-model banner and a theme selector. The five settings pages, CI/release workflows,
and Windows packaging script are later-phase work, not placeholders claimed as complete.

## Development

Prerequisites: Node 22+, npm, Rust/Cargo 1.92+, Python 3.10+ with venv; on Linux, the
WebKitGTK 4.1, GTK3, ALSA, libappindicator and XKB development libraries, plus xprop.
No sudo or apt is used by the project scripts. Enigo uses its x11rb/XTEST backend,
not libxdo. Native Wayland capture/injection is not supported; failures are reported,
not panics. Windows uses WebView2 and Enigo's Unicode SendInput implementation.

From the repository root:

```sh
mkdir -p .cache/tmp .cache/pip
export TMPDIR="$PWD/.cache/tmp" PIP_CACHE_DIR="$PWD/.cache/pip"
/usr/bin/python3 -m venv --system-site-packages engine/.venv
engine/.venv/bin/python -m pip install -r engine/requirements.txt pytest pyinstaller
npm ci
npm run dev                 # web-only development, no Rust IPC
npm run tauri dev           # actual desktop app + local Python sidecar
```

The desktop app starts hidden in the tray; click its icon for Settings. The overlay
route is `?window=overlay`. Closing Settings hides it; Quit stops and joins workers.
The dev launcher executes `engine/.venv/bin/python -m murmur_engine`; on Windows,
create `engine/.venv` with Python and the backend uses `.venv/Scripts/python.exe`.
No model is downloaded or loaded on startup. The default model name is
`distil-small.en`, but a missing installation is explicitly reported. Until the Engine
settings page exists, model management is available through the typed bridge commands
`list_models`, `install_model({name})` and `set_config({patch: {engine: {model: name}}})`.
Installation is always explicit, never triggered by a hotkey or transcription request.

## Build and checks

```sh
npm run build               # TypeScript + token lint + Vite
npm run test:tokens         # positive and negative token-linter fixtures
(cd src-tauri && cargo build)
(cd src-tauri && cargo test)
(cd engine && .venv/bin/python -m pytest -q)
scripts/engine-sidecar.sh --build  # PyInstaller onefile engine, no model downloads
scripts/build-linux.sh      # frozen sidecar, app binary, .deb; optional AppImage
```

Linux packaging uses the project-pinned `npx tauri` CLI rather than assuming a globally
installed `cargo tauri`. `src-tauri/tauri.conf.json` declares the external sidecar and
Linux bundle targets. `tauri.windows.conf.json` automatically overrides targets to NSIS;
Windows builds need a frozen `src-tauri/binaries/murmur-engine-x86_64-pc-windows-msvc.exe`
made from `engine/murmur_engine/__main__.py` with the engine package on PyInstaller's path.
Windows packaging and runtime remain unverified until run on Windows.

Plain debug cargo builds generate a gitignored development externalBin launcher so
Tauri's resource validation succeeds without freezing Python. Release packaging must
use the actual frozen engine. Generated icons are committed; regenerate with
`npx tauri icon assets/murmur.png`. Audio cues are synthesized at runtime: rising/falling
660/880 Hz tones with 8 ms envelopes, and a two-buzz 180 Hz error cue.

## Configuration and operation

Linux paths: `$XDG_CONFIG_HOME/murmur/config.toml` (default `~/.config/murmur`) and
`$XDG_DATA_HOME/murmur` (default `~/.local/share/murmur`). Windows paths:
`%APPDATA%\Murmur\config.toml` and `%LOCALAPPDATA%\Murmur`.
The data directory contains models, optional history, and `murmur.log` plus two rotations
(maximum 2 MiB each). The backend owns atomic TOML writes and preserves unknown keys.
External edits are watched; invalid edits report an error and retain the last valid config.

Default hotkey: Ctrl+Alt+D, hold to record and release to transcribe. Toggle mode starts
and stops on successive presses and stops after sustained silence following speech.
Escape cancels only while recording, with no cancellation sound. Very short taps are
ignored. Rebinding reports invalid/conflicting accelerators; the 100 ms target needs
measurement on each desktop. Capture includes 250 ms pre-roll, a 60 s in-memory ring,
16 kHz mono resampling and a configurable maximum utterance. No audio files are saved.

ASCII text up to 1500 characters uses typing in auto mode; other text uses clipboard
paste. Previous clipboard text is restored after 300 ms unless the user changed it.
Non-text clipboard data cannot be restored by this text-only snapshot; when no previous
text exists, the transcript remains on the clipboard. Clipboard-only/excluded apps never
receive keystrokes, and Murmur's own windows are skipped. Clipboard ownership stays alive
on X11 for later pasting. The exclusion picker hides Settings for three seconds so the
user can focus the desired application.

## Privacy

Speech-to-text runs locally. No accounts, telemetry, cloud transcription or audio storage.
The engine runs with HF_HUB_OFFLINE=1 except within an explicitly requested model install.
Hugging Face telemetry is disabled. A model install contacts Hugging Face to download the
selected weights and emits file-level progress; no audio/transcripts are sent there.
History is off by default. Opt-in SQLite history is pruned on startup and daily; disabling
history stops new entries but does not erase existing entries. `history_clear` deletes and
vacuums the database; filesystem snapshots/backups are outside the application's control.
Transcripts are never written to diagnostic logs. Clipboard managers and the destination
application may retain pasted text independently of Murmur.

## Troubleshooting / QA

- No model: explicitly install a catalog model; incomplete downloads are never treated as
  installed. No model/network operation is performed by the test suite.
- Hotkey conflict: choose another non-modifier accelerator. Escape is reserved for cancel.
- Microphone failure: select an available input; unplug failures rebuild on the next press.
- Engine crash: one automatic restart is permitted; a second failure requires restarting
  Murmur. Check the venv/dependencies or packaged engine and the local diagnostic log.
- Clipboard/paste failure: try explicit typing or clipboard-only mode. X11 injection needs
  DISPLAY and xprop; native Wayland is unverified.
- Python logs go to stderr; stdout is exclusively newline-delimited JSON RPC.

Synthetic audio recipe (optional, not executed by the build):

```sh
mkdir -p .cache/qa
espeak-ng -w .cache/qa/speech.wav 'Murmur local speech test'
aplay .cache/qa/speech.wav    # or paplay
MURMUR_QA_WAV="$PWD/.cache/qa/speech.wav" npm run tauri dev
```

QA WAV input accepts PCM or float WAV, downmixes/resamples, feeds at real-time cadence,
and rewinds at each dictation. It bypasses microphone hardware and stops at EOF.
The test-engine IPC performs a repeatable three-second silent inference probe (VAD may
return empty text). Real transcription/injection performance is not measured in phase 1.

Manual parity (not executed in phase 1): open Settings and the idle/listening overlay on
Linux and Windows with the same theme and scaling. Compare spacing, type sizes and colour
roles; only font-family resolution should differ. Save screenshots in `docs/parity/`.
Do not infer live hotkey, tray, audio, CUDA, injection, packaging or Windows success from
unit tests. No GUI was launched and no Whisper model was downloaded during implementation.

## Implementation notes

Extra crates are narrowly scoped: rusqlite/bundled is mandated for history; base64 encodes
PCM for JSON RPC; regex implements Unicode whole-word replacements; hound reads QA WAV;
windows-sys provides atomic replacement and foreground-process lookup. Single-instance is
a Rust lifecycle plugin and exposes no IPC permission identifier; capabilities list only
real window/event, global-shortcut and autostart permissions, with no shell/filesystem grant.
Lockfiles are committed in compact, valid machine-readable form to stay below the requested
400-line file limit. Cargo may canonicalize its generated lockfile on a subsequent build;
compaction preserves the exact parsed dependency graph. Source modules stay below the limit.
