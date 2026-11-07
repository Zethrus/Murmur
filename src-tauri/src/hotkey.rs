use crate::{app::AppState, config::Config, controller::Action};
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
        matches!(
            p.to_lowercase().as_str(),
            "ctrl" | "control" | "alt" | "shift" | "super" | "meta" | "cmd" | "command"
        )
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
/// What a registered global shortcut does. Routing is by purpose, not by the dictation mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Purpose {
    Dictate,
    Code,
    Rewrite,
    Accept,
    InsertHeld,
}
/// Every nonempty shortcut in the configuration, validated together for collisions.
pub fn bindings(config: &Config) -> Result<Vec<(Shortcut, Purpose)>> {
    let mut keys = vec![(parse(&config.general.hotkey)?, Purpose::Dictate)];
    for (accel, purpose, label) in [
        (&config.general.hotkey_code, Purpose::Code, "code"),
        (&config.enhance.rewrite_hotkey, Purpose::Rewrite, "rewrite"),
        (&config.enhance.accept_hotkey, Purpose::Accept, "accept"),
        (
            &config.injection.insert_held_hotkey,
            Purpose::InsertHeld,
            "insert held text",
        ),
    ] {
        if accel.trim().is_empty() {
            continue;
        }
        let key = parse(accel)?;
        if let Some((_, other)) = keys.iter().find(|(k, _)| *k == key) {
            if *other == Purpose::Dictate && purpose == Purpose::Code {
                bail!("Dictation and code shortcuts must be different");
            }
            bail!("The {label} shortcut is already used by another Murmur action");
        }
        keys.push((key, purpose));
    }
    Ok(keys)
}
fn purpose_of(config: &Config, shortcut: &Shortcut) -> Option<Purpose> {
    bindings(config)
        .ok()?
        .into_iter()
        .find(|(key, _)| key == shortcut)
        .map(|(_, purpose)| purpose)
}
pub fn register(app: &AppHandle, accel: &str) -> Result<()> {
    let shortcut = parse(accel)?;
    let pressed = Arc::new(AtomicBool::new(false));
    app.global_shortcut()
        .on_shortcut(shortcut, move |app, shortcut, event| {
            let down = event.state == ShortcutState::Pressed;
            if !edge(&pressed, down) {
                return;
            }
            let state = app.state::<AppState>();
            let Ok(config) = state.config() else { return };
            let action = match purpose_of(&config, shortcut) {
                // Enhance actions are press-only and independent of hold/toggle mode.
                Some(Purpose::Rewrite) if down => {
                    if state
                        .status()
                        .is_ok_and(|s| matches!(s.state.as_str(), "listening" | "transcribing"))
                    {
                        None
                    } else {
                        Some(Action::Rewrite(state.enhance.shortcut_context()))
                    }
                }
                Some(Purpose::Accept) if down => {
                    Some(Action::AcceptCurrent(state.enhance.shortcut_suggestion()))
                }
                // Press-only; the controller ignores it while dictating.
                Some(Purpose::InsertHeld) if down => {
                    Some(Action::InsertHeld(state.held.current_id()))
                }
                Some(Purpose::Rewrite | Purpose::Accept | Purpose::InsertHeld) => None,
                Some(purpose) => {
                    if state
                        .status()
                        .is_ok_and(|s| ignore_dictation_press(down, &s.state))
                    {
                        return;
                    }
                    action_for(&config.general.hotkey_mode, down, purpose == Purpose::Code)
                }
                None => None,
            };
            if let Some(action) = action {
                if let Err(error) = state.send(action) {
                    state.error(&error.to_string());
                }
            }
        })
        .with_context(|| {
            format!(
                "Shortcut {} is unavailable (another app may own it)",
                serialize(&shortcut)
            )
        })?;
    Ok(())
}
/// Only state changes count: a repeated Pressed or Released for the same shortcut is dropped.
///
/// Windows: global-hotkey registers with MOD_NOREPEAT (no auto-repeat Pressed) and reports
/// Released by polling GetAsyncKeyState for the non-modifier key every 50 ms, so Released can
/// arrive while Control/Alt are still down. Typing then waits for the modifiers
/// (`inject::wait_for_modifiers_released`, B8).
fn edge(pressed: &AtomicBool, down: bool) -> bool {
    pressed.swap(down, Ordering::Relaxed) != down
}
/// A dictation press while the previous transcript is still being transcribed or delivered is
/// ignored. This also stops synthetic keystrokes from re-triggering the hotkey: on Windows a typed
/// letter that matches the hotkey while its modifiers are physically held fires WM_HOTKEY again.
fn ignore_dictation_press(down: bool, state: &str) -> bool {
    down && state == "transcribing"
}
fn action_for(mode: &str, down: bool, code: bool) -> Option<Action> {
    match (mode, down) {
        ("hold", true) => Some(if code {
            Action::StartCode
        } else {
            Action::Start
        }),
        ("hold", false) => Some(Action::Stop),
        ("toggle", true) => Some(if code {
            Action::ToggleCode
        } else {
            Action::Toggle
        }),
        _ => None,
    }
}
/// Transactional: on any failure, registrations are restored to the old set.
pub fn rebind(app: &AppHandle, old: &Config, new: &Config) -> Result<()> {
    let before: Vec<Shortcut> = bindings(old)?.into_iter().map(|(k, _)| k).collect();
    let after: Vec<Shortcut> = bindings(new)?.into_iter().map(|(k, _)| k).collect();
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
        app.global_shortcut()
            .unregister("Escape")
            .context("Release Escape shortcut")?;
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn code_shortcut_routes_hold_and_toggle() {
        assert!(matches!(
            action_for("hold", true, true),
            Some(Action::StartCode)
        ));
        assert!(matches!(
            action_for("hold", false, true),
            Some(Action::Stop)
        ));
        assert!(matches!(
            action_for("toggle", true, true),
            Some(Action::ToggleCode)
        ));
        assert!(action_for("toggle", false, true).is_none());
        assert!(matches!(
            action_for("hold", true, false),
            Some(Action::Start)
        ));
        assert!(matches!(
            action_for("toggle", true, false),
            Some(Action::Toggle)
        ));
    }
    #[test]
    fn only_press_release_edges_are_acted_on() {
        let pressed = AtomicBool::new(false);
        assert!(edge(&pressed, true));
        // A second Pressed without a Released in between (key repeat, duplicate event).
        assert!(!edge(&pressed, true));
        assert!(edge(&pressed, false));
        assert!(!edge(&pressed, false));
        assert!(edge(&pressed, true));
    }
    #[test]
    fn presses_during_delivery_do_not_restart_dictation() {
        assert!(ignore_dictation_press(true, "transcribing"));
        // The hold-mode release still stops the recording.
        assert!(!ignore_dictation_press(false, "transcribing"));
        for state in ["idle", "listening", "done", "error"] {
            assert!(!ignore_dictation_press(true, state));
        }
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
    fn bindings_route_by_purpose_and_reject_collisions() -> Result<()> {
        let mut c = Config::default();
        assert_eq!(bindings(&c)?.len(), 1);
        c.general.hotkey_code = "Ctrl+Alt+C".into();
        c.enhance.rewrite_hotkey = "Ctrl+Alt+R".into();
        c.enhance.accept_hotkey = "Ctrl+Alt+A".into();
        let keys = bindings(&c)?;
        assert_eq!(keys.len(), 4);
        assert_eq!(
            purpose_of(&c, &parse("ctrl+alt+r")?),
            Some(Purpose::Rewrite)
        );
        assert_eq!(purpose_of(&c, &parse("Ctrl+Alt+A")?), Some(Purpose::Accept));
        assert_eq!(purpose_of(&c, &parse("Ctrl+Alt+C")?), Some(Purpose::Code));
        assert_eq!(
            purpose_of(&c, &parse("Ctrl+Alt+D")?),
            Some(Purpose::Dictate)
        );
        c.enhance.accept_hotkey = "Control+Alt+C".into();
        assert!(bindings(&c).is_err());
        c.enhance.accept_hotkey = "Ctrl+Alt+A".into();
        c.injection.insert_held_hotkey = "Ctrl+Alt+I".into();
        assert_eq!(bindings(&c)?.len(), 5);
        assert_eq!(
            purpose_of(&c, &parse("Control+Alt+I")?),
            Some(Purpose::InsertHeld)
        );
        c.injection.insert_held_hotkey = "Ctrl+Alt+R".into();
        assert!(bindings(&c).is_err());
        Ok(())
    }
    #[test]
    fn aliases() -> Result<()> {
        assert_eq!(parse(" ctrl + alt + D ")?, parse("Control+Alt+D")?);
        Ok(())
    }
}
