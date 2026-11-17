# O9: WebView2 host-input reproducer

Purpose: test whether WebView2's
[`AllowHostInputProcessing`](https://learn.microsoft.com/en-us/microsoft-edge/webview2/reference/win32/icorewebview2controlleroptions4)
gets the overlay's mouse press through when another window on the same UI thread is in
the foreground, without breaking release, capture or keyboard focus. Batch C of
[windows-release-unblocking-plan.md](windows-release-unblocking-plan.md) selected this
route after [run 44030456571](desktop-44030456571-o8-topology.md) showed that the
renderer's immediate parent is owned by the WebView2 browser process. O9 is diagnostic
only: it can justify or reject a reviewed Murmur trial, and it can never clear O2.

## Reproducer (`tools/webview2-input-repro`)

A standalone Windows binary that is never released. Its crate versions match
`src-tauri/Cargo.lock` (tao 0.37.2, webview2-com 0.39.1, windows 0.62). It hosts two tao
windows on one UI thread the way Murmur and Wry 0.57.2 do:

- **Hosting:** each window gets a `WRY_WEBVIEW` child (`WS_CHILD | WS_CLIPCHILDREN`)
  with a windowed controller, Wry's parent subclass (bounds on `WM_SIZE`, `MoveFocus` on
  `WM_SETFOCUS` only, move notifications), and Wry's environment arguments. The windows
  share one environment.
- **Overlay:** undecorated, transparent, topmost, skip-taskbar and `focusable(false)`,
  with Murmur's no-activate subclass on the root and on the in-thread descendants. That
  subclass answers `WM_MOUSEACTIVATE` with `MA_NOACTIVATE` and hands activation back on
  `WM_ACTIVATE`. A DOM `mousedown` starts tao's native move loop only while the physical
  button is down, which is the same refusal rule as `start_overlay_drag`.
- **Settings stand-in:** a normal window with one text field.
- **The only variable:** `--host-input on` sets `AllowHostInputProcessing(true)` on the
  overlay controller alone. An unsupported runtime or a failed setter stops the
  candidate rather than running as a silent baseline.

Its JSON-lines log records the fixed page messages (`down`, `up`, …), drag start and
refusal, activation and hand-back, `WM_ENTERSIZEMOVE`/`WM_EXITSIZEMOVE`, capture changes
and moves. It records no page text or typed input. The harness can post `WM_APP+1`
(hide, then show again after 1 s) and `WM_APP+2` (return to the start position) to the
overlay root.

## Check (`--only O9 --repeat 1`)

Manual `windows-build.yml` runs build the reproducer (`continue-on-error`) and hand it
over as the optional `webview2-input-repro.exe`. It is hashed, logged and verified like
every other hand-off file, and preflight refuses O9 when the file is missing.

O9 runs the baseline (`off`) and then the candidate (`on`), with the same sequence each
time:

1. Notepad drag.
2. Settings drag.
3. Hold without moving.
4. Rapid click, drag and release.
5. Leaving the work area.
6. Three repeated drags.
7. Hiding the overlay during a gesture.
8. Notepad drag again.

Before each gesture the overlay returns to its start position. Each gesture's input is
released on every path. After each gesture a unique token is typed through O8's verified
typing gate into the preselected Notepad editor or the Settings field.

Interpretation:

- **Inconclusive:** the baseline does not show Murmur's renderer chain, a Notepad pass
  and a Settings failure.
- **Candidate rejected:** in the candidate, a drag fails to move while held, the button
  stays down, a move loop is left unbalanced, the overlay keeps moving after release,
  focus or foreground changes, or a typed token does not land exactly.
- **Candidate passed reproducer:** none of the above. This justifies, at most, a
  separately reviewed Murmur change, which then needs its own O8 run, ordinary O2 and the
  full validation ladder.
