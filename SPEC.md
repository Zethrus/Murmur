# Murmur — local, private, low-latency dictation for Linux/X11

Implementation spec. Read this entire file before writing code. Build exactly this; do not
add features beyond it. Prefer small, single-purpose modules over large files.

## Target environment (verified, do not re-probe destructively)

- Ubuntu 24.04, **X11** (DISPLAY=:1), GNOME 46 (Ubuntu session, AppIndicator support present).
- Ryzen 7 7735HS (16 threads), 30 GiB RAM, **RTX 4060 8 GB**, driver 580 (CUDA 12.x).
- Python 3.12.3 (`/usr/bin/python3`). Available already: `gi` (GTK3, GTK4, libadwaita 1,
  AyatanaAppIndicator3 0.1, Notify 0.7), `cairo`, `numpy`, `xdotool`, `xclip`, `xsel`,
  `parec`, `paplay`, `ffmpeg`, `notify-send`.
- **Not available:** portaudio19-dev, sounddevice, PyAudio, torch, webrtcvad, Handy typelib.
- Do **not** use apt/sudo. Everything must come from a project venv + system `gi` (use
  `--system-site-packages` so `gi`/`cairo`/`numpy` import).
- Audio is PipeWire; capture with the `parec` subprocess (never PyAudio/sounddevice).

## Product

Hotkey dictation: hold (or toggle) a hotkey, speak, and the transcript is typed into whatever
window has focus. Fully local (faster-whisper / CTranslate2). Warm model, no per-dictation
load. Tiny always-on-top overlay shows state; a GTK3 tray icon gives quick controls; a
GTK4/libadwaita settings app covers configuration including model download/selection.

Non-goals: cloud STT, accounts, telemetry, audio recording storage, Wayland support.

## Process architecture

Two GUI processes sharing one GUI-free core package:

1. `murmur-daemon` — GTK3 + AyatanaAppIndicator3 + overlay + hotkey + audio + STT + injection.
   Started at login from `~/.config/autostart/murmur.desktop`. Idle RAM target ≤ 250 MB
   (engine excluded), steady CPU on idle ≈ 0 % (continuous 16 kHz capture is acceptable,
   ~0.5 % of one core).
2. `murmur-settings` — GTK4 + libadwaita, launched on demand from the tray ("Settings…"),
   exits when its window closes. Communicates with the daemon over a Unix socket; writes
   config, daemon hot-reloads.

Core package `murmur/` must not import `gi` at module import time except in GUI modules.

## Repository layout (create exactly this)

```
/home/zethrus/Projects/murmur/
├── SPEC.md                     # this file
├── README.md                   # quick start, hotkeys, privacy statement, troubleshooting
├── pyproject.toml              # metadata; no packaging of GUI needed
├── requirements.txt            # faster-whisper, ctranslate2, pynput, tomli-w, nvidia-* (cu12)
├── install.sh                  # venv, deps, model dir, autostart entry, smoke test
├── uninstall.sh                # remove autostart + venv + optionally config/models
├── bin/murmur-daemon           # exec shim: sets LD_LIBRARY_PATH for CUDA wheels, runs python -m murmur.daemon
├── bin/murmur-settings         # exec shim
├── murmur/__init__.py
├── murmur/config.py            # dataclasses + TOML load/save/diff, defaults, migration, mtime watcher
├── murmur/state.py             # state machine enum + transition guards + event fanout
├── murmur/paths.py             # XDG dirs, socket path, model dir, lock file
├── murmur/ipc.py               # newline-JSON unix-socket server + client (async-free, thread based)
├── murmur/audio.py             # parec capture process, ring buffer, RMS/VU, utterance assembly
├── murmur/engine.py            # faster-whisper model cache, device/compute resolution, transcribe(), partials()
├── murmur/models.py            # model catalog, install sizes, snapshot_download with progress, delete
├── murmur/postproc.py          # punctuation/capitalisation, fillers, replacements, dictionary prompt
├── murmur/inject.py            # xdotool type fast path, clipboard-paste path, clipboard restore, exclusions
├── murmur/hotkey.py            # pynput listener, hold/toggle/tap semantics, combo parser, capture helper
├── murmur/sounds.py            # generates cue WAVs (numpy) and plays them with paplay, volume/mute
├── murmur/history.py           # sqlite history (opt-in), retention pruning, search
├── murmur/controller.py        # orchestration: hotkey → capture → transcribe → postproc → inject; no GUI
├── murmur/daemon/__init__.py
├── murmur/daemon/__main__.py   # Gtk.Application wiring + single-instance lock + tray + IPC server thread
├── murmur/daemon/overlay.py    # click-through GTK3 overlay pill (idle/listening/transcribing/done/error)
├── murmur/daemon/tray.py       # Ayatana indicator: status, mute sounds, Settings…, Quit
├── murmur/settings_app/__init__.py
├── murmur/settings_app/__main__.py
├── murmur/settings_app/app.py           # Adw.Application + window + NavigationSplitView
├── murmur/settings_app/pages/general.py
├── murmur/settings_app/pages/engine.py
├── murmur/settings_app/pages/audio.py
├── murmur/settings_app/pages/behavior.py
├── murmur/settings_app/pages/privacy.py
├── murmur/settings_app/widgets/hotkey_capture.py
├── murmur/settings_app/widgets/level_meter.py
├── assets/murmur.svg           # simple mic glyph, theme-agnostic, 128x128
├── assets/theme.css            # overlay + settings accents
└── tests/                      # pytest, headless, no GUI, no model download
```

