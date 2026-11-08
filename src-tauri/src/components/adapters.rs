//! The component store seen through the engine's and Enhance's narrow interfaces.
use super::{enhance_id, jobs::Phase, Components, Reason};
use crate::{
    engine::Locate,
    enhance::{
        catalog::{Entry, InstallState},
        download, EnhanceError, ErrorCode, ModelStore,
    },
};
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};

impl Locate for Components {
    fn engine_dir(&self) -> Option<PathBuf> {
        Components::engine_dir(self)
    }
    fn cuda_dir(&self) -> Option<PathBuf> {
        Components::cuda_dir(self)
    }
    fn model_dir(&self, model: &str) -> Option<PathBuf> {
        self.speech_model_dir(model)
    }
}

/// Enhance installs and removals go through the component manager (one job per component,
/// visible on the Components page too); Enhance keeps its own install-state UI on top.
pub struct EnhanceStore(pub Arc<Components>);

fn enhance_error(reason: Reason, message: &str) -> EnhanceError {
    let code = match reason {
        Reason::Network | Reason::NotAllowed => ErrorCode::Network,
        Reason::InsufficientSpace => ErrorCode::InsufficientSpace,
        Reason::ChecksumMismatch => ErrorCode::ChecksumMismatch,
        Reason::SizeMismatch => ErrorCode::SizeMismatch,
        Reason::Cancelled => ErrorCode::Cancelled,
        Reason::Busy => ErrorCode::Busy,
        Reason::InUse | Reason::Io => ErrorCode::RemoveFailed,
        _ => ErrorCode::DownloadFailed,
    };
    EnhanceError::new(code, message)
}

impl ModelStore for EnhanceStore {
    fn state(&self, entry: &Entry) -> InstallState {
        let id = enhance_id(entry.id);
        if self.0.enhance_model_path(entry.id).is_some() {
            InstallState::Installed
        } else if self.0.store.active(&id).is_some() {
            InstallState::Corrupt
        } else {
            InstallState::NotInstalled
        }
    }
    fn model_path(&self, entry: &Entry) -> Option<PathBuf> {
        self.0.enhance_model_path(entry.id)
    }
    fn install(
        &self,
        entry: &'static Entry,
        cancel: &Arc<AtomicBool>,
        progress: &mut dyn FnMut(download::Phase, u64, u64),
    ) -> Result<(), EnhanceError> {
        let id = enhance_id(entry.id);
        self.0
            .install(std::slice::from_ref(&id))
            .map_err(|e| enhance_error(e.reason, &e.message))?;
        loop {
            std::thread::sleep(Duration::from_millis(100));
            if cancel.load(Ordering::Acquire) {
                let _ = self.0.stop(&id, false);
                while self.0.job_running(&id) {
                    std::thread::sleep(Duration::from_millis(50));
                }
                return Err(EnhanceError::new(
                    ErrorCode::Cancelled,
                    "Download cancelled",
                ));
            }
            match self.0.job(&id) {
                None if self.0.enhance_model_path(entry.id).is_some() => return Ok(()),
                None => {
                    return Err(EnhanceError::new(
                        ErrorCode::DownloadFailed,
                        "The Enhance model install did not complete",
                    ))
                }
                Some(job) => match job.phase {
                    Phase::Failed => {
                        let reason = job.reason.unwrap_or(Reason::Io);
                        return Err(enhance_error(
                            reason,
                            job.error.as_deref().unwrap_or("Download failed"),
                        ));
                    }
                    Phase::Paused => {
                        return Err(EnhanceError::new(
                            ErrorCode::Cancelled,
                            "Download paused in Components",
                        ))
                    }
                    Phase::Queued | Phase::Downloading => progress(
                        download::Phase::Downloading,
                        job.bytes_done,
                        job.bytes_total,
                    ),
                    _ => progress(download::Phase::Verifying, job.bytes_total, job.bytes_total),
                },
            }
        }
    }
    fn remove(&self, entry: &Entry) -> Result<(), EnhanceError> {
        let id = enhance_id(entry.id);
        if self.0.store.active(&id).is_none() {
            return Ok(());
        }
        self.0
            .remove(&id)
            .map_err(|e| enhance_error(e.reason, &e.message))
    }
}
