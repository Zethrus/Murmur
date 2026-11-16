# H8 startup window lease (November 15, 2026)

At `a98b9a8941e5b63e8b81d2c8574ab4132a621055`, the mixed H1-first run
`/tmp/murmur-desktop-43805913809` passed the H8 owned-window first probe, then
aborted H1 before Notepad existed: probe 2 was not observed within the unchanged
0.5-second deadline while Murmur `ensure()` opened and hid Settings. The first
H8-only attempt in `/tmp/murmur-desktop-43803047155` aborted at the same
boundary. Its second attempt completed 36 probes and passed the exact Alt and
non-Alt H8 trials. These are observations, not proof that closing the startup
window caused the missed probes. The guard's permanent failure latch correctly
aborted the failed attempts.

The bounded comparison keeps the same H8-selection startup gate and first
owned-window probe. After that probe, the window's GUI thread acknowledges a
passive transition: it stops automatic `SetFocus`, permanently disables prior
foreground restoration, removes `TOPMOST` without activation, and keeps pumping
messages. The window stays alive across `Context` creation, Murmur `ensure()`,
and target creation. A one-time handoff closes it without restoration only after
a normal `TargetApp` or Notepad focus call verifies the registered HWND, live
owner PID, Default input desktop, foreground and native keyboard destination.
Notepad also requires the active owned document and editor focus. Ownership is
rechecked immediately before close and guard health immediately after. Settings,
an arbitrary foreground window, or an unknown owner cannot authorize handoff.
The lease itself sends no input or focus change.

The run owns cleanup at startup failure, abort, and final exit, including runs
with no target. A failed window destroy or thread join fails closed. The same
guard remains active; no hook reset, extra Unicode target probe, timeout change,
prelaunch, or app closure is introduced. A verified handoff retires the startup
window once. Later target teardown gaps remain outside this correction.

Local desktop-harness unit tests cover the lifecycle and fail-closed decisions.
Native behavior on a Windows desktop is pending. The focused H1-first and
H8-only comparison, then the full desktop suite and all release gates, remain
mandatory before claiming a fix or reopening the release gate. The existing
PR CI and manual results at `a98b9a8` predate this change.
