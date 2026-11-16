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
The H1-first native run at `884c6459d3f6aa660ec46afefb39a97edbde787b`
(`/tmp/murmur-desktop-43811071941`) completed the startup handoff: H1 reached
its Notepad assertion, with all 16 probes seen up to that point. H1 failed its
exact comparison: history had 44 characters, Notepad 43, diverging at character
16. Both focus diagnostics show the Notepad editor and no open menu, but the
check queried Notepad through UI Automation before confirming history output.
The subsequent H8 setup aborted on probe 17 despite a live hook heartbeat; the
guard latched the failure and the remaining checks were aborted. This run shows
that startup handoff worked once, not that the guard is reliable across the
next target-creation boundary or that H1 delivery is correct.

The normal Notepad H1/I1/I4/I5 checks now confirm the same utterance through
the existing history and fixture-fidelity oracle before target UI Automation
readback or focus diagnostics. A missing output or held refusal still fails;
diagnostics are captured after the overlay settles or enters held state. Exact
target comparison, H1 menu assertion, I4 clipboard sentinel, and I5 replacement
assertion remain. If the overlay does not reach a settled or held state, the
check keeps the output failure and records why it skipped Notepad diagnostics.
The focused H1/I1/I4/I5 native comparison with the unchanged
guard, then the full desktop suite and release gates, remain mandatory before
claiming a fix or reopening the release gate. The separate probe-17 boundary
miss remains unresolved.
