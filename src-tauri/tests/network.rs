//! Exercise the TLS, auth, LAN and wire components with a stub controller.
use anyhow::{Context, Result};
use futures::{SinkExt, StreamExt};
use murmur::{audio, auth, handshake, lan, tls, wire};
use rustls::{
    client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
    pki_types::{CertificateDer, ServerName, UnixTime},
    DigitallySignedStruct, Error, SignatureScheme,
};
use sha2::{Digest, Sha256};
use std::{
    net::{IpAddr, SocketAddr},
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    },
};
use subtle::ConstantTimeEq;
use tokio::{
    net::{TcpListener, TcpStream},
    sync::oneshot,
};
use tokio_tungstenite::{client_async, tungstenite::Message, WebSocketStream};

#[derive(Debug)]
struct Pin([u8; 32]);
impl ServerCertVerifier for Pin {
    fn verify_server_cert(
        &self,
        cert: &CertificateDer<'_>,
        _: &[CertificateDer<'_>],
        _: &ServerName<'_>,
        _: &[u8],
        _: UnixTime,
    ) -> std::result::Result<ServerCertVerified, Error> {
        let actual: [u8; 32] = Sha256::digest(cert.as_ref()).into();
        if bool::from(self.0.ct_eq(&actual)) {
            Ok(ServerCertVerified::assertion())
        } else {
            Err(Error::General("fingerprint mismatch".into()))
        }
    }
    fn verify_tls12_signature(
        &self,
        _: &[u8],
        _: &CertificateDer<'_>,
        _: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, Error> {
        Err(Error::General("TLS 1.2 disabled".into()))
    }
    fn verify_tls13_signature(
        &self,
        msg: &[u8],
        cert: &CertificateDer<'_>,
        sig: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, Error> {
        rustls::crypto::verify_tls13_signature(
            msg,
            cert,
            sig,
            &rustls::crypto::aws_lc_rs::default_provider().signature_verification_algorithms,
        )
    }
    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        rustls::crypto::aws_lc_rs::default_provider()
            .signature_verification_algorithms
            .supported_schemes()
    }
}
type Client = WebSocketStream<tokio_rustls::client::TlsStream<TcpStream>>;
async fn connect(address: SocketAddr, pin: [u8; 32]) -> Result<Client> {
    let tcp = TcpStream::connect(address).await?;
    let mut config =
        rustls::ClientConfig::builder_with_protocol_versions(&[&rustls::version::TLS13])
            .with_root_certificates(rustls::RootCertStore::empty())
            .with_no_client_auth();
    config
        .dangerous()
        .set_certificate_verifier(Arc::new(Pin(pin)));
    let stream = tokio_rustls::TlsConnector::from(Arc::new(config))
        .connect(ServerName::try_from("localhost")?, tcp)
        .await?;
    Ok(client_async("wss://localhost/", stream).await?.0)
}
async fn line(ws: &mut Client) -> Result<String> {
    Ok(ws.next().await.context("closed")??.into_text()?.to_string())
}
async fn auth_client(ws: &mut Client, credential: &str) -> Result<String> {
    assert!(line(ws).await?.starts_with("HELLO 4 "));
    ws.send(Message::Text("HELLO 4 test phone CAP mic".into()))
        .await?;
    ws.send(Message::Text(format!("AUTH {credential}").into()))
        .await?;
    line(ws).await
}

async fn stub_session(
    socket: TcpStream,
    peer: SocketAddr,
    acceptor: tokio_rustls::TlsAcceptor,
    auth: Arc<Mutex<auth::Auth>>,
    actions: Arc<AtomicUsize>,
) -> Result<()> {
    if !lan::allows(peer) {
        return Ok(());
    }
    let stream = acceptor.accept(socket).await?;
    let mut ws = tokio_tungstenite::accept_async(stream).await?;
    if handshake::authenticate(&mut ws, peer.ip(), &auth, 4)
        .await?
        .is_none()
    {
        return Ok(());
    }
    ws.send(Message::Text(wire::state_line("listening", "none").into()))
        .await?;
    while let Some(message) = ws.next().await {
        match message? {
            Message::Text(text) if wire::valid_line(&text) => {
                if wire::control_verb(&text) == Some("dictate_toggle") {
                    actions.fetch_add(1, Ordering::SeqCst);
                    ws.send(Message::Text("ACK dictate_toggle ON".into()))
                        .await?;
                }
            }
            Message::Binary(_) => {
                ws.close(Some(wire::binary_close())).await?;
                break;
            }
            _ => break,
        }
    }
    Ok(())
}

#[tokio::test]
async fn pinned_websocket_pairing_reconnect_and_controls() -> Result<()> {
    let dir = std::env::temp_dir().join(format!(
        "murmur-network-test-{}-{}",
        std::process::id(),
        auth::now()
    ));
    std::fs::create_dir_all(&dir)?;
    let identity = tls::load_or_create(&dir)?;
    let pin: [u8; 32] = Sha256::digest(std::fs::read(dir.join("companion/tls/cert.der"))?).into();
    let code = {
        let mut auth = auth::Auth::load(&dir)?;
        let code = auth.show_code()?.code;
        (Arc::new(Mutex::new(auth)), code)
    };
    let actions = Arc::new(AtomicUsize::new(0));
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let address = listener.local_addr()?;
    let (stop, mut stopped) = oneshot::channel::<()>();
    let auth = code.0.clone();
    let actions_for_server = actions.clone();
    let server = tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = &mut stopped => break,
                Ok((socket, peer)) = listener.accept() => {
                    tokio::spawn(stub_session(socket, peer, identity.acceptor.clone(), auth.clone(), actions_for_server.clone()));
                }
            }
        }
    });
    let mut wrong = pin;
    wrong[0] ^= 1;
    let wrong_display = wrong
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<Vec<_>>()
        .join(":");
    let host = address.to_string();
    let code_for_cli = code.1.clone();
    let cli = tokio::task::spawn_blocking(move || {
        std::process::Command::new(env!("CARGO_BIN_EXE_murmur-companion-client"))
            .args([
                "--host",
                &host,
                "--fingerprint",
                &wrong_display,
                "--code",
                &code_for_cli,
            ])
            .output()
    })
    .await??;
    assert!(!cli.status.success());
    assert!(String::from_utf8_lossy(&cli.stderr).contains("fingerprint"));
    let mismatch = connect(address, wrong)
        .await
        .err()
        .context("wrong fingerprint was accepted")?;
    assert!(mismatch.to_string().contains("fingerprint mismatch"));
    let refusals = std::sync::atomic::AtomicU64::new(0);
    assert!(!lan::admit(
        SocketAddr::new("8.8.8.8".parse::<IpAddr>()?, 1),
        &refusals
    ));
    assert_eq!(refusals.load(Ordering::Relaxed), 1);
    let mut paired = connect(address, pin).await?;
    let reply = auth_client(&mut paired, &code.1).await?;
    let token = reply
        .strip_prefix("AUTH OK ")
        .context("pairing did not return token")?
        .to_string();
    assert_eq!(token.len(), 64);
    assert!(!std::fs::read_to_string(dir.join("companion/tokens.json"))?.contains(&token));
    assert_eq!(line(&mut paired).await?, "STATE listening");
    paired
        .send(Message::Text("B dictate_toggle".into()))
        .await?;
    assert_eq!(line(&mut paired).await?, "ACK dictate_toggle ON");
    assert_eq!(actions.load(Ordering::SeqCst), 1);
    paired.send(Message::Binary(vec![0; 2].into())).await?;
    let close = paired.next().await.context("binary was not closed")??;
    assert!(
        matches!(close, Message::Close(Some(ref frame)) if frame.reason.contains("unsupported"))
    );
    let mut resumed = connect(address, pin).await?;
    assert_eq!(auth_client(&mut resumed, &token).await?, "AUTH OK");
    assert_eq!(line(&mut resumed).await?, "STATE listening");
    let invalid = format!("{:06}", (code.1.parse::<u32>()? + 1) % 1_000_000);
    for _ in 0..5 {
        let mut bad = connect(address, pin).await?;
        assert_eq!(
            auth_client(&mut bad, &invalid).await?,
            "AUTH DENY invalid_credential"
        );
    }
    let mut locked = connect(address, pin).await?;
    assert_eq!(
        auth_client(&mut locked, &token).await?,
        "AUTH DENY rate_limited"
    );
    let id = code.0.lock().unwrap().list()[0].id.clone();
    assert!(code.0.lock().unwrap().revoke(&id)?);
    assert!(auth::Auth::load(&dir)?.list().is_empty());
    let _ = stop.send(());
    server.await?;
    std::fs::remove_dir_all(&dir)?;
    Ok(())
}

