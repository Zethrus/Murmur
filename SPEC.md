# Murmur: local, private, low-latency dictation (Linux + Windows)

Historical implementation specification, v2. Some details describe earlier milestones.
For current behavior, use the [README](README.md), [configuration reference](docs/configuration.md),
and [companion protocol](companion/protocol.md). The November 5 release is Linux and
Flipper only; Windows and Android are deferred in [the roadmap](docs/roadmap.md).

## Enhance (optional, phase 1)

After immediate delivery of the original dictation, Enhance may suggest a local CPU rewrite in an interactive overlay. It defaults off; model download requires explicit action in Settings → Behavior. The built-in Qwen3-1.7B Q4_K_M model is pinned to one revision and checksum, stored separately from Whisper, and not bundled. Rewrite/accept shortcuts default unassigned. Code and terminal dictation is excluded unless opted in. Deterministic cleanup always runs while respecting Behavior switches; duplicate-word cleanup defaults on for prose. Accept copies to the clipboard and never replaces or pastes into the target app. Discard leaves text unchanged. Phase 2 guarded replacement, cloud inference, and persistent suggestion history are out of scope. See `docs/enhance-design.md` and `docs/enhance-validation.md` for implementation contracts and verification limits.

## Product

Press (or hold) a global hotkey, speak, and the transcript is typed into whatever window has
focus. Fully local speech-to-text. Warm engine, no per-dictation model load. Tiny always-on-top
overlay shows state; tray icon gives quick controls; a calm settings app covers configuration,
including model download and selection. Start/stop cues are audible.

Non-goals: cloud STT, accounts, telemetry, storing audio, Wayland-specific work.

## Stack (decided: do not substitute)

- **Shell/UI: Tauri v2 + vanilla TypeScript + Vite** (no React/Svelte; smallest, fastest).
  System webview: WebKitGTK 4.1 on Linux, WebView2 on Windows. One web UI, two windows:
  `settings` and `overlay`.
- **Rust backend** (`src-tauri`): hotkeys, audio capture, text injection, tray, overlay window
  control, config, sounds, sidecar supervision, IPC commands.
- **Python sidecar** (`engine/`): faster-whisper (CTranslate2) inference + model management,
  newline-delimited JSON-RPC over stdin/stdout. Isolated so a crash never takes the UI down.
- Verified local toolchain: rustc/cargo 1.92, node 22.23 + npm 10.9, webkit2gtk-4.1 (2.52),
  libsoup-3.0, alsa 1.2.11 dev, gtk3 dev, cc/g++/cmake/patchelf present. **No sudo, no apt.**
- Crates to use: `tauri` 2, `tauri-plugin-global-shortcut` 2, `tauri-plugin-autostart` 2,
  `tauri-plugin-single-instance` 2, `cpal` 0.15, `enigo` 0.2 (unicode typing; X11 + Windows
  SendInput), `arboard` (clipboard), `rodio` (cue playback), `serde`/`serde_json`, `toml` 0.8,
  `anyhow`, `tracing` + `tracing-subscriber`, `notify` (config file watcher). Keep the tree lean;
  justify any crate not listed here.

## Targets

| Concern | Linux | Windows |
|---|---|---|
| Hotkey | pynput-equivalent via tauri global-shortcut (works both) | same |
| Audio capture | `cpal` (ALSA → PipeWire) | `cpal` (WASAPI) |
| Typing | `enigo` (X11 XTEST) | `enigo` (SendInput, unicode) |
| Clipboard | `arboard` | `arboard` |
| Tray | Tauri tray-icon (libappindicator via GTK) | Tauri tray-icon |
| Autostart | `tauri-plugin-autostart` (.desktop) | same (HKCU Run) |
| Overlay interaction | Nonfocusable interactive window; click-through only during delivery | same, native focus behavior needs runtime verification |
| Cue audio | `rodio` | `rodio` (WASAPI) |
| Paths | `~/.config/murmur`, `~/.local/share/murmur` | `%APPDATA%\Murmur`, `%LOCALAPPDATA%\Murmur` |

Targets: Ubuntu/Debian (X11 first-class; Wayland may work but is untested and must not crash),
Windows 10/11 x64.

## Repository layout

