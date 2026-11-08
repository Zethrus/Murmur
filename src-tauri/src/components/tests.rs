//! Service-level tests: real files, a local HTTP server and throwaway signing keys.
use super::{
    fetch::{testserver, Https},
    manifest::tests::{component, manifest},
    trust::testkeys::Signer,
    *,
};
use serde_json::json;
use sha2::Digest;
use std::{net::TcpStream, sync::atomic::AtomicUsize, time::SystemTime};

const NOW: u64 = 1_794_000_000;

struct Fixture {
    data: PathBuf,
    signer: Signer,
    server: testserver::Server,
    content: Arc<Mutex<BTreeMap<String, Vec<u8>>>>,
    usage: Arc<Mutex<Usage>>,
    stops: Arc<AtomicUsize>,
    changes: Arc<Mutex<Vec<Kind>>>,
    free: u64,
}

fn now() -> u64 {
    NOW
}
/// After the test manifest's `expires_at` (2027-02-01).
fn after_expiry() -> u64 {
    1_802_000_000
}

fn sha(data: &[u8]) -> String {
    fetch::hex(&sha2::Sha256::digest(data))
}

impl Fixture {
    fn new(name: &str) -> Self {
        let data =
            std::env::temp_dir().join(format!("murmur-components-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&data);
        fs::create_dir_all(&data).unwrap();
        let content: Arc<Mutex<BTreeMap<String, Vec<u8>>>> = Arc::default();
        let served = content.clone();
        let server = testserver::start(Arc::new(
            move |r: &testserver::Request, s: &mut TcpStream| {
                let body = served
                    .lock()
                    .unwrap()
                    .get(r.path.trim_start_matches('/'))
                    .cloned();
                match body {
                    Some(body) => testserver::respond(s, &body, r.range, None),
                    None => {
                        let _ = s.write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n");
                    }
                }
            },
        ));
        Self {
            data,
            signer: Signer::new(SystemTime::now() - Duration::from_secs(400 * 86_400), 3650),
            server,
            content,
            usage: Arc::new(Mutex::new(Usage {
                speech_model: "none".into(),
                enhance_model: None,
            })),
            stops: Arc::default(),
            changes: Arc::default(),
            free: u64::MAX,
        }
    }
    fn url(&self, name: &str) -> String {
        format!("{}/{name}", self.server.base)
    }
    fn serve(&self, name: &str, body: &[u8]) {
        self.content
            .lock()
            .unwrap()
            .insert(name.into(), body.to_vec());
    }
    /// Signed manifest written as the cached copy, so `Components::new` trusts it offline.
    fn publish(&self, components: Vec<serde_json::Value>) {
        let bytes = serde_json::to_vec(&manifest(1, components)).unwrap();
        let signature = self.signer.sign_at(
            &bytes,
            trust::unix(NOW - 60),
            sequoia_openpgp::types::HashAlgorithm::SHA512,
        );
        let files = Files::new(&self.data.join("components"));
        write_atomic(&files.manifest, &bytes).unwrap();
        write_atomic(&files.signature, &signature).unwrap();
    }
    fn start(&self) -> Arc<Components> {
        self.start_at(now)
    }
    fn start_at(&self, now: fn() -> u64) -> Arc<Components> {
        let free = self.free;
        let usage = self.usage.clone();
        let stops = self.stops.clone();
        let changes = self.changes.clone();
        Components::new(
            Options {
                data: self.data.clone(),
                legacy_models: self.data.join("models"),
                legacy_enhance: self.data.join("enhance"),
                root: trust::root_from(&self.signer.public(), &self.signer.fingerprint(), true),
                embedded: None,
                transport: Arc::new(|| {
                    Https::new(Duration::from_secs(2)).map(|t| Box::new(t) as Box<dyn Transport>)
                }),
                space: Arc::new(move |_| Some(free)),
                retry: Retry {
                    retries: 1,
                    backoff: Duration::from_millis(10),
                },
                now,
            },
            Host {
                emit: Arc::new(|_, _| {}),
                stop_engine: Box::new(move || {
                    stops.fetch_add(1, Ordering::Relaxed);
                }),
                changed: Box::new(move |kind| changes.lock().unwrap().push(kind)),
                usage: Box::new(move || usage.lock().unwrap().clone()),
            },
        )
    }
    fn model(&self, id: &str, files: &[(&str, &[u8])]) -> serde_json::Value {
        let mut value = component(id, "speech-model");
        value["model"] = json!(id.trim_start_matches("speech-"));
        value["files"] = json!(files.iter().map(|(name, body)| {
            self.serve(&format!("{id}/{name}"), body);
            json!({"name": name, "size_bytes": body.len(), "sha256": sha(body), "urls": [self.url(&format!("{id}/{name}"))]})
        }).collect::<Vec<_>>());
        value["size_bytes"] = json!(files.iter().map(|(_, b)| b.len()).sum::<usize>());
        value
    }
    fn engine_zip(&self, script: &str) -> serde_json::Value {
        let zip = self.data.join("engine.zip");
        let mode = 0o755;
        archive::tests::build(
            &zip,
            &[archive::tests::Item::File(
                store::ENGINE_FILE,
                script.as_bytes(),
                mode,
            )],
        );
        let bytes = fs::read(&zip).unwrap();
        self.serve("engine.zip", &bytes);
        let mut value = component("engine", "engine");
        value["urls"] = json!([self.url("engine.zip")]);
        value["size_bytes"] = json!(bytes.len());
        value["sha256"] = json!(sha(&bytes));
        value["installed_size_bytes"] = json!(script.len());
        value
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.data);
    }
}

fn wait(components: &Components, id: &str) -> Value {
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let snapshot = components.snapshot();
        let entry = snapshot["components"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["id"] == id)
            .cloned()
            .unwrap();
        if !matches!(entry["state"].as_str(), Some("queued" | "installing")) {
            return entry;
        }
        assert!(Instant::now() < deadline, "job did not finish: {entry}");
        std::thread::sleep(Duration::from_millis(20));
    }
}

