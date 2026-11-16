# Windows desktop follow-up: build 43936010588, attempt 43940689085

The run used commit `0681df8f19c765e9d24e94a168cbb873781b93ea` on Windows
10.0.26200. Its report records 8 passes, 2 failures (O2 and O3), no skips or
aborts. O4, H4, H8, F11, T1, T2, C1 and C3 passed. The guard recorded 181
probes, and the run's owned Notepad tabs were cleaned up. The screenshots and
diagnostics below are local artifacts in `/tmp/murmur-desktop-43940689085/`.

## O3: focus assertion

`O3-discard-focus-diagnostic.txt` shows foreground `0xb00c2e`, a
`RichEditD2DPT` child whose root is the expected Notepad HWND `0x990b40`.
Both handles belong to live PID 33852; the named fixture is active, UIA reports
the Text editor document, native keyboard focus is the same editor child, and
neither menu mode is active. The check rejected this state solely because it
required `foreground == pad.hwnd` before the overlay click. The test therefore
never clicked Discard or typed its next letter in this attempt.

The O3 check now requires the named owned fixture, live Notepad PID, expected
root and per-handle PID, Default input desktop, native editor class and keyboard
focus, UIA editor focus, and no menu mode. It checks that predicate immediately
before and after the overlay click. It then types the exact next letter without
activation or refocus and checks the resulting text. A child from a foreign
process, another root or tab, a menu, and a secure desktop all fail closed.
This is a harness assertion repair; a native O3 rerun is still required.

## O2: drag entry remains unproven

`O2-drag-before.png` and `O2-drag-after.png` were inspected: both show the
same 52×40 idle icon. `O2-drag-diagnostic.txt` records a start point at
`(960, 984)` inside the overlay's visible rectangle `(934, 964, 986, 1004)`,
an overlay-owned child hit, a physically held button and a 300×200 pointer move.
The overlay did not move. There was no stage-1 DOM mouse-down, native drag entry,
or top-level subclass mouse message. The last is inconclusive for WebView2's
separate renderer process. O4 and F11 drag behavior passed in the same attempt.

The settled-icon stage-5 DOM probe logged pill coordinates `[50, 30, 94, 62]`;
the gesture's native rectangle is only 52×40. This suggests a viewport or
coordinate mismatch, but stage 5 is logged once per mount and is not a fresh
sample of this gesture. The QA-only frontend now reports stage 6 after the
idle icon's native size acknowledgement and a paint frame. Stage 6 samples
`elementFromPoint` at the viewport center and records viewport dimensions,
device pixel ratio and pill bounds. O2 waits for that probe, then records fresh
UIA drag bounds, native visible/outer/client geometry, DPI and the hit child's
client origin immediately before the *same* attempted gesture. Those values
allow the next native result to map the actual screen click into the WebView2
client and compare it with the fresh DOM hit. A false hit remains a test failure;
no retry or changed drag input can hide the original gesture. There is no
production drag or focus behavior change.

Local validation: the desktop harness suite passed 212 tests, the UI suite
passed 64 tests, `npm run build` passed, the targeted Rust QA probe test passed,
and `rustfmt --check --edition 2021 src-tauri/src/ipc.rs` passed. The workspace-wide
Rust format check still reports pre-existing differences in the untouched
`src-tauri/src/enhance/` files. This host did not run the Windows desktop checks
or inspect the live WebView2 viewport. The release's manual DPI, Narrator,
visual, build, signature and GPU gates remain separate owner-controlled work.
