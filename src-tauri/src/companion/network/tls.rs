use anyhow::{bail, Context, Result};
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use sha2::{Digest, Sha256};
use std::{fs, path::Path, sync::Arc};

pub struct Identity {
    pub acceptor: tokio_rustls::TlsAcceptor,
    pub fingerprint: String,
}

pub fn fingerprint(der: &[u8]) -> String {
    Sha256::digest(der)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<Vec<_>>()
        .join(":")
}

pub fn load_or_create(data: &Path) -> Result<Identity> {
    let dir = data.join("companion/tls");
    fs::create_dir_all(&dir).context("Create companion TLS directory")?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700))?;
    }
    let cert_path = dir.join("cert.der");
    let key_path = dir.join("key.der");
    if !cert_path.exists() && !key_path.exists() {
        let generated = rcgen::generate_simple_self_signed(vec!["localhost".into()])?;
        write_secret(&key_path, &generated.key_pair.serialize_der())?;
        write_secret(&cert_path, generated.cert.der().as_ref())?;
    }
    if !cert_path.exists() || !key_path.exists() {
        bail!("Companion TLS certificate and key must both exist");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if fs::metadata(&key_path)?.permissions().mode() & 0o077 != 0 {
            bail!("Companion TLS key is group/world readable");
        }
    }
    let cert = fs::read(cert_path)?;
    let key = fs::read(key_path)?;
    let config = rustls::ServerConfig::builder_with_protocol_versions(&[&rustls::version::TLS13])
        .with_no_client_auth()
        .with_single_cert(
            vec![CertificateDer::from(cert.clone())],
            PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(key)),
        )?;
    Ok(Identity {
        acceptor: tokio_rustls::TlsAcceptor::from(Arc::new(config)),
        fingerprint: fingerprint(&cert),
    })
}

fn write_secret(path: &Path, bytes: &[u8]) -> Result<()> {
    use std::io::Write;
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn private_key_permissions_are_enforced() -> Result<()> {
        let dir = std::env::temp_dir().join(format!("murmur-tls-test-{}", std::process::id()));
        fs::create_dir_all(&dir)?;
        let identity = load_or_create(&dir)?;
        assert_eq!(identity.fingerprint.len(), 95);
        assert!(identity
            .fingerprint
            .bytes()
            .all(|b| b.is_ascii_hexdigit() || b == b':'));
        let tls_dir = dir.join("companion/tls");
        let key = tls_dir.join("key.der");
        assert_eq!(fs::metadata(&tls_dir)?.permissions().mode() & 0o777, 0o700);
        assert_eq!(fs::metadata(&key)?.permissions().mode() & 0o777, 0o600);
        fs::set_permissions(&key, fs::Permissions::from_mode(0o644))?;
        assert!(load_or_create(&dir).is_err());
        fs::remove_dir_all(dir)?;
        Ok(())
    }
}
