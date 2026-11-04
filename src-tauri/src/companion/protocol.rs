//! Murmur companion protocol, version 1.
//!
//! Pure encoding and decoding. No I/O, no allocation on the hot path, and no message
//! carries dictation content — see `companion/protocol.md`.

/// Wire protocol revision. Mirrored in `companion/flipper/src/protocol.h`.
pub const PROTOCOL_VERSION: u8 = 1;
/// Longest accepted line, including the newline. Sized so a full ten-key button
/// map fits on one `CFG` line.
pub const MAX_LINE: usize = 160;
/// Longest outbound line we will ever produce.
pub const MAX_OUTBOUND: usize = MAX_LINE;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Button {
    Ok,
    Up,
    Down,
    Back,
    Left,
    Right,
}

impl Button {
    pub const ALL: [Button; 6] = [
        Button::Ok,
        Button::Up,
        Button::Down,
        Button::Back,
        Button::Left,
        Button::Right,
    ];
    /// Key used by the configuration map (`ok`, `up`, …).
    pub fn key(self) -> &'static str {
        match self {
            Button::Ok => "ok",
            Button::Up => "up",
            Button::Down => "down",
            Button::Back => "back",
            Button::Left => "left",
            Button::Right => "right",
        }
    }
    /// Long-press key (`ok_long`, `back_long`, …).
    pub fn long_key(self) -> String {
        format!("{}_long", self.key())
    }
    /// Name used on the wire (`OK`, `UP`, …).
    pub fn wire(self) -> &'static str {
        match self {
            Button::Ok => "OK",
            Button::Up => "UP",
            Button::Down => "DOWN",
            Button::Back => "BACK",
            Button::Left => "LEFT",
            Button::Right => "RIGHT",
        }
    }
    pub fn parse(name: &str) -> Option<Button> {
        Button::ALL.into_iter().find(|b| b.wire() == name)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Idle,
    Listen,
    Think,
    Ready,
    Error,
}

