use crate::{
    components::{self, Components},
    config::{Config, Engine as EngineConfig, Placement},
    controller::{self, Action},
    engine::Engine,
    enhance,
    history::History,
    hotkey, overlay,
    platform::{self, Paths},
    sound::{Cue, SoundPlayer},
};
use anyhow::{Context, Result};
use notify::{RecommendedWatcher, RecursiveMode, Watcher};
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex, MutexGuard,
    },
    thread::{self, JoinHandle},
};
use tauri::{
    menu::{CheckMenuItem, MenuItem},
    AppHandle, Emitter, Manager,
};

/// A queued filesystem reload may lag behind a completed drag. Defer its save
/// until the controller applies the disk config instead of overwriting that edit.
fn persist_overlay_placement(
    path: &Path,
    current: &Config,
    placement: Placement,
) -> Result<Option<Config>> {
    let disk = Config::load(path)?;
    if toml::to_string(current)? != toml::to_string(&disk)? {
        return Ok(None);
    }
    let new = current.patched(json!({"overlay":{"position":"custom","placement":placement}}))?;
    new.save(path)?;
    Ok(Some(new))
}

#[derive(Clone, Serialize)]
pub struct Status {
    pub state: String,
    pub source: Option<String>,
    pub model: String,
    pub device: String,
    pub compute: String,
    pub sidecar_alive: bool,
    pub model_available: bool,
    pub fallback_reason: Option<String>,
    pub error: Option<String>,
}

