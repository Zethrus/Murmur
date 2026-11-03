//! Explicit, user-initiated model download. Never called on startup, config load or rewrite.
//! Streams into `model.gguf.part`, hashing while writing; requires the exact catalog length and
//! digest; atomically promotes; writes `installed.json` last. No transcript access.
use super::{
    catalog::{Entry, InstalledRecord},
    EnhanceError, ErrorCode,
};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

/// Free space kept beyond the artifact itself.
pub const SPACE_RESERVE: u64 = 256 * 1024 * 1024;
const PROGRESS_INTERVAL: Duration = Duration::from_millis(100);

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Starting,
    Downloading,
    Verifying,
}

pub trait Transport: Send + Sync {
    /// Open the immutable URL; returns (content length if known, body reader).
    fn open(
        &self,
        url: &str,
        cancel: &std::sync::Arc<AtomicBool>,
    ) -> Result<(Option<u64>, Box<dyn Read + Send>), EnhanceError>;
}

/// Hosts the model download may reach: the catalog host and Hugging Face's download CDNs.
pub fn allowed_host(host: &str) -> bool {
    matches!(
        host.to_ascii_lowercase().as_str(),
        "huggingface.co"
            | "cdn-lfs.huggingface.co"
            | "cdn-lfs-us-1.hf.co"
            | "cas-bridge.xethub.hf.co"
            | "cas-server.xethub.hf.co"
    )
}

pub fn allowed_url(url: &reqwest::Url) -> bool {
    #[cfg(test)]
    if url.scheme() == "http" && url.host_str() == Some("127.0.0.1") {
        return true;
    }
    url.scheme() == "https"
        && url.username().is_empty()
        && url.password().is_none()
        && url.host_str().is_some_and(allowed_host)
        && url.port().is_none_or(|p| p == 443)
}

pub struct Https {
    client: reqwest::Client,
}
impl Https {
    pub fn new() -> Result<Self, EnhanceError> {
        let policy = reqwest::redirect::Policy::custom(|attempt| {
            if attempt.previous().len() >= 5 {
                attempt.error("too many redirects")
            } else if allowed_url(attempt.url()) {
                attempt.follow()
            } else {
                attempt.error("redirect to a disallowed host")
            }
        });
        let client = reqwest::Client::builder()
            .redirect(policy)
            .https_only(true)
            .connect_timeout(Duration::from_secs(20))
            .user_agent(concat!("Murmur/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|_| {
                EnhanceError::new(ErrorCode::Network, "Could not start a secure connection")
            })?;
        Ok(Self { client })
    }
}

struct StreamReader {
    chunks: mpsc::Receiver<std::io::Result<Vec<u8>>>,
    current: std::io::Cursor<Vec<u8>>,
    cancel: Arc<AtomicBool>,
    stopped: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl Read for StreamReader {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        loop {
            if self.cancel.load(Ordering::Acquire) {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::Interrupted,
                    "cancelled",
                ));
            }
            if (self.current.position() as usize) < self.current.get_ref().len() {
                return self.current.read(buf);
            }
            match self.chunks.recv_timeout(Duration::from_millis(100)) {
                Ok(Ok(chunk)) => self.current = std::io::Cursor::new(chunk),
                Ok(Err(error)) => return Err(error),
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    // A closed channel is end-of-stream when the download finished and a
                    // cancellation when the flag is set; never report a cancel as EOF.
                    if self.cancel.load(Ordering::Acquire) {
                        return Err(std::io::Error::new(
                            std::io::ErrorKind::Interrupted,
                            "cancelled",
                        ));
                    }
                    return Ok(0);
                }
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
            }
        }
    }
}
impl Drop for StreamReader {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::Release);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

