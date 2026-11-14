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
#[cfg(not(target_os = "linux"))]
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
#[cfg(target_os = "linux")]
pub fn set_autostart(app: &AppHandle, enabled: bool) -> Result<()> {
    let home = env_path("HOME")?;
    let config = env::var_os("XDG_CONFIG_HOME")
        .filter(|path| !path.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config"));
    let runtime = app.env();
    let executable = crate::autostart::launcher(
        runtime.appimage.map(PathBuf::from),
        runtime.appdir.map(PathBuf::from),
        env::current_exe().context("Find Murmur executable")?,
    );
    crate::autostart::update(&home, &config, &executable, enabled)
}
#[cfg(not(target_os = "linux"))]
pub fn set_autostart(app: &AppHandle, enabled: bool) -> Result<()> {
    let manager = app.autolaunch();
    if enabled {
        // Refresh existing registrations too: older versions did not include
        // --background, and an update can change the executable's location.
        manager.enable().context("Update autostart")?;
    } else if manager.is_enabled().context("Read autostart state")? {
        manager.disable().context("Update autostart")?;
    }
    Ok(())
}
/// Why Murmur must not type into the focused window. The focus contract holds the transcript
/// (Copy / Discard) instead of sending input that would be dropped or land somewhere unexpected.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(not(windows), allow(dead_code))] // Constructed by the Windows focus reader only.
pub enum Blocked {
    /// Windows: the target runs at a higher integrity level than Murmur (for example "Run as
    /// administrator"); UIPI silently drops synthetic input to it.
    Elevated,
    /// Windows: the target's integrity level could not be read, so delivery cannot be confirmed.
    Unreadable,
    /// Windows: the secure desktop (UAC prompt, lock screen, Ctrl+Alt+Del) has the input.
    SecureDesktop,
}
impl Blocked {
    /// Shown with the held transcript.
    pub fn message(self) -> &'static str {
        match self {
            Blocked::Elevated => {
                "Not typed: the window runs as administrator, and Windows blocks typing into it from Murmur. Copy the text instead."
            }
            Blocked::Unreadable => {
                "Not typed: Murmur could not confirm it may type into this window. Copy the text instead."
            }
            Blocked::SecureDesktop => {
                "Not typed: a Windows security prompt or the lock screen had focus. Copy the text instead."
            }
        }
    }
}
/// The window that has keyboard focus: one of Murmur's own windows, a native window, or a window
/// Murmur cannot deliver to. Native windows are identified by their X11 window ID (Linux) or
/// HWND plus owning process ID and process start time (Windows; HWNDs and PIDs are reused).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Focus {
    Own,
    Window {
        id: u64,
        /// Owning process (Windows). `None` on Linux, where the X11 id alone is compared.
        pid: Option<u32>,
        /// Owning process creation time as a FILETIME (Windows), so a reused PID differs.
        started: Option<u64>,
        class: String,
    },
    /// Focused, but synthetic input would not arrive (elevated target, secure desktop).
    Blocked {
        reason: Blocked,
        class: String,
    },
}
impl Focus {
    /// Application class used for style, exclusion and terminal decisions.
    pub fn class(&self) -> &str {
        match self {
            Focus::Own => "murmur",
            Focus::Window { class, .. } | Focus::Blocked { class, .. } => class,
        }
    }
}
/// What the platform layer reads about the foreground window.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Native {
    id: u64,
    pid: Option<u32>,
    started: Option<u64>,
    class: String,
    blocked: Option<Blocked>,
}
pub fn focused_target(app: &AppHandle) -> Result<Focus> {
    for window in app.webview_windows().values() {
        if window.is_focused().unwrap_or(false) {
            return Ok(Focus::Own);
        }
    }
    let native = native_focus()?;
    if crate::inject::is_own_app(&native.class) {
        return Ok(Focus::Own);
    }
    Ok(match native.blocked {
        Some(reason) => Focus::Blocked {
            reason,
            class: native.class,
        },
        None => Focus::Window {
            id: native.id,
            pid: native.pid,
            started: native.started,
            class: native.class,
        },
    })
}
pub fn focused_app(app: &AppHandle) -> Result<String> {
    focused_target(app).map(|focus| focus.class().to_owned())
}
/// Bring a previously focused window back to the foreground and confirm it took focus.
/// Bounded at roughly 500 ms so a window manager that refuses activation cannot stall delivery.
/// The caller re-checks the full identity (PID and start time too) before delivering.
pub fn activate_window(id: u64) -> Result<()> {
    native_activate(id)?;
    for _ in 0..25 {
        if native_focus().is_ok_and(|focused| focused.id == id) {
            return Ok(());
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    bail!("The original window did not regain focus")
}
/// Focus contract: deliver only when the target's integrity level is known and not above
/// Murmur's own. UIPI drops input sent to a higher level without a reliable error.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn integrity_block(target: Option<u32>, own: Option<u32>) -> Option<Blocked> {
    match (target, own) {
        (Some(target), Some(own)) if target <= own => None,
        (Some(_), Some(_)) => Some(Blocked::Elevated),
        _ => Some(Blocked::Unreadable),
    }
}
/// Focus contract: input goes only to the normal interactive desktop ("Default"). A failed
/// `OpenInputDesktop` (`None`) or any other name ("Winlogon", "Screen-saver") is the secure desktop.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn desktop_block(input_desktop: Option<&str>) -> Option<Blocked> {
    match input_desktop {
        Some(name) if name.eq_ignore_ascii_case("Default") => None,
        _ => Some(Blocked::SecureDesktop),
    }
}
#[cfg(target_os = "linux")]
fn parse_window_id(xprop: &str) -> Option<u64> {
    let id = xprop.split_whitespace().last()?.strip_prefix("0x")?;
    u64::from_str_radix(id, 16).ok().filter(|id| *id != 0)
}
#[cfg(target_os = "linux")]
fn native_focus() -> Result<Native> {
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
    let id = parse_window_id(&String::from_utf8_lossy(&output.stdout))
        .context("No active X11 window")?;
    let output = Command::new("xprop")
        .args(["-id", &id.to_string(), "WM_CLASS"])
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
    // X11 ids are not reused while the window exists; the PID is not compared here.
    Ok(Native {
        id,
        pid: None,
        started: None,
        class: class.to_owned(),
        blocked: None,
    })
}
#[cfg(target_os = "linux")]
fn native_activate(id: u64) -> Result<()> {
    // No --sync: it waits without a deadline when the window manager declines the request.
    let status = Command::new("xdotool")
        .args(["windowactivate", &id.to_string()])
        .status()
        .context("Run xdotool to refocus the original window")?;
    if !status.success() {
        bail!("xdotool windowactivate exited with status {status}");
    }
    Ok(())
}
/// Windows foreground window: HWND, PID, process start time and executable name, plus whether
/// Murmur may type into it (secure desktop, integrity level). B9/B10: verified in source, not run
/// on hardware.
#[cfg(windows)]
fn native_focus() -> Result<Native> {
    use windows_sys::Win32::{
        Foundation::CloseHandle,
        System::Threading::{
            GetCurrentProcess, OpenProcess, QueryFullProcessImageNameW,
            PROCESS_QUERY_LIMITED_INFORMATION,
        },
        UI::WindowsAndMessaging::{GetForegroundWindow, GetWindowThreadProcessId},
    };
    if let Some(reason) = desktop_block(win::input_desktop_name().as_deref()) {
        return Ok(Native {
            id: 0,
            pid: None,
            started: None,
            class: String::new(),
            blocked: Some(reason),
        });
    }
    unsafe {
        let window = GetForegroundWindow();
        let mut pid = 0;
        if window.is_null() {
            bail!("No foreground window");
        }
        GetWindowThreadProcessId(window, &mut pid);
        if pid == 0 {
            bail!("Foreground window has no process");
        }
        let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if process.is_null() {
            return Err(io::Error::last_os_error()).context("Open foreground process");
        }
        let mut buffer = vec![0u16; 32768];
        let mut length = buffer.len() as u32;
        let result = QueryFullProcessImageNameW(process, 0, buffer.as_mut_ptr(), &mut length);
        let error = io::Error::last_os_error();
        let started = win::start_time(process);
        let target_level = win::integrity_level(process);
        CloseHandle(process);
        if result == 0 {
            return Err(error).context("Read focused process path");
        }
        let path = PathBuf::from(String::from_utf16_lossy(&buffer[..length as usize]));
        let name = path
            .file_name()
            .context("Foreground executable has no name")?
            .to_string_lossy()
            .into_owned();
        let own_level = win::integrity_level(GetCurrentProcess());
        Ok(Native {
            id: window as usize as u64,
            pid: Some(pid),
            started,
            class: name,
            blocked: integrity_block(target_level, own_level),
        })
    }
}
#[cfg(windows)]
mod win {
    use windows_sys::Win32::{
        Foundation::{CloseHandle, FILETIME, HANDLE},
        Security::{
            GetSidSubAuthority, GetSidSubAuthorityCount, GetTokenInformation, TokenIntegrityLevel,
            TOKEN_MANDATORY_LABEL, TOKEN_QUERY,
        },
        System::{
            StationsAndDesktops::{
                CloseDesktop, GetUserObjectInformationW, OpenInputDesktop, DESKTOP_READOBJECTS,
                UOI_NAME,
            },
            Threading::{GetProcessTimes, OpenProcessToken},
        },
    };
    /// Name of the desktop receiving input; `None` when it cannot be opened, which is what a
    /// normal process sees while the secure desktop is up.
    pub fn input_desktop_name() -> Option<String> {
        unsafe {
            let desktop = OpenInputDesktop(0, 0, DESKTOP_READOBJECTS);
            if desktop.is_null() {
                return None;
            }
            let mut name = [0u16; 256];
            let mut needed = 0u32;
            let ok = GetUserObjectInformationW(
                desktop as HANDLE,
                UOI_NAME,
                name.as_mut_ptr().cast(),
                (name.len() * 2) as u32,
                &mut needed,
            );
            CloseDesktop(desktop);
            if ok == 0 {
                return None;
            }
            let end = name.iter().position(|&c| c == 0).unwrap_or(name.len());
            Some(String::from_utf16_lossy(&name[..end]))
        }
    }
    /// Mandatory integrity RID of a process token; `None` when the token cannot be read
    /// (typical for an elevated process seen from a standard one).
    pub fn integrity_level(process: HANDLE) -> Option<u32> {
        unsafe {
            let mut token: HANDLE = std::ptr::null_mut();
            if OpenProcessToken(process, TOKEN_QUERY, &mut token) == 0 {
                return None;
            }
            let mut size = 0u32;
            GetTokenInformation(
                token,
                TokenIntegrityLevel,
                std::ptr::null_mut(),
                0,
                &mut size,
            );
            // u64 storage keeps TOKEN_MANDATORY_LABEL's pointer field aligned.
            let mut buffer = vec![0u64; (size as usize).div_ceil(8).max(1)];
            let ok = GetTokenInformation(
                token,
                TokenIntegrityLevel,
                buffer.as_mut_ptr().cast(),
                (buffer.len() * 8) as u32,
                &mut size,
            );
            CloseHandle(token);
            if ok == 0 {
                return None;
            }
            let label = &*(buffer.as_ptr() as *const TOKEN_MANDATORY_LABEL);
            let sid = label.Label.Sid;
            if sid.is_null() {
                return None;
            }
            let count = *GetSidSubAuthorityCount(sid);
            if count == 0 {
                return None;
            }
            Some(*GetSidSubAuthority(sid, u32::from(count) - 1))
        }
    }
    /// Process creation time (FILETIME as u64); distinguishes a reused PID.
    pub fn start_time(process: HANDLE) -> Option<u64> {
        let zero = FILETIME {
            dwLowDateTime: 0,
            dwHighDateTime: 0,
        };
        let (mut created, mut exited, mut kernel, mut user) = (zero, zero, zero, zero);
        unsafe {
            if GetProcessTimes(process, &mut created, &mut exited, &mut kernel, &mut user) == 0 {
                return None;
            }
        }
        Some(u64::from(created.dwHighDateTime) << 32 | u64::from(created.dwLowDateTime))
    }
}
#[cfg(windows)]
fn native_activate(id: u64) -> Result<()> {
    use windows_sys::Win32::UI::WindowsAndMessaging::{IsWindow, SetForegroundWindow};
    let window = id as usize as windows_sys::Win32::Foundation::HWND;
    unsafe {
        if IsWindow(window) == 0 {
            bail!("The original window was closed");
        }
        // Windows may refuse this under its foreground-lock rules; the caller then holds the text.
        if SetForegroundWindow(window) == 0 {
            bail!("Windows declined to refocus the original window");
        }
    }
    Ok(())
}
#[cfg(not(any(windows, target_os = "linux")))]
fn native_focus() -> Result<Native> {
    bail!("Unsupported platform")
}
#[cfg(not(any(windows, target_os = "linux")))]
fn native_activate(_: u64) -> Result<()> {
    bail!("Unsupported platform")
}
/// Windows microphone privacy switches (Settings → Privacy & security → Microphone). Desktop apps
/// such as Murmur are governed by the device switch, "Let apps access your microphone" and "Let
/// desktop apps access your microphone"; any of them off silences capture without an error.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
#[cfg_attr(not(windows), allow(dead_code))] // Read from the Windows registry only.
pub enum MicrophoneBlock {
    /// "Microphone access" for the whole device (HKLM).
    Device,
    /// "Let apps access your microphone" (HKCU).
    Apps,
    /// "Let desktop apps access your microphone" (HKCU …\NonPackaged).
    DesktopApps,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub struct MicrophoneAccess {
    /// `None`: not blocked by a privacy switch (or not Windows).
    pub blocked: Option<MicrophoneBlock>,
    /// The platform has a privacy page `open_microphone_settings` can open.
    pub settings_available: bool,
}
/// Decide from the `Value` strings of the CapabilityAccessManager consent store. A missing value
/// means the default, which is allowed.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn microphone_block(
    device: Option<&str>,
    apps: Option<&str>,
    desktop_apps: Option<&str>,
) -> Option<MicrophoneBlock> {
    let denied = |value: Option<&str>| value.is_some_and(|v| v.eq_ignore_ascii_case("Deny"));
    if denied(device) {
        Some(MicrophoneBlock::Device)
    } else if denied(apps) {
        Some(MicrophoneBlock::Apps)
    } else if denied(desktop_apps) {
        Some(MicrophoneBlock::DesktopApps)
    } else {
        None
    }
}
const MICROPHONE_CONSENT: &str =
    "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone";
