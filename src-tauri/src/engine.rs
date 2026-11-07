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
    /// Stop the process and do not respawn until `Restart` (component files are changing).
    Stop(mpsc::Sender<()>),
    /// Forget earlier failures and a missing engine; spawn again with the current files.
    Restart,
    Quit,
}

/// Component-store lookups the engine needs (implemented by `components::Components`).
pub trait Locate: Send + Sync {
    fn engine_dir(&self) -> Option<PathBuf>;
    fn cuda_dir(&self) -> Option<PathBuf>;
    /// Verified directory of a speech model; None when absent or unverified.
    fn model_dir(&self, model: &str) -> Option<PathBuf>;
}
struct Process {
    child: Child,
    stdin: ChildStdin,
    lines: mpsc::Receiver<Value>,
    readers: Vec<JoinHandle<()>>,
    next_id: u64,
}
impl Process {
    fn spawn(models: &Path, locate: &dyn Locate) -> Result<Self> {
        let mut command = sidecar_command(locate)?;
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
        let mut child = command
            .spawn()
            .context("Start murmur-engine sidecar; check the Python venv or packaged engine")?;
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
                    let message = serde_json::from_str(&line)
                        .unwrap_or_else(|_| json!({"protocol_error":true}));
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
                    let Ok(line) = line else {
                        break;
                    };
                    if line.trim().is_empty() {
                        continue;
                    }
                    // Engine stderr is our only view into Python failures; keep it short and local.
                    let line: String = line.chars().take(400).collect();
                    if line.contains("Automatic engine fallback to CPU:") {
                        tracing::warn!(target: "murmur::engine", "{line}");
                    } else {
                        tracing::debug!(target: "murmur::engine", "stderr: {line}");
                    }
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
        Ok(Self {
            child,
            stdin,
            lines,
            readers: vec![reader, errors],
            next_id: 0,
        })
    }
    fn call(&mut self, request: &Request, app: &AppHandle, stopping: &AtomicBool) -> Result<Value> {
        self.next_id += 1;
        serde_json::to_writer(
            &mut self.stdin,
            &json!({"id":self.next_id,"method":request.method,"params":request.params}),
        )
        .context("Encode engine request")?;
        self.stdin
            .write_all(b"\n")
            .context("Write engine request")?;
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
            let message = match self
                .lines
                .recv_timeout(remaining.min(Duration::from_millis(100)))
            {
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
            // Debug launchers run Python as a child; terminate the whole process group.
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
/// Environment override naming the engine executable or the directory that holds it.
const ENGINE_ENV: &str = "MURMUR_ENGINE";
const ENGINE_FILE: &str = if cfg!(windows) {
    "murmur-engine.exe"
} else {
    "murmur-engine"
};

/// No usable engine executable. Not retried until a component install restarts the engine.
#[derive(Debug)]
struct EngineMissing(Option<PathBuf>);
pub const MISSING_MESSAGE: &str = "Speech engine not installed — open Settings";
impl std::fmt::Display for EngineMissing {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(MISSING_MESSAGE)?;
        match &self.0 {
            Some(path) => write!(f, " ({ENGINE_ENV}={} has no {ENGINE_FILE})", path.display()),
            None => Ok(()),
        }
    }
}
impl std::error::Error for EngineMissing {}

#[derive(Debug, PartialEq)]
enum EngineSource {
    /// Debug builds run the checkout's Python engine.
    Development,
    Executable(PathBuf),
}

/// Onedir engine shipped with the app: `usr/lib/murmur/engine` beside `usr/bin` on Linux
/// (AppImage and .deb), `engine\` beside `murmur.exe` on Windows.
fn packaged_engine(executable_dir: &Path) -> PathBuf {
    if cfg!(windows) {
        executable_dir.join("engine").join(ENGINE_FILE)
    } else {
        executable_dir
            .join("../lib/murmur/engine")
            .join(ENGINE_FILE)
    }
}

/// Resolution order: `MURMUR_ENGINE`, then the development venv (debug builds), then the
/// active engine component (`<data>/components/engine/<version>`), then a packaged onedir
/// engine (source installs; release packages no longer ship one).
fn resolve_engine(
    override_path: Option<PathBuf>,
    executable_dir: &Path,
    development: bool,
    store: Option<PathBuf>,
) -> std::result::Result<EngineSource, EngineMissing> {
    if let Some(path) = override_path.filter(|path| !path.as_os_str().is_empty()) {
        let candidate = if path.is_dir() {
            path.join(ENGINE_FILE)
        } else {
            path.clone()
        };
        return if candidate.is_file() {
            Ok(EngineSource::Executable(candidate))
        } else {
            Err(EngineMissing(Some(path)))
        };
    }
    if development {
        return Ok(EngineSource::Development);
    }
    if let Some(engine) = store
        .map(|dir| dir.join(ENGINE_FILE))
        .filter(|p| p.is_file())
    {
        return Ok(EngineSource::Executable(engine));
    }
    let packaged = packaged_engine(executable_dir);
    if packaged.is_file() {
        Ok(EngineSource::Executable(packaged))
    } else {
        Err(EngineMissing(None))
    }
}

fn development_command() -> Command {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..");
    if cfg!(windows) {
        let mut command = Command::new(root.join("engine/.venv/Scripts/python.exe"));
        command
            .current_dir(root.join("engine"))
            .args(["-m", "murmur_engine"]);
        command
    } else {
        Command::new(root.join("scripts/engine-sidecar.sh"))
    }
}

fn sidecar_command(locate: &dyn Locate) -> Result<Command> {
    let executable = std::env::current_exe().context("Locate Murmur executable")?;
    let parent = executable.parent().context("Executable has no parent")?;
    let override_path = std::env::var_os(ENGINE_ENV).map(PathBuf::from);
    match resolve_engine(
        override_path,
        parent,
        cfg!(debug_assertions),
        locate.engine_dir(),
    )? {
        EngineSource::Development => Ok(development_command()),
        EngineSource::Executable(path) => {
            let mut command = Command::new(path);
            #[cfg(target_os = "linux")]
            configure_linux_cuda(&mut command, parent, locate.cuda_dir())?;
            // The Windows engine adds this directory with os.add_dll_directory.
            #[cfg(windows)]
            if let Some(cuda) = locate.cuda_dir() {
                command.env("MURMUR_CUDA_DIR", cuda);
            }
            Ok(command)
        }
    }
}

/// CUDA libraries: the active GPU add-on component, else a packaged `lib/murmur/cuda`.
#[cfg(target_os = "linux")]
fn configure_linux_cuda(
    command: &mut Command,
    executable_dir: &Path,
    store: Option<PathBuf>,
) -> Result<()> {
    use std::{collections::HashSet, env, ffi::OsString};
    let cuda = match store.filter(|dir| dir.is_dir()) {
        Some(dir) => dir,
        None => executable_dir
            .parent()
            .context("Executable has no runtime parent")?
            .join("lib/murmur/cuda"),
    };
    if !cuda.is_dir() {
        return Ok(());
    }
    let mut seen = HashSet::<OsString>::new();
    let inherited = env::var_os("LD_LIBRARY_PATH")
        .map(|value| env::split_paths(&value).collect::<Vec<_>>())
        .unwrap_or_default();
    let paths = std::iter::once(cuda)
        .chain(inherited)
        .filter(|path| !path.as_os_str().is_empty() && seen.insert(path.as_os_str().to_owned()));
    command.env(
        "LD_LIBRARY_PATH",
        env::join_paths(paths).context("Join engine library paths")?,
    );
    Ok(())
}

#[cfg(all(test, target_os = "linux"))]
mod cuda_launch_tests {
    use super::configure_linux_cuda;
    use std::{fs, process::Command};

    fn root() -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "murmur cuda launch {} {:?}",
            std::process::id(),
            std::thread::current().id()
        ))
    }

    #[test]
    fn release_command_scopes_cuda_path_to_engine() {
        let root = root();
        let bin = root.join("usr/bin");
        let cuda = root.join("usr/lib/murmur/cuda");
        fs::create_dir_all(&bin).unwrap();
        fs::create_dir_all(&cuda).unwrap();
        let mut command = Command::new(bin.join("murmur-engine"));
        configure_linux_cuda(&mut command, &bin, None).unwrap();
        let value = command
            .get_envs()
            .find(|(name, _)| *name == "LD_LIBRARY_PATH")
            .and_then(|(_, value)| value)
            .unwrap();
        assert_eq!(std::env::split_paths(value).next().unwrap(), cuda);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn absent_cuda_keeps_cpu_launch() {
        let root = root();
        let mut command = Command::new("murmur-engine");
        configure_linux_cuda(&mut command, &root, None).unwrap();
        assert!(command
            .get_envs()
            .all(|(name, _)| name != "LD_LIBRARY_PATH"));
    }

    #[test]
    fn cuda_component_wins_over_packaged_runtime() {
        let root = root().with_extension("component");
        let bin = root.join("usr/bin");
        let packaged = root.join("usr/lib/murmur/cuda");
        let component = root.join("data/components/cuda/0.5.0");
        fs::create_dir_all(&bin).unwrap();
        fs::create_dir_all(&packaged).unwrap();
        fs::create_dir_all(&component).unwrap();
        let mut command = Command::new(bin.join("murmur-engine"));
        configure_linux_cuda(&mut command, &bin, Some(component.clone())).unwrap();
        let value = command
            .get_envs()
            .find(|(n, _)| *n == "LD_LIBRARY_PATH")
            .and_then(|(_, v)| v)
            .unwrap();
        let paths: Vec<_> = std::env::split_paths(value).collect();
        assert_eq!(paths[0], component);
        assert!(
            !paths.contains(&packaged),
            "only one CUDA runtime on the loader path"
        );
        fs::remove_dir_all(root).unwrap();
    }
}
#[cfg(test)]
mod resolution_tests {
    use super::{
        missing_engine, packaged_engine, resolve_engine, EngineMissing, EngineSource, ENGINE_FILE,
    };
    use std::{fs, path::PathBuf};

