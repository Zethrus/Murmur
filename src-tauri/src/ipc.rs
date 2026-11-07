use crate::{
    app::{AppState, Status},
    audio,
    config::Config,
    controller::{self, Action},
    history::Entry,
    overlay, platform,
    sound::Cue,
};
use serde_json::{json, Value};
use tauri::{AppHandle, Manager, State, WebviewWindow};

type Reply<T> = Result<T, String>;
fn error(e: impl std::fmt::Display) -> String {
    e.to_string()
}
async fn blocking<T: Send + 'static>(
    f: impl FnOnce() -> anyhow::Result<T> + Send + 'static,
) -> Reply<T> {
    tauri::async_runtime::spawn_blocking(f)
        .await
        .map_err(error)?
        .map_err(|e| format!("{e:#}"))
}
#[tauri::command]
pub fn get_config(state: State<'_, AppState>) -> Reply<Config> {
    state.config().map_err(error)
}
#[tauri::command]
pub async fn set_config(app: AppHandle, patch: Value) -> Reply<Config> {
    blocking(move || app.state::<AppState>().update(patch)).await
}
#[tauri::command]
pub fn get_status(state: State<'_, AppState>) -> Reply<Status> {
    state.status().map_err(error)
}
#[tauri::command]
pub fn companion_status(state: State<'_, AppState>) -> Reply<crate::companion::Status> {
    Ok(state.companion.status())
}
/// Re-push the button map after a settings change.
#[tauri::command]
pub fn companion_resync(state: State<'_, AppState>) -> Reply<()> {
    state.companion.config_changed();
    Ok(())
}
/// Candidate serial devices for the Settings picker.
#[tauri::command]
pub fn companion_ports() -> Reply<Vec<String>> {
    Ok(crate::companion::link::usb::candidates())
}
#[tauri::command]
pub fn companion_network_status(
    state: State<'_, AppState>,
) -> Reply<crate::companion::network::Status> {
    Ok(state.companion_network.status())
}
#[tauri::command]
pub fn companion_pairing_code(
    state: State<'_, AppState>,
) -> Reply<crate::companion::network::auth::Pairing> {
    if !state.config().map_err(error)?.companion.network.enabled {
        return Err("Enable the phone listener first".into());
    }
    state.companion_network.pairing().map_err(error)
}
#[tauri::command]
pub fn companion_revoke_device(state: State<'_, AppState>, id: String) -> Reply<bool> {
    state.companion_network.revoke(&id).map_err(error)
}
fn component_error(error: crate::components::ComponentError) -> String {
    error.message
}
/// Everything the setup wizard and the Components page show. Local only: no network.
#[tauri::command]
pub async fn components_snapshot(app: AppHandle) -> Reply<Value> {
    blocking(move || Ok(app.state::<AppState>().components.snapshot())).await
}
/// Queue installs. Called only from an explicit click in Settings.
#[tauri::command]
pub fn components_install(
    window: WebviewWindow,
    state: State<'_, AppState>,
    ids: Vec<String>,
) -> Reply<()> {
    require_window(&window, "settings")?;
    state.components.install(&ids).map_err(component_error)
}
/// Cancel (discard partial download) or pause (keep it) a queued or running job.
#[tauri::command]
pub fn components_stop(
    window: WebviewWindow,
    state: State<'_, AppState>,
    id: String,
    pause: bool,
) -> Reply<()> {
    require_window(&window, "settings")?;
    state.components.stop(&id, pause).map_err(component_error)
}
#[tauri::command]
pub fn components_resume(
    window: WebviewWindow,
    state: State<'_, AppState>,
    id: String,
) -> Reply<()> {
    require_window(&window, "settings")?;
    state.components.resume(&id).map_err(component_error)
}
#[tauri::command]
pub fn components_discard(
    window: WebviewWindow,
    state: State<'_, AppState>,
    id: String,
) -> Reply<()> {
    require_window(&window, "settings")?;
    state.components.discard(&id).map_err(component_error)
}
#[tauri::command]
pub async fn components_remove(app: AppHandle, window: WebviewWindow, id: String) -> Reply<()> {
    require_window(&window, "settings")?;
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        if state.status().map(|s| s.state != "idle").unwrap_or(true) {
            return Err("Stop dictation before removing a component".to_owned());
        }
        state.components.remove(&id).map_err(component_error)
    })
    .await
    .map_err(error)?
}
#[tauri::command]
pub async fn components_verify(app: AppHandle, window: WebviewWindow, id: String) -> Reply<Value> {
    require_window(&window, "settings")?;
    tauri::async_runtime::spawn_blocking(move || {
        app.state::<AppState>()
            .components
            .verify(&id)
            .map(|v| json!({"verdict": v}))
            .map_err(component_error)
    })
    .await
    .map_err(error)?
}
/// Explicit "Check for updates": the only network request outside an install.
#[tauri::command]
pub async fn components_check_updates(app: AppHandle, window: WebviewWindow) -> Reply<()> {
    require_window(&window, "settings")?;
    tauri::async_runtime::spawn_blocking(move || {
        app.state::<AppState>()
            .components
            .check_updates()
            .map_err(component_error)
    })
    .await
    .map_err(error)?
}
#[tauri::command]
pub async fn test_engine(app: AppHandle) -> Reply<Value> {
    blocking(move || {
        let state = app.state::<AppState>();
        let config = state.config()?;
        // Silence with VAD disabled still iterates the model's inference generator.
        let mut result = state
            .engine
            .request("transcribe", engine_probe_params(&config))?;
        state.refresh_engine()?;
        result["text"] = json!("");
        Ok(result)
    })
    .await
}

