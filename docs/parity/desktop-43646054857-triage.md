# Windows desktop run 43646054857

Build `9993d3f12629a8124d649dcae13e88bc38efe3d3` produced **39 pass, 7 fail,
1 skipped, 0 aborted**. The native artifacts are in
`/tmp/murmur-desktop-43646054857/`; this change has not had a new native run.
The desktop gate remains closed. The earlier manual pass on the same head does
not override the seven failures in this run. T2 remains a visibility skip.

| Check | Native evidence and source finding | Change / native proof still required |
| --- | --- | --- |
| S4 | `S4-harness-error.txt` shows client size `(1000, 780)` both before and after a drag from a hard-coded `GetWindowRect` corner. The screenshot does not establish that this point was a reachable resize grip. | The harness now selects a point that belongs to Settings and answers `HTBOTTOMRIGHT` to `WM_NCHITTEST`. It still performs a mouse drag and rejects no movement or an undersized client. A native rerun must shrink the window, prove the 760 × 560 lower bound, and capture all six pages. If the hit test succeeds but size stays fixed, inspect the mouse events and window sizing messages before changing the configured minimum. |
| O3 | `O3-focus-diagnostic.txt` shows Notepad's `File` menu focused **before** the overlay click. `focus_while_held` called `Notepad.focus(click=False)`, which previously activated Notepad without focusing its document. The click cannot be blamed from this trace. | The no-click path now calls UI Automation `SetFocus` on the editor and verifies editor focus, without a Shift+click. O3 now fails before touching the overlay if editor focus is already lost. Rerun O3 and require editor focus before and after each overlay button and exact `startx`/`startxy` text. If File gains focus only after hotkey release, isolate that release transition. |
| H1, I1, I4, I5 | Every `after-hotkey-focus.txt` records `MenuItem 'File'` immediately after release, before readback. H1/I1 later show Notepad's style dropdown; I4/I5 remain empty. The harness sends Ctrl, Alt, Shift, F9 down; releases F9, then Shift, Alt, Ctrl. The registered F9 shortcut need not reach Notepad. [Windows documents](https://learn.microsoft.com/en-us/windows/win32/inputdev/wm-syskeyup) that default Alt key-up handling may activate the menu. This is a plausible mechanism, not proof that Murmur or Notepad alone caused it. | Preserve the configured Alt shortcut and exact per-utterance oracle. In a native isolated run, sample editor/menu focus after the initial click, while the hotkey is held, immediately after F9 up, immediately after modifier up, and before the first injected character. Compare with a separately configured non-Alt shortcut. Capture `WM_SYSKEYUP`/`SC_KEYMENU` if the transition occurs on Alt release. Do not add an Esc or editor refocus after release: that would alter the target before delivery and hide this failure. |
| F11 | `F11-harness-error.txt` says `Drag to move` is absent while recording. `O1-overlay-listening.png` visibly contains the handle. The DOM marked the handle `role=presentation`, while only the idle pill had the accessible `Drag to move` title that O2 successfully used. | The visible handle now has an accessible name in the recording view. Rerun F11; require movement while the button is held, no target text before mouse-up, and exact delivery only after mouse-up. A post-release position change alone remains insufficient. |

O2's idle drag and restart persistence and M1's Windows Microphone page passed on this
run. The existing input guard observed 838 probes with no abort. No production
injection or focus-policy behavior was changed here; the remaining Notepad menu
transition needs native evidence before a product-side fix can be justified.
