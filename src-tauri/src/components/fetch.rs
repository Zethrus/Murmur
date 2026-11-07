//! Explicit, user-initiated HTTPS downloads. Every URL and every redirect hop must be HTTPS on
//! port 443 to an allowlisted host name (no IP literals, no user info, at most 5 redirects).
//! Downloads resume into a `.part` file with HTTP Range; on resume the existing bytes are
//! hashed again from byte 0, and nothing is trusted until size and SHA-256 match.
use super::{ComponentError, Reason};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Seek, SeekFrom, Write},
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    time::{Duration, Instant},
};

/// Exact host names. GitHub Releases redirect to its asset hosts; Hugging Face serves model
/// files from `huggingface.co` and redirects to the CDN hosts listed (observed 2026-11-06:
/// `us.aws.cdn.hf.co`, `cas-bridge.xethub.hf.co`, `cdn-lfs-us-1.hf.co`). No wildcard is
/// accepted: `anything.hf.co` is refused. Integrity never depends on this list (SHA-256 does).
pub const ALLOWED_HOSTS: &[&str] = &[
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "huggingface.co",
    "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.huggingface.co",
    "cdn-lfs-eu-1.huggingface.co",
    "us.aws.cdn.hf.co",
    "cas-bridge.xethub.hf.co",
    "cdn-lfs-us-1.hf.co",
];
pub const MAX_REDIRECTS: usize = 5;

pub fn allowed_host(host: &str) -> bool {
    ALLOWED_HOSTS.contains(&host.to_ascii_lowercase().as_str())
}

pub fn allowed_url(url: &reqwest::Url) -> bool {
    #[cfg(test)]
    if url.scheme() == "http" && url.host_str() == Some("127.0.0.1") {
        return true;
    }
    url.scheme() == "https"
        && url.username().is_empty()
        && url.password().is_none()
        // `domain()` is None for IP literals.
        && url.domain().is_some_and(allowed_host)
        && url.port().is_none_or(|p| p == 443)
}

pub struct Body {
    /// Bytes the server says follow (the rest after `offset`, or the whole file).
    pub length: Option<u64>,
    /// The server honoured the Range request.
    pub resumed: bool,
    pub reader: Box<dyn Read + Send>,
}

pub trait Transport: Send + Sync {
    /// GET `url` from byte `offset`. The reader fails with `TimedOut` when no byte arrives
    /// for the stall timeout and with `Interrupted` once `cancel` is set.
    fn open(
        &self,
        url: &str,
        offset: u64,
        cancel: &Arc<AtomicBool>,
    ) -> Result<Body, ComponentError>;
}

fn network(message: &str) -> ComponentError {
    ComponentError::new(Reason::Network, message)
}
fn cancelled() -> ComponentError {
    ComponentError::new(Reason::Cancelled, "Download cancelled")
}

pub struct Https {
    client: reqwest::Client,
    stall: Duration,
}