const MODEL_FILES: [(&str, &[u8]); 3] = [
    ("model.bin", b"weights"),
    ("config.json", b"{}"),
    ("tokenizer.json", b"{\"t\":1}"),
];

#[test]
fn installs_engine_and_model_end_to_end() {
    let f = Fixture::new("e2e");
    f.publish(vec![
        f.engine_zip("#!/bin/sh\n[ \"$1\" = --help ]\n"),
        f.model("speech-tiny.en", &MODEL_FILES),
    ]);
    let components = f.start();
    assert_eq!(components.snapshot()["trust"]["ok"], true);
    assert!(
        f.server.requests.lock().unwrap().is_empty(),
        "no network before an explicit install"
    );
    components
        .install(&["speech-tiny.en".into(), "engine".into()])
        .unwrap();
    assert_eq!(wait(&components, "engine")["state"], "active");
    assert_eq!(wait(&components, "speech-tiny.en")["state"], "installed");
    assert!(components
        .engine_dir()
        .unwrap()
        .join(store::ENGINE_FILE)
        .is_file());
    assert_eq!(
        fs::read(
            components
                .speech_model_dir("tiny.en")
                .unwrap()
                .join("model.bin")
        )
        .unwrap(),
        b"weights"
    );
    assert!(
        f.stops.load(Ordering::Relaxed) >= 1,
        "engine stopped before activation"
    );
    assert!(f.changes.lock().unwrap().contains(&Kind::Engine));
    // Selecting the model makes it active and "in use": removal is refused.
    f.usage.lock().unwrap().speech_model = "tiny.en".into();
    assert_eq!(wait(&components, "speech-tiny.en")["state"], "active");
    assert_eq!(
        components.remove("speech-tiny.en").unwrap_err().reason,
        Reason::InUse
    );
    // Verify detects a modified file; the model is then not handed to the engine.
    let dir = components.speech_model_dir("tiny.en").unwrap();
    fs::write(dir.join("model.bin"), b"tampered").unwrap();
    assert_eq!(
        components.verify("speech-tiny.en").unwrap(),
        Verdict::Mismatch
    );
    assert!(components.speech_model_dir("tiny.en").is_none());
    assert_eq!(wait(&components, "speech-tiny.en")["state"], "corrupt");
    f.usage.lock().unwrap().speech_model = "other".into();
    components.remove("speech-tiny.en").unwrap();
    assert!(!f.data.join("components/speech-tiny.en").exists());
    components.shutdown();
}

