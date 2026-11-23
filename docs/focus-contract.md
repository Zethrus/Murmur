# Focus contract

What Murmur does with a finished transcript when the window you dictated into is,
or is not, the window that has focus at delivery. This extends the focus binding
of 0.4.7 (`src-tauri/src/focus_guard.rs`, `src-tauri/src/held.rs`,
`src-tauri/src/controller.rs`). Windows focus and insertion evidence is tracked in
[parity/test-ledger.md](parity/test-ledger.md); the hands-on Windows procedure is
[parity/windows-checklist.md](parity/windows-checklist.md) (items `F1`–`F13`).

## Rules

1. **Identity.** On Windows, at an accepted dictation or code shortcut activation, Murmur
   records the focused target before queuing the action. Other start paths read
   focus when the controller handles their action. Linux retains its existing
   controller-thread focus capture for every start path.
   (`platform::focused_target`):
   - Linux/X11: the X11 window ID from `_NET_ACTIVE_WINDOW` plus its `WM_CLASS`.
     X11 IDs are not reused while the window exists, so the PID is not compared.
   - Windows: the foreground `HWND`, owning PID and creation time, plus the
     keyboard focus `HWND` from `GetGUIThreadInfo`, its thread ID, owning PID and
     creation time. An embedded renderer may own the focused child in another
     process. All identity fields must match. An absent, invalid, unrelated or
     unverified child is blocked. The foreground executable name supplies style.
   - One of Murmur's own windows (Settings, overlay) is "Own".
2. **Deliver only to the identical target.** Just before typing or pasting,
   focus is read again (after any overlay drag has finished). If the identity is
   equal in every field, the transcript is delivered, whatever the mode. Windows
   checks the full target again before each character in the type path.
3. **Otherwise the configured mode decides** (`injection.focus_change`,
   Behavior → When focus changes):
   - `hold` (default): keep the text in the overlay with **Insert here**,
     **Copy** and **Discard**.
   - `refocus`: bring the original top-level window back, then re-check rule 2;
     if that fails or its focused child differs, hold. Top-level activation does
     not restore the original child control.
   - `copy`: put the text on the clipboard; nothing is typed.
   - `insert`: type into whatever has focus now (explicit opt-in), **but never
     into an unknown or blocked target** (rule 4).
4. **Never type into an unknown or blocked target.** These are held (or copied in
   `copy` mode), in every mode including `insert`:
   - the focused window cannot be read (no X11 display, `xprop` failure, no
     foreground window); the held text then shows that error rather than
     "focus changed";
   - Windows: the **secure desktop** has input (UAC prompt, lock screen,
     Ctrl+Alt+Del): `OpenInputDesktop` fails or the input desktop is not named
     `Default`;
   - Windows: the target runs at a **higher integrity level** than Murmur
     (for example "Run as administrator"), or its integrity level cannot be read.
     Windows (UIPI) silently drops synthetic input to such windows, so Murmur must
     not report it as delivered. A dictation that starts and ends in the same
     elevated window is still held. The held text says why.
   - Windows: the foreground thread is in Win32 menu mode. An Alt hotkey can
     move focus into a menu while the top-level window identity stays the same.
   - Windows: the focused child is missing, invalid, outside the foreground
     window, or its process identity or integrity cannot be verified. This
     check is separate from detecting a changed child: a custom menu whose Win32
     menu flags remain false may have a valid HWND. Its difference from the original
     editor is handled by the configured focus-change policy in rule 3.
5. **Held text** lives in memory only: never logged, written to disk or put in
   history until you insert or copy it. One transcript is held at a time; it is
   cleared after **120 seconds**, on Discard, or when the next dictation starts.
   **Insert here** captures the current keyboard target at that retry (you chose
   it), rather than reusing the original dictation target;
   it is refused for Murmur's own windows and for blocked targets (rule 4).
