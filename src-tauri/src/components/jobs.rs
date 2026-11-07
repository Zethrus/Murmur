//! One install job: space check → download (resumable) → verify → extract/stage → smoke test →
//! activate. Job records persist in `jobs.json` so an interrupted job shows as Paused after a
//! restart; Murmur never resumes one on its own.
use super::{
    archive,
    fetch::{self, Retry, Target, Transport},
    manifest::{Archive, Component},
    store::{self, Store, Verdict},
    write_atomic, ComponentError, Reason,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs,
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
};

pub const MARGIN: u64 = 100 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Queued,
    Downloading,
    Verifying,
    Extracting,
    Testing,
    Activating,
    Paused,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Record {
    pub id: String,
    pub version: String,
    pub phase: Phase,
    pub bytes_done: u64,
    pub bytes_total: u64,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub reason: Option<Reason>,
    /// Delete the previously active version after a successful switch.
    #[serde(default)]
    pub replace: bool,
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct File {
    jobs: BTreeMap<String, Record>,
}

/// Load persisted jobs; anything that was running when Murmur stopped becomes Paused.
pub fn load(path: &Path) -> BTreeMap<String, Record> {
    let file: File = fs::read(path)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_default();
    file.jobs
        .into_iter()
        // jobs.json is unsigned: IDs and versions become paths, so check them again.
        .filter(|(id, record)| {
            super::manifest::safe_id(id)
                && *id == record.id
                && super::manifest::safe_version(&record.version)
        })
        .map(|(id, mut record)| {
            if record.phase != Phase::Failed {
                record.phase = Phase::Paused;
            }
            (id, record)
        })
        .collect()
}

pub fn save(path: &Path, jobs: &BTreeMap<String, Record>) -> Result<(), ComponentError> {
    let file = File { jobs: jobs.clone() };
    write_atomic(path, &serde_json::to_vec_pretty(&file).unwrap_or_default())
}

pub fn format_bytes(bytes: u64) -> String {
    const UNITS: [&str; 4] = ["bytes", "KB", "MB", "GB"];
    let mut value = bytes as f64;
    let mut unit = 0;
    while value >= 1000.0 && unit < UNITS.len() - 1 {
        value /= 1000.0;
        unit += 1;
    }
    if unit == 0 {
        format!("{bytes} bytes")
    } else {
        format!("{value:.1} {}", UNITS[unit])
    }
}

/// Free space needed before a job may start.
pub fn check_space(component: &Component, free: Option<u64>) -> Result<(), ComponentError> {
    let Some(free) = free else { return Ok(()) };
    let needed = component.space_needed().saturating_add(MARGIN);
    if free < needed {
        return Err(ComponentError::new(
            Reason::InsufficientSpace,
            format!(
                "Not enough disk space for {}: needs {} free (download {} + installed {} + temporary {} + 100 MB margin), {} available.",
                component.display_name,
                format_bytes(needed),
                format_bytes(component.size_bytes),
                format_bytes(component.installed_size_bytes),
                format_bytes(component.temp_size_bytes),
                format_bytes(free)
            ),
        ));
    }
    Ok(())
}

pub struct Context<'a> {
    pub store: &'a Store,
    pub transport: &'a dyn Transport,
    pub cancel: &'a Arc<AtomicBool>,
    pub retry: Retry,
    pub free: Option<u64>,
    pub replace: bool,
    pub now: u64,
    /// Called right before the switch (stops the engine for engine/CUDA updates).
    pub before_activate: &'a dyn Fn(&Component),
}

fn cancelled() -> ComponentError {
    ComponentError::new(Reason::Cancelled, "Installation cancelled")
}

/// Download (or reuse) every file of `component`, then stage, test and activate it.
pub fn run(
    component: &Component,
    ctx: &Context<'_>,
    progress: &mut dyn FnMut(Phase, u64, u64),
) -> Result<(), ComponentError> {
    check_space(component, ctx.free)?;
    let total = component.size_bytes;
    let staging = ctx.store.staging_dir(&component.id, &component.version)?;
    ctx.store.sweep(&component.id);
    let result = (|| match component.archive {
        Archive::Zip => {
            let part = ctx.store.part(component, None);
            let sha = component.sha256.as_deref().unwrap_or_default();
            let target = Target {
                urls: &component.urls,
                size: total,
                sha256: sha,
                part: &part,
            };
            fetch::download(
                &target,
                ctx.transport,
                ctx.cancel,
                ctx.retry,
                &mut |done, all| progress(Phase::Downloading, done, all),
            )?;
            install_zip(component, &part, ctx, progress)
        }
        Archive::None => {
            let folder = ctx
                .store
                .downloads()
                .join(format!("{}-{}", component.id, component.version));
            let mut offset = 0u64;
            for file in &component.files {
                let done = folder.join(&file.name);
                if !(done.is_file()
                    && fs::metadata(&done).map(|m| m.len()).ok() == Some(file.size_bytes))
                {
                    let part = ctx.store.part(component, Some(&file.name));
                    let target = Target {
                        urls: &file.urls,
                        size: file.size_bytes,
                        sha256: &file.sha256,
                        part: &part,
                    };
                    fetch::download(
                        &target,
                        ctx.transport,
                        ctx.cancel,
                        ctx.retry,
                        &mut |d, _| progress(Phase::Downloading, offset + d, total),
                    )?;
                    store::rename(&part, &done).map_err(|_| {
                        ComponentError::new(Reason::Io, "Could not save the download")
                    })?;
                }
                offset += file.size_bytes;
                progress(Phase::Downloading, offset, total);
            }
            install_files(component, &folder, ctx, progress)
        }
    })();
    if result.is_err() {
        let _ = fs::remove_dir_all(&staging);
    }
    result
}

