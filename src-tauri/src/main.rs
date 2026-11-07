#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod app;
mod audio;
#[cfg(target_os = "linux")]
mod autostart;
#[cfg(test)]
mod coding_tests;
mod companion;
mod components;
mod config;
mod controller;
mod engine;
mod enhance;
mod focus_guard;
mod held;
mod history;
mod hotkey;
mod inject;
mod ipc;
mod overlay;
#[cfg(test)]
mod overlay_tests;
mod platform;
mod postproc;
mod punctuation;
mod sound;

use tauri::{
    menu::{CheckMenuItem, Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};

fn setup(app: &mut tauri::App) -> anyhow::Result<()> {
    let handle = app.handle().clone();
    let overlay_view = handle
        .get_webview_window("overlay")
        .ok_or_else(|| anyhow::anyhow!("Overlay webview unavailable"))?;
    anyhow::ensure!(
        overlay_view
            .resolve_command_scope::<serde_json::Value>("window", "start_dragging")?
            .is_some(),
        "Overlay start-dragging permission denied by the runtime ACL"
    );
    let runtime = app::AppState::new(handle.clone())?;
    app.manage(runtime);
    let state = app.state::<app::AppState>();
    state.companion.start();
    state.companion_network.start();
    // Recoverable desktop limitations/conflicts must not prevent opening Settings.
    for result in [
        hotkey::rebind(&handle, &state.config()?, &state.config()?),
        overlay::apply(&handle, &state.config()?.overlay),
        platform::set_autostart(&handle, state.config()?.general.autostart),
    ] {
        if let Err(error) = result {
            state.error(&format!("{error:#}"));
        }
    }
    let label = MenuItem::with_id(app, "state", "idle", false, None::<&str>)?;
    let dictate = MenuItem::with_id(
        app,
        "dictate",
        "Dictate",
        state.config()?.general.hotkey_mode == "toggle",
        None::<&str>,
    )?;
    let mute = CheckMenuItem::with_id(
        app,
        "mute",
        "Sounds mute",
        true,
        state.config()?.sounds.muted,
        None::<&str>,
    )?;
    let enabled = CheckMenuItem::with_id(
        app,
        "overlay",
        "Overlay enabled",
        true,
        state.config()?.overlay.enabled,
        None::<&str>,
    )?;
    let settings = MenuItem::with_id(app, "settings", "Settings…", true, None::<&str>)?;
    let reload = MenuItem::with_id(app, "reload", "Reload config", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[&label, &dictate, &mute, &enabled, &settings, &reload, &quit],
    )?;
    // The tray uses the single-colour glyph, not the app tile: at 22 px the tile reads as a dark
    // blob, while the blue mic keeps its silhouette against both dark and light panels.
    let icon = tauri::image::Image::from_bytes(include_bytes!("../icons/tray.png"))
        .map_err(|error| anyhow::anyhow!("Load tray icon: {error}"))?;
    TrayIconBuilder::with_id("murmur")
        .icon(icon)
        .tooltip("Murmur — no model")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| {
            let state = app.state::<app::AppState>();
            let result: anyhow::Result<()> = (|| {
                match event.id.as_ref() {
                    "dictate" => {
                        if state.config()?.general.hotkey_mode == "toggle" {
                            state.send(controller::Action::Toggle)?;
                        }
                    }
                    "mute" => {
                        state.send(controller::Action::ToggleMute)?;
                    }
                    "overlay" => {
                        state.send(controller::Action::ToggleOverlay)?;
                    }
                    "settings" => platform::open_settings(app)?,
                    "reload" => state.send(controller::Action::Reload)?,
                    "quit" => app.exit(0),
                    _ => {}
                }
                Ok(())
            })();
            if let Err(error) = result {
                state.error(&error.to_string());
            }
        })
        .on_tray_icon_event(|tray, event| {
            if matches!(
                event,
                TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                }
            ) {
                if let Err(error) = platform::open_settings(tray.app_handle()) {
                    tracing::warn!(%error, "Open settings");
                }
            }
        })
        .build(app)?;
    state.set_tray(app::TrayControls {
        label,
        dictate,
        mute,
        enabled,
    })?;
    state.start()?;
    if show_settings_on_launch(std::env::args_os()) {
        platform::open_settings(&handle)?;
    }
    Ok(())
}

// argv includes the executable. Login startup passes --background; normal
// launches (including a second instance) should bring Settings into view.
fn show_settings_on_launch(args: impl IntoIterator<Item = impl AsRef<std::ffi::OsStr>>) -> bool {
    !args
        .into_iter()
        .skip(1)
        .any(|arg| arg.as_ref() == "--background")
}

fn main() {
    let application = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, args, _| {
            if show_settings_on_launch(args) {
                if let Err(error) = platform::open_settings(app) {
                    tracing::warn!(%error, "Second instance");
                }
            }
        }))
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(
            tauri_plugin_autostart::Builder::new()
                .arg("--background")
                .build(),
        )
        .setup(|app| setup(app).map_err(Into::into))
        .on_window_event(|window, event| {
            if window.label() == "overlay" {
                if let tauri::WindowEvent::Moved(position) = event {
                    overlay::moved(*position);
                }
            }
            if window.label() == "settings" {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    if let Err(error) = window.hide() {
                        tracing::warn!(%error, "Hide settings");
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            ipc::get_config,
            ipc::set_config,
            ipc::get_status,
            ipc::companion_status,
            ipc::companion_resync,
            ipc::companion_ports,
            ipc::companion_network_status,
            ipc::companion_pairing_code,
            ipc::companion_revoke_device,
            ipc::list_models,
            ipc::install_model,
            ipc::remove_model,
            ipc::test_engine,
            ipc::list_devices,
            ipc::set_hotkey,
            ipc::preview_sound,
            ipc::start_dictation,
            ipc::stop_dictation,
            ipc::cancel_dictation,
            ipc::open_settings,
            ipc::quit_app,
            ipc::history_search,
            ipc::history_clear,
            ipc::pick_exclude_app,
            ipc::open_path,
            ipc::apply_overlay_prefs,
            ipc::get_enhance_status,
            ipc::get_enhance_model,
            ipc::install_enhance_model,
            ipc::cancel_enhance_download,
            ipc::remove_enhance_model,
            ipc::rewrite_last_dictation,
            ipc::accept_enhancement,
            ipc::discard_enhancement,
            ipc::insert_held,
            ipc::copy_held,
            ipc::discard_held,
            ipc::get_overlay_snapshot,
            ipc::reset_overlay_position,
            ipc::set_overlay_size,
            ipc::start_overlay_drag
        ])
        .build(tauri::generate_context!());
    match application {
        Ok(app) => app.run(|handle, event| match event {
            tauri::RunEvent::ExitRequested { api, .. } => {
                if let Some(state) = handle.try_state::<app::AppState>() {
                    if !state.closed() {
                        api.prevent_exit();
                        if let Err(error) = state.begin_shutdown() {
                            state.error(&error.to_string());
                        }
                    }
                }
            }
            tauri::RunEvent::Exit => {
                if let Some(state) = handle.try_state::<app::AppState>() {
                    state.join_shutdown();
                }
                inject::stop_helper();
            }
            _ => {}
        }),
        Err(error) => {
            eprintln!("Murmur failed to start: {error:#}");
            std::process::exit(1);
        }
    }
}
