//! Flipper companion link: one supervisor thread, polled transports, no daemon.
//!
//! The thread owns the device and reconnects with backoff. Configuration is re-read
//! whenever the settings change, so the button map and the transport are live.

pub mod link;
pub mod map;
pub mod protocol;
pub mod state;

use crate::{app::AppState, config::Config};
use serde::Serialize;
use std::{
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use tauri::{AppHandle, Emitter, Manager};

/// Outbound heartbeat interval.
const HEARTBEAT: Duration = Duration::from_secs(5);
/// Silence after which the link is considered dead.
const DEAD_AFTER: Duration = Duration::from_secs(15);
/// A repeated press of the same verb inside this window is a bounce, not a second press.
const DEBOUNCE: Duration = Duration::from_millis(250);
/// Live text is coalesced to at most one line per interval: a 20-byte BLE write per
/// keystroke would saturate the link, and a human cannot read faster than this anyway.
const TEXT_INTERVAL: Duration = Duration::from_millis(250);
/// Reconnect backoff ceiling.
const MAX_BACKOFF: Duration = Duration::from_secs(5);

/// Snapshot for the Settings card. Serialized straight to the webview.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct Status {
    pub enabled: bool,
    pub connected: bool,
    pub transport: String,
    pub port: String,
    /// Peer identity, e.g. `flipper 0.2.2`.
    pub device: String,
    pub protocol: u8,
    pub last_button: String,
    pub error: Option<String>,
    /// Signal strength, when the transport reports one (BLE).
    pub rssi: Option<i32>,
    /// Device battery percentage, from its periodic `Q` line.
    pub battery: Option<u8>,
    /// Round-trip latency in ms, measured with a timed probe.
    pub latency_ms: u32,
}

pub struct Companion {
    shared: Arc<Shared>,
}

/// One pending thing to show on the device. Text coalesces: the newest wins, because a
/// partial transcript is only interesting until the next one arrives.
enum Outbound {
    Text(String),
    FinalText(String),
    Suggestion(String),
}

#[derive(Default)]
struct OutboundSlot {
    queued: Option<Outbound>,
    final_text: Option<String>,
    final_published: bool,
}

impl OutboundSlot {
    fn queue_partial(&mut self, text: &str) {
        if !self.final_published {
            self.queued = Some(Outbound::Text(text.to_string()));
        }
    }

    fn queue_final(&mut self, text: &str) {
        self.final_published = true;
        self.final_text = Some(text.to_string());
        if matches!(self.queued, Some(Outbound::Text(_))) {
            self.queued = None;
        }
    }
}

struct Shared {
    app: AppHandle,
    status: Mutex<Status>,
    outbound: Mutex<OutboundSlot>,
    /// Bumped when the configuration changes; the supervisor reloads and resends `CFG`.
    revision: AtomicU64,
    started: AtomicBool,
    stop: AtomicBool,
}

impl Companion {
    pub fn new(app: AppHandle) -> Self {
        Self {
            shared: Arc::new(Shared {
                app,
                status: Mutex::new(Status::default()),
                outbound: Mutex::new(OutboundSlot::default()),
                revision: AtomicU64::new(1),
                started: AtomicBool::new(false),
                stop: AtomicBool::new(false),
            }),
        }
    }

    /// Start the supervisor once. Safe to call on every launch.
    pub fn start(&self) {
        if self.shared.started.swap(true, Ordering::SeqCst) {
            return;
        }
        let shared = Arc::clone(&self.shared);
        thread::Builder::new()
            .name("murmur-companion".into())
            .spawn(move || supervise(shared))
            .ok();
    }

    /// Configuration changed: reload and push a fresh `CFG` line.
    pub fn config_changed(&self) {
        self.shared.revision.fetch_add(1, Ordering::Relaxed);
    }

    pub fn status(&self) -> Status {
        self.shared
            .status
            .lock()
            .map(|status| status.clone())
            .unwrap_or_default()
    }

    /// Show live dictation text on the device. Never logged: this path must not trace,
    /// because the payload is a transcript.
    pub fn publish_text(&self, text: &str) {
        let text = text.trim();
        if text.is_empty() {
            return;
        }
        if let Ok(mut slot) = self.shared.outbound.lock() {
            slot.queue_partial(text);
        }
    }

