//! OpenPGP trust for the component manifest. One primary key, pinned by full fingerprint,
//! certifies an expiring signing subkey; the subkey signs the manifest bytes (detached).
//! Policy: Sequoia's `StandardPolicy`, plus SHA-1 and every DSA size rejected outright.
use super::{ComponentError, Reason};
use sequoia_openpgp::{
    cert::prelude::*,
    parse::{
        stream::{
            DetachedVerifierBuilder, MessageLayer, MessageStructure, VerificationHelper,
        },
        Parse,
    },
    policy::{AsymmetricAlgorithm, StandardPolicy},
    types::{HashAlgorithm, PublicKeyAlgorithm, RevocationStatus},
    Cert, Fingerprint, KeyHandle,
};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

include!(concat!(env!("OUT_DIR"), "/embedded_trust.rs"));

/// Full fingerprint of Murmur's offline primary release key. Empty until the owner creates it
/// (docs/signing.md); until then release builds refuse every install ("release trust root
/// missing") while the app itself keeps working.
pub const RELEASE_PRIMARY_FINGERPRINT: &str = "";

/// Debug builds: path to an armored certificate used as the trust root instead of the pinned
/// one. Release builds honour it only together with `DEV_TRUST_ROOT_ACK_ENV` = `DEV_TRUST_ACK`.
pub const DEV_TRUST_ROOT_ENV: &str = "MURMUR_DEV_TRUST_ROOT";
pub const DEV_TRUST_ROOT_ACK_ENV: &str = "MURMUR_DEV_TRUST_ROOT_ACK";
pub const DEV_TRUST_ACK: &str = "I-understand-this-disables-release-trust";

pub fn policy() -> StandardPolicy<'static> {
    let mut policy = StandardPolicy::new();
    policy.reject_hash(HashAlgorithm::SHA1);
    for algorithm in [
        AsymmetricAlgorithm::DSA1024,
        AsymmetricAlgorithm::DSA2048,
        AsymmetricAlgorithm::DSA3072,
        AsymmetricAlgorithm::DSA4096,
    ] {
        policy.reject_asymmetric_algo(algorithm);
    }
    policy
}

fn untrusted(message: impl Into<String>) -> ComponentError {
    ComponentError::new(Reason::SignatureInvalid, message)
}

pub fn unix(seconds: u64) -> SystemTime {
    UNIX_EPOCH + Duration::from_secs(seconds)
}

#[derive(Debug, Clone)]
pub struct TrustRoot {
    pub cert: Cert,
    /// A development root (not the pinned release key) is in use.
    pub development: bool,
}

/// Parse one public certificate; refuse secret material and keyrings.
pub fn parse_cert(bytes: &[u8]) -> Result<Cert, ComponentError> {
    let cert = Cert::from_bytes(bytes).map_err(|_| untrusted("The signing certificate is unreadable"))?;
    if cert.is_tsk() {
        return Err(untrusted("A signing certificate must not contain secret keys"));
    }
    Ok(cert)
}

/// Accept `bytes` as the trust root only if its primary fingerprint is `expected`.
pub fn root_from(bytes: &[u8], expected: &str, development: bool) -> Result<TrustRoot, ComponentError> {
    let missing = || ComponentError::new(Reason::TrustRootMissing, "Release trust root missing");
    let expected: Fingerprint = expected.parse().map_err(|_| missing())?;
    let cert = parse_cert(bytes).map_err(|_| missing())?;
    if cert.fingerprint() != expected {
        return Err(missing());
    }
    Ok(TrustRoot { cert, development })
}