#[test]
fn removal_refused_when_depended_on() {
    let f = Fixture::new("depends");
    let mut cuda = component("cuda", "cuda");
    cuda["depends"] = json!(["engine"]);
    f.publish(vec![f.engine_zip("#!/bin/sh\nexit 0\n"), cuda]);
    let components = f.start();
    components.install(&["engine".into()]).unwrap();
    assert_eq!(wait(&components, "engine")["state"], "active");
    // Pretend the GPU add-on is installed: the engine cannot be removed under it.
    fs::create_dir_all(f.data.join("components/cuda/1.0.0")).unwrap();
    components
        .store
        .write_active(
            "cuda",
            &store::Active {
                version: "1.0.0".into(),
                kind: Kind::Cuda,
                model: None,
                location: None,
                migrated: false,
                installed_at: 0,
            },
        )
        .unwrap();
    let error = components.remove("engine").unwrap_err();
    assert_eq!(error.reason, Reason::InUse);
    assert!(error.message.contains("cuda"), "{error:?}");
    components.remove("cuda").unwrap();
    components.remove("engine").unwrap();
    assert!(components.engine_dir().is_none());
    // Never touches anything outside <data>/components.
    fs::write(f.data.join("config.toml"), b"keep").unwrap();
    assert!(f.data.join("config.toml").exists());
    components.shutdown();
}

#[test]
fn failed_smoke_test_keeps_previous_engine() {
    let f = Fixture::new("smoke");
    f.publish(vec![f.engine_zip("#!/bin/sh\nexit 0\n")]);
    let components = f.start();
    components.install(&["engine".into()]).unwrap();
    assert_eq!(wait(&components, "engine")["state"], "active");
    components.shutdown();
    // Version 2 of the engine does not start: activation is refused, version 1 stays.
    let mut broken = f.engine_zip("#!/bin/sh\nexit 1\n");
    broken["version"] = json!("2.0.0");
    let bytes = serde_json::to_vec(&manifest(2, vec![broken])).unwrap();
    let files = Files::new(&f.data.join("components"));
    write_atomic(&files.manifest, &bytes).unwrap();
    write_atomic(
        &files.signature,
        &f.signer.sign_at(
            &bytes,
            trust::unix(NOW - 30),
            sequoia_openpgp::types::HashAlgorithm::SHA512,
        ),
    )
    .unwrap();
    let components = f.start();
    components.install(&["engine".into()]).unwrap();
    let entry = wait(&components, "engine");
    assert_eq!(entry["state"], "failed");
    assert_eq!(entry["job"]["reason"], "smoke_test_failed");
    assert_eq!(entry["installed_version"], "1.0.0");
    assert!(components.engine_dir().unwrap().ends_with("engine/1.0.0"));
    assert!(!f.data.join("components/engine/.staging-2.0.0").exists());
    components.shutdown();
}

#[test]
fn interrupted_job_persists_as_paused_and_never_auto_resumes() {
    let f = Fixture::new("paused");
    f.publish(vec![f.model("speech-base.en", &MODEL_FILES)]);
    // A job that was downloading when Murmur stopped.
    let record = Record {
        id: "speech-base.en".into(),
        version: "1.0.0".into(),
        phase: Phase::Downloading,
        bytes_done: 3,
        bytes_total: 17,
        error: None,
        reason: None,
        replace: true,
    };
    jobs::save(
        &f.data.join("components/jobs.json"),
        &BTreeMap::from([(record.id.clone(), record)]),
    )
    .unwrap();
    // A tampered record whose version would escape the downloads folder is dropped on load.
    let mut records = jobs::load(&f.data.join("components/jobs.json"));
    records.insert(
        "speech-tiny.en".into(),
        Record {
            id: "speech-tiny.en".into(),
            version: "../../../outside".into(),
            phase: Phase::Paused,
            bytes_done: 0,
            bytes_total: 1,
            error: None,
            reason: None,
            replace: true,
        },
    );
    jobs::save(&f.data.join("components/jobs.json"), &records).unwrap();
    assert_eq!(jobs::load(&f.data.join("components/jobs.json")).len(), 1);
    let components = f.start();
    std::thread::sleep(Duration::from_millis(200));
    let entry = wait(&components, "speech-base.en");
    assert_eq!(entry["state"], "paused");
    assert_eq!(entry["job"]["phase"], "paused");
    assert!(
        f.server.requests.lock().unwrap().is_empty(),
        "paused jobs never resume on their own"
    );
    // Resume continues it; Discard on a paused job removes record and partial files.
    components.resume("speech-base.en").unwrap();
    assert_eq!(wait(&components, "speech-base.en")["state"], "installed");
    assert!(jobs::load(&f.data.join("components/jobs.json")).is_empty());
    components.shutdown();
}

