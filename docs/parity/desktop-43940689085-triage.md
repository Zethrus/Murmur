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
sample of this gesture. The O2 failure path now records fresh UIA drag bounds,
native visible/outer/client geometry, DPI and the hit child geometry immediately
before the *same* attempted gesture. The next native result can compare those
values with stage 5 and the actual mouse-down; no retry or changed drag input
can hide the original failure. There is no production overlay change yet.

Local validation: `python3 -m unittest scripts.tests.test_desktop_harness`
passed 211 tests. This host did not run the Windows desktop checks or inspect
the live WebView2 viewport. The release's manual DPI, Narrator, visual, build,
signature and GPU gates remain separate owner-controlled work.
