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

fn validate_overlay_drag_acl(webview: &tauri::Webview) -> anyhow::Result<()> {
    anyhow::ensure!(
        webview
            .resolve_command_scope::<serde_json::Value>("window", "start_dragging")?
            .is_some(),
        "Overlay start-dragging permission denied by the runtime ACL"
    );
    Ok(())
}

#[cfg(any(windows, test))]
fn validate_overlay_acl_on_load(label: &str, event: tauri::webview::PageLoadEvent) -> bool {
    label == "overlay" && event == tauri::webview::PageLoadEvent::Finished
}

fn setup(app: &mut tauri::App) -> anyhow::Result<()> {
    let handle = app.handle().clone();
    let overlay_view = handle
        .get_webview_window("overlay")
        .ok_or_else(|| anyhow::anyhow!("Overlay webview unavailable"))?;
    // WebView2 can still report its initial blank URL here. Tauri classifies that as a
    // remote origin, so the local-only capability must be checked after navigation.
    #[cfg(not(windows))]
    validate_overlay_drag_acl(overlay_view.as_ref())?;
    #[cfg(windows)]
    let _ = overlay_view;
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
    // Adopt pre-0.5 models whose hashes match the manifest; local hashing only.
    let migrating = state.components.clone();
    std::thread::Builder::new()
        .name("murmur-migrate".into())
        .spawn(move || migrating.migrate())?;
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

/// Hidden test/maintenance entry point, no window:
/// `murmur --install-component <zip|model dir> --manifest <json> --manifest-sig <sig> [--trust-root <asc>]`.
/// Uses the same verification as the app; a development trust root needs a debug build or
/// MURMUR_DEV_TRUST_ROOT_ACK (see components::trust).
fn install_component_cli(args: &[std::ffi::OsString]) -> Option<i32> {
    let position = args.iter().position(|a| a == "--install-component")?;
    let value = |flag: &str| {
        args.iter()
            .position(|a| a == flag)
            .and_then(|i| args.get(i + 1))
            .map(std::path::PathBuf::from)
    };
    let (Some(path), Some(manifest), Some(signature)) = (
        args.get(position + 1).map(std::path::PathBuf::from),
        value("--manifest"),
        value("--manifest-sig"),
    ) else {
        eprintln!("usage: murmur --install-component <zip|dir> --manifest <json> --manifest-sig <sig> [--trust-root <asc>]");
        return Some(2);
    };
    if let Some(root) = value("--trust-root") {
        std::env::set_var(components::trust::DEV_TRUST_ROOT_ENV, root);
    }
    let _ = tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .try_init();
    let result = platform::Paths::discover()
        .map_err(|e| e.to_string())
        .and_then(|paths| {
            components::install_from_files(&paths.data, &path, &manifest, &signature)
                .map_err(|e| e.message)
        });
    match result {
        Ok(id) => {
            println!("{}", serde_json::json!({"installed": id}));
            Some(0)
        }
        Err(message) => {
            eprintln!("Component install failed: {message}");
            Some(1)
        }
    }
}

/// `--version` / `--help`: print and exit before any window, tray or single-instance handoff, so
/// CI can smoke-test a build (Windows release builds use the GUI subsystem; output still reaches
/// a redirected stdout).
fn info_cli(args: &[std::ffi::OsString]) -> Option<String> {
    let flag = args.get(1)?.to_str()?;
    match flag {
        "--version" | "-V" => Some(format!("murmur {}", env!("CARGO_PKG_VERSION"))),
        "--help" | "-h" => Some(format!(
            "murmur {} — local, private dictation\n\n\
             Usage: murmur [--background]\n\
             \x20 --background   start in the tray without opening Settings (login start)\n\
             \x20 --version      print the version\n\
             \x20 --help         print this help\n\n\
             The speech engine and models are downloaded by the first-run setup.",
            env!("CARGO_PKG_VERSION")
        )),
        _ => None,
    }
}

fn main() {
    let args: Vec<_> = std::env::args_os().collect();
    if let Some(text) = info_cli(&args) {
        println!("{text}");
        return;
    }
    if let Some(code) = install_component_cli(&args) {
        std::process::exit(code);
    }
    let application_builder = tauri::Builder::default();
    #[cfg(windows)]
    let application_builder = application_builder.on_page_load(|webview, payload| {
        if validate_overlay_acl_on_load(webview.label(), payload.event()) {
            if let Err(error) = validate_overlay_drag_acl(webview) {
                eprintln!("Murmur failed to start: {error:#}");
                webview.app_handle().exit(1);
            }
        }
    });
    let application = application_builder
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
                    // Closing the window during first-run setup means "Set up later".
                    let app = window.app_handle().clone();
                    let _ = std::thread::Builder::new()
                        .name("murmur-setup-postpone".into())
                        .spawn(move || {
                            if let Some(state) = app.try_state::<app::AppState>() {
                                state.postpone_setup_if_pending();
                            }
                        });
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
            ipc::components_snapshot,
            ipc::components_install,
            ipc::components_stop,
            ipc::components_resume,
            ipc::components_discard,
            ipc::components_remove,
            ipc::components_verify,
            ipc::components_check_updates,
            ipc::test_engine,
            ipc::list_devices,
            ipc::microphone_access,
            ipc::open_microphone_settings,
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
            tauri::RunEvent::ExitRequested { api, code, .. } => {
                // A denied startup capability is fatal; preserve its failure exit code.
                if !cfg!(windows) || code != Some(1) {
                    if let Some(state) = handle.try_state::<app::AppState>() {
                        if !state.closed() {
                            api.prevent_exit();
                            if let Err(error) = state.begin_shutdown() {
                                state.error(&error.to_string());
                            }
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

#[cfg(test)]
mod cli_tests {
    use super::info_cli;
    use std::ffi::OsString;
    fn args(list: &[&str]) -> Vec<OsString> {
        list.iter().map(OsString::from).collect()
    }
    #[test]
    fn version_and_help_print_without_starting_the_app() {
        let version = info_cli(&args(&["murmur", "--version"])).unwrap();
        assert_eq!(version, format!("murmur {}", env!("CARGO_PKG_VERSION")));
        assert!(info_cli(&args(&["murmur", "--help"]))
            .unwrap()
            .contains("--background"));
        assert_eq!(info_cli(&args(&["murmur"])), None);
        assert_eq!(info_cli(&args(&["murmur", "--background"])), None);
        // Only the first argument counts: `--install-component x --help` is not help.
        assert_eq!(
            info_cli(&args(&["murmur", "--install-component", "--help"])),
            None
        );
    }
}

#[cfg(test)]
mod overlay_acl_tests {
    use super::validate_overlay_acl_on_load;
    use tauri::webview::PageLoadEvent;

    #[test]
    fn windows_acl_check_waits_for_completed_overlay_navigation() {
        assert!(!validate_overlay_acl_on_load(
            "overlay",
            PageLoadEvent::Started
        ));
        assert!(!validate_overlay_acl_on_load(
            "settings",
            PageLoadEvent::Finished
        ));
        assert!(validate_overlay_acl_on_load(
            "overlay",
            PageLoadEvent::Finished
        ));
    }

    #[test]
    fn overlay_drag_capability_is_local_and_scoped_to_overlay() {
        let capability: serde_json::Value =
            serde_json::from_str(include_str!("../capabilities/overlay.json")).unwrap();
        assert_eq!(capability["windows"], serde_json::json!(["overlay"]));
        assert_eq!(
            capability["permissions"],
            serde_json::json!(["core:window:allow-start-dragging"])
        );
        assert!(capability.get("remote").is_none());

        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        assert!(config["app"]["security"]["capabilities"]
            .as_array()
            .unwrap()
            .contains(&serde_json::json!("overlay-drag")));
    }
}
