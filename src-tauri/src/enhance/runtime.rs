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
        mpsc, Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tauri::{AppHandle, Emitter, Manager};

pub struct Runtime {
    pub service: Arc<Enhance>,
    session: Arc<Mutex<Session>>,
    /// FIFO owner of acceptance, invalidation and delivery. Its clipboard I/O never holds
    /// the session mutex; UI reads only take the short session lock.
    delivery_owner: mpsc::Sender<Box<dyn FnOnce() + Send>>,
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

pub(super) fn accept_with(
    session: &Mutex<Session>,
    suggestion_id: Option<u64>,
    write: impl FnOnce(&str) -> Result<(), String>,
) -> (Value, bool) {
    let Some(id) = suggestion_id else {
        return (
            json!({"outcome":"stale","message":"This suggestion is no longer available."}),
            false,
        );
    };
    let decision = lock(session).accept(id);
    let AcceptDecision::Copy { text, again } = decision else {
        return (
            json!({"outcome":"stale","message":"This suggestion is no longer available."}),
            false,
        );
    };
    if again {
        return (
            json!({"outcome":"copied","message":super::session::COPIED}),
            false,
        );
    }
    // The caller runs on the FIFO owner, which orders invalidation and delivery.
    let result = write(&text);
    let outcome = if result.is_ok() { "copied" } else { "failed" };
    let message = result.as_ref().err().cloned();
    lock(session).accepted(id, result, Instant::now());
    (
        json!({"outcome":outcome,"message":message.or(Some(super::session::COPIED.into()))}),
        true,
    )
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
        let (delivery_owner, receiver) = mpsc::channel::<Box<dyn FnOnce() + Send>>();
        std::thread::Builder::new()
            .name("murmur-enhance-delivery".into())
            .spawn(move || {
                while let Ok(action) = receiver.recv() {
                    action();
                }
            })?;
        Ok(Self {
            service: Arc::new(service),
            session: Arc::new(Mutex::new(Session::default())),
            delivery_owner,
            revision: AtomicU64::new(0),
            next_dictation: AtomicU64::new(1),
            partial: Mutex::new(String::new()),
            clipboard: Mutex::new(None),
        })
    }

    fn dispatch_delivery(&self, action: impl FnOnce() + Send + 'static) {
        if self.delivery_owner.send(Box::new(action)).is_err() {
            tracing::warn!("Enhance delivery owner stopped");
        }
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
        state.companion.publish_text(text);
        let _ = state.app.emit("partial", json!({ "text": text }));
        self.publish(state);
    }

    /// A new dictation started: forget the previous one and cancel pending work.
    pub fn begin_dictation(&self, state: &AppState) {
        let session = self.session.clone();
        let worker = self.service.clone();
        let app = state.app.clone();
        self.dispatch_delivery(move || {
            if let Some(id) = lock(&session).begin_dictation() {
                worker.worker.cancel(id);
            }
            let state = app.state::<AppState>();
            state.enhance.publish(&state);
        });
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
        let session = self.session.clone();
        let service = self.service.clone();
        let app = state.app.clone();
        let fallback_config = config.clone();
        self.dispatch_delivery(move || {
            lock(&session).record_delivery(record);
            let state = app.state::<AppState>();
            let current = state.config().unwrap_or(fallback_config);
            if current.enhance.auto_rewrite && service.installed() {
                let _ = state.enhance.rewrite(&state, Trigger::Auto);
            } else {
                state.enhance.publish(&state);
            }
        });
    }

    pub fn shortcut_context(&self) -> Option<(u64, u64)> {
        let session = lock(&self.session);
        session
            .record
            .as_ref()
            .map(|r| (r.dictation_id, session.generation))
    }

    pub fn shortcut_suggestion(&self) -> Option<u64> {
        let session = lock(&self.session);
        session
            .suggestion
            .as_ref()
            .filter(|_| matches!(session.phase(), Phase::Ready | Phase::Notice))
            .map(|s| s.id)
    }

    pub fn rewrite_observed(&self, state: &AppState, observed: Option<(u64, u64)>) -> Value {
        self.rewrite_with(state, Trigger::Manual, observed)
    }

    /// Start (or reveal) a rewrite of the last dictation.
    pub fn rewrite(&self, state: &AppState, trigger: Trigger) -> Value {
        self.rewrite_with(state, trigger, None)
    }

