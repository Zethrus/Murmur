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
section!(Overlay {
    enabled: bool = true,
    position: String = "bottom-center".into(),
    scale: f64 = 1.0,
    show_preview: bool = true,
    idle_hide: bool = true
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
    allow_newlines: bool = false
});
section!(Privacy {
    history_enabled: bool = false,
    history_retention_days: u32 = 7
});
section!(Config {
    general: General = General::default(),
    overlay: Overlay = Overlay::default(),
    sounds: Sounds = Sounds::default(),
    engine: Engine = Engine::default(),
    audio: Audio = Audio::default(),
    injection: Injection = Injection::default(),
    postproc: Postproc = Postproc::default(),
    privacy: Privacy = Privacy::default()
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
        let config: Self = toml::from_str(&fs::read_to_string(path).context("Read config")?)
            .context("Parse config TOML")?;
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
        let primary = crate::hotkey::parse(&self.general.hotkey)?;
        if !self.general.hotkey_code.is_empty()
            && crate::hotkey::parse(&self.general.hotkey_code)? == primary
        {
            bail!("Dictation and code shortcuts must be different");
        }
        for (value, choices) in [
            (self.general.hotkey_mode.as_str(), &["hold", "toggle"][..]),
            (self.general.theme.as_str(), &["auto", "light", "dark"][..]),
            (
                self.overlay.position.as_str(),
                &["bottom-center", "top-center"][..],
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
        ] {
            if !choices.contains(&value) {
                bail!("Unsupported configuration value: {value}");
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
    fn atomic_roundtrip() -> Result<()> {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("target/config-test.toml");
        Config::default().save(&path)?;
        let c = Config::load(&path)?;
        assert_eq!(c.engine.model, "distil-small.en");
        c.save(&path)?;
        fs::remove_file(path)?;
        Ok(())
    }
}
