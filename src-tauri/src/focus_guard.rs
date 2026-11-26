//! Decide whether a finished dictation may go to the window that has focus now.
//!
//! Pure: callers read focus through `platform` and act on the decision. Windows are compared by
//! native identity (X11 window ID on Linux; foreground and keyboard-child identity on Windows),
//! so two windows or focused controls of one application are different targets. Tabs within one
//! HWND still cannot be told apart. A target Murmur cannot type into or cannot identify is
//! never typed into: see docs/focus-contract.md.
use crate::platform::Focus;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FocusMode {
    /// Keep the transcript for review in the overlay.
    Hold,
    /// Bring the original window back, then deliver; hold if that fails.
    Refocus,
    /// Copy the transcript to the clipboard without typing it anywhere.
    Copy,
    /// Deliver to whatever has focus (behaviour before focus binding).
    Insert,
}
impl FocusMode {
    /// Configuration spellings accepted by `injection.focus_change`.
    pub const CHOICES: &'static [&'static str] = &["hold", "refocus", "copy", "insert"];
    /// Unknown spellings are rejected by config validation; fall back to the safest mode.
    pub fn parse(value: &str) -> Self {
        match value {
            "refocus" => FocusMode::Refocus,
            "copy" => FocusMode::Copy,
            "insert" => FocusMode::Insert,
            _ => FocusMode::Hold,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    Deliver,
    Hold,
    Refocus(u64),
    Copy,
}

/// `origin` is the focus when dictation started, `current` the focus at delivery. `None` means
/// the focused window could not be identified, which never counts as a match. A current focus
/// that is unknown or blocked is never typed into, even in Insert mode; Copy still copies and
/// Refocus still tries the original window (it is re-checked before anything is typed).
pub fn decide(origin: Option<&Focus>, current: Option<&Focus>, mode: FocusMode) -> Decision {
    if same_target(origin, current) {
        return Decision::Deliver;
    }
    let deliverable = current.is_some_and(valid_target);
    match mode {
        FocusMode::Insert if deliverable => Decision::Deliver,
        FocusMode::Insert | FocusMode::Hold => Decision::Hold,
        FocusMode::Copy => Decision::Copy,
        FocusMode::Refocus => match origin {
            Some(Focus::Window { id, .. }) => Decision::Refocus(*id),
            // Nothing to return to.
            _ => Decision::Hold,
        },
    }
}

/// Windows foreground and keyboard-child identities must both be complete. A child can belong to
/// an embedded renderer in another process. Linux retains its X11-window-only identity.
fn valid_target(focus: &Focus) -> bool {
    match focus {
        Focus::Own => true,
        Focus::Blocked { .. } => false,
        Focus::Window {
            pid: None,
            started: None,
            keyboard: None,
            ..
        } => true,
        Focus::Window {
            pid: Some(pid),
            started: Some(_),
            keyboard: Some(child),
            ..
        } => *pid != 0 && child.id != 0 && child.thread != 0 && child.pid != 0,
        _ => false,
    }
}
fn same_target(origin: Option<&Focus>, current: Option<&Focus>) -> bool {
    match (origin, current) {
        (
            Some(Focus::Window {
                id: a,
                pid: pa,
                started: sa,
                keyboard: ka,
                ..
            }),
            Some(Focus::Window {
                id: b,
                pid: pb,
                started: sb,
                keyboard: kb,
                ..
            }),
        ) => {
            a == b
                && pa == pb
                && sa == sb
                && ka == kb
                && origin.is_some_and(valid_target)
                && current.is_some_and(valid_target)
        }
        // Blocked targets never match, so even an unchanged elevated window is held.
        // Started and finished in Murmur: delivery skips its own windows, as before.
        (Some(Focus::Own), Some(Focus::Own)) => true,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::platform::Blocked;

    fn window(id: u64, class: &str) -> Focus {
        Focus::Window {
            id,
            pid: None,
            started: None,
            keyboard: None,
            class: class.into(),
        }
    }

    fn windows_window(id: u64, pid: u32, started: u64) -> Focus {
        Focus::Window {
            id,
            pid: Some(pid),
            started: Some(started),
            keyboard: Some(crate::platform::KeyboardTarget {
                id: id + 1,
                thread: 20,
                pid,
                started,
            }),
            class: "notepad.exe".into(),
        }
    }

    #[test]
    fn windows_same_child_delivers_but_another_child_holds() {
        let editor = windows_window(0x1234, 100, 5);
        assert_eq!(
            decide(Some(&editor), Some(&editor), FocusMode::Hold),
            Decision::Deliver
        );
        let mut menu = editor.clone();
        if let Focus::Window {
            keyboard: Some(child),
            ..
        } = &mut menu
        {
            child.id += 1;
        }
        assert_eq!(
            decide(Some(&editor), Some(&menu), FocusMode::Hold),
            Decision::Hold
        );
        assert_eq!(
            decide(Some(&editor), Some(&menu), FocusMode::Refocus),
            Decision::Refocus(0x1234)
        );
        assert_eq!(
            decide(Some(&editor), Some(&menu), FocusMode::Copy),
            Decision::Copy
        );
        assert_eq!(
            decide(Some(&editor), Some(&menu), FocusMode::Insert),
            Decision::Deliver
        );
    }

    #[test]
    fn windows_missing_or_reused_child_never_matches() {
        let editor = windows_window(0x1234, 100, 5);
        let mut missing = editor.clone();
        if let Focus::Window { keyboard, .. } = &mut missing {
            *keyboard = None;
        }
        assert_eq!(
            decide(Some(&editor), Some(&missing), FocusMode::Hold),
            Decision::Hold
        );
        assert_eq!(
            decide(Some(&editor), Some(&missing), FocusMode::Insert),
            Decision::Hold
        );
        let mut reused = editor.clone();
        if let Focus::Window {
            keyboard: Some(child),
            ..
        } = &mut reused
        {
            child.thread += 1;
        }
        assert_eq!(
            decide(Some(&editor), Some(&reused), FocusMode::Hold),
            Decision::Hold
        );
        if let Focus::Window {
            keyboard: Some(child),
            ..
        } = &mut reused
        {
            child.pid -= 1;
            child.started += 1;
        }
        assert_eq!(
            decide(Some(&editor), Some(&reused), FocusMode::Hold),
            Decision::Hold
        );
        if let Focus::Window {
            keyboard: Some(child),
            ..
        } = &mut reused
        {
            child.thread -= 1;
            child.pid += 1;
        }
        assert_eq!(
            decide(Some(&editor), Some(&reused), FocusMode::Hold),
            Decision::Hold
        );
    }

    #[test]
    fn verified_foreign_process_child_can_match() {
        let mut editor = windows_window(0x1234, 100, 5);
        if let Focus::Window {
            keyboard: Some(child),
            ..
        } = &mut editor
        {
            child.pid = 200;
            child.started = 6;
        }
        assert_eq!(
            decide(Some(&editor), Some(&editor), FocusMode::Hold),
            Decision::Deliver
        );
    }

    fn blocked(reason: Blocked) -> Focus {
        Focus::Blocked {
            reason,
            class: "notepad.exe".into(),
        }
    }

    const MODES: [FocusMode; 4] = [
        FocusMode::Hold,
        FocusMode::Refocus,
        FocusMode::Copy,
        FocusMode::Insert,
    ];

    #[test]
    fn same_window_always_delivers() {
        let editor = window(7, "code");
        for mode in MODES {
            assert_eq!(
                decide(Some(&editor), Some(&window(7, "code")), mode),
                Decision::Deliver
            );
            assert_eq!(
                decide(Some(&Focus::Own), Some(&Focus::Own), mode),
                Decision::Deliver
            );
        }
    }

    #[test]
    fn different_window_follows_mode() {
        let notes = window(7, "obsidian");
        let terminal = window(9, "kitty");
        let cases = [
            (FocusMode::Hold, Decision::Hold),
            (FocusMode::Refocus, Decision::Refocus(7)),
            (FocusMode::Copy, Decision::Copy),
            (FocusMode::Insert, Decision::Deliver),
        ];
        for (mode, expected) in cases {
            assert_eq!(decide(Some(&notes), Some(&terminal), mode), expected);
        }
    }

    #[test]
    fn two_windows_of_one_app_are_different_targets() {
        assert_eq!(
            decide(
                Some(&window(1, "kitty")),
                Some(&window(2, "kitty")),
                FocusMode::Hold
            ),
            Decision::Hold
        );
    }

    #[test]
    fn unknown_or_own_start_is_a_mismatch() {
        let editor = window(3, "code");
        for origin in [None, Some(&Focus::Own)] {
            assert_eq!(
                decide(origin, Some(&editor), FocusMode::Hold),
                Decision::Hold
            );
            assert_eq!(
                decide(origin, Some(&editor), FocusMode::Copy),
                Decision::Copy
            );
            // Refocus has no window to return to.
            assert_eq!(
                decide(origin, Some(&editor), FocusMode::Refocus),
                Decision::Hold
            );
            assert_eq!(
                decide(origin, Some(&editor), FocusMode::Insert),
                Decision::Deliver
            );
        }
    }

    #[test]
    fn unknown_current_focus_is_a_mismatch() {
        let editor = window(3, "code");
        assert_eq!(decide(Some(&editor), None, FocusMode::Hold), Decision::Hold);
        assert_eq!(
            decide(Some(&editor), None, FocusMode::Refocus),
            Decision::Refocus(3)
        );
        assert_eq!(decide(None, None, FocusMode::Hold), Decision::Hold);
        assert_eq!(decide(Some(&editor), None, FocusMode::Copy), Decision::Copy);
    }

    #[test]
    fn insert_mode_never_types_into_an_unknown_target() {
        let editor = window(3, "code");
        assert_eq!(
            decide(Some(&editor), None, FocusMode::Insert),
            Decision::Hold
        );
        assert_eq!(decide(None, None, FocusMode::Insert), Decision::Hold);
    }

    #[test]
    fn windows_identity_without_start_time_never_matches() {
        let unknown_start = |pid| Focus::Window {
            id: 0x1234,
            pid: Some(pid),
            started: None,
            keyboard: None,
            class: "notepad.exe".into(),
        };
        // GetProcessTimes failed both times: the PID may have been reused, so this is not the
        // same target (Insert mode still types into whatever has focus, as for any mismatch).
        let origin = unknown_start(100);
        assert_eq!(
            decide(Some(&origin), Some(&unknown_start(100)), FocusMode::Hold),
            Decision::Hold
        );
        assert_eq!(
            decide(Some(&origin), Some(&unknown_start(100)), FocusMode::Refocus),
            Decision::Refocus(0x1234)
        );
        assert_eq!(
            decide(
                Some(&origin),
                Some(&windows_window(0x1234, 100, 5)),
                FocusMode::Hold
            ),
            Decision::Hold
        );
        // Linux identities (no PID, no start time) are unchanged.
        assert_eq!(
            decide(
                Some(&window(7, "code")),
                Some(&window(7, "code")),
                FocusMode::Hold
            ),
            Decision::Deliver
        );
    }

    #[test]
    fn reused_hwnd_with_another_process_is_a_different_target() {
        let origin = windows_window(0x1234, 100, 5);
        // Same HWND, same PID, same start time: the same window.
        assert_eq!(
            decide(
                Some(&origin),
                Some(&windows_window(0x1234, 100, 5)),
                FocusMode::Hold
            ),
            Decision::Deliver
        );
        // HWND reused by another process.
        assert_eq!(
            decide(
                Some(&origin),
                Some(&windows_window(0x1234, 200, 5)),
                FocusMode::Hold
            ),
            Decision::Hold
        );
        // PID reused by a new process (different start time).
        assert_eq!(
            decide(
                Some(&origin),
                Some(&windows_window(0x1234, 100, 6)),
                FocusMode::Hold
            ),
            Decision::Hold
        );
        // Start time unreadable at one end: not provably the same.
        let unknown_start = Focus::Window {
            id: 0x1234,
            pid: Some(100),
            started: None,
            keyboard: None,
            class: "notepad.exe".into(),
        };
        assert_eq!(
            decide(Some(&origin), Some(&unknown_start), FocusMode::Hold),
            Decision::Hold
        );
    }

    #[test]
    fn blocked_targets_are_never_typed_into() {
        for reason in [
            Blocked::Elevated,
            Blocked::Unreadable,
            Blocked::SecureDesktop,
            Blocked::Menu,
        ] {
            let target = blocked(reason);
            // Dictation started and ended in the same elevated window: still held.
            assert_eq!(
                decide(Some(&target), Some(&target), FocusMode::Hold),
                Decision::Hold
            );
            assert_eq!(
                decide(Some(&target), Some(&target), FocusMode::Insert),
                Decision::Hold
            );
            assert_eq!(
                decide(Some(&target), Some(&target), FocusMode::Copy),
                Decision::Copy
            );
            // Nothing to return to from a blocked origin.
            assert_eq!(
                decide(Some(&target), Some(&target), FocusMode::Refocus),
                Decision::Hold
            );
            // Normal origin, blocked at delivery (UAC prompt appeared).
            let editor = windows_window(9, 100, 5);
            assert_eq!(
                decide(Some(&editor), Some(&target), FocusMode::Insert),
                Decision::Hold
            );
            assert_eq!(
                decide(Some(&editor), Some(&target), FocusMode::Refocus),
                Decision::Refocus(9)
            );
        }
    }

    #[test]
    fn mode_spellings_round_trip() {
        for (spelling, mode) in FocusMode::CHOICES.iter().zip(MODES) {
            assert_eq!(FocusMode::parse(spelling), mode);
        }
        assert_eq!(FocusMode::parse("unknown"), FocusMode::Hold);
    }
}
