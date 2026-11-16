# Desktop run 43906690485: harness follow-up

The full native report at `/tmp/murmur-desktop-43906690485` tested
`e70106a487cc6b2507df16c8b52a0a062971e45e`. It recorded **44 pass,
3 fail, 1 skipped, 0 aborted checks**. Its run-level outcome was aborted by
Notepad cleanup. The input guard observed 701 healthy probes with no missed
probe or real-input trip. H8's exact delivery, injection focus and privacy
checks passed. The existing CI and manual Windows gates for this HEAD were
reported passing; this follow-up did not rerun them.

## Observed evidence and bounded corrections

| Area | Native evidence | Harness correction or next discriminator |
| --- | --- | --- |
| Retained Notepad tabs | All 10 final activations were refused before any tab was selected or saved. C3 stopped its session before finalization; the outer run stopped the guard and closed the startup lease before cleanup. The foreground cause is unproven. | Finalization now attempts exact-tab cleanup while the guard is still active. A latched guard abort stops further cleanup input. Exact document title, HWND/PID and save verification remain required; no unknown tab or process is closed. Read-only foreground and owned-window snapshots are emitted around C3 stop and before/after cleanup and guard/lease stop. Activation refusal remains a cleanup failure. Compare those snapshots on the next native run before changing activation behavior. |
| C3 | It passed while reporting both first progress values as `0.000`; the post-restart oracle accepted any non-`None` progress. Rounded output could hide a positive increase. | The second Pause must reach Paused, its numeric baseline must persist through restart and stay unchanged while paused, and explicit Resume must produce a greater value or an `Installed`/`Active` badge **and** an installed `speech-small.en` component. Stalled transfer fails. Notes retain raw numeric values. |
| T2 | It skipped after searching the global UIA root when an icon was absent from the overflow flyout. That does not establish a missing product icon. Mute mismatch previously produced only a note. | Discovery checks an already open overflow before clicking the chevron, then searches only the taskbar or overflow subtree. A menu must be newly opened and owned by the tested Murmur process; unrelated global `#32768` menus are ignored. Failure captures scoped UIA names. A menu item or mute result mismatch fails. An icon unavailable to this best-effort UIA check still skips with a reason. |
| O3 | It failed `Notepad lost focus before the overlay click`; the old assertion ran before diagnostic capture. No click was attempted. | Focus evidence is captured immediately after `focus_while_held` and again when Held is visible, before the foreground assertion. The pre/post-click focus oracles remain in place. The next native run must locate the first focus change from those two snapshots. |
| O2 | Native `WindowFromPoint` rooted at the correct overlay; the left button stayed down while the cursor moved 300×200 px. The overlay rectangle did not move and no new drag log appeared. O4 idle drag and F11 recording drag passed. | A native root hit does not prove a DOM hit or handler receipt. The small next discriminator is a test-only WebView2 overlay snapshot at the failed down point: DOM `elementFromPoint`, `.pill` and `.drag-handle` bounds, computed `pointer-events`, `data-idle`/`data-busy`/`data-dragging`, and whether `beginDrag` received `mousedown`. Source currently routes icon mode through `.pill` while CSS hides `.drag-handle` and busy disables pill pointer events. No native adapter or production drag behavior changed here. |
| H4 | Toggle output was `The quick brown fox jumps over the lazy.. `; the required `dog` was absent, so H4 remains failed. | The QA audio worker supplies about 20 ms of source per timed wait, with scheduling and push overhead. Truncation is possible but unproven. The next discriminator is source samples consumed at the second toggle versus the WAV's unpadded speech sample count, with the second-toggle timestamp. Do not lengthen the wait or accept the missing word from this report. |

## Local verification and outstanding native work

`python3 -m unittest scripts.tests.test_desktop_harness -q` passed 201 focused
harness tests. `python3 -m unittest discover -s scripts/tests -p 'test_*.py' -q`
passed 411 script tests. The new cases cover retained tabs through C1/C3,
unknown tabs, changed PID, refused activation, failed save, guard abort,
paused/stalled/advanced/completed C3 outcomes, closed/open overflow, unrelated
menus, missing icon and false mute result. These are Linux mock and pure tests;
they do not establish native Windows behavior.

The next native run requires a renewed Edison readiness check and authorization
from the run owner. No native run was performed here. Full manual 150% DPI,
mixed DPI, Narrator and visual parity evidence remains pending. Release
acceptance remains closed until the native failures and cleanup are resolved.
