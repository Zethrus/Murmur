//! On-disk component store under `<data>/components`:
//!
//! ```text
//! <id>/<version>/            installed files (read-only by convention)
//! <id>/.staging-<version>/   extraction and smoke test before activation
//! <id>/active.json           which version is in use (atomically replaced)
//! downloads/                 resumable .part files
//! ```
//! Nothing outside `<data>/components` is ever deleted by the store.
use super::{
    fetch,
    manifest::{Component, Kind},
    write_atomic, ComponentError, Reason,
};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

pub const ENGINE_FILE: &str = if cfg!(windows) {
    "murmur-engine.exe"
} else {
    "murmur-engine"
};
/// Files a speech model directory must contain for faster-whisper.
pub const SPEECH_REQUIRED: &[&str] = &["model.bin", "config.json", "tokenizer.json"];

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Active {
    pub version: String,
    pub kind: Kind,
    #[serde(default)]
    pub model: Option<String>,
    /// Files verified in place outside the store (a migration that could not be moved).
    #[serde(default)]
    pub location: Option<PathBuf>,
    #[serde(default)]
    pub migrated: bool,
    pub installed_at: u64,
}

#[derive(Debug, Clone)]
pub struct Store {
    pub root: PathBuf,
    /// Pre-0.5 model folders: the only places outside the store an `active.json` may point
    /// to (a verified migration that could not be moved).
    legacy: Vec<PathBuf>,
}

fn io(message: &str) -> ComponentError {
    ComponentError::new(Reason::Io, message)
}

/// How long a Windows rename may keep retrying a transient failure in total.
pub const RENAME_BUDGET: Duration = Duration::from_secs(5);

/// `fs::rename`, retried with backoff for up to [`RENAME_BUDGET`] while Windows reports a
/// sharing violation or access denied. Verified in source, not run on hardware.
pub fn rename(from: &Path, to: &Path) -> std::io::Result<()> {
    retry_transient(|| fs::rename(from, to), cfg!(windows), std::thread::sleep)
}

/// ERROR_ACCESS_DENIED (5) / ERROR_SHARING_VIOLATION (32): an antivirus scanner, the search
/// indexer or a just-stopped engine still holds a handle; it is usually gone within seconds.
fn transient(error: &std::io::Error) -> bool {
    matches!(error.raw_os_error(), Some(5 | 32))
}

/// Shared retry schedule: 50 ms doubling to 1 s, never sleeping past [`RENAME_BUDGET`].
fn retry_transient(
    mut operation: impl FnMut() -> std::io::Result<()>,
    windows: bool,
    mut sleep: impl FnMut(Duration),
) -> std::io::Result<()> {
    let mut delay = Duration::from_millis(50);
    let mut waited = Duration::ZERO;
    loop {
        match operation() {
            Ok(()) => return Ok(()),
            Err(error) if windows && transient(&error) && waited + delay <= RENAME_BUDGET => {
                sleep(delay);
                waited += delay;
                delay = (delay * 2).min(Duration::from_secs(1));
            }
            Err(error) => return Err(error),
        }
    }
}