impl Transport for Https {
    fn open(
        &self,
        url: &str,
        cancel: &Arc<AtomicBool>,
    ) -> Result<(Option<u64>, Box<dyn Read + Send>), EnhanceError> {
        let parsed = reqwest::Url::parse(url)
            .map_err(|_| EnhanceError::new(ErrorCode::Network, "Invalid model URL"))?;
        if !allowed_url(&parsed) {
            return Err(EnhanceError::new(
                ErrorCode::Network,
                "Model URL is not allowed",
            ));
        }
        let client = self.client.clone();
        let flag = cancel.clone();
        let stopped = Arc::new(AtomicBool::new(false));
        let stop = stopped.clone();
        let (headers_tx, headers_rx) = mpsc::channel();
        let (chunks_tx, chunks_rx) = mpsc::sync_channel(2);
        let thread = std::thread::Builder::new().name("murmur-enhance-http".into()).spawn(move || {
            let Ok(runtime) = tokio::runtime::Builder::new_current_thread().enable_all().build() else {
                let _ = headers_tx.send(Err(EnhanceError::new(ErrorCode::Network, "Could not start download")));
                return;
            };
            runtime.block_on(async move {
                let request = client.get(parsed).send();
                tokio::pin!(request);
                let mut response = loop {
                    tokio::select! {
                        answer = &mut request => match answer {
                            Ok(response) => break response,
                            Err(_) => { let _ = headers_tx.send(Err(EnhanceError::new(ErrorCode::Network, "Could not reach the model server"))); return; }
                        },
                        _ = tokio::time::sleep(Duration::from_millis(100)) => {
                            if flag.load(Ordering::Acquire) || stop.load(Ordering::Acquire) { return; }
                        }
                    }
                };
                if !response.status().is_success() || !allowed_url(response.url()) {
                    let _ = headers_tx.send(Err(EnhanceError::new(ErrorCode::Network, "Model server rejected the download")));
                    return;
                }
                if headers_tx.send(Ok(response.content_length())).is_err() { return; }
                loop {
                    let chunk = tokio::select! {
                        chunk = response.chunk() => chunk,
                        _ = tokio::time::sleep(Duration::from_millis(100)) => {
                            if flag.load(Ordering::Acquire) || stop.load(Ordering::Acquire) { return; }
                            continue;
                        }
                    };
                    let value = match chunk {
                        Ok(Some(bytes)) => Ok(bytes.to_vec()),
                        Ok(None) => return,
                        Err(_) => Err(std::io::Error::other("download interrupted")),
                    };
                    let failed = value.is_err();
                    let mut pending = value;
                    loop {
                        match chunks_tx.try_send(pending) {
                            Ok(()) => break,
                            Err(mpsc::TrySendError::Disconnected(_)) => return,
                            Err(mpsc::TrySendError::Full(value)) => {
                                pending = value;
                                if flag.load(Ordering::Acquire) || stop.load(Ordering::Acquire) { return; }
                                tokio::time::sleep(Duration::from_millis(50)).await;
                            }
                        }
                    }
                    if failed { return; }
                }
            });
        }).map_err(|_| EnhanceError::new(ErrorCode::Network, "Could not start download"))?;
        let headers = loop {
            if cancel.load(Ordering::Acquire) {
                stopped.store(true, Ordering::Release);
                let _ = thread.join();
                return Err(EnhanceError::new(
                    ErrorCode::Cancelled,
                    "Download cancelled",
                ));
            }
            match headers_rx.recv_timeout(Duration::from_millis(100)) {
                Ok(value) => break value,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(_) => {
                    break Err(EnhanceError::new(
                        // The worker closes the channel when it stops; that is a
                        // cancellation when the flag is set, not a server failure.
                        if cancel.load(Ordering::Acquire) {
                            ErrorCode::Cancelled
                        } else {
                            ErrorCode::Network
                        },
                        if cancel.load(Ordering::Acquire) {
                            "Download cancelled"
                        } else {
                            "Could not reach the model server"
                        },
                    ));
                }
            }
        };
        match headers {
            Ok(length) => Ok((
                length,
                Box::new(StreamReader {
                    chunks: chunks_rx,
                    current: std::io::Cursor::new(Vec::new()),
                    cancel: cancel.clone(),
                    stopped,
                    thread: Some(thread),
                }),
            )),
            Err(error) => {
                stopped.store(true, Ordering::Release);
                let _ = thread.join();
                Err(error)
            }
        }
    }
}

fn io_error(message: &str) -> EnhanceError {
    EnhanceError::new(ErrorCode::DownloadFailed, message)
}

