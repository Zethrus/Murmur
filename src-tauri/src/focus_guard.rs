//! Decide whether a finished dictation may go to the window that has focus now.
//!
//! Pure: callers read focus through `platform` and act on the decision. Windows are compared by
//! native identity (X11 window ID or HWND), so two windows of one application are different
//! targets. Tabs inside a single window cannot be told apart.
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
/// the focused window could not be identified, which never counts as a match.
pub fn decide(origin: Option<&Focus>, current: Option<&Focus>, mode: FocusMode) -> Decision {
    if same_target(origin, current) {
        return Decision::Deliver;
    }
    match mode {
        FocusMode::Insert => Decision::Deliver,
        FocusMode::Hold => Decision::Hold,
        FocusMode::Copy => Decision::Copy,
        FocusMode::Refocus => match origin {
            Some(Focus::Window { id, .. }) => Decision::Refocus(*id),
            // Nothing to return to.
            _ => Decision::Hold,
        },
    }
}

fn same_target(origin: Option<&Focus>, current: Option<&Focus>) -> bool {
    match (origin, current) {
        (Some(Focus::Window { id: a, .. }), Some(Focus::Window { id: b, .. })) => a == b,
        // Started and finished in Murmur: delivery skips its own windows, as before.
        (Some(Focus::Own), Some(Focus::Own)) => true,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn window(id: u64, class: &str) -> Focus {
        Focus::Window {
            id,
            class: class.into(),
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
    }

    #[test]
    fn mode_spellings_round_trip() {
        for (spelling, mode) in FocusMode::CHOICES.iter().zip(MODES) {
            assert_eq!(FocusMode::parse(spelling), mode);
        }
        assert_eq!(FocusMode::parse("unknown"), FocusMode::Hold);
    }
}