/// The trust root for this process: the pinned release key, or a development root when allowed.
pub fn load_root() -> Result<TrustRoot, ComponentError> {
    if let Some(path) = std::env::var_os(DEV_TRUST_ROOT_ENV).filter(|p| !p.is_empty()) {
        let acknowledged = std::env::var(DEV_TRUST_ROOT_ACK_ENV).as_deref() == Ok(DEV_TRUST_ACK);
        if cfg!(debug_assertions) || acknowledged {
            tracing::warn!(
                target: "murmur::components",
                "DEVELOPMENT TRUST ROOT IN USE: component signatures are checked against {} instead of Murmur's release key",
                std::path::Path::new(&path).display()
            );
            let bytes = std::fs::read(&path).map_err(|_| {
                ComponentError::new(Reason::TrustRootMissing, "Development trust root is unreadable")
            })?;
            let cert = parse_cert(&bytes)?;
            let fingerprint = cert.fingerprint().to_hex();
            return root_from(&bytes, &fingerprint, true);
        }
        tracing::warn!(target: "murmur::components", "{DEV_TRUST_ROOT_ENV} ignored: release builds require {DEV_TRUST_ROOT_ACK_ENV}");
    }
    match RELEASE_CERT {
        Some(bytes) if !RELEASE_PRIMARY_FINGERPRINT.is_empty() => {
            root_from(bytes, RELEASE_PRIMARY_FINGERPRINT, false)
        }
        _ => Err(ComponentError::new(Reason::TrustRootMissing, "Release trust root missing")),
    }
}

/// Newest self-signature (binding, direct-key or revocation) made by the primary key.
pub fn newest_self_signature(cert: &Cert) -> SystemTime {
    let primary = cert.primary_key();
    let times = primary
        .self_signatures()
        .chain(primary.self_revocations())
        .map(|s| s.signature_creation_time())
        .chain(cert.userids().flat_map(|u| {
            u.self_signatures()
                .chain(u.self_revocations())
                .map(|s| s.signature_creation_time())
                .collect::<Vec<_>>()
        }))
        .chain(cert.keys().subkeys().flat_map(|k| {
            k.self_signatures()
                .chain(k.self_revocations())
                .map(|s| s.signature_creation_time())
                .collect::<Vec<_>>()
        }));
    times.flatten().max().unwrap_or(UNIX_EPOCH)
}

fn primary_revoked(cert: &Cert) -> bool {
    matches!(cert.revocation_status(&policy(), None), RevocationStatus::Revoked(_))
}

fn revoked_error() -> ComponentError {
    ComponentError::new(
        Reason::PrimaryRevoked,
        "Murmur's release key has been revoked; component installs are disabled. Update Murmur from its website.",
    )
}

/// A usable signing subkey at `at`: bound with back-signature, alive, not revoked.
#[allow(deprecated)] // DSA is named only to reject it.
fn signing_subkey_at(cert: &Cert, at: SystemTime) -> bool {
    let policy = policy();
    cert.keys()
        .subkeys()
        .with_policy(&policy, at)
        .for_signing()
        .alive()
        .revoked(false)
        .any(|key| key.key().pk_algo() != PublicKeyAlgorithm::DSA)
}

/// The certificate update rule. `Ok(None)` keeps `current` (candidate not strictly newer).
pub fn accept_update(current: &Cert, candidate: &[u8], issued_at: SystemTime) -> Result<Option<Cert>, ComponentError> {
    let candidate = parse_cert(candidate)?;
    if candidate.fingerprint() != current.fingerprint() {
        return Err(ComponentError::new(Reason::UntrustedKey, "The downloaded certificate belongs to another key"));
    }
    // A valid primary revocation is honoured whether or not the rest of the update is.
    if primary_revoked(&candidate) {
        return Err(revoked_error());
    }
    if newest_self_signature(&candidate) <= newest_self_signature(current) {
        return Ok(None);
    }
    if candidate.with_policy(&policy(), None).is_err() {
        return Err(untrusted("The downloaded certificate is not valid under Murmur's policy"));
    }
    if !signing_subkey_at(&candidate, issued_at) {
        return Err(untrusted("The downloaded certificate has no signing key valid for this manifest"));
    }
    let merged = current
        .clone()
        .merge_public(candidate)
        .map_err(|_| untrusted("Could not merge the certificate update"))?;
    Ok(Some(merged))
}

