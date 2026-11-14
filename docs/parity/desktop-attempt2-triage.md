# Windows desktop attempt 2 triage

Build `856cab49950a1604e472afcb7af7763735134f41`, native run 43632782587:
38 pass, 8 fail, 1 skipped, 0 aborted. Evidence is in
`/tmp/murmur-desktop-43632782587-attempt2/`. The passing P4, I3, H2, F13,
and C1 results are retained; no claim below changes their oracles.

| Check | Observed evidence | Assessment | Repair / next discriminator |
| --- | --- | --- | --- |
| O2 | `O2-drag-diagnostic.txt`: icon bounds `(934,964,986,1004)` before and after a drag from `(960,984)`; cursor reached `(660,784)`, no drag error log. O4 later passed an icon drag. | **Medium:** the failed gesture did not engage; the successful O4 gesture rules out a blanket inability to drag. The diagnostics do not show whether the initial mouse down reached the DOM or `start_dragging`. | Use the UIA `Drag to move` bounds for the start point, record movement while the mouse is held, and retain the persistence assertion. If it still fails, capture hit target and native drag invocation/result before changing production drag behavior. |
| O3 | `O3-focus-diagnostic.txt`: Notepad remained foreground, but UIA focus was `MenuItem 'File'` and the menu was open both before and after the probe `x`; `start` remained unchanged. | **High:** this was a keyboard-focus failure, not a foreground-window switch. **Low:** whether synthetic Alt release, the overlay click, or a readback side effect first moved focus; there is no snapshot between hotkey release and click. | Notepad readback no longer clicks or sends Ctrl+S on UIA failure. A native rerun must sample editor/menu focus immediately after hotkey release and immediately after the overlay click. |
| H1 | `H1-notepad.png` and focus diagnostic: four characters (`the `) reached Notepad; the formatting menu was open with UIA focus `ListItem 'Title'`. | **High:** text was partially delivered into the wrong keyboard state. **Medium:** an Alt/menu accelerator is involved; the report cannot attribute it to Murmur injection or harness hotkey synthesis. | Keep exact same-utterance readback and the explicit menu assertion. The readback repair removes one source of synthetic input during delivery. |
| I1 | `I1-notepad.png`: empty editor, formatting menu open, UIA focus `ListItem 'Title'`. | **High:** focus was outside the editor at assertion time. **Low:** which earlier event opened that menu. | Same readback repair; rerun with a focus transition sample around the hotkey. |
| I4 | `I4-notepad.png`: empty editor and File menu keytips; focus diagnostic says `MenuItem 'File'`, menu open. | **High:** paste did not land in the editor. **Low:** whether Alt/menu focus preceded the paste or was affected by readback. | Readback is now input-free; clipboard restoration and exact text assertions remain. |
| I5 | `I5-notepad.png`: empty editor and File menu keytips; focus diagnostic says `MenuItem 'File'`, menu open. | **High:** Unicode output did not land in the editor. **Low:** the initiating event. | Same readback repair; replacement and exact text assertions remain. |
| F11 | `F11-drag-diagnostic.txt`: origin `(688,904)`, later position `(888,938)`, but engagement was `None` and the full transcript arrived **before** mouse release. The overlay also changes size around delivery, so the later position alone cannot prove a drag. | **High:** the drag-release safety path was not exercised. **Medium:** the guessed `(left+14, vertical center)` point missed the rendered handle; its actual bounds were not recorded. | Start at the UIA `Drag to move` bounds. Require movement while the button is held and no early delivery; a post-release shift remains insufficient. |
| M1 | Murmur displayed the desktop-apps microphone explanation in `M1-desktop-apps-off.png`. `M1-windows-settings-names.txt` is `[]`; the screenshot shows a blank/dark Settings window behind Murmur. | **High:** Windows Settings opened. **Low:** whether the Microphone route loaded; an empty UIA tree cannot prove the wrong page. | Poll for `Microphone` in that window's UIA tree, capture after the content appears or the bounded wait ends, and close a newly opened window on either outcome. No change to the `ms-settings:privacy-microphone` URI. |
| T2 (skipped) | Report says Murmur's icon was hidden from both tray and overflow flyout. | **High:** left-click behavior was not exercised; this is a visibility limitation of the run, not a pass. | Keep the skip. A later native run needs a visible tray icon to test T2. |

The same report shows the harness generated 853 input probes with no guard abort.
Notepad checks use the harness's synthetic `Ctrl+Alt+Shift+F9` chord. The report
does not capture focus immediately after releasing its Alt modifier, so an
Alt/menu interaction is a plausible hypothesis, not an established app defect.
Murmur's Windows injection waits for modifier release without synthesizing
key-ups, and F13 passed the retrigger case; this repair leaves that path alone.
The native desktop gate still requires another run to establish the outcome of
these changes.