#[test]
fn pause_keeps_partial_download_and_discard_removes_it() {
    let f = Fixture::new("pause");
    let big: Vec<u8> = (0..3_000_000u32).map(|i| (i % 251) as u8).collect();
    let files: [(&str, &[u8]); 3] = [
        ("model.bin", &big),
        ("config.json", b"{}"),
        ("tokenizer.json", b"{}"),
    ];
    f.publish(vec![f.model("speech-small.en", &files)]);
    // Slow server so the pause lands mid-body.
    let served = f.content.clone();
    let slow = testserver::start(Arc::new(
        move |r: &testserver::Request, s: &mut TcpStream| {
            let body = served
                .lock()
                .unwrap()
                .get(r.path.trim_start_matches('/'))
                .cloned()
                .unwrap_or_default();
            let start = r.range.unwrap_or(0) as usize;
            let _ = write!(
                s,
                "HTTP/1.1 {}\r\nContent-Length: {}\r\n{}Connection: close\r\n\r\n",
                if r.range.is_some() {
                    "206 Partial Content"
                } else {
                    "200 OK"
                },
                body.len() - start,
                if r.range.is_some() {
                    format!(
                        "Content-Range: bytes {start}-{}/{}\r\n",
                        body.len() - 1,
                        body.len()
                    )
                } else {
                    String::new()
                }
            );
            for chunk in body[start..].chunks(64 * 1024) {
                if s.write_all(chunk).is_err() {
                    return;
                }
                std::thread::sleep(Duration::from_millis(15));
            }
        },
    ));
    let mut value = f.model("speech-small.en", &files);
    value["files"][0]["urls"] = json!([format!("{}/speech-small.en/model.bin", slow.base)]);
    f.publish(vec![value]);
    let components = f.start();
    components.install(&["speech-small.en".into()]).unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while components.snapshot()["components"][0]["job"]["bytes_done"]
        .as_u64()
        .unwrap_or(0)
        < 200_000
    {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    components.stop("speech-small.en", true).unwrap();
    let entry = wait(&components, "speech-small.en");
    assert_eq!(entry["state"], "paused");
    let part = f
        .data
        .join("components/downloads/speech-small.en-1.0.0/model.bin.part");
    let kept = fs::metadata(&part).unwrap().len();
    assert!(kept > 0 && kept < big.len() as u64);
    // Resume uses Range from the kept bytes.
    components.resume("speech-small.en").unwrap();
    assert_eq!(wait(&components, "speech-small.en")["state"], "installed");
    assert!(
        slow.requests
            .lock()
            .unwrap()
            .iter()
            .any(|r| r.1 == Some(kept)),
        "{:?}",
        slow.requests.lock().unwrap()
    );
    components.shutdown();
    // Discard: a failed job's partial files are deleted.
    let f2 = Fixture::new("discard");
    let mut value = f2.model("speech-tiny.en", &MODEL_FILES);
    value["files"][0]["sha256"] = json!("0".repeat(64));
    f2.publish(vec![value]);
    let components = f2.start();
    components.install(&["speech-tiny.en".into()]).unwrap();
    let entry = wait(&components, "speech-tiny.en");
    assert_eq!(
        (entry["state"].as_str(), entry["job"]["reason"].as_str()),
        (Some("failed"), Some("checksum_mismatch"))
    );
    components.discard("speech-tiny.en").unwrap();
    assert_eq!(wait(&components, "speech-tiny.en")["state"], "available");
    assert!(!f2
        .data
        .join("components/downloads/speech-tiny.en-1.0.0")
        .exists());
    components.shutdown();
}

#[test]
fn insufficient_disk_space_names_the_amounts() {
    let mut f = Fixture::new("disk");
    f.free = 50 * 1024 * 1024;
    f.publish(vec![f.model("speech-tiny.en", &MODEL_FILES)]);
    let components = f.start();
    components.install(&["speech-tiny.en".into()]).unwrap();
    let entry = wait(&components, "speech-tiny.en");
    assert_eq!(entry["job"]["reason"], "insufficient_space");
    let message = entry["job"]["error"].as_str().unwrap();
    assert!(
        message.contains("needs 104.9 MB free")
            && message.contains("52.4 MB available")
            && message.contains("100 MB margin"),
        "{message}"
    );
    assert!(
        f.server.requests.lock().unwrap().is_empty(),
        "no download starts without space"
    );
    components.shutdown();
}

#[test]
fn migration_adopts_matching_models_and_flags_mismatches() {
    let f = Fixture::new("migrate");
    f.publish(vec![
        f.model("speech-tiny.en", &MODEL_FILES),
        f.model("speech-base.en", &MODEL_FILES),
    ]);
    // Pre-0.5 layout: <data>/models/<name>/ with the engine's .complete marker.
    let good = f.data.join("models/tiny.en");
    fs::create_dir_all(&good).unwrap();
    for (name, body) in MODEL_FILES {
        fs::write(good.join(name), body).unwrap();
    }
    fs::write(good.join(".complete"), b"1\n").unwrap();
    let bad = f.data.join("models/base.en");
    fs::create_dir_all(&bad).unwrap();
    for (name, body) in MODEL_FILES {
        fs::write(
            bad.join(name),
            if name == "model.bin" {
                b"other weights".as_slice()
            } else {
                body
            },
        )
        .unwrap();
    }
    let components = f.start();
    components.migrate();
    assert!(
        f.server.requests.lock().unwrap().is_empty(),
        "migration never downloads"
    );
    let tiny = wait(&components, "speech-tiny.en");
    assert_eq!(
        (tiny["state"].as_str(), tiny["migrated"].as_bool()),
        (Some("installed"), Some(true))
    );
    assert_eq!(
        fs::read(
            components
                .speech_model_dir("tiny.en")
                .unwrap()
                .join("model.bin")
        )
        .unwrap(),
        b"weights"
    );
    assert!(
        !good.exists(),
        "verified files moved into the store, not copied"
    );
    let base = wait(&components, "speech-base.en");
    assert_eq!(base["state"], "unverified");
    assert!(
        components.speech_model_dir("base.en").is_none(),
        "unverified models are never loaded"
    );
    assert!(
        bad.join("model.bin").exists(),
        "nothing is deleted automatically"
    );
    // Reinstall downloads a verified copy; the unverified folder is still left alone.
    components.install(&["speech-base.en".into()]).unwrap();
    assert_eq!(wait(&components, "speech-base.en")["state"], "installed");
    assert!(bad.join("model.bin").exists());
    components.shutdown();
}

#[test]
fn missing_trust_root_refuses_installs_but_runs() {
    let f = Fixture::new("noroot");
    let components = Components::new(
        Options {
            data: f.data.clone(),
            legacy_models: f.data.join("models"),
            legacy_enhance: f.data.join("enhance"),
            root: Err(ComponentError::new(
                Reason::TrustRootMissing,
                "Release trust root missing",
            )),
            embedded: None,
            transport: Arc::new(|| {
                Https::new(Duration::from_secs(1)).map(|t| Box::new(t) as Box<dyn Transport>)
            }),
            space: Arc::new(|_| None),
            retry: Retry::default(),
            now,
        },
        Host {
            emit: Arc::new(|_, _| {}),
            stop_engine: Box::new(|| {}),
            changed: Box::new(|_| {}),
            usage: Box::new(Usage::default),
        },
    );
    let snapshot = components.snapshot();
    assert_eq!(snapshot["trust"]["ok"], false);
    assert_eq!(snapshot["trust"]["message"], "Release trust root missing");
    assert_eq!(
        components.install(&["engine".into()]).unwrap_err().reason,
        Reason::TrustRootMissing
    );
    assert_eq!(
        components.check_updates().unwrap_err().reason,
        Reason::TrustRootMissing
    );
    components.shutdown();
}

/// A directory where `active.json` belongs makes the final switch fail after a passing smoke
/// test (the rename onto it fails), without relying on file permissions.
fn block_activation(f: &Fixture, id: &str) -> PathBuf {
    let blocker = f.data.join("components").join(id).join("active.json");
    fs::create_dir_all(&blocker).unwrap();
    blocker
}

#[test]
fn failed_activation_restarts_the_stopped_engine() {
    let f = Fixture::new("restart");
    f.publish(vec![f.engine_zip("#!/bin/sh\nexit 0\n")]);
    let components = f.start();
    let blocker = block_activation(&f, "engine");
    components.install(&["engine".into()]).unwrap();
    let entry = wait(&components, "engine");
    assert_eq!(entry["state"], "failed", "{entry}");
    assert!(
        f.stops.load(Ordering::Relaxed) >= 1,
        "smoke test passed, engine stopped"
    );
    assert!(
        f.changes.lock().unwrap().contains(&Kind::Engine),
        "engine restart requested after the failed switch"
    );
    assert!(components.engine_dir().is_none());
    fs::remove_dir_all(blocker).unwrap();
    components.shutdown();
}

#[test]
fn failed_activation_keeps_verified_downloads_for_retry() {
    let f = Fixture::new("keep-downloads");
    f.publish(vec![f.model("speech-tiny.en", &MODEL_FILES)]);
    let components = f.start();
    let blocker = block_activation(&f, "speech-tiny.en");
    components.install(&["speech-tiny.en".into()]).unwrap();
    assert_eq!(wait(&components, "speech-tiny.en")["state"], "failed");
    let fetched = f.server.requests.lock().unwrap().len();
    assert_eq!(fetched, MODEL_FILES.len());
    let kept = f.data.join("components/downloads/speech-tiny.en-1.0.0");
    for (name, body) in MODEL_FILES {
        assert_eq!(fs::read(kept.join(name)).unwrap(), body, "{name} kept");
    }
    // Retry after the cause is fixed: hashed again, no network fetch.
    fs::remove_dir_all(blocker).unwrap();
    components.resume("speech-tiny.en").unwrap();
    assert_eq!(wait(&components, "speech-tiny.en")["state"], "installed");
    assert_eq!(f.server.requests.lock().unwrap().len(), fetched);
    assert!(!kept.exists(), "downloads are cleaned up after activation");
    // A file changed while it waited (same size, so only the hash can tell) is not reused.
    components.remove("speech-tiny.en").unwrap();
    let blocker = block_activation(&f, "speech-tiny.en");
    components.install(&["speech-tiny.en".into()]).unwrap();
    assert_eq!(wait(&components, "speech-tiny.en")["state"], "failed");
    fs::write(kept.join("model.bin"), b"WEIGHTS").unwrap();
    fs::remove_dir_all(blocker).unwrap();
    components.resume("speech-tiny.en").unwrap();
    let entry = wait(&components, "speech-tiny.en");
    assert_eq!(entry["job"]["reason"], "checksum_mismatch", "{entry}");
    components.shutdown();
}

#[test]
fn unsafe_ids_never_reach_store_paths() {
    let f = Fixture::new("ids");
    let mut cuda = component("cuda", "cuda");
    cuda["depends"] = json!(["engine"]);
    f.publish(vec![f.engine_zip("#!/bin/sh\nexit 0\n"), cuda]);
    let components = f.start();
    components.install(&["engine".into()]).unwrap();
    assert_eq!(wait(&components, "engine")["state"], "active");
    fs::create_dir_all(f.data.join("components/cuda/1.0.0")).unwrap();
    components
        .store
        .write_active(
            "cuda",
            &store::Active {
                version: "1.0.0".into(),
                kind: Kind::Cuda,
                model: None,
                location: None,
                migrated: false,
                installed_at: 0,
            },
        )
        .unwrap();
    // `<root>/engine/1.0.0/..` would be the engine's own folder: bypassing the dependency
    // check and deleting the engine under the CUDA add-on.
    let id = "engine/1.0.0/..";
    assert_eq!(components.remove(id).unwrap_err().reason, Reason::NotFound);
    assert!(components.verify(id).is_err());
    assert!(components.stop(id, false).is_err());
    assert!(components.resume(id).is_err());
    assert!(components.discard(id).is_err());
    assert!(components.install(&[id.into()]).is_err());
    assert!(components.store.component_dir(id).is_err());
    assert!(components.store.remove(id).is_err());
    assert!(components.engine_dir().is_some(), "engine untouched");
    assert_eq!(
        f.stops.load(Ordering::Relaxed),
        1,
        "only the install stopped it"
    );
    components.shutdown();
}

#[test]
fn expired_manifest_keeps_a_stale_catalog() {
    let f = Fixture::new("stale");
    let mut cuda = component("cuda", "cuda");
    cuda["depends"] = json!(["engine"]);
    cuda["display_name"] = json!("GPU add-on");
    f.publish(vec![
        f.engine_zip("#!/bin/sh\nexit 0\n"),
        cuda,
        f.model("speech-tiny.en", &MODEL_FILES),
    ]);
    let components = f.start();
    assert_eq!(components.snapshot()["stale"], false);
    components.install(&["engine".into()]).unwrap();
    assert_eq!(wait(&components, "engine")["state"], "active");
    components.shutdown();
    // A paused job from before, then the manifest expires while Murmur is offline.
    let record = Record {
        id: "speech-tiny.en".into(),
        version: "1.0.0".into(),
        phase: Phase::Paused,
        bytes_done: 0,
        bytes_total: 17,
        error: None,
        reason: None,
        replace: true,
    };
    jobs::save(
        &f.data.join("components/jobs.json"),
        &BTreeMap::from([(record.id.clone(), record)]),
    )
    .unwrap();
    fs::create_dir_all(f.data.join("components/cuda/1.0.0")).unwrap();
    let components = f.start_at(after_expiry);
    let snapshot = components.snapshot();
    assert_eq!(snapshot["stale"], true);
    assert_eq!(snapshot["manifest"]["stale"], true);
    assert_eq!(snapshot["trust"]["ok"], true);
    let listed: Vec<&str> = snapshot["components"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["id"].as_str().unwrap())
        .collect();
    assert_eq!(listed, ["engine", "cuda", "speech-tiny.en"], "still listed");
    let requests = f.server.requests.lock().unwrap().len();
    // Nothing new is fetched or activated ...
    assert_eq!(
        components
            .install(&["speech-tiny.en".into()])
            .unwrap_err()
            .reason,
        Reason::Expired
    );
    assert_eq!(
        components.resume("speech-tiny.en").unwrap_err().reason,
        Reason::Expired
    );
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(f.server.requests.lock().unwrap().len(), requests);
    // ... while verify, the manifest's dependency rules and remove keep working.
    assert_eq!(components.verify("engine").unwrap(), Verdict::Verified);
    components
        .store
        .write_active(
            "cuda",
            &store::Active {
                version: "1.0.0".into(),
                kind: Kind::Cuda,
                model: None,
                location: None,
                migrated: false,
                installed_at: 0,
            },
        )
        .unwrap();
    let error = components.remove("engine").unwrap_err();
    assert!(error.message.contains("GPU add-on"), "{error:?}");
    components.remove("cuda").unwrap();
    components.discard("speech-tiny.en").unwrap();
    components.remove("engine").unwrap();
    assert!(components.engine_dir().is_none());
    components.shutdown();
}

#[cfg(unix)]
#[test]
fn failed_removal_restarts_the_stopped_engine() {
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new("remove-restart");
    f.publish(vec![f.engine_zip("#!/bin/sh\nexit 0\n")]);
    let components = f.start();
    components.install(&["engine".into()]).unwrap();
    assert_eq!(wait(&components, "engine")["state"], "active");
    f.changes.lock().unwrap().clear();
    // A read-only store root: the engine folder cannot be deleted.
    let root = f.data.join("components");
    fs::set_permissions(&root, fs::Permissions::from_mode(0o555)).unwrap();
    let result = components.remove("engine");
    fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();
    if result.is_ok() {
        // Running as root: permissions are not enforced, nothing to observe.
        components.shutdown();
        return;
    }
    assert_eq!(result.unwrap_err().reason, Reason::Io);
    assert_eq!(
        f.stops.load(Ordering::Relaxed),
        2,
        "install and removal stopped it"
    );
    assert_eq!(
        f.changes.lock().unwrap().as_slice(),
        [Kind::Engine],
        "restart requested after the failed removal"
    );
    components.shutdown();
}
