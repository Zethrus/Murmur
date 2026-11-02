use crate::config::Overlay;
use anyhow::{Context, Result};
use serde_json::json;
use tauri::{AppHandle, Emitter, LogicalSize, Manager, PhysicalPosition};

pub fn apply(app: &AppHandle, prefs: &Overlay) -> Result<()> {
    let window = app.get_webview_window("overlay").context("Overlay window missing")?;
    window.set_size(LogicalSize::new(608.0 * prefs.scale, 160.0 * prefs.scale)).context("Scale overlay window")?;
    if let Some(monitor) = window.primary_monitor().context("Get primary monitor")? {
        let area = monitor.work_area();
        let dpi = monitor.scale_factor();
        let width = 608.0 * prefs.scale * dpi;
        let height = 160.0 * prefs.scale * dpi;
        let x = f64::from(area.position.x) + (f64::from(area.size.width) - width) / 2.0;
        let y = f64::from(area.position.y)
            + if prefs.position == "top-center" {
                (48.0 - 20.0 * prefs.scale) * dpi
            } else {
                f64::from(area.size.height) - height - (48.0 - 20.0 * prefs.scale) * dpi
            };
        window.set_position(PhysicalPosition::new(x as i32, y as i32)).context("Position overlay")?;
    }
    let idle = app.try_state::<crate::app::AppState>().and_then(|s| s.status().ok()).is_none_or(|s| s.state == "idle");
    if !prefs.enabled || (prefs.idle_hide && idle) {
        window.hide().context("Hide idle overlay")?;
        return Ok(());
    }
    // GTK only has a GdkWindow after the widget is realized, and tao panics (not Result) when
    // asked to shape an unrealized window: show first, then make it click-through.
    window.show().context("Show idle overlay")?;
    if let Err(error) = window.set_ignore_cursor_events(true) {
        tracing::warn!(%error, "Overlay click-through unavailable; hiding it");
        window.hide().context("Hide unsupported overlay")?;
    }
    Ok(())
}
pub fn state(app: &AppHandle, state: &str, prefs: &Overlay) -> Result<()> {
    app.emit("state", json!({"state":state})).context("Emit state")?;
    if prefs.enabled {
        if let Some(window) = app.get_webview_window("overlay") {
            // Same ordering rule as apply(): realize (show) before shaping the window.
            window.show().context("Show overlay")?;
            if let Err(error) = window.set_ignore_cursor_events(true) {
                tracing::warn!(%error, "Overlay click-through unavailable; keeping it hidden");
                window.hide().context("Hide unsupported overlay")?;
            }
        }
    }
    Ok(())
}
pub fn hide(app: &AppHandle) -> Result<()> {
    if let Some(window) = app.get_webview_window("overlay") {
        window.hide().context("Hide overlay")?;
    }
    Ok(())
}
