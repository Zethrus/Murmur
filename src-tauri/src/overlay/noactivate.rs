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
use std::cell::Cell;

/// `WM_MOUSEACTIVATE` / `MA_NOACTIVATE` / `WM_ACTIVATE` / `WA_INACTIVE` (winuser.h), local so the
/// decision logic is testable on every platform.
pub const WM_MOUSEACTIVATE: u32 = 0x0021;
const WM_LBUTTONDOWN: u32 = 0x0201;
const WM_LBUTTONUP: u32 = 0x0202;
const WM_SETFOCUS: u32 = 0x0007;
const WM_KILLFOCUS: u32 = 0x0008;
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

/// One activation/focus message consumes one slot, even though it can emit an entry,
/// hand-back, and exit line. The first over-limit message emits one final cap line.
const QA_ACTIVATION_TRACE_LIMIT: usize = 128;

fn qa_activation_message(message: u32) -> Option<&'static str> {
    match message {
        WM_ACTIVATE => Some("WM_ACTIVATE"),
        WM_SETFOCUS => Some("WM_SETFOCUS"),
        WM_KILLFOCUS => Some("WM_KILLFOCUS"),
        _ => None,
    }
}

#[derive(Debug, PartialEq, Eq)]
enum QaTraceSlot {
    Off,
    Record { id: usize, cap_reached: bool },
    FirstDropped,
    Dropped,
}

fn qa_trace_slot(enabled: bool, prior: usize) -> QaTraceSlot {
    if !enabled {
        QaTraceSlot::Off
    } else if prior < QA_ACTIVATION_TRACE_LIMIT {
        QaTraceSlot::Record {
            id: prior + 1,
            cap_reached: prior + 1 == QA_ACTIVATION_TRACE_LIMIT,
        }
    } else if prior == QA_ACTIVATION_TRACE_LIMIT {
        QaTraceSlot::FirstDropped
    } else {
        QaTraceSlot::Dropped
    }
}

fn qa_trace_enter(current: &Cell<usize>, id: usize) -> usize {
    current.replace(id)
}

