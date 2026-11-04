//! Authentication after the TLS and WebSocket handshakes.
use crate::{auth::Auth, wire};
use anyhow::{bail, Result};
use futures::{SinkExt, StreamExt};
use std::{net::IpAddr, sync::Mutex};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio_tungstenite::{
    tungstenite::{
        protocol::{frame::coding::CloseCode, CloseFrame},
        Message,
    },
    WebSocketStream,
};

pub async fn send_line<S: AsyncRead + AsyncWrite + Unpin>(
    ws: &mut WebSocketStream<S>,
    line: &str,
) -> Result<()> {
    debug_assert!(wire::valid_line(line));
    ws.send(Message::Text(line.into())).await?;
    Ok(())
}

pub async fn next_line<S: AsyncRead + AsyncWrite + Unpin>(
    ws: &mut WebSocketStream<S>,
) -> Result<String> {
    match ws.next().await {
        Some(Ok(Message::Text(line))) if wire::valid_line(&line) => Ok(line.to_string()),
        Some(Ok(Message::Binary(_))) => {
            let _ = ws.close(Some(wire::binary_close())).await;
            bail!("unsupported binary frame")
        }
        Some(Ok(Message::Text(_))) => {
            let _ = ws
                .close(Some(CloseFrame {
                    code: CloseCode::Size,
                    reason: "line limit exceeded".into(),
                }))
                .await;
            bail!("line limit exceeded")
        }
        _ => bail!("session closed or invalid frame"),
    }
}

/// Sends the server greeting, checks the peer greeting, then verifies a code or token.
/// `None` means an explicit denial was sent. The token itself is only sent to the peer.
pub async fn authenticate<S: AsyncRead + AsyncWrite + Unpin>(
    ws: &mut WebSocketStream<S>,
    peer: IpAddr,
    auth: &Mutex<Auth>,
    protocol: u8,
) -> Result<Option<String>> {
    send_line(
        ws,
        &format!("HELLO {protocol} {} host", env!("CARGO_PKG_VERSION")),
    )
    .await?;
    let hello = next_line(ws).await?;
    let mut parts = hello.split(' ');
    if parts.next() != Some("HELLO")
        || parts.next().and_then(|v| v.parse::<u8>().ok()) != Some(protocol)
    {
        send_line(ws, "AUTH DENY protocol").await?;
        return Ok(None);
    }
    let name = parts.next().unwrap_or("");
    if name.is_empty()
        || parts.next() != Some("phone")
        || parts.next() != Some("CAP")
        || parts.next() != Some("mic")
        || parts.next().is_some()
    {
        send_line(ws, "AUTH DENY hello").await?;
        return Ok(None);
    }
    let auth_line = next_line(ws).await?;
    let credential = auth_line.strip_prefix("AUTH ").unwrap_or("");
    let decision = auth.lock().unwrap().authenticate(peer, credential, name)?;
    match decision {
        Ok((id, Some(token))) => {
            send_line(ws, &format!("AUTH OK {token}")).await?;
            Ok(Some(id))
        }
        Ok((id, None)) => {
            send_line(ws, "AUTH OK").await?;
            Ok(Some(id))
        }
        Err(reason) => {
            send_line(ws, &format!("AUTH DENY {reason}")).await?;
            Ok(None)
        }
    }
}
