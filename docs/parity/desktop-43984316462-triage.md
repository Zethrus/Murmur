# Observed StatusBar drag target

Run 43984316462 at `34d4e2e` stopped before a gesture. Each of four reads
recorded two descendants named `Drag to move` under the verified overlay:

- ToolTip (50022), bounds `(970,999,1059,1024)`, outside the overlay.
- StatusBar (50017), bounds `(938,968,982,1000)`, inside the native overlay
  `(934,964,986,1004)` and matching the DOM pill's reported layout.

This confirms the duplicate tooltip in this run. The status target was rejected
only because the selector required Group. Microsoft identifies 50017 as
[UIA_StatusBarControlTypeId](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-controltype-ids).
O8 now accepts this observed type as well as the documented Group mapping;
exact naming, positive containment, root association, unique selection, two
matching snapshots and guard checks remain mandatory. Mixed valid Group and
StatusBar candidates fail as ambiguous. Ordinary O2 and product behavior are unchanged.

All eight guard probes succeeded, with no missed probe, check abort, or owned
Notepad fixture. The activation candidate has still not been exercised by a
gesture. Regression tests reproduce the captured tooltip/StatusBar pair and
mixed-type ambiguity; both fail before this correction. Native O8, ordinary O2,
focus regressions, full/manual parity and release signature/GPU gates remain open.
