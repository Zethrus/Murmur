//! Cross-module deterministic tests: worker + fake backend + session. No real GGUF, no network.
use super::{
    backend::fake::{Fake, Log, Script},
    catalog::Entry,
    output::Outcome,
    prompt::Preset,
    session::{AcceptDecision, DeliveryRecord, Gate, Phase, RewriteStatus, Session, Trigger},
    worker::{Job, Worker, WorkerEvent},
    ErrorCode,
};
use crate::{config::Config, inject::InjectionPath, postproc::Style};
use sha2::{Digest, Sha256};
use std::{
    collections::VecDeque,
    path::PathBuf,
    sync::{mpsc, Arc, Mutex},
    time::{Duration, Instant},
};

fn model_file(name: &str) -> (&'static Entry, PathBuf) {
    let body = b"GGUF-test".repeat(64);
    let dir = std::env::temp_dir().join(format!("murmur-enh-{name}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("model.gguf");
    std::fs::write(&path, &body).unwrap();
    let sha = super::download::hex(&Sha256::digest(&body));
    let entry: &'static Entry = Box::leak(Box::new(Entry {
        id: "test",
        display_name: "Test",
        repo: "x/y",
        revision: "r",
        filename: "m.gguf",
        size_bytes: body.len() as u64,
        sha256: Box::leak(sha.into_boxed_str()),
    }));
    (entry, path)
}

struct Harness {
    worker: Worker,
    events: mpsc::Receiver<WorkerEvent>,
    scripts: Arc<Mutex<VecDeque<Script>>>,
    log: Log,
    entry: &'static Entry,
    path: PathBuf,
}

fn harness(name: &str) -> Harness {
    let (tx, events) = mpsc::channel();
    let tx = Mutex::new(tx);
    let scripts = Arc::new(Mutex::new(VecDeque::new()));
    let log = Log::default();
    let worker = Worker::spawn(
        Box::new(Fake::new(scripts.clone(), log.clone())),
        Arc::new(move |e| {
            let _ = tx.lock().unwrap().send(e);
        }),
    )
    .unwrap();
    let (entry, path) = model_file(name);
    Harness {
        worker,
        events,
        scripts,
        log,
        entry,
        path,
    }
}

impl Harness {
    fn job(&self, request_id: u64, body: &str) -> Job {
        Job {
            request_id,
            dictation_id: 1,
            generation: 1,
            body: body.into(),
            preset: Preset::Concise,
            dictionary: Vec::new(),
            entry: self.entry,
            model_path: self.path.clone(),
            threads: 1,
            load_timeout: Duration::from_secs(5),
            timeout: Duration::from_secs(5),
            unload_after: Duration::from_secs(60),
        }
    }
    fn finished(&self) -> (u64, Result<Outcome, super::EnhanceError>) {
        loop {
            match self
                .events
                .recv_timeout(Duration::from_secs(5))
                .expect("worker event")
            {
                WorkerEvent::Finished {
                    request_id, result, ..
                } => return (request_id, result),
                _ => continue,
            }
        }
    }
}

#[test]
fn worker_success_and_lazy_load_once() {
    let h = harness("ok");
    h.worker.submit(h.job(1, "send the notes to alex"));
    let (id, result) = h.finished();
    assert_eq!(id, 1);
    assert_eq!(
        result.unwrap(),
        Outcome::Changed("Send the notes to alex.".into())
    );
    h.worker.submit(h.job(2, "hello there"));
    let _ = h.finished();
    assert_eq!(
        *h.log.loads.lock().unwrap(),
        1,
        "weights stay warm between requests"
    );
}

#[test]
fn worker_errors_timeout_panic_and_invalid_output() {
    let h = harness("err");
    h.scripts.lock().unwrap().extend([
        Script::Fail(ErrorCode::InferenceFailed),
        Script::Raw("Sure! here you go".into()),
        Script::Panic,
        Script::Delay(Duration::from_secs(3)),
    ]);
    h.worker.submit(h.job(1, "a b"));
    assert_eq!(h.finished().1.unwrap_err().code, ErrorCode::InferenceFailed);
    h.worker.submit(h.job(2, "a b"));
    assert_eq!(h.finished().1.unwrap_err().code, ErrorCode::InvalidOutput);
    h.worker.submit(h.job(3, "a b"));
    assert_eq!(h.finished().1.unwrap_err().code, ErrorCode::InferenceFailed);
    let mut job = h.job(4, "a b");
    job.timeout = Duration::from_millis(50);
    let start = Instant::now();
    h.worker.submit(job);
    assert_eq!(h.finished().1.unwrap_err().code, ErrorCode::Timeout);
    assert!(start.elapsed() < Duration::from_secs(2));
    // Still alive after a contained panic.
    h.worker.submit(h.job(5, "still working"));
    assert!(h.finished().1.is_ok());
}

