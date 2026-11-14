# Windows desktop gate: attempt 2 triage

Build `95e2bf335a1945ca4a21e116fc672ec6ca9c40bb`, desktop run
`43614418846`, attempt 2, on Edison, November 14, 2026. The native report in
`/tmp/murmur-desktop-43614418846-attempt2/desktop-report.md` records **20 pass,
26 fail, 1 skipped, 0 aborted**. Its JSON, screenshots, and logs are the source
for this triage. The exact-SHA CI and manual run `43611266691` passed; neither
establishes that the full desktop gate passes. No native rerun of the changes
below has occurred. The release gate remains closed.

## Ranked findings

| Priority | IDs | What the evidence establishes | Change or next proof |
| --- | --- | --- | --- |
| 1 — product privacy | F12 | `F12-log-excerpt.txt` includes `enigo::platform::win_impl` at DEBUG formatting `text(text: <full transcript>)`. Murmur's own delivery logs record lengths only. | Block all `enigo` tracing targets at the file subscriber, including when `MURMUR_LOG=debug`; a focused Rust test injects the same target and checks that its payload is absent. Rerun F12 against a fresh binary and log. Existing logs need separate removal by the owner. |
| 2 — focus and delivery safety, unproved | O2, F11, O3 | O2's window bounds did not move. F11 reported delivery while the harness held the mouse, but it did not prove the native drag had engaged. O3's Notepad readback was `P`, while its UI screenshot family shows Notepad menus/keytips interfering with text checks. | O2 now captures before/after overlay images; F11 first requires movement while held. Native evidence must show the drag actually starts before judging its release barrier, and O3 must be repeated with a verified editor target. No product focus contract is declared safe from this attempt. |
| 3 — test target and UIA oracle | H1, I1, I4, I5, I6, O3; I3 | H1 screenshot shows Notepad's style menu open with `Title` selected; I4 shows Alt keytips and an empty document. The generic first-TextPattern lookup can read menu text instead of document text. I3 screenshot visibly has sentence A on the PowerShell prompt, while the harness read the window title. | Notepad now selects a Document/Edit element with TextPattern and focuses its bounds. Terminal now selects a descendant TextPattern containing a PowerShell prompt and compares its prompt-line text with that utterance's output. Repeat natively to distinguish real insertion faults from readback errors. |
| 4 — text oracle, unproved | H2, H3, F1, F2, F4, F6, F7 | The same QA WAV produced both `dog ` and `dog. ` in the first TargetApp goldens and later deliveries. F4/F6 compared a copy with punctuation against a golden without punctuation; `.strip()` only removes whitespace. The tests correctly expose the mismatch but do not prove a delivery defect. | Dictation checks opt into Murmur's existing history only in the disposable QA profile; Settings/Privacy captures retain history off. Each dictation records its history cursor before the hotkey; the harness requires one new app output and compares its exact text with the target or clipboard. Fixture-word checks validate recognition separately. First-run samples remain only for semantic checks. The profile is deleted after the run unless `--keep-work`. Native rerun is still required. |
| 5 — UIA selectors and result lifetime | A1, A2, M1, M3, P4, K1 | A1/A2 selected the title-bar `Minimize` button after the `Start at login` label, then requested an unsupported TogglePattern. M1/M3 screenshots show a visible Refresh control; its row associates it with the `Refresh inputs` label, which may be the UIA name. K1's tab log repeats the same complete cycle twice. P4 invoked both Verify buttons but its final two-notes-at-once assertion failed; the screenshot is scrolled away from the engine row. | Select the exact named toggle with TogglePattern; query `Refresh inputs`; stop K1 after one complete tab traversal. P4 now waits for the verified note in each component row before invoking the next Verify. Rerun to confirm both component verdicts and the Refresh selector; a UI rerender is only a hypothesis for the missing final pair. |
| 6 — isolated product or harness unknowns | H6, F13, C1 | H6 saw no shortcut-conflict error after an externally edited config file; the previous shortcut was not exercised after the assertion. Its predicate also looked for an `error:` state that the overlay helper never returns. F13 saw no typing while Ctrl+Alt stayed held for 20 seconds, so its retrigger claim was never tested. C1 did remove the engine (screenshot shows `Not installed`) but found no full missing-engine overlay text; the image shows a clipped label. | H6 now checks actual accessible names, writes the config atomically like Murmur, records reload/rebind metadata and UIA names, and exercises the previous hotkey even if no conflict error appears. F13 records hotkey edges, overlay states, app milestones and first-key time. C1 saves full accessible-name snapshots. Acceptance is unchanged; native evidence must decide whether product fixes are needed. |

`T2` was skipped because the tray icon was absent from the overflow flyout; it is
not a pass. The remaining 20 passing checks are listed in the native report.
The full report's raw excerpts can include synthetic transcripts and local
paths, so they are kept out of the repository.

## Validation of the first follow-up (`96f7a66`)

- `python3 -m unittest scripts.tests.test_desktop_harness -q`: 92 passed.
- `python3 -m unittest discover -s scripts/tests -p 'test_*.py' -q`: 301 passed.
- `cargo test --manifest-path src-tauri/Cargo.toml --no-default-features platform::logging_tests`: 1 passed (the binary's logging filter).
- `cargo check --manifest-path src-tauri/Cargo.toml --no-default-features --target x86_64-pc-windows-gnu`: passed (compile only).
- `rustfmt --edition 2021 --check src-tauri/src/platform.rs` and `git diff --check`: passed. Repository-wide `cargo fmt --check` reports pre-existing differences in untouched Enhance files.
- Native Windows checks: not run for this follow-up; all 26 original failures remain open until a new attempt establishes their status.

## Validation of this follow-up

- `python3 -m unittest scripts.tests.test_desktop_harness -q`: 98 passed, including exact per-utterance output, fixture fidelity, scoped read-only history and Terminal selector cases.
- `python3 -m unittest discover -s scripts/tests -p 'test_*.py' -q`: 307 passed.
- `cargo check --manifest-path src-tauri/Cargo.toml --no-default-features --target x86_64-pc-windows-gnu -q`: passed with pre-existing unused-item warnings. This is compile-only, not a native desktop run.
- `rustfmt --edition 2021 --check src-tauri/src/controller.rs`, Python `compileall`, and `git diff --check`: passed.
- Native Windows checks have not run. Attempt 2 remains 20 pass, 26 fail, 1 skipped; these edits do not establish a release pass.
