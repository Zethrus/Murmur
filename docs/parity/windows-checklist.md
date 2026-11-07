# Windows parity checklist (manual, logged-in session on Edison)

Interactive checks that CI cannot run: the Edison runner service lives in session
0 with no visible desktop, no global hotkeys and no foreground window. Run these
in the normal logged-in desktop session, on the build from the pull request or
draft release under test. Nothing here has been run yet; every Windows behaviour
in Murmur is **verified in source, not run on hardware** until this list is done.

Rules for evidence: synthetic text only (read the sentences below aloud), no
personal paths, account names or notifications in screenshots. Save files under
`docs/parity/captures/windows-<version>/` (PNG/MP4/TXT; large videos stay out of
git, see [README.md](README.md)), and record each result in
[test-ledger.md](test-ledger.md) as executed with the file name. A failed item is
recorded as failed with the observed behaviour; do not retry until it passes
without saying so.

Test sentence A: "The quick brown fox jumps over the lazy dog." Sentence B:
"Version two point one ships on Friday."

## 0. Setup

| Id | Step | Expected | Evidence |
| --- | --- | --- | --- |
| P1 | Record Windows edition/build (`winver`), WebView2 version, GPU/driver (`nvidia-smi`), monitor resolutions and scaling, Murmur version (`murmur.exe --version`) and commit | Filled evidence template (README.md) | `P1-environment.txt` |
| P2 | Quit any running Murmur. Install with `Murmur_<ver>_x64-setup.exe` (unsigned: SmartScreen → More info → Run anyway). Note the install path | Per-user install under `%LOCALAPPDATA%\Programs\Murmur` (or as shown); no UAC prompt; Start-menu entry "Murmur" | `P2-installer.png`, `P2-install-path.txt` |
| P3 | First start: setup appears; choose English-only, `distil-small.en`, CPU; confirm the summary; let it download | Sizes shown before download; progress per component; engine starts without restart; GPU option **not** offered (hidden until validated) | `P3-setup-summary.png`, `P3-setup-done.png` |
| P4 | Settings → Components | Engine and model "Active"; Verify succeeds | `P4-components.png` |

## 1. Settings screenshots (pairs with Linux baseline)

Run `scripts\parity\capture-windows.ps1 -App <path to murmur.exe>` at 100 % and
again at 150 % scaling (Settings → Display). It saves
`windows-<theme>-<page>-<scale>.png` for six pages × light/dark plus a manifest.

| Id | Step | Expected | Evidence |
| --- | --- | --- | --- |
| S1 | Capture at 100 % | 12 PNGs + `manifest-100.md`; compare with `captures/baseline-0.4.7/linux-*` per README.md "Must match" | `windows-*-100.png` |
| S2 | Capture at 150 % | No clipped text, overlapping controls or horizontal scrollbars | `windows-*-150.png` |
| S3 | Theme Auto: switch Windows light/dark (Settings → Personalization → Colors) with Murmur open | Settings and overlay follow without restart | `S3-auto-light.png`, `S3-auto-dark.png` |
| S4 | Resize Settings to the minimum (760 × 560 logical) and back to default 1000 × 780 | Window refuses smaller sizes; every page usable at minimum | `S4-min-<page>.png` |

## 2. Overlay

| Id | Step | Expected | Evidence |
| --- | --- | --- | --- |
| O1 | Disable Idle hide; capture idle, listening (two-line preview), transcribing, done, error (start dictation with the microphone unplugged) | Transparent background, no taskbar button, no shadow box | `O1-overlay-<state>.png` or a recording |
| O2 | Drag the handle to another position; restart Murmur | Position restored on the same monitor | `O2-drag.mp4` |
| O3 | **B7.** Open Notepad and type a word. Produce an Enhance suggestion (or a held transcript), then click **Discard** and **Accept** on the overlay; immediately type more letters without clicking Notepad | Letters continue in Notepad; Notepad's title bar stays active the whole time | `O3-overlay-click-focus.mp4` |
| O4 | Same as O3 but click the overlay's empty area and the drag handle | Notepad keeps focus; drag works | `O4-overlay-empty-click.mp4` |
| O5 | Mixed DPI (if a second monitor is available, set 100 % and 150 %): drag the overlay across the boundary, release on each monitor | Size and position follow the monitor; no jump back | `O5-mixed-dpi-drag.mp4` |
| O6 | Overlay scale minimum/maximum in Settings → General | Pill resizes; text not clipped | `O6-scale-<value>.png` |

## 3. Hotkeys

| Id | Step | Expected | Evidence |
| --- | --- | --- | --- |
| H1 | Default hold hotkey Ctrl+Alt+D in Notepad, say sentence A, release D first and Ctrl+Alt last | Sentence A typed once; no menu opened | `H1-hold.mp4` |
| H2 | Hold mode: release all keys together; then release D and keep Ctrl+Alt down ~1 s | Exactly one transcript each time; dictation does not restart | `H2-release-order.mp4` |
| H3 | **B8.** Hotkey with Shift (set Ctrl+Shift+Space), hold Shift slightly longer than Space | No capitalised/shifted first word; text starts within ~0.6 s after Shift is up | `H3-shift-held.mp4` |
| H4 | Toggle mode: press once, speak, press again | One transcript | `H4-toggle.mp4` |
| H5 | Esc while listening | Cancelled, nothing typed | `H5-esc.mp4` |
| H6 | Set a hotkey already owned by another app (e.g. Win+E) | Inline "unavailable (another app may own it)" error; old hotkey keeps working | `H6-conflict.png` |
| H7 | Code hotkey (Settings → General) into VS Code | Code style (no capitalisation/punctuation changes) | `H7-code.mp4` |

## 4. Text insertion