```
the repository root (./)
├── SPEC.md  README.md  LICENSE  .gitignore
├── package.json  vite.config.ts  tsconfig.json  index.html
├── src/                        # web UI (vanilla TS)
│   ├── main.ts                 # routes: ?window=settings (default) | overlay
│   ├── bridge.ts               # typed wrapper over invoke()/listen()
│   ├── styles/                 # tokens.css, settings.css, overlay.css
│   ├── settings/               # app.ts, pages/{general,engine,audio,behavior,privacy}.ts,
│   │                           # widgets/{hotkey-capture,level-meter,row,list-editor}.ts
│   └── overlay/overlay.ts      # pill renderer (state machine + level bars + partial text)
├── src-tauri/
│   ├── Cargo.toml  build.rs  tauri.conf.json
│   ├── capabilities/default.json
│   ├── icons/ (generate via `cargo tauri icon` from assets/murmur.png)
│   ├── binaries/            # engine sidecar placed here at build time (gitignored)
│   └── src/
│       ├── main.rs          # tauri::Builder, plugins, tray, windows, command registration
│       ├── app.rs           # AppState: config, engine client, controller handle, status
│       ├── config.rs        # serde structs, defaults, load/save, mtime watcher, unknown-key keep
│       ├── platform.rs      # paths, autostart toggle, focused-window class, spawn-new-instance
│       ├── audio.rs         # cpal stream, ring buffer, RMS/VU, utterance assembly, device list
│       ├── hotkey.rs        # combo parse/serialise, hold/toggle/tap/cancel semantics
│       ├── engine.rs        # sidecar spawn/restart, JSON-RPC client, request timeouts, events
│       ├── inject.rs        # enigo typing, clipboard paste + restore, exclusion list
│       ├── postproc.rs      # fillers, replacements, capitalisation, trailing space
│       ├── sound.rs         # synthesise cues (sine mix), rodio playback, volume/mute
│       ├── overlay.rs       # window show/hide/position/scale, event forwarding
│       ├── controller.rs    # state machine: hotkey → capture → transcribe → postproc → inject
│       ├── history.rs       # sqlite (rusqlite, bundled feature) opt-in history + retention
│       └── ipc.rs           # #[tauri::command] surface
├── engine/                  # python sidecar
│   ├── pyproject.toml  requirements.txt
│   ├── murmur_engine/{__main__,rpc,stt,models,devices}.py
│   └── tests/test_{rpc,models,stt}.py
├── assets/murmur.svg, murmur.png (1024), cues documented (generated at runtime, not files)
├── scripts/{dev.sh,build-linux.sh,build-windows.ps1,engine-sidecar.sh}
└── .github/workflows/{linux-build.yml,windows-build.yml,release.yml}
```

## Config (TOML; unknown keys preserved on save)

```toml
[general]
hotkey = "Ctrl+Alt+D"
hotkey_mode = "hold"        # hold | toggle
language = "en"             # "" = autodetect
translate = false

[overlay]
enabled = true
position = "bottom-center"  # bottom-center | top-center
scale = 1.0                 # 0.8 – 1.6
show_preview = true
idle_hide = true

[sounds]
enabled = true
volume = 0.6
muted = false

[engine]
model = "distil-small.en"   # "" = none installed
device = "auto"             # auto | cuda | cpu
compute_type = "auto"       # int8_float16 (cuda) | int8 (cpu) when auto
beam_size = 1
vad_filter = true
dictionary = []

[audio]
device = "default"
gain = 1.0
silence_threshold = 0.012
silence_timeout_ms = 1200
max_utterance_s = 45
min_utterance_ms = 250

[injection]
method = "auto"             # auto | type | clipboard
clipboard_only = false
trailing_space = true
capitalize = true
remove_fillers = false
exclude_apps = []

[postproc]
replacements = { "mur mur" = "Murmur" }

[privacy]
history_enabled = false
history_retention_days = 7
```

Config lives beside the app data (see Targets). Writing is atomic (temp file + rename).
The Rust side owns the file; the UI mutates it only through `config_set` commands.

## Runtime behaviour (contract)

State machine: `idle → listening → transcribing → done → idle`; `listening → idle` (cancel);
`transcribing → error → idle`. Every transition emits a `state` event; `level` ~20 Hz while
listening/capturing; `partial` for live preview; `latency` after injection; `error` with a
human-readable message.

1. **Capture (audio.rs)**: open one cpal input stream at the device's default config, convert
   to mono f32, resample (linear) to 16 kHz, push into a 60 s ring buffer. Compute RMS per 20 ms
   frame → `level` events (throttled 20 Hz) and silence endpoint detection. On hotkey press record
   the start cursor minus 250 ms pre-roll; on stop assemble PCM, apply `gain`, clamp.
   If the stream errors (device unplugged), emit `error`, rebuild the stream on next press.
