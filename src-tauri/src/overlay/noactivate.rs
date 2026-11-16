//! Windows: clicking the overlay must never take keyboard focus from the dictation target (B7).
//!
//! What actually protects the target (verified in source, not run on hardware):
//!
//! 1. `focusable: false` (tauri.conf.json) gives the top-level overlay `WS_EX_NOACTIVATE`
//!    through tao, so a click does not activate it in the first place.
//! 2. If it is activated anyway, the `WM_ACTIVATE` hand-back below calls `SetForegroundWindow`
//!    on the window that lost activation and consumes that activation message.
//! 3. The overlay page prevents `mousedown`'s default on its buttons so the DOM never moves focus
//!    (src/overlay/no-focus.ts).
//!
//! The `WM_MOUSEACTIVATE` → `MA_NOACTIVATE` subclass is kept because it is harmless, but expect
//! it to cover only the top-level window: with default WebView2 hosting the `Chrome_WidgetWin_*`
//! and `Chrome_RenderWidgetHostHWND` windows belong to `msedgewebview2.exe`, not to this thread,
//! so `EnumChildWindows` is expected to find no child this thread may subclass, and Chromium's
//! `LegacyRenderWidgetHostHWND` answers `WM_MOUSEACTIVATE` itself. The number of subclassed
//! windows is logged at info level ("Overlay no-activate subclass") so the Edison checklist
//! (docs/parity/windows-checklist.md, O3/O4/O7) records what really happened.

// The decision helpers are used by the Windows subclass and by tests on every platform.
#![cfg_attr(not(windows), allow(dead_code))]

/// `WM_MOUSEACTIVATE` / `MA_NOACTIVATE` / `WM_ACTIVATE` / `WA_INACTIVE` (winuser.h), local so the
/// decision logic is testable on every platform.
pub const WM_MOUSEACTIVATE: u32 = 0x0021;
const WM_LBUTTONDOWN: u32 = 0x0201;
const WM_LBUTTONUP: u32 = 0x0202;
pub const MA_NOACTIVATE: isize = 3;
pub const WM_ACTIVATE: u32 = 0x0006;
pub const WA_INACTIVE: usize = 0;

/// Reply for a message reaching a subclassed overlay window: `Some(result)` answers it here,
/// `None` passes it on unchanged.
pub fn mouse_activate_reply(message: u32) -> Option<isize> {
    (message == WM_MOUSEACTIVATE).then_some(MA_NOACTIVATE)
}

/// QA observation only. It never supplies a message result or intercepts renderer windows.
fn qa_mouse_message(message: u32) -> Option<&'static str> {
    match message {
        WM_MOUSEACTIVATE => Some("WM_MOUSEACTIVATE"),
        WM_LBUTTONDOWN => Some("WM_LBUTTONDOWN"),
        WM_LBUTTONUP => Some("WM_LBUTTONUP"),
        _ => None,
    }
}

/// Whether an activation of the overlay should hand the foreground back: it was activated
/// (`WA_ACTIVE`/`WA_CLICKACTIVE`) and Windows names the window that lost activation.
pub fn restore_previous_on_activate(message: u32, wparam: usize, previous_is_window: bool) -> bool {
    message == WM_ACTIVATE && (wparam & 0xFFFF) != WA_INACTIVE && previous_is_window
}

/// Keep the hand-back and default dispatch in one testable control-flow boundary.
fn dispatch_activation(
    message: u32,
    wparam: usize,
    previous_is_window: bool,
    restore: impl FnOnce(),
    forward: impl FnOnce() -> isize,
) -> isize {
    if restore_previous_on_activate(message, wparam, previous_is_window) {
        restore();
        // DefWindowProc's active WM_ACTIVATE handling assigns keyboard focus. Do not
        // let it undo the hand-back, even if Windows refuses foreground restoration.
        return 0;
    }
    forward()
}

/// Only windows owned by this thread can be subclassed (`SetWindowSubclass` refuses others).
/// WebView2's renderer windows belong to `msedgewebview2.exe`, so in practice this excludes all
/// of them; see the module documentation.
pub fn can_subclass(owner_process: u32, owner_thread: u32, process: u32, thread: u32) -> bool {
    owner_process == process && owner_thread == thread
}