impl Https {
    pub fn new(stall: Duration) -> Result<Self, ComponentError> {
        let policy = reqwest::redirect::Policy::custom(|attempt| {
            if attempt.previous().len() > MAX_REDIRECTS {
                attempt.error("too many redirects")
            } else if allowed_url(attempt.url()) {
                attempt.follow()
            } else {
                attempt.error("redirect to a disallowed location")
            }
        });
        let client = reqwest::Client::builder()
            .redirect(policy)
            .https_only(!cfg!(test))
            .connect_timeout(Duration::from_secs(20))
            .user_agent(concat!("Murmur/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|_| network("Could not start a secure connection"))?;
        Ok(Self { client, stall })
    }
}

struct StreamReader {
    chunks: mpsc::Receiver<std::io::Result<Vec<u8>>>,
    current: std::io::Cursor<Vec<u8>>,
    cancel: Arc<AtomicBool>,
    stopped: Arc<AtomicBool>,
    stall: Duration,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl Read for StreamReader {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        let waiting = Instant::now();
        loop {
            if self.cancel.load(Ordering::Acquire) {
                return Err(std::io::ErrorKind::Interrupted.into());
            }
            if (self.current.position() as usize) < self.current.get_ref().len() {
                return self.current.read(buf);
            }
            match self.chunks.recv_timeout(Duration::from_millis(100)) {
                Ok(Ok(chunk)) => self.current = std::io::Cursor::new(chunk),
                Ok(Err(error)) => return Err(error),
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    if self.cancel.load(Ordering::Acquire) {
                        return Err(std::io::ErrorKind::Interrupted.into());
                    }
                    return Ok(0);
                }
                Err(mpsc::RecvTimeoutError::Timeout) if waiting.elapsed() >= self.stall => {
                    return Err(std::io::ErrorKind::TimedOut.into());
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
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

/// `Content-Range: bytes <start>-<end>/<total>` must start exactly at `offset`.
fn content_range_start(headers: &reqwest::header::HeaderMap) -> Option<u64> {
    let value = headers.get(reqwest::header::CONTENT_RANGE)?.to_str().ok()?;
    let range = value.strip_prefix("bytes ")?;
    range.split_once('-')?.0.trim().parse().ok()
}

impl Transport for Https {
    fn open(
        &self,
        url: &str,
        offset: u64,
        cancel: &Arc<AtomicBool>,
    ) -> Result<Body, ComponentError> {
        let parsed = reqwest::Url::parse(url).map_err(|_| network("Invalid download URL"))?;
        if !allowed_url(&parsed) {
            return Err(ComponentError::new(
                Reason::NotAllowed,
                "Download location is not allowed",
            ));
        }
        let client = self.client.clone();
        let flag = cancel.clone();
        let stopped = Arc::new(AtomicBool::new(false));
        let stop = stopped.clone();
        let stall = self.stall;
        let (headers_tx, headers_rx) = mpsc::channel();
        let (chunks_tx, chunks_rx) = mpsc::sync_channel(4);
        let thread = std::thread::Builder::new()
            .name("murmur-component-http".into())
            .spawn(move || {
                let Ok(runtime) = tokio::runtime::Builder::new_current_thread().enable_all().build() else {
                    let _ = headers_tx.send(Err(network("Could not start the download")));
                    return;
                };
                runtime.block_on(async move {
                    let mut request = client.get(parsed);
                    if offset > 0 {
                        request = request.header(reqwest::header::RANGE, format!("bytes={offset}-"));
                    }
                    let request = request.send();
                    tokio::pin!(request);
                    let started = Instant::now();
                    let mut response = loop {
                        tokio::select! {
                            answer = &mut request => match answer {
                                Ok(response) => break response,
                                Err(error) => {
                                    let reason = if error.is_redirect() {
                                        ComponentError::new(Reason::NotAllowed, "The server redirected to a location Murmur does not download from")
                                    } else {
                                        network("Could not reach the download server")
                                    };
                                    let _ = headers_tx.send(Err(reason));
                                    return;
                                }
                            },
                            _ = tokio::time::sleep(Duration::from_millis(100)) => {
                                if flag.load(Ordering::Acquire) || stop.load(Ordering::Acquire) { return; }
                                if started.elapsed() >= stall {
                                    let _ = headers_tx.send(Err(network("The download server did not respond")));
                                    return;
                                }
                            }
                        }
                    };
                    let status = response.status();
                    if !allowed_url(response.url()) {
                        let _ = headers_tx.send(Err(ComponentError::new(Reason::NotAllowed, "Download location is not allowed")));
                        return;
                    }
                    if status.is_redirection() {
                        // reqwest returns redirects it cannot follow (e.g. to file:) as-is.
                        let _ = headers_tx.send(Err(ComponentError::new(Reason::NotAllowed, "The server redirected to a location Murmur does not download from")));
                        return;
                    }
                    let resumed = status == reqwest::StatusCode::PARTIAL_CONTENT;
                    if !(status == reqwest::StatusCode::OK || resumed)
                        || (resumed && content_range_start(response.headers()) != Some(offset))
                    {
                        let _ = headers_tx.send(Err(network("The download server rejected the request")));
                        return;
                    }
                    if headers_tx.send(Ok((response.content_length(), resumed))).is_err() {
                        return;
                    }
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
                                    tokio::time::sleep(Duration::from_millis(20)).await;
                                }
                            }
                        }
                        if failed { return; }
                    }
                });
            })
            .map_err(|_| network("Could not start the download"))?;
        let headers = loop {
            if cancel.load(Ordering::Acquire) {
                stopped.store(true, Ordering::Release);
                let _ = thread.join();
                return Err(cancelled());
            }
            match headers_rx.recv_timeout(Duration::from_millis(100)) {
                Ok(value) => break value,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(_) => {
                    break Err(if cancel.load(Ordering::Acquire) {
                        cancelled()
                    } else {
                        network("Could not reach the download server")
                    })
                }
            }
        };
        match headers {
            Ok((length, resumed)) => Ok(Body {
                length,
                resumed,
                reader: Box::new(StreamReader {
                    chunks: chunks_rx,
                    current: std::io::Cursor::new(Vec::new()),
                    cancel: cancel.clone(),
                    stopped,
                    stall,
                    thread: Some(thread),
                }),
            }),
            Err(error) => {
                stopped.store(true, Ordering::Release);
                let _ = thread.join();
                Err(error)
            }
        }
    }
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// SHA-256 of a whole file.
pub fn hash_file(path: &Path, cancel: Option<&AtomicBool>) -> std::io::Result<(u64, String)> {
    let mut file = fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 1 << 20];
    let mut total = 0u64;
    loop {
        if cancel.is_some_and(|c| c.load(Ordering::Acquire)) {
            return Err(std::io::ErrorKind::Interrupted.into());
        }
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hasher.update(&buffer[..n]);
        total += n as u64;
    }
    Ok((total, hex(&hasher.finalize())))
}

#[derive(Debug, Clone, Copy)]
pub struct Retry {
    /// Retries after the first attempt (network errors only).
    pub retries: u32,
    pub backoff: Duration,
}
impl Default for Retry {
    fn default() -> Self {
        Self {
            retries: 3,
            backoff: Duration::from_secs(2),
        }
    }
}

pub struct Target<'a> {
    pub urls: &'a [String],
    pub size: u64,
    pub sha256: &'a str,
    pub part: &'a Path,
}

fn io(message: &str) -> ComponentError {
    ComponentError::new(Reason::Io, message)
}

/// Download `target` into its `.part` file, resuming what is there. `progress(done, total)`.
/// On a verification failure the `.part` is deleted; on network failure or cancellation it
/// is kept so a later Resume continues.
pub fn download(
    target: &Target<'_>,
    transport: &dyn Transport,
    cancel: &Arc<AtomicBool>,
    retry: Retry,
    progress: &mut dyn FnMut(u64, u64),
) -> Result<(), ComponentError> {
    let result = download_inner(target, transport, cancel, retry, progress);
    if matches!(&result, Err(e) if matches!(e.reason, Reason::SizeMismatch | Reason::ChecksumMismatch))
    {
        let _ = fs::remove_file(target.part);
    }
    result
}

fn download_inner(
    target: &Target<'_>,
    transport: &dyn Transport,
    cancel: &Arc<AtomicBool>,
    retry: Retry,
    progress: &mut dyn FnMut(u64, u64),
) -> Result<(), ComponentError> {
    if let Some(parent) = target.part.parent() {
        fs::create_dir_all(parent).map_err(|_| io("Could not create the download folder"))?;
    }
    let mut file = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(target.part)
        .map_err(|_| io("Could not open the download file"))?;
    let mut hasher = Sha256::new();
    let mut done = file
        .metadata()
        .map_err(|_| io("Could not read the download file"))?
        .len();
    if done > target.size {
        file.set_len(0)
            .map_err(|_| io("Could not reset the download file"))?;
        done = 0;
    }
    // Resume: hash every byte already on disk again; nothing from a previous run is trusted.
    let mut buffer = vec![0u8; 1 << 20];
    if done > 0 {
        file.seek(SeekFrom::Start(0))
            .map_err(|_| io("Could not read the download file"))?;
        let mut remaining = done;
        while remaining > 0 {
            if cancel.load(Ordering::Acquire) {
                return Err(cancelled());
            }
            let want = remaining.min(buffer.len() as u64) as usize;
            file.read_exact(&mut buffer[..want])
                .map_err(|_| io("Could not read the download file"))?;
            hasher.update(&buffer[..want]);
            remaining -= want as u64;
        }
    }
    file.seek(SeekFrom::Start(done))
        .map_err(|_| io("Could not read the download file"))?;
    progress(done, target.size);
    let mut failures = 0u32;
    let mut attempt = 0usize;
    while done < target.size {
        if cancel.load(Ordering::Acquire) {
            return Err(cancelled());
        }
        let url = &target.urls[attempt % target.urls.len()];
        attempt += 1;
        let outcome = (|| -> Result<(), ComponentError> {
            let body = transport.open(url, done, cancel)?;
            if done > 0 && !body.resumed {
                // The server ignored Range: start again from byte 0.
                file.set_len(0)
                    .map_err(|_| io("Could not reset the download file"))?;
                file.seek(SeekFrom::Start(0))
                    .map_err(|_| io("Could not reset the download file"))?;
                hasher = Sha256::new();
                done = 0;
                progress(0, target.size);
            }
            if body.length.is_some_and(|n| n != target.size - done) {
                return Err(ComponentError::new(
                    Reason::SizeMismatch,
                    "The server reported an unexpected size",
                ));
            }
            let mut reader = body.reader;
            loop {
                let n = match reader.read(&mut buffer) {
                    Ok(n) => n,
                    Err(_) if cancel.load(Ordering::Acquire) => return Err(cancelled()),
                    Err(error) if error.kind() == std::io::ErrorKind::TimedOut => {
                        return Err(network("The download stalled"))
                    }
                    Err(_) => return Err(network("The download was interrupted")),
                };
                if n == 0 {
                    return Ok(());
                }
                if done + n as u64 > target.size {
                    return Err(ComponentError::new(
                        Reason::SizeMismatch,
                        "The download is larger than expected",
                    ));
                }
                file.write_all(&buffer[..n])
                    .map_err(|_| io("Could not write the download (disk full?)"))?;
                hasher.update(&buffer[..n]);
                done += n as u64;
                progress(done, target.size);
            }
        })();
        match outcome {
            Ok(()) if done < target.size => {
                // Clean end of stream before the expected size: the transfer was cut short.
                failures += 1;
                if failures > retry.retries {
                    return Err(ComponentError::new(
                        Reason::SizeMismatch,
                        "The download ended early",
                    ));
                }
            }
            Ok(()) => {}
            Err(error) if matches!(error.reason, Reason::Network) => {
                failures += 1;
                if failures > retry.retries {
                    return Err(error);
                }
            }
            Err(error) => return Err(error),
        }
        if done < target.size {
            let wait = retry
                .backoff
                .saturating_mul(1 << (failures.saturating_sub(1)).min(6));
            let deadline = Instant::now() + wait;
            while Instant::now() < deadline {
                if cancel.load(Ordering::Acquire) {
                    return Err(cancelled());
                }
                std::thread::sleep(Duration::from_millis(20).min(wait));
            }
        }
    }
    file.flush()
        .and_then(|_| file.sync_all())
        .map_err(|_| io("Could not save the download"))?;
    if !hex(&hasher.finalize()).eq_ignore_ascii_case(target.sha256) {
        return Err(ComponentError::new(
            Reason::ChecksumMismatch,
            "The download failed its SHA-256 check",
        ));
    }
    Ok(())
}

#[cfg(test)]
pub mod testserver {
    //! Minimal HTTP/1.1 server on 127.0.0.1 for fetch and job tests.
    use std::{
        io::{BufRead, BufReader, Write},
        net::{TcpListener, TcpStream},
        sync::{Arc, Mutex},
        thread,
    };

