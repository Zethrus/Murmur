//! Enhance: optional local CPU rewriting of the last dictation. The original transcript is
//! always delivered first; nothing here can block or fail dictation.
pub mod backend;
pub mod catalog;
// The self-contained downloader predates the component manager; the app installs through
// `components::adapters::EnhanceStore`. It stays as the `LegacyStore` used by tests.
#[cfg_attr(not(test), allow(dead_code))]
pub mod download;
#[cfg(feature = "enhance-llama")]
pub mod llama;
pub mod output;
pub mod prompt;
pub mod runtime;
pub mod session;
#[cfg(test)]
mod tests;
pub mod worker;

use catalog::{Entry, InstallState};
use serde::Serialize;
use serde_json::{json, Value};
#[cfg(test)]
use std::path::Path;
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex, MutexGuard,
    },
    thread::{self, JoinHandle},
    time::Duration,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    ModelMissing,
    UnavailableModel,
    BackendUnavailable,
    LoadFailed,
    LoadTimeout,
    Timeout,
    Cancelled,
    InvalidOutput,
    InputTooLong,
    InferenceFailed,
    DownloadFailed,
    ChecksumMismatch,
    SizeMismatch,
    InsufficientSpace,
    Network,
    Busy,
    RemoveFailed,
    Corrupt,
}
impl ErrorCode {
    pub fn as_str(self) -> String {
        serde_json::to_value(self)
            .ok()
            .and_then(|v| v.as_str().map(str::to_owned))
            .unwrap_or_default()
    }
}

/// Sanitized, transcript-free error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnhanceError {
    pub code: ErrorCode,
    pub message: String,
}
impl EnhanceError {
    pub fn new(code: ErrorCode, message: &str) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}
impl std::fmt::Display for EnhanceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for EnhanceError {}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LoadState {
    Unloaded,
    Loading,
    Loaded,
    Unavailable,
}

#[derive(Debug, Clone, Serialize)]
pub struct DownloadInfo {
    pub operation_id: String,
    pub model_id: String,
    pub phase: &'static str,
    pub received_bytes: u64,
    pub total_bytes: u64,
    pub error_code: Option<String>,
    pub message: Option<String>,
}

#[derive(Debug, Clone)]
pub struct Status {
    pub revision: u64,
    pub model_id: String,
    pub install: InstallState,
    pub load: LoadState,
    pub request_id: Option<u64>,
    pub error: Option<EnhanceError>,
    pub download: Option<DownloadInfo>,
    pub threads: u32,
}

pub type Emit = Arc<dyn Fn(&str, Value) + Send + Sync>;

/// Where Enhance models live. The app uses the component manager (signed manifest, pinned
/// hashes, `<data>/components`); `LegacyStore` is the self-contained downloader kept for tests.
pub trait ModelStore: Send + Sync {
    fn state(&self, entry: &Entry) -> InstallState;
    fn model_path(&self, entry: &Entry) -> Option<PathBuf>;
    /// Blocking install; `progress(phase, received, total)`.
    fn install(&self, entry: &'static Entry, cancel: &Arc<AtomicBool>, progress: &mut dyn FnMut(download::Phase, u64, u64)) -> Result<(), EnhanceError>;
    fn remove(&self, entry: &Entry) -> Result<(), EnhanceError>;
}

#[cfg(test)]
pub struct LegacyStore {
    pub root: PathBuf,
    space: fn(&Path) -> Option<u64>,
    transport: Arc<dyn Fn() -> Result<Box<dyn download::Transport>, EnhanceError> + Send + Sync>,
}
#[cfg(test)]
impl LegacyStore {
    pub fn new(root: PathBuf) -> Self {
        Self {
            root,
            space: crate::platform::available_space,
            transport: Arc::new(|| download::Https::new().map(|t| Box::new(t) as Box<dyn download::Transport>)),
        }
    }
}
#[cfg(test)]
impl ModelStore for LegacyStore {
    fn state(&self, entry: &Entry) -> InstallState {
        catalog::local_state(entry, &self.root)
    }
    fn model_path(&self, entry: &Entry) -> Option<PathBuf> {
        Some(entry.model_path(&self.root))
    }
    fn install(&self, entry: &'static Entry, cancel: &Arc<AtomicBool>, progress: &mut dyn FnMut(download::Phase, u64, u64)) -> Result<(), EnhanceError> {
        let space = (self.space)(&self.root);
        (self.transport)().and_then(|transport| download::install(entry, &self.root, transport.as_ref(), cancel, space, progress))
    }
    fn remove(&self, entry: &Entry) -> Result<(), EnhanceError> {
        download::remove(entry, &self.root)
    }
}

struct Operation {
    id: u64,
    cancel: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

pub struct Shared {
    status: Mutex<Status>,
    emit: Emit,
}

pub fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|poison| poison.into_inner())
}