/// Install `entry` under `root`. `progress(phase, received, total)` is rate limited.
pub fn install(
    entry: &Entry,
    root: &Path,
    transport: &dyn Transport,
    cancel: &Arc<AtomicBool>,
    available_space: Option<u64>,
    progress: &mut dyn FnMut(Phase, u64, u64),
) -> Result<(), EnhanceError> {
    let total = entry.size_bytes;
    progress(Phase::Starting, 0, total);
    if let Some(free) = available_space {
        if free < total.saturating_add(SPACE_RESERVE) {
            return Err(EnhanceError::new(
                ErrorCode::InsufficientSpace,
                "Not enough free disk space for the Enhance model",
            ));
        }
    }
    let dir = entry.dir(root);
    fs::create_dir_all(&dir).map_err(|_| io_error("Could not create the model folder"))?;
    let part = dir.join("model.gguf.part");
    let result = (|| {
        let (length, mut body) = transport.open(&entry.url(), cancel)?;
        if length.is_some_and(|n| n != total) {
            return Err(EnhanceError::new(
                ErrorCode::SizeMismatch,
                "Model server reported an unexpected size",
            ));
        }
        let mut file =
            fs::File::create(&part).map_err(|_| io_error("Could not create the download file"))?;
        let mut hasher = Sha256::new();
        let mut received: u64 = 0;
        let mut buffer = vec![0u8; 1 << 20];
        let mut last = Instant::now() - PROGRESS_INTERVAL;
        loop {
            if cancel.load(Ordering::Acquire) {
                return Err(EnhanceError::new(
                    ErrorCode::Cancelled,
                    "Download cancelled",
                ));
            }
            let n = body.read(&mut buffer).map_err(|_| {
                if cancel.load(Ordering::Acquire) {
                    EnhanceError::new(ErrorCode::Cancelled, "Download cancelled")
                } else {
                    EnhanceError::new(ErrorCode::Network, "The download was interrupted")
                }
            })?;
            if n == 0 {
                break;
            }
            received += n as u64;
            if received > total {
                return Err(EnhanceError::new(
                    ErrorCode::SizeMismatch,
                    "Model download was larger than expected",
                ));
            }
            hasher.update(&buffer[..n]);
            file.write_all(&buffer[..n])
                .map_err(|_| io_error("Could not write the model (disk full?)"))?;
            if last.elapsed() >= PROGRESS_INTERVAL {
                last = Instant::now();
                progress(Phase::Downloading, received, total);
            }
        }
        progress(Phase::Downloading, received, total);
        progress(Phase::Verifying, received, total);
        if received != total {
            return Err(EnhanceError::new(
                ErrorCode::SizeMismatch,
                "Model download was incomplete",
            ));
        }
        if !hex(&hasher.finalize()).eq_ignore_ascii_case(entry.sha256) {
            return Err(EnhanceError::new(
                ErrorCode::ChecksumMismatch,
                "Model failed checksum verification",
            ));
        }
        file.flush()
            .map_err(|_| io_error("Could not flush the model"))?;
        file.sync_all()
            .map_err(|_| io_error("Could not sync the model"))?;
        drop(file);
        if cancel.load(Ordering::Acquire) {
            return Err(EnhanceError::new(
                ErrorCode::Cancelled,
                "Download cancelled",
            ));
        }
        fs::rename(&part, dir.join("model.gguf"))
            .map_err(|_| io_error("Could not install the model"))?;
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let record = serde_json::to_vec_pretty(&InstalledRecord::for_entry(entry, now))
            .map_err(|_| io_error("Could not record the installation"))?;
        let tmp = dir.join("installed.json.tmp");
        let mut out =
            fs::File::create(&tmp).map_err(|_| io_error("Could not record the installation"))?;
        out.write_all(&record)
            .and_then(|_| out.sync_all())
            .map_err(|_| io_error("Could not record the installation"))?;
        drop(out);
        fs::rename(&tmp, dir.join("installed.json"))
            .map_err(|_| io_error("Could not record the installation"))?;
        #[cfg(unix)]
        if let Ok(handle) = fs::File::open(&dir) {
            let _ = handle.sync_all();
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&part);
    }
    result
}

/// Remove only the catalog-owned directory for this model ID.
pub fn remove(entry: &Entry, root: &Path) -> Result<(), EnhanceError> {
    let dir = root.join(entry.id);
    match fs::remove_dir_all(&dir) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(EnhanceError::new(
            ErrorCode::RemoveFailed,
            "Could not remove the Enhance model",
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::enhance::catalog::{local_state, InstallState};
    use std::{io::Cursor, path::PathBuf, sync::Mutex};

    struct Fixture {
        body: Vec<u8>,
        length: Option<u64>,
        opened: Mutex<u32>,
        fail_after: Option<usize>,
    }
    struct Broken {
        data: Cursor<Vec<u8>>,
        fail_after: usize,
    }
    impl Read for Broken {
        fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
            if self.data.position() as usize >= self.fail_after {
                return Err(std::io::Error::other("reset"));
            }
            let n = buf.len().min(4);
            self.data.read(&mut buf[..n])
        }
    }
    impl Transport for Fixture {
        fn open(
            &self,
            _: &str,
            _: &Arc<AtomicBool>,
        ) -> Result<(Option<u64>, Box<dyn Read + Send>), EnhanceError> {
            *self.opened.lock().unwrap() += 1;
            let data = Cursor::new(self.body.clone());
            Ok(match self.fail_after {
                Some(n) => (
                    self.length,
                    Box::new(Broken {
                        data,
                        fail_after: n,
                    }),
                ),
                None => (self.length, Box::new(data)),
            })
        }
    }
    fn entry_for(body: &[u8]) -> &'static Entry {
        let sha = hex(&Sha256::digest(body));
        Box::leak(Box::new(Entry {
            id: "test-model",
            display_name: "Test",
            repo: "x/y",
            revision: "abc",
            filename: "m.gguf",
            size_bytes: body.len() as u64,
            sha256: Box::leak(sha.into_boxed_str()),
        }))
    }
    fn root(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("murmur-dl-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        dir
    }
    fn fixture(body: &[u8]) -> Fixture {
        Fixture {
            body: body.to_vec(),
            length: Some(body.len() as u64),
            opened: Mutex::new(0),
            fail_after: None,
        }
    }
    fn run(entry: &Entry, dir: &Path, t: &Fixture) -> Result<(), EnhanceError> {
        install(
            entry,
            dir,
            t,
            &Arc::new(AtomicBool::new(false)),
            None,
            &mut |_, _, _| {},
        )
    }
    #[test]
    fn exact_bytes_install_and_remove() {
        let body = b"GGUF fake model bytes".repeat(100);
        let entry = entry_for(&body);
        let dir = root("ok");
        let mut phases = Vec::new();
        install(
            entry,
            &dir,
            &fixture(&body),
            &Arc::new(AtomicBool::new(false)),
            Some(u64::MAX),
            &mut |p, r, t| phases.push((p, r, t)),
        )
        .unwrap();
        assert_eq!(phases.first().unwrap().0, Phase::Starting);
        assert_eq!(
            phases.last().unwrap(),
            &(Phase::Verifying, body.len() as u64, body.len() as u64)
        );
        assert_eq!(fs::read(entry.model_path(&dir)).unwrap(), body);
        assert!(!entry.dir(&dir).join("model.gguf.part").exists());
        assert_eq!(local_state(entry, &dir), InstallState::Installed);
        remove(entry, &dir).unwrap();
        assert_eq!(local_state(entry, &dir), InstallState::NotInstalled);
        let _ = fs::remove_dir_all(&dir);
    }
    #[test]
    fn wrong_hash_truncated_oversized_and_interrupted() {
        let body = b"0123456789".repeat(50);
        let entry = entry_for(&body);
        let dir = root("bad");
        let mut tampered = body.clone();
        tampered[3] ^= 1;
        assert_eq!(
            run(entry, &dir, &fixture(&tampered)).unwrap_err().code,
            ErrorCode::ChecksumMismatch
        );
        let mut short = fixture(&body[..100]);
        short.length = None;
        assert_eq!(
            run(entry, &dir, &short).unwrap_err().code,
            ErrorCode::SizeMismatch
        );
        let mut long = fixture(&[body.clone(), b"x".to_vec()].concat());
        long.length = None;
        assert_eq!(
            run(entry, &dir, &long).unwrap_err().code,
            ErrorCode::SizeMismatch
        );
        assert_eq!(
            run(entry, &dir, &fixture(&body[..10])).unwrap_err().code,
            ErrorCode::SizeMismatch
        );
        let mut broken = fixture(&body);
        broken.fail_after = Some(40);
        assert_eq!(
            run(entry, &dir, &broken).unwrap_err().code,
            ErrorCode::Network
        );
        assert!(!entry.dir(&dir).join("model.gguf.part").exists());
        assert_eq!(local_state(entry, &dir), InstallState::NotInstalled);
        let _ = fs::remove_dir_all(&dir);
    }
    #[test]
    fn cancellation_and_space() {
        let body = b"abc".repeat(10);
        let entry = entry_for(&body);
        let dir = root("cancel");
        let err = install(
            entry,
            &dir,
            &fixture(&body),
            &Arc::new(AtomicBool::new(true)),
            None,
            &mut |_, _, _| {},
        )
        .unwrap_err();
        assert_eq!(err.code, ErrorCode::Cancelled);
        let t = fixture(&body);
        let err = install(
            entry,
            &dir,
            &t,
            &Arc::new(AtomicBool::new(false)),
            Some(10),
            &mut |_, _, _| {},
        )
        .unwrap_err();
        assert_eq!(err.code, ErrorCode::InsufficientSpace);
        assert_eq!(
            *t.opened.lock().unwrap(),
            0,
            "no network before the space check"
        );
        let _ = fs::remove_dir_all(&dir);
    }
    #[test]
    fn orphaned_part_is_not_installed_and_bad_record_is_corrupt() {
        let body = b"abc".repeat(10);
        let entry = entry_for(&body);
        let dir = root("orphan");
        fs::create_dir_all(entry.dir(&dir)).unwrap();
        fs::write(entry.dir(&dir).join("model.gguf.part"), b"abc").unwrap();
        assert_eq!(local_state(entry, &dir), InstallState::NotInstalled);
        fs::write(entry.model_path(&dir), &body).unwrap();
        assert_eq!(local_state(entry, &dir), InstallState::Corrupt);
        let _ = fs::remove_dir_all(&dir);
    }
    #[test]
    fn stalled_headers_and_body_cancel_promptly() {
        use std::{io::Write as _, net::TcpListener, sync::mpsc as channel, thread};
        for headers in [false, true] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let address = listener.local_addr().unwrap();
            let (release, wait) = channel::channel::<()>();
            let (request_seen, seen) = channel::channel::<()>();
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                let mut request = [0u8; 1024];
                let _ = stream.read(&mut request);
                let _ = request_seen.send(());
                if headers {
                    stream
                        .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 1024\r\n\r\na")
                        .unwrap();
                }
                let _ = wait.recv_timeout(Duration::from_secs(3));
            });
            let transport = Https {
                client: reqwest::Client::builder().build().unwrap(),
            };
            let flag = Arc::new(AtomicBool::new(false));
            // Cancellation is driven by an event, never a sleep: a timer that fires
            // late under parallel test load used to let the second body read block
            // until the server closed, so the read returned Ok(0) instead of an error.
            let timer = if headers {
                None
            } else {
                let cancelling = flag.clone();
                Some(thread::spawn(move || {
                    if seen.recv_timeout(Duration::from_secs(3)).is_ok() {
                        cancelling.store(true, Ordering::Release);
                    }
                }))
            };
            let start = Instant::now();
            let opened = transport.open(&format!("http://{address}/model"), &flag);
            if headers {
                let (_, mut body) = opened.unwrap();
                let mut buffer = [0u8; 1024];
                assert_eq!(body.read(&mut buffer).unwrap(), 1);
                flag.store(true, Ordering::Release);
                assert!(body.read(&mut buffer).is_err());
            } else {
                assert!(matches!(
                    opened,
                    Err(EnhanceError {
                        code: ErrorCode::Cancelled,
                        ..
                    })
                ));
            }
            assert!(start.elapsed() < Duration::from_secs(2));
            if let Some(timer) = timer {
                timer.join().unwrap();
            }
            release.send(()).unwrap();
            server.join().unwrap();
        }
    }
    #[test]
    fn url_policy() {
        for ok in [
            "https://huggingface.co/a/resolve/b/c",
            "https://cdn-lfs.huggingface.co/x",
            "https://cas-bridge.xethub.hf.co/x?sig=1",
        ] {
            assert!(allowed_url(&reqwest::Url::parse(ok).unwrap()), "{ok}");
        }
        for bad in [
            "http://huggingface.co/a",
            "https://user:pw@huggingface.co/a",
            "https://evilhuggingface.co/a",
            "https://huggingface.co.evil.com/a",
            "https://huggingface.co:8443/a",
            "https://example.com/a",
            "https://unreviewed.huggingface.co/a",
            "https://unreviewed.hf.co/a",
        ] {
            assert!(!allowed_url(&reqwest::Url::parse(bad).unwrap()), "{bad}");
        }
        let entry = crate::enhance::catalog::find("qwen3-1.7b-q4_k_m").unwrap();
        assert_eq!(
            entry.url(),
            "https://huggingface.co/unsloth/Qwen3-1.7B-GGUF/resolve/d7f544eead698dbd1f15126ef60b45a1e1933222/Qwen3-1.7B-Q4_K_M.gguf"
        );
    }
}
