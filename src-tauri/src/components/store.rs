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

pub const ENGINE_FILE: &str = if cfg!(windows) { "murmur-engine.exe" } else { "murmur-engine" };
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
}

fn io(message: &str) -> ComponentError {
    ComponentError::new(Reason::Io, message)
}

/// `fs::rename`, retried with backoff while Windows reports a sharing violation.
pub fn rename(from: &Path, to: &Path) -> std::io::Result<()> {
    let mut delay = Duration::from_millis(50);
    for attempt in 0.. {
        match fs::rename(from, to) {
            Ok(()) => return Ok(()),
            // ERROR_ACCESS_DENIED (5) / ERROR_SHARING_VIOLATION (32): a scanner or the
            // engine still holds a handle; give it a moment.
            Err(error) if cfg!(windows) && matches!(error.raw_os_error(), Some(5 | 32)) && attempt < 8 => {
                std::thread::sleep(delay);
                delay = delay.saturating_mul(2);
            }
            Err(error) => return Err(error),
        }
    }
    unreachable!()
}

impl Store {
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }
    pub fn component_dir(&self, id: &str) -> PathBuf {
        self.root.join(id)
    }
    pub fn version_dir(&self, id: &str, version: &str) -> PathBuf {
        self.root.join(id).join(version)
    }
    pub fn staging_dir(&self, id: &str, version: &str) -> PathBuf {
        self.root.join(id).join(format!(".staging-{version}"))
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
                    let done = self.downloads().join(format!("{}-{}", component.id, component.version)).join(&f.name);
                    if done.is_file() { size(done) } else { size(self.part(component, Some(&f.name))) }
                })
                .sum()
        }
    }
    pub fn active(&self, id: &str) -> Option<Active> {
        let bytes = fs::read(self.component_dir(id).join("active.json")).ok()?;
        serde_json::from_slice(&bytes).ok()
    }
    /// Directory holding the active version's files, if it exists.
    pub fn active_dir(&self, id: &str) -> Option<PathBuf> {
        let active = self.active(id)?;
        let dir = match &active.location {
            Some(location) => location.clone(),
            None => self.version_dir(id, &active.version),
        };
        dir.is_dir().then_some(dir)
    }
    pub fn write_active(&self, id: &str, active: &Active) -> Result<(), ComponentError> {
        write_atomic(
            &self.component_dir(id).join("active.json"),
            &serde_json::to_vec_pretty(active).map_err(|_| io("Could not record the component"))?,
        )
    }
    /// Move a smoke-tested staging directory into place and switch `active.json` to it.
    /// The previous version stays on disk unless `replace` asks to delete it afterwards.
    pub fn activate(&self, component: &Component, staging: &Path, replace: bool, now: u64) -> Result<(), ComponentError> {
        let target = self.version_dir(&component.id, &component.version);
        let previous = self.active(&component.id);
        let mut displaced = None;
        if target.exists() {
            // Reinstalling the same version: keep the old copy aside until the switch succeeds.
            let aside = self.component_dir(&component.id).join(format!(".old-{}-{now}", component.version));
            rename(&target, &aside).map_err(|_| io("Could not replace the installed version (is it in use?)"))?;
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
                        let _ = fs::remove_dir_all(self.version_dir(&component.id, &previous.version));
                    }
                }
            }
        }
        Ok(())
    }
    /// Delete everything the store holds for `id`. Callers check dependencies and use first.
    pub fn remove(&self, id: &str) -> Result<(), ComponentError> {
        match fs::remove_dir_all(self.component_dir(id)) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(io("Could not remove the component (is it in use?)")),
        }
    }
    /// Remove leftovers of interrupted activations; never touches active versions.
    pub fn sweep(&self, id: &str) {
        let Ok(entries) = fs::read_dir(self.component_dir(id)) else { return };
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            if name.starts_with(".staging-") || name.starts_with(".old-") {
                let _ = fs::remove_dir_all(entry.path());
            }
        }
    }
}