    struct Layout(PathBuf);
    impl Layout {
        fn new(name: &str) -> Self {
            let root = std::env::temp_dir().join(format!(
                "murmur engine resolution {name} {}",
                std::process::id()
            ));
            let _ = fs::remove_dir_all(&root);
            fs::create_dir_all(root.join("usr/bin")).unwrap();
            Self(root)
        }
        fn bin(&self) -> PathBuf {
            self.0.join("usr/bin")
        }
        fn install_packaged(&self) -> PathBuf {
            let engine = packaged_engine(&self.bin());
            fs::create_dir_all(engine.parent().unwrap()).unwrap();
            fs::write(&engine, b"engine").unwrap();
            engine
        }
    }
    impl Drop for Layout {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn override_file_wins_over_development_and_packaged() {
        let layout = Layout::new("override-file");
        layout.install_packaged();
        let custom = layout.0.join("custom-engine");
        fs::write(&custom, b"engine").unwrap();
        for development in [true, false] {
            assert_eq!(
                resolve_engine(Some(custom.clone()), &layout.bin(), development, None).unwrap(),
                EngineSource::Executable(custom.clone())
            );
        }
    }

    #[test]
    fn override_directory_names_its_engine() {
        let layout = Layout::new("override-dir");
        let directory = layout.0.join("engine-dir");
        fs::create_dir_all(&directory).unwrap();
        fs::write(directory.join(ENGINE_FILE), b"engine").unwrap();
        assert_eq!(
            resolve_engine(Some(directory.clone()), &layout.bin(), false, None).unwrap(),
            EngineSource::Executable(directory.join(ENGINE_FILE))
        );
    }

