# Configuration reference

Linux: `${XDG_CONFIG_HOME:-~/.config}/murmur/config.toml`, data/models/history/logs
under `${XDG_DATA_HOME:-~/.local/share}/murmur`. Windows:
`%APPDATA%\Murmur\config.toml` and `%LOCALAPPDATA%\Murmur` for data.
Use Settings or edit TOML; writes are atomic, unknown keys are preserved, and
invalid edits report errors while retaining the last valid configuration.
The tables below cover user settings. Unknown keys are preserved for compatibility. Defaults are application defaults; a fresh
Linux user install explicitly enables autostart without changing the app default.

| TOML key | Default | Meaning / accepted values |
| --- | --- | --- |
| `general.hotkey` | `"Ctrl+Alt+D"` | Global dictation accelerator; not modifier-only |
| `general.hotkey_mode` | `"hold"` | `hold` or `toggle` |
| `general.hotkey_code` | `""` | Optional distinct accelerator forcing code style |
| `general.language` | `"en"` | Language code; empty string enables autodetection |
| `general.translate` | `false` | Translate speech to English; use a multilingual model |
| `general.autostart` | `false` | Start in the background on login; true in a fresh Linux installer-created config |
| `general.theme` | `"auto"` | `auto`, `light`, `dark`; shared by settings and overlay |
| `overlay.enabled` | `true` | Show overlay |
| `overlay.position` | `"bottom-center"` | `bottom-center` or `top-center` |
| `overlay.scale` | `1.0` | Scale, 0.8–1.6 |
| `overlay.show_preview` | `true` | Live recognition preview |
| `overlay.idle_view` | `"icon"` | Between dictations: `icon` (small draggable app icon), `pill` (full pill), `hidden`. Replaces `idle_hide`; `idle_hide = false` migrates to `pill` |
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
| `injection.focus_change` | `"hold"` | When the focused window at delivery differs from the one at the dictation shortcut: `hold` (review in the overlay), `refocus` (return to the original window, else hold), `copy` (clipboard only), `insert` (type into the current window) |
| `injection.insert_held_hotkey` | `""` | Optional shortcut that types held text into the window focused when pressed |
| `postproc.replacements` | `{ "mur mur" = "Murmur" }` | Case-insensitive whole-word corrections, longest phrase first |
| `postproc.fillers` | `["um", "uh", "er", "hmm"]` | Filler list; no empty entries |
| `postproc.style` | `"auto"` | `prose`, `code`, `auto` (resolve by focused app) |
| `postproc.code_apps` | Editor/terminal list below | App-name/class matches that select code style |
| `postproc.spoken_punctuation` | `true` | Convert punctuation phrases to symbols |
| `postproc.allow_newlines` | `false` | Allow spoken newline outside detected terminals |
| `privacy.history_enabled` | `false` | Opt-in local SQLite transcript history |
| `privacy.history_retention_days` | `7` | Retention, 1–3650 days |
| `companion.enabled` | `false` | Listen for the Flipper companion link |
| `companion.transport` | `"auto"` | `auto`, `usb`, `ble`; auto tries USB first, then Bluetooth |
| `companion.usb_port` | `""` | Explicit serial device; empty discovers the Flipper |
| `postproc.remove_duplicate_words` | `true` | Remove adjacent repeated words |
| `companion.ptt` | `false` | Hold the Flipper OK button to dictate |
| `companion.ble_address` | `""` | Explicit Bluetooth address; empty discovers the device |
| `companion.source` | `"auto"` | `auto`, `local`, `phone`; leave `auto` or `local` for this release |
| `companion.network.enabled` | `false` | Experimental phone listener; Android APK is deferred |
| `companion.network.bind` | `"127.0.0.1"` | Listener IP address |
| `companion.network.port` | `8787` | TLS listener port |
| `companion.network.mdns` | `false` | Reserved; discovery is not implemented |
| `companion.buttons.*` | see docs | Button-to-verb map: `dictate_toggle`, `enhance`, `accept`, `dismiss`, `cancel`, `none` |

Default dictionary: `Tauri`, `faster-whisper`, `CTranslate2`, `WebKitGTK`, `WebView2`,
`serde`, `tokio`, `enigo`, `cpal`, `rodio`, `Murmur`, `zethrus`, `Python`, `TypeScript`,
`npm`, `cargo`, `git`, `stdin`, `stdout`, `JSON-RPC`, `hotkey`, `overlay`, `systemd`,
`Wayland`, `X11`, `pip`, `venv`, `regex`, `null`, `boolean`.

Default code apps: `code`, `cursor`, `vscode`, `code-oss`, `jetbrains`, `zed`,
`sublime`, `gnome-terminal`, `konsole`, `alacritty`, `kitty`, `wezterm`,
`windowsterminal`, `mintty`. Linux matches X11 window classes; Windows uses
foreground executable names. Explicit lists, including empty ones, are preserved.

## Enhance

| TOML key | Default | Purpose |
| --- | --- | --- |
| `enhance.auto_rewrite` | `false` | Offer a rewrite after dictation |
| `enhance.rewrite_hotkey` | `""` | Optional rewrite shortcut |
| `enhance.accept_hotkey` | `""` | Optional accept shortcut |
| `enhance.style` | `"concise"` | Rewrite style |
| `enhance.model` | `"qwen3-1.7b-q4_k_m"` | Local model identifier |
| `enhance.accept_mode` | `"clipboard"` | Accept copies the suggestion |
| `enhance.rewrite_code` | `false` | Allow rewriting code/terminal dictation |
| `enhance.threads` | `0` | Automatic thread count when zero |
| `enhance.unload_after_s` | `120` | Unload the idle model after this many seconds |
| `enhance.load_timeout_ms` | `30000` | Model loading deadline |
| `enhance.timeout_ms` | `60000` | Generation deadline |

`overlay.placement` records a dragged overlay's monitor name, monitor bounds and
fractional x/y position. Let the app write these fields. Zero monitor dimensions
mean that no custom placement is saved.

[Back to the README](../README.md)

Manual launches open Settings. Login entries pass `--background` to keep the
window hidden. Enabled login entries are refreshed when Murmur starts so that
older registrations receive this argument.
