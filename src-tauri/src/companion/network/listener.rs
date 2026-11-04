use super::{lan, tls, wire, Settings, Shared};
use crate::{
    app::AppState,
    companion::{map, protocol},
};
use anyhow::{Context, Result};
use futures::StreamExt;
use murmur::handshake::{self, send_line};
use std::{net::SocketAddr, sync::Arc, time::Duration};
use tauri::Manager;
use tokio::{
    net::TcpListener,
    sync::{watch, Semaphore},
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
    if let Some(previous) = shared
        .sessions
        .lock()
        .unwrap()
        .insert(id.clone(), stop.clone())
    {
        let _ = previous.send(true);
        tracing::info!("Companion device session replaced");
    }
    if !auth.lock().unwrap().has_device(&id) {
        let mut sessions = shared.sessions.lock().unwrap();
        if sessions
            .get(&id)
            .is_some_and(|active| active.same_channel(&stop))
        {
            sessions.remove(&id);
        }
        return Ok(());
    }
    let mut last_phase = String::new();
    let mut tick = tokio::time::interval(Duration::from_millis(100));
    let result: Result<()> = async { loop {
        tokio::select! {
            biased;
            _ = stopped.changed() => {
                let _ = ws.close(Some(CloseFrame { code: CloseCode::Policy, reason: "session ended".into() })).await;
                break Ok(());
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
                    Some(Ok(Message::Binary(_))) => { ws.close(Some(wire::binary_close())).await?; break Ok(()); }
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
        .is_some_and(|active| active.same_channel(&stop))
    {
        sessions.remove(&id);
    }
    result
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
}
