use crate::{app::AppState, controller::Action};
use anyhow::{bail, Context, Result};
use std::{
    str::FromStr,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Instant,
};
use tauri::{AppHandle, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

pub fn parse(accel: &str) -> Result<Shortcut> {
    if accel.trim().is_empty() {
        bail!("Shortcut cannot be empty");
    }
    let parts: Vec<&str> = accel.split('+').map(str::trim).collect();
    if parts.iter().any(|p| p.is_empty()) {
        bail!("Shortcut contains an empty key");
    }
    if parts.iter().all(|p| {
        matches!(p.to_lowercase().as_str(), "ctrl" | "control" | "alt" | "shift" | "super" | "meta" | "cmd" | "command")
    }) {
        bail!("Shortcut requires a non-modifier key");
    }
    let normalized = parts
        .iter()
        .map(|p| match p.to_lowercase().as_str() {
            "ctrl" | "control" => "Control".to_owned(),
            "alt" => "Alt".to_owned(),
            "shift" => "Shift".to_owned(),
            "super" | "meta" | "cmd" | "command" => "Super".to_owned(),
            _ => p.to_string(),
        })
        .collect::<Vec<_>>()
        .join("+");
    let shortcut = Shortcut::from_str(&normalized).context("Invalid shortcut accelerator")?;
    if normalized.eq_ignore_ascii_case("escape") || normalized.eq_ignore_ascii_case("esc") {
        bail!("Escape is reserved for cancelling dictation");
    }
    Ok(shortcut)
}
pub fn serialize(shortcut: &Shortcut) -> String {
    shortcut.to_string()
}
pub fn register(app: &AppHandle, accel: &str) -> Result<()> {
    let shortcut = parse(accel)?;
    let pressed = Arc::new(AtomicBool::new(false));
    app.global_shortcut()
        .on_shortcut(shortcut, move |app, _, event| {
            let down = event.state == ShortcutState::Pressed;
            if pressed.swap(down, Ordering::Relaxed) == down {
                return;
            }
            let state = app.state::<AppState>();
            if down && state.status().is_ok_and(|s| s.state == "transcribing") {
                return;
            }
            if let Ok(config) = state.config() {
                let action = match (config.general.hotkey_mode.as_str(), down) {
                    ("hold", true) => Some(Action::Start),
                    ("hold", false) => Some(Action::Stop),
                    ("toggle", true) => Some(Action::Toggle),
                    _ => None,
                };
                if let Some(action) = action {
                    if let Err(error) = state.send(action) {
                        state.error(&error.to_string());
                    }
                }
            }
        })
        .with_context(|| format!("Shortcut {} is unavailable (another app may own it)", serialize(&shortcut)))?;
    Ok(())
}
pub fn rebind(app: &AppHandle, old: &str, new: &str) -> Result<()> {
    if parse(old)? == parse(new)? && app.global_shortcut().is_registered(parse(new)?) {
        return Ok(());
    }
    let start = Instant::now();
    register(app, new)?;
    if parse(old)? != parse(new)? && app.global_shortcut().is_registered(parse(old)?) {
        if let Err(error) = app.global_shortcut().unregister(parse(old)?) {
            let _ = app.global_shortcut().unregister(parse(new)?);
            return Err(error).context("Unregister old shortcut");
        }
    }
    if start.elapsed().as_millis() > 100 {
        tracing::warn!("Hotkey registration exceeded the 100 ms target");
    }
    Ok(())
}
pub fn escape(app: &AppHandle, enabled: bool) -> Result<()> {
    if enabled {
        app.global_shortcut()
            .on_shortcut("Escape", |app, _, event| {
                if event.state == ShortcutState::Pressed {
                    let _ = app.state::<AppState>().send(Action::Cancel);
                }
            })
            .context("Escape is unavailable; cannot safely start dictation")?;
    } else if app.global_shortcut().is_registered("Escape") {
        app.global_shortcut().unregister("Escape").context("Release Escape shortcut")?;
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn accelerator_roundtrip() -> Result<()> {
        let shortcut = parse("Ctrl+Alt+D")?;
        assert_eq!(parse(&serialize(&shortcut))?, shortcut);
        Ok(())
    }
    #[test]
    fn reject_modifiers_and_bad_keys() {
        for combo in ["Ctrl+Alt", "Shift", "", "Ctrl++D", "Ctrl+notakey", "Escape"] {
            assert!(parse(combo).is_err(), "{combo}");
        }
    }
    #[test]
    fn aliases() -> Result<()> {
        assert_eq!(parse(" ctrl + alt + D ")?, parse("Control+Alt+D")?);
        Ok(())
    }
}
