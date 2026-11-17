# Windows desktop gate: run 43625722198 attempt 2

Build `68c58635a44cc00292a586781076adcfaa133afd`, Edison, November 14, 2026.
The native report at `/tmp/murmur-desktop-43625722198-attempt2/desktop-report.json`
records **33 pass, 13 fail, 1 skipped, 0 aborted**. The 970 guard probes were
healthy, F12 found no transcript in 369 log lines, and the previous exact-head
Windows build passed. Those facts do not establish a passing desktop gate.
This repair pass has not been run on Windows. The desktop report and images are
the evidence; the entries below distinguish observations from hypotheses.

| IDs | Native evidence and confidence | Repair or remaining native proof |
| --- | --- | --- |
| P4 | **High confidence product UI race.** The Verify callback writes `Files verified.` into its row after the backend emits `components/changed`; the page replaces rows on that event. The report says Tiny had no success note, but contains no P4 screenshot or verdict. | Keep each verified version's note in page state and rerender after the Verify reply. UI regression covers a refresh before the reply and a mismatch after success. P4 now captures row names, verification log lines and a screenshot if it still fails. Native pass must show both scoped notes and actual component verdicts. |
| O2, F11 | **Unresolved.** O2 before/after images show the same 52×40 idle icon and identical window bounds. F11 also saw no movement while the mouse was held, so its release barrier was never exercised. Neither image proves that the DOM handle received mouse down or that the native drag command started. | Retain movement and release assertions. New diagnostics record start/end coordinates, foreground, overlay names and drag/delivery log lines. Inspect those plus native movement before changing the app. |
| O3 | **Unresolved focus boundary.** The overlay click left the Notepad document at `start`; the following `x` was absent. The report has no focused-element evidence. Top-level foreground alone does not prove editor focus. | Record UIA focused element and foreground before and after typing. A click-based Notepad focus now requires the focused element to belong to the document. Native rerun must show whether the overlay moves keyboard focus despite the unchanged top-level HWND. |
| H1, I1, I4, I5 | **High confidence that insertion was not observed; cause unresolved.** H1/I1 screenshots show only suffixes in Notepad and the style menu open. I4/I5 show an empty document with Alt keytips visible. This could be editor focus or a menu taking input after the hotkey, not merely a TextPattern misread; the status bar agrees with the short/empty text. | Require real editor focus before clicks and save focused-element, foreground, menu and file evidence after each attempt. Keep exact per-utterance comparisons. A native rerun must show the focus transition and key destination before an injection change is justified. |
| H2 | **Medium confidence delivery loss, cause unresolved.** TargetApp read `ck brown fox…` rather than the full recorded output with modifiers held after D-up. No target key log accompanied this failure. | Save the target state and key events for each release order. Native rerun must compare key stream, widget text, first-key time and modifier state against the exact output. No modifier wait was lengthened. |
| I3 | **High confidence test oracle error.** `I3-terminal.png` visibly shows one sentence on one PowerShell prompt. UIA appended screen-cell spaces, which the exact oracle treated as a duplicate. | Compare the exact observed output as a prefix and require every remaining screen cell to be blank; retain the prompt-count assertion. Unit tests reject truncated or repeated nonblank text. Native readback still required. |
| F13 | **High confidence direct-probe target mismatch.** Timing log records 43 character-producing TargetApp key events and a Murmur delivery to `pythonw.exe` while Tk's widget text stayed empty under held Ctrl+Alt. The earlier probe inferred that typing never started from widget text alone. | The direct probe now requires the exact character event stream and checks for a new listening state after typing begins. The later Insert here type/paste checks still require exact target text and no retrigger. Native rerun must confirm event chars and all phases; the 600 ms app modifier wait is unchanged. |
| M1 | **Medium confidence selector issue.** `M1-desktop-apps-off.png` proves Murmur displayed the Windows switch and the button. The check required a *new* `ApplicationFrameWindow`, so a reused Settings window could fail; there is no Windows Settings screenshot. | Accept an existing Settings window only if it becomes foreground, verify its Microphone page by UIA name, preserve preexisting windows, and capture candidate/feedback diagnostics on failure. Native rerun determines whether ShellExecute actually opened the page. |
| C1 | **High confidence test timing defect, product behavior unproved.** `C1-remove-engine.png` shows Not installed and the missing-engine Settings notice. Overlay names then show `Listening` after the harness pressed and held the hotkey for ten seconds. Hold mode may remain Listening until key-up. | Use the same ten-second attempt budget with an early key-up, then observe the missing-engine message; capture engine log lines if it remains absent. Native rerun must show the overlay's post-release result. |

`T2` remains skipped because the tray icon was absent from the overflow flyout.
The 33 other passes remain as reported; this pass does not reinterpret them.

## Local validation

- `python3 -m unittest discover -s scripts/tests -p 'test_*.py' -q`: 312 passed.
- `node --experimental-vm-modules --test scripts/tests/ui.test.mjs`: 61 passed.
- `npm run build`: passed (`tsc`, token/protocol checks, Vite).
- `git diff --check`: passed.

No Windows desktop run or Rust change was made in this pass. P4, I3, F13, M1
and C1 need native confirmation of the targeted fixes. O2/F11, O3, the Notepad
cases, and H2 remain open for the evidence specified above. The release gate
remains closed.