    #[test]
    fn broken_override_is_reported_not_skipped() {
        let layout = Layout::new("override-missing");
        layout.install_packaged();
        let missing = layout.0.join("nowhere");
        let error = resolve_engine(Some(missing.clone()), &layout.bin(), false, None).unwrap_err();
        assert!(error.to_string().starts_with(super::MISSING_MESSAGE));
        assert!(error.to_string().contains(&missing.display().to_string()));
    }

    #[test]
    fn empty_override_is_ignored() {
        let layout = Layout::new("override-empty");
        assert_eq!(
            resolve_engine(Some(PathBuf::new()), &layout.bin(), true, None).unwrap(),
            EngineSource::Development
        );
    }

    #[test]
    fn development_builds_use_the_checkout_engine() {
        let layout = Layout::new("development");
        layout.install_packaged();
        assert_eq!(
            resolve_engine(None, &layout.bin(), true, None).unwrap(),
            EngineSource::Development
        );
    }

    #[test]
    fn release_builds_use_the_packaged_onedir_engine() {
        let layout = Layout::new("packaged");
        let engine = layout.install_packaged();
        if cfg!(windows) {
            assert!(engine.ends_with("usr/bin/engine/murmur-engine.exe"));
        } else {
            assert!(engine.ends_with("usr/bin/../lib/murmur/engine/murmur-engine"));
        }
        assert_eq!(
            resolve_engine(None, &layout.bin(), false, None).unwrap(),
            EngineSource::Executable(engine)
        );
    }

