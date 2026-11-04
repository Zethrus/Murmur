//! Opt-in TLS-only LAN control link for the Android companion.
mod listener;
pub use murmur::{auth, lan, tls, wire};

use crate::{app::AppState, config::CompanionNetwork};
use anyhow::{Context, Result};
use serde::Serialize;
use std::{
    collections::HashMap,
    path::Path,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    thread,
};
use tauri::{AppHandle, Manager};
use tokio::sync::watch;

#[derive(Clone, PartialEq)]
struct Settings {
    enabled: bool,
    bind: String,
    port: u16,
}
impl From<&CompanionNetwork> for Settings {
    fn from(c: &CompanionNetwork) -> Self {
        Self {
            enabled: c.enabled,
            bind: c.bind.clone(),
            port: c.port,
        }
    }
}
#[derive(Serialize)]
pub struct Status {
    pub enabled: bool,
    pub fingerprint: Option<String>,
    pub error: Option<String>,
    pub rejected_peers: u64,
    pub devices: Vec<auth::PublicDevice>,
}
struct Shared {
    app: AppHandle,
    auth: Option<Mutex<auth::Auth>>,
    sessions: Mutex<HashMap<String, watch::Sender<bool>>>,
    fingerprint: Mutex<Option<String>>,
    error: Mutex<Option<String>>,
    rejected_peers: AtomicU64,
}
impl Shared {
    fn close_all(&self) {
        if let Ok(mut sessions) = self.sessions.lock() {
            for (_, stop) in sessions.drain() {
                let _ = stop.send(true);
            }
        }
    }
}
pub struct Network {
    shared: Arc<Shared>,
    changes: watch::Sender<Option<Settings>>,
    thread: Mutex<Option<thread::JoinHandle<()>>>,
}
impl Network {
    pub fn new(app: AppHandle, data: &Path) -> Self {
        let (changes, _) = watch::channel(Some(Settings {
            enabled: false,
            bind: "127.0.0.1".into(),
            port: 8787,
        }));
        let (auth, error) = match auth::Auth::load(data) {
            Ok(auth) => (Some(Mutex::new(auth)), None),
            Err(error) => {
                tracing::warn!(%error, "Companion token store unavailable");
                (
                    None,
                    Some(format!("Companion token store unavailable: {error:#}")),
                )
            }
        };
        Self {
            shared: Arc::new(Shared {
                app,
                auth,
                sessions: Mutex::new(HashMap::new()),
                fingerprint: Mutex::new(None),
                error: Mutex::new(error),
                rejected_peers: AtomicU64::new(0),
            }),
            changes,
            thread: Mutex::new(None),
        }
    }
    pub fn start(&self) {
        self.config_changed();
        let shared = self.shared.clone();
        let mut changes = self.changes.subscribe();
        let job = thread::Builder::new()
            .name("companion-network".into())
            .spawn(move || {
                let runtime = match tokio::runtime::Builder::new_multi_thread()
                    .worker_threads(2)
                    .enable_all()
                    .build()
                {
                    Ok(runtime) => runtime,
                    Err(error) => {
                        tracing::error!(%error, "Companion network runtime failed");
                        return;
                    }
                };
                runtime.block_on(async move {
                    loop {
                        let setting = changes.borrow_and_update().clone();
                        let Some(setting) = setting else { break };
                        if shared.auth.is_none() {
                            shared.close_all();
                            if changes.changed().await.is_err() {
                                break;
                            }
                            continue;
                        }
                        if setting.enabled {
                            if let Err(error) =
                                listener::run(shared.clone(), &setting, &mut changes).await
                            {
                                tracing::warn!(%error, "Companion network listener failed");
                                *shared.error.lock().unwrap() = Some(error.to_string());
                                if changes.changed().await.is_err() {
                                    break;
                                }
                            }
                        } else {
                            shared.close_all();
                            *shared.fingerprint.lock().unwrap() = None;
                            *shared.error.lock().unwrap() = None;
                            if changes.changed().await.is_err() {
                                break;
                            }
                        }
                    }
                    shared.close_all();
                });
            });
        match job {
            Ok(job) => *self.thread.lock().unwrap() = Some(job),
            Err(error) => tracing::error!(%error, "Start companion network"),
        }
    }
    pub fn config_changed(&self) {
        if let Some(state) = self.shared.app.try_state::<AppState>() {
            if let Ok(config) = state.config() {
                self.changes
                    .send_replace(Some(Settings::from(&config.companion.network)));
            }
        }
    }
    pub fn shutdown(&self) {
        self.changes.send_replace(None);
        if let Some(job) = self.thread.lock().unwrap().take() {
            let _ = job.join();
        }
    }
    pub fn status(&self) -> Status {
        let enabled = self
            .shared
            .app
            .try_state::<AppState>()
            .and_then(|s| s.config().ok())
            .is_some_and(|c| c.companion.network.enabled);
        Status {
            enabled,
            fingerprint: self.shared.fingerprint.lock().unwrap().clone(),
            error: self.shared.error.lock().unwrap().clone(),
            rejected_peers: self.shared.rejected_peers.load(Ordering::Relaxed),
            devices: self
                .shared
                .auth
                .as_ref()
                .map_or_else(Vec::new, |auth| auth.lock().unwrap().list()),
        }
    }
    pub fn pairing(&self) -> Result<auth::Pairing> {
        self.shared
            .auth
            .as_ref()
            .context("Companion token store unavailable")?
            .lock()
            .unwrap()
            .show_code()
    }
    pub fn revoke(&self, id: &str) -> Result<bool> {
        let removed = self
            .shared
            .auth
            .as_ref()
            .context("Companion token store unavailable")?
            .lock()
            .unwrap()
            .revoke(id)?;
        if removed {
            if let Some(stop) = self.shared.sessions.lock().unwrap().remove(id) {
                let _ = stop.send(true);
            }
        }
        Ok(removed)
    }
}