## Config (TOML, `~/.config/murmur/config.toml`)

Read with `tomllib`, write with `tomli_w` (add to requirements). Create with defaults on first
run. Daemon polls mtime every 1 s and reloads on change; reload must be safe mid-session
(never lose a completed transcript). Unknown keys are preserved on save.

```toml
[general]
hotkey = "ctrl+alt+d"        # parsed by hotkey.parse_combo; lowercase names
hotkey_mode = "hold"         # hold | toggle
language = "en"              # whisper language code, "" = autodetect
translate = false

[overlay]
enabled = true
position = "bottom-center"   # bottom-center | top-center
scale = 1.0                  # 0.8 – 1.6
show_preview = true          # live partial text while speaking

[sounds]
enabled = true
volume = 0.6                 # 0.0 – 1.0
muted = false

[engine]
model = "distil-small.en"    # "" = no model installed yet
device = "auto"              # auto | cuda | cpu
compute_type = "auto"        # auto → int8_float16 on cuda, int8 on cpu
beam_size = 1
vad_filter = true
dictionary = []              # list[str] fed as initial_prompt

[audio]
source = "default"           # pactl source name, or "default"
gain = 1.0
silence_threshold = 0.012    # RMS (0..1 float samples)
silence_timeout_ms = 1200    # toggle mode auto-stop
max_utterance_s = 45
min_utterance_ms = 250

[injection]
method = "auto"              # auto | type | clipboard
clipboard_only = false
trailing_space = true
capitalize = true
remove_fillers = false
exclude_apps = []            # lower-case window class substrings; when focused → clipboard only

[postproc]
replacements = { "mur mur" = "Murmur" }

[privacy]
history_enabled = false
history_retention_days = 7
```

## Runtime behaviour (the contract)

State machine: `idle → listening → transcribing → done → idle`, `listening → idle` (cancel),
`transcribing → error → idle`. Transitions emit events (`state`, `level`, `partial`,
`latency`, `error`) to the IPC server for the overlay/settings UI.

1. **Capture** — one long-lived `parec --format=s16le --rate=16000 --channels=1
   --latency-msec=20 [--device=NAME]` process writing to a reader thread that appends to a
   deque ring buffer (~60 s). RMS per 20 ms frame drives the `level` events (throttled to
   ~20 Hz) and the silence detector. On hotkey press: mark start index (keep 250 ms of
   pre-roll so the first syllable is never clipped). On stop: assemble PCM from start index
   to now, apply `gain`, normalise to float32.
2. **Hotkey** — `pynput.keyboard.Listener`. `hold`: key press starts, release stops.
   `toggle`: first press starts, next press stops; auto-stop after `silence_timeout_ms` of
   sub-threshold audio following at least 300 ms of speech. `Esc` while listening cancels and
   discards, with the error cue suppressed. Ignore auto-repeat; require all combo modifiers
   to be held at press time; ignore presses while the combo's non-modifier key is already
   down. Hotkey must be changeable at runtime (listener restart within ~100 ms).
3. **Transcribe** — `engine.transcribe(np.float32, ...)` on a worker thread. Model cached per
   (name, device, compute_type); loaded lazily at startup in a background thread so the
   daemon never blocks on it. `device="auto"` → try CUDA once, on failure log and fall back to
   CPU permanently for the session. Streaming partials (when `overlay.show_preview`): every
   ~700 ms re-transcribe the buffer grown since the last partial, `condition_on_previous_text=
   False`, and emit `partial`. Never block the final pass on a partial.
