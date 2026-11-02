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
use tauri::{AppHandle, Manager, State};

type Reply<T> = Result<T, String>;
fn error(e: impl std::fmt::Display) -> String {
    e.to_string()
}
async fn blocking<T: Send + 'static>(f: impl FnOnce() -> anyhow::Result<T> + Send + 'static) -> Reply<T> {
    tauri::async_runtime::spawn_blocking(f).await.map_err(error)?.map_err(|e| format!("{e:#}"))
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
pub async fn list_models(app: AppHandle) -> Reply<Value> {
    blocking(move || app.state::<AppState>().engine.request("models.list", json!({}))).await
}
#[tauri::command]
pub async fn install_model(app: AppHandle, name: String) -> Reply<Value> {
    blocking(move || {
        let state = app.state::<AppState>();
        let result = state.engine.request("models.install", json!({"name":name}))?;
        state.refresh_engine()?;
        Ok(result)
    })
    .await
}
#[tauri::command]
pub async fn remove_model(app: AppHandle, name: String) -> Reply<Value> {
    blocking(move || {
        let state = app.state::<AppState>();
        if state.status()?.state != "idle" {
            anyhow::bail!("Stop dictation before removing a model");
        }
        let result = state.engine.request("models.remove", json!({"name":name}))?;
        state.refresh_engine()?;
        Ok(result)
    })
    .await
}
#[tauri::command]
pub async fn test_engine(app: AppHandle) -> Reply<Value> {
    blocking(move || {
        let state = app.state::<AppState>();
        let config = state.config()?;
        // A repeatable three-second silent inference probe; VAD may correctly return empty text.
        state.engine.request("transcribe", controller::params(&vec![0.0; 48_000], &config))
    })
    .await
}
#[tauri::command]
pub async fn list_devices() -> Reply<Vec<audio::DeviceInfo>> {
    blocking(audio::list_devices).await
}
#[tauri::command]
pub async fn set_hotkey(app: AppHandle, accel: String) -> Reply<Config> {
    blocking(move || app.state::<AppState>().update(json!({"general":{"hotkey":accel}}))).await
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
            "config" => state.paths.config.parent().ok_or_else(|| anyhow::anyhow!("Config directory missing"))?,
            "data" | "logs" | "history" => &state.paths.data,
            "models" => &state.paths.models,
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
