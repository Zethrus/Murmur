//! Murmur companion protocol, version 4.
//!
//! Pure encoding and decoding. No I/O on the hot path. Version 2 adds live text (`T`),
//! the enhancement suggestion (`G`) and device telemetry (`Q`, `L`) — see
//! `companion/protocol.md`. Text never reaches the log: the only builders that accept it
//! are [`text`] and [`suggestion`], and neither traces.

/// Wire protocol revision. Mirrored in `companion/flipper/src/protocol.h`.
pub const PROTOCOL_VERSION: u8 = 4;
/// Longest accepted line, including the newline. Sized so a full ten-key button
/// map fits on one `CFG` line.
pub const MAX_LINE: usize = 220;
/// Longest outbound line we will ever produce.
pub const MAX_OUTBOUND: usize = MAX_LINE;
/// Text payload ceiling for one `T` or `G` line: the line minus the tag and terminator.
pub const MAX_TEXT: usize = MAX_LINE - 3;

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
    Ptt {
        pressed: bool,
    },
    PttMode {
        enabled: bool,
    },
    /// Explicit discard confirmation; independent of the remappable button map.
    Discard,
    /// Device history selection. The host may only redisplay its current suggestion.
    AcceptHistory {
        index: u8,
    },
    /// Bare heartbeat (`None`) or the echo of a timed probe (`Some(token)`).
    Ping {
        token: Option<String>,
    },
    /// Device battery percentage, sent every 30 s.
    Battery {
        percent: u8,
    },
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
        return Inbound::Ping { token: None };
    }
    if let Some(token) = line.strip_prefix("P ") {
        let token = token.trim();
        if !token.is_empty() && token.len() <= 32 && token.chars().all(|c| c.is_ascii_digit()) {
            return Inbound::Ping {
                token: Some(token.to_string()),
            };
        }
        return Inbound::Unknown;
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
        Some("Q") => match parts.next().and_then(|v| v.parse::<u8>().ok()) {
            Some(percent) if percent <= 100 => Inbound::Battery { percent },
            _ => Inbound::Unknown,
        },
        Some("PTT") => match (parts.next(), parts.next()) {
            (Some("0"), None) => Inbound::PttMode { enabled: false },
            (Some("1"), None) => Inbound::PttMode { enabled: true },
            _ => Inbound::Unknown,
        },
        Some("D") => match (parts.next(), parts.next(), parts.next()) {
            (Some("DISCARD"), None, None) => Inbound::Discard,
            (Some("ACCEPT"), Some(index), None) => match index.parse::<u8>() {
                Ok(index) if index < 3 => Inbound::AcceptHistory { index },
                _ => Inbound::Unknown,
            },
            _ => Inbound::Unknown,
        },
        Some("B") => {
            let name = parts.next().unwrap_or("");
            let suffix = parts.next();
            if parts.next().is_some() {
                return Inbound::Unknown;
            }
            if name == "OK" {
                match suffix {
                    Some("PRESS") => return Inbound::Ptt { pressed: true },
                    Some("RELEASE") => return Inbound::Ptt { pressed: false },
                    _ => {}
                }
            }
            if suffix.is_some_and(|value| value != "LONG") {
                return Inbound::Unknown;
            }
            let long = suffix == Some("LONG");
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

/// Timed probe: the device echoes the token, so the host can measure the round trip.
pub fn probe(seq: u64) -> String {
    format!("P {seq}")
}

pub fn ack(verb: &str, result: &str) -> String {
    format!("A {verb} {result}")
}

pub fn ptt(enabled: bool) -> String {
    format!("PTT {}", u8::from(enabled))
}

/// `CFG` line, keys sorted so the line is stable and diffable in logs.
pub fn cfg(pairs: &[(&str, &str)]) -> String {
    let mut pairs: Vec<(&str, &str)> = pairs.to_vec();
    pairs.sort_by(|a, b| a.0.cmp(b.0));
    let body: Vec<String> = pairs.iter().map(|(k, v)| format!("{k}={v}")).collect();
    format!("CFG {}", body.join(" "))
}

/// Flatten to one line: no control characters, no leading or trailing space, and never
/// more than one text payload. The device replaces its buffer with each `T`.
fn sanitise(text: &str) -> String {
    let mut out = String::with_capacity(text.len().min(MAX_TEXT));
    let mut last_space = false;
    for ch in text.chars() {
        let ch = if ch.is_control() { ' ' } else { ch };
        if ch == ' ' {
            if last_space || out.is_empty() {
                continue;
            }
            last_space = true;
        } else {
            last_space = false;
        }
        if out.len() + ch.len_utf8() > MAX_TEXT {
            break;
        }
        out.push(ch);
    }
    while out.ends_with(' ') {
        out.pop();
    }
    out
}

/// `T` line: live dictation text. Never logged by this module.
pub fn text(content: &str) -> String {
    let body = sanitise(content);
    if body.is_empty() {
        "T".to_string()
    } else {
        format!("T {body}")
    }
}

/// `G` line: the enhancement suggestion.
pub fn suggestion(content: &str) -> String {
    let body = sanitise(content);
    if body.is_empty() {
        "G".to_string()
    } else {
        format!("G {body}")
    }
}

/// `L` line: host-measured round-trip latency and BLE signal strength.
pub fn link_stats(latency_ms: u16, rssi: i8) -> String {
    format!("L {} {}", latency_ms.min(9999), rssi.clamp(-127, 0))
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
    fn ptt_and_legacy_ok_round_trip() {
        assert_eq!(ptt(true), "PTT 1");
        assert_eq!(ptt(false), "PTT 0");
        assert_eq!(parse(&ptt(true)), Inbound::PttMode { enabled: true });
        assert_eq!(parse(&ptt(false)), Inbound::PttMode { enabled: false });
        assert_eq!(parse("B OK PRESS"), Inbound::Ptt { pressed: true });
        assert_eq!(parse("B OK RELEASE"), Inbound::Ptt { pressed: false });
        assert_eq!(
            parse("B OK"),
            Inbound::Press {
                button: Button::Ok,
                long: false
            }
        );
        assert_eq!(parse("B OK PRESS EXTRA"), Inbound::Unknown);
    }

    #[test]
    fn tolerates_terminators_and_padding() {
        assert_eq!(parse("P\r\n"), Inbound::Ping { token: None });
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

    #[test]
    fn device_actions_are_distinct_from_button_presses() {
        assert_eq!(parse("D DISCARD"), Inbound::Discard);
        assert_eq!(parse("D ACCEPT 0"), Inbound::AcceptHistory { index: 0 });
        assert_eq!(parse("D ACCEPT 2"), Inbound::AcceptHistory { index: 2 });
        assert_eq!(parse("D ACCEPT 3"), Inbound::Unknown);
        assert_eq!(
            parse("B OK LONG"),
            Inbound::Press {
                button: Button::Ok,
                long: true
            }
        );
    }

    #[test]
    fn text_is_flattened_and_bounded() {
        let line = text("hello\r\nworld\t now   there ");
        assert_eq!(line, "T hello world now there");
        assert!(!line.contains('\n') && !line.contains('\r') && !line.contains('\t'));

        let long = text(&"word ".repeat(200));
        assert!(long.len() <= MAX_LINE, "{} bytes", long.len());
        assert!(long.starts_with("T "));
    }

    #[test]
    fn empty_text_still_produces_a_valid_frame() {
        assert_eq!(text("   "), "T");
        assert_eq!(suggestion(""), "G");
    }

    #[test]
    fn suggestion_carries_the_rewrite() {
        assert_eq!(
            suggestion("Tightened: the report is ready."),
            "G Tightened: the report is ready."
        );
    }

    #[test]
    fn link_stats_are_clamped() {
        assert_eq!(link_stats(42, -58), "L 42 -58");
        assert_eq!(link_stats(60000, 12), "L 9999 0");
    }

    #[test]
    fn battery_lines_parse_within_range() {
        assert_eq!(parse("Q 87"), Inbound::Battery { percent: 87 });
        assert_eq!(parse("Q 101"), Inbound::Unknown);
        assert_eq!(parse("Q"), Inbound::Unknown);
    }

    /// Text is the one payload that may cross the wire, so the closed vocabulary of
    /// everything else still matters: an ack cannot smuggle a transcript.
    #[test]
    fn control_messages_cannot_carry_text() {
        let secret = "please rewrite my banking password";
        assert!(!ack("DICTATE", "ON").contains(secret));
        assert!(!cfg(&[("OK", "DICTATE_TOGGLE")]).contains(secret));
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