impl Store {
    pub fn new(root: PathBuf) -> Self {
        Self {
            root,
            legacy: Vec::new(),
        }
    }
    pub fn with_legacy(mut self, legacy: Vec<PathBuf>) -> Self {
        self.legacy = legacy;
        self
    }
    /// `<root>/<id>`. Ids become paths, so an unsafe one (`..`, separators) is refused here
    /// as well as at every public entry point.
    pub fn component_dir(&self, id: &str) -> Result<PathBuf, ComponentError> {
        if !super::manifest::safe_id(id) {
            return Err(ComponentError::new(Reason::NotFound, "Unknown component"));
        }
        Ok(self.root.join(id))
    }
    pub fn version_dir(&self, id: &str, version: &str) -> Result<PathBuf, ComponentError> {
        if !super::manifest::safe_version(version) {
            return Err(ComponentError::new(
                Reason::NotFound,
                "Unknown component version",
            ));
        }
        Ok(self.component_dir(id)?.join(version))
    }
    pub fn staging_dir(&self, id: &str, version: &str) -> Result<PathBuf, ComponentError> {
        if !super::manifest::safe_version(version) {
            return Err(ComponentError::new(
                Reason::NotFound,
                "Unknown component version",
            ));
        }
        Ok(self.component_dir(id)?.join(format!(".staging-{version}")))
    }
    /// An `active.json` location outside the version folder must be an absolute path without
    /// `..` inside the component's own folder or a pre-0.5 model folder.
    fn location_allowed(&self, id: &str, location: &Path) -> bool {
        use std::path::Component as Part;
        let Ok(own) = self.component_dir(id) else {
            return false;
        };
        location.is_absolute()
            && !location
                .components()
                .any(|c| matches!(c, Part::ParentDir | Part::CurDir))
            && std::iter::once(&own)
                .chain(&self.legacy)
                .any(|base| location.starts_with(base) && location != base)
    }
    pub fn downloads(&self) -> PathBuf {
        self.root.join("downloads")
    }
    /// `.part` file of a zip, or of one file in a multi-file component.
    pub fn part(&self, component: &Component, file: Option<&str>) -> PathBuf {
        let base = format!("{}-{}", component.id, component.version);
        match file {
            None => self.downloads().join(format!("{base}.part")),
            Some(name) => self.downloads().join(base).join(format!("{name}.part")),
        }
    }
    pub fn discard_parts(&self, component: &Component) {
        let base = format!("{}-{}", component.id, component.version);
        let _ = fs::remove_file(self.downloads().join(format!("{base}.part")));
        let _ = fs::remove_dir_all(self.downloads().join(base));
    }
    pub fn part_bytes(&self, component: &Component) -> u64 {
        let size = |p: PathBuf| fs::metadata(p).map(|m| m.len()).unwrap_or(0);
        if component.files.is_empty() {
            size(self.part(component, None))
        } else {
            component
                .files
                .iter()
                .map(|f| {
                    let done = self
                        .downloads()
                        .join(format!("{}-{}", component.id, component.version))
                        .join(&f.name);
                    if done.is_file() {
                        size(done)
                    } else {
                        size(self.part(component, Some(&f.name)))
                    }
                })
                .sum()
        }
    }
    pub fn active(&self, id: &str) -> Option<Active> {
        let bytes = fs::read(self.component_dir(id).ok()?.join("active.json")).ok()?;
        let active: Active = serde_json::from_slice(&bytes).ok()?;
        // Unsigned: the version and location become paths, so a record naming `..` or a
        // folder outside the store and the legacy model folders is treated as absent.
        let location_ok = active
            .location
            .as_deref()
            .is_none_or(|l| self.location_allowed(id, l));
        (super::manifest::safe_version(&active.version) && location_ok).then_some(active)
    }
    /// Directory holding the active version's files, if it exists.
    pub fn active_dir(&self, id: &str) -> Option<PathBuf> {
        let active = self.active(id)?;
        let dir = match &active.location {
            Some(location) => location.clone(),
            None => self.version_dir(id, &active.version).ok()?,
        };
        dir.is_dir().then_some(dir)
    }
    pub fn write_active(&self, id: &str, active: &Active) -> Result<(), ComponentError> {
        write_atomic(
            &self.component_dir(id)?.join("active.json"),
            &serde_json::to_vec_pretty(active).map_err(|_| io("Could not record the component"))?,
        )
    }
    /// Move a smoke-tested staging directory into place and switch `active.json` to it.
    /// The previous version stays on disk unless `replace` asks to delete it afterwards.
    pub fn activate(
        &self,
        component: &Component,
        staging: &Path,
        replace: bool,
        now: u64,
    ) -> Result<(), ComponentError> {
        let target = self.version_dir(&component.id, &component.version)?;
        let previous = self.active(&component.id);
        let mut displaced = None;
        if target.exists() {
            // Reinstalling the same version: keep the old copy aside until the switch succeeds.
            // Only the CLI/test path (`install_from_files`, no `before_activate`) can get here
            // while the engine runs; the app stops it first.
            let aside = self
                .component_dir(&component.id)?
                .join(format!(".old-{}-{now}", component.version));
            rename(&target, &aside)
                .map_err(|_| io("Could not replace the installed version (is it in use?)"))?;
            displaced = Some(aside);
        }
        if rename(staging, &target).is_err() {
            if let Some(aside) = &displaced {
                let _ = rename(aside, &target);
            }
            return Err(io("Could not activate the new version"));
        }
        let record = Active {
            version: component.version.clone(),
            kind: component.kind,
            model: component.model.clone(),
            location: None,
            migrated: false,
            installed_at: now,
        };
        if let Err(error) = self.write_active(&component.id, &record) {
            let _ = rename(&target, staging);
            if let Some(aside) = &displaced {
                let _ = rename(aside, &target);
            }
            return Err(error);
        }
        if let Some(aside) = displaced {
            let _ = fs::remove_dir_all(aside);
        }
        if replace {
            if let Some(previous) = previous.filter(|p| p.version != component.version) {
                match previous.location {
                    // Files verified in place outside the store are never deleted by it.
                    Some(_) => {}
                    None => {
                        if let Ok(dir) = self.version_dir(&component.id, &previous.version) {
                            let _ = fs::remove_dir_all(dir);
                        }
                    }
                }
            }
        }
        Ok(())
    }
    /// Delete everything the store holds for `id`. Callers check dependencies and use first.
    pub fn remove(&self, id: &str) -> Result<(), ComponentError> {
        match fs::remove_dir_all(self.component_dir(id)?) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(io("Could not remove the component (is it in use?)")),
        }
    }
    /// Remove leftovers of interrupted activations; never touches active versions.
    pub fn sweep(&self, id: &str) {
        let Some(entries) = self
            .component_dir(id)
            .ok()
            .and_then(|d| fs::read_dir(d).ok())
        else {
            return;
        };
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            if name.starts_with(".staging-") || name.starts_with(".old-") {
                let _ = fs::remove_dir_all(entry.path());
            }
        }
    }
}

