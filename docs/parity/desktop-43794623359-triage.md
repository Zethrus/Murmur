# H8 native triage: 43794623359

At `7f47b99f20efd900e9cb54884a307433b5d2d9d0`, the H8-only desktop run in
`/tmp/murmur-desktop-43794623359` ended **0 pass, 1 fail, 0 abort**. All 43
guard probes completed. The physical input devices were disconnected and the
preflight passed; this report has no evidence of user input. The owned Notepad
cleanup apparently succeeded. The release gate remains closed.

| Trial | Hook packet text | UI Automation and saved bytes |
| --- | --- | --- |
| Direct Python control | 135 exact characters, paired Unicode down/up | Exact |
| Alt app hotkey | 45 exact characters matching history, paired, no Alt flags or overflow | 44 characters, first difference at index 10 |
| No-Alt app hotkey | 45 exact characters matching history, paired, no Alt flags or overflow | 44 characters, first difference at index 10 |
| Direct Python in the cleared, same owned no-Alt tab | 45 exact characters, paired | Exact |

The same-tab result proves that this Notepad editor could accept the direct
sender after the failed app trial. It does not prove that Murmur's sender caused
the earlier mismatch. The app logs show a 45-character delivery and no
submission error; history is the expected text. Successful `SendInput` and hook
callbacks establish input-stream submission, not the editor's final text.

## Sender comparison

Both paths submit one `SendInput` batch per character: `INPUT_KEYBOARD`,
`wVk=0`, the UTF-16 unit in `wScan`, `KEYEVENTF_UNICODE` down followed by
`KEYEVENTF_UNICODE | KEYEVENTF_KEYUP`, with `time=0` and `dwExtraInfo=0`.
Both request a 12 ms sleep after each character. Rust runs on Murmur's
controller thread after waiting for physical modifiers to release and checks
the selected top-level target before **every** character. Python runs on the
harness thread, checks the guard and modifier state before the loop, and calls
the guard's real-input check before each `SendInput`. The harness's
`AttachThreadInput` is confined to Notepad activation before the trial; neither
sender attaches input queues during delivery. The harness hook thread calls
`TranslateMessage` for its own pump, not for Notepad's queue. Sampled menu
state was false and editor focus was `RichEditD2DPT`; the packet flags contained
no Alt context. These samples do not establish focus at every packet.

The H8 hook timestamps (successive `VK_PACKET` down events) quantify the actual
spacing; a 12 ms requested sleep is not a 12 ms observed interval:

| Trial | Packets | Median interval | Largest interval | Intervals over 18 ms | Median down/up hook timestamp difference |
| --- | ---: | ---: | ---: | ---: | ---: |
| Direct control | 135 | 13.56 ms | 14.71 ms | 0 | 0 ms |
| Alt app | 45 | 14.19 ms | 20.04 ms | 1 | 1.01 ms |
| No-Alt app | 45 | 14.87 ms | 21.51 ms | 7 | 1.01 ms |
| Same-tab direct | 45 | 13.51 ms | 15.41 ms | 0 | 0 ms |

The app-only delay and variability are real, but neither proves a required
typing speed or a defect in the 12 ms sleep. Hook callback timestamps also do
not measure how long the target held a key: Microsoft documents that events in
one [`SendInput` batch](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)
are inserted serially without interspersed keyboard input. The
[`KEYEVENTF_UNICODE` route](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-keybdinput)
depends on the target's `GetMessage`/`PeekMessage` and `TranslateMessage` to
produce `WM_CHAR`. The same documentation says `SendInput` does not reset
existing keyboard state; the release wait and sampled key state found no held
modifier at delivery. Microsoft also documents that
[`AttachThreadInput`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-attachthreadinput)
resets the attached threads' keyboard state, but H8 shows no attach during
delivery.

## Ranked hypotheses and next test

1. **Concurrent H8 focus sampling perturbs Notepad during insertion.** In the
   Alt trial, the first app packet was at `1794797531.991` seconds, after the
   `after ctrl up` phase at `1794797531.977` and before the `before delivery
   wait` phase completed at `1794797532.686`. That phase reads Notepad's UI
   Automation focus, descendants, status label and text. The phase overlaps
   the packet sequence, although the trace cannot place each individual UI
   Automation call within that sequence. In the no-Alt trial, the first packet was at
   `1794797546.722`, between the `after shift up` (`1794797546.704`) and
   `after ctrl up` (`1794797547.058`) snapshots. Direct Python typing has no
   concurrent phase snapshot. This timing makes sampling the clearest
   H8-specific difference, but a read-only UI Automation call has not been
   shown to change Notepad's text.
2. **Murmur's per-character target checks or its controller-thread schedule
   changes target processing.** Those checks call `OpenInputDesktop`,
   `GetGUIThreadInfo`, process identity and integrity APIs between batches;
   Python does not. The measured app intervals are longer and more variable.
   A causal link to the repeated and missing letters is unproven. Removing the
   checks would weaken the selected-target guard and is not justified.
3. **Notepad's post-hotkey state or its packet-to-character processing differs
   from the later direct input.** The same-tab direct success narrows this to
   transient state. The exact hook stream rules out missing submitted packets,
   but not a target queue or `WM_CHAR` processing fault.

The smallest discriminating native comparison is one H8-only run with the
same fail-closed guard, exact hook/history/UI Automation/saved-byte oracles,
hotkey release order, transcript and 12 ms sender interval. In the app trials,
defer only the post-release `_focus_phase` UI Automation snapshots until history
records completion; retain pre-hotkey and settled post-delivery focus snapshots
and hook timestamps. An exact app result would support a sampling interaction
but require a repeat before acceptance; continued corruption would falsify
sampling as a sufficient explanation in that run and focus investigation on
the per-character check/thread and target message processing. Do not change
the production sender, clipboard preference, focus guard or release gate based
on this trace alone.
