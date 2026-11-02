use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use tauri::{AppHandle, Emitter};
struct Request {
    method: String,
    params: Value,
    timeout: Duration,
    reply: mpsc::Sender<Result<Value>>,
}
enum Message {
    Request(Request),
    Quit,
}
struct Process {
    child: Child,
    stdin: ChildStdin,
    lines: mpsc::Receiver<Value>,
    readers: Vec<JoinHandle<()>>,
    next_id: u64,
}
impl Process {
    fn spawn(models: &Path) -> Result<Self> {
        let mut command = sidecar_command()?;
        command
            .args(["--models-dir"])
            .arg(models)
            .env("HF_HUB_OFFLINE", "1")
            .env("HF_HUB_DISABLE_TELEMETRY", "1")
            .env("DO_NOT_TRACK", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let mut child =
            command.spawn().context("Start murmur-engine sidecar; check the Python venv or packaged engine")?;
        let stdin = child.stdin.take().context("Sidecar stdin missing")?;
        let stdout = child.stdout.take().context("Sidecar stdout missing")?;
        let stderr = child.stderr.take().context("Sidecar stderr missing")?;
        let (tx, lines) = mpsc::channel();
        let reader = thread::Builder::new()
            .name("murmur-engine-stdout".into())
            .spawn(move || {
                for line in BufReader::new(stdout).lines() {
                    let Ok(line) = line else {
                        break;
                    };
                    let message = serde_json::from_str(&line).unwrap_or_else(|_| json!({"protocol_error":true}));
                    if tx.send(message).is_err() {
                        break;
                    }
                }
            })
            .context("Start sidecar protocol reader");
        let reader = match reader {
            Ok(r) => r,
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(e);
            }
        };
        let errors = thread::Builder::new()
            .name("murmur-engine-stderr".into())
            .spawn(move || {
                for line in BufReader::new(stderr).lines() {
                    if line.is_err() {
                        break;
                    }
                    // Third-party libraries can include sensitive values; never forward raw stderr.
                    tracing::debug!("Engine diagnostic received on stderr");
                }
            })
            .context("Start sidecar diagnostic reader");
        let errors = match errors {
            Ok(r) => r,
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = reader.join();
                return Err(e);
            }
        };
        Ok(Self { child, stdin, lines, readers: vec![reader, errors], next_id: 0 })
    }
    fn call(&mut self, request: &Request, app: &AppHandle, stopping: &AtomicBool) -> Result<Value> {
        self.next_id += 1;
        serde_json::to_writer(
            &mut self.stdin,
            &json!({"id":self.next_id,"method":request.method,"params":request.params}),
        )
        .context("Encode engine request")?;
        self.stdin.write_all(b"\n").context("Write engine request")?;
        self.stdin.flush().context("Flush engine request")?;
        let deadline = Instant::now() + request.timeout;
        loop {
            if stopping.load(Ordering::Acquire) {
                bail!("Engine is shutting down");
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                bail!("Engine request timed out");
            }
            let message = match self.lines.recv_timeout(remaining.min(Duration::from_millis(100))) {
                Ok(message) => message,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(error) => return Err(error).context("Engine disconnected"),
            };
            if message.get("protocol_error").is_some() {
                bail!("Invalid sidecar protocol output");
            }
            if message.get("event").and_then(Value::as_str) == Some("download") {
                let _ = app.emit("download", &message);
                continue;
            }
            if message.get("id").and_then(Value::as_u64) != Some(self.next_id) {
                continue;
            }
            if message.get("ok").and_then(Value::as_bool) != Some(true) {
                // A model/request error is not a process crash. Preserve the warm process.
                return Ok(
                    json!({"rpc_error":message["error"]["message"].as_str().unwrap_or("Engine request failed")}),
                );
            }
            return Ok(message.get("result").cloned().unwrap_or(Value::Null));
        }
    }
}
impl Drop for Process {
    fn drop(&mut self) {
        if !matches!(self.child.try_wait(), Ok(Some(_))) {
            // PyInstaller onefile bootloaders have a child; terminate that tree too.
            #[cfg(unix)]
            {
                let _ = Command::new("/bin/kill")
                    .args(["-KILL", "--", &format!("-{}", self.child.id())])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
            }
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                let _ = Command::new("taskkill.exe")
                    .args(["/PID", &self.child.id().to_string(), "/T", "/F"])
                    .creation_flags(0x08000000)
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
            }
            let _ = self.child.kill();
        }
        let _ = self.child.wait();
        for job in self.readers.drain(..) {
            let _ = job.join();
        }
    }
}
fn sidecar_command() -> Result<Command> {
    if cfg!(debug_assertions) {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..");
        #[cfg(windows)]
        {
            let mut command = Command::new(root.join("engine/.venv/Scripts/python.exe"));
            command.current_dir(root.join("engine")).args(["-m", "murmur_engine"]);
            Ok(command)
        }
        #[cfg(not(windows))]
        {
            Ok(Command::new(root.join("scripts/engine-sidecar.sh")))
        }
    } else {
        let executable = std::env::current_exe().context("Locate Murmur executable")?;
        let parent = executable.parent().context("Executable has no parent")?;
        Ok(Command::new(parent.join(if cfg!(windows) { "murmur-engine.exe" } else { "murmur-engine" })))
    }
}
pub struct Engine {
    sender: mpsc::Sender<Message>,
    job: Mutex<Option<JoinHandle<()>>>,
    alive: Arc<AtomicBool>,
    stopping: Arc<AtomicBool>,
    failure: Arc<Mutex<Option<String>>>,
}
impl Engine {
    pub fn new(models: PathBuf, app: AppHandle) -> Result<Self> {
        let (sender, receiver) = mpsc::channel();
        let alive = Arc::new(AtomicBool::new(false));
        let live = alive.clone();
        let stopping = Arc::new(AtomicBool::new(false));
        let stop = stopping.clone();
        let failure = Arc::new(Mutex::new(None));
        let errors = failure.clone();
        let job = thread::Builder::new()
            .name("murmur-engine-supervisor".into())
            .spawn(move || {
                let mut process: Option<Process> = None;
                let mut failures = 0;
                loop {
                    if stop.load(Ordering::Acquire) {
                        break;
                    }
                    if process.is_none() && failures < 2 {
                        match Process::spawn(&models) {
                            Ok(p) => {
                                process = Some(p);
                                live.store(true, Ordering::Release);
                                if let Ok(mut error) = errors.lock() {
                                    *error = None;
                                }
                            }
                            Err(error) => {
                                failures += 1;
                                report(&app, &errors, &format!("{error:#}"), failures);
                            }
                        }
                    }
                    match receiver.recv_timeout(Duration::from_millis(200)) {
                        Ok(Message::Quit) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
                        Ok(Message::Request(request)) => {
                            let result = match process.as_mut() {
                                Some(p) => p.call(&request, &app, &stop),
                                None => Err(anyhow::anyhow!("Engine unavailable after two failures; restart Murmur")),
                            };
                            if result.is_err() && process.is_some() {
                                process = None;
                                live.store(false, Ordering::Release);
                                failures += 1;
                                report(&app, &errors, "Engine crashed or timed out", failures);
                            }
                            let _ = request.reply.send(result);
                        }
                        Err(mpsc::RecvTimeoutError::Timeout) => {
                            let dead = process.as_mut().is_some_and(|p| !matches!(p.child.try_wait(), Ok(None)));
                            if dead {
                                process = None;
                                live.store(false, Ordering::Release);
                                failures += 1;
                                report(&app, &errors, "Engine exited unexpectedly", failures);
                            }
                        }
                    }
                }
                if let Some(p) = &mut process {
                    let (reply, _) = mpsc::channel();
                    let _ = p.call(
                        &Request {
                            method: "shutdown".into(),
                            params: json!({}),
                            timeout: Duration::from_secs(2),
                            reply,
                        },
                        &app,
                        &AtomicBool::new(false),
                    );
                }
                drop(process);
                live.store(false, Ordering::Release);
            })
            .context("Start engine supervisor")?;
        Ok(Self { sender, job: Mutex::new(Some(job)), alive, stopping, failure })
    }
    pub fn request(&self, method: &str, params: Value) -> Result<Value> {
        if self.stopping.load(Ordering::Acquire) {
            bail!("Engine is shutting down");
        }
        let timeout = Duration::from_secs(if method == "models.install" { 1800 } else { 120 });
        let (reply, response) = mpsc::channel();
        self.sender
            .send(Message::Request(Request { method: method.into(), params, timeout, reply }))
            .context("Engine supervisor stopped")?;
        let value =
            response.recv_timeout(timeout + Duration::from_secs(5)).context("Engine request queue timed out")??;
        if let Some(message) = value.get("rpc_error").and_then(Value::as_str) {
            bail!("{message}");
        }
        Ok(value)
    }
    pub fn alive(&self) -> bool {
        self.alive.load(Ordering::Acquire)
    }
    pub fn failure(&self) -> Option<String> {
        self.failure.lock().ok().and_then(|x| x.clone())
    }
    pub fn interrupt(&self) {
        self.stopping.store(true, Ordering::Release);
        let _ = self.sender.send(Message::Quit);
    }
    pub fn shutdown(&self) {
        self.interrupt();
        if let Ok(mut guard) = self.job.lock() {
            if let Some(job) = guard.take() {
                let _ = job.join();
            }
        }
    }
}
fn report(app: &AppHandle, error: &Mutex<Option<String>>, message: &str, failures: u32) {
    let message = if failures >= 2 {
        format!("{message}; engine failed twice. Restart Murmur to retry.")
    } else {
        format!("{message}; restarting engine once")
    };
    if let Ok(mut error) = error.lock() {
        *error = Some(message.clone());
    }
    let _ = app.emit("error", json!({"message": message, "persistent": failures >= 2}));
}
