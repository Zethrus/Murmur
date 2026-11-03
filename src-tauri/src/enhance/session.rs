//! Pure Enhance session state: the last delivered dictation, the active rewrite request, and
//! the suggestion lifecycle. No threads, Tauri or I/O, so every transition is unit-testable.
//! Every result carries (request_id, dictation_id, generation); anything else is stale.
use super::{output::Outcome, EnhanceError, ErrorCode};
use crate::{config::Config, inject::InjectionPath, postproc::Style};
use serde::Serialize;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const SESSION_TTL: Duration = Duration::from_secs(120);
pub const NOTICE_TTL: Duration = Duration::from_secs(4);
pub const COPIED: &str = "Copied. Select the original dictation and paste to replace it.";

#[derive(Debug, Clone)]
pub struct DeliveryRecord {
    pub dictation_id: u64,
    pub cleaned_body: String,
    /// Exact text delivered (kept for diagnostics/tests; never logged).
    #[allow(dead_code)]
    pub delivered_text: String,
    #[allow(dead_code)]
    pub trailing_separator: String,
    pub style: Style,
    pub focused: String,
    pub path: InjectionPath,
    pub completed: Instant,
    pub config: Config,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    None,
    Loading,
    Rewriting,
    Ready,
    Accepting,
    Notice,
}