fn qa_trace_exit(current: &Cell<usize>, parent: usize) {
    current.set(parent);
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
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use windows_sys::Win32::{
        Foundation::{BOOL, HWND, LPARAM, LRESULT, WPARAM},
        System::Threading::{GetCurrentProcessId, GetCurrentThreadId},
        UI::{
            Input::KeyboardAndMouse::GetFocus,
            Shell::{DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass},
            WindowsAndMessaging::{
                EnumChildWindows, GetForegroundWindow, GetWindowThreadProcessId, IsWindow,
                SetForegroundWindow, WM_NCDESTROY,
            },
        },
    };

    /// Subclass id ("MRNA"): one subclass per window, so repeated installs only refresh it.
    const SUBCLASS_ID: usize = 0x4D52_4E41;
    /// Last logged `log_key`, so the info line appears once per change rather than on every show.
    static LOGGED: AtomicUsize = AtomicUsize::new(usize::MAX);
    const QA_MOUSE_LOG_LIMIT: usize = 64;
    static QA_MOUSE_LOGGED: AtomicUsize = AtomicUsize::new(0);
    static QA_TRACE_MESSAGES: AtomicUsize = AtomicUsize::new(0);
    static QA_TRACE_ARMED: AtomicBool = AtomicBool::new(false);
    thread_local! { static QA_TRACE_CURRENT: Cell<usize> = const { Cell::new(0) }; }

    struct QaTrace {
        id: usize,
        parent: usize,
        name: &'static str,
        window: HWND,
    }

    impl QaTrace {
        fn begin(window: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> Option<Self> {
            let name = qa_activation_message(message)?;
            if std::env::var_os("MURMUR_QA_WAV").is_none() {
                return None;
            }
            let prior = QA_TRACE_MESSAGES
                .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |count| {
                    Some(count.saturating_add(1).min(QA_ACTIVATION_TRACE_LIMIT + 1))
                })
                .unwrap_or(QA_ACTIVATION_TRACE_LIMIT + 1);
            let (id, cap_reached) = match qa_trace_slot(true, prior) {
                QaTraceSlot::Record { id, cap_reached } => (id, cap_reached),
                QaTraceSlot::FirstDropped => {
                    tracing::info!(target: "murmur::qa_overlay_activation",
                                   cap = QA_ACTIVATION_TRACE_LIMIT, dropped_at_least = 1,
                                   "QA overlay activation trace cap reached");
                    return None;
                }
                _ => return None,
            };
            let parent = QA_TRACE_CURRENT
                .try_with(|current| qa_trace_enter(current, id))
                .unwrap_or(0);
            let trace = Self {
                id,
                parent,
                name,
                window,
            };
            // lparam is a peer HWND for exactly these three messages. No title or text is read.
            let peer = lparam as HWND;
            unsafe {
                tracing::info!(target: "murmur::qa_overlay_activation",
                               id, parent, phase = "entry", message = name, hwnd = ?window,
                               peer = ?peer, state = wparam & 0xFFFF,
                               thread = GetCurrentThreadId(),
                               foreground = ?GetForegroundWindow(), focus = ?GetFocus(),
                               cap_reached, "QA overlay activation trace");
            }
            Some(trace)
        }

        fn handback(&self, succeeded: bool) {
            unsafe {
                tracing::info!(target: "murmur::qa_overlay_activation",
                               id = self.id, parent = self.parent, phase = "handback",
                               message = self.name, hwnd = ?self.window, succeeded,
                               foreground = ?GetForegroundWindow(), focus = ?GetFocus(),
                               "QA overlay activation trace");
            }
        }

        fn finish(self, result: LRESULT, handback_attempted: bool) {
            unsafe {
                tracing::info!(target: "murmur::qa_overlay_activation",
                               id = self.id, parent = self.parent, phase = "exit",
                               message = self.name, hwnd = ?self.window,
                               result, handback_attempted,
                               foreground = ?GetForegroundWindow(), focus = ?GetFocus(),
                               "QA overlay activation trace");
            }
            let _ = QA_TRACE_CURRENT.try_with(|current| qa_trace_exit(current, self.parent));
        }
    }

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
        let trace = QaTrace::begin(window, message, wparam, lparam);
        // Only WM_ACTIVATE carries a window in lparam; never probe it for other messages.
        let previous = lparam as HWND;
        let previous_is_window =
            message == WM_ACTIVATE && !previous.is_null() && IsWindow(previous) != 0;
        let mut handback_attempted = false;
        let result = dispatch_activation(
            message,
            wparam,
            previous_is_window,
            || {
                // The click gave Murmur the foreground right; hand it straight back.
                handback_attempted = true;
                let succeeded = SetForegroundWindow(previous) != 0;
                if let Some(trace) = &trace {
                    trace.handback(succeeded);
                }
            },
            || {
                if message == WM_NCDESTROY {
                    RemoveWindowSubclass(window, Some(overlay_proc), id);
                }
                DefSubclassProc(window, message, wparam, lparam)
            },
        );
        if let Some(trace) = trace {
            trace.finish(result, handback_attempted);
        }
        result
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
        if top_level
            && std::env::var_os("MURMUR_QA_WAV").is_some()
            && !QA_TRACE_ARMED.swap(true, Ordering::Relaxed)
        {
            tracing::info!(target: "murmur::qa_overlay_activation", top_level, children,
                           cap = QA_ACTIVATION_TRACE_LIMIT,
                           "QA overlay activation trace armed");
        }
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
    fn qa_activation_trace_is_off_by_default_and_bounded_when_enabled() {
        assert_eq!(qa_trace_slot(false, 0), QaTraceSlot::Off);
        assert_eq!(
            qa_trace_slot(false, QA_ACTIVATION_TRACE_LIMIT),
            QaTraceSlot::Off
        );
        assert_eq!(
            qa_trace_slot(true, 0),
            QaTraceSlot::Record {
                id: 1,
                cap_reached: false
            }
        );
        assert_eq!(
            qa_trace_slot(true, QA_ACTIVATION_TRACE_LIMIT - 1),
            QaTraceSlot::Record {
                id: QA_ACTIVATION_TRACE_LIMIT,
                cap_reached: true
            }
        );
        assert_eq!(
            qa_trace_slot(true, QA_ACTIVATION_TRACE_LIMIT),
            QaTraceSlot::FirstDropped
        );
        assert_eq!(
            qa_trace_slot(true, QA_ACTIVATION_TRACE_LIMIT + 1),
            QaTraceSlot::Dropped
        );
    }

    #[test]
    fn qa_trace_nesting_restores_parent_in_exit_order() {
        let current = Cell::new(0);
        let outer_parent = qa_trace_enter(&current, 1);
        assert_eq!(outer_parent, 0);
        let focus_parent = qa_trace_enter(&current, 2);
        assert_eq!(focus_parent, 1);
        qa_trace_exit(&current, focus_parent);
        assert_eq!(current.get(), 1);
        qa_trace_exit(&current, outer_parent);
        assert_eq!(current.get(), 0);
    }

    #[test]
    fn handled_activation_can_nest_focus_before_its_terminal_exit() {
        use std::cell::RefCell;
        let current = Cell::new(0);
        let phases = RefCell::new(Vec::new());
        let outer_parent = qa_trace_enter(&current, 1);
        phases.borrow_mut().push("activation entry");
        let result = dispatch_activation(
            WM_ACTIVATE,
            1,
            true,
            || {
                phases.borrow_mut().push("handback attempt");
                let focus_parent = qa_trace_enter(&current, 2);
                assert_eq!(focus_parent, 1);
                phases.borrow_mut().push("focus entry");
                phases.borrow_mut().push("focus exit");
                qa_trace_exit(&current, focus_parent);
                phases.borrow_mut().push("handback result");
            },
            || panic!("handled activation must not forward"),
        );
        phases.borrow_mut().push("activation exit");
        qa_trace_exit(&current, outer_parent);
        assert_eq!(result, 0);
        assert_eq!(current.get(), 0);
        assert_eq!(
            phases.into_inner(),
            [
                "activation entry",
                "handback attempt",
                "focus entry",
                "focus exit",
                "handback result",
                "activation exit"
            ]
        );
    }

    #[test]
    fn activation_trace_selection_does_not_change_message_replies() {
        assert_eq!(qa_activation_message(WM_ACTIVATE), Some("WM_ACTIVATE"));
        assert_eq!(qa_activation_message(WM_SETFOCUS), Some("WM_SETFOCUS"));
        assert_eq!(qa_activation_message(WM_KILLFOCUS), Some("WM_KILLFOCUS"));
        assert_eq!(qa_activation_message(WM_MOUSEACTIVATE), None);
        for message in [WM_ACTIVATE, WM_SETFOCUS, WM_KILLFOCUS, WM_LBUTTONDOWN] {
            assert_eq!(mouse_activate_reply(message), None);
            assert_eq!(
                dispatch_activation(
                    message,
                    WA_INACTIVE,
                    false,
                    || panic!("unexpected hand-back"),
                    || 99
                ),
                99
            );
        }
        assert_eq!(mouse_activate_reply(WM_MOUSEACTIVATE), Some(MA_NOACTIVATE));
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