6. **Refocus** uses only the platform's sanctioned call: `xdotool
   windowactivate` on Linux, `SetForegroundWindow` on Windows. No
   `AttachThreadInput`, `AllowSetForegroundWindow` or simulated Alt presses.
   If Windows refuses (foreground lock) or the window is gone, the text is held.
   Confirmation waits at most ~500 ms.
7. **Modifiers.** Before typing or pasting, Murmur waits up to 600 ms for
   Control/Alt (Linux) or Control/Alt/Shift/Windows (Windows) to be released, so a
   half-released hold chord cannot turn the first words into shortcuts.
   global-hotkey reports Released on Windows by polling `GetAsyncKeyState` for the
   non-modifier key every 50 ms (registered with `MOD_NOREPEAT`), so the modifiers
   can still be down at that moment. A dictation press that arrives while the
   previous transcript is being transcribed or delivered is ignored, so a
   synthetic letter that matches the hotkey cannot restart dictation. Windows
   delivery stops and holds the full raw transcript if these modifiers remain
   down, with a reason to release them and deliberately retry with Insert here.
8. **The overlay never takes focus.** Three things protect the target on
   Windows: the overlay is `focusable: false`, which gives it
   `WS_EX_NOACTIVATE`; if it is activated anyway, its `WM_ACTIVATE` handler
   hands the foreground straight back to the window that lost it
   (`SetForegroundWindow`); and its buttons prevent `mousedown`'s default, so
   clicking Accept/Discard/Insert never moves DOM focus. The overlay also
   subclasses its windows to answer `WM_MOUSEACTIVATE` with `MA_NOACTIVATE`, but
   with default WebView2 hosting this reaches only the top-level window:
   WebView2's `Chrome_WidgetWin_*` windows belong to `msedgewebview2.exe`, not to
   Murmur's UI thread, and Chromium's `LegacyRenderWidgetHostHWND` answers
   `WM_MOUSEACTIVATE` itself. The subclass is harmless and kept; how many windows
   it covered is logged at info level (`Overlay no-activate subclass`, checklist
   O7). Verified in source, not run on hardware.
9. **Insert here types while Murmur is idle.** `deliver_held` runs from the
   overlay button in the `idle` state, where dictation presses are not ignored
   (rule 7 ignores them only while transcribing or delivering). This is believed
   safe: on Windows Murmur sends each character as
   `KEYEVENTF_UNICODE` (`VK_PACKET`), which `RegisterHotKey` hotkeys do not
   match, and the user has just clicked a button, so no hotkey modifiers are
   held. The paste path sends Ctrl+V, which matters only for a dictation hotkey
   bound to Ctrl+V. F13 separately checks modifier refusal, recovery, and exact
   type and clipboard insertion. **Believed, verify on Edison** (checklist F13).

## What it cannot distinguish

- **Tabs or editor splits within one focused child HWND** cannot be distinguished.
- **Changes during typing.** On Windows, Murmur checks the foreground and focused
  child identity before each character and stops if either changes. An application
  can still change focus in the instant between check and submission, or change
  an internal logical target without changing its HWND; this is a best-effort
  guard, not an atomic focus guarantee. Linux checks
  focus once before typing starts.
- **Same-integrity input filters.** Applications that ignore injected input at
  the same integrity level (some games, remote-desktop clients, password
  prompts in browsers) are not detected; Windows reports an error only when
  `SendInput` inserts fewer events than requested. On a short submission Murmur
  attempts key-up-only cleanup for unmatched keys it inserted in that call;
  cleanup itself can fail, so the full transcript remains held for review.
- **Wayland** is not supported for injection; focus is unknown, so text is held.

## Test matrix

| Rule | Unit test (Rust unless noted) | Manual check (Windows checklist) | Linux manual |
| --- | --- | --- | --- |
| 1 Identity, two windows of one app differ | `focus_guard::tests::two_windows_of_one_app_are_different_targets` | F2 | 0.4.7 X11 two-window test |
| 1 Windows: HWND + PID + start time (a PID without a start time never matches) | `focus_guard::tests::reused_hwnd_with_another_process_is_a_different_target`, `windows_identity_without_start_time_never_matches` | F3 (close and reopen Notepad mid-dictation) | — |
| 1 X11 ID parsing | `platform::focus_tests::active_window_id_parses_from_xprop` | — | covered |
| 2 Same Windows child delivers; changed/missing child holds | `focus_guard::tests::windows_same_child_delivers_but_another_child_holds`, `windows_missing_or_reused_child_never_matches` | H9 repeat needed | n/a |
| 2 Focus read after drag release | `controller::…::delivery_target_reads_focus_after_wait_and_keeps_busy_until_drop`, `…_cleans_up_focus_errors_and_never_reads_after_barrier_failure` | F11 | covered |
| 3 Modes hold/refocus/copy/insert | `focus_guard::tests::different_window_follows_mode`, `mode_spellings_round_trip` | F4–F7 | covered |
| 3 Unknown or own origin | `focus_guard::tests::unknown_or_own_start_is_a_mismatch` | F8 | covered |
| 4 Unknown current focus (held text shows the platform error) | `focus_guard::tests::unknown_current_focus_is_a_mismatch`, `insert_mode_never_types_into_an_unknown_target`, `controller::tests::held_text_says_why_when_focus_is_unreadable_or_blocked` | F9 | — |
| 4 Elevated / unreadable / secure desktop never typed into | `focus_guard::tests::blocked_targets_are_never_typed_into`, `platform::delivery_tests::elevated_or_unreadable_targets_are_blocked`, `only_the_default_desktop_takes_input`, `blocked_focus_keeps_its_class_for_messages` | F9 (UAC prompt), F10 (elevated Notepad) | n/a |
| 5 Held text memory-only, 120 s, by id | `held::tests::held_text_is_addressed_by_id`, `held_text_expires_after_two_minutes`, `notice_has_no_actionable_id_and_expires`, `clear_forgets_everything_and_reports_change`, `snapshot_shows_preview_origin_and_message` | F4, F12 | covered |
| 6 Refocus refusal → hold | decision: `focus_guard::tests::different_window_follows_mode`, `blocked_targets_are_never_typed_into` (Refocus from blocked origin holds); `SetForegroundWindow` refusal itself: none (needs a desktop) | F5 | covered |
| 7 Modifier wait (shared loop, 600 ms bound) | `inject::tests::modifier_wait_returns_once_released`, `modifier_wait_is_bounded_at_600_ms` | H3, H4 | covered |
| 7 Hotkey edges, no re-trigger during delivery | `hotkey::tests::only_press_release_edges_are_acted_on`, `presses_during_delivery_do_not_restart_dictation` | H2, H5 | covered |
| 8 Overlay never activates | `overlay::noactivate::tests::*` (decision logic, log-once key); UI: `overlay buttons never take focus on mouse down but still act on click (Windows B7)` (`scripts/tests/ui.test.mjs`) | O3, O4, O7 | GTK path unchanged |
| 9 Insert here in idle does not re-trigger the hotkey | `scripts/tests/test_desktop_harness.py::F13Tests` (harness assertions; Windows input still required) | F13 | covered |

"covered" in the Linux column means the existing 0.4.7/0.5.0 Linux checks; no
Linux behaviour changed except rule 4's first bullet in `insert` mode, which used
to fail with an error and now holds the text.
