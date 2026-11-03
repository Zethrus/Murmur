//! Glue between the pure Enhance session, the worker/service, the overlay and IPC. All entry
//! points are cheap and never block on inference; failures never reach dictation.
use super::{
    backend,
    prompt::Preset,
    session::{AcceptDecision, DeliveryRecord, Gate, Phase, RewriteStatus, Session, Trigger},
    worker::{self, Job, WorkerEvent},
    Enhance, InstallHooks,
};
use crate::{app::AppState, config::Config, inject::InjectionPath, overlay, postproc::Style};
use anyhow::Result;
use serde_json::{json, Value};
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tauri::{AppHandle, Emitter, Manager};

pub struct Runtime {
    pub service: Arc<Enhance>,
    session: Mutex<Session>,
    revision: AtomicU64,
    next_dictation: AtomicU64,
    partial: Mutex<String>,
    /// Kept alive: on X11 the owning process serves clipboard contents until dropped.
    clipboard: Mutex<Option<arboard::Clipboard>>,
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    super::lock(m)
}

pub fn parse_id(id: &str) -> Option<u64> {
    id.trim().parse().ok()
}

impl Runtime {
    pub fn new(app: &AppHandle, root: PathBuf, config: &Config) -> Result<Self> {
        let emitter = app.clone();
        let worker_app = app.clone();
        let service = Enhance::new(
            root,
            &config.enhance.model,
            config.enhance.threads,
            backend::default_backend(),
            Arc::new(move |name, value| {
                let _ = emitter.emit(name, value);
            }),
            Arc::new(move |event| {
                if let Some(state) = worker_app.try_state::<AppState>() {
                    state.enhance.on_worker(&state, event);
                }
            }),
        )?;
        Ok(Self {
            service: Arc::new(service),
            session: Mutex::new(Session::default()),
            revision: AtomicU64::new(0),
            next_dictation: AtomicU64::new(1),
            partial: Mutex::new(String::new()),
            clipboard: Mutex::new(None),
        })
    }

    pub fn holds_overlay(&self) -> bool {
        lock(&self.session).holds_overlay()
    }

    pub fn snapshot(&self, state: &AppState) -> Value {
        let dictation_state = state
            .status()
            .map(|s| s.state)
            .unwrap_or_else(|_| "idle".into());
        let partial = lock(&self.partial).clone();
        let session = lock(&self.session);
        let now = Instant::now();
        let shown = session
            .suggestion
            .as_ref()
            .filter(|_| matches!(session.phase(), Phase::Ready | Phase::Accepting));
        let revision = self.revision.fetch_add(1, Ordering::Relaxed) + 1;
        json!({
            "revision": revision,
            "dictation_state": dictation_state,
            "delivery_busy": overlay::busy(),
            "partial_text": partial,
            "enhancement": {
                "phase": session.phase(),
                "dictation_id": session.record.as_ref().map(|r| r.dictation_id.to_string()),
                "suggestion_id": shown.map(|s| s.id.to_string()),
                "request_id": session.request.map(|id| id.to_string()),
                "original_text": shown.map(|s| s.original.clone()),
                "proposed_text": shown.map(|s| s.proposed.clone()),
                "acceptance_mode": if backend::compiled() { "clipboard" } else { "unavailable" },
                "message": session.message.clone(),
                "expires_at": if shown.is_some() { session.expires_at_unix_ms(now) } else { None },
            }
        })
    }

    /// Emit the overlay snapshot (overlay window only) and keep its visibility in sync.
    pub fn publish(&self, state: &AppState) {
        let snapshot = self.snapshot(state);
        let _ = state.app.emit_to("overlay", "overlay-update", snapshot);
    }

    fn sync_overlay(&self, state: &AppState) {
        let Ok(config) = state.config() else { return };
        if self.holds_overlay() {
            if let Err(error) = overlay::reveal(&state.app, &config.overlay) {
                tracing::warn!(%error, "Reveal overlay for Enhance");
            }
        } else if config.overlay.idle_hide && state.status().is_ok_and(|s| s.state == "idle") {
            let _ = overlay::hide(&state.app);
        }
    }

    fn changed(&self, state: &AppState) {
        self.publish(state);
        self.sync_overlay(state);
    }

    pub fn set_partial(&self, state: &AppState, text: &str) {
        *lock(&self.partial) = text.to_owned();
        let _ = state.app.emit("partial", json!({ "text": text }));
        self.publish(state);
    }

