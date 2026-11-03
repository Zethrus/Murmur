//! The single persistent inference thread ("murmur-enhance"). Owns the backend and therefore
//! every model/context object. One active request plus at most one queued replacement;
//! cancellation is an atomic flag checked between native calls and never waits behind the
//! active job in a queue.
use super::{
    backend::{Backend, Budget},
    catalog::Entry,
    output::{self, Outcome},
    prompt::{self, Preset},
    EnhanceError, ErrorCode,
};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Read,
    panic::{catch_unwind, AssertUnwindSafe},
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Condvar, Mutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant, SystemTime},
};

#[derive(Clone)]
pub struct Job {
    pub request_id: u64,
    pub dictation_id: u64,
    pub generation: u64,
    pub body: String,
    pub preset: Preset,
    pub dictionary: Vec<String>,
    pub entry: &'static Entry,
    pub model_path: PathBuf,
    pub threads: u32,
    pub load_timeout: Duration,
    pub timeout: Duration,
    pub unload_after: Duration,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WorkerEvent {
    Loading {
        request_id: u64,
    },
    Rewriting {
        request_id: u64,
    },
    Finished {
        request_id: u64,
        dictation_id: u64,
        generation: u64,
        result: Result<Outcome, EnhanceError>,
    },
    Unloaded,
}

type Sink = Arc<dyn Fn(WorkerEvent) + Send + Sync>;

struct Queue {
    pending: Option<Job>,
    active: Option<(u64, Arc<AtomicBool>)>,
    unload_waiters: Vec<mpsc::Sender<()>>,
    shutdown: bool,
    latest: u64,
    cancelled: std::collections::HashSet<u64>,
}

pub struct Worker {
    shared: Arc<(Mutex<Queue>, Condvar)>,
    thread: Mutex<Option<JoinHandle<()>>>,
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|poison| poison.into_inner())
}

/// (len, mtime) of a file whose bytes were hashed and matched the catalog in this process.
type Verified = Option<(PathBuf, u64, Option<SystemTime>)>;

fn verify(
    entry: &Entry,
    path: &PathBuf,
    budget: &Budget,
    cache: &mut Verified,
) -> Result<(), EnhanceError> {
    let meta = fs::metadata(path).map_err(|_| {
        EnhanceError::new(ErrorCode::ModelMissing, "Enhance model is not installed")
    })?;
    let stamp = (path.clone(), meta.len(), meta.modified().ok());
    if cache.as_ref() == Some(&stamp) {
        return Ok(());
    }
    if meta.len() != entry.size_bytes {
        return Err(EnhanceError::new(
            ErrorCode::Corrupt,
            "Enhance model file has the wrong size; reinstall it",
        ));
    }
    let mut file = fs::File::open(path).map_err(|_| {
        EnhanceError::new(ErrorCode::ModelMissing, "Enhance model could not be opened")
    })?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        budget.check()?;
        let n = file.read(&mut buffer).map_err(|_| {
            EnhanceError::new(ErrorCode::LoadFailed, "Could not read the Enhance model")
        })?;
        if n == 0 {
            break;
        }
        hasher.update(&buffer[..n]);
    }
    if !super::download::hex(&hasher.finalize()).eq_ignore_ascii_case(entry.sha256) {
        return Err(EnhanceError::new(
            ErrorCode::Corrupt,
            "Enhance model failed verification; reinstall it",
        ));
    }
    *cache = Some(stamp);
    Ok(())
}

fn run_job(
    backend: &mut dyn Backend,
    job: &Job,
    cancel: &Arc<AtomicBool>,
    sink: &Sink,
    verified: &mut Verified,
) -> Result<Outcome, EnhanceError> {
    let prompt = prompt::build(&job.body, job.preset, &job.dictionary)?;
    if !backend.available() {
        return Err(EnhanceError::new(
            ErrorCode::BackendUnavailable,
            "This build of Murmur does not include the Enhance engine",
        ));
    }
    if !backend.loaded() {
        sink(WorkerEvent::Loading {
            request_id: job.request_id,
        });
        let budget = Budget {
            cancel: cancel.clone(),
            deadline: Instant::now() + job.load_timeout,
            timeout_code: ErrorCode::LoadTimeout,
        };
        verify(job.entry, &job.model_path, &budget, verified)?;
        backend.load(&job.model_path, job.threads, &budget)?;
    } else {
        // Recheck cheap metadata; a changed file forces a fresh hash on next load.
        let budget = Budget {
            cancel: cancel.clone(),
            deadline: Instant::now() + job.load_timeout,
            timeout_code: ErrorCode::LoadTimeout,
        };
        if let Err(error) = verify(job.entry, &job.model_path, &budget, verified) {
            backend.unload();
            return Err(error);
        }
    }
    sink(WorkerEvent::Rewriting {
        request_id: job.request_id,
    });
    let budget = Budget {
        cancel: cancel.clone(),
        deadline: Instant::now() + job.timeout,
        timeout_code: ErrorCode::Timeout,
    };
    let raw = backend.generate(&prompt, &budget)?;
    budget.check()?;
    output::parse(&raw, &prompt, &job.body)
}

