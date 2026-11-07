//! Component manager: the speech engine, the CUDA add-on, speech models and the Enhance model
//! are downloaded only on request, verified against a PGP-signed manifest, staged, smoke
//! tested and atomically activated under `<data>/components`. See docs/components.md.
pub mod adapters;
pub mod archive;
pub mod catalog;
pub mod detect;
pub mod fetch;
pub mod jobs;
pub mod manifest;
pub mod store;
#[cfg(test)]
mod tests;
pub mod trust;

use catalog::{Catalog, Files};
use fetch::{Retry, Transport};
use jobs::{Phase, Record};
use manifest::{Component, Kind, Manifest};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, VecDeque},
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Condvar, Mutex, MutexGuard,
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};
use store::{Store, Verdict};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Reason {
    ManifestInvalid,
    TrustRootMissing,
    PrimaryRevoked,
    SignatureInvalid,
    UntrustedKey,
    Rollback,
    Expired,
    ClockError,
    Network,
    NotAllowed,
    SizeMismatch,
    ChecksumMismatch,
    Cancelled,
    InsufficientSpace,
    ArchiveRejected,
    SmokeTestFailed,
    InUse,
    Busy,
    Io,
    NotFound,
}

/// Sanitised error: never contains URLs with tokens, file contents or transcripts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ComponentError {
    pub reason: Reason,
    pub message: String,
}
impl ComponentError {
    pub fn new(reason: Reason, message: impl Into<String>) -> Self {
        Self {
            reason,
            message: message.into(),
        }
    }
}
impl std::fmt::Display for ComponentError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for ComponentError {}

/// Write `bytes` to `path` via a synced temporary file and rename.
pub fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), ComponentError> {
    let fail = |_| ComponentError::new(Reason::Io, "Could not write component state");
    let parent = path
        .parent()
        .ok_or_else(|| ComponentError::new(Reason::Io, "Invalid state path"))?;
    fs::create_dir_all(parent).map_err(fail)?;
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".tmp");
    let temporary = parent.join(name);
    let mut file = fs::File::create(&temporary).map_err(fail)?;
    file.write_all(bytes)
        .and_then(|_| file.sync_all())
        .map_err(fail)?;
    drop(file);
    store::rename(&temporary, path).map_err(fail)?;
    #[cfg(unix)]
    if let Ok(dir) = fs::File::open(parent) {
        let _ = dir.sync_all();
    }
    Ok(())
}

/// Every public entry point taking a component id checks it first: ids become store paths.
fn check_id(id: &str) -> Result<(), ComponentError> {
    if manifest::safe_id(id) {
        Ok(())
    } else {
        Err(ComponentError::new(Reason::NotFound, "Unknown component"))
    }
}

pub fn speech_id(model: &str) -> String {
    format!("speech-{model}")
}
pub fn enhance_id(model: &str) -> String {
    format!("enhance-{model}")
}

/// What the active configuration references; used for "in use" removal checks.
#[derive(Debug, Clone, Default)]
pub struct Usage {
    pub speech_model: String,
    /// Set while Enhance is enabled for automatic rewrites with this model.
    pub enhance_model: Option<String>,
}

pub type Emit = Arc<dyn Fn(&str, Value) + Send + Sync>;

/// Callbacks into the app; all may be called from the job thread.
pub struct Host {
    pub emit: Emit,
    /// Stop the speech engine before its files (engine or CUDA) are replaced or removed.
    pub stop_engine: Box<dyn Fn() + Send + Sync>,
    /// A component of this kind was installed, removed or migrated.
    pub changed: Box<dyn Fn(Kind) + Send + Sync>,
    pub usage: Box<dyn Fn() -> Usage + Send + Sync>,
}

pub type Space = Arc<dyn Fn(&Path) -> Option<u64> + Send + Sync>;
pub type TransportFactory =
    Arc<dyn Fn() -> Result<Box<dyn Transport>, ComponentError> + Send + Sync>;

pub struct Options {
    pub data: PathBuf,
    /// Pre-0.5 speech models (`<data>/models/<name>/`) and Enhance store.
    pub legacy_models: PathBuf,
    pub legacy_enhance: PathBuf,
    pub root: Result<trust::TrustRoot, ComponentError>,
    pub embedded: Option<(Vec<u8>, Vec<u8>)>,
    pub transport: TransportFactory,
    pub space: Space,
    pub retry: Retry,
    pub now: fn() -> u64,
}

impl Options {
    pub fn production(data: PathBuf, legacy_models: PathBuf, legacy_enhance: PathBuf) -> Self {
        Self {
            data,
            legacy_models,
            legacy_enhance,
            root: trust::load_root(),
            embedded: trust::EMBEDDED_MANIFEST.map(|(m, s)| (m.to_vec(), s.to_vec())),
            transport: Arc::new(|| {
                fetch::Https::new(Duration::from_secs(30))
                    .map(|t| Box::new(t) as Box<dyn Transport>)
            }),
            space: Arc::new(crate::platform::available_space),
            retry: Retry::default(),
            now: catalog::now,
        }
    }
}

