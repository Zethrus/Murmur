use crate::{config::Injection, postproc::Style};
#[cfg(target_os = "linux")]
use anyhow::bail;
use anyhow::{Context, Result};
#[cfg(not(any(target_os = "linux", windows)))]
use enigo::{Direction, Key};
#[cfg(not(windows))]
use enigo::{Enigo, Keyboard, Settings};
#[cfg(target_os = "linux")]
use std::process::Command;
use std::{thread, time::Duration};

/// The selected destination stopped accepting input. `sent_chars` is the number of characters
/// submitted before the check failed; the target may have processed fewer of them.
#[derive(Debug)]
pub struct TargetChanged {
    pub sent_chars: usize,
}
impl std::fmt::Display for TargetChanged {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "Delivery target changed after {} characters were submitted",
            self.sent_chars
        )
    }
}
impl std::error::Error for TargetChanged {}

/// Delivery was refused because the hotkey's physical modifiers stayed down through the wait.
#[derive(Debug)]
pub struct ModifiersHeld;
impl std::fmt::Display for ModifiersHeld {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Modifier keys are still held; release them and choose Insert here to retry")
    }
}
impl std::error::Error for ModifiersHeld {}

#[cfg(any(windows, test))]
mod keyboard_batch {
    use anyhow::{bail, Result};

    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub(super) enum Key {
        Control,
        Shift,
        V,
        Return,
        Tab,
        Unicode(u16),
    }

    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub(super) struct Stroke {
        pub key: Key,
        pub up: bool,
    }

    fn pair(key: Key) -> [Stroke; 2] {
        [Stroke { key, up: false }, Stroke { key, up: true }]
    }

    pub(super) fn character(ch: char) -> Result<Vec<Stroke>> {
        if ch == '\0' {
            bail!("Transcript contains a NUL character");
        }
        let mut events = Vec::with_capacity(4);
        match ch {
            '\n' => events.extend(pair(Key::Return)),
            '\t' => events.extend(pair(Key::Tab)),
            _ => {
                let mut units = [0; 2];
                for &unit in ch.encode_utf16(&mut units).iter() {
                    events.extend(pair(Key::Unicode(unit)));
                }
            }
        }
        Ok(events)
    }

    pub(super) fn paste(shift: bool) -> Vec<Stroke> {
        let mut events = vec![Stroke {
            key: Key::Control,
            up: false,
        }];
        if shift {
            events.push(Stroke {
                key: Key::Shift,
                up: false,
            });
        }
        events.extend(pair(Key::V));
        if shift {
            events.push(Stroke {
                key: Key::Shift,
                up: true,
            });
        }
        events.push(Stroke {
            key: Key::Control,
            up: true,
        });
        events
    }

    /// Only releases downs inserted by this call and not balanced within its inserted prefix.
    /// A cleanup failure is logged, but the original short-submission error remains authoritative.
    pub(super) fn send(events: &[Stroke], mut submit: impl FnMut(&[Stroke]) -> u32) -> Result<()> {
        let sent = submit(events) as usize;
        if sent == events.len() {
            return Ok(());
        }
        let mut down = Vec::new();
        for event in events.iter().take(sent) {
            if event.up {
                if let Some(index) = down.iter().rposition(|key| key == &event.key) {
                    down.remove(index);
                }
            } else {
                down.push(event.key);
            }
        }
        let cleanup: Vec<_> = down
            .into_iter()
            .rev()
            .map(|key| Stroke { key, up: true })
            .collect();
        if !cleanup.is_empty() {
            let released = submit(&cleanup);
            if released != cleanup.len() as u32 {
                tracing::warn!(
                    released,
                    requested = cleanup.len(),
                    "SendInput key-up cleanup incomplete"
                );
            }
        }
        bail!(
            "SendInput submitted {sent} of {} keyboard events",
            events.len()
        )
    }
}

#[cfg(test)]
fn utf16_pairs(ch: char) -> Vec<(u16, u16)> {
    let mut units = [0; 2];
    ch.encode_utf16(&mut units)
        .iter()
        .map(|&unit| (unit, unit))
        .collect()
}