2. **Hotkeys (hotkey.rs)**: global shortcut registration with the configured accelerator.
   `hold`: activation → start, deactivation → stop. `toggle`: activation → start/stop
   alternating; auto-stop after `silence_timeout_ms` of silence following ≥300 ms of speech;
   `Escape` (registered only while listening) cancels with no sound. Re-registering a hotkey at
   runtime must succeed within 100 ms and report failure to the UI (conflict message).
   Parse "Ctrl+Alt+D" style strings into Tauri accelerator syntax; reject modifier-only combos.
3. **Engine (engine.rs + engine/)**: spawn `murmur-engine` sidecar with `--models-dir <path>`;
   stdin/stdout JSON-RPC, one JSON object per line; `{"id":n,"method":"transcribe","params":
   {"pcm_b64":..., "sample_rate":16000, "language":"en", "translate":false, "beam_size":1,
   "vad":true, "initial_prompt":"..."}}` → `{"id":n,"ok":true,"result":{"text":...,"language":
   ...,"duration_ms":...,"infer_ms":...}}`. Methods: `ping`, `status`, `transcribe`,
   `partial`, `models.list`, `models.install` (streams `{"event":"download",...}`), `models.remove`,
   `shutdown`. Auto-restart the sidecar once on crash; surface a persistent error if it fails
   twice. Model loads lazily in the sidecar at first use and stays warm; `device=auto` tries CUDA
   then permanently falls back to CPU, reporting the resolved device in `status`.
4. **Post-process (postproc.rs)**: trim/collapse whitespace, optional filler removal
   (um, uh, er, hmm: configurable list, whole words only), replacement map (case-insensitive,
   longest phrase first), capitalise sentence starts, single trailing space. Never touch numbers,
   mixed-case tokens, URLs or code-like strings.
5. **Inject (inject.rs)**: `auto`: ASCII ≤1500 chars → enigo text typing; else clipboard paste
   (save current clipboard text, set ours, send Ctrl+V, wait 300 ms, restore): with a
   clipboard-restore fallback that leaves the text in the clipboard if restore fails.
   `clipboard_only` or focused-app match in `exclude_apps` → clipboard only.
   Never inject into Murmur's own windows. On Windows use enigo unicode typing for the fast path.
6. **Sounds (sound.rs)**: synthesise at runtime (no audio files in the repo): start = two
   rising sine tones (660→880 Hz, 45 ms each, 8 ms envelope), stop = falling (880→660), error =
   two 180 Hz buzzes. Scale amplitude by `volume`, play fire-and-forget on a dedicated thread
   (rodio Sink), never block the pipeline, skip when muted/disabled.
7. **Overlay (overlay.rs + src/overlay)**: separate transparent, undecorated, always-on-top,
   skip-taskbar window, `ignore_cursor_events(true)`, positioned per config on the primary
   monitor's work area (bottom-centre 48 px up, or top-centre). Content: idle = muted dot + "mur";
   listening = 5 animated level bars + optional 2-line partial preview (pill grows to 560 px wide);
   transcribing = pulsing dots; done = check, hide after 900 ms; error = amber dot, hide after
   1.6 s. With `idle_hide` the window hides 1.5 s after going idle. Repaint only on events.
8. **Tray (main.rs)**: icon, tooltip `Murmur: <state>`, menu: state label (disabled), "Dictate"
   (toggle mode only), Sounds mute check, Overlay enabled check, "Settings…", "Reload config",
   "Quit". Left-click opens Settings. Missing model → tooltip `Murmur: no model`.
9. **History (history.rs)**: opt-in SQLite (`rusqlite` with `bundled`): `entries(id, ts, text,
   app, duration_ms, latency_ms, model)`; prune on startup + daily; UI list/search + delete-all.
10. **First run**: no model: UI shows a banner "Choose a speech model to start dictating" with a
   jump to Engine; hotkey press plays the error cue and sets error state with a clear message.
11. **Single instance**: `tauri-plugin-single-instance`: second launch focuses Settings and exits.

## Coding mode: primary use case: dictating prompts and code (vibecoding)

