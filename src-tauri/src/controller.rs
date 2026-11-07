use crate::{
    app::AppState,
    audio::{AudioCapture, AudioEvent, RemoteAudioSource},
    companion::network::{Policy, Source},
    config::Config,
    focus_guard::{self, Decision, FocusMode},
    held, hotkey, inject,
    inject::InjectionPath,
    overlay,
    platform::{self, Focus},
    postproc::{self, Style},
    sound::Cue,
};
use anyhow::{Context, Result};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use tauri::{AppHandle, Emitter, Manager};

pub enum Action {
    /// A component was installed, removed or migrated: reload what depends on it.
    ComponentsChanged(crate::components::manifest::Kind),
    Start,
    StartCode,
    ToggleCode,
    Stop,
    Cancel,
    Toggle,
    Reconfigure,
    ToggleMute,
    ToggleOverlay,
    Reload,
    Level(f32),
    Endpoint,
    MaxDuration,
    Fault(String),
    Preview(u64, Result<Value>),
    /// Enhance: rewrite the last dictation (shortcut). Press-only, mode-independent.
    Rewrite(Option<(u64, u64)>),
    /// Enhance: accept (copy) the suggestion observed at keypress.
    AcceptCurrent(Option<u64>),
    /// Type the held transcript observed at keypress into the window focused now.
    InsertHeld(Option<u64>),
    /// Copy the held transcript to the clipboard.
    CopyHeld(Option<u64>),
    /// Forget the held transcript.
    DiscardHeld(Option<u64>),
    Quit,
}
pub fn params(pcm: &[f32], config: &Config) -> Value {
    let bytes: Vec<u8> = pcm.iter().flat_map(|sample| sample.to_le_bytes()).collect();
    json!({"pcm_b64":STANDARD.encode(bytes),"sample_rate":16000,
        "language":config.general.language,"translate":config.general.translate,
        "beam_size":config.engine.beam_size,"vad":config.engine.vad_filter,
        "initial_prompt":config.engine.dictionary.join(", "),"model":config.engine.model,
        "device":config.engine.device,"compute_type":config.engine.compute_type})
}

fn preview_due(active: bool, in_flight: bool, since_result: Duration, had_result: bool) -> bool {
    let interval = if had_result {
        Duration::from_secs(1)
    } else {
        Duration::from_millis(800)
    };
    active && !in_flight && since_result >= interval
}

fn preview_valid(epoch: u64, current: u64, active: bool, stopped: Option<u64>) -> bool {
    (active && epoch == current) || stopped == Some(epoch)
}

