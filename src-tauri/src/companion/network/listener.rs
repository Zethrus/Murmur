use super::{audio, lan, tls, wire, MicCommand, RemoteTurn, Session, Settings, Shared};
use crate::{
    app::AppState,
    companion::{map, protocol},
};
use anyhow::{Context, Result};
use futures::StreamExt;
use murmur::handshake::{self, send_line};
use std::{
    net::SocketAddr,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tauri::Manager;
use tokio::{
    net::TcpListener,
    sync::{mpsc, watch, Semaphore},
    task::JoinSet,
};
use tokio_tungstenite::tungstenite::{
    protocol::{frame::coding::CloseCode, CloseFrame, WebSocketConfig},
    Message,
};
pub async fn run(
    shared: Arc<Shared>,
    setting: &Settings,
    changes: &mut watch::Receiver<Option<Settings>>,
) -> Result<()> {
    let data = shared.app.state::<AppState>().paths.data.clone();
    let identity = tls::load_or_create(&data)?;
    let address = SocketAddr::new(setting.bind.parse()?, setting.port);
    let listener = TcpListener::bind(address)
        .await
        .context("Bind companion TLS listener")?;
    *shared.fingerprint.lock().unwrap() = Some(identity.fingerprint);
    *shared.error.lock().unwrap() = None;
    tracing::info!(address = %listener.local_addr()?, "Companion TLS listener ready");
    let mut tasks = JoinSet::new();
    let permits = Arc::new(Semaphore::new(16));
    loop {
        tokio::select! {
            changed = changes.changed() => { if changed.is_err() || *changes.borrow_and_update() != Some(setting.clone()) { break; } }
            accepted = listener.accept() => {
                let (socket, peer) = match accepted {
                    Ok(accepted) => accepted,
                    Err(error) => {
                        tracing::warn!(%error, "Companion listener accept failed");
                        tokio::time::sleep(Duration::from_millis(100)).await;
                        continue;
                    }
                };
                if !lan::admit(peer, &shared.rejected_peers) { continue; }
                let Ok(permit) = permits.clone().try_acquire_owned() else { continue };
                let shared = shared.clone();
                let acceptor = identity.acceptor.clone();
                tasks.spawn(async move {
                    let _permit = permit;
                    if session(shared, peer, socket, acceptor).await.is_err() {
                        tracing::debug!(address = %peer.ip(), "Companion session ended");
                    }
                });
            }
            Some(_) = tasks.join_next(), if !tasks.is_empty() => {}
        }
    }
    shared.close_all();
    while tokio::time::timeout(Duration::from_secs(1), tasks.join_next())
        .await
        .is_ok_and(|r| r.is_some())
    {}
    tasks.abort_all();
    Ok(())
}

async fn session(
    shared: Arc<Shared>,
    peer: SocketAddr,
    socket: tokio::net::TcpStream,
    acceptor: tokio_rustls::TlsAcceptor,
) -> Result<()> {
    let auth = shared
        .auth
        .as_ref()
        .context("Companion token store unavailable")?;
    let (mut ws, id) = tokio::time::timeout(Duration::from_secs(15), async {
        let tls = acceptor.accept(socket).await?;
        let mut options = WebSocketConfig::default();
        options.max_message_size = Some(4096);
        options.max_frame_size = Some(4096);
        let mut ws = tokio_tungstenite::accept_async_with_config(tls, Some(options)).await?;
        let id =
            handshake::authenticate(&mut ws, peer.ip(), auth, protocol::PROTOCOL_VERSION).await?;
        Ok::<_, anyhow::Error>((ws, id))
    })
    .await??;
    let Some(id) = id else {
        return Ok(());
    };
    let (stop, mut stopped) = watch::channel(false);
    let (mic, mut mic_commands) = mpsc::channel(4);
    if let Some(previous) = shared.sessions.lock().unwrap().insert(
        id.clone(),
        Session {
            stop: stop.clone(),
            mic,
        },
    ) {
        let _ = previous.stop.send(true);
        tracing::info!("Companion device session replaced");
    }
    if !auth.lock().unwrap().has_device(&id) {
        let mut sessions = shared.sessions.lock().unwrap();
        if sessions
            .get(&id)
            .is_some_and(|active| active.stop.same_channel(&stop))
        {
            sessions.remove(&id);
        }
        return Ok(());
    }
    let mut last_phase = String::new();
    let mut gate = audio::Gate::default();
    let mut tick = tokio::time::interval(Duration::from_millis(100));
    let result: Result<()> = async { loop {
        tokio::select! {
            biased;
            _ = stopped.changed() => {
                let _ = ws.close(Some(CloseFrame { code: CloseCode::Policy, reason: "session ended".into() })).await;
                break Ok(());
            }
            Some(command) = mic_commands.recv() => {
                match command {
                    MicCommand::Start(next) => {
                        gate.start(next);
                        tokio::time::timeout(Duration::from_secs(5), send_line(&mut ws, &format!("MIC START {next}"))).await??;
                        let mut remote = shared.remote.lock().unwrap();
                        if remote.selected.as_deref() == Some(id.as_str()) && remote.stream == Some(next) {
                            remote.started_at = Some(std::time::Instant::now());
                        }
                    }
                    MicCommand::Stop(old) => {
                        gate.stop();
                        tokio::time::timeout(Duration::from_secs(5), send_line(&mut ws, &format!("MIC STOP {old}"))).await??;
                    }
                }
            }
            _ = tick.tick() => {
                if let Some(state) = shared.app.try_state::<AppState>() {
                    let phase = phase_line(&state);
                    if phase != last_phase {
                        tokio::time::timeout(Duration::from_secs(5), send_line(&mut ws, &phase)).await??;
                        last_phase = phase;
                    }
                }
            }
            inbound = ws.next() => {
                match inbound {
                    Some(Ok(Message::Text(line))) if wire::valid_line(&line) => {
                        if let Some(state) = shared.app.try_state::<AppState>() {
                            if let Some(verb) = requested_verb(&line, &state.config()?.companion.buttons) {
                                if let Some(ack) = map::dispatch(&state, verb) {
                                    tokio::time::timeout(Duration::from_secs(5), send_line(&mut ws, &format!("ACK {} {ack}", verb.as_str()))).await??;
                                }
                            }
                        }
                    }
                    Some(Ok(Message::Binary(bytes))) => {
                        let accepted = receive_frame(&mut gate, &shared.remote, &id, &bytes);
                        if let Err(error) = accepted {
                            let reason = error.to_string();
                            ws.close(Some(CloseFrame { code: CloseCode::Protocol, reason: reason.into() })).await?;
                            break Ok(());
                        }
                    }
                    Some(Ok(Message::Text(_))) => { ws.close(Some(CloseFrame { code: CloseCode::Size, reason: "line limit exceeded".into() })).await?; break Ok(()); }
                    Some(Ok(Message::Close(_))) | None => break Ok(()),
                    Some(Ok(_)) => {},
                    Some(Err(error)) => break Err(error.into()),
                }
            }
        }
    }}.await;
    let mut sessions = shared.sessions.lock().unwrap();
    if sessions
        .get(&id)
        .is_some_and(|active| active.stop.same_channel(&stop))
    {
        sessions.remove(&id);
    }
    result
}

fn receive_frame(
    gate: &mut audio::Gate,
    remote: &Mutex<RemoteTurn>,
    id: &str,
    bytes: &[u8],
) -> std::result::Result<(), audio::Error> {
    let frame = audio::Frame::parse(bytes)?;
    if gate.active() == Some(frame.stream_id) {
        let mut remote = remote.lock().unwrap();
        if remote.selected.as_deref() != Some(id) || remote.stream != Some(frame.stream_id) {
            // end_turn can clear selection before the MIC STOP command reaches this session.
            return Ok(());
        }
        gate.accept(&frame)?;
        remote.ring.push(frame);
        remote.received += 1;
        remote.last_at = Some(Instant::now());
    } else {
        // Only the last stopped stream may send frames after MIC STOP.
        gate.accept(&frame)?;
    }
    Ok(())
}

fn requested_verb(line: &str, buttons: &crate::config::CompanionButtons) -> Option<map::Verb> {
    if let Some(raw) = wire::control_verb(line) {
        return map::Verb::parse(raw);
    }
    if line.starts_with("B ") {
        if let protocol::Inbound::Press { button, long } = protocol::parse(line) {
            return Some(map::verb_for(buttons, button, long));
        }
    }
    None
}
fn phase_line(state: &AppState) -> String {
    let dictation = state.status().map(|s| s.state).unwrap_or_default();
    let snapshot = state.enhance.snapshot(state);
    wire::state_line(
        &dictation,
        snapshot["enhancement"]["phase"].as_str().unwrap_or(""),
    )
    .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{mpsc, Barrier, Mutex};
    #[test]
    fn line_limit_and_binary_rejection() {
        assert_eq!(wire::MAX_LINE, protocol::MAX_LINE);
        assert_eq!(wire::binary_close().code, CloseCode::Unsupported);
    }
    #[test]
    fn verb_routes() {
        let buttons = crate::config::CompanionButtons::default();
        assert_eq!(
            requested_verb("B dictate_toggle", &buttons),
            Some(map::Verb::DictateToggle)
        );
        assert_eq!(
            requested_verb("B OK", &buttons),
            Some(map::Verb::DictateToggle)
        );
        assert_eq!(
            requested_verb("S enhance", &buttons),
            Some(map::Verb::Enhance)
        );
        assert_eq!(
            requested_verb("A accept", &buttons),
            Some(map::Verb::Accept)
        );
    }
    #[test]
    fn in_flight_stop_frames_do_not_end_session() {
        let remote = Mutex::new(RemoteTurn::default());
        let mut gate = audio::Gate::default();
        let frame = |stream_id, sequence| {
            audio::Frame {
                flags: 0,
                stream_id,
                sequence,
                samples: [1; audio::SAMPLES],
            }
            .encode()
        };
        gate.start(17);
        {
            let mut turn = remote.lock().unwrap();
            turn.selected = Some("phone".into());
            turn.stream = Some(17);
        }
        receive_frame(&mut gate, &remote, "phone", &frame(17, 0)).unwrap();
        assert_eq!(remote.lock().unwrap().received, 1);
        // end_turn clears selection before the queued MIC STOP is processed.
        remote.lock().unwrap().selected = None;
        receive_frame(&mut gate, &remote, "phone", &frame(17, 0)).unwrap();
        gate.stop();
        receive_frame(&mut gate, &remote, "phone", &frame(17, 2)).unwrap();
        gate.start(18);
        receive_frame(&mut gate, &remote, "phone", &frame(17, 3)).unwrap();
        assert_eq!(remote.lock().unwrap().received, 1);
        assert_eq!(
            receive_frame(&mut gate, &remote, "phone", &frame(19, 0)),
            Err(audio::Error::Stream)
        );
    }
    #[test]
    fn busy_remote_lock_does_not_drop_valid_frame() {
        let remote = Arc::new(Mutex::new(RemoteTurn::default()));
        {
            let mut turn = remote.lock().unwrap();
            turn.selected = Some("phone".into());
            turn.stream = Some(17);
        }
        let barrier = Arc::new(Barrier::new(2));
        let held = remote.lock().unwrap();
        let remote_for_thread = remote.clone();
        let barrier_for_thread = barrier.clone();
        let (done, received) = mpsc::channel();
        let job = std::thread::spawn(move || {
            let mut gate = audio::Gate::default();
            gate.start(17);
            let bytes = audio::Frame {
                flags: 0,
                stream_id: 17,
                sequence: 0,
                samples: [1; audio::SAMPLES],
            }
            .encode();
            barrier_for_thread.wait();
            done.send(receive_frame(
                &mut gate,
                &remote_for_thread,
                "phone",
                &bytes,
            ))
            .unwrap();
        });
        barrier.wait();
        std::thread::sleep(Duration::from_millis(20));
        drop(held);
        received
            .recv_timeout(Duration::from_secs(1))
            .unwrap()
            .unwrap();
        job.join().unwrap();
        assert_eq!(remote.lock().unwrap().received, 1);
    }
}
