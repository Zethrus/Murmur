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
pub fn choose_path(text: &str, cfg: &Injection, focused: &str, style: Style) -> InjectionPath {
    let app = focused.to_lowercase();
    if app == "murmur"
        || app == "murmur.exe"
        || app.starts_with("murmur settings")
        || app.starts_with("murmur overlay")
    {
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
/// app launch, in its own session, types on request — same context as typing by hand from a shell,
/// which works. The app hands over a payload file and signals a FIFO.
#[cfg(target_os = "linux")]
mod helper {
    use super::*;
    use std::fs;
    use std::io::Write;
    use std::path::{Path, PathBuf};
    use std::process::Stdio;

    struct Paths {
        fifo: PathBuf,
        payload: PathBuf,
        pid: PathBuf,
        done: PathBuf,
    }
    fn paths() -> Result<Paths> {
        let dir = std::env::var_os("XDG_RUNTIME_DIR")
            .map(PathBuf::from)
            .context("XDG_RUNTIME_DIR is unset; cannot reach the injection helper")?;
        Ok(Paths {
            fifo: dir.join("murmur-inject.fifo"),
            payload: dir.join("murmur-inject.txt"),
            pid: dir.join("murmur-inject.pid"),
            done: dir.join("murmur-inject.done"),
        })
    }
    fn alive(pid: &Path) -> bool {
        fs::read_to_string(pid)
            .ok()
            .and_then(|raw| raw.trim().parse::<u32>().ok())
            .map(|pid| Path::new(&format!("/proc/{pid}")).exists())
            .unwrap_or(false)
    }
    fn ensure_running(paths: &Paths) -> Result<()> {
        if alive(&paths.pid) {
            return Ok(());
        }
        if !paths.fifo.exists() {
            let status = Command::new("mkfifo")
                .arg(&paths.fifo)
                .status()
                .context("Create injection FIFO")?;
            if !status.success() {
                bail!("mkfifo exited with status {status}");
            }
        }
        // SIGUSR1 is the trigger: the trap types whatever the payload file holds.
        let script = format!(
            "echo $$ > {pid}; trap 'sleep 0.25; xdotool type --delay 6 \"$(cat {payload})\"; : > {done}' USR1; \
             while kill -0 {owner} 2>/dev/null; do sleep 0.25; done",
            owner = std::process::id(),
            pid = paths.pid.display(),
            payload = paths.payload.display(),
            done = paths.done.display()
        );
        Command::new("setsid")
            .arg("sh")
            .arg("-c")
            .arg(script)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .context("Start injection helper")?;
        for _ in 0..75 {
            if alive(&paths.pid) {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(20));
        }
        bail!("injection helper did not come up")
    }
    /// Hand the transcript to the helper and wait for it to finish typing.
    pub fn submit(text: &str) -> Result<()> {
        let paths = paths()?;
        ensure_running(&paths)?;
        let _ = fs::remove_file(&paths.done);
        let staged = paths.payload.with_extension("staged");
        fs::write(&staged, text).context("Stage transcript for the helper")?;
        fs::rename(&staged, &paths.payload).context("Publish transcript for the helper")?;
        tracing::info!("helper: payload staged");
        let pid = fs::read_to_string(&paths.pid)
            .context("Read injection helper pid")?
            .trim()
            .to_owned();
        tracing::info!(%pid, "helper: signalling");
        let status = Command::new("kill")
            .arg("-USR1")
            .arg(&pid)
            .status()
            .context("Signal injection helper")?;
        tracing::info!(ok = status.success(), "helper: signalled");
        if !status.success() {
            bail!("kill -USR1 {pid} failed");
        }
        // Typing runs at ~6 ms per character; give the helper room to drain the payload.
        // Wait for the helper's completion marker instead of guessing how long typing takes.
        let budget = 300 + text.chars().count() as u64 * 20;
        for _ in 0..(budget / 10) {
            if paths.done.exists() {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(10));
        }
        bail!("injection helper timed out after {budget} ms")
    }
}
/// Stop the injection helper on exit so no orphaned process can type into the session.
#[cfg(target_os = "linux")]
pub fn stop_helper() {
    let Some(dir) = std::env::var_os("XDG_RUNTIME_DIR") else {
        return;
    };
    let runtime = std::path::PathBuf::from(dir);
    if let Ok(raw) = std::fs::read_to_string(runtime.join("murmur-inject.pid")) {
        let pid = raw.trim().to_owned();
        if !pid.is_empty() {
            let _ = Command::new("kill").arg(&pid).status();
        }
    }
    let _ = std::fs::remove_file(runtime.join("murmur-inject.pid"));
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
    let quoted = format!("'{}'", text.replace('\'', "'\\''"));
    let status = Command::new("xdotool")
        .arg("type")
        .arg("--delay")
        .arg("6")
        .arg(&quoted[1..quoted.len() - 1])
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
        // Short transcripts are typed even when the policy chose paste: the chord is the least
        // reliable step in the pipeline, so it stays reserved for text too long to type quickly.
        #[cfg(target_os = "linux")]
        if text.chars().count() <= 0 {
            match xdotool_type(text, focused) {
                Ok(()) => {
                    tracing::info!(
                        chars = text.chars().count(),
                        "Typed transcript instead of pasting"
                    );
                    Self::restore(clipboard, previous, text);
                    return Ok(path);
                }
                Err(error) => tracing::warn!(%error, "Typing failed; falling back to paste"),
            }
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
