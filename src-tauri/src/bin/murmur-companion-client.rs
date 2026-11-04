//! Small pinned-TLS companion harness. Use the printed AUTH OK token for reconnects.
use anyhow::{bail, Context, Result};
use futures::{SinkExt, StreamExt};
use rustls::{
    client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
    pki_types::{CertificateDer, ServerName, UnixTime},
    DigitallySignedStruct, Error, SignatureScheme,
};
use sha2::{Digest, Sha256};
use std::{env, sync::Arc, time::Duration};
use subtle::ConstantTimeEq;
use tokio::net::TcpStream;
use tokio_tungstenite::{client_async, tungstenite::Message};

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
            Err(Error::General("TLS fingerprint mismatch".into()))
        }
    }
    fn verify_tls12_signature(
        &self,
        _: &[u8],
        _: &CertificateDer<'_>,
        _: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, Error> {
        Err(Error::General("TLS 1.2 is disabled".into()))
    }
    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, Error> {
        rustls::crypto::verify_tls13_signature(
            message,
            cert,
            signature,
            &rustls::crypto::aws_lc_rs::default_provider().signature_verification_algorithms,
        )
    }
    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        rustls::crypto::aws_lc_rs::default_provider()
            .signature_verification_algorithms
            .supported_schemes()
    }
}
fn decode_pin(value: &str) -> Result<[u8; 32]> {
    let normalized: String = value.chars().filter(|c| *c != ':').collect();
    if normalized.len() != 64 {
        bail!("Fingerprint must be 32 hex bytes");
    }
    let mut bytes = [0u8; 32];
    for (i, byte) in bytes.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&normalized[i * 2..i * 2 + 2], 16)
            .context("Invalid fingerprint hex")?;
    }
    Ok(bytes)
}
#[tokio::main]
async fn main() -> Result<()> {
    let mut host = None;
    let mut pin = None;
    let mut credential = None;
    let mut say = None;
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        let value = args.next().context("Missing option value")?;
        match arg.as_str() {
            "--host" => host = Some(value),
            "--fingerprint" => pin = Some(decode_pin(&value)?),
            "--code" => {
                if value.len() != 6 || !value.bytes().all(|b| b.is_ascii_digit()) {
                    bail!("Code must be six digits");
                }
                credential = Some(value);
            }
            "--token" => {
                if value.len() != 64 || !value.bytes().all(|b| b.is_ascii_hexdigit()) {
                    bail!("Token must be 64 hex characters");
                }
                credential = Some(value);
            }
            "--say" => {
                if !["B", "S", "A"].contains(&value.as_str()) {
                    bail!("--say must be B, S, or A");
                }
                say = Some(value);
            }
            _ => bail!("Unknown option {arg}"),
        }
    }
    let host = host.context("--host <ip:port> required")?;
    let pin = pin.context("--fingerprint <hex> required")?;
    let credential = credential.context("--code or --token required")?;
    let tcp = TcpStream::connect(&host)
        .await
        .context("Connect to companion host")?;
    let mut config =
        rustls::ClientConfig::builder_with_protocol_versions(&[&rustls::version::TLS13])
            .with_root_certificates(rustls::RootCertStore::empty())
            .with_no_client_auth();
    config
        .dangerous()
        .set_certificate_verifier(Arc::new(Pin(pin)));
    let tls = tokio_rustls::TlsConnector::from(Arc::new(config))
        .connect(ServerName::try_from("localhost")?, tcp)
        .await
        .context("TLS handshake or fingerprint verification failed")?;
    let (mut ws, _) = client_async("wss://localhost/", tls)
        .await
        .context("WebSocket handshake")?;
    if let Some(message) = ws.next().await {
        println!("{}", message?.into_text()?);
    }
    ws.send(Message::Text(
        format!("HELLO 4 {} phone CAP mic", env!("CARGO_PKG_VERSION")).into(),
    ))
    .await?;
    ws.send(Message::Text(format!("AUTH {credential}").into()))
        .await?;
    let reply = ws.next().await.context("No AUTH reply")??.into_text()?;
    println!("{reply}");
    if !reply.starts_with("AUTH OK") {
        bail!("Companion authentication denied");
    }
    if let Some(say) = say {
        let line = match say.as_str() {
            "B" => "B dictate_toggle",
            "S" => "S enhance",
            _ => "A accept",
        };
        ws.send(Message::Text(line.into())).await?;
    }
    while let Ok(Some(message)) = tokio::time::timeout(Duration::from_millis(750), ws.next()).await
    {
        match message? {
            Message::Text(line) => println!("{line}"),
            Message::Close(frame) => {
                if let Some(frame) = frame {
                    println!("CLOSE {}", frame.reason);
                }
                break;
            }
            _ => {}
        }
    }
    Ok(())
}
