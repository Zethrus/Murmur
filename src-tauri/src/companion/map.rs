//! Flipper button to Murmur action mapping.
//!
//! The mapping mirrors the global shortcuts: enhance is press-only and skipped while a
//! dictation is in progress, accept acts on the suggestion observed at press time.

use crate::{app::AppState, companion::protocol::Button, controller::Action};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verb {
    DictateToggle,
    Enhance,
    Accept,
    Dismiss,
    Cancel,
    None,
}

impl Verb {
    pub const ALL: [Verb; 6] = [
        Verb::DictateToggle,
        Verb::Enhance,
        Verb::Accept,
        Verb::Dismiss,
        Verb::Cancel,
        Verb::None,
    ];
    /// Configuration spelling, also used as the `A <verb>` token on the wire.
    pub fn as_str(self) -> &'static str {
        match self {
            Verb::DictateToggle => "dictate_toggle",
            Verb::Enhance => "enhance",
            Verb::Accept => "accept",
            Verb::Dismiss => "dismiss",
            Verb::Cancel => "cancel",
            Verb::None => "none",
        }
    }
    /// `CFG` spelling: uppercase, no underscores ambiguity beyond the documented set.
    pub fn wire(self) -> &'static str {
        match self {
            Verb::DictateToggle => "DICTATE_TOGGLE",
            Verb::Enhance => "ENHANCE",
            Verb::Accept => "ACCEPT",
            Verb::Dismiss => "DISMISS",
            Verb::Cancel => "CANCEL",
            Verb::None => "NONE",
        }
    }
    pub fn parse(value: &str) -> Option<Verb> {
        let value = value.trim().to_ascii_lowercase();
        Verb::ALL.into_iter().find(|v| v.as_str() == value)
    }
}

/// Look the configured verb up for a physical button.
pub fn verb_for(buttons: &crate::config::CompanionButtons, button: Button, long: bool) -> Verb {
    let key = if long {
        button.long_key()
    } else {
        button.key().to_string()
    };
    let raw = match key.as_str() {
        "ok" => &buttons.ok,
        "up" => &buttons.up,
        "down" => &buttons.down,
        "back" => &buttons.back,
        "left" => &buttons.left,
        "right" => &buttons.right,
        "ok_long" => &buttons.ok_long,
        "up_long" => &buttons.up_long,
        "down_long" => &buttons.down_long,
        _ => return Verb::None,
    };
    Verb::parse(raw).unwrap_or(Verb::None)
}

/// Run a verb. Returns the acknowledgement token for the wire, if anything happened.
pub fn dispatch(state: &AppState, verb: Verb) -> Option<&'static str> {
    let busy = state
        .status()
        .is_ok_and(|s| matches!(s.state.as_str(), "listening" | "transcribing"));
    let result = match verb {
        Verb::DictateToggle => {
            if busy {
                state.send(Action::Stop).ok().map(|_| "OFF")
            } else {
                state.send(Action::Start).ok().map(|_| "ON")
            }
        }
        Verb::Enhance => {
            if busy {
                return None;
            }
            state
                .send(Action::Rewrite(state.enhance.shortcut_context()))
                .ok()
                .map(|_| "STARTED")
        }
        Verb::Accept => state
            .send(Action::AcceptCurrent(state.enhance.shortcut_suggestion()))
            .ok()
            .map(|_| "OK"),
        Verb::Dismiss => match state.enhance.shortcut_suggestion() {
            Some(id) => {
                state.enhance.discard(state, Some(id));
                Some("OK")
            }
            None => None,
        },
        Verb::Cancel => state.send(Action::Cancel).ok().map(|_| "OK"),
        Verb::None => None,
    };
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::CompanionButtons;

    #[test]
    fn verbs_round_trip_through_config_and_wire_spellings() {
        for verb in Verb::ALL {
            assert_eq!(Verb::parse(verb.as_str()), Some(verb));
            assert_eq!(Verb::parse(&verb.as_str().to_uppercase()), Some(verb));
            assert_eq!(Verb::parse(verb.wire()), Some(verb));
        }
    }

    #[test]
    fn unknown_verbs_are_rejected_rather_than_guessed() {
        assert!(Verb::parse("banish").is_none());
        assert!(Verb::parse("").is_none());
        assert_eq!(
            verb_for(&CompanionButtons::default(), Button::Ok, true),
            Verb::Cancel
        );
    }

    #[test]
    fn default_map_matches_the_documented_layout() {
        let b = CompanionButtons::default();
        assert_eq!(verb_for(&b, Button::Ok, false), Verb::DictateToggle);
        assert_eq!(verb_for(&b, Button::Up, false), Verb::Enhance);
        assert_eq!(verb_for(&b, Button::Down, false), Verb::Accept);
        assert_eq!(verb_for(&b, Button::Back, false), Verb::Dismiss);
    }

    #[test]
    fn long_press_uses_the_long_key() {
        let mut b = CompanionButtons::default();
        b.up_long = "accept".into();
        assert_eq!(verb_for(&b, Button::Up, false), Verb::Enhance);
        assert_eq!(verb_for(&b, Button::Up, true), Verb::Accept);
    }
}
