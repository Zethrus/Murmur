# O8 gesture-time geometry readiness after run 43966696677

The exact-build Windows O8 run tested `bfe131f7a649a170cd04497bf01ede7210330ee9`
from build run 43960871643. It failed before the first gesture: UI Automation
reported drag-handle bounds `(970, 999, 1059, 1024)` while the native overlay
was `(934, 964, 986, 1004)`. The O8 diagnostic has no case rows. Its stage
4/5/6 launch DOM probes were present, but they predated this UIA read and do
not prove gesture-time geometry. The report records five healthy guard probes,
no abort, no owned Notepad tabs to clean up, and no drag observation. Evidence:
`/tmp/murmur-desktop-43966696677/desktop-report.json` and
`/tmp/murmur-desktop-43966696677/O8-foreground-drag-diagnostic.json`.

This harness-only change makes O8 reacquire the UIA handle bounds in at most
four guarded attempts, with the existing 0.5-second UIA find timeout on each
attempt. Two consecutive identical, usable UIA/native geometry snapshots are
required. Every potentially blocking UIA read is followed by guard, original
process/PID, overlay HWND, foreground root/PID, and input-desktop checks.
Native bounds changing during a read fail immediately. Before input, O8
rechecks native bounds and the hit window's overlay root, then uses the
existing full 300 × 200 work-area and held-movement assertions. Mismatch,
missing UIA handle, and failure details remain in each case's readiness
geometry history. O8 no longer schedules an unconditional mouse-up on a
pre-gesture failure; its local `finally` still releases after a drag attempt.
O2 and its ordinary release oracle are unchanged.

Local validation: all 232 desktop harness tests pass, including transient
mismatch/missing-handle recovery, permanent mismatch with no drag or mouse-up,
guard interruption during UIA, and native geometry, owner, process,
foreground, root, and desktop changes before input. These are mocked Linux
tests. The new readiness path has **not** run on Windows or delivered a gesture;
the `WM_ACTIVATE` hand-back candidate in `bfe131f7` remains untested by O8.
The observations do not establish a root cause for the mismatch or for earlier
foreground-dependent drag failures. A UIA COM call may outlast its polling
timeout; the attempt count bounds retries, not COM wall time.

Next native validation requires a new exact-SHA build and guarded `--only O8
--repeat 1`, followed by unchanged O2 and O3/O4 focus checks if O8 reaches the
gestures. The full 48-check desktop run, manual DPI/Narrator and visual parity,
signature, and GPU gates remain open. Release signing still needs owner
approval; the previously authorized unsigned Authenticode installer does not
authorize primary-key retrieval.