Most dictation output goes into a chat box / editor prompt (Hermes, Cursor, Claude Code, VS Code,
terminals). Speech-to-text mangles technical tokens unless explicitly handled, so coding mode is a
first-class path with its own config, not a hidden extra.

```toml
[postproc]
style = "auto"                 # prose | code | auto  (auto = resolve per focused app)
code_apps = ["code", "cursor", "vscode", "code-oss", "jetbrains", "zed", "sublime",
             "gnome-terminal", "konsole", "alacritty", "kitty", "wezterm", "windowsterminal", "mintty"]
spoken_punctuation = true      # deterministic phrase → symbol map (see below)
allow_newlines = false         # false → "newline"/"next line" emits a space, transcript stays single-line

[general]
hotkey_code = ""               # optional second hotkey that forces code style regardless of app

[injection]
prefer_paste = true            # paste instead of typing for code style and long text
```

Rules:
- **Spoken punctuation map** (case-insensitive, whole-word, longest phrase first, applied before
  the replacement map): `comma` → `,` · `period`/`full stop` → `.` · `question mark` → `?` ·
  `exclamation mark`/`exclamation point` → `!` · `colon` → `:` · `semicolon` → `;` ·
  `open paren`/`open parenthesis`/`left paren` → `(` · `close paren`/`right paren` → `)` ·
  `open bracket` → `[` · `close bracket` → `]` · `open brace` → `{` · `close brace` → `}` ·
  `underscore` → `_` · `hyphen`/`dash` → `-` · `slash` → `/` · `backslash` → `\` · `dot` → `.` ·
  `at sign` → `@` · `hash` → `#` · `asterisk` → `*` · `ampersand` → `&` · `pipe` → `|` ·
  `equals` → `=` · `double equals` → `==` · `arrow` → ` -> ` · `fat arrow` → ` => ` ·
  `percent` → `%` · `dollar sign` → `$` · `plus` → `+` · `less than` → `<` · `greater than` → `>` ·
  `backtick` → `` ` `` · `quote` … `unquote` → wraps the enclosed words in `"` ·
  `newline`/`next line` → `\n` when `allow_newlines` else a single space.
  Symbol substitution inserts the symbol with the spacing a human would use: no space before
  `, . : ; ) ] }`, one space after, no space around `_ / \ . @` when between word characters.
- **Code style never** capitalises sentence starts, never removes fillers unless asked, never
  reflows or "corrects" the sentence, and never rewrites a token containing `_ . / ( ) { } :: => < >`
  or internal capitals. Trailing space is still added when `trailing_space` is true.
- **Injection preference**: code style or `len(text) > 200` → clipboard paste (atomic, preserves
  punctuation and newlines exactly, immune to dropped keystrokes in Electron apps); short prose →
  type. `clipboard_only` still wins, and exclusions still apply.
- **Terminal safety**: when the focused app is in `code_apps` and looks like a terminal, `\n` is
  never injected (a newline would execute a line); multi-line prompts stay one paragraph.
- **Dictionary** seeded with coding vocabulary that Whisper mishears, editable in Settings:
  `Tauri, faster-whisper, CTranslate2, WebKitGTK, WebView2, serde, tokio, enigo, cpal, rodio,
  Murmur, zethrus, Python, TypeScript, npm, cargo, git, stdin, stdout, JSON-RPC, hotkey, overlay,
  systemd, Wayland, X11, pip, venv, regex, null, boolean` (trim to what is plausible; it only
  biases the decoder via `initial_prompt`).
- `[audio] max_utterance_s` default 90: long prompts are normal for vibecoding. Toggle mode is
  the recommended mode here (documented in README), silence auto-stop still applies.
- README gains a "Vibecoding workflow" section: recommend `large-v3-turbo`, toggle mode, code style
  auto-detect, overlay preview to catch misheard identifiers before they land, and the Esc cancel.
- Tests required: punctuation map (spacing, longest-match, no false positives inside words),
  quote/unquote pairing, newline gating, code-style non-capitalisation, style auto-resolution by
  window class, and paste-vs-type selection (style, length, clipboard_only, exclusions).

## IPC surface (Tauri commands; all return `Result<_, String>`)

`get_config`, `set_config(patch)`, `get_status` (state, model, device, compute, sidecar alive),
`list_models`, `install_model(name)`, `remove_model(name)`, `test_engine()`, `list_devices`,
`set_hotkey(accel)`, `preview_sound(kind)`, `start_dictation`, `stop_dictation`,
`cancel_dictation`, `open_settings`, `quit_app`, `history_search(q, limit)`, `history_clear`,
`pick_exclude_app()`, `open_path(kind)`, `apply_overlay_prefs()`.
Events to the webview: `state`, `level`, `partial`, `latency`, `error`, `download`, `config`.

