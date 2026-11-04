//! Opt-in TLS-only LAN control link for the Android companion.
mod listener;
pub use murmur::{audio, auth, lan, tls, wire};

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
    time::{Duration, Instant},
};
use tauri::{AppHandle, Manager};
use tokio::sync::{mpsc, watch};

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
    pub dropped_audio_frames: u64,
    pub devices: Vec<auth::PublicDevice>,
}
struct Shared {
    app: AppHandle,
    auth: Option<Mutex<auth::Auth>>,
    sessions: Mutex<HashMap<String, Session>>,
    fingerprint: Mutex<Option<String>>,
    error: Mutex<Option<String>>,
    rejected_peers: AtomicU64,
    congested_frames: AtomicU64,
    remote: Mutex<RemoteTurn>,
}
struct Session {
    stop: watch::Sender<bool>,
    mic: mpsc::Sender<MicCommand>,
}
#[derive(Clone, Copy)]
enum MicCommand {
    Start(u32),
    Stop(u32),
}
#[derive(Default)]
struct RemoteTurn {
    selected: Option<String>,
    stream: Option<u32>,
    ring: audio::Ring,
    received: u64,
    started_at: Option<Instant>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    Local,
    Phone,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Policy {
    Local,
    Phone,
    Auto,
}
impl Policy {
    pub fn select(self, connected: bool) -> Result<Source> {
        match self {
            Self::Local => Ok(Source::Local),
            Self::Phone if connected => Ok(Source::Phone),
            Self::Phone => anyhow::bail!("Phone microphone unavailable: no paired phone connected"),
            Self::Auto if connected => Ok(Source::Phone),
            Self::Auto => Ok(Source::Local),
        }
    }
    pub fn silence_after(self, elapsed: Duration, received: u64) -> Result<bool> {
        if elapsed < Duration::from_millis(500) || received > 0 {
            return Ok(false);
        }
        match self {
            Self::Phone => {
                anyhow::bail!("Phone microphone sent no audio within 500 ms of MIC START")
            }
            Self::Auto => Ok(true),
            Self::Local => Ok(false),
        }
    }
}
impl Shared {
    fn close_all(&self) {
        if let Ok(mut sessions) = self.sessions.lock() {
            for (_, session) in sessions.drain() {
                let _ = session.stop.send(true);
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
                congested_frames: AtomicU64::new(0),
                remote: Mutex::new(RemoteTurn::default()),
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
            dropped_audio_frames: self.dropped(),
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
            if let Some(session) = self.shared.sessions.lock().unwrap().remove(id) {
                let _ = session.stop.send(true);
            }
        }
        Ok(removed)
    }
    pub fn begin_turn(&self, policy: Policy, stream: u32) -> Result<Source> {
        let sessions = self.shared.sessions.lock().unwrap();
        let selected = sessions.keys().next().cloned();
        let source = policy.select(selected.is_some())?;
        let mut remote = self.shared.remote.lock().unwrap();
        remote.ring.clear();
        remote.received = 0;
        remote.started_at = None;
        remote.selected = if source == Source::Phone {
            selected
        } else {
            None
        };
        remote.stream = remote.selected.as_ref().map(|_| stream);
        if let Some(id) = &remote.selected {
            if sessions
                .get(id)
                .is_none_or(|session| session.mic.try_send(MicCommand::Start(stream)).is_err())
            {
                remote.stream = None;
                remote.selected = None;
                anyhow::bail!("Phone microphone command queue unavailable");
            }
        }
        Ok(source)
    }
    pub fn end_turn(&self) {
        let ending = {
            let mut remote = self.shared.remote.lock().unwrap();
            let ending = remote.selected.take().zip(remote.stream.take());
            remote.started_at = None;
            remote.ring.clear();
            ending
        };
        if let Some((id, stream)) = ending {
            if let Some(session) = self.shared.sessions.lock().unwrap().get(&id) {
                if session.mic.try_send(MicCommand::Stop(stream)).is_err() {
                    let _ = session.stop.send(true);
                }
            }
        }
    }
    pub fn received(&self) -> u64 {
        self.shared.remote.lock().unwrap().received
    }
    pub fn silence_elapsed(&self) -> Option<Duration> {
        self.shared
            .remote
            .lock()
            .unwrap()
            .started_at
            .map(|at| at.elapsed())
    }
    pub fn phone_connected(&self) -> bool {
        let sessions = self.shared.sessions.lock().unwrap();
        self.shared
            .remote
            .lock()
            .unwrap()
            .selected
            .as_ref()
            .is_some_and(|id| sessions.contains_key(id))
    }
    pub fn drain(&self, mut feed: impl FnMut(&audio::Frame)) {
        for _ in 0..audio::QUEUE_FRAMES {
            let frame = self.shared.remote.lock().unwrap().ring.pop();
            match frame {
                Some(frame) => feed(&frame),
                None => break,
            }
        }
    }
    pub fn dropped(&self) -> u64 {
        self.shared.remote.lock().unwrap().ring.dropped()
            + self.shared.congested_frames.load(Ordering::Relaxed)
    }
}

#[cfg(test)]
mod audio_policy_tests {
    use super::*;
    #[test]
    fn selection() {
        assert_eq!(Policy::Local.select(true).unwrap(), Source::Local);
        assert_eq!(Policy::Local.select(false).unwrap(), Source::Local);
        assert_eq!(Policy::Phone.select(true).unwrap(), Source::Phone);
        assert!(Policy::Phone.select(false).is_err());
        assert_eq!(Policy::Auto.select(true).unwrap(), Source::Phone);
        assert_eq!(Policy::Auto.select(false).unwrap(), Source::Local);
        assert!(!Policy::Auto
            .silence_after(Duration::from_millis(499), 0)
            .unwrap());
        assert!(Policy::Auto
            .silence_after(Duration::from_millis(500), 0)
            .unwrap());
        assert!(!Policy::Auto
            .silence_after(Duration::from_secs(1), 1)
            .unwrap());
        assert!(Policy::Phone
            .silence_after(Duration::from_millis(500), 0)
            .is_err());
        assert!(!Policy::Local
            .silence_after(Duration::from_secs(1), 0)
            .unwrap());
    }
}
