use crate::{
    app::AppState,
    audio::{AudioCapture, AudioEvent},
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
struct Controller {
    audio: Option<AudioCapture>,
    injector: inject::Injector,
    active: bool,
    force_code: bool,
    epoch: u64,
    started: Instant,
    reset_at: Option<Instant>,
    hide_at: Option<Instant>,
    preview_at: Instant,
    preview: Option<JoinHandle<()>>,
    prune_at: Instant,
    config: Config,
}
impl Controller {
    fn new(config: Config) -> Self {
        Self {
            audio: None,
            injector: inject::Injector::default(),
            active: false,
            force_code: false,
            epoch: 0,
            started: Instant::now(),
            reset_at: None,
            hide_at: Some(Instant::now() + Duration::from_millis(1500)),
            preview_at: Instant::now(),
            preview: None,
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
        self.prepare_audio(state)?;
        hotkey::escape(&state.app, true)?;
        self.audio.as_mut().context("Microphone unavailable")?.start()?;
        self.active = true;
        self.epoch += 1;
        self.started = Instant::now();
        self.preview_at = Instant::now() + Duration::from_secs(2);
        self.reset_at = None;
        self.hide_at = None;
        state.app.emit("partial", json!({"text":""}))?;
        state.transition("listening")?;
        state.cue(Cue::Start);
        Ok(())
    }
    fn idle(&mut self, state: &AppState) -> Result<()> {
        self.active = false;
        self.epoch += 1;
        self.reset_at = None;
        self.hide_at = Some(Instant::now() + Duration::from_millis(1500));
        hotkey::escape(&state.app, false)?;
        state.app.emit("partial", json!({"text":""}))?;
        state.transition("idle")
    }
    fn cancel(&mut self, state: &AppState) -> Result<()> {
        if self.active {
            if let Some(audio) = &mut self.audio {
                audio.cancel();
            }
            self.idle(state)?;
        }
        Ok(())
    }
    fn stop(&mut self, state: &AppState) -> Result<()> {
        if !self.active {
            return Ok(());
        }
        self.active = false;
        self.epoch += 1;
        hotkey::escape(&state.app, false)?;
        let duration = self.started.elapsed().as_millis() as u64;
        let pcm = self.audio.as_mut().context("Microphone unavailable")?.stop()?;
        if duration < self.config.audio.min_utterance_ms || pcm.is_empty() {
            return self.idle(state);
        }
        state.cue(Cue::Stop);
        state.transition("transcribing")?;
        let start = Instant::now();
        let result = state.engine.request("transcribe", params(&pcm, &self.config))?;
        let text = result["text"].as_str().context("Engine response lacks transcript")?;
        let focused = platform::focused_app(&state.app)?;
        let style = postproc::resolve_style(&self.config, &focused, self.force_code);
        let text = inject::safe_text(&postproc::process_style(text, &self.config, style), &focused);
        if !text.is_empty() {
            self.injector.inject(&text, &self.config.injection, &focused, style)?;
            let latency = start.elapsed().as_millis() as u64;
            state.app.emit("latency", json!({"latency_ms":latency,"text":text}))?;
            if state.config()?.privacy.history_enabled {
                state.history(|history| {
                    if let Some(h) = history {
                        h.insert(&text, &focused, duration, latency, &self.config.engine.model)?;
                    }
                    Ok(())
                })?;
            }
        }
        state.refresh_engine()?;
        state.transition("done")?;
        self.reset_at = Some(Instant::now() + Duration::from_millis(900));
        Ok(())
    }
    fn fail(&mut self, state: &AppState, error: &str) {
        if let Some(audio) = &mut self.audio {
            audio.cancel();
        }
        self.active = false;
        self.epoch += 1;
        let _ = hotkey::escape(&state.app, false);
        state.error(error);
        state.cue(Cue::Error);
        let _ = state.transition("error");
        self.reset_at = Some(Instant::now() + Duration::from_millis(1600));
    }
    fn tick(&mut self, state: &AppState) -> Result<()> {
        let now = Instant::now();
        if self.reset_at.is_some_and(|at| now >= at) {
            self.idle(state)?;
            if state.config()?.overlay.idle_hide {
                overlay::hide(&state.app)?;
                self.hide_at = None;
            }
        }
        if self.hide_at.is_some_and(|at| now >= at) {
            self.hide_at = None;
            if state.config()?.overlay.idle_hide {
                overlay::hide(&state.app)?;
            }
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
        if self.active && self.config.overlay.show_preview && self.preview.is_none() && now >= self.preview_at {
            let pcm = self.audio.as_ref().context("Audio unavailable")?.snapshot()?;
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
            self.preview_at = now + Duration::from_secs(2);
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
            Action::Endpoint if self.active && self.config.general.hotkey_mode == "toggle" => self.stop(state)?,
            Action::MaxDuration if self.active => self.stop(state)?,
            Action::Fault(error) => self.fail(state, &error),
            Action::Preview(epoch, result) => {
                if let Some(job) = self.preview.take() {
                    let _ = job.join();
                }
                if epoch == self.epoch && self.active {
                    if let Ok(result) = result {
                        state.app.emit("partial", json!({"text":result["text"].as_str().unwrap_or("")}))?;
                    }
                }
            }
            Action::ToggleMute => {
                state.update(json!({"sounds":{"muted":!state.config()?.sounds.muted}}))?;
            }
            Action::ToggleOverlay => {
                state.update(json!({"overlay":{"enabled":!state.config()?.overlay.enabled}}))?;
            }
            Action::Reload => state.reload()?,
            Action::Reconfigure => {
                let next = state.config()?;
                let audio_changed = serde_json::to_value(&next.audio)? != serde_json::to_value(&self.config.audio)?;
                let engine_changed = serde_json::to_value(&next.engine)? != serde_json::to_value(&self.config.engine)?;
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
                    state.refresh_engine()?;
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
    if let Err(error) = state.refresh_engine() {
        state.error(&error.to_string());
    }
    if let Err(error) = controller.prepare_audio(&state) {
        state.error(&error.to_string());
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
