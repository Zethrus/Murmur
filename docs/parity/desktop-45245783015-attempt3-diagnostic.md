# H9 delivery-path diagnostic after desktop run 45245783015 attempt 3

## Observed baseline

The local copy of `desktop-report.json` for run 45245783015 attempt 3 reports commit
`78e60c31bb2c385a45e8d7c74e8845a18797f796`. O3 and O4 passed. H1, H8, I1,
I4 and I5 failed. The guard recorded 229 probes and no user-input abort. Final
cleanup reported seven owned Notepad tabs that it could not close safely.

H8's direct Unicode control delivered and saved all 135 characters exactly.
The Alt `Ctrl+Alt+Shift+F9` trial recorded 45 correctly paired `VK_PACKET`
characters, zero packet Alt flags, and one 45-character history output, while
the settled Notepad UIA read contained only `jumps over the lazy dog.` (25
characters). Its save refused because the active tab no longer named that owned
fixture. The non-Alt `Ctrl+Shift+F9` trial in the same original run delivered
and saved its 45-character output exactly. H1 and I1 contained corrupted text;
I4 and I5 had empty Notepad readback. These are observations, not a proven
cause of the text loss or tab change.

## H9 experiment

H9 is opt-in and requires `--only H9 --repeat 1`. It runs seven trials in a
fixed order, each in a newly named run-owned Notepad document:

| Trial | Input path | Registered hotkey | Expected target/clipboard |
| --- | --- | --- | --- |
| control | Harness Unicode sender, fixed sentence A text | None | Exact UIA and saved bytes; sentinel clipboard |
| clipboard-alt | App `clipboard_only=true` | Ctrl+Alt+Shift+F9 | Empty editor and saved file; exact history and clipboard; no delivery keys |
| clipboard-no-alt | App `clipboard_only=true` | Ctrl+Shift+F9 | Same |
| type-alt | App `method=type` | Ctrl+Alt+Shift+F9 | Exact history, packet trace, UIA and saved bytes; sentinel clipboard |
| type-no-alt | App `method=type` | Ctrl+Shift+F9 | Same |
| paste-alt | App `method=clipboard` | Ctrl+Alt+Shift+F9 | Exact history, UIA and saved bytes; sentinel restored |
| paste-no-alt | App `method=clipboard` | Ctrl+Shift+F9 | Same |

All six app trials use fixture A. The direct control sends the same sentence's
fixed text, but is not an inference from the fixture. `clipboard_only=true`
uses the application's real injection setting, which takes precedence over
the method in `inject::choose_path`. It avoids keyboard delivery while keeping
the registered hotkey, recording and transcription path. It is **not** a raw
unregistered hotkey-only equivalence test.

The existing input guard, passive startup probe lease, 60-second idle check,
Notepad preflight and per-run ownership tracking remain in force. H9 refuses a
non-text clipboard it cannot restore. It samples focus before the gesture and
after the 45-second output wait and settled delivery; it does not poll UIA
during injection. It never refocuses or clears the delivered tab. Ctrl+S is
sent only when the same owned editor still has verified keyboard focus. Missing
history, trace overflow, clipboard mismatch, UIA mismatch and save refusal are
all failures; a later passing trial cannot erase an earlier one. Guard loss,
unsettled delivery or unsafe target creation/focus stops further input.

Parent review compared the parser with the retained native H8 trace. Windows
reported sided Ctrl/Alt/Shift VKs (162/164/160), so the diagnostic normalizes
those codes before checking exact hotkey pairs; extra modifier edges still fail.
Paste requires Ctrl alone held across both V edges, no Unicode packets, and no
modifiers left held. Both parser regressions failed before correction and pass
afterward. Each app trial preserves its launch log before the next restart.
The combined cleanup/H9 patch passes 524 script tests locally; compilation and
diff checks pass. These results do not establish native H9 acceptance.

## Dispatch and limits

After the parent lands this diagnostic on the intended PR31 head and a matching
Windows build is handed over on Edison, dispatch `windows-desktop.yml` on that
exact commit with `sha=<full dispatched commit SHA>`, `only=H9`, `repeat=1`, and
all other diagnostic toggles false. The equivalent direct harness selection is
`python scripts/windows/desktop/run.py --build <verified-build-dir> --out <report-dir> --only H9 --repeat 1`.
The workflow's owner-only dispatch, build verification, preflight and guard
still apply. No H9 native run has been performed for this patch.

An exact clipboard-only result would show that the registered gesture reached
recording/transcription and clipboard output on that trial. A difference
between app typing and app paste would narrow the delivery path. An Alt-only
difference would narrow the modifier-dependent interaction. A failed direct
control would keep the desktop or Notepad target itself in scope. None of these
outcomes alone proves root cause, clears H1/I1/I4/I5, or satisfies a release
gate. The original ordinary checks and cleanup must still pass.
