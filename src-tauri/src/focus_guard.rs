//! Decide whether a finished dictation may go to the window that has focus now.
//!
//! Pure: callers read focus through `platform` and act on the decision. Windows are compared by
//! native identity (X11 window ID on Linux; HWND + PID + process start time on Windows), so two
//! windows of one application are different targets. Tabs inside a single window cannot be told
//! apart. A target Murmur cannot type into (elevated window, secure desktop) or cannot identify is
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
    let deliverable = matches!(current, Some(Focus::Window { .. } | Focus::Own));
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

/// Windows identities carry a PID, and then the process start time must be known too: a PID
/// without a start time cannot rule out a reused PID. Linux has neither (X11 IDs are not reused
/// while the window exists), so `None`/`None` still matches there.
fn same_target(origin: Option<&Focus>, current: Option<&Focus>) -> bool {
    match (origin, current) {
        (
            Some(Focus::Window {
                id: a,
                pid: pa,
                started: sa,
                ..
            }),
            Some(Focus::Window {
                id: b,
                pid: pb,
                started: sb,
                ..
            }),
        ) => a == b && pa == pb && sa == sb && (pa.is_none() || sa.is_some()),
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
            class: class.into(),
        }
    }

    fn windows_window(id: u64, pid: u32, started: u64) -> Focus {
        Focus::Window {
            id,
            pid: Some(pid),
            started: Some(started),
            class: "notepad.exe".into(),
        }
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