impl Shared {
    pub fn update(&self, f: impl FnOnce(&mut Status)) {
        let snapshot = {
            let mut status = lock(&self.status);
            f(&mut status);
            status.revision += 1;
            status_json(&status)
        };
        (self.emit)("enhance-status", snapshot);
    }
    pub fn status(&self) -> Status {
        lock(&self.status).clone()
    }
}

pub fn status_json(s: &Status) -> Value {
    json!({
        "revision": s.revision,
        "model_id": s.model_id,
        "installation_state": s.install,
        "load_state": if backend::compiled() { s.load } else { LoadState::Unavailable },
        "backend_available": backend::compiled(),
        "effective_threads": s.threads,
        "request_id": s.request_id.map(|id| id.to_string()),
        "error_code": s.error.as_ref().map(|e| e.code.as_str()),
        "message": s.error.as_ref().map(|e| e.message.clone()),
        "download": s.download,
    })
}

/// Process-wide service: catalog/install state, the download operation, and the worker.
pub struct Enhance {
    pub shared: Arc<Shared>,
    pub worker: worker::Worker,
    next: AtomicU64,
    operation: Mutex<Option<Operation>>,
    removing: AtomicBool,
    /// Operation ID authorised to enable auto-rewrite on success (0 = none).
    enable_intent: AtomicU64,
    store: Arc<dyn ModelStore>,
}

pub struct InstallHooks {
    /// Called on success when enabling was requested and still authorised.
    pub enable: Box<dyn FnOnce(u64, &AtomicU64) + Send>,
}