impl Phase {
    pub fn line(self) -> &'static str {
        match self {
            Phase::Idle => "S IDLE",
            Phase::Listen => "S LISTEN",
            Phase::Think => "S THINK",
            Phase::Ready => "S READY",
            Phase::Error => "S ERROR companion",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Inbound {
    /// Peer announced itself. `app` is a display version, never used for decisions.
    Hello {
        protocol: u8,
        app: String,
        role: String,
    },
    Press {
        button: Button,
        long: bool,
    },
    Ping,
    /// Well-formed but unrecognised: ignored for forward compatibility.
    Unknown,
}

/// Decode one line (without its terminator). Never panics, never fails:
/// anything unrecognised becomes `Inbound::Unknown`.
pub fn parse(line: &str) -> Inbound {
    let line = line.trim_matches(|c| c == '\r' || c == '\n' || c == ' ' || c == '\t');
    if line.is_empty() || line.len() > MAX_LINE {
        return Inbound::Unknown;
    }
    if line == "P" {
        return Inbound::Ping;
    }
    let mut parts = line.split(' ');
    match parts.next() {
        Some("HELLO") => {
            let protocol = parts.next().and_then(|v| v.parse::<u8>().ok());
            let app = parts.next().unwrap_or("").to_string();
            let role = parts.next().unwrap_or("").to_string();
            match protocol {
                Some(protocol) if protocol <= 99 && !app.is_empty() => Inbound::Hello {
                    protocol,
                    app,
                    role,
                },
                _ => Inbound::Unknown,
            }
        }
        Some("B") => {
            let name = parts.next().unwrap_or("");
            let long = parts.next() == Some("LONG");
            match Button::parse(name) {
                Some(button) => Inbound::Press { button, long },
                None => Inbound::Unknown,
            }
        }
        _ => Inbound::Unknown,
    }
}

pub fn hello(role: &str, app: &str) -> String {
    format!("HELLO {PROTOCOL_VERSION} {app} {role}")
}

pub fn ping() -> String {
    "P".to_string()
}

pub fn ack(verb: &str, result: &str) -> String {
    format!("A {verb} {result}")
}

/// `CFG` line, keys sorted so the line is stable and diffable in logs.
pub fn cfg(pairs: &[(&str, &str)]) -> String {
    let mut pairs: Vec<(&str, &str)> = pairs.to_vec();
    pairs.sort_by(|a, b| a.0.cmp(b.0));
    let body: Vec<String> = pairs.iter().map(|(k, v)| format!("{k}={v}")).collect();
    format!("CFG {}", body.join(" "))
}

/// One outbound line, terminated and length-checked.
pub fn frame(line: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(line.len() + 1);
    out.extend_from_slice(line.as_bytes());
    out.push(b'\n');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_hello() {
        assert_eq!(
            parse("HELLO 1 0.2.2 flipper"),
            Inbound::Hello {
                protocol: 1,
                app: "0.2.2".into(),
                role: "flipper".into()
            }
        );
    }

    #[test]
    fn rejects_a_malformed_hello() {
        assert_eq!(parse("HELLO x 0.2.2 flipper"), Inbound::Unknown);
        assert_eq!(parse("HELLO 1"), Inbound::Unknown);
    }

    #[test]
    fn parses_short_and_long_presses() {
        assert_eq!(
            parse("B OK"),
            Inbound::Press {
                button: Button::Ok,
                long: false
            }
        );
        assert_eq!(
            parse("B BACK LONG"),
            Inbound::Press {
                button: Button::Back,
                long: true
            }
        );
        assert_eq!(parse("B SIDEWAYS"), Inbound::Unknown);
    }

    #[test]
    fn tolerates_terminators_and_padding() {
        assert_eq!(parse("P\r\n"), Inbound::Ping);
        assert_eq!(
            parse("  B UP \t"),
            Inbound::Press {
                button: Button::Up,
                long: false
            }
        );
        assert_eq!(parse(""), Inbound::Unknown);
    }

    #[test]
    fn ignores_unknown_and_oversized_lines() {
        assert_eq!(parse("X Y Z"), Inbound::Unknown);
        let long = format!("B {}", "O".repeat(MAX_LINE));
        assert_eq!(parse(&long), Inbound::Unknown);
    }

    #[test]
    fn cfg_is_sorted_and_round_trips_through_the_wire_format() {
        let line = cfg(&[
            ("UP", "ENHANCE"),
            ("BACK", "DISMISS"),
            ("OK", "DICTATE_TOGGLE"),
        ]);
        assert_eq!(line, "CFG BACK=DISMISS OK=DICTATE_TOGGLE UP=ENHANCE");
        assert!(line.len() <= MAX_LINE, "{line}");
    }

    #[test]
    fn outbound_frames_fit_the_limit() {
        for line in [
            hello("host", "0.2.2"),
            ping(),
            ack("DICTATE", "ON"),
            Phase::Think.line().to_string(),
            cfg(&[
                ("OK", "DICTATE_TOGGLE"),
                ("UP", "ENHANCE"),
                ("DOWN", "ACCEPT"),
                ("BACK", "DISMISS"),
                ("OK_LONG", "CANCEL"),
                ("BACK_LONG", "NONE"),
            ]),
        ] {
            assert!(line.len() < MAX_LINE, "{line} is {} bytes", line.len());
            assert!(frame(&line).ends_with(b"\n"));
        }
    }

    #[test]
    fn every_button_round_trips_through_its_wire_name() {
        for button in Button::ALL {
            assert_eq!(Button::parse(button.wire()), Some(button));
            assert_eq!(
                parse(&format!("B {}", button.wire())),
                Inbound::Press {
                    button,
                    long: false
                }
            );
            assert!(button.long_key().ends_with("_long"));
        }
    }

    /// The protocol must never carry dictation text: every outbound line is a fixed
    /// vocabulary, so a sentence cannot survive a round trip.
    #[test]
    fn outbound_vocabulary_cannot_carry_text() {
        let secret = "please rewrite my banking password";
        let line = ack("DICTATE", "ON");
        assert!(!line.contains(secret));
        assert!(!cfg(&[("OK", "DICTATE_TOGGLE")]).contains(secret));
        assert!(phase_lines_are_closed());
    }

    fn phase_lines_are_closed() -> bool {
        [
            Phase::Idle,
            Phase::Listen,
            Phase::Think,
            Phase::Ready,
            Phase::Error,
        ]
        .into_iter()
        .all(|p| p.line().starts_with("S "))
    }
}