    pub struct Request {
        pub path: String,
        pub range: Option<u64>,
    }
    pub type Handler = dyn Fn(&Request, &mut TcpStream) + Send + Sync;

    pub struct Server {
        pub base: String,
        pub requests: Arc<Mutex<Vec<(String, Option<u64>)>>>,
    }

    pub fn start(handler: Arc<Handler>) -> Server {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let requests = Arc::new(Mutex::new(Vec::new()));
        let log = requests.clone();
        thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { break };
                let handler = handler.clone();
                let log = log.clone();
                thread::spawn(move || {
                    let mut reader = BufReader::new(stream.try_clone().unwrap());
                    let mut line = String::new();
                    if reader.read_line(&mut line).is_err() {
                        return;
                    }
                    let path = line.split_whitespace().nth(1).unwrap_or("/").to_owned();
                    let mut range = None;
                    loop {
                        let mut header = String::new();
                        if reader.read_line(&mut header).unwrap_or(0) == 0 || header == "\r\n" {
                            break;
                        }
                        if let Some(value) =
                            header.to_ascii_lowercase().strip_prefix("range: bytes=")
                        {
                            range = value.trim().trim_end_matches('-').parse().ok();
                        }
                    }
                    log.lock().unwrap().push((path.clone(), range));
                    handler(&Request { path, range }, &mut stream);
                    let _ = stream.flush();
                });
            }
        });
        Server { base, requests }
    }

    /// Serve `body` honouring Range; `limit` truncates each response after that many bytes.
    pub fn respond(stream: &mut TcpStream, body: &[u8], range: Option<u64>, limit: Option<usize>) {
        let start = range.unwrap_or(0) as usize;
        let slice = &body[start.min(body.len())..];
        let header = if range.is_some() {
            format!("HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nContent-Range: bytes {start}-{}/{}\r\nConnection: close\r\n\r\n",
                slice.len(), body.len().saturating_sub(1), body.len())
        } else {
            format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                slice.len()
            )
        };
        let _ = stream.write_all(header.as_bytes());
        let _ = stream.write_all(&slice[..limit.unwrap_or(slice.len()).min(slice.len())]);
    }
}

