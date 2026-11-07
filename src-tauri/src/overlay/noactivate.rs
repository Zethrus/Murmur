//! Windows: clicking the overlay must never take keyboard focus from the dictation target (B7).
//!
//! `focusable: false` (tauri.conf.json) already gives the top-level overlay `WS_EX_NOACTIVATE`
//! through tao. The remaining risk is WebView2: a click lands on one of its child HWNDs, and the
//! default `WM_MOUSEACTIVATE` handling activates the top-level window. Every overlay window this
//! thread owns (the top level and WebView2's in-process host children) is subclassed to answer
//! `WM_MOUSEACTIVATE` with `MA_NOACTIVATE`; cross-process children (the renderer's input window)
//! forward the message to these ancestors through `DefWindowProc`. As a last line, if the overlay
//! is activated anyway, foreground goes straight back to the window that lost it.
//!
//! The overlay page also prevents `mousedown` default handling on its buttons so the DOM never
//! moves focus (src/overlay/no-focus.ts). Verified in source, not run on hardware: the Edison
//! checklist (docs/parity/windows-checklist.md, "Overlay focus") is the evidence still owed.

// The decision helpers are used by the Windows subclass and by tests on every platform.
#![cfg_attr(not(windows), allow(dead_code))]

/// `WM_MOUSEACTIVATE` / `MA_NOACTIVATE` / `WM_ACTIVATE` / `WA_INACTIVE` (winuser.h), local so the
/// decision logic is testable on every platform.
pub const WM_MOUSEACTIVATE: u32 = 0x0021;
pub const MA_NOACTIVATE: isize = 3;
pub const WM_ACTIVATE: u32 = 0x0006;
pub const WA_INACTIVE: usize = 0;

/// Reply for a message reaching a subclassed overlay window: `Some(result)` answers it here,
/// `None` passes it on unchanged.
pub fn mouse_activate_reply(message: u32) -> Option<isize> {
    (message == WM_MOUSEACTIVATE).then_some(MA_NOACTIVATE)
}

/// Whether an activation of the overlay should hand the foreground back: it was activated
/// (`WA_ACTIVE`/`WA_CLICKACTIVE`) and Windows names the window that lost activation.
pub fn restore_previous_on_activate(message: u32, wparam: usize, previous_is_window: bool) -> bool {
    message == WM_ACTIVATE && (wparam & 0xFFFF) != WA_INACTIVE && previous_is_window
}

/// Only windows owned by this thread can be subclassed (`SetWindowSubclass` refuses others);
/// cross-process WebView2 children are covered by their in-process ancestors.
pub fn can_subclass(owner_process: u32, owner_thread: u32, process: u32, thread: u32) -> bool {
    owner_process == process && owner_thread == thread
}

#[cfg(windows)]
mod native {
    use super::*;
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

    unsafe extern "system" fn overlay_proc(
        window: HWND,
        message: u32,
        wparam: WPARAM,
        lparam: LPARAM,
        id: usize,
        _data: usize,
    ) -> LRESULT {
        if let Some(reply) = mouse_activate_reply(message) {
            return reply;
        }
        let previous = lparam as HWND;
        if restore_previous_on_activate(
            message,
            wparam,
            !previous.is_null() && IsWindow(previous) != 0,
        ) {
            // The click gave Murmur the foreground right; hand it straight back.
            SetForegroundWindow(previous);
        }
        if message == WM_NCDESTROY {
            RemoveWindowSubclass(window, Some(overlay_proc), id);
        }
        DefSubclassProc(window, message, wparam, lparam)
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
        tracing::debug!(top_level, children, "Overlay windows answer MA_NOACTIVATE");
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
    fn mouse_activation_is_answered_without_activating() {
        assert_eq!(mouse_activate_reply(WM_MOUSEACTIVATE), Some(MA_NOACTIVATE));
        // Clicks themselves (WM_LBUTTONDOWN = 0x0201) still reach the page.
        assert_eq!(mouse_activate_reply(0x0201), None);
        assert_eq!(mouse_activate_reply(WM_ACTIVATE), None);
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
    fn only_windows_of_this_thread_are_subclassed() {
        assert!(can_subclass(10, 20, 10, 20));
        // WebView2 renderer windows belong to msedgewebview2.exe.
        assert!(!can_subclass(11, 20, 10, 20));
        assert!(!can_subclass(10, 21, 10, 20));
    }
}
