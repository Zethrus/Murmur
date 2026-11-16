# H8 native triage: 43780792202

At `ee59fa67b054ed8e898fd8878038648594926184`, the H8-only desktop run in
`/tmp/murmur-desktop-43780792202` completed all trials without an input-guard abort.
All 46 liveness probes and 46 pump heartbeats were received. Keeping the Notepad
fixtures open across Murmur restarts avoided the prior gap in this run; one run does
not establish that fixture lifetime caused the earlier aborts.

| Trial | Hook stream | Document and saved file | Result |
| --- | --- | --- | --- |
| Direct Python control | 135 exact `VK_PACKET` characters; paired down/up; median down interval 13.62 ms | Exact UI Automation and UTF-8 bytes | Passed |
| Alt hotkey | 44 exact characters matching history; paired; median 14.21 ms | Exact UI Automation and saved bytes | Passed |
| No-Alt hotkey | 45 exact characters matching history; paired; no Alt flag or trace overflow; median 14.63 ms | Both contain `The quick brown pppppppps vver the ........ `, 44 characters, diverging at index 16 | Failed |

The no-Alt first packet followed the injected Ctrl key-up by 184 ms. Sampled
foreground HWND, native editor focus (`RichEditD2DPT`), and menu state stayed
consistent before and after delivery. These samples do not prove focus for every
character. The hook shows input *entering* the Windows stream; it does not show
what Notepad took from its queue or translated into `WM_CHAR`. Microsoft documents
that `KEYEVENTF_UNICODE` sends `VK_PACKET` messages to the foreground thread and
that `TranslateMessage` then posts `WM_CHAR`. `SendInput`'s return value counts
events inserted into the input stream, not characters committed by the editor.
See [KEYBDINPUT](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-keybdinput)
and [SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput).

The harness's `AttachThreadInput` call is confined to `win32.activate`, before
the hotkey trial. It does not run in the Rust per-character path. Microsoft notes
that attaching input queues resets their `GetKeyState`/`GetKeyboardState` state;
this run has no evidence that such a reset occurred during delivery. See
[AttachThreadInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-attachthreadinput).
The Rust sender and Python control use the same Unicode down/up event shape and
12 ms requested pause. Rust additionally checks the selected target before each
character. The observed 1 ms median interval difference alone does not justify
raising the delay or changing the user's clipboard preference.

The no-Alt screenshot shows more than eight restored Notepad tabs. The old H8
cleanup searched only eight, and H8 reused a basename across runs. This explains
why it could fail to locate the control tab and made an old restored H8 tab
ambiguous. H8 now gives each run's tabs unique basenames and searches at most 64
tabs. It closes only a tab whose active title and process match the owned
fixture; it never closes the shared Notepad window or an unverified restored tab.

One additional H8 comparison now runs **only after a no-Alt mismatch**: it first
captures the failed screenshot and saved bytes, clears that same verified owned
document, then sends the same 45-character transcript through the Python direct
sender under the same guard. Exact direct output would narrow the difference to
Murmur's path or timing; corrupt direct output would implicate Notepad or the
desktop state. Neither result by itself identifies a production fix. The native
release gate remains closed until a built run proves exact insertion and the
remaining focus checks.