#[cfg(test)]
mod tests {
    use super::{testserver::*, *};
    use std::{net::TcpStream, sync::Mutex};

    fn transport() -> Https {
        Https::new(Duration::from_millis(400)).unwrap()
    }
    fn fast() -> Retry {
        Retry {
            retries: 3,
            backoff: Duration::from_millis(10),
        }
    }
    fn temp(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("murmur-fetch-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }
    fn body() -> Vec<u8> {
        (0..200_000u32).flat_map(|i| i.to_le_bytes()).collect()
    }
    fn sha(data: &[u8]) -> String {
        hex(&Sha256::digest(data))
    }

    #[test]
    fn url_policy() {
        for ok in [
            "https://github.com/Zethrus/Murmur/releases/download/v1/x.zip",
            "https://release-assets.githubusercontent.com/x?token=1",
            "https://objects.githubusercontent.com/x",
            "https://huggingface.co/a/resolve/0123/model.bin",
            "https://cdn-lfs-eu-1.huggingface.co/x",
            "https://us.aws.cdn.hf.co/xet-bridge-us/x",
            "https://GitHub.com:443/x",
        ] {
            assert!(allowed_url(&reqwest::Url::parse(ok).unwrap()), "{ok}");
        }
        for bad in [
            "http://github.com/x",
            "https://user@github.com/x",
            "https://user:pw@github.com/x",
            "https://github.com:8443/x",
            "https://github.com.evil.example/x",
            "https://evil-github.com/x",
            "https://anything.hf.co/x",
            "https://hf.co/x",
            "https://140.82.112.3/x",
            "https://[::1]/x",
            "ftp://github.com/x",
            "http://127.0.0.2/x",
        ] {
            assert!(!allowed_url(&reqwest::Url::parse(bad).unwrap()), "{bad}");
        }
    }

    #[test]
    fn every_disallowed_redirect_class_is_refused() {
        let hops = Arc::new(Mutex::new(0u32));
        let counter = hops.clone();
        let server = start(Arc::new(
            move |request: &Request, stream: &mut TcpStream| {
                let location = match request.path.as_str() {
                    "/http" => "http://github.com/x".to_owned(),
                    "/host" => "https://example.com/x".to_owned(),
                    "/userinfo" => "https://u:p@github.com/x".to_owned(),
                    "/port" => "https://github.com:8443/x".to_owned(),
                    "/ip" => "https://140.82.112.3/x".to_owned(),
                    "/ipv6" => "https://[::1]/x".to_owned(),
                    "/wildcard" => "https://evil.hf.co/x".to_owned(),
                    "/scheme" => "file:///etc/passwd".to_owned(),
                    path => {
                        // /loop/N → /loop/N+1: more than five hops.
                        *counter.lock().unwrap() += 1;
                        let n: u32 = path.trim_start_matches("/loop/").parse().unwrap_or(0);
                        format!("/loop/{}", n + 1)
                    }
                };
                let _ = write!(stream, "HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
            },
        ));
        let t = transport();
        let flag = Arc::new(AtomicBool::new(false));
        for path in [
            "/http",
            "/host",
            "/userinfo",
            "/port",
            "/ip",
            "/ipv6",
            "/wildcard",
            "/scheme",
            "/loop/0",
        ] {
            let error = t
                .open(&format!("{}{path}", server.base), 0, &flag)
                .err()
                .unwrap_or_else(|| panic!("{path} followed"));
            assert_eq!(error.reason, Reason::NotAllowed, "{path}: {error:?}");
        }
        assert_eq!(
            *hops.lock().unwrap(),
            MAX_REDIRECTS as u32 + 1,
            "stops after five redirects"
        );
    }

    #[test]
    fn complete_download_verifies_size_and_hash() {
        let data = Arc::new(body());
        let served = data.clone();
        let server = start(Arc::new(move |r: &Request, s: &mut TcpStream| {
            respond(s, &served, r.range, None)
        }));
        let dir = temp("ok");
        let part = dir.join("x.part");
        let urls = vec![format!("{}/x", server.base)];
        let digest = sha(&data);
        let target = Target {
            urls: &urls,
            size: data.len() as u64,
            sha256: &digest,
            part: &part,
        };
        let mut seen = 0;
        download(
            &target,
            &transport(),
            &Arc::new(AtomicBool::new(false)),
            fast(),
            &mut |d, _| seen = d,
        )
        .unwrap();
        assert_eq!(seen, data.len() as u64);
        assert_eq!(fs::read(&part).unwrap(), *data);
        // Wrong hash: verification failure deletes the part and is not retried.
        fs::remove_file(&part).unwrap();
        let wrong = "0".repeat(64);
        let target = Target {
            sha256: &wrong,
            ..target
        };
        let before = server.requests.lock().unwrap().len();
        assert_eq!(
            download(
                &target,
                &transport(),
                &Arc::new(AtomicBool::new(false)),
                fast(),
                &mut |_, _| {}
            )
            .unwrap_err()
            .reason,
            Reason::ChecksumMismatch
        );
        assert_eq!(server.requests.lock().unwrap().len(), before + 1);
        assert!(!part.exists());
        // Size mismatch reported by the server.
        let target = Target {
            size: data.len() as u64 + 1,
            sha256: &digest,
            ..target
        };
        assert_eq!(
            download(
                &target,
                &transport(),
                &Arc::new(AtomicBool::new(false)),
                fast(),
                &mut |_, _| {}
            )
            .unwrap_err()
            .reason,
            Reason::SizeMismatch
        );
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn range_resume_rehashes_existing_bytes() {
        let data = Arc::new(body());
        let served = data.clone();
        let server = start(Arc::new(move |r: &Request, s: &mut TcpStream| {
            respond(s, &served, r.range, None)
        }));
        let dir = temp("resume");
        let part = dir.join("x.part");
        let urls = vec![format!("{}/x", server.base)];
        let digest = sha(&data);
        let target = Target {
            urls: &urls,
            size: data.len() as u64,
            sha256: &digest,
            part: &part,
        };
        // A genuine prefix resumes from its length with a Range request.
        fs::write(&part, &data[..300_000]).unwrap();
        download(
            &target,
            &transport(),
            &Arc::new(AtomicBool::new(false)),
            fast(),
            &mut |_, _| {},
        )
        .unwrap();
        assert_eq!(
            server.requests.lock().unwrap().last().unwrap().1,
            Some(300_000)
        );
        assert_eq!(fs::read(&part).unwrap(), *data);
        // A corrupted prefix is caught: bytes on disk are hashed again, not trusted.
        let mut corrupt = data[..300_000].to_vec();
        corrupt[10] ^= 1;
        fs::write(&part, &corrupt).unwrap();
        assert_eq!(
            download(
                &target,
                &transport(),
                &Arc::new(AtomicBool::new(false)),
                fast(),
                &mut |_, _| {}
            )
            .unwrap_err()
            .reason,
            Reason::ChecksumMismatch
        );
        assert!(
            !part.exists(),
            "a failed verification never leaves a resumable part"
        );
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn interrupted_transfers_retry_with_range_within_budget() {
        let data = Arc::new(body());
        let served = data.clone();
        // Every response stops after 250 kB: four attempts finish an 800 kB body.
        let server = start(Arc::new(move |r: &Request, s: &mut TcpStream| {
            respond(s, &served, r.range, Some(250_000))
        }));
        let dir = temp("retry");
        let part = dir.join("x.part");
        let urls = vec![format!("{}/x", server.base)];
        let digest = sha(&data);
        let target = Target {
            urls: &urls,
            size: data.len() as u64,
            sha256: &digest,
            part: &part,
        };
        download(
            &target,
            &transport(),
            &Arc::new(AtomicBool::new(false)),
            fast(),
            &mut |_, _| {},
        )
        .unwrap();
        let ranges: Vec<_> = server
            .requests
            .lock()
            .unwrap()
            .iter()
            .map(|r| r.1)
            .collect();
        assert_eq!(ranges, [None, Some(250_000), Some(500_000), Some(750_000)]);
        // A server that always fails uses exactly the retry budget: 1 + 3 attempts.
        let failing = start(Arc::new(|_: &Request, s: &mut TcpStream| {
            let _ = s.write_all(b"HTTP/1.1 503 Busy\r\nContent-Length: 0\r\n\r\n");
        }));
        let urls = vec![format!("{}/x", failing.base)];
        fs::remove_file(&part).ok();
        let target = Target {
            urls: &urls,
            ..target
        };
        assert_eq!(
            download(
                &target,
                &transport(),
                &Arc::new(AtomicBool::new(false)),
                fast(),
                &mut |_, _| {}
            )
            .unwrap_err()
            .reason,
            Reason::Network
        );
        assert_eq!(failing.requests.lock().unwrap().len(), 4);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn stalled_transfer_times_out_and_cancellation_keeps_part() {
        let data = Arc::new(body());
        let served = data.clone();
        let server = start(Arc::new(move |r: &Request, s: &mut TcpStream| {
            respond(s, &served, r.range, Some(100_000));
            std::thread::sleep(Duration::from_secs(3)); // hold the connection open, silent
        }));
        let dir = temp("stall");
        let part = dir.join("x.part");
        let urls = vec![format!("{}/x", server.base)];
        let digest = sha(&data);
        let target = Target {
            urls: &urls,
            size: data.len() as u64,
            sha256: &digest,
            part: &part,
        };
        let no_retry = Retry {
            retries: 0,
            backoff: Duration::from_millis(1),
        };
        let started = Instant::now();
        let error = download(
            &target,
            &transport(),
            &Arc::new(AtomicBool::new(false)),
            no_retry,
            &mut |_, _| {},
        )
        .unwrap_err();
        assert_eq!(error.reason, Reason::Network);
        assert!(error.message.contains("stalled"), "{error:?}");
        assert!(started.elapsed() < Duration::from_secs(3));
        assert_eq!(
            fs::metadata(&part).unwrap().len(),
            100_000,
            "partial bytes kept for resume"
        );
        // Cancel mid-body: the flag stops the read promptly and the part stays.
        fs::remove_file(&part).unwrap();
        let flag = Arc::new(AtomicBool::new(false));
        let trigger = flag.clone();
        let started = Instant::now();
        let error = download(&target, &transport(), &flag, fast(), &mut |done, _| {
            if done >= 50_000 {
                trigger.store(true, Ordering::Release);
            }
        })
        .unwrap_err();
        assert_eq!(error.reason, Reason::Cancelled);
        assert!(started.elapsed() < Duration::from_secs(2));
        assert!(part.exists());
        let _ = fs::remove_dir_all(dir);
    }
}
