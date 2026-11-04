//! Small, app-independent framing rules shared by the listener and network tests.
use tokio_tungstenite::tungstenite::protocol::{frame::coding::CloseCode, CloseFrame};

pub const MAX_LINE: usize = 220;
pub fn valid_line(line: &str) -> bool {
    !line.is_empty() && line.is_ascii() && line.len() + 1 <= MAX_LINE && !line.contains(['\r', '\n'])
}
/// Binary frames are reserved for the remote microphone increment.
pub fn binary_close() -> CloseFrame {
    CloseFrame {
        code: CloseCode::Unsupported,
        reason: "unsupported audio binary frame".into(),
    }
}
pub fn control_verb(line: &str) -> Option<&'static str> {
    let (kind, raw) = line.split_once(' ')?;
    match (kind, raw) {
        ("B", "dictate_toggle") => Some("dictate_toggle"),
        ("S", "enhance") => Some("enhance"),
        ("A", "accept") => Some("accept"),
        ("A", "dismiss") => Some("dismiss"),
        ("A", "cancel") => Some("cancel"),
        _ => None,
    }
}
pub fn state_line(dictation: &str, enhancement: &str) -> &'static str {
    match dictation {
        "listening" => "STATE listening",
        "transcribing" => "STATE transcribing",
        _ => match enhancement {
            "loading" | "rewriting" | "accepting" => "STATE enhancing",
            "ready" => "STATE suggestion",
            _ => "STATE idle",
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn framing_and_state_are_bounded() {
        assert!(valid_line(&"x".repeat(219)));
        assert!(!valid_line(&"x".repeat(220)));
        assert!(!valid_line("x\ny"));
        assert!(!valid_line("é"));
        assert_eq!(binary_close().code, CloseCode::Unsupported);
        assert!(binary_close().reason.contains("unsupported"));
        for line in [
            state_line("listening", "none"),
            state_line("idle", "ready"),
            state_line("idle", "rewriting"),
        ] {
            assert_eq!(line.split_ascii_whitespace().count(), 2);
            assert!(valid_line(line));
        }
    }
}
