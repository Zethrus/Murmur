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
    sync::atomic::{AtomicBool, Ordering},
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
    fn open(&self, url: &str) -> Result<(Option<u64>, Box<dyn Read + Send>), EnhanceError>;
}

/// Hosts the model download may reach: the catalog host and Hugging Face's download CDNs.
pub fn allowed_host(host: &str) -> bool {
    let host = host.to_ascii_lowercase();
    host == "huggingface.co"
        || host.ends_with(".huggingface.co")
        || host == "hf.co"
        || host.ends_with(".hf.co")
}

pub fn allowed_url(url: &reqwest::Url) -> bool {
    url.scheme() == "https"
        && url.username().is_empty()
        && url.password().is_none()
        && url.host_str().is_some_and(allowed_host)
        && url.port().is_none_or(|p| p == 443)
}

pub struct Https {
    client: reqwest::blocking::Client,
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
        let client = reqwest::blocking::Client::builder()
            .redirect(policy)
            .https_only(true)
            .connect_timeout(Duration::from_secs(20))
            .timeout(None)
            .user_agent(concat!("Murmur/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|_| {
                EnhanceError::new(ErrorCode::Network, "Could not start a secure connection")
            })?;
        Ok(Self { client })
    }
}

impl Transport for Https {
    fn open(&self, url: &str) -> Result<(Option<u64>, Box<dyn Read + Send>), EnhanceError> {
        let parsed = reqwest::Url::parse(url)
            .map_err(|_| EnhanceError::new(ErrorCode::Network, "Invalid model URL"))?;
        if !allowed_url(&parsed) {
            return Err(EnhanceError::new(
                ErrorCode::Network,
                "Model URL is not allowed",
            ));
        }
        // Never log the URL after redirects: CDN URLs are signed.
        let response = self.client.get(parsed).send().map_err(|error| {
            tracing::warn!(
                redirect = error.is_redirect(),
                timeout = error.is_timeout(),
                "Enhance download request failed"
            );
            EnhanceError::new(ErrorCode::Network, "Could not reach the model server")
        })?;
        if !response.status().is_success() {
            return Err(EnhanceError::new(
                ErrorCode::Network,
                &format!("Model server returned HTTP {}", response.status().as_u16()),
            ));
        }
        if !allowed_url(response.url()) {
            return Err(EnhanceError::new(
                ErrorCode::Network,
                "Model download was redirected to a disallowed host",
            ));
        }
        Ok((response.content_length(), Box::new(response)))
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
    cancel: &AtomicBool,
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
        let (length, mut body) = transport.open(&entry.url())?;
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
                EnhanceError::new(ErrorCode::Network, "The download was interrupted")
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
        fn open(&self, _: &str) -> Result<(Option<u64>, Box<dyn Read + Send>), EnhanceError> {
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
            &AtomicBool::new(false),
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
            &AtomicBool::new(false),
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
            &AtomicBool::new(true),
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
            &AtomicBool::new(false),
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