4. **Post-process** (`postproc.apply`) in this order: strip surrounding whitespace, collapse
   inner whitespace/newlines to single spaces, optional filler removal, replacement map
   (case-insensitive whole-word/phrase, longest-first), capitalise first letter of each
   sentence (when `capitalize`), ensure single trailing space (when `trailing_space`).
   Do not "fix" technical tokens, casing of mixed-case words, or numbers.
5. **Inject** — `inject.type_text(text, cfg)`:
   - Skip when the focused window class matches `exclude_apps` or `clipboard_only` → copy to
     clipboard only, play the done cue, show overlay "copied".
   - `auto`: ASCII-only text ≤ 1500 chars → `xdotool type --clearmodifiers --delay 0` (single
     argv element, `--` separator). Otherwise (unicode, emoji, long) → clipboard path: save
     CLIPBOARD via `xclip -o -selection clipboard`, set new text, `xdotool key --clearmodifiers
     ctrl+v`, wait 300 ms, restore the previous clipboard, release ownership afterwards.
   - `type` forces xdotool (lossy for non-ASCII: warn once in the log), `clipboard` forces the
     paste path.
   - Never inject into Murmur's own windows (check focused window class != murmur/settings).
6. **Sounds** (`sounds.py`) — three short cues synthesised with numpy at install/first-run into
   `~/.local/share/murmur/cues/` (start: rising two-tone ~90 ms; stop: falling two-tone ~90 ms;
   error: low double buzz), scaled by `volume`, played with `paplay` (fire-and-forget, never
   blocking the pipeline), skipped when `muted`/`enabled=false`. Regenerate on volume change.
   Also mirror state to a `notify-send`-free tray tooltip (no desktop notifications by default;
   the overlay is the indicator).
7. **History** — only when `history_enabled`. SQLite at
   `~/.local/share/murmur/history.db`, table `entries(id, ts, text, app_class, duration_ms,
   latency_ms, model)`. Prune on startup and daily when older than retention. Settings offers
   search + delete-all.
8. **Single instance** — lock file at `$XDG_RUNTIME_DIR/murmur.lock` with PID; a second daemon
   exits 0 after activating the existing overlay (no duplicate hotkey listeners).

## IPC (Unix socket `$XDG_RUNTIME_DIR/murmur.sock`, mode 0600)

Newline-delimited JSON. Client sends `{"id":n,"cmd":...}`; daemon replies
`{"id":n,"ok":true,"data":{...}}` or `{"id":n,"ok":false,"error":"..."}`. Commands:
`status`, `config.get`, `config.set` (`{"patch":{...nested...}}`), `reload`, `engine.install`
(`{"model":"large-v3-turbo"}` → streams `{"event":"download","model":...,"pct":...,"done":false}`),
`engine.remove`, `engine.test`, `devices.list`, `audio.meter.start`/`stop`,
`sounds.preview`, `history.search`, `history.clear`, `dictate.start`/`dictate.stop`,
`quit`. Events (unsolicited): `state`, `level`, `partial`, `latency`, `error`, `download`,
`config.changed`.

## Overlay (daemon/overlay.py)

- Frameless, RGBA, always-on-top, skip taskbar/pager, `set_accept_focus(False)`, and **true
  click-through** via `input_shape_combine_region(cairo.Region())` after realise; no shadow.
- Pill ~180×44 px (× `scale`), rounded 22 px, `position` bottom-center (default 48 px from
  bottom) or top-center, 24 px margin from screen edges; multi-monitor: primary monitor only.
- Content by state: idle → small muted dot + "mur"; listening → 5 live level bars (accent) +
  optional one-line partial preview that grows the pill up to 560 px wide (ellipsise, 2 lines
  max, 13 px text); transcribing → pulsing dot + "…"; done → checkmark, auto-hide after 900 ms;
  error → amber dot, auto-hide after 1.6 s. Idle hides completely after 1.5 s of inactivity
  when `overlay.idle_hide` is true (add this key, default true) — the tray icon is the always-
  visible indicator when the overlay is hidden.
- Repaint only on `level`/`partial`/state change (no timers above 20 Hz). Never animate when
  hidden.

## Tray (daemon/tray.py)

Ayatana indicator with icon from `assets/murmur.svg`, tooltip "Murmur — idle|listening|…",
menu: status label (insensitive), "Dictate (toggle)" (when mode=toggle), sounds mute checkbox,
overlay enabled checkbox, "Settings…" (spawns `bin/murmur-settings`), "Reload config",
"Quit Murmur".

## Settings app (GTK4 + libadwaita)

`Adw.Application`, window 860×640, `Adw.NavigationSplitView` sidebar pages + `Adw.HeaderBar`.
Modern, calm, small: heading + description per row, no dense grids, no raw JSON. Every control
applies immediately (write config → daemon reloads via socket `reload`); no Save button.
Pages and rows:

