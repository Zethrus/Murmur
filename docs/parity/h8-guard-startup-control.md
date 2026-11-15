# H8 guard startup control (November 15, 2026)

The H8-only desktop run at `908923a31bc373c453ee1fee70239a37335e1fb1`
aborted before its first check (`/tmp/murmur-desktop-43763669497`). The initial
guard probe had `SendInput=1`, two hooks installed, a running message pump and
one received heartbeat, but zero keyboard callbacks and zero observed probes.
Two earlier runs at `ecf7595` had the same startup symptom. This does not prove
why Windows did not deliver the hook callback.

The standalone guard run in the same Windows Python environment
(`/tmp/murmur-guard-43711563701`) observed 30 probes over 30 seconds and both
Unicode hook callbacks. Unlike the suite, it created an owned EDIT window on a
thread that drained window messages, focused it, waited for focus and 60 seconds
of idle desktop time, and passed its handle to the guard's first probe. The
suite had instead shown a topmost Tk countdown in a *child process*, waited for
that process to exit, checked only 10 seconds of idle time, and started the
guard with no owned foreground target. The suite parent had no equivalent
window or window message pump. Foreground ownership, the banner's process
lifecycle, and the extra idle interval are possible explanatory differences;
none is an established root cause.

The next H8 run uses the standalone window and focus/idle gate **after** the
normal banner. `SetForegroundWindow` is tried on the owned window; an owner
click is needed only if Windows denies or does not establish focus. Focus loss,
desktop/session preflight failure, or failure to reach 60 seconds idle aborts
before the guard starts. The normal 0.5-second probe deadline and real-input
guard remain in force. The first probe uses the owned target; after guard
startup, the window closes and restores foreground only if it still owns it.
The same guard observer then runs continuously through H8. The window is never
kept as a surrogate for Notepad and the observer is never stopped and restarted
between startup and the trials.

Read `meta.guard_startup` with `meta.input_guard` in the next
`desktop-report.json`:

| Result | Interpretation |
| --- | --- |
| `phase=focus_and_idle_wait` | No H8 injection occurred. Use `focus_wait_state`, preflight problems, and `probe_window` to identify focus, idle or session refusal. |
| `phase=guard_start`, zero probes | The owned-window startup control did not establish the first liveness probe. Compare `owned_probe_status`, hook callbacks, window-received messages, desktop context and banner timing. This does not identify a Windows root cause by itself. |
| `phase=ready`, H8 artifacts present | The control got H8 past startup with one guard. Compare direct-control injected stream and UI Automation text with exact saved bytes, then Alt and non-Alt app trials. Passing startup alone does not pass H8. |
| Any later guard abort | Treat all remaining checks as aborted. Preserve the timeline and probe count; do not infer insertion acceptance. |

The latest available Alt/non-Alt evidence remains
`/tmp/murmur-desktop-43703710595-attempt2`: the Notepad menu/focus problem is
unresolved. H8 native acceptance requires exact direct and app insertion
evidence, including saved-file readback. No native result from this startup
control is claimed here.