#[derive(Debug, PartialEq, Eq)]
enum RemoteAction {
    Continue,
    Fallback,
    Stop,
    Fail(&'static str),
}
fn remote_action(
    policy: Policy,
    connected: bool,
    silence: Option<Duration>,
    received: u64,
    wall_limit: bool,
) -> RemoteAction {
    if wall_limit {
        return RemoteAction::Stop;
    }
    if !connected || silence.is_some_and(|elapsed| elapsed >= Duration::from_millis(500)) {
        return match policy {
            Policy::Phone if !connected => {
                RemoteAction::Fail("Phone microphone disconnected during dictation")
            }
            Policy::Phone => RemoteAction::Fail("Phone microphone stalled for 500 ms"),
            Policy::Auto if received == 0 => RemoteAction::Fallback,
            Policy::Auto => RemoteAction::Stop,
            Policy::Local => RemoteAction::Continue,
        };
    }
    RemoteAction::Continue
}

fn warm_engine(app: &AppHandle, config: &Config) {
    let app = app.clone();
    let config_engine = config.engine.clone();
    let parameters = json!({"model":config.engine.model,"device":config.engine.device,
        "compute_type":config.engine.compute_type});
    if let Err(error) = thread::Builder::new()
        .name("murmur-engine-warm".into())
        .spawn(move || {
            let state = app.state::<AppState>();
            match state.engine.request("warm", parameters) {
                Ok(result) => {
                    if let Err(error) = state.cache_warm_status(&config_engine, &result) {
                        tracing::warn!(%error, "Cache warmed speech model status");
                    }
                }
                Err(error) => tracing::warn!(%error, "Warm speech model"),
            }
        })
    {
        tracing::warn!(%error, "Start speech model warmup");
    }
}
/// What `deliver` sent, handed on to history and Enhance.
struct Delivery {
    body: String,
    text: String,
    style: Style,
    class: String,
    path: InjectionPath,
}
const FOCUS_TARGET_FIRST: &str = "Focus the app you want, then insert.";
const COPIED: &str = "Focus changed during dictation. Copied to the clipboard instead.";
const COPIED_HELD: &str = "Copied to the clipboard.";
struct Controller {
    audio: Option<AudioCapture>,
    local_audio: Option<AudioCapture>,
    local_events_enabled: Arc<AtomicBool>,
    remote_source: Option<RemoteAudioSource>,
    source: Source,
    remote_turn: bool,
    injector: inject::Injector,
    active: bool,
    force_code: bool,
    /// Window focused when the current dictation started; `None` if it could not be read.
    origin: Option<Focus>,
    epoch: u64,
    started: Instant,
    reset_at: Option<Instant>,
    hide_at: Option<Instant>,
    preview_result_at: Instant,
    preview_had_result: bool,
    preview: Option<JoinHandle<()>>,
    stopped_preview_epoch: Option<u64>,
    prune_at: Instant,
    config: Config,
}
impl Controller {
    fn new(config: Config) -> Self {
        Self {
            audio: None,
            local_audio: None,
            local_events_enabled: Arc::new(AtomicBool::new(true)),
            remote_source: None,
            source: Source::Local,
            remote_turn: false,
            injector: inject::Injector::default(),
            active: false,
            force_code: false,
            origin: None,
            epoch: 0,
            started: Instant::now(),
            reset_at: None,
            hide_at: Some(Instant::now() + Duration::from_millis(1500)),
            preview_result_at: Instant::now(),
            preview_had_result: false,
            preview: None,
            stopped_preview_epoch: None,
            prune_at: Instant::now(),
            config,
        }
    }
    fn new_local_audio(&self, state: &AppState) -> Result<AudioCapture> {
        let tx = state.sender();
        let enabled = self.local_events_enabled.clone();
        AudioCapture::new(&self.config.audio, move |event| {
            if !enabled.load(Ordering::Relaxed) {
                return;
            }
            let action = match event {
                AudioEvent::Level(value) => Action::Level(value),
                AudioEvent::Endpoint => Action::Endpoint,
                AudioEvent::MaxDuration => Action::MaxDuration,
                AudioEvent::Error(error) => Action::Fault(error),
            };
            let _ = tx.send(action);
        })
    }
    fn prepare_audio(&mut self, state: &AppState) -> Result<()> {
        if self.audio.as_ref().is_some_and(AudioCapture::healthy) {
            return Ok(());
        }
        self.audio = Some(self.new_local_audio(state)?);
        Ok(())
    }
    fn restore_local_audio(&mut self) {
        self.audio = self.local_audio.take();
        self.local_events_enabled.store(true, Ordering::Relaxed);
    }
    fn drain_remote(&self, state: &AppState) -> Result<()> {
        if let Some(remote) = &self.remote_source {
            let mut result = Ok(());
            state.companion_network.drain(|frame| {
                if result.is_ok() {
                    result = remote.push(&frame.samples);
                }
            });
            result?;
        }
        Ok(())
    }
    fn start(&mut self, state: &AppState, code: bool) -> Result<()> {
        if self.active || state.status()?.state == "transcribing" {
            return Ok(());
        }
        self.config = state.config()?;
        self.force_code = code;
        // Bind this dictation to the window focused at the hotkey press.
        self.origin = platform::focused_target(&state.app)
            .inspect_err(|error| tracing::info!(%error, "Dictation target window unknown"))
            .ok();
        if state.held.clear() {
            state.enhance.refresh_overlay(state);
        }
        if !state.status()?.model_available {
            state.refresh_engine()?;
        }
        if !state.status()?.model_available {
            anyhow::bail!("Choose a speech model to start dictating");
        }
        let policy = match self.config.companion.source.as_str() {
            "local" => Policy::Local,
            "phone" => Policy::Phone,
            _ => Policy::Auto,
        };
        let source = state
            .companion_network
            .begin_turn(policy, self.epoch.wrapping_add(1) as u32)?;
        if source == Source::Phone {
            self.local_events_enabled.store(false, Ordering::Relaxed);
            if self.source == Source::Local {
                self.local_audio = self.audio.take();
            } else {
                self.audio = None;
            }
            if policy == Policy::Auto {
                if self.local_audio.as_ref().is_none_or(AudioCapture::healthy) {
                    self.local_audio = match self.new_local_audio(state) {
                        Ok(local) => Some(local),
                        Err(error) => {
                            tracing::warn!(%error, "Prepare fallback microphone");
                            None
                        }
                    };
                }
                if let Some(local) = &mut self.local_audio {
                    local.start()?;
                }
            }
            let tx = state.sender();
            let (capture, remote) = AudioCapture::new_remote(&self.config.audio, move |event| {
                let action = match event {
                    AudioEvent::Level(value) => Action::Level(value),
                    AudioEvent::Endpoint => Action::Endpoint,
                    AudioEvent::MaxDuration => Action::MaxDuration,
                    AudioEvent::Error(error) => Action::Fault(error),
                };
                let _ = tx.send(action);
            });
            self.audio = Some(capture);
            self.remote_source = Some(remote);
            self.remote_turn = true;
        } else {
            self.remote_source = None;
            if self.source != Source::Local {
                self.restore_local_audio();
            }
            self.local_events_enabled.store(true, Ordering::Relaxed);
            self.prepare_audio(state)?;
            self.remote_turn = false;
        }
        self.source = source;
        tracing::info!(
            source = if source == Source::Phone {
                "phone"
            } else {
                "local"
            },
            "Dictation source selected"
        );
        state.audio_source(Some(if source == Source::Phone {
            "phone"
        } else {
            "local"
        }));
        hotkey::escape(&state.app, true)?;
        self.audio
            .as_mut()
            .context("Microphone unavailable")?
            .start()?;
        self.active = true;
        self.epoch += 1;
        self.started = Instant::now();
        self.preview_result_at = self.started;
        self.preview_had_result = false;
        self.stopped_preview_epoch = None;
        self.reset_at = None;
        self.hide_at = None;
        state.enhance.begin_dictation(state);
        state.enhance.set_partial(state, "");
        state.transition("listening")?;
        state.cue(Cue::Start);
        Ok(())
    }
    fn idle(&mut self, state: &AppState) -> Result<()> {
        state.companion_network.end_turn();
        if let Some(local) = &mut self.local_audio {
            local.cancel();
        }
        self.local_events_enabled.store(true, Ordering::Relaxed);
        state.audio_source(None);
        self.active = false;
        self.epoch += 1;
        self.stopped_preview_epoch = None;
        self.reset_at = None;
        self.hide_at = Some(Instant::now() + Duration::from_millis(1500));
        hotkey::escape(&state.app, false)?;
        state.enhance.set_partial(state, "");
        state.transition("idle")
    }
    fn cancel(&mut self, state: &AppState) -> Result<()> {
        if self.active {
            if let Some(audio) = &mut self.audio {
                audio.cancel();
            }
            self.idle(state)?;
        } else {
            self.stopped_preview_epoch = None;
        }
        Ok(())
    }
    fn stop(&mut self, state: &AppState) -> Result<()> {
        if !self.active {
            return Ok(());
        }
        self.drain_remote(state)?;
        state.companion_network.end_turn();
        if let Some(local) = &mut self.local_audio {
            local.cancel();
        }
        self.local_events_enabled.store(true, Ordering::Relaxed);
        hotkey::escape(&state.app, false)?;
        let duration = self.started.elapsed().as_millis() as u64;
        let pcm = self
            .audio
            .as_mut()
            .context("Microphone unavailable")?
            .stop()?;
        if duration < self.config.audio.min_utterance_ms || pcm.is_empty() {
            tracing::info!(
                duration_ms = duration,
                samples = pcm.len(),
                "utterance discarded"
            );
            return self.idle(state);
        }
        tracing::info!(
            duration_ms = duration,
            samples = pcm.len(),
            "utterance captured"
        );
        state.cue(Cue::Stop);
        state.transition("transcribing")?;
        let start = Instant::now();
        let result = state
            .engine
            .request("transcribe", params(&pcm, &self.config))?;
        let text = result["text"]
            .as_str()
            .context("Engine response lacks transcript")?;
        tracing::info!(
            infer_ms = result["infer_ms"].as_f64().unwrap_or(0.0),
            chars = text.len(),
            "transcript received"
        );
        let (delivery_guard, current) = delivery_target(
            |busy| set_delivery_busy(state, busy),
            || platform::focused_target(&state.app),
        )?;
        let mode = FocusMode::parse(&self.config.injection.focus_change);
        let decision = focus_guard::decide(self.origin.as_ref(), current.as_ref().ok(), mode);
        let mut held = false;
        let delivered = match decision {
            Decision::Deliver => {
                let class = current?.class().to_owned();
                self.deliver(state, text, self.force_code, &class, false, duration, start)?
            }
            Decision::Refocus(id) => match self.refocus(state, id) {
                Some(class) => {
                    self.deliver(state, text, self.force_code, &class, false, duration, start)?
                }
                None => {
                    held = self.hold(state, text, duration);
                    None
                }
            },
            Decision::Copy => {
                let class = self.copy_class(current.as_ref().ok());
                let delivery =
                    self.deliver(state, text, self.force_code, &class, true, duration, start)?;
                if delivery.is_some() {
                    state.held.notice(COPIED, Instant::now());
                    state.enhance.refresh_overlay(state);
                }
                delivery
            }
            Decision::Hold => {
                held = self.hold(state, text, duration);
                // Say why when the window could not take typing at all (elevated, secure desktop)
                // or could not be read (for example Wayland), instead of "focus changed".
                if let (true, Some(reason)) = (held, hold_reason(&current)) {
                    if let Some(id) = state.held.current_id() {
                        state.held.set_message(id, &reason);
                        state.enhance.refresh_overlay(state);
                    }
                }
                None
            }
        };
        drop(delivery_guard);
        if delivered.is_none() && !held {
            state.companion.publish_final_text("");
        }
        state.refresh_engine()?;
        state.transition(if held { "idle" } else { "done" })?;
        state.audio_source(None);
        self.active = false;
        self.stopped_preview_epoch = self.preview.as_ref().map(|_| self.epoch);
        self.epoch += 1;
        if held {
            self.hide_at = Some(Instant::now() + Duration::from_millis(1500));
        } else {
            self.reset_at = Some(Instant::now() + Duration::from_millis(900));
        }
        // The original is delivered first; Enhance only ever runs afterwards.
        if let Some(delivery) = delivered {
            self.hand_off(state, delivery);
        }
        Ok(())
    }
    /// Post-process the raw transcript for the destination app and inject it, then record
    /// latency and history. Returns `None` when post-processing leaves nothing to send.
    #[allow(clippy::too_many_arguments)]
    fn deliver(
        &mut self,
        state: &AppState,
        raw: &str,
        force_code: bool,
        class: &str,
        clipboard_only: bool,
        duration: u64,
        start: Instant,
    ) -> Result<Option<Delivery>> {
        let style = postproc::resolve_style(&self.config, class, force_code);
        let body = postproc::clean_body(raw, &self.config, style);
        let text = inject::safe_text(&postproc::process_cleaned(&body, &self.config), class);
        if text.is_empty() {
            return Ok(None);
        }
        let mut injection = self.config.injection.clone();
        injection.clipboard_only |= clipboard_only;
        // Both callers acquire the drag-release barrier before reading focus.
        let path = self.injector.inject(&text, &injection, class, style)?;
        let latency = start.elapsed().as_millis() as u64;
        tracing::info!(latency_ms = latency, app = %class, style = ?style, chars = text.len(), "transcript delivered");
        state
            .app
            .emit("latency", json!({"latency_ms":latency,"text":text}))?;
        if state.config()?.privacy.history_enabled {
            state.history(|history| {
                if let Some(h) = history {
                    h.insert(&text, class, duration, latency, &self.config.engine.model)?;
                }
                Ok(())
            })?;
        }
        Ok(Some(Delivery {
            body,
            text,
            style,
            class: class.to_owned(),
            path,
        }))
    }
    fn hand_off(&self, state: &AppState, delivery: Delivery) {
        state.enhance.delivered(
            state,
            delivery.body,
            delivery.text,
            delivery.style,
            &delivery.class,
            delivery.path,
            &self.config,
        );
    }
    /// Bring the original window back. Returns its class once it has focus again.
    fn refocus(&self, state: &AppState, id: u64) -> Option<String> {
        if let Err(error) = platform::activate_window(id) {
            tracing::info!(%error, "Refocus original window failed; holding transcript");
            return None;
        }
        let now = platform::focused_target(&state.app).ok();
        match focus_guard::decide(self.origin.as_ref(), now.as_ref(), FocusMode::Hold) {
            Decision::Deliver => now.map(|focus| focus.class().to_owned()),
            _ => None,
        }
    }
    /// Style for clipboard-only delivery follows the window dictation started in.
    fn copy_class(&self, current: Option<&Focus>) -> String {
        [self.origin.as_ref(), current]
            .into_iter()
            .flatten()
            .find_map(|focus| match focus {
                Focus::Window { class, .. } | Focus::Blocked { class, .. } => Some(class.clone()),
                Focus::Own => None,
            })
            .unwrap_or_default()
    }
    /// Keep the transcript for review. Returns whether anything was held.
    fn hold(&self, state: &AppState, raw: &str, duration: u64) -> bool {
        let origin = self.origin.as_ref().map(|focus| match focus {
            Focus::Own => "Murmur".to_owned(),
            Focus::Window { class, .. } | Focus::Blocked { class, .. } => class.clone(),
        });
        let class = origin.as_deref().unwrap_or_default();
        let style = postproc::resolve_style(&self.config, class, self.force_code);
        let preview = postproc::process_cleaned(
            &postproc::clean_body(raw, &self.config, style),
            &self.config,
        );
        if preview.is_empty() {
            return false;
        }
        tracing::info!(
            origin = origin.as_deref().unwrap_or("unknown"),
            chars = preview.len(),
            "Transcript held: focus changed during dictation"
        );
        state.companion.publish_suggestion(&preview);
        state.held.hold(
            held::Pending {
                raw: raw.to_owned(),
                preview,
                force_code: self.force_code,
                origin,
                duration_ms: duration,
            },
            Instant::now(),
        );
        state.enhance.refresh_overlay(state);
        true
    }
    fn idle_for_held(&self, state: &AppState) -> bool {
        !self.active && state.status().is_ok_and(|s| s.state != "transcribing")
    }
    /// Deliver held text: type it into the window focused now (the user chose where it goes),
    /// or copy it. Held text is kept, with a message, unless delivery succeeds.
    fn deliver_held(&mut self, state: &AppState, id: Option<u64>, clipboard: bool) -> Result<()> {
        let Some(held) = id.and_then(|id| state.held.get(id)) else {
            return Ok(());
        };
        if !self.idle_for_held(state) {
            return Ok(());
        }
        let (delivery_guard, current) = delivery_target(
            |busy| set_delivery_busy(state, busy),
            || platform::focused_target(&state.app),
        )?;
        let class = if clipboard {
            // Clipboard-only output still follows the original app's style.
            held.origin
                .clone()
                .filter(|class| !inject::is_own_app(class))
                .unwrap_or_default()
        } else {
            match current {
                Ok(Focus::Window { class, .. }) => class,
                Ok(Focus::Blocked { reason, .. }) => {
                    self.held_message(state, held.id, reason.message());
                    return Ok(());
                }
                Ok(Focus::Own) => {
                    self.held_message(state, held.id, FOCUS_TARGET_FIRST);
                    return Ok(());
                }
                Err(error) => {
                    self.held_message(state, held.id, &format!("{error:#}"));
                    return Ok(());
                }
            }
        };
        let delivered = match self.deliver(
            state,
            &held.raw,
            held.force_code,
            &class,
            clipboard,
            held.duration_ms,
            Instant::now(),
        ) {
            Ok(delivered) => delivered,
            Err(error) => {
                tracing::warn!(%error, "Deliver held transcript");
                self.held_message(state, held.id, &format!("Could not deliver: {error:#}"));
                return Ok(());
            }
        };
        drop(delivery_guard);
        state.held.discard(held.id);
        if clipboard && delivered.is_some() {
            state.held.notice(COPIED_HELD, Instant::now());
        }
        state.enhance.refresh_overlay(state);
        let Some(delivery) = delivered else {
            return Ok(());
        };
        state.transition("done")?;
        self.reset_at = Some(Instant::now() + Duration::from_millis(900));
        self.hand_off(state, delivery);
        Ok(())
    }
    fn held_message(&self, state: &AppState, id: u64, message: &str) {
        if state.held.set_message(id, message) {
            state.enhance.refresh_overlay(state);
        }
    }
    fn fail(&mut self, state: &AppState, error: &str) {
        state.companion_network.end_turn();
        if let Some(local) = &mut self.local_audio {
            local.cancel();
        }
        self.local_events_enabled.store(true, Ordering::Relaxed);
        state.audio_source(None);
        if let Some(audio) = &mut self.audio {
            audio.cancel();
        }
        self.active = false;
        self.epoch += 1;
        self.stopped_preview_epoch = None;
        let _ = hotkey::escape(&state.app, false);
        state.error(error);
        state.cue(Cue::Error);
        let _ = state.transition("error");
        self.reset_at = Some(Instant::now() + Duration::from_millis(1600));
    }
    fn tick(&mut self, state: &AppState) -> Result<()> {
        let now = Instant::now();
        if self.active && self.remote_turn {
            self.drain_remote(state)?;
            let policy = if self.config.companion.source == "phone" {
                Policy::Phone
            } else {
                Policy::Auto
            };
            match remote_action(
                policy,
                state.companion_network.phone_connected(),
                state.companion_network.silence_elapsed(),
                state.companion_network.received(),
                self.started.elapsed() >= Duration::from_secs(self.config.audio.max_utterance_s),
            ) {
                RemoteAction::Stop => {
                    self.stop(state)?;
                    return Ok(());
                }
                RemoteAction::Fail(message) => anyhow::bail!("{message}"),
                RemoteAction::Continue => {}
                RemoteAction::Fallback => {
                    state.companion_network.end_turn();
                    self.remote_source = None;
                    self.restore_local_audio();
                    if self.audio.as_ref().is_none_or(AudioCapture::healthy) {
                        self.audio = None;
                        self.prepare_audio(state)?;
                        self.audio.as_mut().unwrap().start()?;
                    }
                    self.source = Source::Local;
                    self.remote_turn = false;
                    tracing::info!(source = "local", "Dictation source selected");
                    state.audio_source(Some("local"));
                }
            }
        }
        if self.reset_at.is_some_and(|at| now >= at) {
            self.idle(state)?;
            if state.config()?.overlay.idle_hide && !state.overlay_held() {
                overlay::hide(&state.app)?;
                self.hide_at = None;
            }
        }
        if self.hide_at.is_some_and(|at| now >= at) {
            self.hide_at = None;
            if state.config()?.overlay.idle_hide && !state.overlay_held() && !self.active {
                overlay::hide(&state.app)?;
            }
        }
        state.enhance.tick(state);
        if state.held.tick(now) {
            state.enhance.refresh_overlay(state);
        }
        if let Err(error) = state.save_overlay_placement() {
            tracing::warn!(%error, "Save overlay placement; retrying");
        }
        if now >= self.prune_at {
            let days = state.config()?.privacy.history_retention_days;
            state.history(|h| {
                if let Some(h) = h {
                    h.prune(days)?;
                }
                Ok(())
            })?;
            self.prune_at = now + Duration::from_secs(86_400);
        }
        if (self.config.overlay.show_preview || self.config.companion.enabled)
            && preview_due(
                self.active,
                self.preview.is_some(),
                now.saturating_duration_since(self.preview_result_at),
                self.preview_had_result,
            )
        {
            let pcm = self
                .audio
                .as_ref()
                .context("Audio unavailable")?
                .snapshot()?;
            let pcm = &pcm[pcm.len().saturating_sub(16_000 * 6)..];
            let parameters = params(pcm, &self.config);
            let engine = state.engine.clone();
            let tx = state.sender();
            let epoch = self.epoch;
            self.preview = Some(
                thread::Builder::new()
                    .name("murmur-preview".into())
                    .spawn(move || {
                        let result = engine.request("partial", parameters);
                        let _ = tx.send(Action::Preview(epoch, result));
                    })
                    .context("Start preview worker")?,
            );
        }
        Ok(())
    }
    fn action(&mut self, state: &AppState, action: Action) -> Result<()> {
        match action {
            Action::Start => self.start(state, false)?,
            Action::StartCode => self.start(state, true)?,
            Action::Stop => self.stop(state)?,
            Action::Cancel => self.cancel(state)?,
            Action::Toggle | Action::ToggleCode => {
                if self.active {
                    self.stop(state)?;
                } else {
                    self.start(state, matches!(action, Action::ToggleCode))?;
                }
            }
            Action::Level(rms) => {
                state.app.emit("level", json!({"rms":rms}))?;
            }
            Action::Endpoint if self.active && self.config.general.hotkey_mode == "toggle" => {
                self.stop(state)?
            }
            Action::MaxDuration if self.active => self.stop(state)?,
            Action::Fault(error) => self.fail(state, &error),
            Action::Preview(epoch, result) => {
                if let Some(job) = self.preview.take() {
                    let _ = job.join();
                }
                let valid =
                    preview_valid(epoch, self.epoch, self.active, self.stopped_preview_epoch);
                if epoch == self.epoch && self.active {
                    self.preview_result_at = Instant::now();
                    self.preview_had_result = true;
                }
                if self.stopped_preview_epoch == Some(epoch) {
                    self.stopped_preview_epoch = None;
                }
                match (valid, result) {
                    (true, Ok(result)) => state
                        .enhance
                        .set_partial(state, result["text"].as_str().unwrap_or("")),
                    (false, Ok(result)) => tracing::debug!(
                        chars = result["text"].as_str().map_or(0, str::len),
                        reason = "stale turn",
                        "Preview dropped"
                    ),
                    (_, Err(_)) => tracing::debug!(
                        chars = 0,
                        reason = "engine request failed",
                        "Preview dropped"
                    ),
                }
            }
            Action::ToggleMute => {
                state.update(json!({"sounds":{"muted":!state.config()?.sounds.muted}}))?;
            }
            Action::ToggleOverlay => {
                state.update(json!({"overlay":{"enabled":!state.config()?.overlay.enabled}}))?;
            }
            Action::Reload => state.reload()?,
            Action::ComponentsChanged(kind) => {
                use crate::components::manifest::Kind;
                match kind {
                    Kind::Engine | Kind::Cuda => state.engine.restart(),
                    Kind::EnhanceModel => {
                        state.enhance.service.refresh(&self.config.enhance.model);
                        state.enhance.refresh_overlay(state);
                    }
                    Kind::SpeechModel => {}
                }
                if kind != Kind::EnhanceModel && !self.active {
                    match state.refresh_engine() {
                        Ok(result) if result["model_available"].as_bool() == Some(true) => {
                            warm_engine(&state.app, &self.config)
                        }
                        Ok(_) => {}
                        Err(error) => {
                            tracing::info!(%error, "Engine status after component change")
                        }
                    }
                }
            }
            Action::Rewrite(observed) => {
                if observed.is_some() && !self.active && state.status()?.state != "transcribing" {
                    let _ = state.enhance.rewrite_observed(state, observed);
                }
            }
            Action::AcceptCurrent(id) => {
                if let Some(id) = id {
                    let _ = state.enhance.accept(state, Some(id));
                }
            }
            Action::InsertHeld(id) => self.deliver_held(state, id, false)?,
            Action::CopyHeld(id) => self.deliver_held(state, id, true)?,
            Action::DiscardHeld(id) => {
                if id.is_some_and(|id| state.held.discard(id)) {
                    state.enhance.refresh_overlay(state);
                }
            }
            Action::Reconfigure => {
                let next = state.config()?;
                let audio_changed =
                    serde_json::to_value(&next.audio)? != serde_json::to_value(&self.config.audio)?;
                let engine_changed = serde_json::to_value(&next.engine)?
                    != serde_json::to_value(&self.config.engine)?;
                let hotkey_changed = next.general.hotkey != self.config.general.hotkey
                    || next.general.hotkey_mode != self.config.general.hotkey_mode
                    || next.general.hotkey_code != self.config.general.hotkey_code;
                if audio_changed || engine_changed || hotkey_changed {
                    self.cancel(state)?;
                }
                if !self.active {
                    self.config = next;
                }
                if audio_changed {
                    self.audio = None;
                    self.local_audio = None;
                    self.prepare_audio(state)?;
                }
                if engine_changed {
                    let result = state.refresh_engine()?;
                    if result["model_available"].as_bool() == Some(true) {
                        warm_engine(&state.app, &self.config);
                    }
                }
                if !self.active {
                    self.hide_at = Some(Instant::now() + Duration::from_millis(1500));
                }
                overlay::apply(&state.app, &state.config()?.overlay)?;
            }
            _ => {}
        }
        Ok(())
    }
}
/// Keep new drags blocked until the target has been checked and delivery finishes.
/// Cleanup also publishes on hold, empty, and error paths so held actions re-enable.
struct DeliveryGuard<F: FnMut(bool) -> Result<()>>(F);

impl<F: FnMut(bool) -> Result<()>> Drop for DeliveryGuard<F> {
    fn drop(&mut self) {
        if let Err(error) = (self.0)(false) {
            tracing::warn!(%error, "Restore overlay cursor handling");
        }
    }
}

fn delivery_target<F: FnMut(bool) -> Result<()>, T>(
    mut set_busy: F,
    focused: impl FnOnce() -> T,
) -> Result<(DeliveryGuard<F>, T)> {
    set_busy(true)?; // may wait for the compositor's current drag to release
    let guard = DeliveryGuard(set_busy);
    Ok((guard, focused()))
}

/// The message for a held transcript when the current window, not a focus change, is the reason:
/// a blocked target says why, and an unreadable focus shows the platform error (as Insert here
/// does). `None` keeps the default "focus changed" text.
fn hold_reason(current: &Result<Focus>) -> Option<String> {
    match current {
        Ok(Focus::Blocked { reason, .. }) => Some(reason.message().to_owned()),
        Ok(Focus::Window { .. } | Focus::Own) => None,
        Err(error) => Some(format!("{error:#}")),
    }
}

fn set_delivery_busy(state: &AppState, busy: bool) -> Result<()> {
    let result = overlay::set_busy(&state.app, busy);
    state.enhance.publish(state);
    result
}

pub fn run(app: AppHandle, receiver: mpsc::Receiver<Action>) {
    let state = app.state::<AppState>();
    let config = match state.config() {
        Ok(config) => config,
        Err(error) => {
            state.error(&error.to_string());
            return;
        }
    };
    let mut controller = Controller::new(config);
    match state.refresh_engine() {
        Ok(result) if result["model_available"].as_bool() == Some(true) => {
            warm_engine(&app, &controller.config);
        }
        Err(error) => state.error(&error.to_string()),
        _ => {}
    }
    if controller.config.companion.source != "phone" {
        if let Err(error) = controller.prepare_audio(&state) {
            state.error(&error.to_string());
        }
    }
    loop {
        match receiver.recv_timeout(Duration::from_millis(50)) {
            Ok(Action::Quit) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Ok(action) => {
                if let Err(error) = controller.action(&state, action) {
                    controller.fail(&state, &format!("{error:#}"));
                }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
        }
        if let Err(error) = controller.tick(&state) {
            controller.fail(&state, &format!("{error:#}"));
        }
    }
    let _ = controller.cancel(&state);
    if let Some(job) = controller.preview.take() {
        let _ = job.join();
    }
}

#[cfg(test)]
mod tests {
    use super::{preview_due, preview_valid, remote_action, Controller, Policy, RemoteAction};
    use crate::{audio::AudioCapture, config::Config};
    use std::time::Duration;

    #[test]
    fn held_text_says_why_when_focus_is_unreadable_or_blocked() {
        use crate::platform::{Blocked, Focus};
        // Wayland in insert (or hold) mode: the real reason, not "focus changed".
        let wayland: anyhow::Result<Focus> = Err(anyhow::anyhow!(
            "Text injection requires X11; Wayland is untested"
        ));
        assert_eq!(
            super::hold_reason(&wayland).as_deref(),
            Some("Text injection requires X11; Wayland is untested")
        );
        let context: anyhow::Result<Focus> =
            Err(anyhow::Error::msg("No such file").context("Read active X11 window (xprop)"));
        assert_eq!(
            super::hold_reason(&context).as_deref(),
            Some("Read active X11 window (xprop): No such file")
        );
        let elevated = Ok(Focus::Blocked {
            reason: Blocked::Elevated,
            class: "notepad.exe".into(),
        });
        assert_eq!(
            super::hold_reason(&elevated).as_deref(),
            Some(Blocked::Elevated.message())
        );
        // A readable, different window keeps the default "focus changed" text.
        let other = Ok(Focus::Window {
            id: 2,
            pid: None,
            started: None,
            class: "editor".into(),
        });
        assert_eq!(super::hold_reason(&other), None);
        assert_eq!(super::hold_reason(&Ok(Focus::Own)), None);
    }

    #[test]
    fn delivery_target_reads_focus_after_wait_and_keeps_busy_until_drop() {
        use std::sync::{
            atomic::{AtomicBool, AtomicU64, Ordering},
            mpsc, Arc,
        };
        let focus = Arc::new(AtomicU64::new(1));
        let busy = Arc::new(AtomicBool::new(false));
        let (started, ready) = mpsc::channel();
        let (release, waiting) = mpsc::channel();
        let worker_focus = focus.clone();
        let worker_busy = busy.clone();
        let worker = std::thread::spawn(move || {
            let (guard, current) = super::delivery_target(
                |value| {
                    worker_busy.store(value, Ordering::SeqCst);
                    if value {
                        started.send(()).unwrap();
                        waiting.recv_timeout(Duration::from_secs(2)).unwrap();
                    }
                    Ok(())
                },
                || crate::platform::Focus::Window {
                    id: worker_focus.load(Ordering::SeqCst),
                    pid: None,
                    started: None,
                    class: "editor".into(),
                },
            )
            .unwrap();
            let origin = crate::platform::Focus::Window {
                id: 1,
                pid: None,
                started: None,
                class: "editor".into(),
            };
            assert_eq!(
                crate::focus_guard::decide(
                    Some(&origin),
                    Some(&current),
                    crate::focus_guard::FocusMode::Hold
                ),
                crate::focus_guard::Decision::Hold
            );
            assert!(worker_busy.load(Ordering::SeqCst));
            drop(guard);
        });
        ready.recv_timeout(Duration::from_secs(2)).unwrap();
        focus.store(2, Ordering::SeqCst); // target changes while a drag delays delivery
        release.send(()).unwrap();
        worker.join().unwrap();
        assert!(!busy.load(Ordering::SeqCst));
    }

    #[test]
    fn delivery_target_cleans_up_focus_errors_and_never_reads_after_barrier_failure() {
        use std::cell::{Cell, RefCell};
        let publications = RefCell::new(Vec::new());
        let attempt = || -> anyhow::Result<()> {
            let (_guard, focus) = super::delivery_target(
                |value| {
                    publications.borrow_mut().push(value);
                    Ok(())
                },
                || -> anyhow::Result<()> { anyhow::bail!("focus unavailable") },
            )?;
            focus?;
            Ok(())
        };
        assert!(attempt().is_err());
        assert_eq!(*publications.borrow(), vec![true, false]);
        let queried = Cell::new(false);
        let failed =
            super::delivery_target(|_| anyhow::bail!("drag timed out"), || queried.set(true));
        assert!(failed.is_err());
        assert!(!queried.get());
    }

    #[test]
    fn preview_waits_since_previous_result_and_skips_in_flight() {
        assert!(!preview_due(true, false, Duration::from_millis(799), false));
        assert!(preview_due(true, false, Duration::from_millis(800), false));
        assert!(!preview_due(true, false, Duration::from_millis(999), true));
        assert!(preview_due(true, false, Duration::from_secs(1), true));
        assert!(!preview_due(true, true, Duration::from_secs(5), true));
        assert!(!preview_due(false, false, Duration::from_secs(5), true));
    }

    #[test]
    fn stopped_turn_accepts_its_preview_but_cancelled_turn_does_not() {
        assert!(preview_valid(7, 8, false, Some(7)));
        assert!(!preview_valid(7, 8, false, None));
        assert!(preview_valid(8, 8, true, None));
        assert!(!preview_valid(7, 9, true, None));
    }
    #[test]
    fn phone_stalls_and_auto_preserves_received_audio() {
        let gap = Some(Duration::from_millis(500));
        assert_eq!(
            remote_action(Policy::Phone, true, gap, 1, false),
            RemoteAction::Fail("Phone microphone stalled for 500 ms")
        );
        assert_eq!(
            remote_action(Policy::Auto, true, gap, 0, false),
            RemoteAction::Fallback
        );
        assert_eq!(
            remote_action(Policy::Auto, true, gap, 1, false),
            RemoteAction::Stop
        );
        assert_eq!(
            remote_action(Policy::Auto, false, None, 1, false),
            RemoteAction::Stop
        );
        assert_eq!(
            remote_action(Policy::Auto, false, None, 0, false),
            RemoteAction::Fallback
        );
        assert_eq!(
            remote_action(Policy::Phone, true, None, 1, true),
            RemoteAction::Stop
        );
        assert_eq!(
            remote_action(
                Policy::Auto,
                true,
                Some(Duration::from_millis(499)),
                0,
                false
            ),
            RemoteAction::Continue
        );
    }
    #[test]
    fn local_capture_survives_phone_turn_and_keeps_preroll() {
        let config = Config::default();
        let mut controller = Controller::new(config.clone());
        let (local, feed) = AudioCapture::new_remote(&config.audio, |_| {});
        controller.local_audio = Some(local);
        feed.push(&[1; 320]).unwrap();
        controller.restore_local_audio();
        controller.audio.as_mut().unwrap().start().unwrap();
        assert_eq!(
            controller.audio.as_ref().unwrap().snapshot().unwrap().len(),
            320
        );

        controller.local_audio = controller.audio.take();
        controller.local_audio.as_mut().unwrap().start().unwrap();
        feed.push(&[2; 320]).unwrap();
        controller.restore_local_audio();
        assert_eq!(
            controller.audio.as_ref().unwrap().snapshot().unwrap().len(),
            640
        );
    }
}