/// Dynamic `NEEDED` entries of a 64-bit little-endian ELF file (no external tools). Reads
/// only the header, program headers, dynamic segment and the strings it names: CUDA libraries
/// are hundreds of megabytes.
pub fn elf_needed(path: &Path) -> Option<Vec<String>> {
    use std::io::{Read, Seek, SeekFrom};
    let mut file = fs::File::open(path).ok()?;
    let mut read_at = |offset: u64, length: usize| -> Option<Vec<u8>> {
        if length > 16 << 20 {
            return None;
        }
        let mut buffer = vec![0u8; length];
        file.seek(SeekFrom::Start(offset)).ok()?;
        file.read_exact(&mut buffer).ok()?;
        Some(buffer)
    };
    let u16_at = |d: &[u8], o: usize| d.get(o..o + 2).map(|b| u16::from_le_bytes([b[0], b[1]]));
    let u32_at = |d: &[u8], o: usize| {
        d.get(o..o + 4)
            .map(|b| u32::from_le_bytes(b.try_into().unwrap()))
    };
    let u64_at = |d: &[u8], o: usize| {
        d.get(o..o + 8)
            .map(|b| u64::from_le_bytes(b.try_into().unwrap()))
    };
    let header = read_at(0, 64)?;
    if header.get(0..4)? != b"\x7fELF" || header[4] != 2 || header[5] != 1 {
        return None;
    }
    let (phoff, phentsize, phnum) = (
        u64_at(&header, 0x20)?,
        u16_at(&header, 0x36)? as usize,
        u16_at(&header, 0x38)? as usize,
    );
    let table = read_at(phoff, phentsize.checked_mul(phnum)?)?;
    let mut loads = Vec::new();
    let mut dynamic = None;
    for i in 0..phnum {
        let base = i * phentsize;
        let (offset, vaddr, filesz) = (
            u64_at(&table, base + 8)?,
            u64_at(&table, base + 16)?,
            u64_at(&table, base + 32)?,
        );
        match u32_at(&table, base)? {
            1 => loads.push((vaddr, offset, filesz)),
            2 => dynamic = Some((offset, filesz as usize)),
            _ => {}
        }
    }
    let (offset, size) = dynamic?;
    let entries = read_at(offset, size)?;
    let mut needed = Vec::new();
    let mut strtab = None;
    for i in 0..size / 16 {
        match (u64_at(&entries, i * 16)?, u64_at(&entries, i * 16 + 8)?) {
            (0, _) => break,
            (1, value) => needed.push(value),
            (5, value) => strtab = Some(value),
            _ => {}
        }
    }
    let strtab = strtab?;
    let (vaddr, file_offset, _) = loads
        .iter()
        .find(|(v, _, s)| strtab >= *v && strtab < v + s)?;
    let table = strtab - vaddr + file_offset;
    needed
        .into_iter()
        .map(|index| {
            let bytes = read_at(table + index, 256).or_else(|| read_at(table + index, 64))?;
            let end = bytes.iter().position(|b| *b == 0)?;
            String::from_utf8(bytes[..end].to_vec()).ok()
        })
        .collect()
}