| Id | Step | Expected | Evidence |
| --- | --- | --- | --- |
| I1 | Sentence A into Notepad (type path) | Exact text | `I1-notepad.png` |
| I2 | Sentence B into VS Code | Exact text | `I2-vscode.png` |
| I3 | Sentence A into Windows Terminal (PowerShell) | Typed on one line, nothing executed | `I3-terminal.png` |
| I4 | A transcript over 60 characters with Prefer paste on, into Notepad | Pasted; previous clipboard restored | `I4-paste.mp4` |
| I5 | Non-ASCII ("café déjà vu") into Notepad | Correct characters (clipboard path) | `I5-unicode.png` |
| I6 | Clipboard only (exclude `notepad.exe`) | Nothing typed; clipboard holds the text | `I6-clipboard-only.png` |

## 5. Focus contract ([focus-contract.md](../focus-contract.md))

| Id | Step | Expected | Evidence |
| --- | --- | --- | --- |
| F1 | Start and finish in the same Notepad window | Delivered | `F1.mp4` |
| F2 | Two Notepad windows: start in one, click the other before release | Held ("Not inserted: focus changed"); Insert here types into the window focused when clicked | `F2.mp4` |
| F3 | Start in Notepad, close it and open a new Notepad before release (possible HWND/PID reuse) | Held | `F3.mp4` |
| F4 | Hold mode: Copy, then Discard on a second held transcript; wait 2 min on a third | Copy puts text on clipboard; Discard clears; third expires after 120 s | `F4.mp4` |
| F5 | Refocus mode: start in Notepad, switch to VS Code before release | Notepad comes back and receives the text, or (if Windows refuses) the text is held — never typed into VS Code | `F5.mp4` |
| F6 | Copy mode with a focus change | Clipboard only, notice shown | `F6.mp4` |
| F7 | Insert mode with a focus change | Typed into the new window | `F7.mp4` |
| F8 | Start dictation from the Settings window, finish in Notepad | Held (unknown/own origin) | `F8.mp4` |
| F9 | **B9.** Insert mode: start in Notepad; before release trigger a UAC prompt (e.g. run `regedit`) and leave it up | Held with "a Windows security prompt or the lock screen had focus"; nothing typed anywhere | `F9-secure-desktop.mp4` (phone video: the secure desktop cannot be screen-recorded) |
| F10 | **B9.** Run Notepad as administrator; dictate into it (Murmur not elevated), in hold and insert modes | Held with "the window runs as administrator…"; Copy works; nothing claims "delivered" | `F10-elevated-notepad.mp4` |
| F11 | Drag the overlay while a transcript is ready, release over Notepad | Delivery waits for the release, then follows rules above | `F11-drag-delivery.mp4` |
| F12 | Check `%LOCALAPPDATA%\Murmur\murmur.log` after F2–F10 | No transcript text in the log | `F12-log-excerpt.txt` |

## 6. Tray, autostart, single instance

| Id | Step | Expected | Evidence |
| --- | --- | --- | --- |
| T1 | Left-click the tray icon | Settings opens and comes to the front | `T1-tray-left.mp4` |
| T2 | Right-click: each menu item (Dictate in toggle mode, Mute, Overlay, Settings, Reload, Quit) | Each works; Quit exits and removes the icon | `T2-tray-menu.mp4` |
| T3 | Launch `murmur.exe` again while running | No second instance; Settings comes forward | `T3-single-instance.mp4` |
| A1 | Enable Start with Windows; sign out and in | Murmur starts in the tray without opening Settings (`--background`); `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` has the entry | `A1-autostart.png`, `A1-run-key.txt` |
| A2 | Disable it | Run entry removed | `A2-run-key.txt` |

## 7. Microphone privacy

| Id | Step | Expected | Evidence |
| --- | --- | --- | --- |
| M1 | Settings → Privacy & security → Microphone: turn off "Let desktop apps access your microphone"; open Murmur → Audio, press Refresh | Explanation names that switch; **Open Windows privacy settings** opens the Microphone page | `M1-desktop-apps-off.png` |
| M2 | Turn off "Microphone access" (device) | Explanation names the device switch | `M2-device-off.png` |
| M3 | Turn everything back on, press Refresh, dictate | No explanation; level meter moves; dictation works | `M3-restored.png` |
| M4 | Unplug every microphone | "No microphone found" explanation with the Settings button | `M4-no-devices.png` |

## 8. Keyboard and Narrator

| Id | Step | Expected | Evidence |
| --- | --- | --- | --- |
| K1 | Settings: Tab through every page | Visible focus ring; logical order; Enter/Space activate | `K1-keyboard.mp4` |
| K2 | Narrator (Ctrl+Win+Enter) on General and Audio | Page buttons, controls and their labels are announced | `K2-narrator.txt` (transcript of what was read) |
| K3 | Reduced motion (Settings → Accessibility → Visual effects → Animation off) | Overlay animations reduced | `K3-reduced-motion.mp4` |

## 9. Components on Windows

| Id | Step | Expected | Evidence |
| --- | --- | --- | --- |
| C1 | Components → Remove the engine while Murmur is idle | Removed; overlay says the engine is missing; no "in use" file error | `C1-remove-engine.png` |
| C2 | Reinstall the engine while an antivirus scan of `%LOCALAPPDATA%\Murmur` runs | Install succeeds (rename retries ≤ 5 s) or fails with a clear message and keeps the previous state | `C2-reinstall.png` |
| C3 | Pause and resume a model download; quit Murmur mid-download and restart | Paused after restart; resumes only on click | `C3-pause-resume.mp4` |

When all items pass, attach the evidence folder to the pull request or release
discussion and update [matrix.md](matrix.md) rows from "verified in source" to
"verified on hardware (<date>, Edison)".