    /// The delivered text must survive a suggestion arriving before the next link tick.
    /// An empty final clears the device's provisional preview.
    pub fn publish_final_text(&self, text: &str) {
        let text = text.trim();
        if let Ok(mut slot) = self.shared.outbound.lock() {
            slot.queue_final(text);
        }
    }

    pub fn clear_pending(&self) {
        if let Ok(mut slot) = self.shared.outbound.lock() {
            *slot = OutboundSlot::default();
        }
    }

    /// Show the enhancement suggestion on the device once it is ready.
    pub fn publish_suggestion(&self, text: &str) {
        let text = text.trim();
        if text.is_empty() {
            return;
        }
        if let Ok(mut slot) = self.shared.outbound.lock() {
            slot.queued = Some(Outbound::Suggestion(text.to_string()));
        }
    }

    /// Ask the supervisor to stop (used on quit and by tests).
    pub fn stop(&self) {
        self.shared.stop.store(true, Ordering::SeqCst);
    }
}

fn publish(shared: &Shared, next: Status) {
    let changed = match shared.status.lock() {
        Ok(mut current) => {
            if *current == next {
                false
            } else {
                *current = next.clone();
                true
            }
        }
        Err(_) => false,
    };
    if changed {
        let _ = shared.app.emit("companion", &next);
    }
}

/// Full status for a live session, including the telemetry the device and the transport
/// report. Callers only differ in `connected` and in the error text they add afterwards.
fn session_status(config: &Config, active: &Session, connected: bool) -> Status {
    Status {
        enabled: true,
        connected,
        transport: config.companion.transport.clone(),
        port: active.link.describe(),
        device: active.device.clone(),
        protocol: protocol::PROTOCOL_VERSION,
        last_button: String::new(),
        error: None,
        rssi: active.link.rssi(),
        battery: active.battery,
        latency_ms: active.latency_ms,
    }
}

fn describe_error(error: &anyhow::Error) -> String {
    format!("{error:#}")
}

fn text_due(last: Instant, now: Instant) -> bool {
    now.duration_since(last) >= TEXT_INTERVAL
}

/// Device-side state of one connection attempt.
struct Session {
    key: String,
    link: Box<dyn link::Link>,
    device: String,
    negotiated: bool,
    last_rx: Instant,
    last_ping: Instant,
    phase: String,
    cfg_for: String,
    ptt_for: Option<bool>,
    battery: Option<u8>,
    latency_ms: u32,
    probe_seq: u64,
    probe_sent: Option<(String, Instant)>,
    last_text: Instant,
}