    /// A new dictation started: forget the previous one and cancel pending work.
    pub fn begin_dictation(&self, state: &AppState) {
        let cancel = lock(&self.session).begin_dictation();
        if let Some(id) = cancel {
            self.service.worker.cancel(id);
        }
        self.publish(state);
    }

    #[allow(clippy::too_many_arguments)]
    pub fn delivered(
        &self,
        state: &AppState,
        cleaned_body: String,
        delivered_text: String,
        style: Style,
        focused: &str,
        path: InjectionPath,
        config: &Config,
    ) {
        let separator = if config.injection.trailing_space {
            " "
        } else {
            ""
        };
        let record = DeliveryRecord {
            dictation_id: self.next_dictation.fetch_add(1, Ordering::Relaxed),
            cleaned_body,
            delivered_text,
            trailing_separator: separator.into(),
            style,
            focused: focused.into(),
            path,
            completed: Instant::now(),
            config: config.clone(),
        };
        lock(&self.session).record_delivery(record);
        let current = state.config().unwrap_or_else(|_| config.clone());
        if current.enhance.auto_rewrite && self.service.installed() {
            let _ = self.rewrite(state, Trigger::Auto);
        } else {
            self.publish(state);
        }
    }

    /// Start (or reveal) a rewrite of the last dictation.
    pub fn rewrite(&self, state: &AppState, trigger: Trigger) -> Value {
        let reply = |status: RewriteStatus, message: Option<String>| json!({ "status": status, "message": message });
        let Ok(config) = state.config() else {
            return reply(
                RewriteStatus::Unavailable,
                Some("Configuration unavailable".into()),
            );
        };
        let dictating = state
            .status()
            .is_ok_and(|s| matches!(s.state.as_str(), "listening" | "transcribing"));
        let gate = Gate {
            installed: self.service.installed(),
            backend: backend::compiled(),
            overlay: config.overlay.enabled,
            dictating,
        };
        let job = {
            let mut session = lock(&self.session);
            if let Err(status) = session.check(trigger, gate, Instant::now()) {
                let message = session.message.clone();
                drop(session);
                self.changed(state);
                return reply(status, message);
            }
            let Some((entry, model_path)) = self.service.job_entry(&config.enhance.model) else {
                session.notice("Enhance model is not available.", Instant::now());
                drop(session);
                self.changed(state);
                return reply(RewriteStatus::ModelMissing, None);
            };
            let record = session.record.as_ref().expect("checked by Session::check");
            let request_id = self.service.next_id();
            let job = Job {
                request_id,
                dictation_id: record.dictation_id,
                generation: session.generation,
                body: record.cleaned_body.clone(),
                preset: Preset::parse(&config.enhance.style),
                dictionary: config.engine.dictionary.clone(),
                entry,
                model_path,
                threads: worker::effective_threads(config.enhance.threads),
                load_timeout: Duration::from_millis(config.enhance.load_timeout_ms),
                timeout: Duration::from_millis(config.enhance.timeout_ms),
                unload_after: Duration::from_secs(config.enhance.unload_after_s),
            };
            session.started(request_id, self.service.loaded(), trigger);
            job
        };
        self.service.worker.submit(job);
        self.changed(state);
        reply(RewriteStatus::Queued, None)
    }

    pub fn on_worker(&self, state: &AppState, event: WorkerEvent) {
        let visible = {
            let mut session = lock(&self.session);
            match event {
                WorkerEvent::Loading { request_id } => session.progress(request_id, Phase::Loading),
                WorkerEvent::Rewriting { request_id } => {
                    session.progress(request_id, Phase::Rewriting)
                }
                WorkerEvent::Finished {
                    request_id,
                    dictation_id,
                    generation,
                    result,
                } => {
                    let suggestion = self.service.next_id();
                    session.finish(
                        request_id,
                        dictation_id,
                        generation,
                        &result,
                        suggestion,
                        Instant::now(),
                    )
                }
                WorkerEvent::Unloaded => false,
            }
        };
        if visible {
            self.changed(state);
        }
    }

    fn copy(&self, text: &str) -> Result<(), String> {
        let mut clipboard = lock(&self.clipboard);
        if clipboard.is_none() {
            *clipboard = Some(arboard::Clipboard::new().map_err(|_| "Clipboard unavailable")?);
        }
        clipboard
            .as_mut()
            .ok_or("Clipboard unavailable")?
            .set_text(text.to_owned())
            .map_err(|_| "Could not copy the suggestion".to_owned())
    }