struct Live {
    record: Record,
    cancel: Arc<AtomicBool>,
    pause: bool,
    rate: f64,
    eta: Option<u64>,
    sample: (Instant, u64),
    emitted: Instant,
}
impl Live {
    fn new(record: Record) -> Self {
        let done = record.bytes_done;
        Self {
            record,
            cancel: Arc::new(AtomicBool::new(false)),
            pause: false,
            rate: 0.0,
            eta: None,
            sample: (Instant::now(), done),
            emitted: Instant::now() - Duration::from_secs(1),
        }
    }
    fn progress_json(&self) -> Value {
        json!({"id": self.record.id, "phase": self.record.phase, "bytes_done": self.record.bytes_done,
            "bytes_total": self.record.bytes_total, "rate": self.rate.round(), "eta": self.eta,
            "error": self.record.error, "reason": self.record.reason})
    }
}

#[derive(Default)]
struct Inner {
    catalog: Option<Catalog>,
    error: Option<ComponentError>,
    development: bool,
    jobs: BTreeMap<String, Live>,
    queue: VecDeque<String>,
    running: Option<String>,
    /// Pre-0.5 files that did not match the manifest: shown as unverified, never loaded.
    unverified: BTreeMap<String, PathBuf>,
    /// Results of an explicit Verify.
    verdicts: BTreeMap<String, Verdict>,
    migrating: bool,
    checking: bool,
    hardware: Option<detect::Hardware>,
}

pub struct Components {
    pub store: Store,
    files: Files,
    jobs_file: PathBuf,
    legacy_models: PathBuf,
    legacy_enhance: PathBuf,
    root: Result<trust::TrustRoot, ComponentError>,
    inner: Mutex<Inner>,
    wake: Condvar,
    host: Host,
    transport: TransportFactory,
    space: Space,
    retry: Retry,
    now: fn() -> u64,
    stopping: AtomicBool,
    worker: Mutex<Option<JoinHandle<()>>>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|p| p.into_inner())
}

impl Components {
    /// Load trust, the offline manifest and persisted jobs. Never touches the network.
    pub fn new(options: Options, host: Host) -> Arc<Self> {
        let root_dir = options.data.join("components");
        let _ = fs::create_dir_all(&root_dir);
        let files = Files::new(&root_dir);
        let jobs_file = root_dir.join("jobs.json");
        let mut inner = Inner::default();
        match &options.root {
            Ok(root) => {
                inner.development = root.development;
                let embedded = options
                    .embedded
                    .as_ref()
                    .map(|(m, s)| (m.as_slice(), s.as_slice()));
                match catalog::load_offline(root, &files, embedded, (options.now)()) {
                    Ok(catalog) => inner.catalog = Some(catalog),
                    Err(error) => inner.error = Some(error),
                }
            }
            Err(error) => inner.error = Some(error.clone()),
        }
        for (id, record) in jobs::load(&jobs_file) {
            inner.jobs.insert(id, Live::new(record));
        }
        if let Some(error) = &inner.error {
            tracing::warn!(target: "murmur::components", reason = ?error.reason, "{}", error.message);
        }
        let this = Arc::new(Self {
            store: Store::new(root_dir),
            files,
            jobs_file,
            legacy_models: options.legacy_models,
            legacy_enhance: options.legacy_enhance,
            root: options.root,
            inner: Mutex::new(inner),
            wake: Condvar::new(),
            host,
            transport: options.transport,
            space: options.space,
            retry: options.retry,
            now: options.now,
            stopping: AtomicBool::new(false),
            worker: Mutex::new(None),
        });
        let worker = this.clone();
        if let Ok(handle) = std::thread::Builder::new()
            .name("murmur-components".into())
            .spawn(move || worker.work())
        {
            *lock(&this.worker) = Some(handle);
        }
        this
    }

    // ---- lookups used by the engine and Enhance ----