impl Worker {
    pub fn spawn(mut backend: Box<dyn Backend + Send>, sink: Sink) -> std::io::Result<Self> {
        let shared = Arc::new((
            Mutex::new(Queue {
                pending: None,
                active: None,
                unload_waiters: Vec::new(),
                shutdown: false,
                latest: 0,
                cancelled: std::collections::HashSet::new(),
            }),
            Condvar::new(),
        ));
        let state = shared.clone();
        let thread = thread::Builder::new()
            .name("murmur-enhance".into())
            .spawn(move || {
                let (queue, signal) = &*state;
                let mut unload_at: Option<Instant> = None;
                let mut verified: Verified = None;
                loop {
                    let next = {
                        let mut q = lock(queue);
                        loop {
                            if q.shutdown {
                                break None;
                            }
                            if !q.unload_waiters.is_empty() {
                                break Some(Err(std::mem::take(&mut q.unload_waiters)));
                            }
                            if let Some(job) = q.pending.take() {
                                let cancel = Arc::new(AtomicBool::new(false));
                                q.active = Some((job.request_id, cancel.clone()));
                                break Some(Ok((job, cancel)));
                            }
                            match unload_at {
                                Some(at) if Instant::now() >= at => {
                                    break Some(Err(Vec::new()));
                                }
                                Some(at) => {
                                    let wait = at.saturating_duration_since(Instant::now());
                                    q = signal.wait_timeout(q, wait).map(|(g, _)| g).unwrap_or_else(|p| p.into_inner().0);
                                }
                                None => {
                                    q = signal.wait(q).unwrap_or_else(|p| p.into_inner());
                                }
                            }
                        }
                    };
                    match next {
                        None => break,
                        Some(Err(waiters)) => {
                            let was = backend.loaded();
                            backend.unload();
                            unload_at = None;
                            if was {
                                sink(WorkerEvent::Unloaded);
                            }
                            for waiter in waiters {
                                let _ = waiter.send(());
                            }
                        }
                        Some(Ok((job, cancel))) => {
                            let result = catch_unwind(AssertUnwindSafe(|| {
                                run_job(backend.as_mut(), &job, &cancel, &sink, &mut verified)
                            }))
                            .unwrap_or_else(|_| {
                                Err(EnhanceError::new(ErrorCode::InferenceFailed, "The Enhance engine failed"))
                            });
                            if matches!(&result, Err(e) if matches!(e.code, ErrorCode::InferenceFailed | ErrorCode::Corrupt | ErrorCode::LoadFailed)) {
                                backend.unload();
                                verified = None;
                            }
                            lock(queue).active = None;
                            sink(WorkerEvent::Finished {
                                request_id: job.request_id,
                                dictation_id: job.dictation_id,
                                generation: job.generation,
                                result,
                            });
                            unload_at = backend.loaded().then(|| Instant::now() + job.unload_after);
                        }
                    }
                }
                backend.unload();
            })?;
        Ok(Self {
            shared,
            thread: Mutex::new(Some(thread)),
        })
    }
    /// Returns false for a cancelled, stale, or shutdown submission.
    pub fn submit(&self, job: Job) -> bool {
        let (queue, signal) = &*self.shared;
        let mut q = lock(queue);
        if q.cancelled.remove(&job.request_id) || q.shutdown || job.request_id <= q.latest {
            return false;
        }
        q.latest = job.request_id;
        if let Some((_, cancel)) = &q.active {
            cancel.store(true, Ordering::Release);
        }
        q.pending = Some(job);
        signal.notify_all();
        true
    }
    /// Cancel the matching active/queued request. Returns true if a queued job was removed
    /// (it will produce no Finished event).
    pub fn cancel(&self, request_id: u64) -> bool {
        let (queue, _) = &*self.shared;
        let mut q = lock(queue);
        if request_id > q.latest {
            q.cancelled.insert(request_id);
        }
        if let Some((id, cancel)) = &q.active {
            if *id == request_id {
                cancel.store(true, Ordering::Release);
            }
        }
        if q.pending
            .as_ref()
            .is_some_and(|j| j.request_id == request_id)
        {
            q.pending = None;
            return true;
        }
        false
    }
    /// Cancel everything and wait until the model and context are dropped.
    pub fn unload_and_wait(&self, timeout: Duration) -> bool {
        let (tx, rx) = mpsc::channel();
        {
            let (queue, signal) = &*self.shared;
            let mut q = lock(queue);
            q.pending = None;
            if let Some((_, cancel)) = &q.active {
                cancel.store(true, Ordering::Release);
            }
            q.unload_waiters.push(tx);
            signal.notify_all();
        }
        rx.recv_timeout(timeout).is_ok()
    }
    /// Request an unload without waiting (e.g. auto-rewrite switched off).
    pub fn unload_soon(&self) {
        let (tx, _rx) = mpsc::channel();
        let (queue, signal) = &*self.shared;
        let mut q = lock(queue);
        q.unload_waiters.push(tx);
        signal.notify_all();
    }
    pub fn shutdown(&self) {
        {
            let (queue, signal) = &*self.shared;
            let mut q = lock(queue);
            q.shutdown = true;
            q.pending = None;
            if let Some((_, cancel)) = &q.active {
                cancel.store(true, Ordering::Release);
            }
            signal.notify_all();
        }
        if let Some(thread) = lock(&self.thread).take() {
            let deadline = Instant::now() + Duration::from_secs(20);
            while !thread.is_finished() && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(10));
            }
            if thread.is_finished() {
                let _ = thread.join();
            }
        }
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        self.shutdown();
    }
}

pub fn effective_threads(configured: u32) -> u32 {
    if configured > 0 {
        return configured.min(16);
    }
    let cpus = thread::available_parallelism()
        .map(|n| n.get() as u32)
        .unwrap_or(2);
    cpus.saturating_sub(1).clamp(1, 4)
}
