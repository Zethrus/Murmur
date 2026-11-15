# Focused Windows desktop diagnostic 43661757411

Build `fff62c23c57e6f91690af6013a3fd68749f84251` completed with one pass and one fail. The
input guard observed 46 probes with no abort. Mouse activity was recorded during O2, but its
drag and restart persistence assertions passed; the activity is not an explanation for H8.

| Check | Observed native evidence | Conclusion |
| --- | --- | --- |
| O2 | Overlay moved from `(934, 964, 986, 1004)` to `(634, 764, 686, 804)` while the mouse button was held, and retained those bounds after restart. | Drag and persistence passed in this run. |
| H8 Alt | Notepad's editor had UIA focus through F9 up and Shift up. Immediately after Alt up, with Ctrl still held, focus was `MenuItem 'File'`; after delivery it was `ListItem 'Title'`. The exact 45-character history output differed from the 43-character UIA document readback at character 0. | Alt release is the observed focus transition. The present evidence does not identify a safe cross-application menu mask. |
| H8 no Alt | Editor UIA focus remained throughout. Before the delivery wait the document already reported 45 characters, but the exact output differed at character 16: `The quick brown ` was followed by spaces. | The focus failure is absent, but the source of text corruption is not yet proven. |

On Windows, enigo 0.2.1 sends a whole string's Unicode key pairs through one `SendInput` call.
The Windows type path now sends one character at a time, pausing briefly after every eight. This
is a targeted hypothesis for the no Alt failure, not a native validation result. H8 now saves
Notepad screenshots before closing either trial and compares the saved document with the exact
same-utterance history text after close. These readbacks should distinguish a TextPattern artifact
from visible or persisted corruption while retaining the exact UIA output oracle. No transcript
is added to the app's logs or a diagnostic clipboard.

[Microsoft's `WM_SYSKEYUP` documentation](https://learn.microsoft.com/en-us/windows/win32/inputdev/wm-syskeyup)
describes `SC_KEYMENU` when Alt is released. A global low-level keyboard hook can suppress a
message, but [its documentation](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc)
also says it prevents the target window and other hooks from seeing that key event. There is no
native evidence here that swallowing Alt up would leave Notepad and other targets with a coherent
keyboard state. A repair Escape or refocus after Alt release would hide the failure. Neither was
added.

The next native gate is focused H8, H1, I1, I4 and I5 on a build containing these changes, before
the full desktop suite. The release gate remains closed until exact insertion and focus assertions
pass in native runs.