fn engine_probe_params(config: &crate::config::Config) -> Value {
    let mut params = controller::params(&vec![0.0; 48_000], config);
    params["vad"] = json!(false);
    params
}

#[cfg(test)]
mod engine_probe_tests {
    #[test]
    fn probe_forces_inference_without_changing_saved_vad() {
        let config = crate::config::Config::default();
        assert!(config.engine.vad_filter);
        assert_eq!(super::engine_probe_params(&config)["vad"], false);
        assert!(config.engine.vad_filter);
    }
}
#[tauri::command]
pub async fn list_devices() -> Reply<Vec<audio::DeviceInfo>> {
    blocking(audio::list_devices).await
}
/// Windows microphone privacy state for the Audio page (never blocked elsewhere).
#[tauri::command]
pub async fn microphone_access() -> Reply<platform::MicrophoneAccess> {
    blocking(|| Ok(platform::microphone_access())).await
}
#[tauri::command]
pub async fn open_microphone_settings() -> Reply<()> {
    blocking(platform::open_microphone_settings).await
}
#[tauri::command]
pub async fn set_hotkey(app: AppHandle, accel: String) -> Reply<Config> {
    blocking(move || {
        app.state::<AppState>()
            .update(json!({"general":{"hotkey":accel}}))
    })
    .await
}
#[tauri::command]
pub fn preview_sound(state: State<'_, AppState>, kind: String) -> Reply<()> {
    let cue = match kind.as_str() {
        "start" => Cue::Start,
        "stop" => Cue::Stop,
        "error" => Cue::Error,
        _ => return Err("Unknown cue kind".into()),
    };
    state.cue(cue);
    Ok(())
}
#[tauri::command]
pub fn start_dictation(state: State<'_, AppState>) -> Reply<()> {
    state.send(Action::Start).map_err(error)
}
#[tauri::command]
pub fn stop_dictation(state: State<'_, AppState>) -> Reply<()> {
    state.send(Action::Stop).map_err(error)
}
#[tauri::command]
pub fn cancel_dictation(state: State<'_, AppState>) -> Reply<()> {
    state.send(Action::Cancel).map_err(error)
}
#[tauri::command]
pub fn open_settings(app: AppHandle) -> Reply<()> {
    platform::open_settings(&app).map_err(error)
}
#[tauri::command]
pub fn quit_app(app: AppHandle) -> Reply<()> {
    app.exit(0);
    Ok(())
}
#[tauri::command]
pub async fn history_search(app: AppHandle, q: String, limit: u32) -> Reply<Vec<Entry>> {
    blocking(move || {
        app.state::<AppState>().history(|history| match history {
            Some(h) => h.search(&q, limit),
            None => Ok(Vec::new()),
        })
    })
    .await
}
#[tauri::command]
pub async fn history_clear(app: AppHandle) -> Reply<()> {
    blocking(move || {
        app.state::<AppState>().history(|history| {
            if let Some(h) = history {
                h.clear()?;
            }
            Ok(())
        })
    })
    .await
}
#[tauri::command]
pub async fn pick_exclude_app(app: AppHandle) -> Reply<String> {
    blocking(move || {
        if let Some(window) = app.get_webview_window("settings") {
            window.hide()?;
        }
        std::thread::sleep(std::time::Duration::from_secs(3));
        let focused = platform::focused_app(&app);
        platform::open_settings(&app)?;
        focused
    })
    .await
}
#[tauri::command]
pub async fn open_path(app: AppHandle, kind: String) -> Reply<()> {
    blocking(move || {
        let state = app.state::<AppState>();
        let path = match kind.as_str() {
            "config" => state
                .paths
                .config
                .parent()
                .ok_or_else(|| anyhow::anyhow!("Config directory missing"))?,
            "data" | "logs" | "history" => &state.paths.data,
            "models" => &state.paths.models,
            "components" => &state.components.store.root,
            "enhance_models" => &state.paths.enhance_models,
            _ => anyhow::bail!("Unknown app folder kind"),
        };
        platform::open_path(path)
    })
    .await
}
#[tauri::command]
pub fn apply_overlay_prefs(app: AppHandle, state: State<'_, AppState>) -> Reply<()> {
    overlay::apply(&app, &state.config().map_err(error)?.overlay).map_err(error)
}