    pub fn engine_dir(&self) -> Option<PathBuf> {
        self.store
            .active_dir("engine")
            .filter(|d| d.join(store::ENGINE_FILE).is_file())
    }
    pub fn cuda_dir(&self) -> Option<PathBuf> {
        self.store.active_dir("cuda")
    }
    /// Verified speech model directory for an `engine.model` name.
    pub fn speech_model_dir(&self, model: &str) -> Option<PathBuf> {
        let id = speech_id(model);
        if !manifest::safe_id(&id) {
            return None;
        }
        let inner = lock(&self.inner);
        if inner
            .verdicts
            .get(&id)
            .is_some_and(|v| *v != Verdict::Verified)
        {
            return None;
        }
        let no_catalog = inner.catalog.is_none();
        drop(inner);
        if let Some(dir) = self.store.active_dir(&id) {
            return Some(dir);
        }
        // Development convenience only: without any trusted manifest, debug builds keep
        // using pre-0.5 models in place. Release builds never load unverified files.
        if cfg!(debug_assertions) && no_catalog {
            let legacy = self.legacy_models.join(model);
            if store::SPEECH_REQUIRED
                .iter()
                .all(|f| legacy.join(f).is_file())
            {
                return Some(legacy);
            }
        }
        None
    }
    pub fn enhance_model_path(&self, model: &str) -> Option<PathBuf> {
        let id = enhance_id(model);
        if lock(&self.inner)
            .verdicts
            .get(&id)
            .is_some_and(|v| *v != Verdict::Verified)
        {
            return None;
        }
        self.store
            .active_dir(&id)
            .map(|d| d.join("model.gguf"))
            .filter(|p| p.is_file())
    }
    pub fn manifest(&self) -> Option<Manifest> {
        lock(&self.inner)
            .catalog
            .as_ref()
            .map(|c| c.manifest.clone())
    }
    pub fn job(&self, id: &str) -> Option<Record> {
        lock(&self.inner).jobs.get(id).map(|j| j.record.clone())
    }
    pub fn job_running(&self, id: &str) -> bool {
        lock(&self.inner)
            .jobs
            .get(id)
            .is_some_and(|j| !matches!(j.record.phase, Phase::Paused | Phase::Failed))
    }

    // ---- state for the UI ----

    fn emit_changed(&self) {
        (self.host.emit)("components/changed", json!({}));
    }

    pub fn hardware(&self) -> detect::Hardware {
        if let Some(hardware) = lock(&self.inner).hardware.clone() {
            let mut hardware = hardware;
            hardware.free_bytes = (self.space)(&self.store.root);
            return hardware;
        }
        let found = detect::detect(&self.store.root);
        lock(&self.inner).hardware = Some(found.clone());
        found
    }

    fn state_of(
        &self,
        inner: &Inner,
        component: &Component,
        usage: &Usage,
    ) -> (&'static str, Option<String>) {
        let active = self.store.active(&component.id);
        let installed_version = active.as_ref().map(|a| a.version.clone());
        if let Some(job) = inner.jobs.get(&component.id) {
            let phase = match job.record.phase {
                Phase::Paused => "paused",
                Phase::Failed => "failed",
                Phase::Queued => "queued",
                _ => "installing",
            };
            return (phase, installed_version);
        }
        if inner
            .verdicts
            .get(&component.id)
            .is_some_and(|v| *v != Verdict::Verified)
        {
            return ("corrupt", installed_version);
        }
        match active {
            Some(_) if self.store.active_dir(&component.id).is_none() => {
                ("corrupt", installed_version)
            }
            Some(_) => {
                let selected = match component.kind {
                    Kind::SpeechModel => {
                        component.model.as_deref() == Some(usage.speech_model.as_str())
                    }
                    Kind::EnhanceModel => {
                        component.model.is_some() && component.model == usage.enhance_model
                    }
                    Kind::Engine | Kind::Cuda => true,
                };
                (
                    if selected { "active" } else { "installed" },
                    installed_version,
                )
            }
            None if inner.unverified.contains_key(&component.id) => ("unverified", None),
            None => ("available", None),
        }
    }

    fn in_use(&self, component: &Component, usage: &Usage) -> Option<String> {
        match component.kind {
            Kind::SpeechModel if component.model.as_deref() == Some(usage.speech_model.as_str()) => {
                Some("This is the selected speech model. Choose another model first.".into())
            }
            Kind::EnhanceModel if component.model.is_some() && component.model == usage.enhance_model => {
                Some("Enhance uses this model for automatic rewrites. Turn automatic rewrites off first.".into())
            }
            _ => None,
        }
    }

    fn dependents(&self, inner: &Inner, id: &str) -> Vec<String> {
        let mut names = Vec::new();
        if let Some(catalog) = &inner.catalog {
            for other in catalog.manifest.local() {
                if other.depends.iter().any(|d| d == id) && self.store.active(&other.id).is_some() {
                    names.push(other.display_name.clone());
                }
            }
        } else if id == "engine" && self.store.active("cuda").is_some() {
            names.push("NVIDIA GPU acceleration".into());
        }
        names
    }

