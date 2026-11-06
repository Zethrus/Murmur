use crate::{config::Injection, postproc::Style};
use anyhow::{bail, Context, Result};
use enigo::{Direction, Enigo, Key, Keyboard, Settings};
#[cfg(target_os = "linux")]
use std::process::Command;
use std::{thread, time::Duration};

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum InjectionPath {
    Type,
    Clipboard,
    ClipboardOnly,
    Skip,
}
/// Murmur's own windows never receive a transcript.
pub fn is_own_app(focused: &str) -> bool {
    let app = focused.to_lowercase();
    app == "murmur"
        || app == "murmur.exe"
        || app.starts_with("murmur settings")
        || app.starts_with("murmur overlay")
}
pub fn choose_path(text: &str, cfg: &Injection, focused: &str, style: Style) -> InjectionPath {
    let app = focused.to_lowercase();
    if is_own_app(&app) {
        return InjectionPath::Skip;
    }
    if cfg.clipboard_only
        || cfg
            .exclude_apps
            .iter()
            .any(|s| !s.is_empty() && app.contains(&s.to_lowercase()))
    {
        return InjectionPath::ClipboardOnly;
    }
    // Typing is bounded by the input layer's speed, so long or code-shaped transcripts prefer the
    // clipboard round trip; short prose is typed directly and leaves the clipboard untouched.
    if cfg.prefer_paste && (style == Style::Code || text.chars().count() > 60) {
        return InjectionPath::Clipboard;
    }
    if cfg.method == "type" || (cfg.method == "auto" && text.is_ascii() && text.len() <= 1500) {
        InjectionPath::Type
    } else {
        InjectionPath::Clipboard
    }
}
pub fn is_terminal(focused: &str) -> bool {
    let app = focused.to_lowercase();
    [
        "terminal",
        "konsole",
        "alacritty",
        "kitty",
        "wezterm",
        "mintty",
        "xterm",
        "powershell",
        "cmd.exe",
        "tilix",
        "terminator",
        "urxvt",
    ]
    .iter()
    .any(|name| app.contains(name))
}

/// X11: drop any modifier the hotkey left pressed, otherwise the first word is consumed as an
/// accelerator (Ctrl+Alt+A opens a menu instead of typing "And").
#[cfg(target_os = "linux")]
fn clear_modifiers() {
    let _ = Command::new("xdotool")
        .args(["keyup", "ctrl", "alt", "shift", "super"])
        .status();
}
#[cfg(target_os = "linux")]
fn modifiers_held() -> bool {
    use x11rb::connection::Connection;
    use x11rb::protocol::xproto::{ConnectionExt, ModMask};
    let Ok((conn, screen)) = x11rb::connect(None) else {
        return false;
    };
    let root = conn.setup().roots[screen].root;
    match conn
        .query_pointer(root)
        .ok()
        .and_then(|cookie| cookie.reply().ok())
    {
        Some(state) => {
            let mask = u16::from(ModMask::CONTROL) | u16::from(ModMask::M1);
            u16::from(state.mask) & mask != 0
        }
        None => false,
    }
}
/// Hold-to-talk can deliver while the chord is still held, and a killed helper can leave Control or
/// Alt latched in the server. Release them ourselves through XTEST until the state is clean, because
/// xdotool's `--clearmodifiers` releases them immediately before the first keystroke, which eats the
/// opening word as a menu accelerator.
#[cfg(target_os = "linux")]
fn release_modifiers() {
    use x11rb::connection::Connection;
    use x11rb::protocol::xproto::{ConnectionExt as _, KEY_RELEASE_EVENT};
    use x11rb::protocol::xtest::ConnectionExt as _;
    const CONTROL_L: u8 = 37;
    const ALT_L: u8 = 64;
    const SHIFT_L: u8 = 50;
    const SUPER_L: u8 = 133;
    let Ok((conn, _screen)) = x11rb::connect(None) else {
        return;
    };
    for _ in 0..25 {
        if !modifiers_held() {
            return;
        }
        for keycode in [CONTROL_L, ALT_L, SHIFT_L, SUPER_L] {
            let _ = conn.xtest_fake_input(KEY_RELEASE_EVENT, keycode, 0, 0, 0, 0, 0);
        }
        let _ = conn.flush();
        thread::sleep(Duration::from_millis(20));
    }
}
/// Poll (never synthesise) until Control/Alt are released: a half-released hold chord turns the
/// opening words into menu accelerators. Bounded at 600 ms so a stuck modifier cannot stall typing.
#[cfg(target_os = "linux")]
fn wait_for_modifiers_released() {
    for _ in 0..30 {
        if !modifiers_held() {
            return;
        }
        thread::sleep(Duration::from_millis(20));
    }
}
/// Long-lived injection helper.
///
/// Spawning `xdotool` per utterance from the GTK process loses the opening token of the transcript
/// (verified with `xev`: the keystrokes never reach the focused window). A helper started once at
/// app launch, in its own session, types on request in the same context as typing by hand from a shell,
/// which works. The app uses a private payload file and signals its owned child.
#[cfg(target_os = "linux")]
mod helper {
    use super::*;
    use std::fs;
    use std::os::unix::{fs::DirBuilderExt, process::CommandExt};
    use std::path::PathBuf;
    use std::process::{Child, Stdio};
    use std::sync::Mutex;