#[cfg(windows)]
fn registry_string(root: windows_sys::Win32::System::Registry::HKEY, key: &str) -> Option<String> {
    use windows_sys::Win32::System::Registry::{RegGetValueW, RRF_RT_REG_SZ};
    let wide = |text: &str| text.encode_utf16().chain(Some(0)).collect::<Vec<u16>>();
    let (key, value) = (wide(key), wide("Value"));
    let mut buffer = [0u16; 64];
    let mut size = (buffer.len() * 2) as u32;
    let status = unsafe {
        RegGetValueW(
            root,
            key.as_ptr(),
            value.as_ptr(),
            RRF_RT_REG_SZ,
            std::ptr::null_mut(),
            buffer.as_mut_ptr().cast(),
            &mut size,
        )
    };
    if status != 0 {
        return None;
    }
    let end = buffer.iter().position(|&c| c == 0).unwrap_or(buffer.len());
    Some(String::from_utf16_lossy(&buffer[..end]))
}
/// Read the microphone privacy switches. Verified in source, not run on hardware.
pub fn microphone_access() -> MicrophoneAccess {
    #[cfg(windows)]
    {
        use windows_sys::Win32::System::Registry::{HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE};
        let desktop = format!("{MICROPHONE_CONSENT}\\NonPackaged");
        MicrophoneAccess {
            blocked: microphone_block(
                registry_string(HKEY_LOCAL_MACHINE, MICROPHONE_CONSENT).as_deref(),
                registry_string(HKEY_CURRENT_USER, MICROPHONE_CONSENT).as_deref(),
                registry_string(HKEY_CURRENT_USER, &desktop).as_deref(),
            ),
            settings_available: true,
        }
    }
    #[cfg(not(windows))]
    {
        let _ = MICROPHONE_CONSENT;
        MicrophoneAccess {
            blocked: None,
            settings_available: false,
        }
    }
}
/// Open Windows Settings at the microphone privacy page.
pub fn open_microphone_settings() -> Result<()> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOWNORMAL};
        let wide = |text: &str| text.encode_utf16().chain(Some(0)).collect::<Vec<u16>>();
        let (verb, target) = (wide("open"), wide("ms-settings:privacy-microphone"));
        let result = unsafe {
            ShellExecuteW(
                std::ptr::null_mut(),
                verb.as_ptr(),
                target.as_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                SW_SHOWNORMAL,
            )
        };
        // ShellExecuteW reports success with a value greater than 32.
        if result as isize <= 32 {
            bail!("Windows Settings did not open (code {})", result as isize);
        }
        Ok(())
    }
    #[cfg(not(windows))]
    bail!("Microphone privacy settings exist only on Windows")
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
    // enigo's Windows debug event formats `text(text: <entire transcript>)`.
    // Its target is outside Murmur, so our own metadata-only transcript logs
    // cannot prevent this payload from reaching murmur.log.
    if target == "enigo" || target.starts_with("enigo::") {
        false
    } else if target == "tungstenite"
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
mod delivery_tests {
    use super::*;
    // Windows mandatory integrity RIDs (SECURITY_MANDATORY_*_RID).
    const LOW: u32 = 0x1000;
    const INTEGRITY_MEDIUM: u32 = 0x2000;
    const INTEGRITY_HIGH: u32 = 0x3000;
    const SYSTEM: u32 = 0x4000;