#[test]
fn replacement_cancels_active_and_replaces_queued() {
    let h = harness("cancel");
    h.scripts
        .lock()
        .unwrap()
        .push_back(Script::Delay(Duration::from_secs(10)));
    h.worker.submit(h.job(1, "first"));
    // Wait until it is running.
    loop {
        if matches!(
            h.events.recv_timeout(Duration::from_secs(5)).unwrap(),
            WorkerEvent::Rewriting { request_id: 1 }
        ) {
            break;
        }
    }
    let start = Instant::now();
    h.worker.submit(h.job(2, "second"));
    let (id, result) = h.finished();
    assert_eq!((id, result.unwrap_err().code), (1, ErrorCode::Cancelled));
    assert!(
        start.elapsed() < Duration::from_millis(500),
        "cooperative cancellation is prompt"
    );
    assert_eq!(h.finished().0, 2);
}

#[test]
fn unload_waits_and_idle_unload() {
    let h = harness("unload");
    h.worker.submit(h.job(1, "x y"));
    let _ = h.finished();
    assert!(h.worker.unload_and_wait(Duration::from_secs(5)));
    assert_eq!(*h.log.unloads.lock().unwrap(), 1);
    let mut job = h.job(2, "x y");
    job.unload_after = Duration::from_millis(0);
    h.worker.submit(job);
    let _ = h.finished();
    let deadline = Instant::now() + Duration::from_secs(5);
    while *h.log.unloads.lock().unwrap() < 2 && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(
        *h.log.unloads.lock().unwrap(),
        2,
        "zero unload delay drops weights after the request"
    );
}

#[test]
fn missing_or_corrupt_model_never_loads() {
    let h = harness("missing");
    let mut job = h.job(1, "x");
    job.model_path = h.path.with_file_name("absent.gguf");
    h.worker.submit(job);
    assert_eq!(h.finished().1.unwrap_err().code, ErrorCode::ModelMissing);
    std::fs::write(&h.path, b"tampered").unwrap();
    h.worker.submit(h.job(2, "x"));
    assert_eq!(h.finished().1.unwrap_err().code, ErrorCode::Corrupt);
    assert_eq!(*h.log.loads.lock().unwrap(), 0);
}

#[test]
fn shutdown_joins_with_active_work() {
    let h = harness("shutdown");
    h.scripts
        .lock()
        .unwrap()
        .push_back(Script::Delay(Duration::from_secs(10)));
    h.worker.submit(h.job(1, "x"));
    std::thread::sleep(Duration::from_millis(50));
    let start = Instant::now();
    h.worker.shutdown();
    assert!(start.elapsed() < Duration::from_secs(2));
}

#[test]
fn unavailable_backend_reports_cleanly() {
    let (tx, rx) = mpsc::channel();
    let tx = Mutex::new(tx);
    let worker = Worker::spawn(
        Box::new(super::backend::Unavailable),
        Arc::new(move |e| {
            let _ = tx.lock().unwrap().send(e);
        }),
    )
    .unwrap();
    let (entry, path) = model_file("unavail");
    worker.submit(Job {
        request_id: 9,
        dictation_id: 1,
        generation: 1,
        body: "x".into(),
        preset: Preset::Concise,
        dictionary: vec![],
        entry,
        model_path: path,
        threads: 1,
        load_timeout: Duration::from_secs(1),
        timeout: Duration::from_secs(1),
        unload_after: Duration::from_secs(1),
    });
    match rx.recv_timeout(Duration::from_secs(5)).unwrap() {
        WorkerEvent::Finished { result, .. } => {
            assert_eq!(result.unwrap_err().code, ErrorCode::BackendUnavailable)
        }
        other => panic!("{other:?}"),
    }
}

// ---- Session ----

fn record(id: u64, body: &str, style: Style, path: InjectionPath) -> DeliveryRecord {
    DeliveryRecord {
        dictation_id: id,
        cleaned_body: body.into(),
        delivered_text: format!("{body} "),
        trailing_separator: " ".into(),
        style,
        focused: "firefox".into(),
        path,
        completed: Instant::now(),
        config: Config::default(),
    }
}
const OPEN: Gate = Gate {
    installed: true,
    backend: true,
    overlay: true,
    dictating: false,
};

fn ready(session: &mut Session, request: u64, suggestion: u64) {
    let now = Instant::now();
    assert!(session.check(Trigger::Auto, OPEN, now).is_ok());
    session.started(request, true, Trigger::Auto);
    let generation = session.generation;
    let dictation = session.record.as_ref().unwrap().dictation_id;
    assert!(session.finish(
        request,
        dictation,
        generation,
        &Ok(Outcome::Changed("Hello, world.".into())),
        suggestion,
        now
    ));
}