/// Dynamic `NEEDED` entries of a 64-bit little-endian ELF file (no external tools).
pub fn elf_needed(path: &Path) -> Option<Vec<String>> {
    let data = fs::read(path).ok()?;
    let u16_at = |o: usize| data.get(o..o + 2).map(|b| u16::from_le_bytes([b[0], b[1]]));
    let u64_at = |o: usize| data.get(o..o + 8).map(|b| u64::from_le_bytes(b.try_into().unwrap()));
    if data.get(0..4)? != b"\x7fELF" || *data.get(4)? != 2 || *data.get(5)? != 1 {
        return None;
    }
    let (phoff, phentsize, phnum) = (u64_at(0x20)? as usize, u16_at(0x36)? as usize, u16_at(0x38)? as usize);
    let mut loads = Vec::new();
    let mut dynamic = None;
    for i in 0..phnum {
        let base = phoff + i * phentsize;
        let kind = u32::from_le_bytes(data.get(base..base + 4)?.try_into().ok()?);
        let (offset, vaddr, filesz) = (u64_at(base + 8)?, u64_at(base + 16)?, u64_at(base + 32)?);
        match kind {
            1 => loads.push((vaddr, offset, filesz)),
            2 => dynamic = Some((offset as usize, filesz as usize)),
            _ => {}
        }
    }
    let (offset, size) = dynamic?;
    let mut needed = Vec::new();
    let mut strtab = None;
    for i in 0..size / 16 {
        let tag = u64_at(offset + i * 16)?;
        let value = u64_at(offset + i * 16 + 8)?;
        match tag {
            0 => break,
            1 => needed.push(value as usize),
            5 => strtab = Some(value),
            _ => {}
        }
    }
    let strtab = strtab?;
    let (vaddr, file_offset, _) = loads.iter().find(|(v, _, s)| strtab >= *v && strtab < v + s)?;
    let table = (strtab - vaddr + file_offset) as usize;
    needed
        .into_iter()
        .map(|index| {
            let start = table + index;
            let end = start + data.get(start..)?.iter().position(|b| *b == 0)?;
            String::from_utf8(data[start..end].to_vec()).ok()
        })
        .collect()
}

fn smoke_failed(message: impl Into<String>) -> ComponentError {
    ComponentError::new(Reason::SmokeTestFailed, message)
}