    #[test]
    fn elevated_or_unreadable_targets_are_blocked() {
        let medium = Some(INTEGRITY_MEDIUM);
        assert_eq!(integrity_block(medium, medium), None);
        assert_eq!(integrity_block(Some(LOW), medium), None);
        assert_eq!(
            integrity_block(Some(INTEGRITY_HIGH), medium),
            Some(Blocked::Elevated)
        );
        assert_eq!(
            integrity_block(Some(SYSTEM), medium),
            Some(Blocked::Elevated)
        );
        // Murmur itself elevated may type into elevated windows.
        assert_eq!(
            integrity_block(Some(INTEGRITY_HIGH), Some(INTEGRITY_HIGH)),
            None
        );
        assert_eq!(integrity_block(None, medium), Some(Blocked::Unreadable));
        assert_eq!(integrity_block(medium, None), Some(Blocked::Unreadable));
    }

    #[test]
    fn only_the_default_desktop_takes_input() {
        assert_eq!(desktop_block(Some("Default")), None);
        assert_eq!(desktop_block(Some("default")), None);
        for secure in [Some("Winlogon"), Some("Screen-saver"), Some(""), None] {
            assert_eq!(desktop_block(secure), Some(Blocked::SecureDesktop));
        }
    }

    #[test]
    fn blocked_focus_keeps_its_class_for_messages() {
        let focus = Focus::Blocked {
            reason: Blocked::Elevated,
            class: "notepad.exe".into(),
        };
        assert_eq!(focus.class(), "notepad.exe");
        assert!(Blocked::Elevated.message().contains("administrator"));
    }
}
#[cfg(test)]
mod microphone_tests {
    use super::*;
    #[test]
    fn any_privacy_switch_off_blocks_the_microphone() {
        assert_eq!(microphone_block(None, None, None), None);
        assert_eq!(
            microphone_block(Some("Allow"), Some("Allow"), Some("Allow")),
            None
        );
        assert_eq!(
            microphone_block(Some("Deny"), Some("Allow"), None),
            Some(MicrophoneBlock::Device)
        );
        assert_eq!(
            microphone_block(None, Some("Deny"), Some("Deny")),
            Some(MicrophoneBlock::Apps)
        );
        assert_eq!(
            microphone_block(Some("Allow"), Some("Allow"), Some("deny")),
            Some(MicrophoneBlock::DesktopApps)
        );
        // Unknown strings are not treated as a block.
        assert_eq!(microphone_block(Some("Prompt"), None, None), None);
    }
    #[cfg(not(windows))]
    #[test]
    fn other_platforms_report_no_block_and_no_settings_page() {
        let access = microphone_access();
        assert_eq!(access.blocked, None);
        assert!(!access.settings_available);
        assert!(open_microphone_settings().is_err());
        assert_eq!(
            serde_json::to_value(MicrophoneAccess {
                blocked: Some(MicrophoneBlock::DesktopApps),
                settings_available: true
            })
            .unwrap(),
            serde_json::json!({"blocked":"desktop_apps","settings_available":true})
        );
    }
}
#[cfg(all(test, target_os = "linux"))]
mod focus_tests {
    use super::parse_window_id;
    #[test]
    fn active_window_id_parses_from_xprop() {
        assert_eq!(
            parse_window_id("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3a00007\n"),
            Some(0x3a00007)
        );
        assert_eq!(
            parse_window_id("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0"),
            None
        );
        assert_eq!(parse_window_id("_NET_ACTIVE_WINDOW:  not found."), None);
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
    fn sensitive_dependency_payloads_are_not_formatted() {
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
        let transcript = "The private transcript should not be logged";
        tracing::subscriber::with_default(subscriber, || {
            tracing::trace!(target: "tungstenite::protocol", "{token}");
            tracing::trace!(target: "tokio_tungstenite::protocol", "{token}");
            tracing::debug!(target: "enigo::platform::win_impl", "text(text: {transcript})");
            tracing::info!(target: "enigo", "{transcript}");
            tracing::info!(target: "murmur", "application log probe");
        });
        let formatted = String::from_utf8(output.0.lock().unwrap().clone()).unwrap();
        assert!(formatted.contains("application log probe"));
        assert!(!formatted.contains(token));
        assert!(!formatted.contains(transcript));
    }
}