#[test]
fn session_ready_accept_is_idempotent_and_stale_ids_rejected() {
    let mut s = Session::default();
    s.begin_dictation();
    s.record_delivery(record(1, "hello world", Style::Prose, InjectionPath::Type));
    ready(&mut s, 10, 11);
    assert_eq!(s.phase(), Phase::Ready);
    assert_eq!(s.accept(99), AcceptDecision::Stale);
    assert_eq!(
        s.accept(11),
        AcceptDecision::Copy {
            text: "Hello, world. ".into(),
            again: false
        }
    );
    s.accepted(11, Ok(()), Instant::now());
    assert_eq!(s.phase(), Phase::Notice);
    assert_eq!(
        s.accept(11),
        AcceptDecision::Copy {
            text: "Hello, world. ".into(),
            again: true
        }
    );
    // Manual rewrite of the same dictation reveals instead of regenerating.
    assert_eq!(
        s.check(Trigger::Manual, OPEN, Instant::now()),
        Err(RewriteStatus::Revealed)
    );
    assert_eq!(s.phase(), Phase::Ready);
}

#[test]
fn pending_discard_only_cancels_the_matching_request() {
    let mut s = Session::default();
    s.begin_dictation();
    s.record_delivery(record(1, "hello world", Style::Prose, InjectionPath::Type));
    s.started(51, false, Trigger::Manual);
    assert_eq!(s.discard(Some(50)), None);
    assert_eq!(s.request, Some(51));
    assert_eq!(s.discard(Some(51)), Some(51));
    assert_eq!(s.request, None);
    assert_eq!(s.phase(), Phase::None);
}

#[test]
fn new_dictation_invalidates_results_and_suggestions() {
    let mut s = Session::default();
    s.begin_dictation();
    s.record_delivery(record(
        1,
        "hello world",
        Style::Prose,
        InjectionPath::Clipboard,
    ));
    assert!(s.check(Trigger::Auto, OPEN, Instant::now()).is_ok());
    s.started(5, false, Trigger::Auto);
    let old_generation = s.generation;
    assert_eq!(s.phase(), Phase::Loading);
    assert_eq!(s.begin_dictation(), Some(5), "running request is cancelled");
    assert!(!s.finish(
        5,
        1,
        old_generation,
        &Ok(Outcome::Changed("x".into())),
        6,
        Instant::now()
    ));
    assert!(s.suggestion.is_none());
    assert_eq!(s.phase(), Phase::None);
    assert_eq!(s.accept(6), AcceptDecision::Stale);
}

#[test]
fn gating_rules() {
    let now = Instant::now();
    let mut s = Session::default();
    assert_eq!(
        s.check(Trigger::Manual, OPEN, now),
        Err(RewriteStatus::NoDictation)
    );
    assert_eq!(s.message.as_deref(), Some("Dictate something first."));
    s.record_delivery(record(1, "fn main", Style::Code, InjectionPath::Clipboard));
    assert_eq!(
        s.check(Trigger::Manual, OPEN, now),
        Err(RewriteStatus::CodeStyle)
    );
    let mut opted = record(2, "fn main", Style::Code, InjectionPath::Clipboard);
    opted.config.enhance.rewrite_code = true;
    s.record_delivery(opted);
    assert!(s.check(Trigger::Manual, OPEN, now).is_ok());
    s.record_delivery(record(3, "hi there", Style::Prose, InjectionPath::Skip));
    assert_eq!(
        s.check(Trigger::Auto, OPEN, now),
        Err(RewriteStatus::NoDictation)
    );
    s.record_delivery(record(4, "hi there", Style::Prose, InjectionPath::Type));
    assert_eq!(
        s.check(
            Trigger::Manual,
            Gate {
                dictating: true,
                ..OPEN
            },
            now
        ),
        Err(RewriteStatus::Busy)
    );
    assert_eq!(
        s.check(
            Trigger::Manual,
            Gate {
                installed: false,
                ..OPEN
            },
            now
        ),
        Err(RewriteStatus::ModelMissing)
    );
    assert_eq!(
        s.check(
            Trigger::Manual,
            Gate {
                backend: false,
                ..OPEN
            },
            now
        ),
        Err(RewriteStatus::Unavailable)
    );
    assert_eq!(
        s.check(
            Trigger::Auto,
            Gate {
                overlay: false,
                ..OPEN
            },
            now
        ),
        Err(RewriteStatus::Unavailable)
    );
    s.started(7, true, Trigger::Manual);
    assert_eq!(
        s.check(Trigger::Manual, OPEN, now),
        Err(RewriteStatus::AlreadyRunning)
    );
}