    pub fn snapshot(&self) -> Value {
        let usage = (self.host.usage)();
        let hardware = self.hardware();
        let inner = lock(&self.inner);
        let mut components = Vec::new();
        let listed: Vec<Component> = inner
            .catalog
            .as_ref()
            .map(|c| c.manifest.local().cloned().collect())
            .unwrap_or_default();
        for component in &listed {
            let (state, installed_version) = self.state_of(&inner, component, &usage);
            let job = inner.jobs.get(&component.id).map(Live::progress_json);
            let mut value = serde_json::to_value(component).unwrap_or(Value::Null);
            value["state"] = json!(state);
            value["installed_version"] = json!(installed_version);
            value["update_available"] = json!(installed_version
                .as_ref()
                .is_some_and(|v| *v != component.version));
            value["in_use"] = json!(self.in_use(component, &usage));
            value["dependents"] = json!(self.dependents(&inner, &component.id));
            value["job"] = job.unwrap_or(Value::Null);
            value["legacy_path"] = json!(inner.unverified.get(&component.id));
            value["migrated"] = json!(self.store.active(&component.id).is_some_and(|a| a.migrated));
            // Signed URLs are not needed by the UI.
            if let Some(object) = value.as_object_mut() {
                object.remove("urls");
                object.remove("files");
            }
            components.push(value);
        }
        // Installed components the current manifest does not list (or no manifest at all).
        if let Ok(entries) = fs::read_dir(&self.store.root) {
            for entry in entries.flatten() {
                let id = entry.file_name().to_string_lossy().into_owned();
                if !manifest::safe_id(&id) || listed.iter().any(|c| c.id == id) {
                    continue;
                }
                if let Some(active) = self.store.active(&id) {
                    let present = self.store.active_dir(&id).is_some();
                    components.push(json!({"id": id, "kind": active.kind, "display_name": id, "description": "",
                        "version": active.version, "installed_version": active.version, "model": active.model,
                        "state": if present { "installed" } else { "corrupt" }, "update_available": false,
                        "in_use": Value::Null, "dependents": [], "job": Value::Null, "unlisted": true}));
                }
            }
        }
        let recommendation = detect::recommend(&hardware);
        let stale = inner
            .catalog
            .as_ref()
            .is_some_and(|c| c.is_stale((self.now)()));
        json!({
            "trust": {"ok": inner.catalog.is_some(), "development": inner.development,
                "reason": inner.error.as_ref().map(|e| e.reason), "message": inner.error.as_ref().map(|e| e.message.clone())},
            "manifest": inner.catalog.as_ref().map(|c| json!({"serial": c.manifest.serial, "issued_at": c.manifest.issued_at,
                "expires_at": c.manifest.expires_at, "source": c.source, "stale": stale})),
            "stale": stale,
            "hardware": hardware,
            "recommendation": recommendation,
            "components": components,
            "migrating": inner.migrating,
            "checking": inner.checking,
            "engine_installed": self.engine_dir().is_some(),
        })
    }

    // ---- jobs ----

    fn persist(&self, inner: &Inner) {
        let records = inner
            .jobs
            .iter()
            .map(|(id, live)| (id.clone(), live.record.clone()))
            .collect();
        if let Err(error) = jobs::save(&self.jobs_file, &records) {
            tracing::warn!(target: "murmur::components", "{error}");
        }
    }

