//! Compiled, immutable Enhance model metadata. No network request is needed to list it, and an
//! arbitrary configured string is never turned into a path or URL: only catalog entries are.
use serde::Serialize;
use std::{
    fs,
    path::{Path, PathBuf},
};

pub const DEFAULT_MODEL_ID: &str = "qwen3-1.7b-q4_k_m";

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
pub struct Entry {
    pub id: &'static str,
    pub display_name: &'static str,
    pub repo: &'static str,
    pub revision: &'static str,
    pub filename: &'static str,
    pub size_bytes: u64,
    pub sha256: &'static str,
}

pub const CATALOG: &[Entry] = &[Entry {
    id: DEFAULT_MODEL_ID,
    display_name: "Qwen3 1.7B (Q4_K_M)",
    repo: "unsloth/Qwen3-1.7B-GGUF",
    revision: "d7f544eead698dbd1f15126ef60b45a1e1933222",
    filename: "Qwen3-1.7B-Q4_K_M.gguf",
    size_bytes: 1_107_409_472,
    sha256: "b139949c5bd74937ad8ed8c8cf3d9ffb1e99c866c823204dc42c0d91fa181897",
}];

pub fn find(id: &str) -> Option<&'static Entry> {
    CATALOG.iter().find(|entry| entry.id == id)
}

#[cfg_attr(not(test), allow(dead_code))] // Paths and URL serve the test-only LegacyStore.
impl Entry {
    /// Immutable revision URL; the only URL Murmur ever requests for this model.
    pub fn url(&self) -> String {
        format!(
            "https://huggingface.co/{}/resolve/{}/{}",
            self.repo, self.revision, self.filename
        )
    }
    /// `<root>/<model-id>/<revision>/`
    pub fn dir(&self, root: &Path) -> PathBuf {
        root.join(self.id).join(self.revision)
    }
    pub fn model_path(&self, root: &Path) -> PathBuf {
        self.dir(root).join("model.gguf")
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum InstallState {
    NotInstalled,
    Downloading,
    Verifying,
    Installed,
    Removing,
    Corrupt,
    UnavailableModel,
    Error,
}

#[cfg_attr(not(test), allow(dead_code))]
#[derive(Debug, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct InstalledRecord {
    pub id: String,
    pub repo: String,
    pub revision: String,
    pub filename: String,
    pub size_bytes: u64,
    pub sha256: String,
    pub installed_at_unix: u64,
}

#[cfg_attr(not(test), allow(dead_code))]
impl InstalledRecord {
    pub fn for_entry(entry: &Entry, installed_at_unix: u64) -> Self {
        Self {
            id: entry.id.into(),
            repo: entry.repo.into(),
            revision: entry.revision.into(),
            filename: entry.filename.into(),
            size_bytes: entry.size_bytes,
            sha256: entry.sha256.into(),
            installed_at_unix,
        }
    }
    pub fn matches(&self, entry: &Entry) -> bool {
        self.id == entry.id
            && self.revision == entry.revision
            && self.size_bytes == entry.size_bytes
            && self.sha256.eq_ignore_ascii_case(entry.sha256)
    }
}

#[cfg_attr(not(test), allow(dead_code))]
/// Cheap local inspection (no hashing): metadata plus the installation record.
/// An orphaned `.part` file means "not installed".
pub fn local_state(entry: &Entry, root: &Path) -> InstallState {
    let dir = entry.dir(root);
    let model = dir.join("model.gguf");
    let Ok(meta) = fs::metadata(&model) else {
        return InstallState::NotInstalled;
    };
    let record = fs::read_to_string(dir.join("installed.json"))
        .ok()
        .and_then(|text| serde_json::from_str::<InstalledRecord>(&text).ok());
    match record {
        Some(record) if record.matches(entry) && meta.len() == entry.size_bytes => {
            InstallState::Installed
        }
        _ => InstallState::Corrupt,
    }
}
