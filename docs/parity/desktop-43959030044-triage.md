# Foreground-dependent idle drag: bounded candidate correction

O8 run 43959030044 tested `bb46efdb65dfea1ba9c7f2b4d1d53c59ef535f2b`.
The same Murmur process failed both gestures with its Settings HWND foreground
(first hidden, later visible), and passed both gestures with owned Notepad
foreground. Successful gestures moved the full 300 × 200 pixels while held;
restart restored the final origin exactly. Settings' startup hide wait succeeded.
All 24 guard probes succeeded and the owned Notepad fixture was cleaned up.
Failed gestures had no observed DOM mouse-down or native drag entry. These
observations localize the failure but do not record a transient activation sequence.

The reviewed candidate consumes an active `WM_ACTIVATE` after the existing
foreground hand-back attempt. Previously that branch still forwarded to default
processing. [Microsoft's contract](https://learn.microsoft.com/en-us/windows/win32/inputdev/wm-activate)
requires zero for a handled message and documents default keyboard-focus assignment
when activating a non-minimized window. The candidate leaves inactive/invalid-target
activation forwarding, mouse replies, window styles and subclass ownership intact.
It does not change Linux behavior, drag geometry, guard deadlines or test assertions.

A dispatch regression failed against the previous fall-through and passes with
the terminal return. All seven no-activate tests and Windows GNU compilation with
`--no-default-features` passed. These are not native acceptance evidence.

Next: exact-build O8 once, then unchanged O2 and O3/O4 focus regressions, including
keyboard focus as well as foreground ownership. Full desktop, manual parity,
release signatures and GPU gates remain outstanding. If O2 still fails, capture
one bounded activation/hand-back/nested-focus sequence on already-owned overlay
HWNDs; absence of activation entry falsifies this candidate explanation for that
gesture. Do not extend this into blind rebuilds or foreground workarounds.