#[tokio::test]
async fn paired_toggle_streams_one_second_in_order_without_audio_files() -> Result<()> {
    let dir = std::env::temp_dir().join(format!(
        "murmur-audio-test-{}-{}",
        std::process::id(),
        auth::now()
    ));
    std::fs::create_dir_all(&dir)?;
    let identity = tls::load_or_create(&dir)?;
    let pin: [u8; 32] = Sha256::digest(std::fs::read(dir.join("companion/tls/cert.der"))?).into();
    let mut authenticator = auth::Auth::load(&dir)?;
    let code = authenticator.show_code()?.code;
    let authenticator = Arc::new(Mutex::new(authenticator));
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let address = listener.local_addr()?;
    let server = tokio::spawn(async move {
        let (socket, peer) = listener.accept().await?;
        let tls = identity.acceptor.accept(socket).await?;
        let mut ws = tokio_tungstenite::accept_async(tls).await?;
        handshake::authenticate(&mut ws, peer.ip(), &authenticator, 4)
            .await?
            .context("auth denied")?;
        let mut gate = audio::Gate::default();
        let mut sink = Vec::<i16>::new();
        while let Some(message) = ws.next().await {
            match message? {
                Message::Text(line) if line == "B dictate_toggle" && gate.active().is_none() => {
                    gate.start(17);
                    ws.send(Message::Text("MIC START 17".into())).await?;
                }
                Message::Text(line) if line == "B dictate_toggle" => {
                    gate.stop();
                    ws.send(Message::Text("MIC STOP 17".into())).await?;
                    break;
                }
                Message::Binary(bytes) => {
                    let frame = audio::Frame::parse(&bytes)?;
                    gate.accept(&frame)?;
                    sink.extend_from_slice(&frame.samples);
                }
                _ => anyhow::bail!("unexpected frame"),
            }
        }
        Ok::<_, anyhow::Error>(sink)
    });
    let mut wav = std::io::Cursor::new(Vec::new());
    {
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: 16_000,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut writer = hound::WavWriter::new(&mut wav, spec)?;
        for sample in -8000i16..8000i16 {
            writer.write_sample(sample)?;
        }
        writer.finalize()?;
    }
    wav.set_position(0);
    let mut reader = hound::WavReader::new(wav)?;
    let mut samples = reader.samples::<i16>();
    let mut client = connect(address, pin).await?;
    assert!(auth_client(&mut client, &code)
        .await?
        .starts_with("AUTH OK "));
    client
        .send(Message::Text("B dictate_toggle".into()))
        .await?;
    assert_eq!(line(&mut client).await?, "MIC START 17");
    for sequence in 0..50 {
        let mut pcm = [0i16; audio::SAMPLES];
        for sample in &mut pcm {
            *sample = samples.next().context("short WAV")??;
        }
        client
            .send(Message::Binary(
                audio::Frame {
                    flags: 0,
                    stream_id: 17,
                    sequence,
                    samples: pcm,
                }
                .encode()
                .to_vec()
                .into(),
            ))
            .await?;
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    assert!(samples.next().is_none());
    client
        .send(Message::Text("B dictate_toggle".into()))
        .await?;
    assert_eq!(line(&mut client).await?, "MIC STOP 17");
    let sink = server.await??;
    assert_eq!(sink.len(), 16_000);
    assert!(sink.iter().copied().eq(-8000i16..8000i16));
    fn files(path: &std::path::Path, out: &mut Vec<std::path::PathBuf>) -> Result<()> {
        for entry in std::fs::read_dir(path)? {
            let path = entry?.path();
            if path.is_dir() {
                files(&path, out)?;
            } else {
                out.push(path);
            }
        }
        Ok(())
    }
    let mut written = Vec::new();
    files(&dir, &mut written)?;
    assert!(written.iter().all(|path| matches!(
        path.extension().and_then(|s| s.to_str()),
        Some("der" | "json")
    )));
    std::fs::remove_dir_all(dir)?;
    Ok(())
}
