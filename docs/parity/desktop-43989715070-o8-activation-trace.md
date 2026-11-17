# O8 activation/focus contingency after run 43989715070

The exact-build Windows O8 run tested `0a81d95d167f77f7debb72d299b8a7d5cebd52ae` from
the manual Windows build 43985396850. Its artifact is
`/tmp/murmur-desktop-43989715070/O8-foreground-drag-diagnostic.json`. All four
gestures used a fresh, unique `Drag to move` StatusBar (UIA type 50017) inside
the native overlay; the same-named ToolTip was rejected. The cold hidden-Settings
and later visible-Settings gestures failed to move, with no stage 1/2 DOM probe
or native drag entry in either gesture's log delta. Both Notepad-foreground
gestures moved 300 × 200 pixels while held and retained the same foreground
owner and keyboard focus. The successful origin was restored at restart with
0 px difference. The report records 26 healthy guard probes and successful
cleanup. These are observations, not evidence of an activation sequence.

The production `bfe131f` candidate, which consumes an active `WM_ACTIVATE`
after the foreground hand-back attempt, did not resolve the Settings failures.
The next QA build adds one bounded observation at the existing, owned overlay
subclass callback. With `MURMUR_QA_WAV`, it records `WM_ACTIVATE` and nested
`WM_SETFOCUS`/`WM_KILLFOCUS` entry and exit, plus the actual hand-back return
value. IDs and parent IDs show reentrancy; only HWND, thread, foreground/focus
handles, message state, result, and success are logged. It reads no titles,
text, or transcript, and does not subclass renderer windows. The limit is 128
messages, at most three lines per message plus one dropped-record line. The
arm line proves installation; a `cap_reached` line prevents interpreting a
later zero-entry gesture as absence. O8 stores per-gesture trace lines and an
armed/capped/activation-entry summary without changing the movement oracle.

On a new exact-SHA build, run guarded `--only O8 --repeat 1` once. If a failed
gesture has an armed, uncapped, valid paired focus trace but no `WM_ACTIVATE` entry, activation
hand-back is falsified as its explanation at this subclass boundary. Stop this
branch rather than widening instrumentation or guessing a product repair.
An entry and its nested focus/hand-back result can instead be reviewed as
the one planned discriminator; it cannot by itself prove a renderer-side cause.
This local change has no native acceptance result yet.

Review found three diagnostic defects, corrected before native dispatch: the
reserved tracing field `message` is now `native_message` with display formatting;
focus peers come from `wParam` while activation peers come from `lParam`, and
activation state is omitted for focus messages. O8 validates exact fields,
unique IDs, thread/parent nesting, hand-back correlation and matched exits.
Empty, malformed, truncated, unknown-phase or unmatched captures are inconclusive,
even when an arm marker exists. A cap reached before the gesture also prevents
absence inference. This stricter interpretation does not guarantee trace delivery.

The actual installed Rust formatter output was fed through the Python parser:
activation was counted and prohibited absence; complete focus-only records were
accepted. All 13 focused Rust tests and 30 O8 tests passed locally. Native message
delivery and the original drag failure remain unresolved.

Ordinary O2 and its assertions remain unchanged. Native acceptance still needs
ordinary O2, the full 48 checks, manual DPI/Narrator/visual parity, signature,
and GPU gates. Windows run 43985322971 and Linux run 43985323276 were
intentionally cancelled; final CI must be restored before merge. Release
signing requires owner environment approval. The user authorized an unsigned
Authenticode installer; retrieving the primary key remains prohibited.
Edison is free for user activity; coordinate availability before a native run.
