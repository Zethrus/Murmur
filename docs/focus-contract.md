# Focus contract

What Murmur does with a finished transcript when the window you dictated into is,
or is not, the window that has focus at delivery. This extends the focus binding
of 0.4.7 (`src-tauri/src/focus_guard.rs`, `src-tauri/src/held.rs`,
`src-tauri/src/controller.rs`); the Windows rows are **verified in source, not
run on hardware**. Evidence is tracked in
[parity/test-ledger.md](parity/test-ledger.md); the hands-on Windows procedure is
[parity/windows-checklist.md](parity/windows-checklist.md) (items `F1`–`F12`).

## Rules

1. **Identity.** At the hotkey press Murmur records the focused window
   (`platform::focused_target`):
   - Linux/X11: the X11 window ID from `_NET_ACTIVE_WINDOW` plus its `WM_CLASS`.
     X11 IDs are not reused while the window exists, so the PID is not compared.
   - Windows: the foreground `HWND`, the owning process ID, and that process's
     creation time (`GetProcessTimes`), plus the executable name. HWNDs and PIDs
     are reused; all three must match.
   - One of Murmur's own windows (Settings, overlay) is "Own".
2. **Deliver only to the identical target.** Just before typing or pasting,
   focus is read again (after any overlay drag has finished). If the identity is
   equal in every field, the transcript is delivered, whatever the mode.
3. **Otherwise the configured mode decides** (`injection.focus_change`,
   Behavior → When focus changes):
   - `hold` (default): keep the text in the overlay with **Insert here**,
     **Copy** and **Discard**.
   - `refocus`: bring the original window back, then re-check rule 2; if that
     fails or the identity differs, hold.
   - `copy`: put the text on the clipboard; nothing is typed.
   - `insert`: type into whatever has focus now (explicit opt-in), **but never
     into an unknown or blocked target** (rule 4).
4. **Never type into an unknown or blocked target.** These are held (or copied in
   `copy` mode), in every mode including `insert`:
   - the focused window cannot be read (no X11 display, `xprop` failure, no
     foreground window);
   - Windows: the **secure desktop** has input (UAC prompt, lock screen,
     Ctrl+Alt+Del): `OpenInputDesktop` fails or the input desktop is not named
     `Default`;
   - Windows: the target runs at a **higher integrity level** than Murmur
     (for example "Run as administrator"), or its integrity level cannot be read.
     Windows (UIPI) silently drops synthetic input to such windows, so Murmur must
     not report it as delivered. A dictation that starts and ends in the same
     elevated window is still held. The held text says why.
5. **Held text** lives in memory only: never logged, written to disk or put in
   history until you insert or copy it. One transcript is held at a time; it is
   cleared after **120 seconds**, on Discard, or when the next dictation starts.
   **Insert here** types into the window focused at that moment (you chose it);
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
   synthetic letter that matches the hotkey cannot restart dictation.
8. **The overlay never takes focus.** It is `focusable: false` (Windows:
   `WS_EX_NOACTIVATE`); on Windows its window and WebView2's in-process children
   answer `WM_MOUSEACTIVATE` with `MA_NOACTIVATE`, and if it is activated anyway
   the foreground is handed straight back to the window that lost it. Its buttons
   prevent `mousedown`'s default, so clicking Accept/Discard/Insert never moves
   keyboard focus.

## What it cannot distinguish

- **Tabs inside one window** (browser tabs, terminal tabs, editor splits) share
  one window identity: switching tabs mid-dictation is delivered to the new tab.
- **Changes during typing.** Focus is checked once, just before typing starts;
  switching windows while a long transcript is being typed is not detected.
- **Same-integrity input filters.** Applications that ignore injected input at
  the same integrity level (some games, remote-desktop clients, password
  prompts in browsers) are not detected; `enigo` reports an error only when
  `SendInput` inserts fewer events than requested.
- **Wayland** is not supported for injection; focus is unknown, so text is held.

## Test matrix

| Rule | Unit test (Rust unless noted) | Manual check (Windows checklist) | Linux manual |
| --- | --- | --- | --- |
| 1 Identity, two windows of one app differ | `focus_guard::tests::two_windows_of_one_app_are_different_targets` | F2 | 0.4.7 X11 two-window test |
| 1 Windows: HWND + PID + start time | `focus_guard::tests::reused_hwnd_with_another_process_is_a_different_target` | F3 (close and reopen Notepad mid-dictation) | — |
| 1 X11 ID parsing | `platform::focus_tests::active_window_id_parses_from_xprop` | — | covered |
| 2 Same window always delivers | `focus_guard::tests::same_window_always_delivers` | F1 | covered |
| 2 Focus read after drag release | `controller::…::delivery_target_reads_focus_after_wait_and_keeps_busy_until_drop`, `…_cleans_up_focus_errors_and_never_reads_after_barrier_failure` | F11 | covered |
| 3 Modes hold/refocus/copy/insert | `focus_guard::tests::different_window_follows_mode`, `mode_spellings_round_trip` | F4–F7 | covered |
| 3 Unknown or own origin | `focus_guard::tests::unknown_or_own_start_is_a_mismatch` | F8 | covered |
| 4 Unknown current focus | `focus_guard::tests::unknown_current_focus_is_a_mismatch`, `insert_mode_never_types_into_an_unknown_target` | F9 | — |
| 4 Elevated / unreadable / secure desktop never typed into | `focus_guard::tests::blocked_targets_are_never_typed_into`, `platform::delivery_tests::elevated_or_unreadable_targets_are_blocked`, `only_the_default_desktop_takes_input`, `blocked_focus_keeps_its_class_for_messages` | F9 (UAC prompt), F10 (elevated Notepad) | n/a |
| 5 Held text memory-only, 120 s, by id | `held::tests::held_text_is_addressed_by_id`, `held_text_expires_after_two_minutes`, `notice_has_no_actionable_id_and_expires`, `clear_forgets_everything_and_reports_change`, `snapshot_shows_preview_origin_and_message` | F4, F12 | covered |
| 6 Refocus refusal → hold | decision: `focus_guard::tests::different_window_follows_mode`, `blocked_targets_are_never_typed_into` (Refocus from blocked origin holds); `SetForegroundWindow` refusal itself: none (needs a desktop) | F5 | covered |
| 7 Modifier wait (shared loop, 600 ms bound) | `inject::tests::modifier_wait_returns_once_released`, `modifier_wait_is_bounded_at_600_ms` | H3, H4 | covered |
| 7 Hotkey edges, no re-trigger during delivery | `hotkey::tests::only_press_release_edges_are_acted_on`, `presses_during_delivery_do_not_restart_dictation` | H2, H5 | covered |
| 8 Overlay never activates | `overlay::noactivate::tests::*` (decision logic); UI: `overlay buttons never take focus on mouse down but still act on click (Windows B7)` (`scripts/tests/ui.test.mjs`) | O3, O4 | GTK path unchanged |

"covered" in the Linux column means the existing 0.4.7/0.5.0 Linux checks; no
Linux behaviour changed except rule 4's first bullet in `insert` mode, which used
to fail with an error and now holds the text.
