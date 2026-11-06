# Linux / Windows design parity

Status, November 5, 2026: today's release covers Linux and Flipper. Earlier Linux
checks do not establish that a newly changed binary has passed hardware testing.
Matched cross-platform screenshots and Windows hardware validation are pending. Do not mark a checklist item complete solely because a build or DOM test
passes. Windows remains **unverified on real Windows hardware**.

Overlay movement implementation and Linux native test instructions are recorded in
[overlay-drag.md](overlay-drag.md). Linux results do not clear the Windows
interaction or packaged-release gates below.

## Must match

Use the same commit, model, language, theme, settings and test transcript on both
systems. Compare at equal logical window size and overlay scale, not merely equal
physical screenshot size.

- [ ] Settings: sidebar width/order, six page labels, content gutters, row heights,
  cards, radii, spacing, text hierarchy, label alignment and wrapping.
- [ ] General: shortcut chips/capture/conflict errors, hold/toggle, language,
  translation, overlay controls, cue controls, theme and autostart setting.
- [ ] Engine: model cards, installed/selected/missing states, download progress,
  hardware/status labels, device/compute controls, dictionary and test result.
- [ ] Audio: selected input, gain, meter layout, threshold and duration controls;
  empty device list and disconnected-microphone errors remain understandable.
- [ ] Behavior: code/prose/auto style, code-app list, punctuation/newline controls,
  paste/typing choices, exclusions, fillers, replacements and last transcript.
- [ ] Enhance: explicit model install/progress, disabled default, shortcut suggestions,
  code/terminal opt-in and local-processing explanation match.
- [ ] Enhance overlay: pending/ready previews, Accept copies without pasting, Discard,
  no-changes and stale-result states; clicks/drags retain focus in the target app.
- [ ] Privacy: local-only explanation, opt-in history, retention, search/clear,
  confirmation dialogs and correct platform-specific path labels.
- [ ] Light, dark and auto themes: equivalent semantic color roles, contrasts,
  focus rings, disabled/hover/selected/error states and overlay theme updates.
- [ ] Fonts: equivalent size, weight, line height and hierarchy; no clipping,
  unintended overflow or layout changes when the fallback family resolves.
- [ ] Overlay: transparent, undecorated, interactive without activating its window,
  always-on-top, no taskbar button; draggable handle, stored monitor-relative placement,
  resize and work-area clamping work on both platforms.
- [ ] Overlay states: idle label, listening bars/preview (including wrapped text),
  transcribing dots, done check and amber error; hiding/transition behavior agrees.
- [ ] Keyboard accessibility: tab order, visible focus, capture/reset behavior and
  usable controls. Reduced-motion preference is respected.
- [ ] Resizing: default 1000 × 780 settings window and 760 × 560 minimum remain
  usable; long model/device names, dictionary entries and translations fit.
- [ ] Runtime: hold/release and toggle/silence stop, distinct code shortcut, Esc
  cancel, cue order/mute, tray actions, single-instance focus and login startup.
- [ ] Coding: same deterministic punctuation/replacements, code-style protections,
  clipboard-only exclusions and terminal newline suppression for matched targets.
- [ ] Privacy/error semantics: no implicit downloads, history off by default,
  recoverable device errors, one sidecar restart and clear persistent failure.

Behavior rows require interaction tests or short screen recordings; still images
cannot prove click-through, hotkeys, injection, sound, autostart or model fallback.
Check them separately and record the result alongside the screenshot evidence.

## May differ

- Native window borders/titlebar, tray/menu chrome, taskbar, file manager and
  OS permission/security prompts. Linux uses WebKitGTK, Windows uses WebView2.
- Font-family resolution (Ubuntu/Segoe UI/system fallback), glyph rasterization,
  antialiasing and small subpixel differences; not spacing or semantic type size.
- OS scrollbar rendering within the shared token dimensions; not unreadable or
  inaccessible controls, arbitrary component padding or platform CSS forks.
- Audio device names/backends (ALSA/PipeWire versus WASAPI), monitor work area,
  screen DPI, X11 window classes versus Windows process names, and data paths.
- Autostart mechanism (.desktop versus HKCU Run), package format and security UI.
- Latency, GPU availability/VRAM, model throughput and system-reserved shortcuts.
  Record measurements separately; never copy Linux timings into Windows rows.

All shared design corrections belong in the existing token/component system;
this checklist is not a request to redesign the app or add platform-specific UI.

## Capture comparison screenshots

1. Build/install the same commit on Linux/X11 and Windows 10/11 x64. Record commit,
   app version, OS version, desktop/window manager, WebKitGTK/WebView2 version,
   monitor resolution, OS scaling/DPI, font family, theme and overlay scale.
2. Use a clean test profile or back up your own config first. Apply identical
   preferences, model and dictionary; disable history and use synthetic, nonprivate
   text. Do not upload personal prompts, paths, account names or microphone audio.
3. Set both desktops to 100% scaling for the baseline. Open Settings from the tray
   at its default logical size. Capture every page in light and dark themes; also
   confirm Auto follows the OS. Repeat at minimum size and a higher DPI such as
   150% to expose clipping, recording the actual scaling on each image pair.
4. Capture empty/error states separately: missing model, no devices, conflicting
   shortcut and sidecar unavailable. Trigger safely in a disposable test profile;
   do not delete working models or kill unrelated processes merely for a picture.
5. Capture the overlay in idle, listening (with bars and a two-line preview),
   transcribing, done and error states. Temporarily disable Idle hide for idle;
   use a screen recording to extract the short-lived states at native resolution.
   Capture both positions and at least baseline/min/max overlay scales. Restore
   normal preferences afterward.
6. Linux: use GNOME Screenshot/Print Screen or the desktop's screenshot tool.
   Windows: use Snipping Tool/Win+Shift+S. Use a delayed capture or recording for
   the overlay so invoking the capture tool does not cancel the recording hotkey.
   Save PNG, include enough background to assess transparency, and do not resize,
   retouch colors or scale only one platform's image to make differences disappear.
7. Name files `<platform>-<theme>-<page-or-state>-<scale>.png`, for example
   `linux-dark-general-100.png`, `windows-dark-general-100.png`, and
   `linux-dark-overlay-listening-100.png`. Put corresponding evidence here or in
   `docs/parity/captures/`, with a small Markdown manifest recording metadata,
   actions, pass/fail observations and any accepted platform differences.
8. Compare pairs side-by-side at 1:1 pixels for equal DPI. For differing DPI,
   compare logical dimensions separately; do not demand pixel-identical glyphs.
   Check component edges, wrapping, semantic colors and overlay positioning.
   Optional image diffs are triage aids, not automatic parity verdicts.
9. Keep small, privacy-reviewed PNGs with the documentation. Large captures and
   video under `captures/` are gitignored; link external evidence only after
   reviewing it for personal information. Add real screenshots to the README only after reviewing them for personal data.
   The current banner is an illustration, not a screenshot.

## Evidence record template

```text
Commit / app version:
Linux OS / desktop / WebKitGTK:
Windows OS / WebView2:
Monitor / resolution / DPI / fonts (each OS):
Theme / window size / overlay scale / position:
Model / device / compute / relevant config:
Screenshot filenames / recording timestamps:
Interaction tests performed and observed results:
Must-match differences (include reproduction steps):
Accepted native differences:
Unverified items / tester:
```