fn supervise(shared: Arc<Shared>) {
    let mut cached: Option<Config> = None;
    let mut revision = u64::MAX;
    let mut session: Option<Session> = None;
    let mut retry_at = Instant::now();
    let mut backoff = Duration::from_secs(1);
    let mut last_press: Option<(Instant, map::Verb)> = None;
    // Log a failure streak once, not every retry.
    let mut failing = false;

    while !shared.stop.load(Ordering::Relaxed) {
        let current = shared.revision.load(Ordering::Relaxed);
        if current != revision {
            revision = current;
            cached = shared
                .app
                .try_state::<AppState>()
                .and_then(|state| state.config().ok());
        }
        let Some(config) = cached.as_ref() else {
            publish(&shared, Status::default());
            thread::sleep(link::POLL);
            continue;
        };
        if !config.companion.enabled {
            session = None;
            if let Ok(mut slot) = shared.outbound.lock() {
                *slot = OutboundSlot::default();
            }
            publish(
                &shared,
                Status {
                    enabled: false,
                    transport: config.companion.transport.clone(),
                    ..Status::default()
                },
            );
            thread::sleep(Duration::from_millis(250));
            continue;
        }

        let key = format!(
            "{}|{}|{}",
            config.companion.transport, config.companion.usb_port, config.companion.ble_address
        );
        if session.as_ref().is_some_and(|s| s.key != key) {
            session = None;
            retry_at = Instant::now();
        }

        if session.is_none() && Instant::now() >= retry_at {
            match link::open(
                &config.companion.transport,
                &config.companion.usb_port,
                &config.companion.ble_address,
            ) {
                Ok(mut opened) => {
                    let port = opened.describe();
                    let hello = protocol::hello("host", env!("CARGO_PKG_VERSION"));
                    if let Err(error) = opened.write_line(&hello) {
                        backoff = retry_at_after(&mut retry_at, backoff);
                        publish(
                            &shared,
                            Status {
                                enabled: true,
                                transport: config.companion.transport.clone(),
                                port,
                                error: Some(format!("Handshake failed: {error}")),
                                ..Status::default()
                            },
                        );
                        thread::sleep(link::POLL);
                        continue;
                    }
                    backoff = Duration::from_secs(1);
                    failing = false;
                    tracing::info!(port = %port, transport = %config.companion.transport, "Companion link opened");
                    session = Some(Session {
                        key: key.clone(),
                        link: opened,
                        device: String::new(),
                        negotiated: false,
                        last_rx: Instant::now(),
                        last_ping: Instant::now(),
                        phase: String::new(),
                        cfg_for: String::new(),
                        ptt_for: None,
                        battery: None,
                        latency_ms: 0,
                        probe_seq: 1,
                        probe_sent: None,
                        last_text: Instant::now() - TEXT_INTERVAL,
                    });
                    publish(
                        &shared,
                        Status {
                            enabled: true,
                            transport: config.companion.transport.clone(),
                            port,
                            protocol: protocol::PROTOCOL_VERSION,
                            error: None,
                            ..Status::default()
                        },
                    );
                }
                Err(error) => {
                    if !failing {
                        failing = true;
                        tracing::warn!(%error, "Companion device unavailable");
                    }
                    backoff = retry_at_after(&mut retry_at, backoff);
                    publish(
                        &shared,
                        Status {
                            enabled: true,
                            transport: config.companion.transport.clone(),
                            port: config.companion.usb_port.clone(),
                            error: Some(describe_error(&error)),
                            ..Status::default()
                        },
                    );
                }
            }
            thread::sleep(link::POLL);
            continue;
        }

        let Some(active) = session.as_mut() else {
            thread::sleep(Duration::from_millis(100));
            continue;
        };

        // Push the button map once the peer has identified itself and whenever it changes.
        let map_line = cfg_line(&config.companion.buttons);
        if active.negotiated && active.cfg_for != map_line {
            if active.link.write_line(&map_line).is_ok() {
                active.cfg_for = map_line.clone();
            }
        }
        if active.negotiated && active.ptt_for != Some(config.companion.ptt) {
            if active
                .link
                .write_line(&protocol::ptt(config.companion.ptt))
                .is_ok()
            {
                active.ptt_for = Some(config.companion.ptt);
            }
        }

        let mut dropped = false;
        for _ in 0..8 {
            match active.link.poll_line() {
                Ok(Some(line)) => {
                    active.last_rx = Instant::now();
                    match protocol::parse(&line) {
                        protocol::Inbound::Hello { protocol, app, .. } => {
                            active.device = format!("flipper {app}");
                            if protocol == protocol::PROTOCOL_VERSION {
                                active.negotiated = true;
                                tracing::info!(%app, protocol, "Companion handshake complete");
                                let _ = active.link.write_line(&map_line);
                                active.cfg_for = map_line.clone();
                                if active
                                    .link
                                    .write_line(&protocol::ptt(config.companion.ptt))
                                    .is_ok()
                                {
                                    active.ptt_for = Some(config.companion.ptt);
                                }
                                publish(
                                    &shared,
                                    Status {
                                        enabled: true,
                                        connected: true,
                                        transport: config.companion.transport.clone(),
                                        port: active.link.describe(),
                                        device: active.device.clone(),
                                        protocol,
                                        last_button: String::new(),
                                        error: None,
                                        ..Status::default()
                                    },
                                );
                            } else {
                                active.negotiated = false;
                                tracing::warn!(%app, protocol, expected = protocol::PROTOCOL_VERSION, "Companion protocol mismatch");
                                publish(
                                    &shared,
                                    Status {
                                        enabled: true,
                                        connected: true,
                                        transport: config.companion.transport.clone(),
                                        port: active.link.describe(),
                                        device: active.device.clone(),
                                        protocol,
                                        last_button: String::new(),
                                        error: Some(format!(
                                            "Companion speaks protocol {protocol}, Murmur expects {}. Update the app on the Flipper.",
                                            protocol::PROTOCOL_VERSION
                                        )),
                                        ..Status::default()
                                    },
                                );
                            }
                        }
                        protocol::Inbound::Press { button, long } => {
                            if !active.negotiated {
                                continue;
                            }
                            if config.companion.ptt && button == protocol::Button::Ok && !long {
                                continue;
                            }
                            let verb = map::verb_for(&config.companion.buttons, button, long);
                            if verb == map::Verb::None {
                                continue;
                            }
                            let now = Instant::now();
                            let repeated = last_press.is_some_and(|(at, last)| {
                                last == verb && now.duration_since(at) < DEBOUNCE
                            });
                            last_press = Some((now, verb));
                            if repeated {
                                continue;
                            }
                            let ack = shared
                                .app
                                .try_state::<AppState>()
                                .and_then(|state| map::dispatch(&state, verb));
                            let label = format!(
                                "{}{} · {}",
                                button.wire(),
                                if long { " long" } else { "" },
                                verb.as_str()
                            );
                            tracing::info!(button = %button.wire(), long, verb = verb.as_str(), ack = ?ack, "Companion press");
                            if let Some(result) = ack {
                                let _ = active
                                    .link
                                    .write_line(&protocol::ack(verb.as_str(), result));
                                // Force the next tick to re-send the state line.
                                active.phase = String::new();
                                publish(
                                    &shared,
                                    Status {
                                        enabled: true,
                                        connected: true,
                                        transport: config.companion.transport.clone(),
                                        port: active.link.describe(),
                                        device: active.device.clone(),
                                        protocol: protocol::PROTOCOL_VERSION,
                                        last_button: label,
                                        error: None,
                                        ..Status::default()
                                    },
                                );
                            }
                        }
                        protocol::Inbound::Ptt { pressed } => {
                            if !active.negotiated || !config.companion.ptt {
                                continue;
                            }
                            let ack = shared.app.try_state::<AppState>().and_then(|state| {
                                state
                                    .send(if pressed {
                                        crate::controller::Action::Start
                                    } else {
                                        crate::controller::Action::Stop
                                    })
                                    .ok()
                            });
                            if ack.is_some() {
                                let _ = active.link.write_line(&protocol::ack(
                                    "dictate_toggle",
                                    if pressed { "ON" } else { "OFF" },
                                ));
                                active.phase = String::new();
                            }
                        }
                        protocol::Inbound::Discard => {
                            if !active.negotiated {
                                continue;
                            }
                            if let Some(state) = shared.app.try_state::<AppState>() {
                                if state.send(crate::controller::Action::Cancel).is_ok() {
                                    let _ = active.link.write_line(&protocol::ack("CANCEL", "OK"));
                                    active.phase = String::new();
                                }
                            }
                        }
                        protocol::Inbound::AcceptHistory { index: _ } => {
                            if !active.negotiated {
                                continue;
                            }
                            // Device history contains only 40-character previews and no
                            // host dictation IDs. Accepting by index could accept a different
                            // current suggestion, so show that suggestion instead.
                            let suggestion = shared.app.try_state::<AppState>().and_then(|state| {
                                state.enhance.snapshot(&state)["enhancement"]["proposed_text"]
                                    .as_str()
                                    .map(str::to_owned)
                            });
                            if let Some(text) = suggestion {
                                let _ = active.link.write_line(&protocol::suggestion(&text));
                            } else {
                                let _ = active
                                    .link
                                    .write_line(&protocol::ack("ACCEPT", "UNAVAILABLE"));
                            }
                        }
                        // A heartbeat is presence, not a request: answering it would make
                        // both ends reply to each other's replies forever.
                        protocol::Inbound::Ping { token } => {
                            // Only the echo of our own timed probe is a measurement. A bare
                            // heartbeat is presence and is never answered.
                            if let (Some(token), Some((pending, sent))) =
                                (token, active.probe_sent.as_ref())
                            {
                                if &token == pending {
                                    active.latency_ms =
                                        sent.elapsed().as_millis().min(9_999) as u32;
                                    active.probe_sent = None;
                                    let rssi = active.link.rssi().unwrap_or(0).clamp(-127, 0) as i8;
                                    if let Err(error) = active.link.write_line(
                                        &protocol::link_stats(active.latency_ms as u16, rssi),
                                    ) {
                                        tracing::warn!(%error, "Companion link stats write failed");
                                    }
                                    publish(&shared, session_status(&config, active, true));
                                }
                            }
                        }
                        protocol::Inbound::Battery { percent } => {
                            active.battery = Some(percent);
                            publish(&shared, session_status(&config, active, true));
                        }
                        protocol::Inbound::PttMode { .. } => {}
                        protocol::Inbound::Unknown => {}
                    }
                }
                Ok(None) => break,
                Err(error) => {
                    tracing::warn!(%error, "Companion link lost");
                    publish(
                        &shared,
                        Status {
                            enabled: true,
                            connected: false,
                            transport: config.companion.transport.clone(),
                            port: active.link.describe(),
                            device: active.device.clone(),
                            protocol: protocol::PROTOCOL_VERSION,
                            last_button: String::new(),
                            error: Some(format!("Link lost: {error}")),
                            ..Status::default()
                        },
                    );
                    dropped = true;
                    break;
                }
            }
        }
        if dropped {
            session = None;
            retry_at = Instant::now() + backoff;
            thread::sleep(link::POLL);
            continue;
        }

        // Heartbeat, and a hard timeout when the peer goes silent.
        if active.last_ping.elapsed() >= HEARTBEAT {
            let seq = active.probe_seq;
            if let Err(error) = active.link.write_line(&protocol::probe(seq)) {
                tracing::warn!(%error, "Companion link dropped: heartbeat write failed");
                failing = true;
                session = None;
                retry_at = Instant::now();
                continue;
            }
            active.probe_seq += 1;
            active.probe_sent = Some((seq.to_string(), Instant::now()));
            active.last_ping = Instant::now();
        }
        if active.last_rx.elapsed() > DEAD_AFTER {
            tracing::warn!(port = %active.link.describe(), "Companion link timed out");
            failing = true;
            session = None;
            retry_at = Instant::now();
            continue;
        }

        let phase = shared
            .app
            .try_state::<AppState>()
            .map(|state| state::phase(&state).line().to_string())
            .unwrap_or_default();
        // A new turn clears the device's live buffer, so announce it before the first T.
        if active.negotiated && phase == protocol::Phase::Listen.line() && phase != active.phase {
            if active.link.write_line(&phase).is_ok() {
                active.phase = phase.clone();
            }
        }

        // Live text and suggestions. Text is coalesced and rate-limited so a fast partial
        // cannot flood a BLE link; a suggestion goes out once, immediately.
        if active.negotiated {
            let next = shared.outbound.lock().ok().and_then(|mut slot| {
                slot.final_text
                    .take()
                    .map(Outbound::FinalText)
                    .or_else(|| slot.queued.take())
            });
            if let Some(outbound) = next {
                let (line, is_partial) = match &outbound {
                    Outbound::Text(body) => (protocol::text(body), true),
                    Outbound::FinalText(body) => (protocol::text(body), false),
                    Outbound::Suggestion(body) => (protocol::suggestion(body), false),
                };
                if is_partial && !text_due(active.last_text, Instant::now()) {
                    // Too soon: keep the newest text and try again on the next tick.
                    if let Ok(mut slot) = shared.outbound.lock() {
                        if slot.queued.is_none() {
                            slot.queued = Some(outbound);
                        }
                    }
                } else {
                    match active.link.write_line(&line) {
                        Ok(()) if !matches!(outbound, Outbound::Suggestion(_)) => {
                            active.last_text = Instant::now();
                        }
                        Err(_) if matches!(outbound, Outbound::FinalText(_)) => {
                            if let Ok(mut slot) = shared.outbound.lock() {
                                if slot.final_text.is_none() {
                                    if let Outbound::FinalText(body) = outbound {
                                        slot.final_text = Some(body);
                                    }
                                }
                            }
                        }
                        Err(_) if matches!(outbound, Outbound::Suggestion(_)) => {
                            if let Ok(mut slot) = shared.outbound.lock() {
                                slot.queued = Some(outbound);
                            }
                        }
                        _ => {}
                    }
                }
            }
        }

        // At the end of a turn, send any pending T before the state change so the
        // device can remember the newest text in its RAM history.
        if active.negotiated && !phase.is_empty() && phase != active.phase {
            if active.link.write_line(&phase).is_ok() {
                active.phase = phase;
            }
        }

        thread::sleep(link::POLL);
    }
    publish(&shared, Status::default());
}