fn finish(
    component: &Component,
    staging: &Path,
    ctx: &Context<'_>,
    progress: &mut dyn FnMut(Phase, u64, u64),
) -> Result<(), ComponentError> {
    let total = component.size_bytes;
    progress(Phase::Testing, total, total);
    store::smoke_test(component, staging)?;
    if ctx.cancel.load(Ordering::Acquire) {
        return Err(cancelled());
    }
    progress(Phase::Activating, total, total);
    (ctx.before_activate)(component);
    ctx.store
        .activate(component, staging, ctx.replace, ctx.now)?;
    ctx.store.discard_parts(component);
    Ok(())
}

/// A verified zip on disk: extract, test, activate.
pub fn install_zip(
    component: &Component,
    zip: &Path,
    ctx: &Context<'_>,
    progress: &mut dyn FnMut(Phase, u64, u64),
) -> Result<(), ComponentError> {
    let total = component.size_bytes;
    let staging = ctx.store.staging_dir(&component.id, &component.version)?;
    let _ = fs::remove_dir_all(&staging);
    progress(Phase::Extracting, total, total);
    let limit = component
        .installed_size_bytes
        .saturating_add(component.installed_size_bytes / 20);
    archive::extract(zip, &staging, limit, ctx.cancel)?;
    finish(component, &staging, ctx, progress)
}

fn install_files(
    component: &Component,
    folder: &Path,
    ctx: &Context<'_>,
    progress: &mut dyn FnMut(Phase, u64, u64),
) -> Result<(), ComponentError> {
    let total = component.size_bytes;
    let staging = ctx.store.staging_dir(&component.id, &component.version)?;
    let _ = fs::remove_dir_all(&staging);
    fs::create_dir_all(&staging)
        .map_err(|_| ComponentError::new(Reason::Io, "Could not create the staging folder"))?;
    progress(Phase::Verifying, total, total);
    // Completed files may have waited on disk across restarts: hash them all again.
    match store::verify_files(component, folder, ctx.cancel)? {
        Verdict::Verified => {}
        _ => {
            ctx.store.discard_parts(component);
            return Err(ComponentError::new(
                Reason::ChecksumMismatch,
                "A downloaded file changed on disk; download it again",
            ));
        }
    }
    for file in &component.files {
        store::rename(&folder.join(&file.name), &staging.join(&file.name))
            .map_err(|_| ComponentError::new(Reason::Io, "Could not stage the download"))?;
    }
    finish(component, &staging, ctx, progress)
}

/// Install from a local zip (offline tests, `--install-component`): verify size and SHA-256
/// against the trusted manifest before anything is extracted.
pub fn install_local(
    component: &Component,
    zip: &Path,
    ctx: &Context<'_>,
) -> Result<(), ComponentError> {
    if component.archive != Archive::Zip {
        return Err(ComponentError::new(
            Reason::NotAllowed,
            "Only zip components can be installed from a file",
        ));
    }
    check_space(component, ctx.free)?;
    let (size, digest) = fetch::hash_file(zip, Some(ctx.cancel))
        .map_err(|_| ComponentError::new(Reason::Io, "Could not read the archive"))?;
    if size != component.size_bytes {
        return Err(ComponentError::new(
            Reason::SizeMismatch,
            "The archive size does not match the manifest",
        ));
    }
    if !component
        .sha256
        .as_deref()
        .is_some_and(|s| s.eq_ignore_ascii_case(&digest))
    {
        return Err(ComponentError::new(
            Reason::ChecksumMismatch,
            "The archive failed its SHA-256 check",
        ));
    }
    let result = install_zip(component, zip, ctx, &mut |_, _, _| {});
    if result.is_err() {
        if let Ok(staging) = ctx.store.staging_dir(&component.id, &component.version) {
            let _ = fs::remove_dir_all(staging);
        }
    }
    result
}
