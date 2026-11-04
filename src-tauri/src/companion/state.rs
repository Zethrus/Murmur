//! Map Murmur's live state onto the companion protocol's coarse phases.

use crate::{app::AppState, companion::protocol::Phase};

/// One phase for the Flipper's display. Dictation wins over a pending suggestion:
/// a suggestion that is still visible while you record the next sentence is not the
/// most useful thing to show.
pub fn phase(state: &AppState) -> Phase {
    if let Ok(status) = state.status() {
        match status.state.as_str() {
            "listening" => return Phase::Listen,
            "transcribing" => return Phase::Think,
            "error" => return Phase::Error,
            _ => {}
        }
    }
    if state.enhance.shortcut_suggestion().is_some() {
        Phase::Ready
    } else {
        Phase::Idle
    }
}