#[cfg(any(windows, test))]
fn guarded_chars(
    text: &str,
    mut still_target: impl FnMut() -> Result<bool>,
    mut send: impl FnMut(char) -> Result<()>,
    mut pause: impl FnMut(),
) -> Result<()> {
    for (index, ch) in text.chars().enumerate() {
        if !still_target().unwrap_or(false) {
            return Err(TargetChanged { sent_chars: index }.into());
        }
        send(ch)?;
        pause();
    }
    Ok(())
}

#[cfg(windows)]
mod win_input {
    use super::keyboard_batch::{Key, Stroke};
    use super::*;
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
        SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE,
        VK_CONTROL, VK_RETURN, VK_SHIFT, VK_TAB, VK_V,
    };

    fn key(vk: u16, scan: u16, flags: u32) -> INPUT {
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: vk,
                    wScan: scan,
                    dwFlags: flags,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        }
    }
    fn send(events: &[Stroke]) -> Result<()> {
        keyboard_batch::send(events, |strokes| {
            let input: Vec<_> = strokes
                .iter()
                .map(|stroke| {
                    let (vk, scan, flags) = match stroke.key {
                        Key::Control => (VK_CONTROL, 0, 0),
                        Key::Shift => (VK_SHIFT, 0, 0),
                        Key::V => (VK_V, 0, 0),
                        Key::Return => (VK_RETURN, 0, 0),
                        Key::Tab => (VK_TAB, 0, 0),
                        Key::Unicode(unit) => (0, unit, KEYEVENTF_UNICODE),
                    };
                    key(
                        vk,
                        scan,
                        flags | if stroke.up { KEYEVENTF_KEYUP } else { 0 },
                    )
                })
                .collect();
            unsafe {
                SendInput(
                    input.len() as u32,
                    input.as_ptr(),
                    std::mem::size_of::<INPUT>() as i32,
                )
            }
        })
    }
    pub fn character(ch: char) -> Result<()> {
        send(&keyboard_batch::character(ch)?)
    }
    pub fn paste(shift: bool) -> Result<()> {
        send(&keyboard_batch::paste(shift))
    }
}

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
    use x11rb::protocol::xproto::KEY_RELEASE_EVENT;
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
/// Probes and interval for [`wait_for_modifiers_released`]: 30 × 20 ms = 600 ms.
const MODIFIER_PROBES: u32 = 30;
const MODIFIER_STEP: Duration = Duration::from_millis(20);
/// Shared control flow: poll `held` until it reports no modifier, at most `probes` times with
/// `sleep(step)` in between. Returns whether the modifiers were released in time.
fn wait_until_released(
    mut held: impl FnMut() -> bool,
    mut sleep: impl FnMut(Duration),
    probes: u32,
    step: Duration,
) -> bool {
    for probe in 0..probes {
        if !held() {
            return true;
        }
        if probe + 1 < probes {
            sleep(step);
        }
    }
    false
}
/// Poll (never synthesise) until the hotkey's modifiers are released: a half-released hold chord
/// turns the opening words into menu accelerators, and on Windows a synthesised letter while
/// Ctrl+Alt is still physically down can even fire the dictation hotkey again. Bounded at 600 ms
/// so a stuck modifier cannot stall typing.
#[cfg(any(target_os = "linux", windows))]
fn wait_for_modifiers_released() -> bool {
    let released = wait_until_released(
        modifiers_held,
        thread::sleep,
        MODIFIER_PROBES,
        MODIFIER_STEP,
    );
    if !released {
        tracing::info!("Modifier keys still held after 600 ms");
    }
    released
}
/// Windows: Control, Alt, Shift or either Windows key physically down. GetAsyncKeyState reads the
/// asynchronous key state (high bit = down now), the same source global-hotkey polls to report
/// Released. B8: verified in source, not run on hardware.
#[cfg(windows)]
fn modifiers_held() -> bool {
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
        GetAsyncKeyState, VK_CONTROL, VK_LWIN, VK_MENU, VK_RWIN, VK_SHIFT,
    };
    [VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN]
        .into_iter()
        .any(|key| unsafe { GetAsyncKeyState(i32::from(key)) } < 0)
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
        mut still_target: impl FnMut() -> Result<bool>,
    ) -> Result<InjectionPath> {
        let safe = safe_text(text, focused);
        let text = safe.as_str();
        let path = choose_path(text, cfg, focused, style);
        if path == InjectionPath::Skip || text.is_empty() {
            return Ok(path);
        }
        if path == InjectionPath::Type {
            return self.type_text(text, path, focused, &mut still_target);
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
        #[cfg(windows)]
        let sent = (|| {
            if !wait_for_modifiers_released() {
                return Err(ModifiersHeld.into());
            }
            if !still_target().unwrap_or(false) {
                return Err(TargetChanged { sent_chars: 0 }.into());
            }
            win_input::paste(is_terminal(focused))
        })();
        #[cfg(not(any(target_os = "linux", windows)))]
        let sent = Self::paste_with_enigo(focused);
        thread::sleep(Duration::from_millis(350));
        Self::restore(clipboard, previous, text);
        sent?;
        Ok(path)
    }
    fn type_text(
        &self,
        text: &str,
        path: InjectionPath,
        focused: &str,
        mut still_target: impl FnMut() -> Result<bool>,
    ) -> Result<InjectionPath> {
        #[cfg(windows)]
        let _ = focused;
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
        #[cfg(windows)]
        {
            if !wait_for_modifiers_released() {
                return Err(ModifiersHeld.into());
            }
            guarded_chars(text, &mut still_target, win_input::character, || {
                thread::sleep(Duration::from_millis(12))
            })
            .context("Type transcript into selected application")?;
        }
        #[cfg(not(windows))]
        {
            let _ = &mut still_target;
            Enigo::new(&Settings::default())
                .context("Connect keyboard injection")?
                .text(text)
                .context("Type transcript into focused application")?;
        }
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
    #[cfg(not(any(target_os = "linux", windows)))]
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
    fn unicode_key_pairs_keep_each_key_up_with_its_own_unit() {
        assert_eq!(utf16_pairs('é'), vec![(0x00e9, 0x00e9)]);
        assert_eq!(utf16_pairs('😀'), vec![(0xd83d, 0xd83d), (0xde00, 0xde00)]);
    }

    #[test]
    fn short_send_releases_only_unmatched_downs_from_its_own_prefix() {
        use keyboard_batch::{Key, Stroke};
        let up = |key| Stroke { key, up: true };
        let cases = [
            (
                "paste",
                keyboard_batch::paste(false),
                vec![
                    vec![],
                    vec![up(Key::Control)],
                    vec![up(Key::V), up(Key::Control)],
                    vec![up(Key::Control)],
                ],
            ),
            (
                "terminal paste",
                keyboard_batch::paste(true),
                vec![
                    vec![],
                    vec![up(Key::Control)],
                    vec![up(Key::Shift), up(Key::Control)],
                    vec![up(Key::V), up(Key::Shift), up(Key::Control)],
                    vec![up(Key::Shift), up(Key::Control)],
                    vec![up(Key::Control)],
                ],
            ),
            (
                "return",
                keyboard_batch::character('\n').unwrap(),
                vec![vec![], vec![up(Key::Return)]],
            ),
            (
                "tab",
                keyboard_batch::character('\t').unwrap(),
                vec![vec![], vec![up(Key::Tab)]],
            ),
            (
                "unicode",
                keyboard_batch::character('é').unwrap(),
                vec![vec![], vec![up(Key::Unicode(0xe9))]],
            ),
            (
                "surrogates",
                keyboard_batch::character('😀').unwrap(),
                vec![
                    vec![],
                    vec![up(Key::Unicode(0xd83d))],
                    vec![],
                    vec![up(Key::Unicode(0xde00))],
                ],
            ),
        ];
        for (name, events, expected) in cases {
            assert_eq!(expected.len(), events.len(), "{name}");
            for (prefix, cleanup) in expected.into_iter().enumerate() {
                let mut calls = Vec::new();
                let error = keyboard_batch::send(&events, |batch| {
                    calls.push(batch.to_vec());
                    if calls.len() == 1 {
                        prefix as u32
                    } else {
                        batch.len() as u32
                    }
                })
                .unwrap_err();
                assert_eq!(
                    error.to_string(),
                    format!(
                        "SendInput submitted {prefix} of {} keyboard events",
                        events.len()
                    ),
                    "{name}"
                );
                assert_eq!(
                    calls,
                    if cleanup.is_empty() {
                        vec![events.clone()]
                    } else {
                        vec![events.clone(), cleanup.clone()]
                    },
                    "{name} prefix {prefix}"
                );
                if name == "paste" && calls.len() == 2 {
                    // A held Shift was never in this submission, so cleanup must not release it.
                    assert!(calls[1].iter().all(|event| event.key != Key::Shift));
                }
                if !cleanup.is_empty() {
                    let mut attempts = 0;
                    let original = keyboard_batch::send(&events, |_| {
                        attempts += 1;
                        if attempts == 1 {
                            prefix as u32
                        } else {
                            0
                        }
                    })
                    .unwrap_err();
                    assert_eq!(
                        original.to_string(),
                        error.to_string(),
                        "{name} prefix {prefix}"
                    );
                    assert_eq!(
                        attempts, 2,
                        "cleanup failure is never retried: {name} prefix {prefix}"
                    );
                }
            }
            let mut calls = 0;
            keyboard_batch::send(&events, |batch| {
                calls += 1;
                batch.len() as u32
            })
            .unwrap();
            assert_eq!(calls, 1, "full send never needs cleanup: {name}");
        }
    }

    #[test]
    fn failed_cleanup_keeps_original_short_send_error_and_does_not_retry_text() {
        let events = keyboard_batch::paste(true);
        let mut calls = Vec::new();
        let error = keyboard_batch::send(&events, |batch| {
            calls.push(batch.to_vec());
            if calls.len() == 1 {
                3
            } else {
                0
            }
        })
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "SendInput submitted 3 of 6 keyboard events"
        );
        assert_eq!(calls.len(), 2);
        assert_eq!(
            calls[1],
            vec![
                keyboard_batch::Stroke {
                    key: keyboard_batch::Key::V,
                    up: true
                },
                keyboard_batch::Stroke {
                    key: keyboard_batch::Key::Shift,
                    up: true
                },
                keyboard_batch::Stroke {
                    key: keyboard_batch::Key::Control,
                    up: true
                },
            ]
        );
        assert!(calls[1].iter().all(|event| event.up));
    }
    #[test]
    fn guarded_typing_stops_before_destination_changes() {
        let mut sent = String::new();
        let mut probes = 0;
        let error = guarded_chars(
            "Aé😀Z",
            || {
                probes += 1;
                Ok(probes <= 2)
            },
            |ch| {
                sent.push(ch);
                Ok(())
            },
            || {},
        )
        .context("Type transcript into selected application")
        .unwrap_err();
        assert_eq!(sent, "Aé");
        assert_eq!(error.downcast_ref::<TargetChanged>().unwrap().sent_chars, 2);
        assert_eq!(probes, 3);
    }
    #[test]
    fn unreadable_destination_does_not_submit_a_character() {
        let mut sent = String::new();
        let error = guarded_chars(
            "text",
            || anyhow::bail!("focus unavailable"),
            |ch| {
                sent.push(ch);
                Ok(())
            },
            || {},
        )
        .unwrap_err();
        assert!(sent.is_empty());
        assert_eq!(error.downcast_ref::<TargetChanged>().unwrap().sent_chars, 0);
    }
    #[test]
    fn guarded_typing_preserves_exact_unicode_text() {
        let mut sent = String::new();
        guarded_chars(
            "café 日本語 😀\n",
            || Ok(true),
            |ch| {
                sent.push(ch);
                Ok(())
            },
            || {},
        )
        .unwrap();
        assert_eq!(sent, "café 日本語 😀\n");
    }
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
    fn modifier_wait_returns_once_released() {
        let mut probes = 0;
        let mut slept = Duration::ZERO;
        let released = wait_until_released(
            || {
                probes += 1;
                probes < 4
            },
            |step| slept += step,
            MODIFIER_PROBES,
            MODIFIER_STEP,
        );
        assert!(released);
        assert_eq!(probes, 4);
        assert_eq!(slept, MODIFIER_STEP * 3);
    }
    #[test]
    fn modifier_wait_is_bounded_at_600_ms() {
        let mut probes = 0;
        let mut slept = Duration::ZERO;
        let released = wait_until_released(
            || {
                probes += 1;
                true
            },
            |step| slept += step,
            MODIFIER_PROBES,
            MODIFIER_STEP,
        );
        assert!(!released);
        assert_eq!(probes, MODIFIER_PROBES);
        assert!(slept <= Duration::from_millis(600));
        // Nothing held: no sleep at all.
        assert!(wait_until_released(
            || false,
            |_| panic!("slept"),
            30,
            MODIFIER_STEP
        ));
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
