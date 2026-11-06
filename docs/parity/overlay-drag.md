# Overlay drag validation

Implementation baseline: `0fafe0b94c3bd7a2e1c64d62e5671bb0e7df7312` plus the
overlay-drag changes in this worktree, November 6, 2026. Linux is the current
reproduction target; Windows native validation follows when its build is available.

## Causes and changes

- The Linux shadow workaround assigned `GDK_WINDOW_TYPE_HINT_NOTIFICATION`.
  GNOME/Mutter 46 treats notifications as nonmovable. The overlay now retains its
  normal managed window type, with focus disabled, always-on-top and skip-taskbar
  properties intact. See [Mutter's window policy](https://github.com/GNOME/mutter/blob/46.0/src/core/window.c)
  and [X11 move handling](https://github.com/GNOME/mutter/blob/46.0/src/x11/window-x11.c).
- WebKitGTK's pointer-event sequence did not reliably recover after the compositor
  consumed release: the first drag worked, the next did not. The dedicated handle
  now uses primary `mousedown`, prevents its default selection action, and has a
  24 × 24 CSS-pixel target at normal scale. Suggestion text and buttons remain
  separate from the handle.
- Native gesture ownership replaces the old 400 ms programmatic-move exclusion
  and 300 ms inactivity-based save. A stationary held mouse does not end a drag.
  Linux reads GDK button state; Windows reads the logical primary button through
  `GetAsyncKeyState` and the swapped-button setting. Polling runs on the UI thread;
  no runtime mutex is held across native window calls.
- The backend reads final native geometry after release and 150 ms of settling.
  Resizing and ordinary repositioning defer during a drag. Live placement remains
  authoritative before the config save, and placement-only saves no longer reapply
  window placement or rebind shortcuts. Failed disk writes retry. Explicit presets,
  reset and disable invalidate pending placement; cancelled native moves retain
  ownership until release.
- Automatic placement saves defer when disk configuration differs from the live
  configuration, allowing a queued reload to apply manual TOML edits first.
  Invalid TOML is retained and reported rather than replaced by an automatic save.
  This protects edits already on disk at the freshness check; it does not provide
  a filesystem transaction with an external editor writing simultaneously.
- GTK's non-resizable window also needed its default size and size request updated;
  `resize()` alone left an oversized native rectangle. The window remains
  non-resizable. Text delivery waits for an active drag to finish before enabling
  click-through and injecting text. Drag watchdog timeout is five minutes.

## Automated checks

Observed on November 6, 2026:

| Check | Result |
| --- | --- |
| UI tests | 36 passed |
| Frontend build, TypeScript, token and protocol checks | Passed |
| Rust default features | 188 passed, 2 explicitly ignored |
| Rust without default features | 188 passed, 1 explicitly ignored |
| Windows GNU cross-check, without default features | Passed |
| Native GNOME/X11 sequence described below | Passed |

Local run logs and native evidence are in `.omx/evidence/overlay-drag/` (ignored
workspace artifacts). Ignored Rust tests were not executed.

PR review follow-up: the targeted Rust regression
`placement_save_preserves_external_edit_until_reload` passes. It verifies that a
pending external edit survives a placement-save attempt, then both the edited
setting and placement persist after reload, and incomplete TOML is not overwritten.

Run from the repository root:

```sh
npm ci --no-audit --no-fund
npm run test:ui
npm run build
xvfb-run -a cargo test --manifest-path src-tauri/Cargo.toml
xvfb-run -a cargo test --manifest-path src-tauri/Cargo.toml --no-default-features
```

The UI suite includes consecutive gestures, primary-button and busy-state guards,
error recovery, deferred hiding, and suggestion interactions. Rust coverage includes
release/settling, late motion, immediate movement, timeout, save retries, stale
acknowledgements, preset cancellation, placement math, non-resizable GTK sizing,
native X11 input shape and injection acknowledgement ordering.

Windows compilation was checked using the installed GNU target:

```sh
TAURI_CONFIG='{"bundle":{"externalBin":[]}}' cargo check \
  --manifest-path src-tauri/Cargo.toml --target x86_64-pc-windows-gnu \
  --release --no-default-features
```

This skips sidecar packaging and does **not** validate Windows interaction,
the Windows package, or the default Enhance backend on Windows.

## Native Linux test

Requires GNOME Shell, Xvfb, xauth, dbus-run-session, xdotool, xprop and system
Python 3.11+ with PyGObject/GTK 3. The script starts its own X server, session bus,
GNOME Shell and temporary XDG profile. It does not operate on the user's desktop
or Murmur settings. It selects client windows rather than Mutter decoration
windows when checking focus.

```sh
npm run build
cargo build --manifest-path src-tauri/Cargo.toml --bin murmur --features custom-protocol
/usr/bin/python3 scripts/test-overlay-drag.py src-tauri/target/debug/murmur \
  --evidence .omx/evidence/overlay-drag/native
```

The test uses real pointer input through the WebKitGTK handle and GNOME's native
move operation. It checks 20 consecutive moves including work-area edges, a
1.2-second stationary held button, retained target focus, deferred scale changes,
hide/show and restart persistence, and a preset selected during a held drag.
Final geometry tolerance is two physical pixels. Evidence contains environment,
executable SHA-256, native window properties, per-drag positions, and logs.

## Remaining release gates

The isolated desktop test uses an executable with embedded frontend assets and
default features on Ubuntu 24.04 / GNOME 46 / X11, one 100%-scale monitor. It is
not a packaged release or an end-to-end dictation test. Before release validation
is declared complete, record these independently:

- The actual Linux package on the target desktop, including shadow appearance,
  listening/transcribing/Enhance content changes, actual text delivery, and
  subsequent dragging after delivery.
- Real multiple monitors, negative origins, mixed 100%/150% DPI, monitor removal
  and reconnect. Pure geometry tests cover these calculations; physical monitor
  interaction remains unverified.
- Windows packaged builds with WebView2 on each supported Windows version:
  consecutive drags, held pauses, release, swapped primary buttons, focus,
  click-through delivery, edge placement, DPI changes, reset and restart.
- Native Wayland positioning/restoration remains outside the validated scope.

Do not mark Windows or complete cross-platform parity passed from Linux results
or a cross-compilation check.
