# H9 native delivery comparison: run 45264782573

Exact build: `24fb38d39edcd54cd649eaf026bc5a3d849367d1`, from manual Windows
run 45259981415. Artifacts are retained at `/tmp/murmur-desktop-45264782573`.
All seven trials ran. The initial owner-assisted focus handoff succeeded after
60 seconds idle. All 100 guard probes succeeded, with no missed probe or unhook
error. H9 failed its delivery/focus assertions; final cleanup also failed.

## Trial evidence

| Trial | Observed result |
| --- | --- |
| Direct Unicode | 44 characters exact in packet trace, UIA and saved file; Plain text editor |
| Clipboard-only Alt | One exact history/clipboard output, no delivery keyboard events, editor remains empty; focus changes from editor to File menu, so saving is refused |
| Clipboard-only non-Alt | One exact history/clipboard output, no delivery keyboard events, empty editor/file, editor focus preserved |
| Type Alt | One 44-character output, exact paired Unicode packets with no Alt flags; 35 corrupt visible characters, Plain text changes to Formatted, save does not settle |
| Type non-Alt | 44 characters exact in history, packets, UIA and saved file; Plain text editor, clipboard sentinel unchanged |
| Paste Alt | One 44-character output, valid Ctrl+V trace and restored clipboard; editor remains empty and File menu has focus, saving refused |
| Paste non-Alt | 44 characters exact in history, UIA and saved file; valid Ctrl+V trace, restored clipboard and editor focus |

These are four fully successful diagnostic trials and three failures, not seven
ordinary acceptance checks. The original H1/I1/I4/I5 checks remain outstanding.
All six app trials recorded exactly one output. The clipboard-only Alt trial
demonstrates the focus transition without either Unicode or paste delivery.
Native GUI menu flags remained false even when UIA identified Notepad's File
menu; absence of those flags does not establish editor focus in this application.

## Cleanup evidence

Cleanup closed paste-no-alt, paste-alt and type-no-alt. Saving the type-alt
fixture then failed. The new read-only screenshot and focus artifact establish
the blocker: Notepad displayed **Save as Markdown to keep formatting**, with
**Save as Markdown file**, **Save as text file**, and **Cancel** choices. The
owned `.txt` fixture remained dirty and its file was empty. This is direct
evidence of a formatting-save prompt, rather than merely an inferred tab-search
or foreground failure.

The harness left that prompt and the three remaining fixtures in the same
window untouched. It recorded their cleanup as skipped failures, not success,
and did not dismiss the prompt or close unrelated restored tabs. Four run-owned
fixtures remain unretired; Edison was released to its owner after the run stopped.

## Interpretation and next boundary

The observed failure is Alt-dependent on this build and desktop. The same
registered Alt gesture causes a File-menu focus transition even when app keyboard
delivery is disabled. Non-Alt type and paste both succeed. This narrows the next
production investigation to registered-hotkey/modifier interaction with the target;
it does not establish the precise Windows message mechanism or justify suppressing
arbitrary keys, synthesizing modifier releases, or refocusing/dismissing menus.

Correct packet submission alone cannot prove correct target processing. Switching
to paste would not repair the observed Alt case. Changing the default shortcut
alone would leave existing configured Alt shortcuts unresolved. The Markdown
prompt explains cleanup failure but does not explain the initial focus transition.

A bounded source review is evaluating the smallest compatible repair to the
registered-hotkey path while preserving Linux behavior, configured shortcuts and
the original keyboard target. No production patch or new desktop dispatch follows
from this note alone. A candidate must retain release-order, held-modifier refusal,
exact-output and guard tests, then pass the matching-build native comparison and
ordinary delivery checks before repeated/full/manual release gates can advance.

## Prepared follow-up, not yet natively validated

The source review identified a concrete containment gap: Windows focus identity
currently tracks the foreground HWND and process lifetime but omits the focused
child. The same-window editor-to-File transition can therefore pass the delivery
comparison. Windows keyboard-target identity and capture at accepted activation
are being implemented separately; they must not be presented as an Alt-menu fix
or make held text count as successful insertion.

H9's clipboard-only trials now record read-only native foreground/focus snapshots
at the existing chord phases, including immediately after each key release. No
UIA lookup occurs during those phases. The type/paste trials retain timestamp-only
phases until output settles. These snapshots can locate the first observed child
focus change relative to the chord, but cannot prove the exact target message path
or rule out timing effects. They do not cancel menus, refocus, change release order,
or add another trial/retry. Read errors stop further trial input and retain evidence.

Three regressions cover changing native focus, failed observation and preservation
of deferred reads in typing trials. The 15 H9 tests and 527-script suite pass
locally; compilation and diff checks pass. No new Windows run was dispatched.