    fn require_catalog<'a>(&self, inner: &'a Inner) -> Result<&'a Catalog, ComponentError> {
        inner.catalog.as_ref().ok_or_else(|| {
            inner.error.clone().unwrap_or_else(|| {
                ComponentError::new(Reason::NotFound, "No component manifest is available")
            })
        })
    }

    /// A catalog that may fetch or activate new bytes: present and not expired.
    fn require_fresh<'a>(&self, inner: &'a Inner) -> Result<&'a Catalog, ComponentError> {
        let catalog = self.require_catalog(inner)?;
        if catalog.is_stale((self.now)()) {
            return Err(catalog::expired());
        }
        Ok(catalog)
    }

    /// Queue installs (dependencies first). The caller is the user's explicit click.
    pub fn install(&self, ids: &[String]) -> Result<(), ComponentError> {
        for id in ids {
            check_id(id)?;
        }
        let mut inner = lock(&self.inner);
        let catalog = self.require_fresh(&inner)?.clone();
        let mut order: Vec<Component> = Vec::new();
        fn visit(
            catalog: &Manifest,
            id: &str,
            order: &mut Vec<Component>,
            depth: usize,
        ) -> Result<(), ComponentError> {
            let component = catalog.find(id).ok_or_else(|| {
                ComponentError::new(Reason::NotFound, format!("Unknown component {id}"))
            })?;
            if depth > 8 {
                return Err(ComponentError::new(
                    Reason::ManifestInvalid,
                    "Component dependencies form a cycle",
                ));
            }
            for dependency in &component.depends {
                visit(catalog, dependency, order, depth + 1)?;
            }
            if !order.iter().any(|c| c.id == component.id) {
                order.push(component.clone());
            }
            Ok(())
        }
        for id in ids {
            visit(&catalog.manifest, id, &mut order, 0)?;
        }
        for component in order {
            let current = self
                .store
                .active(&component.id)
                .is_some_and(|a| a.version == component.version)
                && self.store.active_dir(&component.id).is_some()
                && !inner
                    .verdicts
                    .get(&component.id)
                    .is_some_and(|v| *v != Verdict::Verified);
            let explicitly = ids.contains(&component.id);
            if (current && !explicitly)
                || inner
                    .jobs
                    .get(&component.id)
                    .is_some_and(|j| !matches!(j.record.phase, Phase::Paused | Phase::Failed))
            {
                continue;
            }
            let record = Record {
                id: component.id.clone(),
                version: component.version.clone(),
                phase: Phase::Queued,
                bytes_done: self.store.part_bytes(&component),
                bytes_total: component.size_bytes,
                error: None,
                reason: None,
                replace: true,
            };
            inner.verdicts.remove(&component.id);
            inner.jobs.insert(component.id.clone(), Live::new(record));
            if !inner.queue.contains(&component.id) {
                inner.queue.push_back(component.id.clone());
            }
        }
        self.persist(&inner);
        drop(inner);
        self.wake.notify_all();
        self.emit_changed();
        Ok(())
    }

    /// Pause (keep partial downloads) or cancel (discard them) a queued or running job.
    pub fn stop(&self, id: &str, pause: bool) -> Result<(), ComponentError> {
        let mut inner = lock(&self.inner);
        let running = inner.running.as_deref() == Some(id);
        let Some(job) = inner.jobs.get_mut(id) else {
            return Err(ComponentError::new(
                Reason::NotFound,
                "No job for this component",
            ));
        };
        if running {
            job.pause = pause;
            job.cancel.store(true, Ordering::Release);
            return Ok(());
        }
        inner.queue.retain(|q| q != id);
        if pause {
            if let Some(job) = inner.jobs.get_mut(id) {
                job.record.phase = Phase::Paused;
            }
        } else {
            let job = inner.jobs.remove(id);
            self.discard_files(&inner, id, job.map(|j| j.record.version));
        }
        self.persist(&inner);
        drop(inner);
        self.emit_changed();
        Ok(())
    }

    /// Resume a paused or failed job (from its partial download).
    pub fn resume(&self, id: &str) -> Result<(), ComponentError> {
        check_id(id)?;
        let mut inner = lock(&self.inner);
        self.require_fresh(&inner)?;
        let job = inner
            .jobs
            .get_mut(id)
            .filter(|j| matches!(j.record.phase, Phase::Paused | Phase::Failed))
            .ok_or_else(|| ComponentError::new(Reason::NotFound, "Nothing to resume"))?;
        job.record.phase = Phase::Queued;
        job.record.error = None;
        job.record.reason = None;
        job.cancel = Arc::new(AtomicBool::new(false));
        job.pause = false;
        inner.queue.push_back(id.to_owned());
        self.persist(&inner);
        drop(inner);
        self.wake.notify_all();
        self.emit_changed();
        Ok(())
    }

    /// Forget a paused or failed job and delete its partial download.
    pub fn discard(&self, id: &str) -> Result<(), ComponentError> {
        let mut inner = lock(&self.inner);
        let paused = inner
            .jobs
            .get(id)
            .is_some_and(|j| matches!(j.record.phase, Phase::Paused | Phase::Failed));
        if !paused {
            return Err(ComponentError::new(
                Reason::Busy,
                "Only paused or failed jobs can be discarded",
            ));
        }
        let job = inner.jobs.remove(id);
        self.discard_files(&inner, id, job.map(|j| j.record.version));
        self.persist(&inner);
        drop(inner);
        self.emit_changed();
        Ok(())
    }

    fn discard_files(&self, inner: &Inner, id: &str, version: Option<String>) {
        if let Some(component) = inner.catalog.as_ref().and_then(|c| c.manifest.find(id)) {
            let mut component = component.clone();
            if let Some(version) = version {
                component.version = version;
            }
            self.store.discard_parts(&component);
        }
    }

    fn work(self: Arc<Self>) {
        loop {
            let (component, cancel) = {
                let mut inner = lock(&self.inner);
                loop {
                    if self.stopping.load(Ordering::Acquire) {
                        return;
                    }
                    if let Some(id) = inner.queue.pop_front() {
                        let component = inner
                            .catalog
                            .as_ref()
                            .and_then(|c| c.manifest.find(&id))
                            .cloned();
                        match (component, inner.jobs.get_mut(&id)) {
                            (Some(component), Some(job)) => {
                                job.record.phase = Phase::Downloading;
                                job.record.version = component.version.clone();
                                job.sample = (Instant::now(), job.record.bytes_done);
                                let cancel = job.cancel.clone();
                                inner.running = Some(id);
                                self.persist(&inner);
                                break (component, cancel);
                            }
                            _ => continue,
                        }
                    }
                    inner = self.wake.wait(inner).unwrap_or_else(|p| p.into_inner());
                }
            };
            self.emit_changed();
            let result = self.run_job(&component, &cancel);
            let mut inner = lock(&self.inner);
            inner.running = None;
            let pause = inner.jobs.get(&component.id).is_some_and(|j| j.pause);
            match &result {
                Ok(()) => {
                    inner.jobs.remove(&component.id);
                    inner.unverified.remove(&component.id);
                }
                Err(error) if error.reason == Reason::Cancelled && pause => {
                    if let Some(job) = inner.jobs.get_mut(&component.id) {
                        job.record.phase = Phase::Paused;
                        job.record.bytes_done = self.store.part_bytes(&component);
                    }
                }
                Err(error) if error.reason == Reason::Cancelled => {
                    inner.jobs.remove(&component.id);
                    self.store.discard_parts(&component);
                }
                Err(error) => {
                    tracing::warn!(target: "murmur::components", id = %component.id, reason = ?error.reason, "{}", error.message);
                    if let Some(job) = inner.jobs.get_mut(&component.id) {
                        job.record.phase = Phase::Failed;
                        job.record.error = Some(error.message.clone());
                        job.record.reason = Some(error.reason);
                        job.record.bytes_done = self.store.part_bytes(&component);
                        let progress = job.progress_json();
                        (self.host.emit)("components/progress", progress);
                    }
                }
            }
            self.persist(&inner);
            drop(inner);
            if result.is_ok() {
                tracing::info!(target: "murmur::components", id = %component.id, version = %component.version, "component installed");
                (self.host.changed)(component.kind);
            }
            self.emit_changed();
        }
    }

    fn run_job(
        &self,
        component: &Component,
        cancel: &Arc<AtomicBool>,
    ) -> Result<(), ComponentError> {
        let transport = (self.transport)()?;
        let replace = lock(&self.inner)
            .jobs
            .get(&component.id)
            .is_some_and(|j| j.record.replace);
        let stop_engine = |c: &Component| {
            if matches!(c.kind, Kind::Engine | Kind::Cuda) {
                (self.host.stop_engine)();
            }
        };
        let ctx = jobs::Context {
            store: &self.store,
            transport: transport.as_ref(),
            cancel,
            retry: self.retry,
            free: (self.space)(&self.store.root),
            replace,
            now: (self.now)(),
            before_activate: &stop_engine,
        };
        jobs::run(component, &ctx, &mut |phase, done, total| {
            self.progress(&component.id, phase, done, total)
        })
    }

    fn progress(&self, id: &str, phase: Phase, done: u64, total: u64) {
        let mut inner = lock(&self.inner);
        let Some(job) = inner.jobs.get_mut(id) else {
            return;
        };
        let phase_changed = job.record.phase != phase;
        job.record.phase = phase;
        job.record.bytes_done = done;
        job.record.bytes_total = total;
        let elapsed = job.sample.0.elapsed();
        if elapsed >= Duration::from_secs(1) {
            let rate = done.saturating_sub(job.sample.1) as f64 / elapsed.as_secs_f64();
            job.rate = if job.rate == 0.0 {
                rate
            } else {
                job.rate * 0.7 + rate * 0.3
            };
            job.eta =
                (job.rate > 1.0).then(|| (total.saturating_sub(done) as f64 / job.rate) as u64);
            job.sample = (Instant::now(), done);
        }
        if phase_changed || job.emitted.elapsed() >= Duration::from_millis(250) || done == total {
            job.emitted = Instant::now();
            let value = job.progress_json();
            if phase_changed {
                self.persist(&inner);
            }
            drop(inner);
            (self.host.emit)("components/progress", value);
        }
    }

    // ---- removal, verification, updates ----

    pub fn remove(&self, id: &str) -> Result<(), ComponentError> {
        let usage = (self.host.usage)();
        let inner = lock(&self.inner);
        if inner
            .jobs
            .get(id)
            .is_some_and(|j| !matches!(j.record.phase, Phase::Paused | Phase::Failed))
        {
            return Err(ComponentError::new(
                Reason::Busy,
                "This component is being installed",
            ));
        }
        let active = self.store.active(id).ok_or_else(|| {
            ComponentError::new(Reason::NotFound, "This component is not installed")
        })?;
        let dependents = self.dependents(&inner, id);
        if !dependents.is_empty() {
            return Err(ComponentError::new(
                Reason::InUse,
                format!(
                    "{} needs this component. Remove it first.",
                    dependents.join(", ")
                ),
            ));
        }
        let component = inner
            .catalog
            .as_ref()
            .and_then(|c| c.manifest.find(id))
            .cloned()
            .unwrap_or_else(|| Component {
                id: id.into(),
                version: active.version.clone(),
                os: manifest::OS.into(),
                arch: manifest::ARCH.into(),
                kind: active.kind,
                urls: vec![],
                size_bytes: 0,
                sha256: None,
                installed_size_bytes: 0,
                temp_size_bytes: 0,
                archive: manifest::Archive::Zip,
                files: vec![],
                depends: vec![],
                license: manifest::License {
                    name: String::new(),
                    url: String::new(),
                },
                provenance: manifest::Provenance {
                    kind: manifest::ProvenanceType::MurmurAttestedOnly,
                    source_url: String::new(),
                    source_revision: String::new(),
                    source_sha256: String::new(),
                    note: String::new(),
                },
                display_name: id.into(),
                description: String::new(),
                model: active.model.clone(),
                quality: None,
                speed: None,
                memory_mb: None,
                languages: None,
            });
        if let Some(reason) = self.in_use(&component, &usage) {
            return Err(ComponentError::new(Reason::InUse, reason));
        }
        drop(inner);
        if matches!(active.kind, Kind::Engine | Kind::Cuda) {
            (self.host.stop_engine)();
        }
        self.store.remove(id)?;
        let mut inner = lock(&self.inner);
        inner.verdicts.remove(id);
        drop(inner);
        tracing::info!(target: "murmur::components", %id, "component removed");
        (self.host.changed)(active.kind);
        self.emit_changed();
        Ok(())
    }

    /// Re-check installed files: hashes for file components, the smoke test for archives.
    pub fn verify(&self, id: &str) -> Result<Verdict, ComponentError> {
        let component = {
            let inner = lock(&self.inner);
            self.require_catalog(&inner)?.manifest.find(id).cloned()
        }
        .ok_or_else(|| ComponentError::new(Reason::NotFound, "Unknown component"))?;
        let dir = self.store.active_dir(id).ok_or_else(|| {
            ComponentError::new(Reason::NotFound, "This component is not installed")
        })?;
        let installed = self.store.active(id).map(|a| a.version);
        let verdict = if installed.as_deref() != Some(component.version.as_str()) {
            // An older version: only the smoke test can be repeated.
            if store::smoke_test(&component, &dir).is_ok() {
                Verdict::Verified
            } else {
                Verdict::Mismatch
            }
        } else if component.files.is_empty() {
            if store::smoke_test(&component, &dir).is_ok() {
                Verdict::Verified
            } else {
                Verdict::Mismatch
            }
        } else {
            store::verify_files(&component, &dir, &AtomicBool::new(false))?
        };
        lock(&self.inner).verdicts.insert(id.to_owned(), verdict);
        (self.host.changed)(component.kind);
        self.emit_changed();
        Ok(verdict)
    }

    /// Explicit "Check for updates": fetch manifest, signature and certificate together.
    pub fn check_updates(&self) -> Result<(), ComponentError> {
        let root = self.root.clone()?;
        {
            let mut inner = lock(&self.inner);
            if inner.checking {
                return Err(ComponentError::new(
                    Reason::Busy,
                    "Already checking for updates",
                ));
            }
            inner.checking = true;
        }
        self.emit_changed();
        let result = (self.transport)().and_then(|t| {
            catalog::fetch_and_admit(
                &root,
                &self.files,
                t.as_ref(),
                &catalog::base_url(),
                (self.now)(),
            )
        });
        let mut inner = lock(&self.inner);
        inner.checking = false;
        let outcome = match result {
            Ok(catalog) => {
                tracing::info!(target: "murmur::components", serial = catalog.manifest.serial, "component manifest updated");
                inner.catalog = Some(catalog);
                inner.error = None;
                Ok(())
            }
            Err(error) => {
                if error.reason == Reason::PrimaryRevoked {
                    inner.catalog = None;
                    inner.error = Some(error.clone());
                }
                Err(error)
            }
        };
        drop(inner);
        self.emit_changed();
        outcome
    }

    // ---- migration of pre-0.5 models ----

    /// Hash pre-0.5 speech and Enhance models against the manifest once. Matching files are
    /// moved into the store (same file system, no copy, no download); anything else is left
    /// where it is and shown as unverified. Nothing is deleted.
    pub fn migrate(&self) {
        let Some(manifest) = self.manifest() else {
            return;
        };
        lock(&self.inner).migrating = true;
        self.emit_changed();
        let mut changed = Vec::new();
        for component in manifest.local() {
            if self.store.active(&component.id).is_some() || self.stopping.load(Ordering::Acquire) {
                continue;
            }
            let legacy = match (component.kind, component.model.as_deref()) {
                (Kind::SpeechModel, Some(model)) => self.legacy_models.join(model),
                (Kind::EnhanceModel, Some(model)) => {
                    let revision = component.provenance.source_revision.clone();
                    self.legacy_enhance.join(model).join(revision)
                }
                _ => continue,
            };
            if !manifest::safe_id(component.model.as_deref().unwrap_or(""))
                && component.kind == Kind::SpeechModel
            {
                continue;
            }
            let Ok(meta) = fs::symlink_metadata(&legacy) else {
                continue;
            };
            if !meta.is_dir() {
                continue;
            }
            let verdict =
                store::verify_files(component, &legacy, &self.stopping).unwrap_or(Verdict::Missing);
            if verdict != Verdict::Verified {
                tracing::info!(target: "murmur::components", id = %component.id, ?verdict, "pre-0.5 model not verified; left in place");
                lock(&self.inner)
                    .unverified
                    .insert(component.id.clone(), legacy);
                changed.push(component.kind);
                continue;
            }
            let target = self.store.version_dir(&component.id, &component.version);
            let moved = fs::create_dir_all(self.store.component_dir(&component.id)).is_ok()
                && store::rename(&legacy, &target).is_ok();
            let record = store::Active {
                version: component.version.clone(),
                kind: component.kind,
                model: component.model.clone(),
                location: (!moved).then(|| legacy.clone()),
                migrated: true,
                installed_at: (self.now)(),
            };
            match self.store.write_active(&component.id, &record) {
                Ok(()) => {
                    tracing::info!(target: "murmur::components", id = %component.id, moved, "pre-0.5 model verified and adopted");
                    changed.push(component.kind);
                }
                Err(error) => tracing::warn!(target: "murmur::components", "{error}"),
            }
        }
        lock(&self.inner).migrating = false;
        changed.dedup();
        for kind in changed {
            (self.host.changed)(kind);
        }
        self.emit_changed();
    }

    pub fn shutdown(&self) {
        self.stopping.store(true, Ordering::Release);
        {
            // Mark jobs paused before cancelling, so the worker keeps their partial
            // downloads: an interrupted job resumes later, it is not discarded.
            let mut inner = lock(&self.inner);
            for job in inner.jobs.values_mut() {
                job.pause = true;
                job.cancel.store(true, Ordering::Release);
            }
        }
        self.wake.notify_all();
        if let Some(worker) = lock(&self.worker).take() {
            let _ = worker.join();
        }
        let mut inner = lock(&self.inner);
        for job in inner.jobs.values_mut() {
            if job.record.phase != Phase::Failed {
                job.record.phase = Phase::Paused;
            }
        }
        self.persist(&inner);
    }
}