struct Helper<'a> {
    cert: &'a Cert,
    issued_at: SystemTime,
}

impl VerificationHelper for Helper<'_> {
    fn get_certs(&mut self, _: &[KeyHandle]) -> sequoia_openpgp::Result<Vec<Cert>> {
        Ok(vec![self.cert.clone()])
    }
    #[allow(deprecated)] // DSA is named only to reject it.
    fn check(&mut self, structure: MessageStructure) -> sequoia_openpgp::Result<()> {
        let policy = policy();
        let mut failure = None;
        for layer in structure {
            let MessageLayer::SignatureGroup { results } = layer else {
                return Err(anyhow::anyhow!("unexpected message structure"));
            };
            for result in results {
                let good = match result {
                    Ok(good) => good,
                    Err(error) => {
                        failure.get_or_insert_with(|| error.to_string());
                        continue;
                    }
                };
                let key = good.ka.key();
                if key.fingerprint() == self.cert.fingerprint() {
                    failure.get_or_insert_with(|| "signed by the primary key, not the signing subkey".into());
                    continue;
                }
                if key.pk_algo() == PublicKeyAlgorithm::DSA || good.sig.hash_algo() == HashAlgorithm::SHA1 {
                    failure.get_or_insert_with(|| "weak algorithm".into());
                    continue;
                }
                let subkey = self.cert.keys().subkeys().key_handle(key.fingerprint()).next();
                let valid_at_issue = self
                    .cert
                    .keys()
                    .subkeys()
                    .with_policy(&policy, self.issued_at)
                    .key_handle(key.fingerprint())
                    .for_signing()
                    .alive()
                    .revoked(false)
                    .next()
                    .is_some();
                let revoked_now = subkey.is_some_and(|k| {
                    k.self_revocations().next().is_some()
                        && k.with_policy(&policy, None)
                            .map_or(true, |k| !matches!(k.revocation_status(), RevocationStatus::NotAsFarAsWeKnow))
                });
                if !valid_at_issue {
                    failure.get_or_insert_with(|| "the signing key was not valid when the manifest was issued".into());
                } else if revoked_now {
                    failure.get_or_insert_with(|| "the signing key has been revoked".into());
                } else {
                    return Ok(());
                }
            }
        }
        Err(anyhow::anyhow!(failure.unwrap_or_else(|| "no signature".into())))
    }
}

/// Verify a detached signature over exactly `data`. `issued_at` is the manifest's issue time.
pub fn verify(cert: &Cert, data: &[u8], signature: &[u8], issued_at: SystemTime) -> Result<(), ComponentError> {
    if primary_revoked(cert) {
        return Err(revoked_error());
    }
    let policy = policy();
    if cert.with_policy(&policy, None).is_err() {
        return Err(untrusted("Murmur's release certificate is not valid under the signature policy"));
    }
    let helper = Helper { cert, issued_at };
    let mut verifier = DetachedVerifierBuilder::from_bytes(signature)
        .map_err(|_| untrusted("The manifest signature is unreadable"))?
        .with_policy(&policy, None, helper)
        .map_err(|_| untrusted("The manifest signature could not be checked"))?;
    verifier.verify_bytes(data).map_err(|error| {
        // Sequoia's message names the policy rule; no secrets or contents are in it.
        let detail = error.to_string();
        let detail: String = detail.chars().take(200).collect();
        untrusted(format!("The component manifest signature is not valid ({detail})"))
    })
}

#[cfg(test)]
pub mod testkeys {
    //! Throwaway keys generated per test run. Nothing here is ever written to the repository.
    use super::*;
    use sequoia_openpgp::{
        packet::signature::SignatureBuilder,
        serialize::SerializeInto,
        types::{KeyFlags, ReasonForRevocation, SignatureType},
    };

    pub struct Signer {
        pub cert: Cert,
    }

