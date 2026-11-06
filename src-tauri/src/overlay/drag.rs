//! OS-owned movement with release detection independent of webview pointer-up.
use super::movement::{Drag, Progress};
use super::{display_for, displays, hide_main, placement_for, position, runtime, Rect};
use anyhow::{Context, Result};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};

fn owns_drag(generation: u64) -> bool {
    let rt = runtime();
    rt.drag
        .as_ref()
        .is_some_and(|drag| drag.generation == generation)
}

// Native drag consumes pointer-up on some backends. Query the native button state
// on the UI thread; neither a JS promise nor a pause in movement means release.
#[cfg(target_os = "linux")]
fn primary_button_down(window: &tauri::WebviewWindow) -> Result<bool> {
    use gtk::prelude::*;
    let gtk = window.gtk_window()?;
    let pointer = gtk
        .display()
        .default_seat()
        .and_then(|seat| seat.pointer())
        .context("Desktop pointer unavailable")?;
    let root = pointer
        .position()
        .0
        .root_window()
        .context("Desktop root unavailable")?;
    Ok(root
        .device_position(&pointer)
        .3
        .contains(gtk::gdk::ModifierType::BUTTON1_MASK))
}

#[cfg(windows)]
fn primary_button_down(_window: &tauri::WebviewWindow) -> Result<bool> {
    use windows_sys::Win32::UI::{
        Input::KeyboardAndMouse::{GetAsyncKeyState, VK_LBUTTON, VK_RBUTTON},
        WindowsAndMessaging::{GetSystemMetrics, SM_SWAPBUTTON},
    };
    // GetAsyncKeyState uses physical buttons; the DOM uses the logical primary button.
    unsafe {
        let key = if GetSystemMetrics(SM_SWAPBUTTON) != 0 {
            VK_RBUTTON
        } else {
            VK_LBUTTON
        };
        Ok(GetAsyncKeyState(i32::from(key)) < 0)
    }
}

#[cfg(not(any(target_os = "linux", windows)))]
fn primary_button_down(_window: &tauri::WebviewWindow) -> Result<bool> {
    anyhow::bail!("Overlay dragging is supported on Linux/X11 and Windows")
}

async fn on_main<T: Send + 'static>(
    app: &AppHandle,
    action: impl FnOnce(AppHandle) -> Result<T> + Send + 'static,
) -> Result<T> {
    let (send, receive) = tokio::sync::oneshot::channel();
    let handle = app.clone();
    app.run_on_main_thread(move || {
        let _ = send.send(action(handle));
    })?;
    receive.await.context("Overlay event loop closed")?
}

fn finish_drag(app: &AppHandle, generation: u64, capture: bool) -> Result<()> {
    if !owns_drag(generation) {
        return Ok(());
    }
    let window = app
        .get_webview_window("overlay")
        .context("Overlay window missing")?;
    // Query native geometry after release; a queued Moved event may describe an older position.
    let capture = capture && runtime().drag.as_ref().is_some_and(|drag| !drag.cancelled);
    let placement = if capture {
        let pos = window.outer_position()?;
        let size = window.outer_size()?;
        let rect = Rect {
            x: pos.x,
            y: pos.y,
            w: size.width,
            h: size.height,
        };
        let list = displays(&window)?;
        let changed = runtime()
            .drag
            .as_ref()
            .is_some_and(|drag| drag.initial != (pos.x, pos.y));
        if changed {
            list.get(display_for(&list, &rect).unwrap_or(0))
                .map(|display| placement_for(display, &rect))
        } else {
            None
        }
    } else {
        None
    };
    let hide = {
        let mut rt = runtime();
        if !rt
            .drag
            .as_ref()
            .is_some_and(|drag| drag.generation == generation)
        {
            return Ok(());
        }
        let cancelled = rt.drag.as_ref().is_some_and(|drag| drag.cancelled);
        rt.drag = None;
        if let Some(placement) = placement.filter(|_| !cancelled) {
            rt.placement = Some(placement);
            rt.unsaved = true;
            rt.retry_at = None;
        }
        std::mem::take(&mut rt.hide_pending)
    };
    let prefs = app.state::<crate::app::AppState>().config()?.overlay;
    position(app, &prefs)?;
    if hide {
        hide_main(app)?;
    }
    Ok(())
}

/// Returns only once the native gesture has released and settled. All native window
/// operations stay on the UI thread; no runtime mutex is held across a native call.
pub async fn drag(app: AppHandle) -> Result<()> {
    let generation = on_main(&app, |app| {
        let window = app
            .get_webview_window("overlay")
            .context("Overlay window missing")?;
        anyhow::ensure!(
            app.state::<crate::app::AppState>()
                .config()?
                .overlay
                .enabled,
            "Overlay disabled"
        );
        anyhow::ensure!(
            primary_button_down(&window)?,
            "Release received before drag started; try again"
        );
        let pos = window.outer_position()?;
        let generation = {
            let mut rt = runtime();
            anyhow::ensure!(!rt.busy, "Overlay is delivering text");
            anyhow::ensure!(rt.drag.is_none(), "Overlay is already moving");
            rt.generation += 1;
            let mut drag = Drag::new((pos.x, pos.y), Instant::now());
            drag.generation = rt.generation;
            rt.drag = Some(drag);
            rt.generation
        };
        if let Err(error) = window.start_dragging() {
            runtime().drag = None;
            return Err(error.into());
        }
        Ok(generation)
    })
    .await?;
    let result = async {
        loop {
            tokio::time::sleep(Duration::from_millis(50)).await;
            let done = on_main(&app, move |app| {
                if !owns_drag(generation) {
                    return Ok(true);
                }
                let window = app
                    .get_webview_window("overlay")
                    .context("Overlay window missing")?;
                let down = primary_button_down(&window)?;
                let progress = runtime()
                    .drag
                    .as_mut()
                    .context("Drag ended")?
                    .poll(down, Instant::now());
                match progress {
                    Progress::Held => Ok(false),
                    Progress::TimedOut => {
                        anyhow::bail!("Overlay drag timed out; release the mouse and try again")
                    }
                    Progress::Settled => {
                        finish_drag(&app, generation, true)?;
                        Ok(true)
                    }
                }
            })
            .await?;
            if done {
                return Ok(());
            }
        }
    }
    .await;
    if result.is_err() {
        // Includes destroyed-window/query failures. Always release ownership even if
        // restoring native geometry also fails, so later gestures and hiding recover.
        let _ = on_main(&app, move |app| {
            let restored = finish_drag(&app, generation, false);
            let mut rt = runtime();
            if rt
                .drag
                .as_ref()
                .is_some_and(|drag| drag.generation == generation)
            {
                rt.drag = None;
            }
            restored
        })
        .await;
    }
    result
}
