# Linux / Windows parity matrix

Status, November 2026. Linux behaviour is what the current Linux build does.
Every Windows entry is **unverified on hardware**: Windows CI only compiles, and
no Windows build has been run on a real machine. "Verified in source" means a
reviewer read the code path; it is not a test result. Evidence for each check is
recorded in [test-ledger.md](test-ledger.md); the visual checklist is in
[README.md](README.md).

| Feature | Linux behaviour | Windows status | Gap / work | Verification |
| --- | --- | --- | --- | --- |
| Hold/toggle hotkey, code hotkey, Esc cancel | X11 key grab via tauri-plugin-global-shortcut | RegisterHotKey; key-release delivery unverified on hardware | Confirm release events; report registration conflicts | Rust unit tests; Windows interaction test |
| Microphone list, selection, gain, meter | ALSA/PipeWire through cpal | WASAPI through cpal; unverified on hardware | Detect the Windows microphone privacy block and offer a button to `ms-settings:privacy-microphone` | Unit tests; manual check |
| Overlay: transparent, on top, draggable, never activates, states | GTK workarounds ([overlay-drag.md](overlay-drag.md)) | `focusable:false` yields WS_EX_NOACTIVATE; unverified on hardware | B7 (WebView2 child takes focus on click); mixed-DPI placement | Interaction test, screenshot pairs |
| Text insertion (type / paste / clipboard only) | xdotool, waits for modifiers to be released | enigo SendInput; unverified on hardware | B8, B9 | Two-window integration test |
| Focus contract (hold / refocus / copy / insert) | X11 window id at hotkey press vs delivery (`focus_guard.rs`, `held.rs`) | HWND only; unverified on hardware | B10; `SetForegroundWindow` may refuse, which must fall back to hold (no AttachThreadInput tricks) | Unit tests; integration test |
| Tray, single instance, autostart | AppIndicator tray, XDG autostart `.desktop` | Tray and HKCU Run; unverified on hardware | Left-click on the tray icon should open Settings | Manual check |
| Settings: six pages, light/dark/auto themes | WebKitGTK; baseline images in `captures/baseline-0.4.7/` | WebView2; never captured | Fonts and DPI differences; capture Windows pairs | Screenshot pairs |
| Speech model management | Python engine downloads from Hugging Face (B4) | Same code; unverified on hardware | Replaced by the component manager (PR-B) | New tests in PR-B |
| CPU inference | Works (frozen onedir engine) | Built by CI; never run | — | Smoke test on hardware |
| GPU inference | cuBLAS-only CUDA runtime shipped beside the engine; verified on RTX 4060 | No CUDA path (B11) | CUDA add-on component; option hidden until validated on Windows hardware | Linux hardware here; Windows blocked (no NVIDIA Windows machine attached yet) |
| Enhance (local rewrite) | llama.cpp CPU, explicit model download | Same code; unverified on hardware | Move model to signed components (PR-B) | Existing Rust tests |
| Companion (USB / BLE / LAN) | Works with Flipper Zero | Compiles; unverified on hardware | Verify all three transports | Manual check |
| Paths, logs, config | XDG directories | `%APPDATA%` / `%LOCALAPPDATA%`; unverified on hardware | Documentation | Unit tests |

## Known bugs (not requirements)

These were found while building the matrix. They are tracked separately from
parity: fixing one does not by itself establish parity.

| Id | Bug | Status |
| --- | --- | --- |
| B1 | Linux packages shipped cuDNN 9 and two NVRTC copies (≈1.55 GB unpacked) that inference never loads. CTranslate2 4.6.3's Linux wheel has no cuDNN or NVRTC references and runs the Whisper encoder's two Conv1D layers via im2col + cuBLAS GEMM. This is version-specific. | Fixed in PR-A: cuBLAS only; `scripts/check-engine-imports.py` fails the build if a future CTranslate2 references cuDNN/NVRTC. Verified on an RTX 4060 with `distil-small.en`, `large-v3-turbo` (full precision × VAD matrix) and `large-v3`. Windows wheel not yet inspected. |
| B2 | `murmur` binary shipped unstripped (≈10 MB of symbols). | Fixed in PR-A (`[profile.release] strip = true`). |
| B3 | The same shared library copied 2–3 times instead of symlinked (≈20 MB unpacked). | Fixed in PR-A (`dedupe_libraries`). |
| B4 | Speech models download the latest Hugging Face revision with no pinned revision and no hash check, and an install cannot be cancelled; the `.complete` marker is the only integrity signal (`engine/murmur_engine/models.py`). `hf_hub_download` does resume. `large-v3-turbo` comes from a third-party repository (`dropbox-dash`), which a provenance field must record. | Open; PR-B. |
| B5 | The engine was duplicated in the tar.gz; PyAV/FFmpeg (≈128 MB) and hf_xet (11 MB) were frozen but unused. | Fixed in PR-A. |
| B6 | The onefile engine unpacked hundreds of MB into `TMPDIR` on every start. | Fixed in PR-A (onedir). |
| B7 | Windows: `focusable:false` already gives the overlay WS_EX_NOACTIVATE through tao, but clicking Accept/Discard can still give keyboard focus to the WebView2 child HWND and take it from the target app. Fix: answer WM_MOUSEACTIVATE with MA_NOACTIVATE on the child, or handle pointer input only. | Open; PR-C; verify on hardware. |
| B8 | Windows: global-hotkey reports Released by polling GetAsyncKeyState, but modifiers may still be held when typing or pasting starts. Linux waits for them (`inject.rs`). Fix: wait on VK_CONTROL/VK_MENU/VK_SHIFT/VK_LWIN with GetAsyncKeyState. Verified in source, untested on hardware. | Open; PR-C. |
| B9 | Windows: enigo returns an error when SendInput inserts fewer events than requested and `inject.rs` propagates it, but UIPI drops into elevated windows are still not reliably reported. Fix: compare the target's token integrity level (OpenProcessToken + TokenIntegrityLevel) with Murmur's before delivery and hold instead. | Open; PR-C. |
| B10 | Windows: target identity is the HWND only (`platform.rs` `native_focus`); HWNDs can be reused. Fix: HWND + PID + process start time. | Open; PR-C. |
| B11 | Windows CI: no CUDA and no GPU path; it publishes with a third-party action that bypasses the draft-release flow. | Open; PR-C. |