fn retry_at_after(retry_at: &mut Instant, backoff: Duration) -> Duration {
    *retry_at = Instant::now() + backoff;
    std::cmp::min(backoff * 2, MAX_BACKOFF)
}

/// `CFG` line for the current map, keys sorted by `protocol::cfg`.
pub fn cfg_line(buttons: &crate::config::CompanionButtons) -> String {
    let mut pairs: Vec<(String, &'static str)> = Vec::new();
    for (key, value) in [
        ("OK", buttons.ok.as_str()),
        ("UP", buttons.up.as_str()),
        ("DOWN", buttons.down.as_str()),
        ("BACK", buttons.back.as_str()),
        ("LEFT", buttons.left.as_str()),
        ("RIGHT", buttons.right.as_str()),
        ("OK_LONG", buttons.ok_long.as_str()),
        ("UP_LONG", buttons.up_long.as_str()),
        ("DOWN_LONG", buttons.down_long.as_str()),
    ] {
        if let Some(verb) = map::Verb::parse(value) {
            pairs.push((key.to_string(), verb.wire()));
        }
    }
    let pairs: Vec<(&str, &str)> = pairs.iter().map(|(k, v)| (k.as_str(), *v)).collect();
    protocol::cfg(&pairs)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::CompanionButtons;

    use std::time::{Duration, Instant};

    #[test]
    fn text_is_due_only_after_the_coalescing_window() {
        let start = Instant::now();
        assert!(!text_due(start, start + Duration::from_millis(249)));
        assert!(text_due(start, start + TEXT_INTERVAL));
    }

    #[test]
    fn late_preview_cannot_replace_final_text() {
        let mut slot = OutboundSlot::default();
        slot.queue_partial("preview");
        slot.queue_final("finished");
        slot.queue_partial("late preview");
        assert!(slot.queued.is_none());
        assert!(matches!(slot.final_text.as_deref(), Some("finished")));
        let mut next_turn = OutboundSlot::default();
        next_turn.queue_partial("new preview");
        assert!(matches!(next_turn.queued, Some(Outbound::Text(_))));
    }

    #[test]
    fn empty_final_replaces_pending_preview_with_clear_frame() {
        let mut slot = OutboundSlot::default();
        slot.queue_partial("preview");
        slot.queue_final("");
        assert!(slot.queued.is_none());
        assert_eq!(
            slot.final_text.as_deref().map(protocol::text),
            Some("T".into())
        );
    }

    #[test]
    fn cfg_line_covers_every_default_button() {
        let line = cfg_line(&CompanionButtons::default());
        for key in ["OK=", "UP=", "DOWN=", "BACK="] {
            assert!(line.contains(key), "{line} missing {key}");
        }
        assert!(line.starts_with("CFG "));
        assert!(
            line.len() < protocol::MAX_LINE,
            "{line} is {} bytes",
            line.len()
        );
    }

    #[test]
    fn cfg_line_skips_unknown_verbs_instead_of_forwarding_them() {
        let mut buttons = CompanionButtons::default();
        buttons.left = "not_a_verb".into();
        let line = cfg_line(&buttons);
        assert!(!line.contains("LEFT="), "{line}");
    }

    #[test]
    fn cfg_line_is_sorted_so_it_is_stable() {
        let line = cfg_line(&CompanionButtons::default());
        let keys: Vec<&str> = line
            .trim_start_matches("CFG ")
            .split(' ')
            .map(|pair| pair.split('=').next().unwrap_or(""))
            .collect();
        let mut sorted = keys.clone();
        sorted.sort();
        assert_eq!(keys, sorted);
    }
}
