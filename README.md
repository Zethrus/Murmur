# Murmur — phase 2

Local, private dictation for Linux/X11 and Windows 10/11 x64. Phase 2 adds the coding-mode
pipeline, five complete settings pages, and the event-driven overlay to the Rust backend,
isolated Python engine, typed IPC, shared light/dark tokens, and build plumbing.
CI/release workflows and the Windows packaging script remain later-phase work.
Desktop interactions and visual parity remain unverified: this phase was implemented and
tested without launching the GUI or downloading speech models.

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
`distil-small.en`, but a missing installation is explicitly reported. Open Engine in Settings
to install a model and choose **Use model**. Installation progress reports completed files,
not individual bytes; large weight files may spend a while at the same percentage.
Installation is always explicit, never triggered by a hotkey or transcription request.

## Build and checks

```sh
npm run build               # TypeScript + token lint + Vite
npm run test:tokens         # positive and negative token-linter fixtures
npm run test:ui             # DOM contract tests with mocked IPC; no GUI or network
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
measurement on each desktop. Capture includes 250 ms pre-roll, a 91 s in-memory ring,
16 kHz mono resampling and a configurable maximum utterance. The sidecar accepts 91 seconds
of PCM to include the 90-second utterance plus pre-roll/callback headroom. No audio files are saved.

With **Prefer paste** enabled (the default), code style or more than 200 characters uses
clipboard paste, even if the method is set to Type. Disable Prefer paste to use the legacy
method selection (Auto types ASCII up to 1500 bytes; other text uses paste). Linux terminal
windows use Ctrl+Shift+V; other windows use Ctrl+V. Terminal output always flattens newlines,
even when newlines are enabled. Previous clipboard text is restored after 300 ms unless the user changed it.
Non-text clipboard data cannot be restored by this text-only snapshot; when no previous
text exists, the transcript remains on the clipboard. Clipboard-only/excluded apps never
receive keystrokes, and Murmur's own windows are skipped. Clipboard ownership stays alive
on X11 for later pasting. The exclusion picker hides Settings for three seconds so the
user can focus the desired application.

## Vibecoding workflow

1. In Engine, install **large-v3-turbo** for technical prompts, then choose **Use model**.
   The catalog contains a CTranslate2 conversion; model weights are fetched only on Install.
2. In General, choose **Press to start / stop** (toggle mode). Long prompts can run for
   90 seconds; silence auto-stop still applies. Adjust the silence timeout in Audio if needed.
3. Keep Behavior → **Dictation style: Auto**. VS Code, Cursor, JetBrains, Zed, Sublime and
   the listed terminals use code style. Edit Code apps to match your window classes.
4. Keep the overlay's **Live preview** enabled to catch misheard identifiers before they land.
   Press **Escape** while listening to cancel. The preview is recognition text, not a code validator.
5. Optionally bind a **Code shortcut** to force code style for one dictation, regardless of
   the focused app. It uses the same hold/toggle semantics as the main shortcut.

Say “foo underscore bar”, “double equals”, “fat arrow”, or “quote hello unquote”. Spoken
punctuation is case-insensitive, whole-word and longest-match first. “Dot” joins identifiers
(`foo dot rs` → `foo.rs`); “period” / “full stop” ends a sentence. Code style never capitalises
or rewrites existing code-like/mixed-case tokens. Fillers are kept unless removal is enabled.
“Newline” / “next line” becomes a space by default. Even with Allow newlines enabled,
terminal windows always receive one paragraph to avoid executing a line accidentally.

The editable dictionary starts with coding vocabulary (Tauri, CTranslate2, TypeScript,
JSON-RPC, and others). Existing explicit dictionary lists, including an empty list, are
preserved when upgrading; defaults only fill missing fields.

## Settings guide

- **General:** both hotkeys, hold/toggle, language/translation, overlay, sounds, theme and autostart.
- **Engine:** offline model catalog, explicit install/remove/select, file-level progress,
  compute device/precision, beam/VAD, a measured silent 3-second test, and dictionary editor.
- **Audio:** input selection/refresh, idle live meter, gain, silence and utterance durations.
- **Behavior:** coding style/app matching, spoken punctuation/newlines, injection preferences,
  exclusions/picker, filler words, editable replacement rules, and the last session transcript.
- **Privacy:** local-only explanation, opt-in retention, debounced history search/delete-all,
  conventional OS paths and Open folder buttons (which resolve actual XDG/Windows paths).

The DOM tests exercise page rendering, saved controls, conflicts, empty/offline states,
model/history actions, event cleanup and overlay timing with explicit mock IPC. They do not
render pixels or replace desktop integration/visual parity checks.

Settings use only the typed bridge. All settings saves go through the Rust config transaction;
rejected hotkeys or invalid values show errors and retain the last saved value. Removing a
replacement requires saving the whole replacement table. History deletion and model removal
ask for confirmation. Themes are shared with the overlay; reduced motion is respected.

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
return empty text). Real transcription/injection performance has not been measured in this phase.

Manual parity (not executed in either implementation phase): open Settings and the idle/listening overlay on
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