fn apply_warm_status(
    current: &EngineConfig,
    expected: &EngineConfig,
    status: &mut Status,
    result: &Value,
) -> bool {
    if current.model != expected.model
        || current.device != expected.device
        || current.compute_type != expected.compute_type
        || result["loaded"].as_bool() != Some(true)
        || result["model"].as_str() != Some(expected.model.as_str())
    {
        return false;
    }
    let (Some(device), Some(compute), Some(available)) = (
        result["device"].as_str(),
        result["compute"].as_str(),
        result["model_available"].as_bool(),
    ) else {
        return false;
    };
    status.model = expected.model.clone();
    status.model_available = available;
    status.device = device.into();
    status.compute = compute.into();
    status.fallback_reason = result["fallback_reason"].as_str().map(str::to_owned);
    true
}
#[derive(Clone)]
pub struct TrayControls {
    pub label: MenuItem<tauri::Wry>,
    pub dictate: MenuItem<tauri::Wry>,
    pub mute: CheckMenuItem<tauri::Wry>,
    pub enabled: CheckMenuItem<tauri::Wry>,
}
pub struct AppState {
    pub app: AppHandle,
    pub paths: Paths,
    pub engine: Arc<Engine>,
    pub components: Arc<Components>,
    pub enhance: enhance::runtime::Runtime,
    pub companion: crate::companion::Companion,
    pub companion_network: crate::companion::network::Network,
    /// Transcript held back because focus changed during dictation.
    pub held: crate::held::HeldStore,
    config: Mutex<Config>,
    transaction: Mutex<()>,
    status: Mutex<Status>,
    sender: mpsc::Sender<Action>,
    receiver: Mutex<Option<mpsc::Receiver<Action>>>,
    controller: Mutex<Option<JoinHandle<()>>>,
    watcher: Mutex<Option<RecommendedWatcher>>,
    sound: Mutex<SoundPlayer>,
    history: Mutex<Option<History>>,
    tray: Mutex<Option<TrayControls>>,
    closing: AtomicBool,
    closed: AtomicBool,
    shutdown_job: Mutex<Option<JoinHandle<()>>>,
}
pub fn lock<T>(mutex: &Mutex<T>) -> Result<MutexGuard<'_, T>> {
    mutex
        .lock()
        .map_err(|_| anyhow::anyhow!("Internal state lock poisoned"))
}
impl AppState {
    pub fn new(app: AppHandle) -> Result<Self> {
        let paths = Paths::discover()?;
        platform::init_logging(&paths.data)?;
        let config = Config::load(&paths.config)?;
        if !paths.config.exists() {
            config.save(&paths.config)?;
        }
        let history = if config.privacy.history_enabled || paths.history.exists() {
            let h = History::open(&paths.history)?;
            h.prune(config.privacy.history_retention_days)?;
            Some(h)
        } else {
            None
        };
        tracing::info!(
            version = env!("CARGO_PKG_VERSION"),
            config = %paths.config.display(),
            models = %paths.models.display(),
            model = %config.engine.model,
            device = %config.engine.device,
            hotkey = %config.general.hotkey,
            mode = %config.general.hotkey_mode,
            "murmur starting"
        );
        let components = Components::new(
            components::Options::production(
                paths.data.clone(),
                paths.models.clone(),
                paths.enhance_models.clone(),
            ),
            component_host(&app),
        );
        let engine = Arc::new(Engine::new(
            paths.models.clone(),
            app.clone(),
            components.clone(),
        )?);
        let enhance = enhance::runtime::Runtime::new(
            &app,
            &config,
            Arc::new(components::adapters::EnhanceStore(components.clone())),
        )
        .context("Start Enhance worker")?;
        let (sender, receiver) = mpsc::channel();
        let status = Status {
            state: "idle".into(),
            source: None,
            model: config.engine.model.clone(),
            device: "unloaded".into(),
            compute: "unloaded".into(),
            sidecar_alive: false,
            model_available: false,
            fallback_reason: None,
            error: None,
        };
        let companion_network = crate::companion::network::Network::new(app.clone(), &paths.data);
        Ok(Self {
            app: app.clone(),
            paths,
            engine,
            components,
            enhance,
            companion: crate::companion::Companion::new(app),
            companion_network,
            held: crate::held::HeldStore::default(),
            config: Mutex::new(config),
            transaction: Mutex::new(()),
            status: Mutex::new(status),
            sender,
            receiver: Mutex::new(Some(receiver)),
            controller: Mutex::new(None),
            watcher: Mutex::new(None),
            sound: Mutex::new(SoundPlayer::new()?),
            history: Mutex::new(history),
            tray: Mutex::new(None),
            closing: AtomicBool::new(false),
            closed: AtomicBool::new(false),
            shutdown_job: Mutex::new(None),
        })
    }
    pub fn cancel_enhance_download(&self, operation_id: u64) -> Result<bool> {
        let _transaction = lock(&self.transaction)?;
        Ok(self.enhance.service.cancel_download(operation_id))
    }
    /// Setup is pending while no engine is installed and the user has not postponed it.
    pub fn postpone_setup_if_pending(&self) {
        let pending = self.components.engine_dir().is_none()
            && self.config().map(|c| !c.setup.postponed).unwrap_or(false);
        if pending {
            if let Err(error) = self.update(json!({"setup":{"postponed":true}})) {
                tracing::warn!(%error, "Postpone setup");
            }
        }
    }
    pub fn config(&self) -> Result<Config> {
        Ok(lock(&self.config)?.clone())
    }
    /// A pending or ready Enhance suggestion, or a held transcript, keeps the overlay visible
    /// even when the idle view is hidden.
    pub fn overlay_held(&self) -> bool {
        self.enhance.holds_overlay() || self.held.holds_overlay()
    }
    pub fn status(&self) -> Result<Status> {
        let mut status = lock(&self.status)?.clone();
        status.sidecar_alive = self.engine.alive();
        if let Some(error) = self.engine.failure() {
            status.error = Some(error);
        }
        Ok(status)
    }
    pub fn refresh_engine(&self) -> Result<Value> {
        let c = self.config()?;
        let result = self.engine.request(
            "status",
            json!({"model":c.engine.model,"device":c.engine.device,"compute_type":c.engine.compute_type}),
        )?;
        {
            let mut s = lock(&self.status)?;
            s.model = c.engine.model;
            s.model_available = result["model_available"].as_bool().unwrap_or(false);
            s.device = result["device"].as_str().unwrap_or("unloaded").into();
            s.compute = result["compute"].as_str().unwrap_or("unloaded").into();
            s.fallback_reason = result["fallback_reason"].as_str().map(str::to_owned);
        }
        self.refresh_tray()?;
        self.app
            .emit("state", json!({"state":self.status()?.state}))?;
        Ok(result)
    }
    pub fn cache_warm_status(&self, expected: &EngineConfig, result: &Value) -> Result<()> {
        let applied = {
            let config = lock(&self.config)?;
            let mut status = lock(&self.status)?;
            apply_warm_status(&config.engine, expected, &mut status, result)
        };
        if applied {
            self.refresh_tray()?;
            self.app
                .emit("state", json!({"state":self.status()?.state}))?;
        }
        Ok(())
    }
    pub fn send(&self, action: Action) -> Result<()> {
        if self.closing.load(Ordering::Acquire) {
            anyhow::bail!("Murmur is shutting down");
        }
        self.sender
            .send(action)
            .context("Dictation controller stopped")
    }
    pub fn sender(&self) -> mpsc::Sender<Action> {
        self.sender.clone()
    }
    pub fn set_tray(&self, controls: TrayControls) -> Result<()> {
        *lock(&self.tray)? = Some(controls);
        self.refresh_tray()
    }
    pub fn refresh_tray(&self) -> Result<()> {
        let c = self.config()?;
        let s = self.status()?;
        let label = if !s.model_available {
            "no model"
        } else {
            &s.state
        };
        if let Some(tray) = self.app.tray_by_id("murmur") {
            tray.set_tooltip(Some(format!("Murmur — {label}")))?;
        }
        let items = lock(&self.tray)?.clone();
        if let Some(items) = items {
            items.label.set_text(label)?;
            items
                .dictate
                .set_enabled(c.general.hotkey_mode == "toggle")?;
            items.mute.set_checked(c.sounds.muted)?;
            items.enabled.set_checked(c.overlay.enabled)?;
        }
        Ok(())
    }
    pub fn transition(&self, state: &str) -> Result<()> {
        {
            let mut s = lock(&self.status)?;
            s.state = state.into();
            if state == "listening" {
                s.error = None;
            }
        }
        overlay::state(&self.app, state, &self.config()?.overlay)?;
        self.enhance.publish(self);
        self.refresh_tray()
    }
    pub fn error(&self, message: &str) {
        if let Ok(mut s) = self.status.lock() {
            s.error = Some(message.into());
        }
        let _ = self.app.emit("error", json!({"message":message}));
    }
    pub fn audio_source(&self, source: Option<&str>) {
        if let Ok(mut status) = self.status.lock() {
            status.source = source.map(str::to_owned);
        }
        let _ = self.app.emit(
            "state",
            json!({"state":self.status().ok().map(|s| s.state),"source":source}),
        );
    }
    pub fn cue(&self, cue: Cue) {
        if let (Ok(sound), Ok(config)) = (self.sound.lock(), self.config()) {
            sound.play(cue, &config.sounds);
        }
    }
    fn apply_runtime(&self, old: &Config, new: &Config) -> Result<()> {
        hotkey::rebind(&self.app, old, new)?;
        if old.overlay.position != new.overlay.position
            || serde_json::to_value(&old.overlay.placement)?
                != serde_json::to_value(&new.overlay.placement)?
            || (old.overlay.enabled && !new.overlay.enabled)
        {
            overlay::reset_placement();
        }
        if let Err(error) = platform::set_autostart(&self.app, new.general.autostart)
            .and_then(|_| overlay::apply(&self.app, &new.overlay))
        {
            let _ = hotkey::rebind(&self.app, new, old);
            let _ = platform::set_autostart(&self.app, old.general.autostart);
            let _ = overlay::apply(&self.app, &old.overlay);
            return Err(error);
        }
        Ok(())
    }
    /// Persist observed geometry without reapplying native placement, rebinding
    /// hotkeys, or reconfiguring the engine. Settings transactions still serialize it.
    pub fn save_overlay_placement(&self) -> Result<()> {
        let _transaction = lock(&self.transaction)?;
        let Some((generation, placement)) = overlay::pending_placement() else {
            return Ok(());
        };
        let new = match persist_overlay_placement(&self.paths.config, &self.config()?, placement) {
            Ok(Some(new)) => new,
            Ok(None) => {
                // Keep placement pending. Reload preserves it for unrelated edits,
                // but explicit position/reset/disable edits invalidate it normally.
                self.send(Action::Reload)?;
                return Ok(());
            }
            Err(error) => {
                overlay::placement_saved(generation, false);
                return Err(error);
            }
        };
        *lock(&self.config)? = new.clone();
        overlay::placement_saved(generation, true);
        self.app
            .emit("config", &new)
            .context("Emit overlay placement")?;
        Ok(())
    }
    pub fn update(&self, patch: Value) -> Result<Config> {
        self.update_checked(patch, || true)?
            .context("Configuration update skipped")
    }
    pub fn update_checked(
        &self,
        patch: Value,
        authorized: impl FnOnce() -> bool,
    ) -> Result<Option<Config>> {
        let _transaction = lock(&self.transaction)?;
        if patch.pointer("/enhance/auto_rewrite") == Some(&Value::Bool(false)) {
            // Even an explicit false → false choice revokes pending download consent.
            self.enhance.service.clear_enable_intent();
        }
        if !authorized() {
            return Ok(None);
        }
        let old = self.config()?;
        let new = old.patched(patch)?;
        self.apply_runtime(&old, &new)?;
        if let Err(error) = new.save(&self.paths.config) {
            let _ = self.apply_runtime(&new, &old);
            return Err(error);
        }
        *lock(&self.config)? = new.clone();
        self.enhance.config_changed(self, &old, &new);
        self.companion.config_changed();
        self.companion_network.config_changed();
        self.app
            .emit("config", &new)
            .context("Emit config update")?;
        self.send(Action::Reconfigure)?;
        self.refresh_tray()?;
        Ok(Some(new))
    }
    pub fn reload(&self) -> Result<()> {
        let _transaction = lock(&self.transaction)?;
        let new = Config::load(&self.paths.config)?;
        let old = self.config()?;
        if toml::to_string(&old)? == toml::to_string(&new)? {
            return Ok(());
        }
        self.apply_runtime(&old, &new)?;
        *lock(&self.config)? = new.clone();
        self.enhance.config_changed(self, &old, &new);
        self.companion.config_changed();
        self.companion_network.config_changed();
        self.app.emit("config", &new)?;
        self.send(Action::Reconfigure)?;
        self.refresh_tray()
    }
    pub fn history<T>(&self, f: impl FnOnce(Option<&History>) -> Result<T>) -> Result<T> {
        let config = self.config()?;
        let mut history = lock(&self.history)?;
        if history.is_none() && config.privacy.history_enabled {
            *history = Some(History::open(&self.paths.history)?);
        }
        f(history.as_ref())
    }
    pub fn start(&self) -> Result<()> {
        let rx = lock(&self.receiver)?
            .take()
            .context("Controller already started")?;
        let app = self.app.clone();
        *lock(&self.controller)? = Some(
            thread::Builder::new()
                .name("murmur-controller".into())
                .spawn(move || controller::run(app, rx))
                .context("Start dictation controller")?,
        );
        let sender = self.sender.clone();
        let path = self.paths.config.clone();
        let mut watcher =
            notify::recommended_watcher(move |event: notify::Result<notify::Event>| match event {
                Ok(event)
                    if event.paths.contains(&path)
                        && !matches!(event.kind, notify::EventKind::Access(_)) =>
                {
                    let _ = sender.send(Action::Reload);
                }
                Err(error) => {
                    let _ = sender.send(Action::Fault(format!("Config watcher: {error}")));
                }
                _ => {}
            })
            .context("Create config watcher")?;
        watcher
            .watch(
                self.paths
                    .config
                    .parent()
                    .context("Config parent missing")?,
                RecursiveMode::NonRecursive,
            )
            .context("Watch configuration directory")?;
        *lock(&self.watcher)? = Some(watcher);
        Ok(())
    }
    pub fn closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }
    pub fn begin_shutdown(&self) -> Result<()> {
        if self.closing.swap(true, Ordering::AcqRel) {
            return Ok(());
        }
        let app = self.app.clone();
        let job = thread::Builder::new()
            .name("murmur-shutdown".into())
            .spawn(move || {
                let state = app.state::<AppState>();
                state.shutdown();
                state.closed.store(true, Ordering::Release);
                app.exit(0);
            })
            .context("Start shutdown worker")?;
        *lock(&self.shutdown_job)? = Some(job);
        Ok(())
    }
    pub fn shutdown(&self) {
        self.companion_network.shutdown();
        if let Ok(mut watcher) = self.watcher.lock() {
            watcher.take();
        }
        let _ = self.sender.send(Action::Quit);
        self.components.shutdown();
        self.engine.interrupt();
        if let Ok(mut guard) = self.controller.lock() {
            if let Some(job) = guard.take() {
                let _ = job.join();
            }
        }
        self.engine.shutdown();
        self.enhance.shutdown();
        if let Ok(mut sound) = self.sound.lock() {
            sound.shutdown();
        }
    }
    pub fn join_shutdown(&self) {
        if let Ok(mut guard) = self.shutdown_job.lock() {
            if let Some(job) = guard.take() {
                let _ = job.join();
            }
        }
    }
}

