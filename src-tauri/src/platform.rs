use anyhow::{bail, Context, Result};
use std::{
    env,
    fs::{self, File, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    process::Command,
    sync::{Arc, Mutex},
};
use tauri::{AppHandle, Manager};
use tauri_plugin_autostart::ManagerExt;
use tracing_subscriber::{filter::filter_fn, prelude::*};
#[derive(Clone)]
pub struct Paths {
    pub config: PathBuf,
    pub data: PathBuf,
    pub models: PathBuf,
    /// Enhance model store, separate from the Whisper models directory.
    pub enhance_models: PathBuf,
    pub history: PathBuf,
}
impl Paths {
    pub fn discover() -> Result<Self> {
        #[cfg(windows)]
        let (config, data) = (
            env_path("APPDATA")?.join("Murmur"),
            env_path("LOCALAPPDATA")?.join("Murmur"),
        );
        #[cfg(not(windows))]
        let (config, data) = {
            let home = env_path("HOME")?;
            (
                env::var_os("XDG_CONFIG_HOME")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| home.join(".config"))
                    .join("murmur"),
                env::var_os("XDG_DATA_HOME")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| home.join(".local/share"))
                    .join("murmur"),
            )
        };
        for dir in [&config, &data] {
            fs::create_dir_all(dir).with_context(|| format!("Create {}", dir.display()))?;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            for dir in [&config, &data] {
                fs::set_permissions(dir, fs::Permissions::from_mode(0o700))
                    .context("Protect app data directory")?;
            }
        }
        Ok(Self {
            config: config.join("config.toml"),
            models: data.join("models"),
            enhance_models: data.join("enhance").join("models"),
            history: data.join("history.sqlite"),
            data,
        })
    }
}
/// Bytes available to this user on the filesystem holding `path` (or its nearest existing
/// ancestor). `None` when it cannot be determined; callers then skip the pre-check.
pub fn available_space(path: &Path) -> Option<u64> {
    let mut probe = path.to_path_buf();
    while !probe.exists() {
        probe = probe.parent()?.to_path_buf();
    }
    available_space_at(&probe)
}
#[cfg(unix)]
fn available_space_at(path: &Path) -> Option<u64> {
    use std::os::unix::ffi::OsStrExt;
    let c = std::ffi::CString::new(path.as_os_str().as_bytes()).ok()?;
    let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statvfs(c.as_ptr(), &mut stat) } != 0 {
        return None;
    }
    #[allow(clippy::unnecessary_cast)]
    Some((stat.f_bavail as u64).saturating_mul(stat.f_frsize as u64))
}
#[cfg(windows)]
fn available_space_at(path: &Path) -> Option<u64> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW;
    let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let mut free: u64 = 0;
    if unsafe {
        GetDiskFreeSpaceExW(
            wide.as_ptr(),
            &mut free,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    } == 0
    {
        return None;
    }
    Some(free)
}
#[cfg(not(any(unix, windows)))]
fn available_space_at(_: &Path) -> Option<u64> {
    None
}
fn env_path(key: &str) -> Result<PathBuf> {
    Ok(PathBuf::from(
        env::var_os(key).with_context(|| format!("{key} is unset"))?,
    ))
}
pub fn open_settings(app: &AppHandle) -> Result<()> {
    let window = app
        .get_webview_window("settings")
        .context("Settings window missing")?;
    window.show().context("Show settings")?;
    window.unminimize().context("Restore settings")?;
    window.set_focus().context("Focus settings")?;
    Ok(())
}
pub fn set_autostart(app: &AppHandle, enabled: bool) -> Result<()> {
    let manager = app.autolaunch();
    if manager.is_enabled().context("Read autostart state")? != enabled {
        if enabled {
            manager.enable()
        } else {
            manager.disable()
        }
        .context("Update autostart")?;
    }
    Ok(())
}
pub fn focused_app(app: &AppHandle) -> Result<String> {
    for window in app.webview_windows().values() {
        if window.is_focused().unwrap_or(false) {
            return Ok("murmur".into());
        }
    }
    native_focus()
}
#[cfg(target_os = "linux")]
fn native_focus() -> Result<String> {
    if env::var_os("DISPLAY").is_none() {
        bail!("Text injection requires X11; Wayland is untested");
    }
    let output = Command::new("xprop")
        .args(["-root", "_NET_ACTIVE_WINDOW"])
        .output()
        .context("Read active X11 window (xprop)")?;
    if !output.status.success() {
        bail!("Cannot read active window; Wayland may restrict injection");
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let id = text
        .split_whitespace()
        .last()
        .filter(|id| id.starts_with("0x") && *id != "0x0")
        .context("No active X11 window")?;
    let output = Command::new("xprop")
        .args(["-id", id, "WM_CLASS"])
        .output()
        .context("Read focused application class")?;
    if !output.status.success() {
        bail!("Focused application disappeared");
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let class = text
        .split('"')
        .nth(3)
        .or_else(|| text.split('"').nth(1))
        .context("Focused window has no WM_CLASS")?;
    Ok(class.to_owned())
}
#[cfg(windows)]
fn native_focus() -> Result<String> {
    use windows_sys::Win32::{
        Foundation::CloseHandle,
        System::Threading::{
            OpenProcess, QueryFullProcessImageNameW, PROCESS_QUERY_LIMITED_INFORMATION,
        },
        UI::WindowsAndMessaging::{GetForegroundWindow, GetWindowThreadProcessId},
    };
    unsafe {
        let window = GetForegroundWindow();
        let mut pid = 0;
        if window.is_null() {
            bail!("No foreground window");
        }
        GetWindowThreadProcessId(window, &mut pid);
        let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if process.is_null() {
            return Err(io::Error::last_os_error()).context("Open foreground process");
        }
        let mut buffer = vec![0u16; 32768];
        let mut length = buffer.len() as u32;
        let result = QueryFullProcessImageNameW(process, 0, buffer.as_mut_ptr(), &mut length);
        let error = io::Error::last_os_error();
        CloseHandle(process);
        if result == 0 {
            return Err(error).context("Read focused process path");
        }
        let path = PathBuf::from(String::from_utf16_lossy(&buffer[..length as usize]));
        Ok(path
            .file_name()
            .context("Foreground executable has no name")?
            .to_string_lossy()
            .into_owned())
    }
}
#[cfg(not(any(windows, target_os = "linux")))]
fn native_focus() -> Result<String> {
    bail!("Unsupported platform")
}
pub fn open_path(path: &Path) -> Result<()> {
    fs::create_dir_all(path).context("Create app folder")?;
    #[cfg(windows)]
    let mut command = Command::new("explorer.exe");
    #[cfg(not(windows))]
    let mut command = Command::new("xdg-open");
    let status = command
        .arg(path)
        .status()
        .context("Open application folder")?;
    if !status.success() {
        bail!("Folder opener failed: {status}");
    }
    Ok(())
}
#[allow(dead_code)]
pub fn spawn_new_instance() -> Result<()> {
    Command::new(env::current_exe().context("Locate Murmur executable")?)
        .spawn()
        .context("Launch second instance")?
        .wait()
        .context("Wait for second-instance handoff")?;
    Ok(())
}
struct Rolling {
    path: PathBuf,
    file: File,
    size: u64,
}
impl Rolling {
    fn write(&mut self, data: &[u8]) -> io::Result<usize> {
        if self.size + data.len() as u64 > 2 * 1024 * 1024 {
            self.file.flush()?;
            let second = self.path.with_extension("log.2");
            let first = self.path.with_extension("log.1");
            if second.exists() {
                fs::remove_file(&second)?;
            }
            if first.exists() {
                fs::rename(&first, second)?;
            }
            // Copy + truncate works with the open log handle on Windows too.
            fs::copy(&self.path, first)?;
            self.file.set_len(0)?;
            self.size = 0;
        }
        let count = self.file.write(data)?;
        self.size += count as u64;
        Ok(count)
    }
}
#[derive(Clone)]
struct LogWriter(Arc<Mutex<Rolling>>);
impl Write for LogWriter {
    fn write(&mut self, data: &[u8]) -> io::Result<usize> {
        self.0
            .lock()
            .map_err(|_| io::Error::other("Log lock poisoned"))?
            .write(data)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.0
            .lock()
            .map_err(|_| io::Error::other("Log lock poisoned"))?
            .file
            .flush()
    }
}
pub fn init_logging(data: &Path) -> Result<()> {
    let path = data.join("murmur.log");
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .context("Open diagnostic log")?;
    let size = file.metadata().context("Read log size")?.len();
    let writer = LogWriter(Arc::new(Mutex::new(Rolling { path, file, size })));
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::fmt::layer()
                .with_ansi(false)
                .with_writer(move || writer.clone())
                .with_filter(
                    tracing_subscriber::EnvFilter::try_from_env("MURMUR_LOG")
                        .unwrap_or_else(|_| "info".into()),
                )
                .with_filter(filter_fn(payload_safe_for_metadata)),
        )
        .try_init()
        .map_err(|e| anyhow::anyhow!("Initialize logs: {e}"))?;
    Ok(())
}
fn payload_safe_for_metadata(metadata: &tracing::Metadata<'_>) -> bool {
    let target = metadata.target();
    if target == "tungstenite"
        || target.starts_with("tungstenite::")
        || target == "tokio_tungstenite"
        || target.starts_with("tokio_tungstenite::")
    {
        *metadata.level() <= tracing::Level::WARN
    } else {
        true
    }
}

#[cfg(test)]
mod logging_tests {
    use super::*;

    #[derive(Clone)]
    struct Capture(Arc<Mutex<Vec<u8>>>);
    impl Write for Capture {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn auth_token_is_not_formatted_by_websocket_targets() {
        let output = Capture(Arc::new(Mutex::new(Vec::new())));
        let subscriber = tracing_subscriber::registry().with(
            tracing_subscriber::fmt::layer()
                .with_ansi(false)
                .with_writer({
                    let output = output.clone();
                    move || output.clone()
                })
                .with_filter(tracing_subscriber::EnvFilter::new(
                    "trace,tungstenite::protocol=trace,tokio_tungstenite::protocol=trace",
                ))
                .with_filter(filter_fn(payload_safe_for_metadata)),
        );
        let token = "AUTH OK secret-token-probe";
        tracing::subscriber::with_default(subscriber, || {
            tracing::trace!(target: "tungstenite::protocol", "{token}");
            tracing::trace!(target: "tokio_tungstenite::protocol", "{token}");
            tracing::info!(target: "murmur", "application log probe");
        });
        let formatted = String::from_utf8(output.0.lock().unwrap().clone()).unwrap();
        assert!(formatted.contains("application log probe"));
        assert!(!formatted.contains(token));
    }
}
