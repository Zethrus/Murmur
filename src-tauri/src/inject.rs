use crate::config::Injection;
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
pub fn choose_path(text: &str, cfg: &Injection, focused: &str) -> InjectionPath {
    let app = focused.to_lowercase();
    if app == "murmur" || app == "murmur.exe" || app.starts_with("murmur settings") || app.starts_with("murmur overlay")
    {
        return InjectionPath::Skip;
    }
    if cfg.clipboard_only || cfg.exclude_apps.iter().any(|s| !s.is_empty() && app.contains(&s.to_lowercase())) {
        return InjectionPath::ClipboardOnly;
    }
    if cfg.method == "type" || (cfg.method == "auto" && text.is_ascii() && text.len() <= 1500) {
        InjectionPath::Type
    } else {
        InjectionPath::Clipboard
    }
}
#[derive(Default)]
pub struct Injector {
    clipboard: Option<arboard::Clipboard>,
}
impl Injector {
    // Keep ownership alive: on X11 the process serves clipboard contents until dropped.
    pub fn inject(&mut self, text: &str, cfg: &Injection, focused: &str) -> Result<InjectionPath> {
        let path = choose_path(text, cfg, focused);
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
            let paste = keyboard.key(Key::Unicode('v'), Direction::Click);
            let release = keyboard.key(Key::Control, Direction::Release);
            paste.context("Paste transcript")?;
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
        assert_eq!(choose_path("hello", &c, "editor"), InjectionPath::Type);
        assert_eq!(choose_path("héllo", &c, "editor"), InjectionPath::Clipboard);
        assert_eq!(choose_path(&"a".repeat(1501), &c, "editor"), InjectionPath::Clipboard);
        assert_eq!(choose_path(&"a".repeat(1500), &c, "editor"), InjectionPath::Type);
    }
    #[test]
    fn exclusions_and_own_windows() {
        let c = Injection { exclude_apps: vec!["terminal".into()], ..Default::default() };
        assert_eq!(choose_path("secret", &c, "Gnome-Terminal"), InjectionPath::ClipboardOnly);
        assert_eq!(choose_path("text", &c, "Murmur Settings"), InjectionPath::Skip);
    }
    #[test]
    fn explicit_modes() {
        let mut c = Injection { method: "type".into(), ..Default::default() };
        assert_eq!(choose_path("日本語", &c, "editor"), InjectionPath::Type);
        c.clipboard_only = true;
        assert_eq!(choose_path("hi", &c, "editor"), InjectionPath::ClipboardOnly);
    }
}
