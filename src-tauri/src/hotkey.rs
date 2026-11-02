use crate::{app::AppState, config::General, controller::Action};
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
        .on_shortcut(shortcut, move |app, shortcut, event| {
            let down = event.state == ShortcutState::Pressed;
            if pressed.swap(down, Ordering::Relaxed) == down {
                return;
            }
            let state = app.state::<AppState>();
            if down && state.status().is_ok_and(|s| s.state == "transcribing") {
                return;
            }
            if let Ok(config) = state.config() {
                let code = parse(&config.general.hotkey_code).is_ok_and(|key| key == *shortcut);
                let action = action_for(&config.general.hotkey_mode, down, code);
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
fn action_for(mode: &str, down: bool, code: bool) -> Option<Action> {
    match (mode, down) {
        ("hold", true) => Some(if code { Action::StartCode } else { Action::Start }),
        ("hold", false) => Some(Action::Stop),
        ("toggle", true) => Some(if code { Action::ToggleCode } else { Action::Toggle }),
        _ => None,
    }
}
fn shortcuts(general: &General) -> Result<Vec<Shortcut>> {
    let mut keys = vec![parse(&general.hotkey)?];
    if !general.hotkey_code.is_empty() {
        let code = parse(&general.hotkey_code)?;
        if keys.contains(&code) {
            bail!("Dictation and code shortcuts must be different");
        }
        keys.push(code);
    }
    Ok(keys)
}
pub fn rebind(app: &AppHandle, old: &General, new: &General) -> Result<()> {
    let before = shortcuts(old)?;
    let after = shortcuts(new)?;
    let start = Instant::now();
    let mut added = Vec::new();
    for key in &after {
        if !app.global_shortcut().is_registered(*key) {
            if let Err(error) = register(app, &serialize(key)) {
                for key in added {
                    let _ = app.global_shortcut().unregister(key);
                }
                return Err(error);
            }
            added.push(*key);
        }
    }
    let mut removed = Vec::new();
    for key in &before {
        if !after.contains(key) && app.global_shortcut().is_registered(*key) {
            if let Err(error) = app.global_shortcut().unregister(*key) {
                for key in removed {
                    let _ = register(app, &serialize(&key));
                }
                for key in added {
                    let _ = app.global_shortcut().unregister(key);
                }
                return Err(error).context("Unregister old shortcut");
            }
            removed.push(*key);
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
    fn code_shortcut_routes_hold_and_toggle() {
        assert!(matches!(action_for("hold", true, true), Some(Action::StartCode)));
        assert!(matches!(action_for("hold", false, true), Some(Action::Stop)));
        assert!(matches!(action_for("toggle", true, true), Some(Action::ToggleCode)));
        assert!(action_for("toggle", false, true).is_none());
        assert!(matches!(action_for("hold", true, false), Some(Action::Start)));
        assert!(matches!(action_for("toggle", true, false), Some(Action::Toggle)));
    }
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