/// Compact key of one install's outcome, compared to log only when it changes. Never
/// `usize::MAX`, which marks "nothing logged yet".
pub fn log_key(top_level: bool, children: usize) -> usize {
    (children.min(1 << 20) << 1) | usize::from(top_level)
}

#[cfg(windows)]
mod native {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use windows_sys::Win32::{
        Foundation::{BOOL, HWND, LPARAM, LRESULT, WPARAM},
        System::Threading::{GetCurrentProcessId, GetCurrentThreadId},
        UI::{
            Shell::{DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass},
            WindowsAndMessaging::{
                EnumChildWindows, GetWindowThreadProcessId, IsWindow, SetForegroundWindow,
                WM_NCDESTROY,
            },
        },
    };

    /// Subclass id ("MRNA"): one subclass per window, so repeated installs only refresh it.
    const SUBCLASS_ID: usize = 0x4D52_4E41;
    /// Last logged `log_key`, so the info line appears once per change rather than on every show.
    static LOGGED: AtomicUsize = AtomicUsize::new(usize::MAX);
    const QA_MOUSE_LOG_LIMIT: usize = 64;
    static QA_MOUSE_LOGGED: AtomicUsize = AtomicUsize::new(0);

    unsafe extern "system" fn overlay_proc(
        window: HWND,
        message: u32,
        wparam: WPARAM,
        lparam: LPARAM,
        id: usize,
        _data: usize,
    ) -> LRESULT {
        if let Some(name) = qa_mouse_message(message) {
            if std::env::var_os("MURMUR_QA_WAV").is_some() {
                if QA_MOUSE_LOGGED.fetch_add(1, Ordering::Relaxed) < QA_MOUSE_LOG_LIMIT {
                    // Only this process/thread's already-subclassed HWNDs reach this callback.
                    // WebView2 renderer mouse messages are outside this observation boundary.
                    tracing::info!(target: "murmur::qa_overlay_drag", hwnd = ?window,
                                   message = name, "QA overlay native mouse");
                }
            }
        }
        if let Some(reply) = mouse_activate_reply(message) {
            return reply;
        }
        // Only WM_ACTIVATE carries a window in lparam; never probe it for other messages.
        let previous = lparam as HWND;
        let previous_is_window =
            message == WM_ACTIVATE && !previous.is_null() && IsWindow(previous) != 0;
        dispatch_activation(
            message,
            wparam,
            previous_is_window,
            || {
                // The click gave Murmur the foreground right; hand it straight back.
                SetForegroundWindow(previous);
            },
            || {
                if message == WM_NCDESTROY {
                    RemoveWindowSubclass(window, Some(overlay_proc), id);
                }
                DefSubclassProc(window, message, wparam, lparam)
            },
        )
    }

    fn subclass(window: HWND) -> bool {
        unsafe {
            let mut process = 0u32;
            let thread = GetWindowThreadProcessId(window, &mut process);
            if !can_subclass(process, thread, GetCurrentProcessId(), GetCurrentThreadId()) {
                return false;
            }
            SetWindowSubclass(window, Some(overlay_proc), SUBCLASS_ID, 0) != 0
        }
    }

    unsafe extern "system" fn each_child(window: HWND, count: LPARAM) -> BOOL {
        if subclass(window) {
            *(count as *mut usize) += 1;
        }
        1
    }

    /// Subclass the overlay and every descendant this thread owns. Must run on the UI thread;
    /// idempotent, so it is repeated whenever the overlay is shown (WebView2 creates its
    /// children asynchronously after the window exists).
    pub fn install(window: &tauri::WebviewWindow) {
        let Ok(handle) = window.hwnd() else {
            return;
        };
        let top = handle.0 as HWND;
        let mut children = 0usize;
        let top_level = subclass(top);
        unsafe {
            EnumChildWindows(top, Some(each_child), &mut children as *mut usize as LPARAM);
        }
        let key = log_key(top_level, children);
        if LOGGED.swap(key, Ordering::Relaxed) != key {
            // Evidence for the Edison checklist (O7): children=0 is expected when all of
            // WebView2's windows live in msedgewebview2.exe.
            tracing::info!(
                top_level,
                children,
                "Overlay no-activate subclass: top-level and in-process child windows"
            );
        }
    }
}

#[cfg(windows)]
pub use native::install;

