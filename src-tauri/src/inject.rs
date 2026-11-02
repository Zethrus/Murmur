use crate::{config::Injection, postproc::Style};
use anyhow::{Context, Result};
use enigo::{Direction, Enigo, Key, Keyboard, Settings};
use std::{thread, time::Duration};

#[derive(Debug, PartialEq, Eq)]
pub enum InjectionPath {
    Type,
    Clipboard,
    ClipboardOnly,
    Skip,
}
pub fn choose_path(text: &str, cfg: &Injection, focused: &str, style: Style) -> InjectionPath {
    let app = focused.to_lowercase();
    if app == "murmur" || app == "murmur.exe" || app.starts_with("murmur settings") || app.starts_with("murmur overlay")
    {
        return InjectionPath::Skip;
    }
    if cfg.clipboard_only || cfg.exclude_apps.iter().any(|s| !s.is_empty() && app.contains(&s.to_lowercase())) {
        return InjectionPath::ClipboardOnly;
    }
    if cfg.prefer_paste && (style == Style::Code || text.chars().count() > 200) {
        return InjectionPath::Clipboard;
    }
    if cfg.method == "type" || (cfg.method == "auto" && text.is_ascii() && text.len() <= 1500) {
        InjectionPath::Type
    } else {
        InjectionPath::Clipboard
    }
}
pub fn is_terminal(focused: &str) -> bool {
    let app = focused.to_lowercase();
    [
        "terminal",
        "konsole",
        "alacritty",
        "kitty",
        "wezterm",
        "mintty",
        "xterm",
        "powershell",
        "cmd.exe",
        "tilix",
        "terminator",
        "urxvt",
    ]
    .iter()
    .any(|name| app.contains(name))
}
pub fn safe_text(text: &str, focused: &str) -> String {
    if is_terminal(focused) {
        text.replace("\r\n", " ").replace(['\r', '\n', '\u{2028}', '\u{2029}'], " ")
    } else {
        text.to_owned()
    }
}
#[derive(Default)]
pub struct Injector {
    clipboard: Option<arboard::Clipboard>,
}
impl Injector {
    // Keep ownership alive: on X11 the process serves clipboard contents until dropped.
    pub fn inject(&mut self, text: &str, cfg: &Injection, focused: &str, style: Style) -> Result<InjectionPath> {
        let safe = safe_text(text, focused);
        let text = safe.as_str();
        let path = choose_path(text, cfg, focused, style);
        if path == InjectionPath::Skip || text.is_empty() {
            return Ok(path);
        }
        if path == InjectionPath::Type {
            Enigo::new(&Settings::default())
                .context("Connect keyboard injection")?
                .text(text)
                .context("Type transcript into focused application")?;
            return Ok(path);
        }
        if self.clipboard.is_none() {
            self.clipboard = Some(arboard::Clipboard::new().context("Open clipboard")?);
        }
        let clipboard = self.clipboard.as_mut().context("Clipboard unavailable")?;
        let previous = if path == InjectionPath::Clipboard {
            match clipboard.get_text() {
                Ok(text) => Some(text),
                Err(arboard::Error::ContentNotAvailable) => None,
                Err(error) => return Err(error).context("Save clipboard text before pasting"),
            }
        } else {
            None
        };
        clipboard.set_text(text).context("Copy transcript")?;
        if path == InjectionPath::ClipboardOnly {
            return Ok(path);
        }
        let result = (|| -> Result<()> {
            let mut keyboard = Enigo::new(&Settings::default()).context("Connect paste keyboard")?;
            keyboard.key(Key::Control, Direction::Press).context("Press paste modifier")?;
            let shift = cfg!(target_os = "linux") && is_terminal(focused);
            let paste = (|| -> Result<()> {
                if shift {
                    keyboard.key(Key::Shift, Direction::Press).context("Press terminal paste modifier")?;
                }
                keyboard.key(Key::Unicode('v'), Direction::Click).context("Paste transcript")
            })();
            let shift_release = if shift { keyboard.key(Key::Shift, Direction::Release) } else { Ok(()) };
            let release = keyboard.key(Key::Control, Direction::Release);
            paste?;
            shift_release.context("Release terminal paste modifier")?;
            release.context("Release paste modifier")?;
            Ok(())
        })();
        thread::sleep(Duration::from_millis(300));
        if let Some(previous) = previous {
            // Do not overwrite a clipboard update made by the user during the paste delay.
            if clipboard.get_text().ok().as_deref() == Some(text) {
                if let Err(error) = clipboard.set_text(previous) {
                    tracing::warn!(%error, "Clipboard restore failed; retaining transcript");
                    let _ = clipboard.set_text(text);
                }
            }
        }
        result?;
        Ok(path)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn auto_paths() {
        let c = Injection::default();
        assert_eq!(choose_path("hello", &c, "editor", Style::Prose), InjectionPath::Type);
        assert_eq!(choose_path("héllo", &c, "editor", Style::Prose), InjectionPath::Clipboard);
        assert_eq!(choose_path(&"a".repeat(1501), &c, "editor", Style::Prose), InjectionPath::Clipboard);
        assert_eq!(choose_path(&"a".repeat(200), &c, "editor", Style::Prose), InjectionPath::Type);
    }
    #[test]
    fn exclusions_and_own_windows() {
        let c = Injection { exclude_apps: vec!["terminal".into()], ..Default::default() };
        assert_eq!(choose_path("secret", &c, "Gnome-Terminal", Style::Prose), InjectionPath::ClipboardOnly);
        assert_eq!(choose_path("text", &c, "Murmur Settings", Style::Prose), InjectionPath::Skip);
    }
    #[test]
    fn explicit_modes() {
        let mut c = Injection { method: "type".into(), ..Default::default() };
        assert_eq!(choose_path("日本語", &c, "editor", Style::Prose), InjectionPath::Type);
        c.clipboard_only = true;
        assert_eq!(choose_path("hi", &c, "editor", Style::Prose), InjectionPath::ClipboardOnly);
    }
}