    fn rewrite_with(
        &self,
        state: &AppState,
        trigger: Trigger,
        observed: Option<(u64, u64)>,
    ) -> Value {
        let reply = |status: RewriteStatus, message: Option<String>| json!({ "status": status, "message": message });
        let Ok(config) = state.config() else {
            tracing::info!(?trigger, reason = "config_unavailable", "Enhance rewrite skipped");
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
            rewrite_code: config.enhance.rewrite_code,
        };
        {
            let mut session = lock(&self.session);
            if observed.is_some_and(|(dictation, generation)| {
                session.generation != generation
                    || session
                        .record
                        .as_ref()
                        .is_none_or(|r| r.dictation_id != dictation)
            }) {
                tracing::info!(?trigger, reason = "expired", "Enhance rewrite skipped");
                return reply(RewriteStatus::Expired, None);
            }
            if let Err(status) = session.check(trigger, gate, Instant::now()) {
                // RewriteStatus serializes to the short machine-readable gate reason.
                let reason = json!(status);
                tracing::info!(?trigger, reason = reason.as_str(), "Enhance rewrite skipped");
                let message = session.message.clone();
                drop(session);
                self.changed(state);
                return reply(status, message);
            }
            let Some((entry, model_path)) = self.service.job_entry(&config.enhance.model) else {
                tracing::info!(?trigger, reason = "model_missing", "Enhance rewrite skipped");
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
            let threads = job.threads;
            session.started(request_id, self.service.loaded(), trigger);
            if !self.service.worker.submit(job) {
                tracing::info!(request_id, ?trigger, reason = "submit_rejected", "Enhance rewrite skipped");
                session.discard(Some(request_id));
                drop(session);
                self.changed(state);
                return reply(RewriteStatus::Busy, None);
            }
            tracing::info!(request_id, ?trigger, threads, timeout_ms = config.enhance.timeout_ms, installed = gate.installed, "Enhance job submitted");
        };
        self.changed(state);
        reply(RewriteStatus::Queued, None)
    }

    pub fn on_worker(&self, state: &AppState, event: WorkerEvent) {
        let (visible, suggestion) = {
            let mut session = lock(&self.session);
            let finished = matches!(event, WorkerEvent::Finished { .. });
            let visible = match event {
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
            };
            let suggestion = if finished && visible && session.phase() == Phase::Ready {
                session.suggestion.as_ref().map(|s| s.proposed.clone())
            } else {
                None
            };
            (visible, suggestion)
        };
        if let Some(text) = suggestion {
            state.companion.publish_suggestion(&text);
        }
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
        let (reply_tx, reply_rx) = mpsc::channel();
        let app = state.app.clone();
        self.dispatch_delivery(move || {
            let state = app.state::<AppState>();
            let (response, changed) = accept_with(&state.enhance.session, suggestion_id, |text| {
                state.enhance.copy(text)
            });
            if changed {
                state.enhance.changed(&state);
            }
            let _ = reply_tx.send(response);
        });
        reply_rx
            .recv()
            .unwrap_or_else(|_| json!({"outcome":"failed","message":"Enhance delivery stopped"}))
    }

    pub fn discard(&self, state: &AppState, suggestion_id: Option<u64>) {
        let session = self.session.clone();
        let worker = self.service.clone();
        let app = state.app.clone();
        self.dispatch_delivery(move || {
            let mut guard = lock(&session);
            let matches = suggestion_id.is_some_and(|id| {
                guard.suggestion.as_ref().is_some_and(|s| s.id == id) || guard.request == Some(id)
            });
            if matches {
                if let Some(id) = guard.discard(suggestion_id) {
                    worker.worker.cancel(id);
                }
            }
            drop(guard);
            let state = app.state::<AppState>();
            state.enhance.changed(&state);
        });
    }

    pub fn tick(&self, state: &AppState) {
        if lock(&self.session).tick(Instant::now()) {
            self.changed(state);
        }
    }

    fn invalidate(&self) {
        let session = self.session.clone();
        let worker = self.service.clone();
        self.dispatch_delivery(move || {
            if let Some(id) = lock(&session).invalidate() {
                worker.worker.cancel(id);
            }
        });
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
        if model_changed
            || (old.overlay.enabled && !new.overlay.enabled)
            || (old.enhance.rewrite_code
                && !new.enhance.rewrite_code
                && lock(&self.session).record.as_ref().is_some_and(|record| {
                    record.style == Style::Code || crate::inject::is_terminal(&record.focused)
                }))
        {
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
            enable: Box::new(move |id, intent| {
                if let Some(state) = handle.try_state::<AppState>() {
                    if let Err(error) = state.update_checked(
                        json!({"enhance":{"auto_rewrite":true},"overlay":{"enabled":true}}),
                        || intent.load(Ordering::Acquire) == id,
                    ) {
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
        self.service.clear_enable_intent();
        // Always commit an explicit disable, including false → false: this transaction
        // serializes with the download-completion consent check and revokes pending intent.
        state
            .update(json!({"enhance":{"auto_rewrite":false}}))
            .map_err(|e| format!("{e:#}"))?;
        let result = self.service.remove(model_id).map_err(|e| e.message);
        self.changed(state);
        result
    }

    pub fn shutdown(&self) {
        self.service.shutdown();
        lock(&self.clipboard).take();
    }
}
