# PR 26 desktop focus run: 43918857232

The Windows 10.0.26200 run at `/tmp/murmur-desktop-43918857232` tested exactly
`1401e7d2dc23adec77b08360b2c97365c2a8a788` from the passing manual build
[43914427509](https://github.com/Zethrus/Murmur/actions/runs/43914427509).
It recorded **5 pass, 5 fail, 0 skipped, 0 aborted**: O4, H8, F11, T1 and C1
passed; O2, O3, H4, T2 and C3 failed. The input guard recorded 167 healthy probes
and no abort. All five retained, run-owned Notepad tabs were saved and closed at
finalization (`hwnd=0` in the after-cleanup snapshot), with no cleanup failure.
This is direct evidence that cleanup worked in this focused run, not proof for
every Windows configuration. The prior full run `43906690485` had 44 pass,
3 fail, 1 skip and 10 Notepad cleanup failures; its 701 probes remained healthy.

## Source-proven correction and remaining native questions

| Check | Exact native observation | Change in this worktree | Still unproved |
| --- | --- | --- | --- |
| C3 | Second Pause showed `Paused`; the harness then failed because it could not read a progress bar. | `component.ts` hides the bar for paused jobs. C3 now reads the signed component's version, `jobs.json` identity/phase/bytes and its exact partial file in the disposable profile. It requires positive matching bytes at Pause, identical bytes after restart and while stalled, then additional consumed bytes after explicit Resume or `Installed`/`Active` with verified installation. No app UI changed. | Native C3 pause, quit, restart and resumed transfer on a new build. Network failure remains distinct from a product failure. |
| O3 | Immediately after `focus_while_held`, foreground was `0x120a38` while the fixture window was `0x90b78`; native focus was `RichEditD2DPT`, UIA focus was the Text editor, and no menu was open. No overlay button was clicked. | The existing pre/post-button focus requirements remain strict. Both pre-click artifacts now include foreground, active and focus HWND roots/PIDs, exact active fixture and editor class. [GetAncestor `GA_ROOT`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getancestor) identifies parent roots; [GetGUIThreadInfo](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getguithreadinfo) supplies active/focus HWNDs but can report transient invalid handles during activation. | Whether `0x120a38` is owned by the exact active Notepad fixture, and whether Discard/Copy preserve focus and typed input. No child HWND is silently accepted yet. |
| T2 | T1 found the icon and passed. T2 later found no newly visible Murmur-owned menu; its failure artifact showed no overflow window, no menu, and no Murmur icon in taskbar names. | The action artifact now names the attempted operation and records icon point/rect, UIA HWND, native hit/root/PID, overflow before/after and visible menus before/after. A previously invisible menu HWND can be reused, but a menu already visible before the click cannot satisfy freshness. The menu still must be visible and owned by the tested Murmur PID. Mute mismatch still fails. | Whether the overflow closes on right-click, the icon coordinate becomes stale, or the product fails to open its menu. The missing menu has not been reclassified as a pass. |
| O2 | The native hit rooted at the overlay; a 300×200 physical drag held the button, but the window did not move and no native drag entry was logged. O4 idle drag and F11 recording drag passed. The newer `/tmp/murmur-desktop-43930741072/O2-drag-diagnostic.txt` confirms the held button and unmoved overlay, but its only DOM sample was stage 0 with an expanded pill. | In `MURMUR_QA_WAV` sessions only, stage 4 is logged after the DOM capture listener is installed; stage 5 records the settled icon's center hit, bounds, pointer events and visibility. O2 requires both in order before dragging and retains QA DOM and native mouse boundary lines on failure. The existing owned-window subclass logs at most 64 `WM_MOUSEACTIVATE`, `WM_LBUTTONDOWN` and `WM_LBUTTONUP` messages in QA only. The QA command is restricted to the overlay window and returns disabled without the QA WAV environment. | Whether WebView2's renderer receives the mouse-down and which DOM/CSS branch handles the icon gesture. The subclass only sees same-process, same-thread windows; absence of a native line does not prove that the cross-process renderer missed input. Native drag and focus behavior were not changed. |
| H4 | The exact toggle output omitted `dog`: `The quick brown fox jumps over the lazy.. `. | QA WAV capture logs source samples consumed, total source samples, captured PCM samples and elapsed time on stop, without transcript or path. H4 failure retains that log, the second-toggle timestamp, exact unpadded fixture sample count and total padded sample count. The recognition oracle and timing remain unchanged. | Whether the QA worker consumed the final spoken samples before stop, or recognition failed despite complete input. |

The C3 correction is source-proven. O2, O3, T2 and H4 remain diagnostic changes,
not inferred product fixes. The Windows run above predates all changes in this
worktree. No Edison/native rerun, dispatch, push or release action occurred here.

## Verification and next run

Local tests cover C3 hidden-bar replacement, paused bytes, stalled/advanced/reset
bytes, component identity mismatch and verified completion; O3 focus evidence;
T2 open overflow, foreign/preexisting menus and invisible HWND reuse; H4 failure
artifact; and QA drag probe receipt. The native runner must still validate the
new observations on Windows. Reuse the ten-check selection
`O2,O3,O4,H4,H8,F11,T1,T2,C1,C3` on a newly built exact HEAD after the run
owner's Edison readiness/authorization gate, so the previous pass controls and
Notepad finalization are comparable. Examine O2 QA stages, O3 root/PID, T2
before/after icon/menu surfaces, H4 consumed versus unpadded samples, and C3
byte identity before changing product behavior.

Validation at the earlier triage point: 417 Python script tests passed; 62 UI tests passed;
`npm run build` passed TypeScript, token/protocol checks and Vite; Rust without
default features passed 290 unit tests (2 intentionally ignored) and 2 network
tests; Windows GNU release cross-check without default features passed. Targeted
Rust formatting of the edited files and `git diff --check` passed. The repository
wide `cargo fmt --check` still reports pre-existing formatting differences in
unmodified Enhance files. None of these checks exercises Windows UI behavior.

Rust with default features also passed 290 unit tests (3 intentionally ignored)
and the same 2 network tests.

QA overlay log stages are 0 initial handshake before listener installation,
1 DOM mouse-down, 2 handler accepted, 3 handler refused, 4 capture listener
armed and acknowledged by Rust, 5 settled visible idle icon. Target kinds are
0 other, 1 pill, 2 handle, 3 icon bars. Stage 5 samples the visible pill center;
the dedicated `.drag-handle` is hidden in icon mode. The new stages are local
diagnostics awaiting a Windows build and rerun, not a native O2 pass.
For this O2 diagnostic update, 63 UI tests, 209 desktop harness tests, 295 Rust
main tests (2 ignored), 11 Rust library tests and 2 network tests passed;
`npm run build`, Windows GNU `cargo check --no-default-features`, targeted Rust
formatting and `git diff --check` passed. These checks do not observe WebView2
on Edison.

Release acceptance remains closed on the five native failures and outstanding
manual 150%/mixed-DPI, Narrator and visual parity evidence. PR checks for the
tested HEAD were intentionally cancelled; the manual build above passed.
