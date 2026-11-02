use crate::{
    config::Config,
    controller::{self, Action},
    engine::Engine,
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

#[derive(Clone, Serialize)]
pub struct Status {
    pub state: String,
    pub model: String,
    pub device: String,
    pub compute: String,
    pub sidecar_alive: bool,
    pub model_available: bool,
    pub error: Option<String>,
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
        let engine = Arc::new(Engine::new(paths.models.clone(), app.clone())?);
        let (sender, receiver) = mpsc::channel();
        let status = Status {
            state: "idle".into(),
            model: config.engine.model.clone(),
            device: "unloaded".into(),
            compute: "unloaded".into(),
            sidecar_alive: false,
            model_available: false,
            error: None,
        };
        Ok(Self {
            app,
            paths,
            engine,
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
    pub fn config(&self) -> Result<Config> {
        Ok(lock(&self.config)?.clone())
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
        }
        self.refresh_tray()?;
        self.app
            .emit("state", json!({"state":self.status()?.state}))?;
        Ok(result)
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
        self.refresh_tray()
    }
    pub fn error(&self, message: &str) {
        if let Ok(mut s) = self.status.lock() {
            s.error = Some(message.into());
        }
        let _ = self.app.emit("error", json!({"message":message}));
    }
    pub fn cue(&self, cue: Cue) {
        if let (Ok(sound), Ok(config)) = (self.sound.lock(), self.config()) {
            sound.play(cue, &config.sounds);
        }
    }
    fn apply_runtime(&self, old: &Config, new: &Config) -> Result<()> {
        hotkey::rebind(&self.app, &old.general, &new.general)?;
        if let Err(error) = platform::set_autostart(&self.app, new.general.autostart)
            .and_then(|_| overlay::apply(&self.app, &new.overlay))
        {
            let _ = hotkey::rebind(&self.app, &new.general, &old.general);
            let _ = platform::set_autostart(&self.app, old.general.autostart);
            let _ = overlay::apply(&self.app, &old.overlay);
            return Err(error);
        }
        Ok(())
    }
    pub fn update(&self, patch: Value) -> Result<Config> {
        let _transaction = lock(&self.transaction)?;
        let old = self.config()?;
        let new = old.patched(patch)?;
        self.apply_runtime(&old, &new)?;
        if let Err(error) = new.save(&self.paths.config) {
            let _ = self.apply_runtime(&new, &old);
            return Err(error);
        }
        *lock(&self.config)? = new.clone();
        self.app
            .emit("config", &new)
            .context("Emit config update")?;
        self.send(Action::Reconfigure)?;
        self.refresh_tray()?;
        Ok(new)
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
        if let Ok(mut watcher) = self.watcher.lock() {
            watcher.take();
        }
        let _ = self.sender.send(Action::Quit);
        self.engine.interrupt();
        if let Ok(mut guard) = self.controller.lock() {
            if let Some(job) = guard.take() {
                let _ = job.join();
            }
        }
        self.engine.shutdown();
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