fn smoke_failed(message: impl Into<String>) -> ComponentError {
    ComponentError::new(Reason::SmokeTestFailed, message)
}

/// Run `program` with a deadline; true when it exits successfully.
fn runs(program: &Path, args: &[&str], timeout: Duration) -> bool {
    let mut command = Command::new(program);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let Ok(mut child) = command.spawn() else {
        return false;
    };
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.success(),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(50)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return false;
            }
        }
    }
}

/// Check a staged component before it may become active.
pub fn smoke_test(component: &Component, dir: &Path) -> Result<(), ComponentError> {
    match component.kind {
        Kind::Engine => {
            let engine = dir.join(ENGINE_FILE);
            if !engine.is_file() {
                return Err(smoke_failed(
                    "The engine archive has no murmur-engine executable",
                ));
            }
            if !runs(&engine, &["--help"], Duration::from_secs(120)) {
                return Err(smoke_failed(
                    "The downloaded engine does not start on this computer",
                ));
            }
        }
        Kind::Cuda if cfg!(target_os = "linux") => {
            let libraries: Vec<_> = fs::read_dir(dir)
                .map_err(|_| smoke_failed("The GPU add-on is empty"))?
                .flatten()
                .map(|e| e.file_name().to_string_lossy().into_owned())
                .filter(|n| n.contains(".so"))
                .collect();
            if !libraries.iter().any(|n| n.starts_with("libcublas.so")) {
                return Err(smoke_failed("The GPU add-on lacks cuBLAS"));
            }
            for library in &libraries {
                let needed = elf_needed(&dir.join(library))
                    .ok_or_else(|| smoke_failed(format!("{library} is not a valid library")))?;
                for name in needed {
                    // CUDA libraries must resolve inside the add-on; libcuda is the driver's.
                    if name.starts_with("libcu") && !name.starts_with("libcuda.so")
                        || name.starts_with("libnv")
                    {
                        if !dir.join(&name).exists() {
                            return Err(smoke_failed(format!(
                                "{library} needs {name}, which the add-on lacks"
                            )));
                        }
                    }
                }
            }
        }
        Kind::Cuda => {
            // Windows add-on: cublas64_12.dll and cublasLt64_12.dll, both PE images.
            let names: Vec<String> = fs::read_dir(dir)
                .into_iter()
                .flatten()
                .flatten()
                .map(|e| e.file_name().to_string_lossy().to_ascii_lowercase())
                .collect();
            for prefix in ["cublas64_", "cublaslt64_"] {
                let Some(name) = names
                    .iter()
                    .find(|n| n.starts_with(prefix) && n.ends_with(".dll"))
                else {
                    return Err(smoke_failed("The GPU add-on lacks cuBLAS"));
                };
                let mut magic = [0u8; 2];
                let pe = fs::File::open(dir.join(name))
                    .and_then(|mut f| std::io::Read::read_exact(&mut f, &mut magic))
                    .is_ok()
                    && &magic == b"MZ";
                if !pe {
                    return Err(smoke_failed(format!("{name} is not a valid library")));
                }
            }
        }
        Kind::SpeechModel => {
            for name in SPEECH_REQUIRED {
                if !dir.join(name).is_file() {
                    return Err(smoke_failed(format!("The speech model lacks {name}")));
                }
            }
        }
        Kind::EnhanceModel => {
            let mut magic = [0u8; 4];
            let ok = fs::File::open(dir.join("model.gguf"))
                .and_then(|mut f| std::io::Read::read_exact(&mut f, &mut magic))
                .is_ok();
            if !ok || &magic != b"GGUF" {
                return Err(smoke_failed("The Enhance model is not a GGUF file"));
            }
        }
    }
    Ok(())
}