## Design tokens (single source of truth, cross-platform parity)

The UI is one codebase, so parity is mostly structural: but WebKitGTK and WebView2 differ in
fonts, default control chrome, scrollbars and sub-pixel rendering. Fix that with tokens, not
per-platform CSS forks.

- `src/styles/tokens.css` is the ONLY place literals live. Layers:
  1. **Primitive** tokens (`--grey-50 … --grey-950`, `--blue-400/500/600`, `--amber-500`,
     `--green-500`, radii `--r-sm/md/lg/pill`, spacing `--sp-1…--sp-10` on a 4 px scale, type
     scale `--fs-11 … --fs-22`, weights, line-heights, motion `--dur-fast/base/slow`,
     easing `--ease-out`, z-index, border widths).
  2. **Semantic** tokens (`--bg`, `--surface`, `--surface-raised`, `--border`, `--text`,
     `--text-muted`, `--accent`, `--accent-contrast`, `--danger`, `--ok`, `--warn`, `--shadow-1/2`,
     `--overlay-bg`, `--meter-track`, `--meter-fill`) mapped from primitives once, per theme.
  3. **Component** tokens (`--row-height`, `--sidebar-w`, `--card-radius`, `--switch-track`,
     `--slider-thumb`, `--pill-h`): no component CSS reads primitives directly.
- Themes: `:root` (light), `:root[data-theme="dark"]`, plus `@media (prefers-color-scheme: dark)`
  when `data-theme="auto"`. Every semantic token has a value in both themes; the settings app can
  switch Light / Dark / Auto, and the overlay follows the same tokens.
- **Platform parity tokens**: `--font-ui` = `"Inter", "Ubuntu", "Segoe UI", system-ui, sans-serif`
  (WebKitGTK resolves Ubuntu, WebView2 resolves Segoe UI: same metrics bucket, no layout shift);
  `--font-mono` = `"JetBrains Mono", "Cascadia Mono", "Ubuntu Mono", monospace`;
  `--scrollbar-w: 10px`; `--hairline: 1px` (accepts 1 device px on both). Both OSes use the same
  sizes, radii, spacing and motion; only font resolution and the scrollbar engine differ, and both
  are pinned by tokens.
- No raw hex, px radius, or ad-hoc spacing outside `tokens.css`. `scripts/check-tokens.mjs`
  fails the build on any `#rrggbb`/`rgb(`/`hsl(` in `src/**/*.css` and `src/**/*.ts` outside the
  token file, and on any `border-radius:` / `padding:` literal in component CSS that is not a
  token reference. Wire it into `npm run build` (and CI).
- Shared chrome normalisation in `tokens.css`: `* { box-sizing: border-box }`,
  `::selection` from tokens, custom scrollbar width/colour per theme, `-webkit-tap-highlight: none`,
  `font-synthesis: none`, and a `:focus-visible` ring from `--accent` at 2 px/2 px offset.
- Parity check (manual, in README + DoD): open Settings and the overlay on Linux; on Windows open
  the same build and confirm identical spacing/typography/colour roles, only font family differing.
  Record both screenshots under `docs/parity/` (gitignored if large: keep small PNGs).

## Settings UI (must look deliberate, not default-browser)

- Sidebar (240 px) with 5 sections and a footer showing daemon status dot + version; content pane
  max-width 720 px, generous spacing (24 px gutters), one concept per row: label + hint on the
  left, control on the right. Rounded 12 px cards on `--surface` with 1 px border, subtle shadow.
- Typography: system stack (`Inter`, `Ubuntu`, `Segoe UI`, system-ui); 13 px body, 15 px section
  titles, 11 px uppercase muted hints with 0.06em tracking. Dark and light via
  `prefers-color-scheme`, both defined as CSS custom properties; accent `#7aa2f7` dark /
  `#3b6fd4` light; 150 ms ease transitions; no spinners longer than necessary.
- Controls: custom-styled switch, slider (track + 14 px thumb), select, and a **hotkey capture
  field** that records a combo (shows key chips, clears with Backspace, rejects modifier-only,
  shows inline conflict error). Level meter = 24 rounded bars driven by `level` events.