#[test]
fn unchanged_errors_and_cancel_outcomes() {
    let now = Instant::now();
    let mut s = Session::default();
    s.record_delivery(record(1, "Hello there.", Style::Prose, InjectionPath::Type));
    s.check(Trigger::Auto, OPEN, now).unwrap();
    s.started(1, true, Trigger::Auto);
    let g = s.generation;
    assert!(s.finish(1, 1, g, &Ok(Outcome::Unchanged), 2, now));
    assert_eq!(
        (s.phase(), s.message.as_deref()),
        (Phase::Notice, Some("No changes suggested"))
    );
    assert!(s.suggestion.is_none());
    s.discard(None);
    s.check(Trigger::Manual, OPEN, now).unwrap();
    s.started(3, true, Trigger::Manual);
    assert!(s.finish(
        3,
        1,
        g,
        &Err(super::EnhanceError::new(
            ErrorCode::Timeout,
            "Rewrite timed out"
        )),
        4,
        now
    ));
    assert_eq!(s.message.as_deref(), Some("Rewrite timed out"));
    s.check(Trigger::Manual, OPEN, now).unwrap();
    s.started(5, true, Trigger::Manual);
    assert_eq!(
        s.discard(None),
        Some(5),
        "discarding a pending rewrite cancels it"
    );
    assert!(!s.finish(5, 1, g, &Ok(Outcome::Changed("x".into())), 6, now));
}

#[test]
fn expiry_clears_text_and_notices_time_out() {
    let mut s = Session::default();
    s.record_delivery(record(1, "hello world", Style::Prose, InjectionPath::Type));
    ready(&mut s, 2, 3);
    assert!(s.holds_overlay());
    assert!(s.tick(Instant::now() + super::session::SESSION_TTL + Duration::from_secs(1)));
    assert!(s.record.is_none() && s.suggestion.is_none());
    assert!(!s.holds_overlay());
    assert_eq!(
        s.check(Trigger::Manual, OPEN, Instant::now()),
        Err(RewriteStatus::Expired)
    );
    assert!(s.tick(Instant::now() + super::session::NOTICE_TTL + Duration::from_secs(1)));
    assert_eq!(s.phase(), Phase::None);
}

#[test]
fn accepted_text_is_delivery_formatted_not_recleaned() {
    let mut s = Session::default();
    let mut r = record(1, "um the plan", Style::Prose, InjectionPath::Type);
    r.config.injection.trailing_space = false;
    r.config.injection.remove_fillers = true;
    s.record_delivery(r);
    s.check(Trigger::Auto, OPEN, Instant::now()).unwrap();
    s.started(1, true, Trigger::Auto);
    let g = s.generation;
    s.finish(
        1,
        1,
        g,
        &Ok(Outcome::Changed("Um,   the plan\nworks".into())),
        2,
        Instant::now(),
    );
    // Fillers are not re-removed from model output; only whitespace/newline policy and separator.
    assert_eq!(
        s.accept(2),
        AcceptDecision::Copy {
            text: "Um, the plan works".into(),
            again: false
        }
    );
}

/// Real-model check, opt-in: MURMUR_ENHANCE_MODEL=/path/to/model.gguf
#[test]
#[ignore]
fn real_model_rewrite_quality() {
    use super::backend::{Backend, Budget};
    use super::llama::Llama;
    use super::{output, prompt, ErrorCode};
    use std::{
        path::Path,
        sync::{
            atomic::AtomicBool,
            Arc,
        },
        time::{Duration, Instant},
    };
    let Ok(path) = std::env::var("MURMUR_ENHANCE_MODEL") else {
        return;
    };
    let cases = [
        "So I want you to write a function that takes a list of numbers and returns the sum of the even ones please",
        "The bug is that the cache gets cleared every time we reload the page",
        "we need to add retry logic to the upload endpoint because it fails when the network drops and the user loses their draft",
    ];
    let mut backend = Llama::default();
    let budget = Budget {
        cancel: Arc::new(AtomicBool::new(false)),
        deadline: Instant::now() + Duration::from_secs(180),
        timeout_code: ErrorCode::Timeout,
    };
    backend.load(Path::new(&path), 8, &budget).unwrap();
    for body in cases {
        for preset in [prompt::Preset::Concise, prompt::Preset::Thorough] {
        let prompt = prompt::build(body, preset, &[]).unwrap();
        let started = Instant::now();
        let raw = backend.generate(&prompt, &budget).unwrap();
        let elapsed = started.elapsed();
        println!("\nIN : {body}\nRAW: {raw}\nMS : {:?}", elapsed.as_millis());
        match output::parse(&raw, &prompt, body) {
            Ok(output::Outcome::Changed(text)) => println!("OUT: {text}"),
            Ok(output::Outcome::Unchanged) => println!("OUT: <unchanged>"),
            Err(e) => println!("ERR: {e:?}"),
        }
        }
    }
}