/// Result of checking files against the manifest.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    Verified,
    Mismatch,
    Missing,
}

/// Hash each manifest file inside `dir` (multi-file components only).
pub fn verify_files(
    component: &Component,
    dir: &Path,
    cancel: &std::sync::atomic::AtomicBool,
) -> Result<Verdict, ComponentError> {
    for file in &component.files {
        let path = dir.join(&file.name);
        let Ok(meta) = fs::metadata(&path) else {
            return Ok(Verdict::Missing);
        };
        if meta.len() != file.size_bytes {
            return Ok(Verdict::Mismatch);
        }
        match fetch::hash_file(&path, Some(cancel)) {
            Ok((_, digest)) if digest.eq_ignore_ascii_case(&file.sha256) => {}
            Ok(_) => return Ok(Verdict::Mismatch),
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {
                return Err(ComponentError::new(
                    Reason::Cancelled,
                    "Verification cancelled",
                ))
            }
            Err(_) => return Ok(Verdict::Missing),
        }
    }
    Ok(Verdict::Verified)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::components::manifest::{tests::component, Manifest};

    fn store(name: &str) -> Store {
        let root = std::env::temp_dir().join(format!("murmur-store-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        Store::new(root)
    }
    fn model(id: &str, version: &str) -> Component {
        let mut value = component(id, "speech-model");
        value["version"] = serde_json::json!(version);
        let manifest = Manifest::parse(
            &serde_json::to_vec(&crate::components::manifest::tests::manifest(
                1,
                vec![value],
            ))
            .unwrap(),
        )
        .unwrap();
        manifest.components[0].clone()
    }
    fn stage(store: &Store, c: &Component, files: &[&str]) -> PathBuf {
        let staging = store.staging_dir(&c.id, &c.version).unwrap();
        fs::create_dir_all(&staging).unwrap();
        for f in files {
            fs::write(staging.join(f), c.version.as_bytes()).unwrap();
        }
        staging
    }

    #[test]
    fn active_location_must_stay_inside_known_folders() {
        let base = store("location");
        let legacy = base.root.join("legacy-models");
        let s = Store::new(base.root.join("components")).with_legacy(vec![legacy.clone()]);
        let record = |location: Option<PathBuf>| Active {
            version: "1".into(),
            kind: Kind::SpeechModel,
            model: Some("x".into()),
            location,
            migrated: true,
            installed_at: 0,
        };
        let accepted = |location: Option<PathBuf>| {
            s.write_active("speech-x", &record(location)).unwrap();
            s.active("speech-x").is_some()
        };
        assert!(accepted(None));
        assert!(accepted(Some(legacy.join("x"))), "a pre-0.5 model folder");
        assert!(accepted(Some(
            s.component_dir("speech-x").unwrap().join("1")
        )));
        assert!(
            !accepted(Some(PathBuf::from("/etc"))),
            "outside every known folder"
        );
        assert!(!accepted(Some(legacy.clone())), "the legacy root itself");
        assert!(
            !accepted(Some(legacy.join("x/../../.."))),
            "parent components"
        );
        assert!(
            !accepted(Some(PathBuf::from("legacy-models/x"))),
            "relative"
        );
        assert!(
            !accepted(Some(s.component_dir("speech-y").unwrap())),
            "another component"
        );
        let _ = fs::remove_dir_all(&base.root);
    }

    #[test]
    fn staged_activation_keeps_previous_on_failed_smoke_test() {
        let s = store("activate");
        let v1 = model("speech-x", "1");
        let staging = stage(&s, &v1, SPEECH_REQUIRED);
        smoke_test(&v1, &staging).unwrap();
        s.activate(&v1, &staging, true, 1).unwrap();
        assert_eq!(s.active("speech-x").unwrap().version, "1");
        // Version 2 fails its smoke test: version 1 stays active and intact.
        let v2 = model("speech-x", "2");
        let staging = stage(&s, &v2, &["model.bin"]);
        assert_eq!(
            smoke_test(&v2, &staging).unwrap_err().reason,
            Reason::SmokeTestFailed
        );
        s.sweep("speech-x");
        assert!(!staging.exists());
        assert_eq!(s.active("speech-x").unwrap().version, "1");
        assert_eq!(
            fs::read(s.active_dir("speech-x").unwrap().join("model.bin")).unwrap(),
            b"1"
        );
        // A good version 2 replaces 1; with replace=false the old directory stays.
        let staging = stage(&s, &v2, SPEECH_REQUIRED);
        s.activate(&v2, &staging, false, 2).unwrap();
        assert_eq!(s.active("speech-x").unwrap().version, "2");
        assert!(s.version_dir("speech-x", "1").unwrap().is_dir());
        // Reinstalling the active version swaps it in place; with replace=true the previously
        // active version (here: the same one) is the only candidate, so version 1 stays.
        let staging = stage(&s, &v2, SPEECH_REQUIRED);
        s.activate(&v2, &staging, true, 3).unwrap();
        assert!(s.version_dir("speech-x", "1").unwrap().is_dir());
        assert!(s
            .version_dir("speech-x", "2")
            .unwrap()
            .join("config.json")
            .is_file());
        assert!(fs::read_dir(s.component_dir("speech-x").unwrap())
            .unwrap()
            .flatten()
            .all(|e| !e.file_name().to_string_lossy().starts_with(".old-")));
        // Version 3 with replace=true deletes the previously active version 2.
        let v3 = model("speech-x", "3");
        let staging = stage(&s, &v3, SPEECH_REQUIRED);
        s.activate(&v3, &staging, true, 4).unwrap();
        assert!(!s.version_dir("speech-x", "2").unwrap().exists());
        let _ = fs::remove_dir_all(&s.root);
    }

    #[test]
    fn windows_rename_retries_sharing_violations_for_about_five_seconds() {
        use std::io::{Error, ErrorKind};
        // Locked the whole time: gives up within the budget.
        let mut attempts = 0;
        let mut waited = Duration::ZERO;
        let result = retry_transient(
            || {
                attempts += 1;
                Err(Error::from_raw_os_error(32))
            },
            true,
            |d| waited += d,
        );
        assert_eq!(result.unwrap_err().raw_os_error(), Some(32));
        assert!(waited <= RENAME_BUDGET, "{waited:?}");
        assert!(waited >= Duration::from_secs(4), "{waited:?}");
        assert_eq!(attempts, 9);
        // Released after two tries (access denied, then sharing violation): succeeds.
        let mut codes = vec![5, 32].into_iter();
        let mut sleeps = 0;
        retry_transient(
            || {
                codes
                    .next()
                    .map_or(Ok(()), |c| Err(Error::from_raw_os_error(c)))
            },
            true,
            |_| sleeps += 1,
        )
        .unwrap();
        assert_eq!(sleeps, 2);
        // Other errors, and every error off Windows, fail at once.
        let mut slept = false;
        assert!(retry_transient(
            || Err(Error::new(ErrorKind::NotFound, "gone")),
            true,
            |_| slept = true
        )
        .is_err());
        assert!(retry_transient(
            || Err(Error::from_raw_os_error(32)),
            false,
            |_| slept = true
        )
        .is_err());
        assert!(!slept);
    }
    #[test]
    fn active_json_is_replaced_atomically() {
        let s = store("atomic");
        let v1 = model("speech-y", "1");
        let staging = stage(&s, &v1, SPEECH_REQUIRED);
        s.activate(&v1, &staging, false, 1).unwrap();
        let dir = s.component_dir("speech-y").unwrap();
        let names: Vec<_> = fs::read_dir(&dir)
            .unwrap()
            .flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .collect();
        assert!(names.iter().all(|n| !n.ends_with(".tmp")), "{names:?}");
        // A torn write cannot exist: a leftover temp file never replaces the record.
        fs::write(dir.join("active.json.tmp"), b"{garbage").unwrap();
        assert_eq!(s.active("speech-y").unwrap().version, "1");
        // A corrupt record means "not active", never a crash or a guess.
        fs::write(dir.join("active.json"), b"{garbage").unwrap();
        assert!(s.active("speech-y").is_none());
        // A version that would leave the component folder is refused.
        fs::write(
            dir.join("active.json"),
            br#"{"version":"../../x","kind":"speech-model","installed_at":0}"#,
        )
        .unwrap();
        assert!(s.active("speech-y").is_none() && s.active_dir("speech-y").is_none());
        let _ = fs::remove_dir_all(&s.root);
    }

    #[test]
    fn elf_needed_reads_real_libraries() {
        // The test binary itself is a dynamically linked ELF on Linux.
        if cfg!(target_os = "linux") {
            let needed = elf_needed(&std::env::current_exe().unwrap()).unwrap();
            assert!(
                needed.iter().any(|n| n.starts_with("libc.so")),
                "{needed:?}"
            );
        }
        assert!(elf_needed(Path::new("/nonexistent")).is_none());
    }

    #[test]
    fn engine_smoke_test_runs_help() {
        let s = store("engine");
        let mut value = component("engine", "engine");
        value["version"] = serde_json::json!("1");
        let manifest = Manifest::parse(
            &serde_json::to_vec(&crate::components::manifest::tests::manifest(
                1,
                vec![value],
            ))
            .unwrap(),
        )
        .unwrap();
        let engine = &manifest.components[0];
        let dir = s.staging_dir("engine", "1").unwrap();
        fs::create_dir_all(&dir).unwrap();
        assert!(smoke_test(engine, &dir).is_err(), "missing executable");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let exe = dir.join(ENGINE_FILE);
            fs::write(&exe, "#!/bin/sh\n[ \"$1\" = --help ]\n").unwrap();
            fs::set_permissions(&exe, fs::Permissions::from_mode(0o755)).unwrap();
            smoke_test(engine, &dir).unwrap();
            fs::write(&exe, "#!/bin/sh\nexit 3\n").unwrap();
            assert!(smoke_test(engine, &dir).is_err());
        }
        let _ = fs::remove_dir_all(&s.root);
    }
}