/// Headless `--install-component`: verify `manifest` + `signature` with the trust root in
/// force, then install `path` (a zip, or a directory of model files) without the network.
pub fn install_from_files(
    data: &Path,
    path: &Path,
    manifest_path: &Path,
    signature_path: &Path,
) -> Result<String, ComponentError> {
    let root = trust::load_root()?;
    let store_root = data.join("components");
    let files = Files::new(&store_root);
    let bytes = fs::read(manifest_path)
        .map_err(|_| ComponentError::new(Reason::Io, "Cannot read the manifest"))?;
    let signature = fs::read(signature_path)
        .map_err(|_| ComponentError::new(Reason::Io, "Cannot read the signature"))?;
    let manifest = catalog::verify(
        &root,
        fs::read(&files.cert).ok().as_deref(),
        &bytes,
        &signature,
    )?;
    let sha = catalog::digest(&bytes);
    let mut state = catalog::RollbackState::load(&files.state);
    state.admit(&manifest, &sha, catalog::now(), 0)?;
    state.save(&files.state)?;
    write_atomic(&files.manifest, &bytes)?;
    write_atomic(&files.signature, &signature)?;
    let store = Store::new(store_root);
    let cancel = Arc::new(AtomicBool::new(false));
    let none: &dyn Fn(&Component) = &|_| {};
    struct Offline;
    impl Transport for Offline {
        fn open(
            &self,
            _: &str,
            _: u64,
            _: &Arc<AtomicBool>,
        ) -> Result<fetch::Body, ComponentError> {
            Err(ComponentError::new(Reason::NotAllowed, "Offline install"))
        }
    }
    let ctx = jobs::Context {
        store: &store,
        transport: &Offline,
        cancel: &cancel,
        retry: Retry::default(),
        free: crate::platform::available_space(data),
        replace: true,
        now: catalog::now(),
        before_activate: none,
    };
    if path.is_dir() {
        let component = manifest
            .local()
            .filter(|c| !c.files.is_empty())
            .find(|c| store::verify_files(c, path, &cancel).ok() == Some(Verdict::Verified))
            .ok_or_else(|| {
                ComponentError::new(
                    Reason::ChecksumMismatch,
                    "The folder matches no component in the manifest",
                )
            })?;
        let staging = store.staging_dir(&component.id, &component.version);
        let _ = fs::remove_dir_all(&staging);
        fs::create_dir_all(&staging)
            .map_err(|_| ComponentError::new(Reason::Io, "Cannot stage the component"))?;
        for file in &component.files {
            fs::copy(path.join(&file.name), staging.join(&file.name))
                .map_err(|_| ComponentError::new(Reason::Io, "Cannot copy the model"))?;
        }
        if let Err(error) =
            store::verify_files(component, &staging, &cancel).and_then(|v| match v {
                Verdict::Verified => store::smoke_test(component, &staging),
                _ => Err(ComponentError::new(
                    Reason::ChecksumMismatch,
                    "The copied files changed",
                )),
            })
        {
            let _ = fs::remove_dir_all(&staging);
            return Err(error);
        }
        store.activate(component, &staging, true, catalog::now())?;
        return Ok(component.id.clone());
    }
    let (size, digest) = fetch::hash_file(path, None)
        .map_err(|_| ComponentError::new(Reason::Io, "Cannot read the archive"))?;
    let component = manifest
        .local()
        .find(|c| c.sha256.as_deref() == Some(digest.as_str()) && c.size_bytes == size)
        .ok_or_else(|| {
            ComponentError::new(
                Reason::ChecksumMismatch,
                "The archive matches no component in the manifest",
            )
        })?;
    jobs::install_local(component, path, &ctx)?;
    Ok(component.id.clone())
}