    /// Phase 1: accept copies the delivery-formatted suggestion; it never pastes.
    pub fn accept(&self, state: &AppState, suggestion_id: Option<u64>) -> Value {
        let decision = match suggestion_id {
            Some(id) => lock(&self.session).accept(id),
            None => AcceptDecision::Stale,
        };
        let AcceptDecision::Copy { text, .. } = decision else {
            return json!({ "outcome": "stale", "message": "This suggestion is no longer available." });
        };
        let id = suggestion_id.unwrap_or_default();
        let result = self.copy(&text);
        let outcome = if result.is_ok() { "copied" } else { "failed" };
        let message = result.as_ref().err().cloned();
        lock(&self.session).accepted(id, result, Instant::now());
        self.changed(state);
        json!({ "outcome": outcome, "message": message.or(Some(super::session::COPIED.into())) })
    }

    /// Accept shortcut: acts on the visible suggestion only.
    pub fn accept_current(&self, state: &AppState) {
        let id = {
            let session = lock(&self.session);
            session
                .suggestion
                .as_ref()
                .filter(|_| matches!(session.phase(), Phase::Ready | Phase::Notice))
                .map(|s| s.id)
        };
        if id.is_some() {
            let _ = self.accept(state, id);
        }
    }

    pub fn discard(&self, state: &AppState, suggestion_id: Option<u64>) {
        let cancel = {
            let mut session = lock(&self.session);
            let matches = suggestion_id.is_some_and(|id| {
                session.suggestion.as_ref().is_some_and(|s| s.id == id)
                    || session.request == Some(id)
            });
            if matches {
                session.discard(suggestion_id)
            } else {
                None
            }
        };
        if let Some(id) = cancel {
            self.service.worker.cancel(id);
        }
        self.changed(state);
    }

    pub fn tick(&self, state: &AppState) {
        if lock(&self.session).tick(Instant::now()) {
            self.changed(state);
        }
    }

    fn invalidate(&self) {
        if let Some(id) = lock(&self.session).invalidate() {
            self.service.worker.cancel(id);
        }
    }

    /// React to a committed configuration change.
    pub fn config_changed(&self, state: &AppState, old: &Config, new: &Config) {
        if old.enhance.auto_rewrite != new.enhance.auto_rewrite {
            // An explicit choice supersedes a pending "enable after install".
            self.service.clear_enable_intent();
        }
        self.service.refresh(&new.enhance.model);
        self.service.set_threads(new.enhance.threads);
        let model_changed = old.enhance.model != new.enhance.model;
        if model_changed || (old.overlay.enabled && !new.overlay.enabled) {
            self.invalidate();
        }
        if old.enhance.auto_rewrite && !new.enhance.auto_rewrite {
            let cancel = {
                let mut session = lock(&self.session);
                if session.request.is_some() && session.trigger == Some(Trigger::Auto) {
                    session.discard(None)
                } else {
                    None
                }
            };
            if let Some(id) = cancel {
                self.service.worker.cancel(id);
            }
        }
        if model_changed
            || (old.enhance.auto_rewrite && !new.enhance.auto_rewrite)
            || old.enhance.threads != new.enhance.threads
        {
            self.service.worker.unload_soon();
        }
        self.changed(state);
    }

    /// Explicit user-initiated download.
    pub fn install(
        &self,
        app: &AppHandle,
        model_id: &str,
        enable_after: bool,
    ) -> Result<String, String> {
        let handle = app.clone();
        let hooks = InstallHooks {
            enable: Box::new(move || {
                if let Some(state) = handle.try_state::<AppState>() {
                    if let Err(error) = state
                        .update(json!({"enhance":{"auto_rewrite":true},"overlay":{"enabled":true}}))
                    {
                        state.error(&format!("{error:#}"));
                    }
                }
            }),
        };
        self.service
            .install(model_id, enable_after, hooks)
            .map(|id| id.to_string())
            .map_err(|e| e.message)
    }

    /// Blocking: cancels work, unloads, deletes the model and turns auto-rewrite off.
    pub fn remove(&self, state: &AppState, model_id: &str) -> Result<(), String> {
        self.invalidate();
        if state.config().is_ok_and(|c| c.enhance.auto_rewrite) {
            state
                .update(json!({"enhance":{"auto_rewrite":false}}))
                .map_err(|e| format!("{e:#}"))?;
        }
        let result = self.service.remove(model_id).map_err(|e| e.message);
        self.changed(state);
        result
    }

    pub fn shutdown(&self) {
        self.service.shutdown();
        lock(&self.clipboard).take();
    }
}