/// Run `program` with a deadline; true when it exits successfully.
fn runs(program: &Path, args: &[&str], timeout: Duration) -> bool {
    let mut command = Command::new(program);
    command.args(args).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let Ok(mut child) = command.spawn() else { return false };
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
                return Err(smoke_failed("The engine archive has no murmur-engine executable"));
            }
            if !runs(&engine, &["--help"], Duration::from_secs(120)) {
                return Err(smoke_failed("The downloaded engine does not start on this computer"));
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
                let needed = elf_needed(&dir.join(library)).ok_or_else(|| smoke_failed(format!("{library} is not a valid library")))?;
                for name in needed {
                    // CUDA libraries must resolve inside the add-on; libcuda is the driver's.
                    if name.starts_with("libcu") && !name.starts_with("libcuda.so") || name.starts_with("libnv") {
                        if !dir.join(&name).exists() {
                            return Err(smoke_failed(format!("{library} needs {name}, which the add-on lacks")));
                        }
                    }
                }
            }
        }
        Kind::Cuda => {
            let has_dll = fs::read_dir(dir).into_iter().flatten().flatten().any(|e| {
                e.file_name().to_string_lossy().to_ascii_lowercase().starts_with("cublas64_")
            });
            if !has_dll {
                return Err(smoke_failed("The GPU add-on lacks cuBLAS"));
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
pub fn verify_files(component: &Component, dir: &Path, cancel: &std::sync::atomic::AtomicBool) -> Result<Verdict, ComponentError> {
    for file in &component.files {
        let path = dir.join(&file.name);
        let Ok(meta) = fs::metadata(&path) else { return Ok(Verdict::Missing) };
        if meta.len() != file.size_bytes {
            return Ok(Verdict::Mismatch);
        }
        match fetch::hash_file(&path, Some(cancel)) {
            Ok((_, digest)) if digest.eq_ignore_ascii_case(&file.sha256) => {}
            Ok(_) => return Ok(Verdict::Mismatch),
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {
                return Err(ComponentError::new(Reason::Cancelled, "Verification cancelled"))
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
        let manifest = Manifest::parse(&serde_json::to_vec(&crate::components::manifest::tests::manifest(1, vec![value])).unwrap()).unwrap();
        manifest.components[0].clone()
    }
    fn stage(store: &Store, c: &Component, files: &[&str]) -> PathBuf {
        let staging = store.staging_dir(&c.id, &c.version);
        fs::create_dir_all(&staging).unwrap();
        for f in files {
            fs::write(staging.join(f), c.version.as_bytes()).unwrap();
        }
        staging
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
        assert_eq!(smoke_test(&v2, &staging).unwrap_err().reason, Reason::SmokeTestFailed);
        s.sweep("speech-x");
        assert!(!staging.exists());
        assert_eq!(s.active("speech-x").unwrap().version, "1");
        assert_eq!(fs::read(s.active_dir("speech-x").unwrap().join("model.bin")).unwrap(), b"1");
        // A good version 2 replaces 1; with replace=false the old directory stays.
        let staging = stage(&s, &v2, SPEECH_REQUIRED);
        s.activate(&v2, &staging, false, 2).unwrap();
        assert_eq!(s.active("speech-x").unwrap().version, "2");
        assert!(s.version_dir("speech-x", "1").is_dir());
        // Reinstalling the active version swaps it in place; with replace=true the previously
        // active version (here: the same one) is the only candidate, so version 1 stays.
        let staging = stage(&s, &v2, SPEECH_REQUIRED);
        s.activate(&v2, &staging, true, 3).unwrap();
        assert!(s.version_dir("speech-x", "1").is_dir());
        assert!(s.version_dir("speech-x", "2").join("config.json").is_file());
        assert!(fs::read_dir(s.component_dir("speech-x")).unwrap().flatten().all(|e| !e.file_name().to_string_lossy().starts_with(".old-")));
        // Version 3 with replace=true deletes the previously active version 2.
        let v3 = model("speech-x", "3");
        let staging = stage(&s, &v3, SPEECH_REQUIRED);
        s.activate(&v3, &staging, true, 4).unwrap();
        assert!(!s.version_dir("speech-x", "2").exists());
        let _ = fs::remove_dir_all(&s.root);
    }

    #[test]
    fn active_json_is_replaced_atomically() {
        let s = store("atomic");
        let v1 = model("speech-y", "1");
        let staging = stage(&s, &v1, SPEECH_REQUIRED);
        s.activate(&v1, &staging, false, 1).unwrap();
        let dir = s.component_dir("speech-y");
        let names: Vec<_> = fs::read_dir(&dir).unwrap().flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect();
        assert!(names.iter().all(|n| !n.ends_with(".tmp")), "{names:?}");
        // A torn write cannot exist: a leftover temp file never replaces the record.
        fs::write(dir.join("active.json.tmp"), b"{garbage").unwrap();
        assert_eq!(s.active("speech-y").unwrap().version, "1");
        // A corrupt record means "not active", never a crash or a guess.
        fs::write(dir.join("active.json"), b"{garbage").unwrap();
        assert!(s.active("speech-y").is_none());
        let _ = fs::remove_dir_all(&s.root);
    }

    #[test]
    fn elf_needed_reads_real_libraries() {
        // The test binary itself is a dynamically linked ELF on Linux.
        if cfg!(target_os = "linux") {
            let needed = elf_needed(&std::env::current_exe().unwrap()).unwrap();
            assert!(needed.iter().any(|n| n.starts_with("libc.so")), "{needed:?}");
        }
        assert!(elf_needed(Path::new("/nonexistent")).is_none());
    }

    #[test]
    fn engine_smoke_test_runs_help() {
        let s = store("engine");
        let mut value = component("engine", "engine");
        value["version"] = serde_json::json!("1");
        let manifest = Manifest::parse(&serde_json::to_vec(&crate::components::manifest::tests::manifest(1, vec![value])).unwrap()).unwrap();
        let engine = &manifest.components[0];
        let dir = s.staging_dir("engine", "1");
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
