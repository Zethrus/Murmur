# O9 host-input reproducer run 44041874181

Exact build 44039871465 of `5de04441`. Guarded `--only O9 --repeat 1` completed. Two
earlier attempts stopped before any reproducer input: 44037195020 lost the first guard
probe on the ordinary guard start (fixed by using the passive startup lease), and
44039468949 hit a harness type error (`Context.build` is `murmur.Build`).

## Baseline (`off`) reproduces Murmur

Renderer chain `Chrome_RenderWidgetHostHWND → Chrome_WidgetWin_1 (foreign) →
Chrome_WidgetWin_0 → WRY_WEBVIEW → root`, classified as a foreign immediate parent,
identical to Murmur in run 44030456571.

- Both Notepad-foreground drags moved: DOM `down`, drag engaged, one balanced move loop,
  no hand-back.
- None of the eight Settings-foreground gestures delivered a DOM `down` or started a
  drag. Each one activated the overlay once and handed back to Settings. This is
  Murmur's O8 signature.

## Candidate (`on`): `AllowHostInputProcessing` on the overlay controller only

- The option was reported `enabled` (runtime 157). The hit window is now the in-process
  `Chrome_WidgetWin_0` (chain `Chrome_WidgetWin_0 → WRY_WEBVIEW → root`).
- **Every full-travel Settings-foreground gesture moved while held**: the drag, the
  leave-work-area drag, all three repeats and hide-during-gesture. Each delivered a DOM
  `down`, engaged the drag once and left one balanced move loop.
- Hold-without-moving and rapid click-drag-release engaged without moving the window.
- No case left the button down, an unbalanced move loop, or movement after release, so
  the [WebView2Feedback#5701](https://github.com/MicrosoftEdge/WebView2Feedback/issues/5701)
  release/capture loss did not reproduce here. The foreground owner was kept every time.
- Both Notepad-foreground drags still moved, with no activation.

## Caveat: programmatic activation remains in the Settings case

With Settings in the foreground, the candidate still activates the overlay (`WM_ACTIVATE`
state 1) about three times per gesture, and Murmur's hand-back returns Settings each
time. One sample, in the rapid case, saw Settings' native focus briefly on its root
window rather than its WebView2 child. Wry's `WM_SETFOCUS` → `MoveFocus` then restored
it, and the typed token landed exactly at the caret. External-app foregrounds (Notepad)
showed no activation at all.

## Typing mismatches were a harness artifact

The Settings typing mismatches occurred in **both** modes from the fifth gesture
onwards. Each new token was inserted mid-text (`o9f1o9f2o9f3oo9f59f4`), because the
field was re-clicked at its centre once it held text. It was never a focus failure. The
harness now presses End on the verified field before each gesture.

## Decision

The reproducer passed on mouse delivery, movement and release. The remaining activation
is the plan's "later programmatic activation" case. Next step: a Murmur trial with the
option set only on the `overlay` webview, keeping the hand-back, then O8 and ordinary
O2. If O8 records a keyboard-focus change, evaluate the plan's separately reviewed
thread-local activation veto. Do not loosen the oracle.