- **General** — hotkey capture widget (Gtk.EventControllerKey, shows combo chips; rejects
  modifier-only combos; shows conflict warning if the combo fails to bind), mode (hold/toggle),
  language dropdown (whisper languages, "Auto"), translate switch, overlay enabled, overlay
  position, overlay scale slider, live preview switch, sound volume slider + mute + "Preview"
  buttons, autostart switch (writes/removes `~/.config/autostart/murmur.desktop`).
- **Engine** — model list from `murmur/models.py` catalog (tiny.en, base.en, small.en,
  distil-small.en, distil-medium.en, medium.en, large-v3-turbo, large-v3) each with size,
  accuracy hint, Installed state, Install/Remove button, progress bar during download; device
  selector (Auto/CUDA/CPU) showing detected hardware line ("CUDA · RTX 4060 · int8_float16"),
  beam size spin, VAD switch, "Test 3 s of audio" latency readout, dictionary list editor.
- **Audio** — input device dropdown (from `pactl list short sources`, excluding `.monitor`),
  gain slider, live level meter (uses `level` events), silence threshold slider, silence
  timeout spin, max utterance spin.
- **Behavior** — injection method dropdown, clipboard-only switch, trailing space,
  capitalisation, filler removal, exclusions list (add/remove app class), replacements table
  (add/remove rows), "Show last transcript" read-only row.
- **Privacy** — explicit "Everything stays on this machine" block, history switch, retention
  days spin, search field + results list (last 50, click to copy), "Delete all history"
  destructive button with confirmation, paths list (config, models, history, log) with
  "Open folder".

Empty/first-run state: if `engine.model` is "" or not installed, the General page shows an
`Adw.Banner` "Choose a speech model to start dictating" with a button that jumps to Engine, and
the tray tooltip says "no model". The daemon must not crash when no model is available; it
sounds the error cue and emits an error state on hotkey press.

## Performance & quality targets (verify and report measured numbers)

- Hotkey → first audio sample: < 60 ms (ring buffer makes this near-zero).
- 5 s utterance, GPU large-v3-turbo int8_float16 → injected text: ≤ 600 ms wall clock.
- Idle daemon RSS ≤ 250 MB excluding the loaded model; no CPU spin (idle < 1 %).
- Zero network access at runtime: only `models.install` touches the network (huggingface_hub),
  and `HF_HUB_OFFLINE=1` is set in the daemon process unless a download is in flight.

## Code rules

- Type hints on public functions; dataclasses for config; no global mutable state outside
  `config.load/save`; one reason to change per module; ≤ 400 lines per file (split instead).
- Threads, not asyncio, except GTK's own main loop; every worker thread is daemonised and
  joins on shutdown; no busy-wait loops (use `threading.Event`).
- All subprocess calls use argv lists (no `shell=True`), with timeouts, and are killed on
  shutdown (`parec` especially).
- Logging via `logging` to `~/.local/share/murmur/murmur.log` (RotatingFileHandler, 2 MB × 3),
  level from `MURMUR_LOG` env (default INFO). Never log transcript text unless
  `MURMUR_LOG=DEBUG`.
- Fail soft: a broken engine, missing `xdotool`, or unplugged mic must surface as an error
  state + log line, never a daemon crash.
- No new system packages, no sudo, no systemd unit in v1 (autostart .desktop only).

## Definition of done

1. `./install.sh` completes from a clean checkout: venv with `--system-site-packages`,
   requirements installed, cue WAVs generated, autostart entry written, `pytest -q` green.
2. `pytest -q` covers: config round-trip + unknown-key preservation, hotkey combo parse/match
   (hold, toggle, autorepeat, modifier ordering), postproc (fillers, replacements, casing,
   trailing space, unicode untouched), inject path selection (ascii/unicode/length/exclusions)
   with subprocess mocked, silence endpointing maths, sounds volume scaling, history CRUD +
   retention pruning, IPC request/response over a temp socket, model catalog sanity.
3. `bin/murmur-daemon` starts on this X11 session, shows the tray icon, and `status` over the
   socket returns the live state; `bin/murmur-settings` opens and can change the hotkey with the
   daemon picking it up within 1 s.
4. A real end-to-end run with a small model: hotkey → speak → correct text injected into a
   focused text field, with start/stop cues audible. Document the exact repro commands in
   README (including how to synthesise a test utterance with `espeak-ng`/`ffmpeg` if present).
5. Measured numbers from target section recorded in README under "Performance".
6. `git` repository initialised with a sensible `.gitignore` (no venv, no models, no caches).