    impl Signer {
        /// Certify-only primary, one signing subkey valid for `subkey_days` from `created`.
        pub fn new(created: SystemTime, subkey_days: u64) -> Self {
            let (cert, _) = CertBuilder::new()
                .add_userid("Murmur test release <test@invalid>")
                .set_creation_time(created)
                .set_cipher_suite(CipherSuite::Cv25519)
                .set_primary_key_flags(KeyFlags::empty().set_certification())
                .add_subkey(
                    KeyFlags::empty().set_signing(),
                    Duration::from_secs(subkey_days * 86_400),
                    None,
                )
                .generate()
                .unwrap();
            Self { cert }
        }
        pub fn public(&self) -> Vec<u8> {
            self.cert.armored().to_vec().unwrap()
        }
        pub fn fingerprint(&self) -> String {
            self.cert.fingerprint().to_hex()
        }
        fn subkey_pair(&self) -> sequoia_openpgp::crypto::KeyPair {
            self.cert
                .keys()
                .subkeys()
                .secret()
                .next()
                .unwrap()
                .key()
                .clone()
                .into_keypair()
                .unwrap()
        }
        fn primary_pair(&self) -> sequoia_openpgp::crypto::KeyPair {
            self.cert.primary_key().key().clone().parts_into_secret().unwrap().into_keypair().unwrap()
        }
        pub fn sign_at(&self, data: &[u8], at: SystemTime, hash: HashAlgorithm) -> Vec<u8> {
            let mut pair = self.subkey_pair();
            let signature = SignatureBuilder::new(SignatureType::Binary)
                .set_signature_creation_time(at)
                .unwrap()
                .set_hash_algo(hash)
                .sign_message(&mut pair, data)
                .unwrap();
            sequoia_openpgp::Packet::from(signature).to_vec().unwrap()
        }
        pub fn sign(&self, data: &[u8]) -> Vec<u8> {
            self.sign_at(data, SystemTime::now(), HashAlgorithm::SHA512)
        }
        pub fn sign_with_primary(&self, data: &[u8]) -> Vec<u8> {
            let mut pair = self.primary_pair();
            let signature = SignatureBuilder::new(SignatureType::Binary).sign_message(&mut pair, data).unwrap();
            sequoia_openpgp::Packet::from(signature).to_vec().unwrap()
        }
        pub fn revoke_subkey(&mut self) {
            let mut pair = self.primary_pair();
            let subkey = self.cert.keys().subkeys().next().unwrap().key().clone();
            let revocation = SubkeyRevocationBuilder::new()
                .set_reason_for_revocation(ReasonForRevocation::KeyCompromised, b"test")
                .unwrap()
                .build(&mut pair, &self.cert, &subkey, None)
                .unwrap();
            self.cert = self.cert.clone().insert_packets(revocation).unwrap().0;
        }
        pub fn revoke_primary(&mut self) {
            let mut pair = self.primary_pair();
            let revocation = self.cert.revoke(&mut pair, ReasonForRevocation::KeyCompromised, b"test").unwrap();
            self.cert = self.cert.clone().insert_packets(revocation).unwrap().0;
        }
        /// Re-certify: a fresh signing subkey bound now (a strictly newer certificate).
        pub fn add_signing_subkey(&mut self, days: u64) {
            use sequoia_openpgp::packet::key::Key4;
            let mut primary = self.primary_pair();
            let subkey: sequoia_openpgp::packet::Key<_, sequoia_openpgp::packet::key::SubordinateRole> =
                Key4::generate_ecc(true, sequoia_openpgp::types::Curve::Ed25519).unwrap().into();
            let mut subkey_pair = subkey.clone().into_keypair().unwrap();
            let backsig = SignatureBuilder::new(SignatureType::PrimaryKeyBinding)
                .sign_primary_key_binding(&mut subkey_pair, self.cert.primary_key().key(), &subkey)
                .unwrap();
            let binding = SignatureBuilder::new(SignatureType::SubkeyBinding)
                .set_key_flags(KeyFlags::empty().set_signing())
                .unwrap()
                .set_key_validity_period(Duration::from_secs(days * 86_400))
                .unwrap()
                .set_embedded_signature(backsig)
                .unwrap()
                .sign_subkey_binding(&mut primary, None, &subkey)
                .unwrap();
            self.cert = self
                .cert
                .clone()
                .insert_packets(vec![sequoia_openpgp::Packet::from(subkey), binding.into()])
                .unwrap()
                .0;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{testkeys::Signer, *};

    fn now() -> SystemTime {
        SystemTime::now()
    }
    fn days(n: u64) -> Duration {
        Duration::from_secs(n * 86_400)
    }
    const DATA: &[u8] = b"{\"schema_version\":1}";

    #[test]
    fn good_signature_verifies_and_tampering_fails() {
        let signer = Signer::new(now() - days(10), 365);
        let sig = signer.sign(DATA);
        verify(&signer.cert, DATA, &sig, now()).unwrap();
        // Armored signatures, as produced by `gpg --detach-sign --armor`, are accepted too.
        let mut armored = Vec::new();
        {
            use sequoia_openpgp::armor::{Kind, Writer};
            use std::io::Write;
            let mut writer = Writer::new(&mut armored, Kind::Signature).unwrap();
            writer.write_all(&sig).unwrap();
            writer.finalize().unwrap();
        }
        verify(&signer.cert, DATA, &armored, now()).unwrap();
        assert_eq!(verify(&signer.cert, b"{\"schema_version\":2}", &sig, now()).unwrap_err().reason, Reason::SignatureInvalid);
        let mut bad = sig.clone();
        let last = bad.len() - 3;
        bad[last] ^= 0x40;
        assert!(verify(&signer.cert, DATA, &bad, now()).is_err());
        assert!(verify(&signer.cert, DATA, b"not a signature", now()).is_err());
    }

    #[test]
    fn unknown_key_and_primary_signature_rejected() {
        let signer = Signer::new(now() - days(1), 30);
        let stranger = Signer::new(now() - days(1), 30);
        assert!(verify(&signer.cert, DATA, &stranger.sign(DATA), now()).is_err());
        let error = verify(&signer.cert, DATA, &signer.sign_with_primary(DATA), now()).unwrap_err();
        assert!(error.message.contains("not valid"), "{error:?}");
    }

    #[test]
    fn expired_subkey_rejected() {
        // Subkey valid for one day from ten days ago; the manifest was issued now.
        let signer = Signer::new(now() - days(10), 1);
        let sig = signer.sign_at(DATA, now() - days(10) + Duration::from_secs(3600), HashAlgorithm::SHA512);
        assert!(verify(&signer.cert, DATA, &sig, now()).is_err(), "issued after expiry");
        // Same signature, manifest claimed issued while the key was valid: accepted.
        verify(&signer.cert, DATA, &sig, now() - days(10) + Duration::from_secs(7200)).unwrap();
    }

    #[test]
    fn revoked_subkey_and_primary_rejected() {
        let mut signer = Signer::new(now() - days(5), 365);
        let sig = signer.sign_at(DATA, now() - days(1), HashAlgorithm::SHA512);
        signer.revoke_subkey();
        assert!(verify(&signer.cert, DATA, &sig, now() - days(1)).is_err());
        let mut signer = Signer::new(now() - days(5), 365);
        let sig = signer.sign(DATA);
        signer.revoke_primary();
        assert_eq!(verify(&signer.cert, DATA, &sig, now()).unwrap_err().reason, Reason::PrimaryRevoked);
    }

    #[test]
    fn sha1_signature_rejected() {
        let signer = Signer::new(now() - days(5), 365);
        let sig = signer.sign_at(DATA, now() - Duration::from_secs(60), HashAlgorithm::SHA1);
        assert!(verify(&signer.cert, DATA, &sig, now()).is_err());
        let sig = signer.sign_at(DATA, now() - Duration::from_secs(60), HashAlgorithm::SHA256);
        verify(&signer.cert, DATA, &sig, now()).unwrap();
    }

    #[test]
    fn dsa_key_rejected() {
        use sequoia_openpgp::{
            packet::{key::Key4, signature::SignatureBuilder, UserID},
            types::{KeyFlags, SignatureType},
            Packet,
        };
        let key: sequoia_openpgp::packet::Key<_, sequoia_openpgp::packet::key::PrimaryRole> =
            Key4::generate_dsa(2048).unwrap().into();
        let mut pair = key.clone().into_keypair().unwrap();
        let uid = UserID::from("DSA <dsa@invalid>");
        let binding = SignatureBuilder::new(SignatureType::PositiveCertification)
            .set_key_flags(KeyFlags::empty().set_signing().set_certification())
            .unwrap()
            .sign_userid_binding(&mut pair, key.parts_as_public(), &uid)
            .unwrap();
        let cert = Cert::from_packets(vec![Packet::from(key.clone()), uid.into(), binding.into()].into_iter()).unwrap();
        let sig = SignatureBuilder::new(SignatureType::Binary).sign_message(&mut pair, DATA).unwrap();
        use sequoia_openpgp::serialize::SerializeInto;
        let sig = Packet::from(sig).to_vec().unwrap();
        assert!(verify(&cert.strip_secret_key_material(), DATA, &sig, now()).is_err());
    }

    #[test]
    fn release_root_requires_matching_fingerprint() {
        let signer = Signer::new(now() - days(1), 30);
        assert!(root_from(&signer.public(), &signer.fingerprint(), false).is_ok());
        let other = Signer::new(now() - days(1), 30);
        assert_eq!(root_from(&signer.public(), &other.fingerprint(), false).unwrap_err().reason, Reason::TrustRootMissing);
        assert_eq!(root_from(b"garbage", &signer.fingerprint(), false).unwrap_err().reason, Reason::TrustRootMissing);
        assert_eq!(root_from(&signer.public(), "", false).unwrap_err().reason, Reason::TrustRootMissing);
        // Secret material is never acceptable as a root.
        use sequoia_openpgp::serialize::SerializeInto;
        let secret = signer.cert.as_tsk().armored().to_vec().unwrap();
        assert!(root_from(&secret, &signer.fingerprint(), false).is_err());
        if RELEASE_PRIMARY_FINGERPRINT.is_empty() {
            assert!(std::env::var_os(DEV_TRUST_ROOT_ENV).is_some() || load_root().is_err());
        }
    }

    #[test]
    fn certificate_update_rule() {
        let mut signer = Signer::new(now() - days(30), 365);
        let original = signer.cert.clone().strip_secret_key_material();
        // Same certificate: not strictly newer, keep the current one.
        assert!(accept_update(&original, &signer.public(), now()).unwrap().is_none());
        // Another key's certificate is refused.
        let stranger = Signer::new(now() - days(1), 30);
        assert_eq!(accept_update(&original, &stranger.public(), now()).unwrap_err().reason, Reason::UntrustedKey);
        // A strictly newer certificate with a freshly bound subkey is accepted and merged.
        signer.add_signing_subkey(365);
        let updated = accept_update(&original, &signer.public(), now()).unwrap().expect("newer");
        assert_eq!(updated.keys().subkeys().count(), 2);
        // The older subkey still covers an earlier issue time, so the merged update is usable.
        assert!(accept_update(&original, &signer.public(), now() - days(20)).is_ok());
        // Revoked subkey only: newer, but no usable signing key.
        let mut lone = Signer::new(now() - days(30), 365);
        let base = lone.cert.clone().strip_secret_key_material();
        lone.revoke_subkey();
        assert!(accept_update(&base, &lone.public(), now()).is_err());
        // Revoked primary: refused permanently, regardless of anything else.
        let mut revoked = Signer::new(now() - days(30), 365);
        let base = revoked.cert.clone().strip_secret_key_material();
        revoked.revoke_primary();
        assert_eq!(accept_update(&base, &revoked.public(), now()).unwrap_err().reason, Reason::PrimaryRevoked);
    }
}