- Pages: General (hotkey, mode, language, translate, overlay prefs, sounds + preview buttons,
  autostart), Engine (model cards with size/accuracy/installed state, install progress bar,
  device selector with detected hardware line, beam, VAD, "Test 3 s" showing measured latency,
  dictionary list editor), Audio (device select, gain, live meter, silence threshold/timeout, max
  utterance), Behavior (injection method, clipboard-only, trailing space, capitalise, fillers,
  exclusions list, replacements table, last transcript), Privacy (local-only statement, history
  switch + retention + search + clear, path list with "Open folder").
- Empty states and disabled states must be handled (no model, no devices, sidecar down).

## Build & packaging

- `npm run dev` → Vite; `npm run tauri dev` → dev app with the Python sidecar from
  `scripts/engine-sidecar.sh` (Linux) or `engine/.venv/Scripts/python.exe` (Windows).
- `scripts/build-linux.sh`: `npm ci && npm run build && cargo tauri build` → binary +
  `.deb` (bundle targets `["deb","appimage"]`, appimage optional/allow failure).
- **Windows**: `scripts/build-windows.ps1` (PowerShell, no admin): creates `engine\.venv`,
  `pip install -r engine\requirements.txt pyinstaller`, builds `murmur-engine.exe` with
  `--onefile`, copies to `src-tauri\binaries\murmur-engine-x86_64-pc-windows-msvc.exe`,
  `npm ci`, `npm run build`, `cargo tauri build --target x86_64-pc-windows-msvc` → portable
  `murmur.exe` in `target\release\` plus optional NSIS installer from the bundler.
  `tauri.conf.json` must declare `bundle.externalBin: ["binaries/murmur-engine"]` and per-platform
  targets so both builds work without editing config.
- CI: `.github/workflows/windows-build.yml` (windows-latest: rust + node + python, cache cargo,
  upload artifacts exe + installer) and `linux-build.yml` (ubuntu-22.04: apt installs
  webkit2gtk-4.1-dev etc., build, upload deb/AppImage). `release.yml` on tag `v*` publishes both.
- Icons: Tauri needs `src-tauri/icons/*`; generate from `assets/murmur.png` via
  `cargo tauri icon assets/murmur.png` (commit the generated icons).

## Code rules

- Rust: one responsibility per module, ≤400 lines, no `unwrap()` on fallible I/O (use `?` +
  `anyhow` context), all threads named + joined on shutdown, no busy loops, `tracing` logs to
  `<data>/murmur.log` (rolling, 2 MB × 3).
- TS: no framework, no globals; a `bridge.ts` typed façade is the only place touching Tauri;
  state comes from events, never polled faster than 4 Hz.
- Python: typed public functions, dataclasses, no asyncio, no globals; JSON-RPC loop must never
  print anything except protocol lines on stdout (logs to stderr).
- No transcript text in logs unless `MURMUR_LOG=debug`. No network in the app except explicit
  model install (`HF_HUB_OFFLINE=1` when idle).

## Definition of done

1. `cargo build --manifest-path src-tauri/Cargo.toml` succeeds on Linux with zero warnings that
   matter; `cargo test --manifest-path src-tauri/Cargo.toml` green for config, accelerator parse,
   postproc, injection path selection, silence maths, sounds amplitude, history CRUD.
2. `engine` tests green (`pytest -q` in `engine/`), all model calls mocked, no network.
3. `npm run build` (tsc + vite + `node scripts/check-tokens.mjs`) clean; `npm run tauri build --
   --no-bundle` produces a binary that launches on this X11 session, shows tray icon + overlay,
   and answers `get_status`. No hardcoded colour/spacing literals outside `tokens.css`.
4. Hand-verified on this machine: settings window opens with all five pages, screenshot captured;
   overlay screenshots for idle and listening; hotkey rebind takes effect without restart.
5. `scripts/build-windows.ps1` reviewed line by line for correctness (cannot be executed here);
   documented as "unverified until run on Windows" in README.
6. README: install, run, hotkeys, privacy statement, performance numbers, Windows build steps,
   troubleshooting (no model, hotkey conflict, mic, sidecar crash), and the test-audio recipe
   (`espeak-ng` or `ffmpeg` synth + `aplay`/`paplay`), plus how to point the app at a wav instead
   of the mic for QA (`MURMUR_QA_WAV=<path>` must be supported by audio.rs).
7. Git repo with clean commits; no build artifacts committed.