    static HELPER: Mutex<Option<Helper>> = Mutex::new(None);

    struct Helper {
        child: Child,
        directory: PathBuf,
    }
    impl Drop for Helper {
        fn drop(&mut self) {
            // Kill the session's process group while its leader is still our unreaped
            // child. This also stops an in-flight xdotool. Never signal a reaped PID.
            if matches!(self.child.try_wait(), Ok(None)) {
                unsafe {
                    libc::kill(-(self.child.id() as libc::pid_t), libc::SIGKILL);
                }
            }
            let _ = self.child.kill();
            let _ = self.child.wait();
            let _ = fs::remove_dir_all(&self.directory);
        }
    }
    fn running(child: &mut Child) -> Result<bool> {
        Ok(child
            .try_wait()
            .context("Check injection helper")?
            .is_none())
    }
    fn signal(child: &mut Child) -> Result<()> {
        if !running(child)? {
            bail!("Injection helper exited");
        }
        // HELPER stays locked through delivery. No other thread can reap this child
        // between the status check and signal, so PID reuse cannot target a stranger.
        if unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGUSR1) } != 0 {
            return Err(std::io::Error::last_os_error()).context("Signal injection helper");
        }
        Ok(())
    }
    fn start() -> Result<Helper> {
        let runtime = std::env::var_os("XDG_RUNTIME_DIR")
            .map(PathBuf::from)
            .context("XDG_RUNTIME_DIR is unset; cannot reach the injection helper")?;
        let mut nonce = [0u8; 16];
        getrandom::fill(&mut nonce).map_err(|error| anyhow::anyhow!("Helper nonce: {error}"))?;
        let suffix: String = nonce.iter().map(|byte| format!("{byte:02x}")).collect();
        let directory = runtime.join(format!("murmur-inject-{suffix}"));
        fs::DirBuilder::new().mode(0o700).create(&directory)?;
        let script = "trap 'sleep 0.25; if xdotool type --delay 6 -- \"$(cat payload)\"; then : > done; else : > failed; fi' USR1; : > ready; while :; do sleep 0.25; done";
        let owner = std::process::id();
        let mut command = Command::new("sh");
        command
            .arg("-c")
            .arg(script)
            .current_dir(&directory)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        unsafe {
            command.pre_exec(move || {
                if libc::setsid() == -1 || libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) == -1
                {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::getppid() != owner as libc::pid_t {
                    return Err(std::io::Error::from_raw_os_error(libc::ECHILD));
                }
                Ok(())
            });
        }
        let child = match command.spawn() {
            Ok(child) => child,
            Err(error) => {
                let _ = fs::remove_dir_all(&directory);
                return Err(error).context("Start injection helper");
            }
        };
        let mut helper = Helper { child, directory };
        for _ in 0..75 {
            if helper.directory.join("ready").exists() && running(&mut helper.child)? {
                return Ok(helper);
            }
            thread::sleep(Duration::from_millis(20));
        }
        bail!("Injection helper did not become ready")
    }
    pub fn submit(text: &str) -> Result<()> {
        let mut slot = HELPER
            .lock()
            .map_err(|_| anyhow::anyhow!("Helper lock poisoned"))?;
        if let Some(helper) = slot.as_mut() {
            if !running(&mut helper.child)? {
                *slot = None;
            }
        }
        if slot.is_none() {
            *slot = Some(start()?);
        }
        let helper = slot.as_mut().context("Injection helper unavailable")?;
        let payload = helper.directory.join("payload");
        let done = helper.directory.join("done");
        let failed = helper.directory.join("failed");
        let _ = fs::remove_file(&done);
        let _ = fs::remove_file(&failed);
        fs::write(&payload, text).context("Stage transcript for the helper")?;
        let result = (|| {
            signal(&mut helper.child)?;
            let budget = 1000 + text.chars().count() as u64 * 20;
            for _ in 0..(budget / 10) {
                if done.exists() {
                    return Ok(());
                }
                if failed.exists() || !running(&mut helper.child)? {
                    bail!("Injection helper failed");
                }
                thread::sleep(Duration::from_millis(10));
            }
            bail!("Injection helper timed out after {budget} ms")
        })();
        let _ = fs::remove_file(payload);
        if result.is_err() {
            *slot = None;
        }
        result
    }
    pub fn stop() {
        if let Ok(mut helper) = HELPER.lock() {
            *helper = None;
        }
    }
    #[cfg(test)]
    mod tests {
        use super::*;
        #[test]
        fn exited_child_is_not_signalled() {
            let mut child = Command::new("sh").args(["-c", "exit 0"]).spawn().unwrap();
            child.wait().unwrap();
            assert!(signal(&mut child).is_err());
        }
        #[test]
        fn dropping_helper_stops_descendants() {
            let directory =
                std::env::temp_dir().join(format!("murmur-descendant-test-{}", std::process::id()));
            fs::create_dir_all(&directory).unwrap();
            let mut command = Command::new("sh");
            command
                .args([
                    "-c",
                    "sleep 30 & echo $! > descendant; while :; do sleep 1; done",
                ])
                .current_dir(&directory);
            unsafe {
                command.pre_exec(|| {
                    if libc::setsid() == -1 {
                        return Err(std::io::Error::last_os_error());
                    }
                    Ok(())
                });
            }
            let child = command.spawn().unwrap();
            let helper = Helper {
                child,
                directory: directory.clone(),
            };
            let pid = (0..100)
                .find_map(|_| {
                    let pid = fs::read_to_string(directory.join("descendant"))
                        .ok()
                        .and_then(|value| value.trim().parse::<u32>().ok());
                    if pid.is_none() {
                        thread::sleep(Duration::from_millis(10));
                    }
                    pid
                })
                .expect("Helper descendant did not start");
            drop(helper);
            for _ in 0..100 {
                match fs::read_to_string(format!("/proc/{pid}/stat")) {
                    Err(_) => return,
                    Ok(stat) if stat.rsplit_once(") ").unwrap().1.starts_with('Z') => return,
                    _ => thread::sleep(Duration::from_millis(10)),
                }
            }
            panic!("Helper descendant survived shutdown");
        }
        #[test]
        fn dropping_owned_helper_does_not_kill_unrelated_process() {
            let mut unrelated = Command::new("sleep").arg("30").spawn().unwrap();
            let directory =
                std::env::temp_dir().join(format!("murmur-helper-test-{}", unrelated.id()));
            fs::create_dir(&directory).unwrap();
            fs::write(
                directory.join("murmur-inject.pid"),
                unrelated.id().to_string(),
            )
            .unwrap();
            let child = Command::new("sleep").arg("30").spawn().unwrap();
            drop(Helper {
                child,
                directory: directory.clone(),
            });
            assert!(running(&mut unrelated).unwrap());
            unrelated.kill().unwrap();
            unrelated.wait().unwrap();
            assert!(!directory.exists());
        }
    }
}
/// Stop only the helper owned by this application process.
#[cfg(target_os = "linux")]
pub fn stop_helper() {
    helper::stop();
}
#[cfg(not(target_os = "linux"))]
pub fn stop_helper() {}
/// Type the transcript through the long-lived helper; fall back to a direct call if it is missing.
#[cfg(target_os = "linux")]
fn xdotool_type(text: &str, _class: &str) -> Result<()> {
    // Note: never synthesise modifier releases here. Fake Control/Alt key-ups during a hold-mode
    // cycle desynchronise the global hotkey grab, after which the chord is swallowed and ignored.
    // Polling is safe: it only observes.
    wait_for_modifiers_released();
    match helper::submit(text) {
        Ok(()) => return Ok(()),
        Err(error) => tracing::warn!(%error, "Injection helper unavailable; typing directly"),
    }
    let status = Command::new("xdotool")
        .arg("type")
        .arg("--delay")
        .arg("6")
        .arg("--")
        .arg(text)
        .status()
        .context("Run xdotool for typing")?;
    if status.success() {
        Ok(())
    } else {
        bail!("xdotool type exited with status {status}")
    }
}
#[cfg(target_os = "linux")]
fn xdotool_chord(shift: bool) -> Result<()> {
    clear_modifiers();
    release_modifiers();
    // Let the selection owner announce itself before the target reads CLIPBOARD.
    thread::sleep(Duration::from_millis(150));
    let combo = if shift { "ctrl+shift+v" } else { "ctrl+v" };
    let status = Command::new("xdotool")
        .arg("key")
        .arg(combo)
        .status()
        .context("Run xdotool for paste")?;
    if status.success() {
        Ok(())
    } else {
        bail!("xdotool key {combo} exited with status {status}")
    }
}
pub fn safe_text(text: &str, focused: &str) -> String {
    if is_terminal(focused) {
        text.replace("\r\n", " ")
            .replace(['\r', '\n', '\u{2028}', '\u{2029}'], " ")
    } else {
        text.to_owned()
    }
}
#[derive(Default)]
pub struct Injector {
    clipboard: Option<arboard::Clipboard>,
}
impl Injector {
    // Keep ownership alive: on X11 the process serves clipboard contents until dropped.
    pub fn inject(
        &mut self,
        text: &str,
        cfg: &Injection,
        focused: &str,
        style: Style,
    ) -> Result<InjectionPath> {
        let safe = safe_text(text, focused);
        let text = safe.as_str();
        let path = choose_path(text, cfg, focused, style);
        if path == InjectionPath::Skip || text.is_empty() {
            return Ok(path);
        }
        if path == InjectionPath::Type {
            return self.type_text(text, path, focused);
        }
        if self.clipboard.is_none() {
            self.clipboard = Some(arboard::Clipboard::new().context("Open clipboard")?);
        }
        let clipboard = self.clipboard.as_mut().context("Clipboard unavailable")?;
        let previous = if path == InjectionPath::Clipboard {
            match clipboard.get_text() {
                Ok(text) => Some(text),
                Err(arboard::Error::ContentNotAvailable) => None,
                Err(error) => return Err(error).context("Save clipboard text before pasting"),
            }
        } else {
            None
        };
        clipboard.set_text(text).context("Copy transcript")?;
        if path == InjectionPath::ClipboardOnly {
            return Ok(path);
        }
        #[cfg(target_os = "linux")]
        let sent = xdotool_chord(is_terminal(focused));
        #[cfg(not(target_os = "linux"))]
        let sent = Self::paste_with_enigo(focused);
        thread::sleep(Duration::from_millis(350));
        Self::restore(clipboard, previous, text);
        sent?;
        Ok(path)
    }
    fn type_text(&self, text: &str, path: InjectionPath, focused: &str) -> Result<InjectionPath> {
        #[cfg(target_os = "linux")]
        {
            match xdotool_type(text, focused) {
                Ok(()) => {
                    tracing::info!(
                        chars = text.chars().count(),
                        "Typed transcript with XTEST key events"
                    );
                    return Ok(path);
                }
                Err(error) => tracing::warn!(%error, "xdotool typing failed; using enigo"),
            }
        }
        Enigo::new(&Settings::default())
            .context("Connect keyboard injection")?
            .text(text)
            .context("Type transcript into focused application")?;
        Ok(path)
    }
    fn restore(clipboard: &mut arboard::Clipboard, previous: Option<String>, text: &str) {
        let Some(previous) = previous else { return };
        // Do not overwrite a clipboard update made by the user while the transcript was pasted.
        if clipboard.get_text().ok().as_deref() == Some(text) {
            if let Err(error) = clipboard.set_text(previous) {
                tracing::warn!(%error, "Clipboard restore failed; retaining transcript");
                let _ = clipboard.set_text(text);
            }
        }
    }
    #[cfg(not(target_os = "linux"))]
    fn paste_with_enigo(focused: &str) -> Result<()> {
        let mut keyboard = Enigo::new(&Settings::default()).context("Connect paste keyboard")?;
        keyboard
            .key(Key::Control, Direction::Press)
            .context("Press paste modifier")?;
        let shift = is_terminal(focused);
        let paste = (|| -> Result<()> {
            if shift {
                keyboard
                    .key(Key::Shift, Direction::Press)
                    .context("Press terminal paste modifier")?;
            }
            keyboard
                .key(Key::Unicode('v'), Direction::Click)
                .context("Paste transcript")
        })();
        let shift_release = if shift {
            keyboard.key(Key::Shift, Direction::Release)
        } else {
            Ok(())
        };
        let release = keyboard.key(Key::Control, Direction::Release);
        paste?;
        shift_release.context("Release terminal paste modifier")?;
        release.context("Release paste modifier")?;
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn auto_paths() {
        let c = Injection::default();
        assert_eq!(
            choose_path("hello", &c, "editor", Style::Prose),
            InjectionPath::Type
        );
        assert_eq!(
            choose_path("héllo", &c, "editor", Style::Prose),
            InjectionPath::Clipboard
        );
        assert_eq!(
            choose_path(&"a".repeat(1501), &c, "editor", Style::Prose),
            InjectionPath::Clipboard
        );
        assert_eq!(
            choose_path(&"a".repeat(200), &c, "editor", Style::Prose),
            InjectionPath::Clipboard
        );
        assert_eq!(
            choose_path(&"a".repeat(40), &c, "editor", Style::Prose),
            InjectionPath::Type
        );
    }
    #[test]
    fn exclusions_and_own_windows() {
        let c = Injection {
            exclude_apps: vec!["terminal".into()],
            ..Default::default()
        };
        assert_eq!(
            choose_path("secret", &c, "Gnome-Terminal", Style::Prose),
            InjectionPath::ClipboardOnly
        );
        assert_eq!(
            choose_path("text", &c, "Murmur Settings", Style::Prose),
            InjectionPath::Skip
        );
    }
    #[test]
    fn own_windows_and_terminal_destinations() {
        for own in ["murmur", "Murmur.exe", "Murmur Settings", "murmur overlay"] {
            assert!(is_own_app(own), "{own}");
        }
        assert!(!is_own_app("obsidian"));
        // Held text inserted into a terminal is re-made safe for that destination.
        assert_eq!(safe_text("echo one\nrm two", "kitty"), "echo one rm two");
        assert_eq!(
            safe_text("line one\nline two", "obsidian"),
            "line one\nline two"
        );
    }
    #[test]
    fn explicit_modes() {
        let mut c = Injection {
            method: "type".into(),
            ..Default::default()
        };
        assert_eq!(
            choose_path("日本語", &c, "editor", Style::Prose),
            InjectionPath::Type
        );
        c.clipboard_only = true;
        assert_eq!(
            choose_path("hi", &c, "editor", Style::Prose),
            InjectionPath::ClipboardOnly
        );
    }
}
