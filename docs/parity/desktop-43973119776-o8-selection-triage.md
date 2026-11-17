# O8 UIA drag-affordance selection after run 43973119776

## Observed evidence

The exact-build Windows O8 run tested `905f565884ce49413a0f26d31c762b4db24893a1`
from build run 43969884049. All four fresh UIA reads selected bounds
`(970, 999, 1059, 1024)` with center `(1014, 1011)`, outside the unchanged
native icon overlay `(934, 964, 986, 1004)`. O8 stopped before any gesture.
The report has seven healthy guard probes, no missed probe or abort, and no
owned Notepad tabs. The earlier 43966696677 run reported the same UIA bounds.
Evidence: `/tmp/murmur-desktop-43973119776/desktop-report.json` and
`/tmp/murmur-desktop-43973119776/O8-foreground-drag-diagnostic.json`.

The stage-6 launch DOM probe showed a 52 × 40 viewport, a visible pill at
`[4, 4, 48, 36]`, and a pill hit at `(26, 20)`. That corresponds to the native
icon bounds, but the launch probe predates the gesture-time UIA read. Source
shows that icon mode hides the dedicated `.drag-handle` (`role="img"`) and
sets the visible `.pill` (`role="status"`) title to `Drag to move`; icon-mode
mouse down is handled by the pill (`src/overlay/overlay.ts`). The old selector
used the first descendant with that name, regardless of control type. The
prior diagnostic did **not** record that descendant's control type or native
association. A tooltip or other duplicate name is plausible, not confirmed.
WAI-ARIA defines `status` as named from author, and the
[accessible-name rules](https://www.w3.org/TR/accname-1.2/) include HTML
`title` as an author-supplied name; Windows exposure still needs observation.

## Harness correction

Only O8's snapshot selector changes. It enumerates same-named descendants of
the UIA element obtained from the overlay HWND, verifies that the root HWND
matches, and accepts exactly one Group candidate with usable bounds fully
inside the current native overlay. The Group type follows the
[W3C UIA mapping for `role=status`](https://www.w3.org/TR/core-aam-1.2/);
the hidden image role's `50006` type follows
[Microsoft's UIA identifiers](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-controltype-ids).
When a candidate exposes a native HWND, its top-level root must be the
overlay. A zero native HWND is associated only through the verified overlay
UIA subtree. Missing, outside, unreadable, foreign, or ambiguous candidates
stop selection; no point is guessed, clipped, or moved into the window.

Every readiness attempt now records candidates' names, control types, bounds,
native HWND/root, association and rejection reasons, including the selected
candidate when there is one. O8 still requires two consecutive identical
UIA/native snapshots within four guarded attempts. Guard, PID/HWND,
foreground owner, desktop, geometry and hit-root checks remain before input;
full 300 × 200 work-area travel, the permanent 0.5-second input-guard latch,
and O2's ordinary release oracle remain intact. No production drag code
changed.

## Validation and limits

Local Linux mock validation: all 238 desktop harness tests pass. Added cases
cover a tooltip before a valid pill, hidden image, duplicate valid groups,
unknown UIA root, only rejected/outside/foreign candidates with zero input,
guard trip during enumeration, and fresh geometry converging only after two
matching reads. The existing readiness, ownership and geometry regressions
also pass. These tests cannot establish the actual WebView2 UIA mapping or
confirm that this selector will find the pill on Windows. If it does not, the
candidate records should show which type and bounds were exposed. UIA COM
calls may outlast the nominal 0.5-second polling timeout.

The `bfe131f` production `WM_ACTIVATE` hand-back change remains untested by
O8 because the last two Windows runs stopped before gestures. The earlier
`bb46efd` O8 run had two Settings movement failures and two Notepad passes;
this selection change does not explain or repair those results. A new
exact-SHA guarded O8 run is needed before claiming native behavior, followed
by unchanged O2 and focus checks if gestures occur. The full O2 gate, full
48-check desktop run, manual DPI/Narrator and visual parity, signature and
GPU gates remain open. Release signing still needs owner approval; the
previous unsigned Authenticode authorization does not authorize primary-key
retrieval.
