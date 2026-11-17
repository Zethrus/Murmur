#![cfg_attr(windows, windows_subsystem = "windows")]
//! Diagnostic reproducer for Murmur's Windows overlay drag (docs/parity/webview2-input-repro.md).
//!
//! Two tao windows on one UI thread, hosted like Murmur through Wry 0.57.2: each window gets a
//! `WRY_WEBVIEW` child that owns a windowed WebView2 controller, with Wry's parent subclass
//! (focus, size, move notifications). The overlay is undecorated, always on top, transparent,
//! skip-taskbar and `focusable(false)` (WS_EX_NOACTIVATE), and carries Murmur's activation
//! hand-back subclass. A DOM mousedown asks the host to start the native move loop only while
//! the physical left button is down, as `start_overlay_drag` does.
//!
//! The only variable is `--host-input on|off`, which sets
//! `ICoreWebView2ControllerOptions4::AllowHostInputProcessing` on the overlay controller alone.
//! Events go to a JSON-lines log; no page text, title or user input is recorded.

#[cfg(not(windows))]
fn main() {
    eprintln!("webview2-input-repro runs on Windows only");
    std::process::exit(2);
}

#[cfg(windows)]
mod app;

#[cfg(windows)]
fn main() {
    if let Err(error) = app::run() {
        app::log(
            "fatal",
            &format!("\"error\":{}", app::json_string(&error.to_string())),
        );
        eprintln!("{error}");
        std::process::exit(1);
    }
}

/// Pure helpers shared by the app and the tests on every platform.
pub mod logic {
    /// Fixed vocabulary the pages may send; anything else is logged as `unknown`.
    pub const PAGE_MESSAGES: &[&str] = &[
        "ready",
        "down",
        "up",
        "pointercancel",
        "lostpointercapture",
        "focus",
        "blur",
    ];

    pub fn page_message(raw: &str) -> &'static str {
        PAGE_MESSAGES
            .iter()
            .copied()
            .find(|known| *known == raw)
            .unwrap_or("unknown")
    }

    #[derive(Debug, PartialEq, Eq, Clone, Copy)]
    pub enum HostInput {
        Off,
        On,
    }

    #[derive(Debug, PartialEq, Eq)]
    pub struct Args {
        pub host_input: HostInput,
        pub log: String,
        pub data: String,
    }

    pub fn parse_args(args: &[String]) -> Result<Args, String> {
        let (mut host_input, mut log, mut data) = (None, None, None);
        let mut items = args.iter();
        while let Some(flag) = items.next() {
            let value = items
                .next()
                .ok_or_else(|| format!("{flag} needs a value"))?;
            match flag.as_str() {
                "--host-input" => {
                    host_input = Some(match value.as_str() {
                        "on" => HostInput::On,
                        "off" => HostInput::Off,
                        _ => return Err("--host-input must be on or off".into()),
                    })
                }
                "--log" => log = Some(value.clone()),
                "--data" => data = Some(value.clone()),
                _ => return Err(format!("unknown argument {flag}")),
            }
        }
        Ok(Args {
            host_input: host_input.ok_or("--host-input is required")?,
            log: log.ok_or("--log is required")?,
            data: data.ok_or("--data is required")?,
        })
    }

    /// Murmur's hand-back rule (src-tauri/src/overlay/noactivate.rs), kept identical.
    pub fn hand_back(activation_state: usize, previous_is_window: bool) -> bool {
        (activation_state & 0xFFFF) != 0 && previous_is_window
    }

    pub fn json_string(text: &str) -> String {
        let mut out = String::from("\"");
        for c in text.chars() {
            match c {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
                c => out.push(c),
            }
        }
        out.push('"');
        out
    }
}

#[cfg(test)]
mod tests {
    use super::logic::*;

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn page_messages_are_a_closed_vocabulary() {
        assert_eq!(page_message("down"), "down");
        assert_eq!(page_message("down\"}"), "unknown");
        assert_eq!(page_message("my secret text"), "unknown");
    }

    #[test]
    fn arguments_are_strict() {
        let parsed = parse_args(&strings(&[
            "--host-input",
            "on",
            "--log",
            "l",
            "--data",
            "d",
        ]))
        .unwrap();
        assert_eq!(parsed.host_input, HostInput::On);
        assert!(parse_args(&strings(&[
            "--host-input",
            "yes",
            "--log",
            "l",
            "--data",
            "d"
        ]))
        .is_err());
        assert!(parse_args(&strings(&["--log", "l", "--data", "d"])).is_err());
        assert!(parse_args(&strings(&["--host-input", "off", "--log"])).is_err());
        assert!(parse_args(&strings(&[
            "--host-input",
            "off",
            "--log",
            "l",
            "--data",
            "d",
            "--x",
            "1"
        ]))
        .is_err());
    }

    #[test]
    fn hand_back_matches_murmur() {
        assert!(hand_back(1, true));
        assert!(hand_back(2 | 0x1_0000, true));
        assert!(!hand_back(0, true));
        assert!(!hand_back(2, false));
    }

    #[test]
    fn json_strings_are_escaped() {
        assert_eq!(json_string("a\"b\\c\n"), "\"a\\\"b\\\\c\\u000a\"");
    }
}