fn require_window(window: &WebviewWindow, label: &str) -> Reply<()> {
    if window.label() == label {
        Ok(())
    } else {
        Err(format!(
            "This command is only available from the {label} window"
        ))
    }
}
#[tauri::command]
pub fn get_enhance_status(state: State<'_, AppState>) -> Reply<Value> {
    Ok(state.enhance.service.status_json())
}
#[tauri::command]
pub fn get_enhance_model(state: State<'_, AppState>) -> Reply<Value> {
    let config = state.config().map_err(error)?;
    Ok(state.enhance.service.model_json(&config.enhance.model))
}
#[tauri::command]
pub fn install_enhance_model(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, AppState>,
    model_id: String,
    enable_after_install: bool,
) -> Reply<Value> {
    require_window(&window, "settings")?;
    let id = state
        .enhance
        .install(&app, &model_id, enable_after_install)?;
    Ok(json!({ "operation_id": id }))
}
#[tauri::command]
pub async fn cancel_enhance_download(app: AppHandle, operation_id: String) -> Reply<()> {
    let state = app.state::<AppState>();
    if state.enhance.service.removing() {
        return Err("Busy: model removal is running".into());
    }
    if let Some(id) = crate::enhance::runtime::parse_id(&operation_id) {
        return tauri::async_runtime::spawn_blocking(move || {
            let state = app.state::<AppState>();
            if !state.cancel_enhance_download(id).map_err(error)?
                && state.enhance.service.removing()
            {
                return Err("Busy: model removal is running".into());
            }
            Ok(())
        })
        .await
        .map_err(error)?;
    }
    Ok(())
}
#[tauri::command]
pub async fn remove_enhance_model(
    app: AppHandle,
    window: WebviewWindow,
    model_id: String,
) -> Reply<()> {
    require_window(&window, "settings")?;
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        state.enhance.remove(&state, &model_id)
    })
    .await
    .map_err(error)?
}
#[tauri::command]
pub fn rewrite_last_dictation(state: State<'_, AppState>) -> Reply<Value> {
    Ok(state
        .enhance
        .rewrite(&state, crate::enhance::session::Trigger::Manual))
}
#[tauri::command]
pub async fn accept_enhancement(app: AppHandle, suggestion_id: String) -> Reply<Value> {
    let id = crate::enhance::runtime::parse_id(&suggestion_id);
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        state.enhance.accept(&state, id)
    })
    .await
    .map_err(error)
}
#[tauri::command]
pub fn discard_enhancement(state: State<'_, AppState>, suggestion_id: String) -> Reply<()> {
    let id = crate::enhance::runtime::parse_id(&suggestion_id);
    state.enhance.discard(&state, id);
    Ok(())
}
fn held_action(state: &AppState, held_id: &str, action: fn(Option<u64>) -> Action) -> Reply<()> {
    let id = crate::enhance::runtime::parse_id(held_id);
    state.send(action(id)).map_err(error)
}
/// Type the held transcript into the window focused now.
#[tauri::command]
pub fn insert_held(state: State<'_, AppState>, held_id: String) -> Reply<()> {
    held_action(&state, &held_id, Action::InsertHeld)
}
#[tauri::command]
pub fn copy_held(state: State<'_, AppState>, held_id: String) -> Reply<()> {
    held_action(&state, &held_id, Action::CopyHeld)
}
#[tauri::command]
pub fn discard_held(state: State<'_, AppState>, held_id: String) -> Reply<()> {
    held_action(&state, &held_id, Action::DiscardHeld)
}
#[tauri::command]
pub fn get_overlay_snapshot(state: State<'_, AppState>) -> Reply<Value> {
    Ok(state.enhance.snapshot(&state))
}
#[tauri::command]
pub async fn reset_overlay_position(app: AppHandle) -> Reply<()> {
    blocking(move || {
        overlay::reset_placement();
        app.state::<AppState>().update(json!({"overlay":{
            "position":"bottom-center",
            "placement":{"monitor_name":"","monitor_x":0,"monitor_y":0,"monitor_width":0,
                "monitor_height":0,"x_fraction":0.5,"y_fraction":1.0}
        }}))?;
        Ok(())
    })
    .await
}
#[tauri::command]
pub fn set_overlay_size(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, AppState>,
    width: f64,
    height: f64,
    layout_revision: u64,
    layout_instance: String,
) -> Reply<()> {
    require_window(&window, "overlay")?;
    let prefs = state.config().map_err(error)?.overlay;
    overlay::set_size(
        &app,
        &prefs,
        width,
        height,
        layout_revision,
        &layout_instance,
    )
    .inspect_err(|error| tracing::warn!(%error, "overlay sizing failed"))
    .map_err(error)?;
    Ok(())
}

#[tauri::command]
pub async fn start_overlay_drag(app: AppHandle, window: WebviewWindow) -> Reply<()> {
    require_window(&window, "overlay")?;
    overlay::drag(app)
        .await
        .inspect_err(|error| tracing::warn!(%error, "overlay drag failed"))
        .map_err(error)
}
