# H8 Windows restart guard triage: 43767810016

At `cf7eb4b1efe4fed3bdc00fb022490098a909d921`, the H8-only native run in
`/tmp/murmur-desktop-43767810016` aborted after 26 successful guard probes. The first
probe had both owned-window hook callbacks. The same guard remained active for the entire
attempt; the report records no replacement or intentional pause.

| Phase | Native evidence | Verdict |
| --- | --- | --- |
| Direct control | All 135 characters reached the Notepad UI Automation document and saved UTF-8 file. The hook saw exact, paired Unicode key events and two interleaved guard probes. | Control passed. |
| `Ctrl+Alt+Shift+F9` | Editor focus remained throughout the sampled modifier phases. The 45-character hook stream, history output, UI Automation document, and saved file matched exactly; key pairs were complete. Murmur logged delivery to Notepad. | Alt trial passed in this diagnostic run. |
| `Ctrl+Shift+F9` | The new Murmur process logged startup at 22:11:56.643 UTC. Before the first Notepad phase, probe 27 was not seen within 0.5 s despite `SendInput` returning one event in 0 ms. The guard aborted. | No non-Alt delivery verdict. |

At abort, the observer report had 404 keyboard callbacks, 27 posted and received
heartbeats, `pump_exited_before_stop: false`, and no unhook error. These are cumulative
counts; they do not establish whether the keyboard callback or heartbeat ran **after**
probe 27 was sent. The normal shutdown later returned `GetMessageW == 0`, which does not
show that the pump exited before the abort. A successful `SendInput` return proves stream
submission, not hook receipt. [Microsoft documents `SendInput`'s return value](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput).

Murmur's pinned `global-hotkey 0.8.0` Windows implementation registers the shortcut
with `RegisterHotKey`, receives `WM_HOTKEY` through a hidden window, and polls
`GetAsyncKeyState` for release. It has no `WH_KEYBOARD_LL` callback or event suppression
path. The Tauri plugin wraps that manager. This rules out a *direct* library hook-chain
block of an unrelated reserved-key release; it does not explain the missing probe.
[Microsoft's hotkey API](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey)
also describes the `WM_HOTKEY` delivery path. Windows can silently remove a slow low-level
hook, so absence of an unhook error is not proof that the observer hook survived.
[Microsoft's low-level hook documentation](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc)
describes that failure mode.

The next built H8-only run now records a `missed_probe` snapshot with keyboard callback
counts before send and at timeout, post-send heartbeat counts and age, last keyboard
callback age, and hook-thread liveness. Timeline marks delimit each Murmur stop, spawn,
window-ready, and startup-wait phase, plus the Alt and non-Alt `ensure()` calls. This
narrows pump delay versus missing or mismatched keyboard callback and places the loss
within the restart. It still fails closed at the same 0.5 s limit, observes
real input, and leaves the exact delivery oracle unchanged. No production hotkey fix is
justified until the callback loss is located. The full native release gate remains closed.