/// The component manager's callbacks. `AppState` is looked up lazily: jobs and migration
/// run on their own threads and may finish before or after Settings opens.
fn component_host(app: &AppHandle) -> components::Host {
    let emitter = app.clone();
    let stopper = app.clone();
    let notifier = app.clone();
    let reader = app.clone();
    components::Host {
        emit: Arc::new(move |name, value| {
            let _ = emitter.emit(name, value);
        }),
        stop_engine: Box::new(move || {
            stopper
                .try_state::<AppState>()
                .is_none_or(|state| state.engine.stop_and_wait())
        }),
        changed: Box::new(move |kind| {
            if let Some(state) = notifier.try_state::<AppState>() {
                let _ = state.send(Action::ComponentsChanged(kind));
            }
        }),
        usage: Box::new(move || {
            reader
                .try_state::<AppState>()
                .and_then(|state| state.config().ok())
                .map(|config| components::Usage {
                    speech_model: config.engine.model.clone(),
                    enhance_model: config
                        .enhance
                        .auto_rewrite
                        .then(|| config.enhance.model.clone()),
                })
                .unwrap_or_default()
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::{apply_warm_status, persist_overlay_placement, Status};
    use crate::config::{Config, Engine, Placement};
    use serde_json::json;

    #[test]
    fn placement_save_preserves_external_edit_until_reload() -> anyhow::Result<()> {
        let dir =
            std::env::temp_dir().join(format!("murmur-placement-reload-{}", std::process::id()));
        let path = dir.join("config.toml");
        let current = Config::default();
        current.save(&path)?;
        let edited = current.patched(json!({"general":{"theme":"dark"}}))?;
        edited.save(&path)?; // external edit while the controller still holds `current`
        let placement = Placement {
            x_fraction: 0.25,
            ..Placement::default()
        };
        assert!(persist_overlay_placement(&path, &current, placement.clone())?.is_none());
        let reloaded = Config::load(&path)?;
        assert_eq!(reloaded.general.theme, "dark");
        assert_eq!(reloaded.overlay.position, current.overlay.position);
        assert!(persist_overlay_placement(&path, &reloaded, placement)?.is_some());
        let saved = Config::load(&path)?;
        assert_eq!(saved.general.theme, "dark");
        assert_eq!(saved.overlay.position, "custom");
        assert_eq!(saved.overlay.placement.x_fraction, 0.25);
        // An editor's incomplete TOML must also survive an automatic save attempt.
        std::fs::write(&path, "[general")?;
        assert!(persist_overlay_placement(&path, &saved, Placement::default()).is_err());
        assert_eq!(std::fs::read_to_string(&path)?, "[general");
        std::fs::remove_dir_all(dir)?;
        Ok(())
    }

    #[test]
    fn warm_status_updates_cache_only_for_current_configuration() {
        let expected = Engine::default();
        let mut status = Status {
            state: "idle".into(),
            source: None,
            model: expected.model.clone(),
            device: "unloaded".into(),
            compute: "unloaded".into(),
            sidecar_alive: true,
            model_available: true,
            fallback_reason: None,
            error: None,
        };
        let result = json!({"model":expected.model,"loaded":true,"device":"cpu",
            "compute":"int8","model_available":true,"fallback_reason":"CUDA library missing"});
        let mut changed = expected.clone();
        changed.device = "cuda".into();
        assert!(!apply_warm_status(
            &changed,
            &expected,
            &mut status,
            &result
        ));
        assert_eq!(status.device, "unloaded");
        assert!(apply_warm_status(
            &expected,
            &expected,
            &mut status,
            &result
        ));
        assert_eq!(status.device, "cpu");
        assert_eq!(status.compute, "int8");
        assert_eq!(
            status.fallback_reason.as_deref(),
            Some("CUDA library missing")
        );
    }
}
