use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, fs, io::Write, path::Path};

type Extra = BTreeMap<String, toml::Value>;
macro_rules! section {
    ($name:ident { $($field:ident: $ty:ty = $value:expr),* $(,)? }) => {
        #[derive(Debug, Clone, Serialize, Deserialize)]
        #[serde(default)]
        pub struct $name { $(pub $field: $ty,)* #[serde(flatten)] pub extra: Extra }
        impl Default for $name {
            fn default() -> Self { Self { $($field: $value,)* extra: Extra::new() } }
        }
    };
}
section!(General {
    hotkey: String = "Ctrl+Alt+D".into(),
    hotkey_mode: String = "hold".into(),
    language: String = "en".into(),
    translate: bool = false,
    autostart: bool = false,
    theme: String = "auto".into(),
    hotkey_code: String = "".into()
});
section!(Placement {
    monitor_name: String = "".into(),
    monitor_x: i32 = 0,
    monitor_y: i32 = 0,
    monitor_width: u32 = 0,
    monitor_height: u32 = 0,
    x_fraction: f64 = 0.5,
    y_fraction: f64 = 1.0
});
impl Placement {
    /// A zero-size monitor record means "no saved custom placement".
    pub fn is_set(&self) -> bool {
        self.monitor_width > 0 && self.monitor_height > 0
    }
}
section!(Overlay {
    enabled: bool = true,
    position: String = "bottom-center".into(),
    scale: f64 = 1.0,
    show_preview: bool = true,
    idle_hide: bool = true,
    placement: Placement = Placement::default()
});
section!(Sounds {
    enabled: bool = true,
    volume: f32 = 0.6,
    muted: bool = false
});
section!(Engine {
    model: String = "distil-small.en".into(), device: String = "auto".into(),
    compute_type: String = "auto".into(), beam_size: u32 = 1, vad_filter: bool = true,
    dictionary: Vec<String> = ["Tauri", "faster-whisper", "CTranslate2", "WebKitGTK", "WebView2", "serde", "tokio", "enigo", "cpal", "rodio", "Murmur", "zethrus", "Python", "TypeScript", "npm", "cargo", "git", "stdin", "stdout", "JSON-RPC", "hotkey", "overlay", "systemd", "Wayland", "X11", "pip", "venv", "regex", "null", "boolean"].map(String::from).to_vec()
});
section!(Audio {
    device: String = "default".into(),
    gain: f32 = 1.0,
    silence_threshold: f32 = 0.012,
    silence_timeout_ms: u64 = 1200,
    max_utterance_s: u64 = 90,
    min_utterance_ms: u64 = 250
});
section!(Injection {
    method: String = "auto".into(), clipboard_only: bool = false, trailing_space: bool = true,
    capitalize: bool = true, remove_fillers: bool = false, exclude_apps: Vec<String> = Vec::new(), prefer_paste: bool = true
});
section!(Postproc {
    replacements: BTreeMap<String, String> = BTreeMap::from([("mur mur".into(), "Murmur".into())]),
    fillers: Vec<String> = ["um", "uh", "er", "hmm"].map(String::from).to_vec(),
    style: String = "auto".into(),
    code_apps: Vec<String> = ["code","cursor","vscode","code-oss","jetbrains","zed","sublime","gnome-terminal","konsole","alacritty","kitty","wezterm","windowsterminal","mintty"].map(String::from).to_vec(),
    spoken_punctuation: bool = true,
    allow_newlines: bool = false,
    remove_duplicate_words: bool = true
});
section!(Privacy {
    history_enabled: bool = false,
    history_retention_days: u32 = 7
});
section!(Enhance {
    auto_rewrite: bool = false,
    rewrite_hotkey: String = "".into(),
    accept_hotkey: String = "".into(),
    style: String = "concise".into(),
    model: String = crate::enhance::catalog::DEFAULT_MODEL_ID.into(),
    accept_mode: String = "clipboard".into(),
    rewrite_code: bool = false,
    threads: u32 = 0,
    unload_after_s: u64 = 120,
    load_timeout_ms: u64 = 30000,
    timeout_ms: u64 = 60000
});
// Flipper button map. Values are verb names validated by `Companion::validate`.
section!(CompanionButtons {
    ok: String = "dictate_toggle".into(),
    up: String = "enhance".into(),
    down: String = "accept".into(),
    back: String = "dismiss".into(),
    left: String = "none".into(),
    right: String = "none".into(),
    ok_long: String = "cancel".into(),
    up_long: String = "none".into(),
    down_long: String = "none".into()
});
section!(CompanionNetwork {
    enabled: bool = false,
    bind: String = "127.0.0.1".into(),
    port: u16 = 8787,
    mdns: bool = false
});
section!(Companion {
    enabled: bool = false,
    ptt: bool = false,
    transport: String = "auto".into(),
    usb_port: String = "".into(),
    ble_address: String = "".into(),
    source: String = "auto".into(),
    network: CompanionNetwork = CompanionNetwork::default(),
    buttons: CompanionButtons = CompanionButtons::default()
});
impl CompanionButtons {
    /// Every mapping as `(config key, verb)`, in a stable order.
    pub fn pairs(&self) -> [(&'static str, &str); 9] {
        [
            ("ok", self.ok.as_str()),
            ("up", self.up.as_str()),
            ("down", self.down.as_str()),
            ("back", self.back.as_str()),
            ("left", self.left.as_str()),
            ("right", self.right.as_str()),
            ("ok_long", self.ok_long.as_str()),
            ("up_long", self.up_long.as_str()),
            ("down_long", self.down_long.as_str()),
        ]
    }
}
section!(Config {
    general: General = General::default(),
    overlay: Overlay = Overlay::default(),
    sounds: Sounds = Sounds::default(),
    engine: Engine = Engine::default(),
    audio: Audio = Audio::default(),
    injection: Injection = Injection::default(),
    postproc: Postproc = Postproc::default(),
    privacy: Privacy = Privacy::default(),
    enhance: Enhance = Enhance::default(),
    companion: Companion = Companion::default()
});

fn merge(target: &mut toml::Value, patch: toml::Value) {
    if let (Some(dst), Some(src)) = (target.as_table_mut(), patch.as_table()) {
        for (key, value) in src {
            if let Some(old) = dst.get_mut(key) {
                if key == "replacements" {
                    *old = value.clone();
                } else {
                    merge(old, value.clone());
                }
            } else {
                dst.insert(key.clone(), value.clone());
            }
        }
    } else {
        *target = patch;
    }
}
impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        if !path.exists() {
            return Ok(Self::default());
        }
        let mut config: Self = toml::from_str(&fs::read_to_string(path).context("Read config")?)
            .context("Parse config TOML")?;
        // Default migration: old installs inherited a deadline too short for CPU inference.
        // Apply only on disk load; live patches retain an explicitly chosen 15 seconds.
        if config.enhance.timeout_ms == 15000 {
            config.enhance.timeout_ms = 60000;
        }
        config.validate()?;
        Ok(config)
    }
    pub fn patched(&self, patch: serde_json::Value) -> Result<Self> {
        if !patch.is_object() {
            bail!("Configuration patch must be an object");
        }
        let mut value = toml::Value::try_from(self).context("Serialize config")?;
        merge(
            &mut value,
            toml::Value::try_from(patch).context("Invalid configuration value")?,
        );
        let result: Self = value.try_into().context("Invalid config field type")?;
        result.validate()?;
        Ok(result)
    }
    pub fn validate(&self) -> Result<()> {
        crate::hotkey::bindings(self)?;
        for (value, choices) in [
            (self.general.hotkey_mode.as_str(), &["hold", "toggle"][..]),
            (self.general.theme.as_str(), &["auto", "light", "dark"][..]),
            (
                self.overlay.position.as_str(),
                &["bottom-center", "top-center", "custom"][..],
            ),
            (self.engine.device.as_str(), &["auto", "cuda", "cpu"][..]),
            (
                self.engine.compute_type.as_str(),
                &[
                    "auto",
                    "int8",
                    "int8_float16",
                    "float16",
                    "float32",
                    "int8_float32",
                ][..],
            ),
            (
                self.injection.method.as_str(),
                &["auto", "type", "clipboard"][..],
            ),
            (self.postproc.style.as_str(), &["prose", "code", "auto"][..]),
            (
                self.enhance.style.as_str(),
                &["concise", "thorough", "structured"][..],
            ),
            // Phase 1 accepts only by copying; verified replacement is not implemented.
            (self.enhance.accept_mode.as_str(), &["clipboard"][..]),
            (
                self.companion.transport.as_str(),
                &["auto", "usb", "ble"][..],
            ),
            (
                self.companion.source.as_str(),
                &["auto", "local", "phone"][..],
            ),
        ] {
            if !choices.contains(&value) {
                bail!("Unsupported configuration value: {value}");
            }
        }
        if self
            .companion
            .network
            .bind
            .parse::<std::net::IpAddr>()
            .is_err()
        {
            bail!("Companion network bind must be an IP address");
        }
        if self.companion.network.port == 0 {
            bail!("Companion network port must be nonzero");
        }
        for (key, value) in self.companion.buttons.pairs() {
            if crate::companion::map::Verb::parse(value).is_none() {
                bail!("Unsupported companion verb for {key}: {value}");
            }
        }
        if !(0.8..=1.6).contains(&self.overlay.scale) {
            bail!("Overlay scale must be 0.8–1.6");
        }
        if !(0.0..=1.0).contains(&self.sounds.volume) {
            bail!("Sound volume must be 0–1");
        }
        if !(0.0..=10.0).contains(&self.audio.gain) {
            bail!("Audio gain must be 0–10");
        }
        if !(0.0..=1.0).contains(&self.audio.silence_threshold) {
            bail!("Invalid silence threshold");
        }
        if !(1..=90).contains(&self.audio.max_utterance_s)
            || self.audio.min_utterance_ms == 0
            || self.audio.min_utterance_ms > self.audio.max_utterance_s * 1000
            || !(100..=60_000).contains(&self.audio.silence_timeout_ms)
        {
            bail!("Invalid audio duration limits (maximum 90 seconds)");
        }
        if !(1..=10).contains(&self.engine.beam_size) {
            bail!("Beam size must be 1–10");
        }
        let p = &self.overlay.placement;
        if !(p.x_fraction.is_finite()
            && p.y_fraction.is_finite()
            && (0.0..=1.0).contains(&p.x_fraction)
            && (0.0..=1.0).contains(&p.y_fraction))
        {
            bail!("Overlay placement fractions must be 0–1");
        }
        let e = &self.enhance;
        if e.threads > 16 {
            bail!("Enhance threads must be 0 (automatic) or 1–16");
        }
        if e.unload_after_s > 600 {
            bail!("Enhance unload delay must be 0–600 seconds");
        }
        if !(1_000..=120_000).contains(&e.load_timeout_ms) {
            bail!("Enhance load timeout must be 1–120 seconds");
        }
        if !(1_000..=120_000).contains(&e.timeout_ms) {
            bail!("Enhance rewrite timeout must be 1–120 seconds");
        }
        if e.model.len() > 128 {
            bail!("Enhance model identifier is too long");
        }
        if !(1..=3650).contains(&self.privacy.history_retention_days) {
            bail!("Invalid history retention");
        }
        if self
            .postproc
            .replacements
            .keys()
            .any(|s| s.trim().is_empty())
            || self.postproc.fillers.iter().any(|s| s.trim().is_empty())
        {
            bail!("Empty replacement/filler");
        }
        Ok(())
    }
    pub fn save(&self, path: &Path) -> Result<()> {
        self.validate()?;
        let parent = path.parent().context("Config path has no parent")?;
        fs::create_dir_all(parent).context("Create config directory")?;
        let temporary = path.with_extension("toml.tmp");
        let mut options = fs::OpenOptions::new();
        options.create(true).truncate(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(&temporary)
            .context("Create temporary config")?;
        file.write_all(
            toml::to_string_pretty(self)
                .context("Encode config")?
                .as_bytes(),
        )
        .context("Write config")?;
        file.sync_all().context("Sync config")?;
        drop(file);
        atomic_replace(&temporary, path).context("Atomically replace config")
    }
}
#[cfg(not(windows))]
fn atomic_replace(source: &Path, dest: &Path) -> Result<()> {
    fs::rename(source, dest)?;
    Ok(())
}
#[cfg(windows)]
fn atomic_replace(source: &Path, dest: &Path) -> Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };
    let s: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let d: Vec<u16> = dest.as_os_str().encode_wide().chain(Some(0)).collect();
    if unsafe {
        MoveFileExW(
            s.as_ptr(),
            d.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn defaults_and_patch() -> Result<()> {
        let c = Config::default().patched(serde_json::json!({"audio":{"gain":2.0}}))?;
        assert_eq!(c.audio.gain, 2.0);
        assert_eq!(c.general.hotkey, "Ctrl+Alt+D");
        Ok(())
    }
    #[test]
    fn unknown_keys_survive() -> Result<()> {
        let c: Config = toml::from_str("future = 'keep'\n[audio]\nnew_knob = 42")?;
        let c = c.patched(serde_json::json!({"audio":{"gain":1.5}}))?;
        assert_eq!(c.extra["future"].as_str(), Some("keep"));
        assert_eq!(c.audio.extra["new_knob"].as_integer(), Some(42));
        Ok(())
    }
    #[test]
    fn invalid_patch_rejected() {
        assert!(Config::default()
            .patched(serde_json::json!({"overlay":{"scale":4.0}}))
            .is_err());
        assert!(Config::default()
            .patched(serde_json::json!({"audio":{"gain":"bad"}}))
            .is_err());
    }
    #[test]
    fn enhance_defaults_and_migration() -> Result<()> {
        let c: Config =
            toml::from_str("[general]\nhotkey='Ctrl+Alt+D'\n[overlay]\nposition='top-center'")?;
        c.validate()?;
        assert!(!c.enhance.auto_rewrite);
        assert_eq!(c.enhance.rewrite_hotkey, "");
        assert_eq!(c.enhance.accept_hotkey, "");
        assert_eq!(c.enhance.model, "qwen3-1.7b-q4_k_m");
        assert_eq!(c.enhance.accept_mode, "clipboard");
        assert!(!c.enhance.rewrite_code);
        assert!(c.postproc.remove_duplicate_words);
        assert_eq!(c.overlay.position, "top-center");
        assert!(!c.overlay.placement.is_set());
        Ok(())
    }
    #[test]
    fn enhance_timeout_default_migration_on_load_only() -> Result<()> {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("target/config-timeout-test.toml");
        fs::create_dir_all(path.parent().unwrap())?;
        for (contents, expected) in [
            ("[enhance]\ntimeout_ms = 15000", 60000),
            ("[enhance]\ntimeout_ms = 20000", 20000),
            ("[enhance]", 60000),
        ] {
            fs::write(&path, contents)?;
            assert_eq!(Config::load(&path)?.enhance.timeout_ms, expected);
        }
        fs::remove_file(&path)?;
        assert_eq!(Config::load(&path)?.enhance.timeout_ms, 60000);
        let patched =
            Config::default().patched(serde_json::json!({"enhance":{"timeout_ms":15000}}))?;
        assert_eq!(patched.enhance.timeout_ms, 15000);
        for timeout in [1000, 60000, 120000] {
            assert!(Config::default()
                .patched(serde_json::json!({"enhance":{"timeout_ms":timeout}}))
                .is_ok());
        }
        Ok(())
    }
    #[test]
    fn enhance_validation() {
        let base = Config::default();
        for patch in [
            serde_json::json!({"enhance":{"style":"verbose"}}),
            serde_json::json!({"enhance":{"accept_mode":"verified_selection"}}),
            serde_json::json!({"enhance":{"threads":17}}),
            serde_json::json!({"enhance":{"unload_after_s":601}}),
            serde_json::json!({"enhance":{"timeout_ms":500}}),
            serde_json::json!({"enhance":{"timeout_ms":120001}}),
            serde_json::json!({"enhance":{"load_timeout_ms":200000}}),
            serde_json::json!({"overlay":{"placement":{"x_fraction":1.5}}}),
            serde_json::json!({"enhance":{"rewrite_hotkey":"Ctrl+Alt+D"}}),
            serde_json::json!({"enhance":{"rewrite_hotkey":"Ctrl+Alt+R","accept_hotkey":"Ctrl+Alt+R"}}),
            serde_json::json!({"enhance":{"accept_hotkey":"Escape"}}),
        ] {
            assert!(base.patched(patch.clone()).is_err(), "{patch}");
        }
        let ok = base
            .patched(serde_json::json!({"enhance":{"rewrite_hotkey":"Ctrl+Alt+R","accept_hotkey":"Ctrl+Alt+A","style":"structured"},"overlay":{"position":"custom"}}))
            .unwrap();
        assert_eq!(ok.enhance.style, "structured");
        // Unknown model IDs are retained, not rejected.
        let unknown = base
            .patched(serde_json::json!({"enhance":{"model":"other"}}))
            .unwrap();
        assert_eq!(unknown.enhance.model, "other");
    }
    #[test]
    fn atomic_roundtrip() -> Result<()> {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("target/config-test.toml");
        Config::default().save(&path)?;
        let c = Config::load(&path)?;
        assert_eq!(c.engine.model, "distil-small.en");
        c.save(&path)?;
        fs::remove_file(path)?;
        Ok(())
    }
    #[test]
    fn companion_network_defaults_and_validation() {
        let config = Config::default();
        assert!(!config.companion.network.enabled);
        assert_eq!(config.companion.network.bind, "127.0.0.1");
        assert_eq!(config.companion.network.port, 8787);
        assert!(!config.companion.network.mdns);
        assert_eq!(config.companion.source, "auto");
        for patch in [
            serde_json::json!({"companion":{"source":"remote"}}),
            serde_json::json!({"companion":{"network":{"bind":"example.com"}}}),
            serde_json::json!({"companion":{"network":{"port":0}}}),
        ] {
            assert!(config.patched(patch).is_err());
        }
        assert!(config.patched(serde_json::json!({"companion":{"source":"phone","network":{"enabled":true,"bind":"::1","port":9999}}})).is_ok());
    }
}
