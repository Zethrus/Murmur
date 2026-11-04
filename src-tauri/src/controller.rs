use crate::{
    app::AppState,
    audio::{AudioCapture, AudioEvent, RemoteAudioSource},
    companion::network::{Policy, Source},
    config::Config,
    hotkey, inject, overlay, platform, postproc,
    sound::Cue,
};
use anyhow::{Context, Result};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde_json::{json, Value};
use std::{
    sync::mpsc,
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use tauri::{AppHandle, Emitter, Manager};

pub enum Action {
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
struct Controller {
    audio: Option<AudioCapture>,
    remote_source: Option<RemoteAudioSource>,
    source: Source,
    remote_turn: bool,
    injector: inject::Injector,
    active: bool,
    force_code: bool,
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
            remote_source: None,
            source: Source::Local,
            remote_turn: false,
            injector: inject::Injector::default(),
            active: false,
            force_code: false,
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
    fn prepare_audio(&mut self, state: &AppState) -> Result<()> {
        if self.audio.as_ref().is_some_and(AudioCapture::healthy) {
            return Ok(());
        }
        self.audio = None;
        let tx = state.sender();
        self.audio = Some(AudioCapture::new(&self.config.audio, move |event| {
            let action = match event {
                AudioEvent::Level(value) => Action::Level(value),
                AudioEvent::Endpoint => Action::Endpoint,
                AudioEvent::MaxDuration => Action::MaxDuration,
                AudioEvent::Error(error) => Action::Fault(error),
            };
            let _ = tx.send(action);
        })?);
        Ok(())
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
                self.audio = None;
            }
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
        let focused = platform::focused_app(&state.app)?;
        let style = postproc::resolve_style(&self.config, &focused, self.force_code);
        let body = postproc::clean_body(text, &self.config, style);
        let text = inject::safe_text(&postproc::process_cleaned(&body, &self.config), &focused);
        let mut delivered = None;
        if !text.is_empty() {
            // Click-through only while keystrokes/paste are in flight.
            overlay::set_busy(&state.app, true)?;
            state.enhance.publish(state);
            let injected = self
                .injector
                .inject(&text, &self.config.injection, &focused, style);
            if let Err(error) = overlay::set_busy(&state.app, false) {
                tracing::warn!(%error, "Restore overlay cursor handling");
            }
            state.enhance.publish(state);
            let path = injected?;
            delivered = Some(path);
            let latency = start.elapsed().as_millis() as u64;
            tracing::info!(latency_ms = latency, app = %focused, style = ?style, chars = text.len(), "transcript delivered");
            state
                .app
                .emit("latency", json!({"latency_ms":latency,"text":text}))?;
            if state.config()?.privacy.history_enabled {
                state.history(|history| {
                    if let Some(h) = history {
                        h.insert(
                            &text,
                            &focused,
                            duration,
                            latency,
                            &self.config.engine.model,
                        )?;
                    }
                    Ok(())
                })?;
            }
        }
        if text.is_empty() {
            state.companion.publish_final_text("");
        }
        state.refresh_engine()?;
        state.transition("done")?;
        state.audio_source(None);
        self.active = false;
        self.stopped_preview_epoch = self.preview.as_ref().map(|_| self.epoch);
        self.epoch += 1;
        self.reset_at = Some(Instant::now() + Duration::from_millis(900));
        // The original is delivered first; Enhance only ever runs afterwards.
        if let Some(path) = delivered {
            state
                .enhance
                .delivered(state, body, text, style, &focused, path, &self.config);
        }
        Ok(())
    }
    fn fail(&mut self, state: &AppState, error: &str) {
        state.companion_network.end_turn();
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
            let fallback = if !state.companion_network.phone_connected() {
                if policy == Policy::Phone {
                    anyhow::bail!("Phone microphone disconnected during dictation");
                }
                true
            } else if let Some(elapsed) = state.companion_network.silence_elapsed() {
                policy.silence_after(elapsed, state.companion_network.received())?
            } else {
                false
            };
            if fallback {
                state.companion_network.end_turn();
                self.remote_source = None;
                self.audio = None;
                self.prepare_audio(state)?;
                self.audio.as_mut().unwrap().start()?;
                self.source = Source::Local;
                self.remote_turn = false;
                self.started = Instant::now();
                tracing::info!(source = "local", "Dictation source selected");
                state.audio_source(Some("local"));
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
        match overlay::settled_placement(&state.app) {
            Ok(Some(placement)) => {
                overlay::suppress_moves();
                state.update(json!({"overlay":{"position":"custom","placement":placement}}))?;
            }
            Ok(None) => {}
            Err(error) => tracing::warn!(%error, "Overlay placement"),
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
    use super::{preview_due, preview_valid};
    use std::time::Duration;

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
}