    #[test]
    fn missing_engine_is_classified_as_permanent() {
        let absent = anyhow::Error::new(EngineMissing(None));
        assert_eq!(
            missing_engine(&absent).as_deref(),
            Some(super::MISSING_MESSAGE)
        );
        assert_eq!(missing_engine(&anyhow::anyhow!("spawn failed")), None);
    }

    #[test]
    fn release_without_engine_reports_missing() {
        let layout = Layout::new("absent");
        let error = resolve_engine(None, &layout.bin(), false, None).unwrap_err();
        assert_eq!(
            error.to_string(),
            "Speech engine not installed — open Settings"
        );
    }

    #[test]
    fn component_engine_wins_over_packaged_but_not_override_or_venv() {
        let layout = Layout::new("component");
        let packaged = layout.install_packaged();
        let store = layout.0.join("data/components/engine/0.5.0");
        fs::create_dir_all(&store).unwrap();
        // An engine directory without the executable is ignored.
        assert_eq!(
            resolve_engine(None, &layout.bin(), false, Some(store.clone())).unwrap(),
            EngineSource::Executable(packaged)
        );
        fs::write(store.join(ENGINE_FILE), b"engine").unwrap();
        assert_eq!(
            resolve_engine(None, &layout.bin(), false, Some(store.clone())).unwrap(),
            EngineSource::Executable(store.join(ENGINE_FILE))
        );
        assert_eq!(
            resolve_engine(None, &layout.bin(), true, Some(store.clone())).unwrap(),
            EngineSource::Development
        );
        let custom = layout.0.join("custom-engine");
        fs::write(&custom, b"engine").unwrap();
        assert_eq!(
            resolve_engine(Some(custom.clone()), &layout.bin(), false, Some(store)).unwrap(),
            EngineSource::Executable(custom)
        );
    }
}
pub struct Engine {
    locate: Arc<dyn Locate>,
    sender: mpsc::Sender<Message>,
    job: Mutex<Option<JoinHandle<()>>>,
    alive: Arc<AtomicBool>,
    stopping: Arc<AtomicBool>,
    failure: Arc<Mutex<Option<String>>>,
}
impl Engine {
    pub fn new(models: PathBuf, app: AppHandle, locate: Arc<dyn Locate>) -> Result<Self> {
        let (sender, receiver) = mpsc::channel();
        let lookup = locate.clone();
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
                // Set when no engine executable exists; cleared when a component install
                // restarts the engine.
                let mut missing: Option<String> = None;
                // Engine files are being replaced: do not spawn until Restart.
                let mut held = false;
                loop {
                    if stop.load(Ordering::Acquire) {
                        break;
                    }
                    if process.is_none() && failures < 2 && missing.is_none() && !held {
                        match Process::spawn(&models, lookup.as_ref()) {
                            Ok(p) => {
                                process = Some(p);
                                live.store(true, Ordering::Release);
                                if let Ok(mut error) = errors.lock() {
                                    *error = None;
                                }
                            }
                            Err(error) => match missing_engine(&error) {
                                Some(message) => {
                                    report_missing(&app, &errors, &message);
                                    missing = Some(message);
                                }
                                None => {
                                    failures += 1;
                                    report(&app, &errors, &format!("{error:#}"), failures);
                                }
                            },
                        }
                    }
                    match receiver.recv_timeout(Duration::from_millis(200)) {
                        Ok(Message::Quit) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
                        Ok(Message::Stop(done)) => {
                            held = true;
                            if let Some(mut p) = process.take() {
                                shutdown_process(&mut p, &app);
                            }
                            live.store(false, Ordering::Release);
                            let _ = done.send(());
                        }
                        Ok(Message::Restart) => {
                            held = false;
                            failures = 0;
                            missing = None;
                            if let Some(mut p) = process.take() {
                                shutdown_process(&mut p, &app);
                            }
                            live.store(false, Ordering::Release);
                            if let Ok(mut error) = errors.lock() {
                                *error = None;
                            }
                        }
                        Ok(Message::Request(request)) => {
                            let result = match process.as_mut() {
                                Some(p) => p.call(&request, &app, &stop),
                                None if held => {
                                    Err(anyhow::anyhow!("The speech engine is being updated"))
                                }
                                None => {
                                    Err(anyhow::anyhow!(missing.clone().unwrap_or_else(|| {
                                        "Engine unavailable after two failures; restart Murmur"
                                            .into()
                                    })))
                                }
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
                            let dead = process
                                .as_mut()
                                .is_some_and(|p| !matches!(p.child.try_wait(), Ok(None)));
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
                    shutdown_process(p, &app);
                }
                drop(process);
                live.store(false, Ordering::Release);
            })
            .context("Start engine supervisor")?;
        Ok(Self {
            locate,
            sender,
            job: Mutex::new(Some(job)),
            alive,
            stopping,
            failure,
        })
    }
    /// Stop the engine and keep it stopped until `restart` (blocking, bounded at 20 s).
    /// True once the supervisor confirms no engine process is left (it waits for the child to
    /// exit), or when the supervisor is gone and so is its child.
    pub fn stop_and_wait(&self) -> bool {
        let (done, wait) = mpsc::channel();
        if self.sender.send(Message::Stop(done)).is_err() {
            return true;
        }
        wait.recv_timeout(Duration::from_secs(20)).is_ok()
    }
    /// Spawn again with the current files: the reset path after a component change.
    pub fn restart(&self) {
        let _ = self.sender.send(Message::Restart);
    }
    pub fn request(&self, method: &str, mut params: Value) -> Result<Value> {
        if self.stopping.load(Ordering::Acquire) {
            bail!("Engine is shutting down");
        }
        attach_model_path(self.locate.as_ref(), method, &mut params);
        let timeout = Duration::from_secs(if method == "models.install" {
            1800
        } else {
            120
        });
        let (reply, response) = mpsc::channel();
        self.sender
            .send(Message::Request(Request {
                method: method.into(),
                params,
                timeout,
                reply,
            }))
            .context("Engine supervisor stopped")?;
        let value = response
            .recv_timeout(timeout + Duration::from_secs(5))
            .context("Engine request queue timed out")??;
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
/// Requests that name a model carry the verified directory (or null) from the store.
fn attach_model_path(locate: &dyn Locate, method: &str, params: &mut Value) {
    if !matches!(method, "status" | "warm" | "transcribe" | "partial") {
        return;
    }
    if let Some(model) = params
        .get("model")
        .and_then(Value::as_str)
        .map(str::to_owned)
    {
        params["model_path"] = match locate.model_dir(&model) {
            Some(dir) => json!(dir.to_string_lossy()),
            None => Value::Null,
        };
    }
}
fn shutdown_process(process: &mut Process, app: &AppHandle) {
    let (reply, _) = mpsc::channel();
    let _ = process.call(
        &Request {
            method: "shutdown".into(),
            params: json!({}),
            timeout: Duration::from_secs(2),
            reply,
        },
        app,
        &AtomicBool::new(false),
    );
}
#[cfg(test)]
mod model_path_tests {
    use super::{attach_model_path, Locate};
    use serde_json::json;
    use std::path::PathBuf;
    struct Fixed;
    impl Locate for Fixed {
        fn engine_dir(&self) -> Option<PathBuf> {
            None
        }
        fn cuda_dir(&self) -> Option<PathBuf> {
            None
        }
        fn model_dir(&self, model: &str) -> Option<PathBuf> {
            (model == "tiny.en").then(|| PathBuf::from("/data/components/speech-tiny.en/abc"))
        }
    }
    #[test]
    fn model_requests_carry_verified_path_or_null() {
        let mut params = json!({"model": "tiny.en"});
        attach_model_path(&Fixed, "transcribe", &mut params);
        assert_eq!(params["model_path"], "/data/components/speech-tiny.en/abc");
        let mut params = json!({"model": "base.en"});
        attach_model_path(&Fixed, "warm", &mut params);
        assert!(
            params["model_path"].is_null(),
            "unverified or absent: never a guessed path"
        );
        let mut params = json!({"name": "tiny.en"});
        attach_model_path(&Fixed, "models.list", &mut params);
        assert!(params.get("model_path").is_none());
    }
}
fn missing_engine(error: &anyhow::Error) -> Option<String> {
    error
        .downcast_ref::<EngineMissing>()
        .map(ToString::to_string)
}
fn report_missing(app: &AppHandle, error: &Mutex<Option<String>>, message: &str) {
    tracing::warn!(target: "murmur::engine", "{message}");
    if let Ok(mut error) = error.lock() {
        *error = Some(message.to_owned());
    }
    let _ = app.emit("error", json!({"message": message, "persistent": true}));
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
    let _ = app.emit(
        "error",
        json!({"message": message, "persistent": failures >= 2}),
    );
}
