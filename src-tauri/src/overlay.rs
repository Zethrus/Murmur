//! Native overlay window: placement (preset or user-dragged, per monitor), measured sizing,
//! visibility, and interactivity. The overlay is never focused; it is clickable while visible
//! except during actual text injection, when it becomes click-through.
mod drag;
mod movement;
use crate::config::{Overlay, Placement};
use anyhow::{Context, Result};
pub use drag::drag;
use movement::Drag;
use serde_json::json;
use std::{
    sync::{Mutex, OnceLock},
    time::{Duration, Instant},
};
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, PhysicalSize};

/// Logical size before the view reports a measurement.
pub const DEFAULT_SIZE: (f64, f64) = (608.0, 160.0);
pub const MIN_SIZE: (f64, f64) = (120.0, 40.0);
pub const MAX_SIZE: (f64, f64) = (1200.0, 900.0);
/// Distance from the work-area edge for the preset positions, in logical px.
const EDGE_INSET: f64 = 28.0;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub w: u32,
    pub h: u32,
}
impl Rect {
    fn intersection(&self, other: &Rect) -> u64 {
        let x0 = i64::from(self.x).max(i64::from(other.x));
        let y0 = i64::from(self.y).max(i64::from(other.y));
        let x1 =
            (i64::from(self.x) + i64::from(self.w)).min(i64::from(other.x) + i64::from(other.w));
        let y1 =
            (i64::from(self.y) + i64::from(self.h)).min(i64::from(other.y) + i64::from(other.h));
        if x1 <= x0 || y1 <= y0 {
            0
        } else {
            ((x1 - x0) * (y1 - y0)) as u64
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Display {
    pub name: String,
    /// Whole monitor bounds (identity).
    pub bounds: Rect,
    pub work: Rect,
    pub scale: f64,
}

/// Exact saved monitor, else same name, else same geometry, else primary (index 0).
pub fn choose_display(displays: &[Display], placement: &Placement) -> usize {
    let geometry = |d: &Display| {
        d.bounds.x == placement.monitor_x
            && d.bounds.y == placement.monitor_y
            && d.bounds.w == placement.monitor_width
            && d.bounds.h == placement.monitor_height
    };
    if !placement.is_set() {
        return 0;
    }
    displays
        .iter()
        .position(|d| geometry(d) && d.name == placement.monitor_name)
        .or_else(|| {
            displays.iter().position(|d| {
                !placement.monitor_name.is_empty() && d.name == placement.monitor_name
            })
        })
        .or_else(|| displays.iter().position(geometry))
        .unwrap_or(0)
}

/// Presets target the primary display even when an earlier custom record remains.
pub fn display_index(displays: &[Display], prefs: &Overlay) -> usize {
    if prefs.position == "custom" {
        choose_display(displays, &prefs.placement)
    } else {
        0
    }
}

/// The monitor with the largest intersection with the window.
pub fn display_for(displays: &[Display], window: &Rect) -> Option<usize> {
    displays
        .iter()
        .enumerate()
        .map(|(i, d)| (i, d.bounds.intersection(window)))
        .filter(|(_, area)| *area > 0)
        .max_by_key(|(_, area)| *area)
        .map(|(i, _)| i)
}

/// Clamp a physical size into the work area (shrinking if the screen is smaller).
pub fn clamp_size(work: &Rect, w: u32, h: u32) -> (u32, u32) {
    (w.min(work.w).max(1), h.min(work.h).max(1))
}

/// Physical top-left for a `w`×`h` window inside the display's work area.
pub fn place(prefs: &Overlay, display: &Display, w: u32, h: u32) -> (i32, i32) {
    let work = display.work;
    let travel_x = f64::from(work.w.saturating_sub(w));
    let travel_y = f64::from(work.h.saturating_sub(h));
    let (fx, fy) = if prefs.position == "custom" && prefs.placement.is_set() {
        (prefs.placement.x_fraction, prefs.placement.y_fraction)
    } else {
        let inset = (EDGE_INSET * display.scale).min(travel_y / 2.0);
        let fy = if travel_y <= 0.0 {
            0.0
        } else if prefs.position == "top-center" {
            inset / travel_y
        } else {
            (travel_y - inset) / travel_y
        };
        (0.5, fy)
    };
    let fx = if fx.is_finite() {
        fx.clamp(0.0, 1.0)
    } else {
        0.5
    };
    let fy = if fy.is_finite() {
        fy.clamp(0.0, 1.0)
    } else {
        1.0
    };
    (
        work.x + (travel_x * fx).round() as i32,
        work.y + (travel_y * fy).round() as i32,
    )
}

/// Normalised travel fractions (clamped to the work area) for a window on `display`.
pub fn placement_for(display: &Display, window: &Rect) -> Placement {
    let work = display.work;
    let fraction = |offset: i64, travel: u32| {
        if travel == 0 {
            0.5
        } else {
            (offset as f64 / f64::from(travel)).clamp(0.0, 1.0)
        }
    };
    Placement {
        monitor_name: display.name.clone(),
        monitor_x: display.bounds.x,
        monitor_y: display.bounds.y,
        monitor_width: display.bounds.w,
        monitor_height: display.bounds.h,
        x_fraction: fraction(
            i64::from(window.x) - i64::from(work.x),
            work.w.saturating_sub(window.w),
        ),
        y_fraction: fraction(
            i64::from(window.y) - i64::from(work.y),
            work.h.saturating_sub(window.h),
        ),
        ..Placement::default()
    }
}

#[derive(Debug, Default)]
struct Runtime {
    /// Logical size reported by the view (already includes the scale preference).
    size: Option<(f64, f64)>,
    layout_revision: u64,
    layout_instance: String,
    generation: u64,
    drag: Option<Drag>,
    placement: Option<Placement>,
    unsaved: bool,
    retry_at: Option<Instant>,
    hide_pending: bool,
    busy: bool,
}
fn runtime() -> std::sync::MutexGuard<'static, Runtime> {
    static RUNTIME: OnceLock<Mutex<Runtime>> = OnceLock::new();
    RUNTIME
        .get_or_init(|| Mutex::new(Runtime::default()))
        .lock()
        .unwrap_or_else(|p| p.into_inner())
}
impl Runtime {
    fn prefs(&self, prefs: &Overlay) -> Option<Overlay> {
        if self.drag.is_some() {
            return None;
        }
        let mut prefs = prefs.clone();
        if let Some(placement) = &self.placement {
            prefs.position = "custom".into();
            prefs.placement = placement.clone();
        }
        Some(prefs)
    }

    fn moved(&mut self, position: (i32, i32), now: Instant) {
        if let Some(drag) = &mut self.drag {
            drag.moved(position, now);
        }
    }

    fn reset(&mut self) {
        self.generation += 1;
        // Keep native gesture ownership until release, but discard its placement.
        // A preset request during a native grab must not fight the compositor.
        if let Some(drag) = &mut self.drag {
            drag.cancelled = true;
        }
        self.placement = None;
        self.unsaved = false;
        self.retry_at = None;
        self.hide_pending = false;
    }

    fn pending(&self, now: Instant) -> Option<(u64, Placement)> {
        if self.drag.is_some() || !self.unsaved || self.retry_at.is_some_and(|at| now < at) {
            return None;
        }
        self.placement
            .clone()
            .map(|placement| (self.generation, placement))
    }

    fn saved(&mut self, generation: u64, success: bool, now: Instant) {
        if self.generation == generation {
            self.unsaved = !success;
            self.retry_at = (!success).then(|| now + Duration::from_secs(1));
        }
    }
}

// Visibility decisions run in FIFO order on the event loop. Never hold a worker-side
// mutex while a native window getter sends a synchronous query to that same loop.
// The enqueue operation returns without awaiting the native getter inside the action.
fn queue_visibility<E>(
    enqueue: impl FnOnce(Box<dyn FnOnce() + Send>) -> std::result::Result<(), E>,
    action: impl FnOnce() + Send + 'static,
) -> std::result::Result<(), E> {
    enqueue(Box::new(action))
}

// Only the worker waits; the main executor sends after applying native cursor state.
fn queue_busy_ack<E>(
    enqueue: impl FnOnce(Box<dyn FnOnce() + Send>) -> std::result::Result<(), E>,
    action: impl FnOnce() -> Result<()> + Send + 'static,
) -> Result<()>
where
    E: std::fmt::Display,
{
    let (send, receive) = std::sync::mpsc::sync_channel(1);
    queue_visibility(enqueue, move || {
        let _ = send.send(action());
    })
    .map_err(|e| anyhow::anyhow!("Queue overlay click-through: {e}"))?;
    receive
        .recv_timeout(Duration::from_secs(2))
        .context("Overlay click-through native acknowledgement timed out")?
}

fn visibility_on_main(
    app: &AppHandle,
    action: impl FnOnce(AppHandle) + Send + 'static,
) -> Result<()> {
    let handle = app.clone();
    queue_visibility(
        |task| app.run_on_main_thread(move || task()),
        move || action(handle),
    )
    .context("Queue overlay visibility")
}

fn displays(window: &tauri::WebviewWindow) -> Result<Vec<Display>> {
    let primary = window.primary_monitor().ok().flatten();
    let mut list: Vec<Display> = window
        .available_monitors()
        .context("List monitors")?
        .into_iter()
        .map(|m| Display {
            name: m.name().cloned().unwrap_or_default(),
            bounds: Rect {
                x: m.position().x,
                y: m.position().y,
                w: m.size().width,
                h: m.size().height,
            },
            work: Rect {
                x: m.work_area().position.x,
                y: m.work_area().position.y,
                w: m.work_area().size.width,
                h: m.work_area().size.height,
            },
            scale: m.scale_factor(),
        })
        .collect();
    if let Some(p) = primary {
        if let Some(i) = list
            .iter()
            .position(|d| d.bounds.x == p.position().x && d.bounds.y == p.position().y)
        {
            let primary = list.remove(i);
            list.insert(0, primary);
        }
    }
    Ok(list)
}

/// Force a fully transparent window and page background. Without this the GTK/WebKit
/// surface paints an opaque dark rounded box behind the pill on some desktops.
fn clear_background(app: &AppHandle) {
    let Some(window) = app.get_webview_window("overlay") else {
        return;
    };
    let transparent = tauri::window::Color(0, 0, 0, 0);
    if let Err(error) = window.set_background_color(Some(transparent)) {
        tracing::debug!(%error, "overlay background reset skipped");
    }
}

/// Size and position the overlay without changing visibility or focus.
pub fn position(app: &AppHandle, prefs: &Overlay) -> Result<()> {
    let (prefs, size) = {
        let rt = runtime();
        let Some(prefs) = rt.prefs(prefs) else {
            return Ok(());
        };
        (prefs, rt.size)
    };
    let window = app
        .get_webview_window("overlay")
        .context("Overlay window missing")?;
    let list = displays(&window)?;
    let Some(display) = list.get(display_index(&list, &prefs)) else {
        return Ok(());
    };
    let (lw, lh) = size.unwrap_or((DEFAULT_SIZE.0 * prefs.scale, DEFAULT_SIZE.1 * prefs.scale));
    let (w, h) = clamp_size(
        &display.work,
        (lw * display.scale).round() as u32,
        (lh * display.scale).round() as u32,
    );
    let (x, y) = place(&prefs, display, w, h);
    #[cfg(target_os = "linux")]
    {
        use gtk::prelude::*;
        let gtk = window.gtk_window()?;
        let scale = f64::from(gtk.scale_factor());
        resize_gtk(
            &gtk,
            (f64::from(w) / scale).round() as i32,
            (f64::from(h) / scale).round() as i32,
        );
    }
    window
        .set_size(PhysicalSize::new(w, h))
        .context("Size overlay")?;
    window
        .set_position(PhysicalPosition::new(x, y))
        .context("Position overlay")?;
    Ok(())
}

/// GTK ignores resize() on a non-resizable window if its natural/default
/// requisition still asks for the old size. Update that requisition as well;
/// this preserves non-resizability without changing the window's semantic type.
#[cfg(target_os = "linux")]
fn resize_gtk(window: &gtk::ApplicationWindow, width: i32, height: i32) {
    use gtk::prelude::*;
    window.set_default_size(width, height);
    window.set_size_request(width, height);
}

fn show_interactive(window: &tauri::WebviewWindow) -> Result<()> {
    // GTK only has a GdkWindow after the widget is realized, and tao panics (not Result) when
    // asked to shape an unrealized window: show first, then set cursor handling. Never focus.
    window.show().context("Show overlay")?;
    let busy = runtime().busy;
    if let Err(error) = window.set_ignore_cursor_events(busy) {
        tracing::warn!(%error, "Overlay cursor handling unavailable; hiding it");
        window.hide().context("Hide unsupported overlay")?;
    }
    Ok(())
}

pub fn apply(app: &AppHandle, prefs: &Overlay) -> Result<()> {
    let prefs = prefs.clone();
    visibility_on_main(app, move |app| {
        if let Err(error) = apply_main(&app, &prefs) {
            tracing::warn!(%error, "Apply overlay visibility");
        }
    })
}
fn apply_main(app: &AppHandle, prefs: &Overlay) -> Result<()> {
    clear_background(app);
    // Keep the normal managed window type: Mutter forbids moving notification windows.
    position(app, prefs)?;
    let window = app
        .get_webview_window("overlay")
        .context("Overlay window missing")?;
    let state = app.try_state::<crate::app::AppState>();
    let idle = state
        .as_ref()
        .and_then(|s| s.status().ok())
        .is_none_or(|s| s.state == "idle");
    let held = state.as_ref().is_some_and(|s| s.overlay_held());
    if !prefs.enabled || (prefs.idle_hide && idle && !held && !dragging()) {
        window.hide().context("Hide idle overlay")?;
        return Ok(());
    }
    show_interactive(&window)
}
pub fn state(app: &AppHandle, state: &str, prefs: &Overlay) -> Result<()> {
    app.emit("state", json!({"state":state}))
        .context("Emit state")?;
    if prefs.enabled {
        reveal(app, prefs)?;
    }
    Ok(())
}
/// Show (if enabled) without a dictation state change, e.g. when a suggestion becomes ready.
/// Re-clamps placement when the window was hidden (displays may have changed).
pub fn reveal(app: &AppHandle, prefs: &Overlay) -> Result<()> {
    let prefs = prefs.clone();
    visibility_on_main(app, move |app| {
        if let Err(error) = reveal_main(&app, &prefs) {
            tracing::warn!(%error, "Reveal overlay");
        }
    })
}
fn reveal_main(app: &AppHandle, prefs: &Overlay) -> Result<()> {
    if !prefs.enabled {
        return Ok(());
    }
    if let Some(window) = app.get_webview_window("overlay") {
        if !window.is_visible().unwrap_or(false) {
            if let Err(error) = position(app, prefs) {
                tracing::warn!(%error, "Overlay placement");
            }
        }
        show_interactive(&window)?;
    }
    Ok(())
}
pub fn hide(app: &AppHandle) -> Result<()> {
    visibility_on_main(app, |app| {
        if let Err(error) = hide_main(&app) {
            tracing::warn!(%error, "Hide overlay");
        }
    })
}
fn hide_main(app: &AppHandle) -> Result<()> {
    {
        let mut rt = runtime();
        if rt.drag.is_some() {
            rt.hide_pending = true;
            return Ok(());
        }
    }
    if app
        .try_state::<crate::app::AppState>()
        .is_some_and(|state| {
            state.overlay_held()
                || state
                    .status()
                    .is_ok_and(|s| matches!(s.state.as_str(), "listening" | "transcribing"))
        })
    {
        return Ok(());
    }
    if let Some(window) = app.get_webview_window("overlay") {
        window.hide().context("Hide overlay")?;
    }
    Ok(())
}
#[cfg(target_os = "linux")]
fn apply_native_cursor(window: &gtk::ApplicationWindow, busy: bool) -> Result<()> {
    use gtk::prelude::*;
    if busy {
        let gdk = window.window().context("Overlay GTK window not realized")?;
        let region =
            gtk::cairo::Region::create_rectangle(&gtk::cairo::RectangleInt::new(0, 0, 1, 1));
        gdk.input_shape_combine_region(&region, 0, 0);
        gdk.display().flush();
    } else {
        window.input_shape_combine_region(None);
    }
    Ok(())
}

// A compositor's move grab can consume synthetic keyboard events. Let the user
// release it before injection, while busy blocks another gesture. Called only on
// the controller worker; the UI and drag watchdog continue independently.
fn wait_for_drag_release(mut moving: impl FnMut() -> bool, timeout: Duration) -> Result<()> {
    let deadline = Instant::now() + timeout;
    while moving() {
        anyhow::ensure!(
            Instant::now() < deadline,
            "Overlay move did not release before text delivery"
        );
        std::thread::sleep(Duration::from_millis(25));
    }
    Ok(())
}

/// Click-through while text is being injected so a stray click cannot land on the overlay.
pub fn set_busy(app: &AppHandle, busy: bool) -> Result<()> {
    runtime().busy = busy;
    if busy {
        if let Err(error) =
            wait_for_drag_release(dragging, movement::MAX_DRAG + Duration::from_secs(2))
        {
            runtime().busy = false;
            return Err(error);
        }
    }
    let handle = app.clone();
    let action = move || {
        if let Some(window) = handle.get_webview_window("overlay") {
            if window.is_visible().context("Check overlay visibility")? {
                if runtime().busy != busy {
                    return Ok(()); // timed-out or superseded request
                }
                #[cfg(target_os = "linux")]
                let updated = window
                    .gtk_window()
                    .context("Get overlay GTK window")
                    .and_then(|gtk| apply_native_cursor(&gtk, busy));
                #[cfg(not(target_os = "linux"))]
                let updated = window
                    .set_ignore_cursor_events(busy)
                    .context("Set overlay cursor handling");
                if let Err(error) = updated {
                    tracing::warn!(%error, "Overlay cursor handling unavailable; hiding it");
                    window.hide().context("Hide unsupported overlay")?;
                }
            }
        }
        Ok(())
    };
    if busy {
        // Called by the controller worker, never by the UI thread. No mutex is held here.
        let result = queue_busy_ack(|task| app.run_on_main_thread(move || task()), action);
        if result.is_err() {
            runtime().busy = false;
            // A timed-out dispatch may still be in flight. Queue restoration after it.
            let handle = app.clone();
            let _ = visibility_on_main(app, move |_| {
                if let Some(window) = handle.get_webview_window("overlay") {
                    #[cfg(target_os = "linux")]
                    if let Ok(gtk) = window.gtk_window() {
                        let _ = apply_native_cursor(&gtk, false);
                    }
                    #[cfg(not(target_os = "linux"))]
                    let _ = window.set_ignore_cursor_events(false);
                }
            });
        }
        result
    } else {
        visibility_on_main(app, move |_| {
            if let Err(error) = action() {
                tracing::warn!(%error, "Restore overlay cursor handling");
            }
        })
    }
}
#[cfg(test)]
mod visibility_tests {
    use super::queue_visibility;
    use std::sync::mpsc;
    use std::time::{Duration, Instant};

    #[test]
    fn native_move_release_precedes_injection() {
        use std::sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        };
        let held = Arc::new(AtomicBool::new(true));
        let (send, receive) = mpsc::channel();
        let moving = held.clone();
        let worker = std::thread::spawn(move || {
            super::wait_for_drag_release(|| moving.load(Ordering::Acquire), Duration::from_secs(2))
                .unwrap();
            send.send(()).unwrap();
        });
        assert!(receive.recv_timeout(Duration::from_millis(100)).is_err());
        held.store(false, Ordering::Release);
        receive.recv_timeout(Duration::from_secs(1)).unwrap();
        worker.join().unwrap();
        assert!(super::wait_for_drag_release(|| true, Duration::ZERO).is_err());
    }

    #[test]
    fn stalled_main_executor_blocks_injection_until_click_through_ack() {
        let (queue, main) = mpsc::channel::<Box<dyn FnOnce() + Send>>();
        let (ready, injection) = mpsc::channel::<()>();
        let worker = std::thread::spawn(move || {
            super::queue_busy_ack(|task| queue.send(task), || Ok(())).unwrap();
            ready.send(()).unwrap(); // injection may start only after native update
        });
        let task = main.recv_timeout(Duration::from_secs(1)).unwrap();
        assert!(injection.recv_timeout(Duration::from_millis(100)).is_err());
        task();
        injection.recv_timeout(Duration::from_secs(1)).unwrap();
        worker.join().unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn acknowledgement_follows_native_x11_input_shape() {
        use gtk::prelude::*;
        use x11rb::protocol::shape::{ConnectionExt as _, SK};
        gtk::init().unwrap();
        let app = gtk::Application::new(Some("com.murmur.native-ack-test"), Default::default());
        // Registration can lose a transient race with the GTK lock when the test binary
        // runs other tests in parallel; retry briefly and report the real error if it stays shut.
        let mut failure = None;
        for _ in 0..20 {
            match app.register(None::<&gtk::gio::Cancellable>) {
                Ok(()) => {
                    failure = None;
                    break;
                }
                Err(error) => {
                    failure = Some(error);
                    std::thread::sleep(Duration::from_millis(50));
                }
            }
        }
        if let Some(error) = failure {
            panic!("GTK application registration unavailable: {error}");
        }
        let window = gtk::ApplicationWindow::new(&app);
        window.set_default_size(608, 160);
        window.set_resizable(false);
        window.show_all();
        // Regression: resize() alone leaves a non-resizable GTK window at 608x200.
        for (w, h) in [(144, 66), (200, 100)] {
            super::resize_gtk(&window, w, h);
            window.resize(w, h);
            let deadline = Instant::now() + Duration::from_secs(1);
            while window.size() != (w, h) && Instant::now() < deadline {
                while gtk::glib::MainContext::default().iteration(false) {}
                std::thread::sleep(Duration::from_millis(5));
            }
            assert_eq!(window.size(), (w, h));
            assert!(!window.is_resizable());
        }
        let gdk = window.window().unwrap();
        let xid = gdk.downcast::<gdkx11::X11Window>().unwrap().xid() as u32;
        let (conn, _) = x11rb::connect(None).unwrap();
        let shape = || {
            conn.shape_get_rectangles(xid, SK::INPUT)
                .unwrap()
                .reply()
                .unwrap()
                .rectangles
                .into_iter()
                .map(|r| (r.x, r.y, r.width, r.height))
                .collect::<Vec<_>>()
        };
        assert_eq!(shape(), vec![(0, 0, 200, 100)]);
        let (queue, main) = mpsc::channel::<Box<dyn FnOnce() + Send>>();
        let (ready, injection) = mpsc::channel::<()>();
        let worker = std::thread::spawn(move || {
            super::queue_busy_ack(
                |task| queue.send(task),
                || {
                    // The production native boundary, not a simulated acknowledgement.
                    let window = gtk::Window::list_toplevels()
                        .into_iter()
                        .find_map(|w| w.downcast::<gtk::ApplicationWindow>().ok())
                        .unwrap();
                    super::apply_native_cursor(&window, true)
                },
            )
            .unwrap();
            ready.send(()).unwrap();
        });
        // The queued closure executes on this GTK thread, not on the worker.
        let task = main.recv_timeout(Duration::from_secs(1)).unwrap();
        assert!(injection.recv_timeout(Duration::from_millis(100)).is_err());
        task();
        injection.recv_timeout(Duration::from_secs(1)).unwrap();
        assert_eq!(shape(), vec![(0, 0, 1, 1)]);
        worker.join().unwrap();
    }

    #[test]
    fn stalled_executor_returns_explicit_timeout() {
        let (queue, _main) = mpsc::channel::<Box<dyn FnOnce() + Send>>();
        let start = Instant::now();
        let error = super::queue_busy_ack(|task| queue.send(task), || Ok(())).unwrap_err();
        assert!(error.to_string().contains("timed out"));
        assert!(start.elapsed() < Duration::from_secs(3));
    }

    #[test]
    fn queued_window_query_does_not_block_worker_or_second_dispatch() {
        let (queue, main) = mpsc::channel::<Box<dyn FnOnce() + Send>>();
        let (release, wait) = mpsc::channel::<()>();
        let start = Instant::now();
        queue_visibility(
            |task| queue.send(task),
            move || {
                wait.recv().unwrap();
            },
        )
        .unwrap();
        queue_visibility(|task| queue.send(task), || {}).unwrap();
        assert!(start.elapsed() < Duration::from_millis(250));
        let first = main.recv_timeout(Duration::from_secs(1)).unwrap();
        let second = main.recv_timeout(Duration::from_secs(1)).unwrap();
        let executor = std::thread::spawn(move || {
            first();
            second();
        });
        release.send(()).unwrap();
        executor.join().unwrap();
    }
}

pub fn busy() -> bool {
    runtime().busy
}
/// Validated, revisioned size report from the overlay view (logical px). Returns whether the
/// native window was resized.
pub fn set_size(
    app: &AppHandle,
    prefs: &Overlay,
    width: f64,
    height: f64,
    revision: u64,
    instance: &str,
) -> Result<bool> {
    let Some(next) = accept_size(width, height, revision, instance)? else {
        return Ok(false);
    };
    runtime().size = Some(next);
    position(app, prefs)?;
    Ok(true)
}
/// Pure validation for `set_size`; also records the revision.
pub fn accept_size(
    width: f64,
    height: f64,
    revision: u64,
    instance: &str,
) -> Result<Option<(f64, f64)>> {
    if instance.len() > 128 || instance.is_empty() {
        anyhow::bail!("Invalid overlay layout instance");
    }
    if !(width.is_finite() && height.is_finite()) || width <= 0.0 || height <= 0.0 {
        anyhow::bail!("Overlay size must be finite and positive");
    }
    let mut rt = runtime();
    if rt.layout_instance != instance {
        rt.layout_instance = instance.to_owned();
        rt.layout_revision = 0;
        rt.size = None;
    }
    if revision <= rt.layout_revision {
        return Ok(None);
    }
    rt.layout_revision = revision;
    let next = (
        width.clamp(MIN_SIZE.0, MAX_SIZE.0),
        height.clamp(MIN_SIZE.1, MAX_SIZE.1),
    );
    if rt
        .size
        .is_some_and(|(w, h)| (next.0 - w).abs() < 1.0 && (next.1 - h).abs() < 1.0)
    {
        return Ok(None);
    }
    Ok(Some(next))
}
/// Only explicit handle gestures own placement. Late programmatic events outside a
/// gesture cannot be mistaken for user movement, regardless of dispatch latency.
pub fn moved(position: PhysicalPosition<i32>) {
    runtime().moved((position.x, position.y), Instant::now());
}

pub fn dragging() -> bool {
    runtime().drag.is_some()
}

/// Explicit preset/reset/disable wins over a pending or active gesture.
pub fn reset_placement() {
    runtime().reset();
}

pub fn pending_placement() -> Option<(u64, Placement)> {
    runtime().pending(Instant::now())
}

pub fn placement_saved(generation: u64, success: bool) {
    runtime().saved(generation, success, Instant::now());
}

#[cfg(test)]
mod placement_runtime_tests {
    use super::*;

    fn saved_position() -> Placement {
        Placement {
            monitor_width: 1920,
            monitor_height: 1080,
            x_fraction: 0.2,
            y_fraction: 0.4,
            ..Placement::default()
        }
    }

    #[test]
    fn resize_defers_during_drag_and_uses_unsaved_position_after_release() {
        let now = Instant::now();
        let mut rt = Runtime {
            drag: Some(Drag::new((100, 100), now)),
            ..Runtime::default()
        };
        rt.size = Some((600.0, 240.0)); // newest measurement is retained
        assert!(rt.prefs(&Overlay::default()).is_none());
        rt.drag = None;
        rt.placement = Some(saved_position());
        rt.unsaved = true;
        let prefs = rt.prefs(&Overlay::default()).unwrap();
        assert_eq!(prefs.position, "custom");
        assert_eq!(prefs.placement.x_fraction, 0.2);
        assert_eq!(rt.size, Some((600.0, 240.0)));
    }

    #[test]
    fn programmatic_events_do_not_create_or_overwrite_pending_user_placement() {
        let mut rt = Runtime::default();
        rt.moved((100, 200), Instant::now());
        assert!(rt.pending(Instant::now()).is_none());
        rt.placement = Some(saved_position());
        rt.unsaved = true;
        rt.moved((800, 900), Instant::now()); // delayed old set_position acknowledgement
        assert_eq!(rt.pending(Instant::now()).unwrap().1.x_fraction, 0.2);
    }

    #[test]
    fn failed_save_retries_and_stale_ack_cannot_clear_newer_placement() {
        let now = Instant::now();
        let mut rt = Runtime {
            generation: 2,
            placement: Some(saved_position()),
            unsaved: true,
            ..Runtime::default()
        };
        rt.saved(2, false, now);
        assert!(rt.pending(now).is_none());
        assert!(rt.pending(now + Duration::from_secs(1)).is_some());
        rt.saved(1, true, now);
        assert!(rt.unsaved);
        rt.saved(2, true, now);
        assert!(rt.pending(now).is_none());
        assert_eq!(
            rt.prefs(&Overlay::default()).unwrap().placement.x_fraction,
            0.2
        );
    }

    #[test]
    fn reset_cancels_active_drag_and_pending_save() {
        let mut rt = Runtime {
            generation: 2,
            placement: Some(saved_position()),
            unsaved: true,
            drag: Some(Drag::new((100, 100), Instant::now())),
            ..Runtime::default()
        };
        rt.reset();
        assert_eq!(rt.generation, 3);
        assert!(rt.pending(Instant::now()).is_none());
        assert!(rt.drag.as_ref().unwrap().cancelled);
        assert!(rt.prefs(&Overlay::default()).is_none());
        rt.drag = None; // release of the cancelled native grab
        assert_eq!(
            rt.prefs(&Overlay::default()).unwrap().position,
            "bottom-center"
        );
    }
}