#[cfg(not(windows))]
pub fn install(_window: &tauri::WebviewWindow) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn handled_activation_never_runs_default_focus_processing() {
        use std::cell::Cell;
        for state in [1, 2, 2 | 0x1_0000] {
            let restored = Cell::new(false);
            let forwarded = Cell::new(false);
            let result = dispatch_activation(
                WM_ACTIVATE,
                state,
                true,
                || restored.set(true),
                || {
                    forwarded.set(true);
                    99
                },
            );
            assert!(restored.get());
            assert!(
                !forwarded.get(),
                "hand-back must terminate activation dispatch"
            );
            assert_eq!(result, 0);
        }
    }

    #[test]
    fn unhandled_activation_and_other_messages_keep_default_dispatch() {
        for (message, state, valid) in [
            (WM_ACTIVATE, WA_INACTIVE, true),
            (WM_ACTIVATE, 1, false),
            (WM_LBUTTONDOWN, 1, true),
            (WM_LBUTTONUP, 1, true),
            (0x0082, 1, true), // WM_NCDESTROY must retain subclass cleanup.
        ] {
            assert_eq!(
                dispatch_activation(
                    message,
                    state,
                    valid,
                    || panic!("unexpected hand-back"),
                    || 99
                ),
                99
            );
        }
    }

    #[test]
    fn mouse_activation_is_answered_without_activating() {
        assert_eq!(mouse_activate_reply(WM_MOUSEACTIVATE), Some(MA_NOACTIVATE));
        // Clicks themselves (WM_LBUTTONDOWN = 0x0201) still reach the page.
        assert_eq!(mouse_activate_reply(WM_LBUTTONDOWN), None);
        assert_eq!(mouse_activate_reply(WM_LBUTTONUP), None);
        assert_eq!(mouse_activate_reply(WM_ACTIVATE), None);
    }

    #[test]
    fn qa_mouse_observation_does_not_change_message_replies() {
        for (message, name) in [
            (WM_MOUSEACTIVATE, "WM_MOUSEACTIVATE"),
            (WM_LBUTTONDOWN, "WM_LBUTTONDOWN"),
            (WM_LBUTTONUP, "WM_LBUTTONUP"),
        ] {
            assert_eq!(qa_mouse_message(message), Some(name));
        }
        assert_eq!(qa_mouse_message(WM_ACTIVATE), None);
        assert_eq!(mouse_activate_reply(WM_MOUSEACTIVATE), Some(MA_NOACTIVATE));
        assert_eq!(mouse_activate_reply(WM_LBUTTONDOWN), None);
        assert_eq!(mouse_activate_reply(WM_LBUTTONUP), None);
    }

    #[test]
    fn activation_hands_foreground_back_only_when_activated() {
        const WA_ACTIVE: usize = 1;
        const WA_CLICKACTIVE: usize = 2;
        assert!(restore_previous_on_activate(WM_ACTIVATE, WA_ACTIVE, true));
        assert!(restore_previous_on_activate(
            WM_ACTIVATE,
            WA_CLICKACTIVE,
            true
        ));
        // High word carries the minimized flag; only the low word is the state.
        assert!(restore_previous_on_activate(
            WM_ACTIVATE,
            WA_CLICKACTIVE | 0x1_0000,
            true
        ));
        assert!(!restore_previous_on_activate(
            WM_ACTIVATE,
            WA_INACTIVE,
            true
        ));
        assert!(!restore_previous_on_activate(WM_ACTIVATE, WA_ACTIVE, false));
        assert!(!restore_previous_on_activate(
            WM_MOUSEACTIVATE,
            WA_ACTIVE,
            true
        ));
    }

    #[test]
    fn subclass_outcome_is_logged_once_per_change() {
        assert_ne!(log_key(true, 0), log_key(false, 0));
        assert_ne!(log_key(true, 0), log_key(true, 1));
        assert_eq!(log_key(true, 2), log_key(true, 2));
        // The "nothing logged yet" sentinel is never a real outcome.
        assert_ne!(log_key(true, usize::MAX), usize::MAX);
    }

    #[test]
    fn only_windows_of_this_thread_are_subclassed() {
        assert!(can_subclass(10, 20, 10, 20));
        // WebView2 renderer windows belong to msedgewebview2.exe.
        assert!(!can_subclass(11, 20, 10, 20));
        assert!(!can_subclass(10, 21, 10, 20));
    }
}