impl Enhance {
    #[cfg(test)]
    pub fn new(
        root: PathBuf,
        model_id: &str,
        threads: u32,
        backend: Box<dyn backend::Backend + Send>,
        emit: Emit,
        on_worker: Arc<dyn Fn(worker::WorkerEvent) + Send + Sync>,
    ) -> std::io::Result<Self> {
        let store = Arc::new(LegacyStore::new(root));
        Self::with_store(model_id, threads, backend, emit, on_worker, store)
    }
    pub fn with_store(
        model_id: &str,
        threads: u32,
        backend: Box<dyn backend::Backend + Send>,
        emit: Emit,
        on_worker: Arc<dyn Fn(worker::WorkerEvent) + Send + Sync>,
        store: Arc<dyn ModelStore>,
    ) -> std::io::Result<Self> {
        let shared = Arc::new(Shared {
            status: Mutex::new(Status {
                revision: 0,
                model_id: model_id.into(),
                install: InstallState::NotInstalled,
                load: LoadState::Unloaded,
                request_id: None,
                error: None,
                download: None,
                threads: worker::effective_threads(threads),
            }),
            emit,
        });
        let sink_shared = shared.clone();
        let sink: Arc<dyn Fn(worker::WorkerEvent) + Send + Sync> = Arc::new(move |event| {
            use worker::WorkerEvent::*;
            match &event {
                Loading { request_id } => sink_shared.update(|s| {
                    s.load = LoadState::Loading;
                    s.request_id = Some(*request_id);
                }),
                Rewriting { request_id } => sink_shared.update(|s| {
                    s.load = LoadState::Loaded;
                    s.request_id = Some(*request_id);
                }),
                Finished { result, .. } => sink_shared.update(|s| {
                    s.request_id = None;
                    match result {
                        Err(e)
                            if matches!(
                                e.code,
                                ErrorCode::LoadFailed
                                    | ErrorCode::LoadTimeout
                                    | ErrorCode::Corrupt
                                    | ErrorCode::InferenceFailed
                                    | ErrorCode::ModelMissing
                            ) =>
                        {
                            s.load = LoadState::Unloaded;
                            if e.code == ErrorCode::Corrupt {
                                s.install = InstallState::Corrupt;
                            }
                            s.error = Some(e.clone());
                        }
                        Err(e) if e.code == ErrorCode::Cancelled => {
                            if s.load == LoadState::Loading {
                                s.load = LoadState::Unloaded;
                            }
                        }
                        _ => {
                            s.error = None;
                        }
                    }
                }),
                Unloaded => sink_shared.update(|s| s.load = LoadState::Unloaded),
            }
            on_worker(event);
        });
        let worker = worker::Worker::spawn(backend, sink)?;
        let service = Self {
            shared,
            worker,
            next: AtomicU64::new(1),
            operation: Mutex::new(None),
            removing: AtomicBool::new(false),
            enable_intent: AtomicU64::new(0),
            store,
        };
        service.refresh(model_id);
        Ok(service)
    }
    pub fn next_id(&self) -> u64 {
        self.next.fetch_add(1, Ordering::Relaxed)
    }
    /// Cheap, local-only installation check for the configured model.
    pub fn refresh(&self, model_id: &str) {
        let state = match catalog::find(model_id) {
            None => InstallState::UnavailableModel,
            Some(entry) => self.store.state(entry),
        };
        self.shared.update(|s| {
            s.model_id = model_id.into();
            if !matches!(
                s.install,
                InstallState::Downloading | InstallState::Verifying | InstallState::Removing
            ) {
                s.install = state;
            }
        });
    }
    pub fn set_threads(&self, threads: u32) {
        let effective = worker::effective_threads(threads);
        if self.shared.status().threads != effective {
            self.shared.update(|s| s.threads = effective);
        }
    }
    pub fn installed(&self) -> bool {
        self.shared.status().install == InstallState::Installed
    }
    pub fn loaded(&self) -> bool {
        self.shared.status().load == LoadState::Loaded
    }
    pub fn status_json(&self) -> Value {
        status_json(&self.shared.status())
    }
    pub fn model_json(&self, model_id: &str) -> Value {
        let entry = catalog::find(model_id).or_else(|| catalog::find(catalog::DEFAULT_MODEL_ID));
        match entry {
            Some(e) => json!({
                "id": e.id,
                "display_name": e.display_name,
                "repo": e.repo,
                "revision": e.revision,
                "filename": e.filename,
                "size_bytes": e.size_bytes,
                "sha256": e.sha256,
                "installed": self.store.state(e) == InstallState::Installed,
                "configured": e.id == model_id,
            }),
            None => Value::Null,
        }
    }
    pub fn job_entry(&self, model_id: &str) -> Option<(&'static Entry, PathBuf)> {
        catalog::find(model_id).and_then(|e| self.store.model_path(e).map(|p| (e, p)))
    }
    /// A user choice about auto-rewrite supersedes a pending "enable after install".
    pub fn clear_enable_intent(&self) {
        self.enable_intent.store(0, Ordering::Release);
    }
    fn emit_download(&self, info: &DownloadInfo) {
        (self.shared.emit)(
            "enhance-download",
            serde_json::to_value(info).unwrap_or(Value::Null),
        );
    }
    pub fn install(
        self: &Arc<Self>,
        model_id: &str,
        enable_after: bool,
        hooks: InstallHooks,
    ) -> Result<u64, EnhanceError> {
        let entry = catalog::find(model_id).ok_or_else(|| {
            EnhanceError::new(ErrorCode::UnavailableModel, "Unknown Enhance model")
        })?;
        let mut operation = lock(&self.operation);
        if self.removing.load(Ordering::Acquire) {
            return Err(EnhanceError::new(
                ErrorCode::Busy,
                "Model removal is running",
            ));
        }
        if let Some(op) = operation.as_ref() {
            if op.thread.as_ref().is_some_and(|t| !t.is_finished()) {
                return Err(EnhanceError::new(
                    ErrorCode::Busy,
                    "Another model operation is running",
                ));
            }
        }
        if let Some(mut old) = operation.take() {
            if let Some(t) = old.thread.take() {
                let _ = t.join();
            }
        }
        let id = self.next_id();
        let cancel = Arc::new(AtomicBool::new(false));
        self.enable_intent
            .store(if enable_after { id } else { 0 }, Ordering::Release);
        let info = DownloadInfo {
            operation_id: id.to_string(),
            model_id: entry.id.into(),
            phase: "starting",
            received_bytes: 0,
            total_bytes: entry.size_bytes,
            error_code: None,
            message: None,
        };
        self.shared.update(|s| {
            s.install = InstallState::Downloading;
            s.error = None;
            s.download = Some(info.clone());
        });
        self.emit_download(&info);
        let this = self.clone();
        let flag = cancel.clone();
        let thread = thread::Builder::new()
            .name("murmur-enhance-download".into())
            .spawn(move || this.run_install(entry, id, flag, hooks))
            .map_err(|_| {
                EnhanceError::new(ErrorCode::DownloadFailed, "Could not start the download")
            })?;
        *operation = Some(Operation {
            id,
            cancel,
            thread: Some(thread),
        });
        Ok(id)
    }
    fn run_install(
        &self,
        entry: &'static Entry,
        id: u64,
        cancel: Arc<AtomicBool>,
        hooks: InstallHooks,
    ) {
        let mut info = DownloadInfo {
            operation_id: id.to_string(),
            model_id: entry.id.into(),
            phase: "starting",
            received_bytes: 0,
            total_bytes: entry.size_bytes,
            error_code: None,
            message: None,
        };
        let result = {
            self.store.install(
                entry,
                &cancel,
                &mut |phase, received, total| {
                    info.phase = match phase {
                        download::Phase::Starting => "starting",
                        download::Phase::Downloading => "downloading",
                        download::Phase::Verifying => "verifying",
                    };
                    info.received_bytes = received;
                    info.total_bytes = total;
                    self.emit_download(&info);
                    self.shared.update(|s| {
                        if !self.removing() && s.install != InstallState::Removing {
                            if phase == download::Phase::Verifying {
                                s.install = InstallState::Verifying;
                            }
                            s.download = Some(info.clone());
                        }
                    });
                },
            )
        };
        let local = self.store.state(entry);
        match &result {
            Ok(()) => info.phase = "complete",
            Err(e) if e.code == ErrorCode::Cancelled => info.phase = "cancelled",
            Err(e) => {
                info.phase = "error";
                info.error_code = Some(e.code.as_str());
                info.message = Some(e.message.clone());
            }
        }
        self.emit_download(&info);
        self.shared.update(|s| {
            if self.removing() || s.install == InstallState::Removing {
                return;
            }
            let failed = matches!(&result, Err(e) if e.code != ErrorCode::Cancelled);
            s.install = if s.model_id != entry.id {
                s.install
            } else if failed && local == InstallState::NotInstalled {
                InstallState::Error
            } else {
                local
            };
            s.download = None;
            s.error = match &result {
                Err(e) if e.code != ErrorCode::Cancelled => Some(e.clone()),
                _ => None,
            };
        });
        if result.is_ok()
            && !cancel.load(Ordering::Acquire)
            && self.enable_intent.load(Ordering::Acquire) == id
        {
            (hooks.enable)(id, &self.enable_intent);
        }
        let _ = self
            .enable_intent
            .compare_exchange(id, 0, Ordering::AcqRel, Ordering::Acquire);
    }
    pub fn removing(&self) -> bool {
        self.removing.load(Ordering::Acquire)
    }
    pub fn cancel_download(&self, operation_id: u64) -> bool {
        let operation = lock(&self.operation);
        if self.removing() {
            return false;
        }
        match operation.as_ref() {
            Some(op) if op.id == operation_id => {
                op.cancel.store(true, Ordering::Release);
                let _ = self.enable_intent.compare_exchange(
                    operation_id,
                    0,
                    Ordering::AcqRel,
                    Ordering::Acquire,
                );
                true
            }
            _ => false,
        }
    }
    fn stop_operation(&self) {
        self.clear_enable_intent();
        let mut operation = lock(&self.operation);
        if let Some(op) = operation.as_mut() {
            op.cancel.store(true, Ordering::Release);
            if let Some(thread) = op.thread.as_ref() {
                let deadline = std::time::Instant::now() + Duration::from_secs(5);
                while !thread.is_finished() && std::time::Instant::now() < deadline {
                    thread::sleep(Duration::from_millis(10));
                }
                if !thread.is_finished() {
                    return;
                }
            }
        }
        if let Some(mut op) = operation.take() {
            if let Some(thread) = op.thread.take() {
                let _ = thread.join();
            }
        }
    }
    /// Cancel any download and inference, wait for the model to be dropped, then delete only
    /// the catalog-owned directory. Blocking; run off the UI thread.
    pub fn remove(&self, model_id: &str) -> Result<(), EnhanceError> {
        let entry = catalog::find(model_id).ok_or_else(|| {
            EnhanceError::new(ErrorCode::UnavailableModel, "Unknown Enhance model")
        })?;
        let mut previous = {
            let mut operation = lock(&self.operation);
            if self.removing.load(Ordering::Acquire) {
                // A timed-out download retains this reservation until its thread has
                // terminated and a retry explicitly acknowledges that termination.
                let finished = operation
                    .as_ref()
                    .and_then(|op| op.thread.as_ref())
                    .is_some_and(JoinHandle::is_finished);
                if !finished {
                    return Err(EnhanceError::new(
                        ErrorCode::Busy,
                        "Model removal is running",
                    ));
                }
                if let Some(mut op) = operation.take() {
                    if let Some(thread) = op.thread.take() {
                        let _ = thread.join();
                    }
                }
            } else {
                self.removing.store(true, Ordering::Release);
            }
            self.clear_enable_intent();
            let previous = operation.take();
            if let Some(op) = &previous {
                op.cancel.store(true, Ordering::Release);
            }
            previous
        };
        // A timed-out download must keep the reservation and its JoinHandle alive.
        struct Release<'a>(&'a AtomicBool, bool);
        impl Drop for Release<'_> {
            fn drop(&mut self) {
                if self.1 {
                    self.0.store(false, Ordering::Release);
                }
            }
        }
        let mut release = Release(&self.removing, true);
        self.shared.update(|s| s.install = InstallState::Removing);
        if let Some(op) = previous.as_mut() {
            if let Some(thread) = op.thread.as_ref() {
                let deadline = std::time::Instant::now() + Duration::from_secs(5);
                while !thread.is_finished() && std::time::Instant::now() < deadline {
                    thread::sleep(Duration::from_millis(10));
                }
                if !thread.is_finished() {
                    let error = EnhanceError::new(
                        ErrorCode::RemoveFailed,
                        "The model download did not stop in time",
                    );
                    self.shared.update(|s| {
                        s.install = self.store.state(entry);
                        s.error = Some(error.clone());
                    });
                    // Restore the handle before exposing the timeout; no competing
                    // install or removal can pass the still-set reservation.
                    *lock(&self.operation) = previous.take();
                    release.1 = false;
                    return Err(error);
                }
            }
            if let Some(thread) = op.thread.take() {
                let _ = thread.join();
            }
        }
        if !self.worker.unload_and_wait(Duration::from_secs(20)) {
            let error = EnhanceError::new(
                ErrorCode::RemoveFailed,
                "The Enhance engine did not stop in time",
            );
            self.shared.update(|s| {
                s.install = self.store.state(entry);
                s.error = Some(error.clone());
            });
            return Err(error);
        }
        let result = self.store.remove(entry);
        self.shared.update(|s| {
            s.install = self.store.state(entry);
            s.load = LoadState::Unloaded;
            s.download = None;
            s.error = result.as_ref().err().cloned();
        });
        result
    }
    pub fn shutdown(&self) {
        self.stop_operation();
        self.worker.shutdown();
    }
}
