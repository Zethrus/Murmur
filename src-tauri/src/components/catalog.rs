//! Where the trusted manifest comes from (embedded, cached, fetched) and the anti-rollback
//! state. `state.json` is unsigned: it protects against a network or mirror attacker serving
//! an older signed manifest, not against someone who can already write the user's files.
use super::{
    fetch::{self, Transport},
    manifest::{Manifest, CHANNEL},
    trust::{self, TrustRoot},
    write_atomic, ComponentError, Reason,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    path::{Path, PathBuf},
    sync::{atomic::AtomicBool, Arc},
};

pub const MANIFEST_FILE: &str = "components-manifest.json";
pub const SIGNATURE_FILE: &str = "components-manifest.json.sig";
pub const CERT_FILE: &str = "murmur-release.asc";
/// Release assets of the newest published (never draft) release.
pub const RELEASE_BASE: &str = "https://github.com/Zethrus/Murmur/releases/latest/download/";
pub const URL_ENV: &str = "MURMUR_COMPONENTS_URL";
const MAX_SMALL: u64 = 1 << 20;
/// The manifest built into the binary may be issued up to this far "in the future": a computer
/// whose clock is slightly behind must not report a clock error on first run. Fetched and
/// cached manifests keep the strict rule.
pub const EMBEDDED_CLOCK_SLACK: u64 = 24 * 3600;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Source {
    Embedded,
    Cached,
    Fetched,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct Seen {
    pub serial: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct RollbackState {
    #[serde(default)]
    pub channels: BTreeMap<String, Seen>,
    #[serde(default)]
    pub newest_issued_at: u64,
    #[serde(default)]
    pub primary_revoked: bool,
}

#[derive(Debug, Clone)]
pub struct Catalog {
    pub manifest: Manifest,
    pub sha256: String,
    pub source: Source,
    /// Verified and not a rollback, but past `expires_at`: listed for display, verify and
    /// remove; nothing new is downloaded or activated until Check for updates succeeds.
    pub stale: bool,
}

impl Catalog {
    /// Expired at load, or has expired since.
    pub fn is_stale(&self, now: u64) -> bool {
        self.stale || !self.manifest.expires_at().is_ok_and(|e| e >= now)
    }
}

pub fn digest(bytes: &[u8]) -> String {
    fetch::hex(&Sha256::digest(bytes))
}

impl RollbackState {
    pub fn load(path: &Path) -> Self {
        fs::read(path)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default()
    }
    pub fn save(&self, path: &Path) -> Result<(), ComponentError> {
        write_atomic(path, &serde_json::to_vec_pretty(self).unwrap_or_default())
    }
    /// Freshness and anti-rollback rules; records the manifest when accepted. `clock_slack`
    /// tolerates a manifest issued that many seconds after `now` (the embedded manifest only).
    /// Rollback is checked before expiry, so `Reason::Expired` means "trusted, not a rollback,
    /// only too old": the caller may still list it as a stale catalog.
    pub fn admit(
        &mut self,
        manifest: &Manifest,
        sha256: &str,
        now: u64,
        clock_slack: u64,
    ) -> Result<(), ComponentError> {
        if self.primary_revoked {
            return Err(ComponentError::new(
                Reason::PrimaryRevoked,
                "Murmur's release key has been revoked; component installs are disabled.",
            ));
        }
        let issued = manifest.issued_at()?;
        let clock = now.saturating_add(clock_slack);
        if clock < issued || clock < self.newest_issued_at {
            return Err(ComponentError::new(
                Reason::ClockError,
                "Clock error likely: this computer's date is earlier than a component manifest Murmur has already seen. Check the system clock.",
            ));
        }
        if let Some(seen) = self.channels.get(&manifest.channel) {
            if manifest.serial < seen.serial {
                return Err(ComponentError::new(
                    Reason::Rollback,
                    "Refused an older component manifest (rollback)",
                ));
            }
            if manifest.serial == seen.serial && seen.sha256 != sha256 {
                return Err(ComponentError::new(
                    Reason::Rollback,
                    "Refused a different manifest with an already used serial number",
                ));
            }
        }
        if manifest.expires_at()? < now {
            return Err(expired());
        }
        self.channels.insert(
            manifest.channel.clone(),
            Seen {
                serial: manifest.serial,
                sha256: sha256.into(),
            },
        );
        self.newest_issued_at = self.newest_issued_at.max(issued);
        Ok(())
    }
}

pub fn expired() -> ComponentError {
    ComponentError::new(
        Reason::Expired,
        "The component list has expired. Use Check for updates.",
    )
}

/// Trust paths under `<data>/components`.
#[derive(Debug, Clone)]
pub struct Files {
    pub state: PathBuf,
    pub cert: PathBuf,
    pub manifest: PathBuf,
    pub signature: PathBuf,
}
impl Files {
    pub fn new(root: &Path) -> Self {
        Self {
            state: root.join("state.json"),
            cert: root.join("trust").join(CERT_FILE),
            manifest: root.join("trust").join(MANIFEST_FILE),
            signature: root.join("trust").join(SIGNATURE_FILE),
        }
    }
}

/// The certificate in force: the pinned root merged with the stored certificate update when
/// that update is acceptable against the root. Never falls back to the bare root because of a
/// manifest's issue time: the verifier checks subkey validity at that time itself.
pub fn effective_cert(
    root: &TrustRoot,
    stored: Option<&[u8]>,
) -> Result<sequoia_openpgp::Cert, ComponentError> {
    if let Some(bytes) = stored {
        match trust::accept_update(&root.cert, bytes) {
            Ok(Some(cert)) => return Ok(cert),
            Ok(None) => {}
            Err(error) if error.reason == Reason::PrimaryRevoked => return Err(error),
            Err(_) => {
                tracing::warn!(target: "murmur::components", "Ignoring a stored certificate update that no longer verifies")
            }
        }
    }
    Ok(root.cert.clone())
}

/// Verify `manifest` + `signature` with `cert` and parse. Nothing is used unless this is Ok.
pub fn verify_with(
    cert: &sequoia_openpgp::Cert,
    manifest: &[u8],
    signature: &[u8],
) -> Result<Manifest, ComponentError> {
    let parsed = Manifest::parse(manifest)?;
    let issued_at = parsed.issued_at()?;
    trust::verify(cert, manifest, signature, trust::unix(issued_at))?;
    Ok(parsed)
}

/// Verify with the certificate in force (root + stored update).
pub fn verify(
    root: &TrustRoot,
    cert_update: Option<&[u8]>,
    manifest: &[u8],
    signature: &[u8],
) -> Result<Manifest, ComponentError> {
    verify_with(&effective_cert(root, cert_update)?, manifest, signature)
}

pub fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Load the best trusted manifest available offline: embedded and cached copies are verified
/// on every load; the higher serial that passes the rollback rules wins. When every verified
/// candidate has merely expired, the newest one is returned as a stale catalog.
pub fn load_offline(
    root: &TrustRoot,
    files: &Files,
    embedded: Option<(&[u8], &[u8])>,
    now: u64,
) -> Result<Catalog, ComponentError> {
    let mut state = RollbackState::load(&files.state);
    let fail = |state: &mut RollbackState, error: ComponentError| {
        if error.reason == Reason::PrimaryRevoked {
            state.primary_revoked = true;
            let _ = state.save(&files.state);
        }
        error
    };
    let cert = match effective_cert(root, fs::read(&files.cert).ok().as_deref()) {
        Ok(cert) => cert,
        Err(error) => return Err(fail(&mut state, error)),
    };
    let cached = fs::read(&files.manifest)
        .ok()
        .zip(fs::read(&files.signature).ok());
    let mut candidates = Vec::new();
    let mut last_error = None;
    for (source, pair) in [
        (
            Source::Cached,
            cached.as_ref().map(|(m, s)| (m.as_slice(), s.as_slice())),
        ),
        (Source::Embedded, embedded),
    ] {
        let Some((bytes, signature)) = pair else {
            continue;
        };
        match verify_with(&cert, bytes, signature) {
            Ok(manifest) => candidates.push(Catalog {
                sha256: digest(bytes),
                manifest,
                source,
                stale: false,
            }),
            Err(error) => {
                if error.reason == Reason::PrimaryRevoked {
                    return Err(fail(&mut state, error));
                }
                last_error = Some(error);
            }
        }
    }
    candidates.sort_by_key(|c| std::cmp::Reverse(c.manifest.serial));
    let mut stale = None;
    for candidate in candidates {
        let slack = match candidate.source {
            Source::Embedded => EMBEDDED_CLOCK_SLACK,
            Source::Cached | Source::Fetched => 0,
        };
        let mut trial = state.clone();
        match trial.admit(&candidate.manifest, &candidate.sha256, now, slack) {
            Ok(()) => {
                if trial != state {
                    trial.save(&files.state)?;
                }
                return Ok(candidate);
            }
            Err(error) if error.reason == Reason::Expired => {
                stale.get_or_insert(Catalog {
                    stale: true,
                    ..candidate
                });
                last_error = Some(error);
            }
            Err(error) => last_error = Some(error),
        }
    }
    if let Some(stale) = stale {
        return Ok(stale);
    }
    Err(last_error.unwrap_or_else(|| {
        ComponentError::new(
            Reason::NotFound,
            "No component manifest is available yet. Use Check for updates.",
        )
    }))
}

/// Read a small file over HTTPS into memory.
pub fn fetch_small(
    transport: &dyn Transport,
    url: &str,
    cancel: &Arc<AtomicBool>,
) -> Result<Vec<u8>, ComponentError> {
    let body = transport.open(url, 0, cancel)?;
    if body.length.is_some_and(|n| n > MAX_SMALL) {
        return Err(ComponentError::new(
            Reason::SizeMismatch,
            "Manifest download is too large",
        ));
    }
    let mut bytes = Vec::new();
    body.reader
        .take(MAX_SMALL + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| {
            ComponentError::new(Reason::Network, "The manifest download was interrupted")
        })?;
    if bytes.len() as u64 > MAX_SMALL {
        return Err(ComponentError::new(
            Reason::SizeMismatch,
            "Manifest download is too large",
        ));
    }
    Ok(bytes)
}

/// Fetch manifest, signature and certificate together. The manifest is trusted all or
/// nothing. The certificate is judged on its own against the certificate in force (root +
/// stored update): an acceptable, strictly newer update is persisted even when the manifest
/// next to it is refused, so a revocation-only emergency update always takes effect. An older
/// certificate never replaces a newer stored one (that would undo a subkey revocation).
pub fn fetch_and_admit(
    root: &TrustRoot,
    files: &Files,
    transport: &dyn Transport,
    base: &str,
    now: u64,
) -> Result<Catalog, ComponentError> {
    let cancel = Arc::new(AtomicBool::new(false));
    let manifest = fetch_small(transport, &format!("{base}{MANIFEST_FILE}"), &cancel)?;
    let signature = fetch_small(transport, &format!("{base}{SIGNATURE_FILE}"), &cancel)?;
    let fetched_cert = fetch_small(transport, &format!("{base}{CERT_FILE}"), &cancel)?;
    let mut state = RollbackState::load(&files.state);
    let revoked = |state: &mut RollbackState, error: ComponentError| {
        if error.reason == Reason::PrimaryRevoked {
            state.primary_revoked = true;
            let _ = state.save(&files.state);
        }
        error
    };
    let stored = fs::read(&files.cert).ok();
    let current = effective_cert(root, stored.as_deref()).map_err(|e| revoked(&mut state, e))?;
    let cert = match trust::accept_update(&current, &fetched_cert) {
        Ok(Some(merged)) => {
            persist_cert(files, stored.as_deref(), &merged)?;
            merged
        }
        Ok(None) => current,
        Err(error) => return Err(revoked(&mut state, error)),
    };
    let manifest_parsed = verify_with(&cert, &manifest, &signature)?;
    let sha = digest(&manifest);
    state.admit(&manifest_parsed, &sha, now, 0)?;
    write_atomic(&files.manifest, &manifest)?;
    write_atomic(&files.signature, &signature)?;
    state.save(&files.state)?;
    debug_assert_eq!(manifest_parsed.channel, CHANNEL);
    Ok(Catalog {
        manifest: manifest_parsed,
        sha256: sha,
        source: Source::Fetched,
        stale: false,
    })
}

/// Store an accepted certificate update (root + every accepted update, merged). Refuses to
/// replace a stored certificate whose newest self-signature is as new or newer.
fn persist_cert(
    files: &Files,
    stored: Option<&[u8]>,
    merged: &sequoia_openpgp::Cert,
) -> Result<(), ComponentError> {
    use sequoia_openpgp::serialize::SerializeInto;
    if let Some(previous) = stored.and_then(|b| trust::parse_cert(b).ok()) {
        if previous.fingerprint() == merged.fingerprint()
            && trust::newest_self_signature(merged) <= trust::newest_self_signature(&previous)
        {
            return Ok(());
        }
    }
    let armored = merged
        .armored()
        .to_vec()
        .map_err(|_| ComponentError::new(Reason::Io, "Could not save the certificate update"))?;
    write_atomic(&files.cert, &armored)
}

/// Manifest location: the release, or an override for development and tests.
pub fn base_url() -> String {
    let allowed = cfg!(debug_assertions)
        || std::env::var(trust::DEV_TRUST_ROOT_ACK_ENV).as_deref() == Ok(trust::DEV_TRUST_ACK);
    match std::env::var(URL_ENV) {
        Ok(url) if allowed && !url.is_empty() => {
            if url.ends_with('/') {
                url
            } else {
                format!("{url}/")
            }
        }
        _ => RELEASE_BASE.into(),
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use crate::components::{
        fetch::{testserver, Https},
        manifest::tests::{component, manifest},
        trust::testkeys::Signer,
    };
    use serde_json::{json, Value};
    use std::time::{Duration, SystemTime};

    pub fn bytes(value: &Value) -> Vec<u8> {
        serde_json::to_vec(value).unwrap()
    }
    fn root(signer: &Signer) -> TrustRoot {
        trust::root_from(&signer.public(), &signer.fingerprint(), true).unwrap()
    }
    fn dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("murmur-catalog-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }
    const NOW: u64 = 1_794_000_000; // 2026-11-06

    fn signer() -> Signer {
        Signer::new(SystemTime::now() - Duration::from_secs(400 * 86_400), 3650)
    }
    fn sign_at(signer: &Signer, data: &[u8], at: u64) -> Vec<u8> {
        signer.sign_at(
            data,
            trust::unix(at),
            sequoia_openpgp::types::HashAlgorithm::SHA512,
        )
    }

    #[test]
    fn rollback_rules() {
        let parse = |v: &Value| Manifest::parse(&bytes(v)).unwrap();
        let mut state = RollbackState::default();
        let five = parse(&manifest(5, vec![component("engine", "engine")]));
        state.admit(&five, "a", NOW, 0).unwrap();
        assert_eq!(
            state
                .admit(
                    &parse(&manifest(4, vec![component("engine", "engine")])),
                    "b",
                    NOW,
                    0
                )
                .unwrap_err()
                .reason,
            Reason::Rollback
        );
        assert_eq!(
            state.admit(&five, "different", NOW, 0).unwrap_err().reason,
            Reason::Rollback,
            "same serial, different bytes"
        );
        state.admit(&five, "a", NOW, 0).unwrap();
        state
            .admit(
                &parse(&manifest(6, vec![component("engine", "engine")])),
                "c",
                NOW,
                0,
            )
            .unwrap();
        let mut expired = manifest(7, vec![component("engine", "engine")]);
        expired["expires_at"] = json!("2026-11-02T00:00:00Z");
        assert_eq!(
            state
                .admit(&parse(&expired), "d", NOW, 0)
                .unwrap_err()
                .reason,
            Reason::Expired
        );
        // Clock earlier than a manifest already seen (issued 2026-11-01).
        assert_eq!(
            state
                .admit(&parse(&manifest(8, vec![])), "e", 1_790_000_000, 0)
                .unwrap_err()
                .reason,
            Reason::ClockError
        );
        let mut fresh = RollbackState::default();
        assert_eq!(
            fresh
                .admit(&five, "a", 1_790_000_000, 0)
                .unwrap_err()
                .reason,
            Reason::ClockError,
            "manifest from the future"
        );
        // Within the slack (the embedded manifest only), a slightly slow clock is tolerated.
        let issued = five.issued_at().unwrap();
        RollbackState::default()
            .admit(&five, "a", issued - 3600, EMBEDDED_CLOCK_SLACK)
            .unwrap();
        // Expiry is judged after rollback: an expired older serial is still a rollback.
        let mut old = manifest(4, vec![]);
        old["expires_at"] = json!("2026-11-02T00:00:00Z");
        assert_eq!(
            state.admit(&parse(&old), "f", NOW, 0).unwrap_err().reason,
            Reason::Rollback
        );
        fresh.primary_revoked = true;
        assert_eq!(
            fresh.admit(&five, "a", NOW, 0).unwrap_err().reason,
            Reason::PrimaryRevoked
        );
    }

    #[test]
    fn offline_load_verifies_and_prefers_higher_serial() {
        let signer = signer();
        let root = root(&signer);
        let d = dir("offline");
        let files = Files::new(&d);
        let embedded = bytes(&manifest(3, vec![component("engine", "engine")]));
        let embedded_sig = sign_at(&signer, &embedded, NOW - 86_400);
        let catalog = load_offline(&root, &files, Some((&embedded, &embedded_sig)), NOW).unwrap();
        assert_eq!(
            (catalog.source, catalog.manifest.serial),
            (Source::Embedded, 3)
        );
        // A tampered embedded manifest is not trusted.
        let mut tampered = embedded.clone();
        let pos = tampered.iter().position(|b| *b == b'3').unwrap();
        tampered[pos] = b'4';
        assert_eq!(
            load_offline(&root, &files, Some((&tampered, &embedded_sig)), NOW)
                .unwrap_err()
                .reason,
            Reason::SignatureInvalid
        );
        // A cached newer manifest wins; then an embedded older one is not a rollback error.
        let cached = bytes(&manifest(9, vec![component("engine", "engine")]));
        write_atomic(&files.manifest, &cached).unwrap();
        write_atomic(&files.signature, &sign_at(&signer, &cached, NOW - 3600)).unwrap();
        let catalog = load_offline(&root, &files, Some((&embedded, &embedded_sig)), NOW).unwrap();
        assert_eq!(
            (catalog.source, catalog.manifest.serial),
            (Source::Cached, 9)
        );
        // Signed by a stranger: refused, nothing trusted.
        let stranger = Signer::new(SystemTime::now() - Duration::from_secs(86_400), 30);
        let other = bytes(&manifest(10, vec![]));
        assert!(verify(&root, None, &other, &sign_at(&stranger, &other, NOW)).is_err());
        let _ = fs::remove_dir_all(d);
    }

    #[test]
    fn fetch_is_all_or_nothing() {
        use std::{
            net::TcpStream,
            sync::{Arc, Mutex},
        };
        let signer = signer();
        let root = root(&signer);
        let d = dir("fetch");
        let files = Files::new(&d);
        let served: Arc<Mutex<BTreeMap<String, Vec<u8>>>> = Arc::default();
        let content = served.clone();
        let server = testserver::start(Arc::new(
            move |request: &testserver::Request, stream: &mut TcpStream| match content
                .lock()
                .unwrap()
                .get(request.path.trim_start_matches('/'))
            {
                Some(body) => testserver::respond(stream, body, None, None),
                None => {
                    use std::io::Write;
                    let _ =
                        stream.write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n");
                }
            },
        ));
        let base = format!("{}/", server.base);
        let transport = Https::new(Duration::from_secs(2)).unwrap();
        let publish = |serial: u64, signature: Option<Vec<u8>>| {
            let m = bytes(&manifest(serial, vec![component("engine", "engine")]));
            let s = signature.unwrap_or_else(|| sign_at(&signer, &m, NOW - 60));
            let mut map = served.lock().unwrap();
            map.insert(MANIFEST_FILE.into(), m);
            map.insert(SIGNATURE_FILE.into(), s);
            map.insert(CERT_FILE.into(), signer.public());
        };
        // Missing certificate: nothing trusted, nothing written.
        publish(4, None);
        served.lock().unwrap().remove(CERT_FILE);
        assert!(fetch_and_admit(&root, &files, &transport, &base, NOW).is_err());
        assert!(!files.manifest.exists() && !files.state.exists());
        publish(4, None);
        let catalog = fetch_and_admit(&root, &files, &transport, &base, NOW).unwrap();
        assert_eq!(catalog.manifest.serial, 4);
        assert!(files.manifest.exists());
        // Bad signature: refused; the cached manifest is untouched.
        publish(5, Some(b"garbage".to_vec()));
        assert!(fetch_and_admit(&root, &files, &transport, &base, NOW).is_err());
        assert_eq!(
            Manifest::parse(&fs::read(&files.manifest).unwrap())
                .unwrap()
                .serial,
            4
        );
        // Rollback to serial 3 is refused.
        publish(3, None);
        assert_eq!(
            fetch_and_admit(&root, &files, &transport, &base, NOW)
                .unwrap_err()
                .reason,
            Reason::Rollback
        );
        let _ = fs::remove_dir_all(d);
    }

    #[test]
    fn revoked_primary_disables_installs_permanently() {
        let mut signer = signer();
        let root = root(&signer);
        let d = dir("revoked");
        let files = Files::new(&d);
        let m = bytes(&manifest(2, vec![component("engine", "engine")]));
        let sig = sign_at(&signer, &m, NOW - 60);
        signer.revoke_primary();
        // The revocation arrives as a certificate update next to a validly signed manifest.
        write_atomic(&files.cert, &signer.public()).unwrap();
        write_atomic(&files.manifest, &m).unwrap();
        write_atomic(&files.signature, &sig).unwrap();
        assert_eq!(
            load_offline(&root, &files, None, NOW).unwrap_err().reason,
            Reason::PrimaryRevoked
        );
        // Even after the revoked certificate disappears, the state keeps refusing.
        fs::remove_file(&files.cert).unwrap();
        assert_eq!(
            load_offline(&root, &files, None, NOW).unwrap_err().reason,
            Reason::PrimaryRevoked
        );
        let _ = fs::remove_dir_all(d);
    }

    type Served = Arc<std::sync::Mutex<BTreeMap<String, Vec<u8>>>>;
    /// A server that answers from a mutable map; 404 for anything else.
    fn serve() -> (testserver::Server, Served) {
        use std::net::TcpStream;
        let served: Served = Arc::default();
        let content = served.clone();
        let server = testserver::start(Arc::new(
            move |request: &testserver::Request, stream: &mut TcpStream| match content
                .lock()
                .unwrap()
                .get(request.path.trim_start_matches('/'))
            {
                Some(body) => testserver::respond(stream, body, None, None),
                None => {
                    use std::io::Write;
                    let _ =
                        stream.write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n");
                }
            },
        ));
        (server, served)
    }

    #[test]
    fn embedded_manifest_tolerates_slow_clock_but_cached_does_not() {
        let signer = signer();
        let root = root(&signer);
        let d = dir("slack");
        let files = Files::new(&d);
        let m = bytes(&manifest(3, vec![component("engine", "engine")]));
        let issued = Manifest::parse(&m).unwrap().issued_at().unwrap();
        let sig = sign_at(&signer, &m, issued);
        // First run with the clock an hour behind the build: embedded manifest accepted, and
        // still accepted on the next start with the same clock.
        for _ in 0..2 {
            let catalog = load_offline(&root, &files, Some((&m, &sig)), issued - 3600).unwrap();
            assert_eq!((catalog.source, catalog.stale), (Source::Embedded, false));
        }
        // Two days behind is a clock error even for the embedded manifest.
        let _ = fs::remove_file(&files.state);
        assert_eq!(
            load_offline(&root, &files, Some((&m, &sig)), issued - 2 * 86_400)
                .unwrap_err()
                .reason,
            Reason::ClockError
        );
        // The same manifest as a cached (fetched) copy keeps the strict rule.
        let _ = fs::remove_file(&files.state);
        write_atomic(&files.manifest, &m).unwrap();
        write_atomic(&files.signature, &sig).unwrap();
        assert_eq!(
            load_offline(&root, &files, None, issued - 3600)
                .unwrap_err()
                .reason,
            Reason::ClockError
        );
        let _ = fs::remove_dir_all(d);
    }

    #[test]
    fn expired_manifest_loads_as_stale_catalog() {
        let signer = signer();
        let root = root(&signer);
        let d = dir("stale");
        let files = Files::new(&d);
        let m = bytes(&manifest(3, vec![component("engine", "engine")]));
        let sig = sign_at(&signer, &m, NOW - 86_400);
        let expires = Manifest::parse(&m).unwrap().expires_at().unwrap();
        let fresh = load_offline(&root, &files, Some((&m, &sig)), NOW).unwrap();
        assert!(!fresh.stale && !fresh.is_stale(NOW));
        assert!(fresh.is_stale(expires + 1), "goes stale while running");
        let stale = load_offline(&root, &files, Some((&m, &sig)), expires + 1).unwrap();
        assert!(stale.stale && stale.manifest.serial == 3);
        // Stale is still subject to rollback: an expired manifest older than one already
        // seen is refused outright.
        let mut state = RollbackState::load(&files.state);
        state.channels.insert(
            CHANNEL.into(),
            Seen {
                serial: 9,
                sha256: "x".into(),
            },
        );
        state.save(&files.state).unwrap();
        assert_eq!(
            load_offline(&root, &files, Some((&m, &sig)), expires + 1)
                .unwrap_err()
                .reason,
            Reason::Rollback
        );
        let _ = fs::remove_dir_all(d);
    }

    #[test]
    fn older_certificate_cannot_undo_a_subkey_revocation() {
        let day = Duration::from_secs(86_400);
        let mut signer = signer();
        let root = root(&signer);
        let a = signer.subkeys()[0].clone();
        // C1: newer than the root (an extra subkey), subkey A still valid.
        signer.add_signing_subkey_at(3650, SystemTime::now() - 30 * day);
        let c1 = signer.public();
        // C2: A revoked (leaked), B added.
        signer.revoke_subkey_at(&a, SystemTime::now() - 20 * day);
        let b = signer.add_signing_subkey_at(3650, SystemTime::now() - 20 * day);
        let c2 = signer.public();
        let d = dir("cert-rollback");
        let files = Files::new(&d);
        let (server, served) = serve();
        let base = format!("{}/", server.base);
        let transport = Https::new(Duration::from_secs(2)).unwrap();
        let publish = |serial: u64, by: &sequoia_openpgp::Fingerprint, cert: &[u8]| {
            let m = bytes(&manifest(serial, vec![component("engine", "engine")]));
            let s = signer.sign_by(by, &m, trust::unix(NOW - 60));
            let mut map = served.lock().unwrap();
            map.insert(MANIFEST_FILE.into(), m);
            map.insert(SIGNATURE_FILE.into(), s);
            map.insert(CERT_FILE.into(), cert.to_vec());
        };
        // The owner's C2 with a manifest signed by B: accepted and stored.
        publish(4, &b, &c2);
        fetch_and_admit(&root, &files, &transport, &base, NOW).unwrap();
        let stored = fs::read(&files.cert).unwrap();
        // Attacker: older C1 (newer than the root, no revocation) + higher serial signed by A.
        publish(5, &a, &c1);
        let error = fetch_and_admit(&root, &files, &transport, &base, NOW).unwrap_err();
        assert_eq!(error.reason, Reason::SignatureInvalid, "{error:?}");
        assert_eq!(fs::read(&files.cert).unwrap(), stored, "C2 kept");
        assert_eq!(
            Manifest::parse(&fs::read(&files.manifest).unwrap())
                .unwrap()
                .serial,
            4
        );
        // Offline, A stays revoked too: a manifest it signed is not trusted.
        let m = bytes(&manifest(6, vec![]));
        let s = signer.sign_by(&a, &m, trust::unix(NOW - 60));
        assert!(load_offline(&root, &files, Some((&m, &s)), NOW)
            .is_ok_and(|c| c.manifest.serial == 4 && c.source == Source::Cached));
        assert!(verify(&root, Some(&stored), &m, &s).is_err());
        let _ = fs::remove_dir_all(d);
    }

    #[test]
    fn revocation_only_certificate_update_takes_effect() {
        let mut signer = signer();
        let root = root(&signer);
        let d = dir("revoke-only");
        let files = Files::new(&d);
        let (server, served) = serve();
        let base = format!("{}/", server.base);
        let transport = Https::new(Duration::from_secs(2)).unwrap();
        let m = bytes(&manifest(4, vec![component("engine", "engine")]));
        let s = sign_at(&signer, &m, NOW - 60);
        let publish = |m: &[u8], s: &[u8], cert: Vec<u8>| {
            let mut map = served.lock().unwrap();
            map.insert(MANIFEST_FILE.into(), m.to_vec());
            map.insert(SIGNATURE_FILE.into(), s.to_vec());
            map.insert(CERT_FILE.into(), cert);
        };
        publish(&m, &s, signer.public());
        fetch_and_admit(&root, &files, &transport, &base, NOW).unwrap();
        // Emergency: the sole signing subkey is revoked; nothing new can be signed yet.
        signer.revoke_subkey();
        publish(&m, &s, signer.public());
        assert!(fetch_and_admit(&root, &files, &transport, &base, NOW).is_err());
        // The revocation was stored anyway, so the cached manifest it signed is refused.
        assert!(files.cert.exists());
        assert!(load_offline(&root, &files, None, NOW).is_err());
        let _ = fs::remove_dir_all(d);
    }

    #[test]
    fn stored_update_covers_manifests_issued_before_its_new_subkey() {
        let mut signer = signer();
        let root = root(&signer);
        let d = dir("new-subkey");
        let files = Files::new(&d);
        let a = signer.subkeys()[0].clone();
        // Cached manifest signed by A; then a certificate update binds B after it was issued.
        let m = bytes(&manifest(4, vec![component("engine", "engine")]));
        write_atomic(&files.manifest, &m).unwrap();
        write_atomic(
            &files.signature,
            &signer.sign_by(&a, &m, trust::unix(NOW - 60)),
        )
        .unwrap();
        signer.add_signing_subkey(365);
        write_atomic(&files.cert, &signer.public()).unwrap();
        let catalog = load_offline(&root, &files, None, NOW).unwrap();
        assert_eq!(
            (catalog.source, catalog.manifest.serial),
            (Source::Cached, 4)
        );
        let _ = fs::remove_dir_all(d);
    }
}