#[derive(Debug, Clone)]
pub struct Suggestion {
    pub id: u64,
    pub dictation_id: u64,
    pub original: String,
    /// Delivery-formatted body (no trailing separator) shown in the preview.
    pub proposed: String,
    /// Exact clipboard text on accept: proposed + configured separator.
    pub delivery: String,
    pub copied: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RewriteStatus {
    Queued,
    Revealed,
    NoDictation,
    Busy,
    AlreadyRunning,
    ModelMissing,
    CodeStyle,
    Unavailable,
    Expired,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Trigger {
    #[default]
    Auto,
    Manual,
}

/// Facts about the environment needed to decide whether a rewrite may start.
#[derive(Debug, Clone, Copy)]
pub struct Gate {
    pub installed: bool,
    pub backend: bool,
    pub overlay: bool,
    pub dictating: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AcceptDecision {
    Copy { text: String, again: bool },
    Stale,
}

#[derive(Debug, Default)]
pub struct Session {
    pub generation: u64,
    pub record: Option<DeliveryRecord>,
    pub request: Option<u64>,
    pub phase: PhaseState,
    pub suggestion: Option<Suggestion>,
    pub message: Option<String>,
    pub expires: Option<Instant>,
    pub notice_until: Option<Instant>,
    /// What started the active request (auto requests are cancelled when auto mode turns off).
    pub trigger: Option<Trigger>,
    expired: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PhaseState(pub Phase);
impl Default for PhaseState {
    fn default() -> Self {
        Self(Phase::None)
    }
}

pub fn rewritable(record: &DeliveryRecord) -> bool {
    record.config.enhance.rewrite_code
        || (record.style == Style::Prose && !crate::inject::is_terminal(&record.focused))
}

impl Session {
    pub fn phase(&self) -> Phase {
        self.phase.0
    }
    fn set(&mut self, phase: Phase) {
        self.phase = PhaseState(phase);
    }
    /// Pending or ready suggestions keep the overlay visible regardless of idle_hide.
    pub fn holds_overlay(&self) -> bool {
        self.phase() != Phase::None
    }
    /// A new dictation invalidates everything. Returns the request to cancel, if any.
    pub fn begin_dictation(&mut self) -> Option<u64> {
        self.generation += 1;
        self.record = None;
        self.suggestion = None;
        self.message = None;
        self.expires = None;
        self.notice_until = None;
        self.expired = false;
        self.set(Phase::None);
        self.request.take()
    }
    pub fn record_delivery(&mut self, record: DeliveryRecord) {
        self.expires = Some(record.completed + SESSION_TTL);
        self.expired = false;
        self.record = Some(record);
    }
    pub fn notice(&mut self, message: &str, now: Instant) {
        self.set(Phase::Notice);
        self.message = Some(message.into());
        self.notice_until = Some(now + NOTICE_TTL);
    }
    /// Decide whether a rewrite of the last dictation may start. On success the caller must
    /// call `started` with a fresh request ID.
    pub fn check(
        &mut self,
        trigger: Trigger,
        gate: Gate,
        now: Instant,
    ) -> Result<(), RewriteStatus> {
        if gate.dictating {
            return Err(RewriteStatus::Busy);
        }
        let Some(record) = &self.record else {
            if trigger == Trigger::Manual {
                let status = if self.expired {
                    RewriteStatus::Expired
                } else {
                    RewriteStatus::NoDictation
                };
                self.notice("Dictate something first.", now);
                return Err(status);
            }
            return Err(RewriteStatus::NoDictation);
        };
        if self.request.is_some() {
            return Err(RewriteStatus::AlreadyRunning);
        }
        if self
            .suggestion
            .as_ref()
            .is_some_and(|s| s.dictation_id == record.dictation_id)
        {
            if self.phase() != Phase::Ready {
                self.set(Phase::Ready);
                self.message = None;
                self.notice_until = None;
            }
            return Err(RewriteStatus::Revealed);
        }
        if !matches!(
            record.path,
            InjectionPath::Type | InjectionPath::Clipboard | InjectionPath::ClipboardOnly
        ) || record.cleaned_body.trim().is_empty()
        {
            return Err(RewriteStatus::NoDictation);
        }
        // The overlay is the only place a suggestion can be reviewed.
        if !gate.overlay {
            return Err(RewriteStatus::Unavailable);
        }
        if !rewritable(record) {
            if trigger == Trigger::Manual {
                self.notice("Code and terminal dictation is not rewritten.", now);
            }
            return Err(RewriteStatus::CodeStyle);
        }
        if !gate.backend {
            if trigger == Trigger::Manual {
                self.notice("This build does not include the Enhance engine.", now);
            }
            return Err(RewriteStatus::Unavailable);
        }
        if !gate.installed {
            self.notice(
                "Enhance model is not installed. Install it in Settings → Behavior.",
                now,
            );
            return Err(RewriteStatus::ModelMissing);
        }
        Ok(())
    }
    pub fn started(&mut self, request_id: u64, loaded: bool, trigger: Trigger) {
        self.request = Some(request_id);
        self.trigger = Some(trigger);
        self.message = None;
        self.notice_until = None;
        self.set(if loaded {
            Phase::Rewriting
        } else {
            Phase::Loading
        });
    }
    pub fn progress(&mut self, request_id: u64, phase: Phase) -> bool {
        if self.request == Some(request_id) && matches!(phase, Phase::Loading | Phase::Rewriting) {
            self.set(phase);
            return true;
        }
        false
    }
    /// Apply a worker result. Returns false (and changes nothing) for stale results.
    pub fn finish(
        &mut self,
        request_id: u64,
        dictation_id: u64,
        generation: u64,
        result: &Result<Outcome, EnhanceError>,
        suggestion_id: u64,
        now: Instant,
    ) -> bool {
        let current = self.request == Some(request_id)
            && generation == self.generation
            && self
                .record
                .as_ref()
                .is_some_and(|r| r.dictation_id == dictation_id);
        if !current {
            return false;
        }
        self.request = None;
        let record = self.record.as_ref().expect("checked above");
        match result {
            Ok(Outcome::Changed(text)) => {
                let (delivery, separator) =
                    crate::postproc::format_delivery(text, &record.config, &record.focused);
                let proposed = delivery
                    .strip_suffix(separator.as_str())
                    .unwrap_or(&delivery)
                    .to_owned();
                if proposed.is_empty() || proposed == record.cleaned_body.trim() {
                    self.notice("No changes suggested", now);
                } else {
                    self.suggestion = Some(Suggestion {
                        id: suggestion_id,
                        dictation_id,
                        original: record.cleaned_body.clone(),
                        proposed,
                        delivery,
                        copied: false,
                    });
                    self.message = None;
                    self.notice_until = None;
                    self.expires = Some(now + SESSION_TTL);
                    self.set(Phase::Ready);
                }
            }
            Ok(Outcome::Unchanged) => self.notice("No changes suggested", now),
            Err(error) if error.code == ErrorCode::Cancelled => {
                self.set(Phase::None);
                self.message = None;
            }
            Err(error) => self.notice(&error.message, now),
        }
        true
    }
    pub fn accept(&mut self, suggestion_id: u64) -> AcceptDecision {
        match &self.suggestion {
            Some(s) if s.id == suggestion_id => {
                let again = s.copied;
                let text = s.delivery.clone();
                if !again {
                    self.set(Phase::Accepting);
                }
                AcceptDecision::Copy { text, again }
            }
            _ => AcceptDecision::Stale,
        }
    }
    pub fn accepted(&mut self, suggestion_id: u64, result: Result<(), String>, now: Instant) {
        let Some(s) = self.suggestion.as_mut().filter(|s| s.id == suggestion_id) else {
            return;
        };
        match result {
            Ok(()) => {
                s.copied = true;
                self.notice(COPIED, now);
            }
            Err(message) => {
                self.set(Phase::Ready);
                self.message = Some(message);
            }
        }
    }
    /// Idempotent. Returns a running request to cancel when discarding a pending rewrite.
    pub fn discard(&mut self, suggestion_id: Option<u64>) -> Option<u64> {
        match suggestion_id {
            Some(id) if self.suggestion.as_ref().is_some_and(|s| s.id == id) => {
                self.suggestion = None;
                self.message = None;
                self.notice_until = None;
                self.set(Phase::None);
                None
            }
            Some(id) if self.request == Some(id) => {
                self.request = None;
                self.message = None;
                self.notice_until = None;
                self.set(Phase::None);
                Some(id)
            }
            Some(_) => None,
            None => {
                let request = self.request.take();
                if request.is_some() || self.phase() == Phase::Notice {
                    self.message = None;
                    self.notice_until = None;
                    self.set(Phase::None);
                }
                request
            }
        }
    }
    /// Clears expired text from memory. Returns true if anything visible changed.
    pub fn tick(&mut self, now: Instant) -> bool {
        let mut changed = false;
        if self.notice_until.is_some_and(|at| now >= at) {
            self.notice_until = None;
            self.message = None;
            if self.phase() == Phase::Notice {
                // A copied suggestion stays acceptable (idempotent) but is no longer shown.
                self.set(Phase::None);
            }
            changed = true;
        }
        if self.request.is_none() && self.expires.is_some_and(|at| now >= at) {
            self.record = None;
            self.suggestion = None;
            self.expires = None;
            self.expired = true;
            if self.phase() != Phase::None {
                self.set(Phase::None);
                self.message = None;
            }
            changed = true;
        }
        changed
    }
    pub fn expires_at_unix_ms(&self, now: Instant) -> Option<u64> {
        let at = self.expires?;
        self.suggestion.as_ref()?;
        let remaining = at.saturating_duration_since(now);
        SystemTime::now()
            .checked_add(remaining)?
            .duration_since(UNIX_EPOCH)
            .ok()
            .map(|d| d.as_millis() as u64)
    }
    /// Invalidate pending work without forgetting the last dictation (model removal,
    /// incompatible config change). Returns the request to cancel.
    pub fn invalidate(&mut self) -> Option<u64> {
        self.generation += 1;
        self.suggestion = None;
        self.message = None;
        self.notice_until = None;
        self.set(Phase::None);
        self.request.take()
    }
}
