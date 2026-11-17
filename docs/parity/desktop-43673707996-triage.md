# Focused Windows desktop report 43673707996

At `d45646f1677c7e334007eace97ad15ae97b7fd8f`, the focused run recorded one pass and five
failures. Its artifacts are under `/tmp/murmur-desktop-43673707996` on the test host. The input
guard observed 209 liveness probes; this is evidence that observation stayed live, not proof that
the reserved-key probes cannot affect Notepad's input path.

| Check | Observed result | Limit |
| --- | --- | --- |
| F13 | Ctrl+Alt held through transcription refused delivery, retained the full transcript, and recovery by deliberate typing and paste passed. | This does not establish delivery into Notepad. |
| H1, I1 | Same-utterance history and Notepad UI Automation text differ. Screenshots show missing and repeated letters, with Notepad displaying `Formatted`. | A successful `SendInput` submission does not establish that Notepad processed every `VK_PACKET` as a character. These trials have no saved-file readback. |
| I4, I5 | The Notepad documents are empty; UI Automation focus is `MenuItem 'File'`. I4's screenshot shows the File menu keyboard cues and `Plain text` status. | The exact point at which focus moved in these trials was not sampled. |
| H8 | Alt-up moved focus from the editor to Notepad's File menu. The app held/refused the transcript and history had zero outputs. The check stopped before the non-Alt trial. | The hold is a failure/no-delivery observation, not an exact-delivery pass; this run provides no non-Alt comparison. |

The opt-in H8 diagnostic now starts with a fresh harness-owned Notepad document. It sends a fixed
fixture directly with the same per-character Unicode event shape and 12 ms interval as the app,
while the real-input guard and its reserved-key liveness probe stay active. Three fixture copies
span more than one guard interval; the probe count during input is recorded. No hotkey or inference
is involved in this control. H8 then runs Alt and non-Alt app trials separately. A zero-output
Alt hold is recorded as `held_refused` when the overlay says held, and the non-Alt trial still runs.
Every trial retains the exact history oracle when an app output exists, captures UI Automation and
native focus and Notepad's mode label, takes a pre-save screenshot, and copies the exact saved file
bytes from the disposable document into the report. Save happens only after the delivery observation.

Interpret the next native result by comparing the control's UI Automation and saved file with the
Alt and non-Alt results. A corrupt direct control implicates the target/input/guard environment
without app inference; an exact control with corrupt app delivery narrows the investigation to the
app's timing, focus, or input path. The control cannot alone prove which of those mechanisms is
responsible. Native `GetGUIThreadInfo` is observation only and can return transient or absent focus
handles. Neither probe suppression, synthetic modifier release, Escape, nor refocus is used to hide
the menu transition. The desktop gate remains closed until exact native insertion and focus checks
pass on a built commit.

Windows documents the [`VK_PACKET` to `WM_CHAR` path](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-keybdinput)
and the [limitations of `GetGUIThreadInfo`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getguithreadinfo).
