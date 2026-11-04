use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    fs,
    net::IpAddr,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};
use subtle::ConstantTimeEq;

pub fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn random_hex<const N: usize>() -> Result<String> {
    let mut bytes = [0u8; N];
    getrandom::fill(&mut bytes)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
fn hash_token(token: &str) -> [u8; 32] {
    Sha256::digest(token.as_bytes()).into()
}
fn hash_hex(token: &str) -> String {
    hash_token(token)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
fn equal_hash(expected: &str, token: &str) -> bool {
    let actual = hash_hex(token);
    expected.as_bytes().ct_eq(actual.as_bytes()).into()
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Device {
    pub id: String,
    pub name: String,
    pub created_at: u64,
    pub token_hash: String,
    pub last_seen: Option<u64>,
}
#[derive(Clone, Serialize)]
pub struct PublicDevice {
    pub id: String,
    pub name: String,
    pub created_at: u64,
    pub last_seen: Option<u64>,
}
impl From<&Device> for PublicDevice {
    fn from(d: &Device) -> Self {
        Self {
            id: d.id.clone(),
            name: d.name.clone(),
            created_at: d.created_at,
            last_seen: d.last_seen,
        }
    }
}

#[derive(Clone, Serialize)]
pub struct Pairing {
    pub code: String,
    pub expires_at: u64,
}
#[derive(Default)]
pub struct CodeSlot(Option<Pairing>);
impl CodeSlot {
    pub fn regenerate(&mut self, at: u64) -> Result<Pairing> {
        let code = loop {
            let raw = random_hex::<4>()?;
            let number = u32::from_str_radix(&raw, 16)? % 1_000_000;
            let candidate = format!("{number:06}");
            if self.0.as_ref().is_none_or(|old| old.code != candidate) {
                break Pairing {
                    code: candidate,
                    expires_at: at + 300,
                };
            }
        };
        self.0 = Some(code.clone());
        Ok(code)
    }
    pub fn consume(&mut self, candidate: &str, at: u64) -> bool {
        let Some(current) = self.0.as_ref() else {
            return false;
        };
        if at >= current.expires_at {
            self.0 = None;
            return false;
        }
        if candidate.len() != 6 || !candidate.bytes().all(|b| b.is_ascii_digit()) {
            return false;
        }
        let valid: bool = current.code.as_bytes().ct_eq(candidate.as_bytes()).into();
        if valid {
            self.0 = None;
        }
        valid
    }
}

#[derive(Default)]
pub struct RateLimiter {
    failed: HashMap<IpAddr, (u8, u64)>,
}
impl RateLimiter {
    fn key(ip: IpAddr) -> IpAddr {
        match ip {
            IpAddr::V6(v6) => v6.to_ipv4_mapped().map(IpAddr::V4).unwrap_or(ip),
            _ => ip,
        }
    }
    pub fn allowed(&mut self, ip: IpAddr, at: u64) -> bool {
        let ip = Self::key(ip);
        if let Some((count, until)) = self.failed.get(&ip).copied() {
            if count >= 5 {
                if at < until {
                    return false;
                }
                self.failed.remove(&ip);
            }
        }
        true
    }
    pub fn failure(&mut self, ip: IpAddr, at: u64) {
        let ip = Self::key(ip);
        let entry = self.failed.entry(ip).or_insert((0, 0));
        entry.0 += 1;
        if entry.0 >= 5 {
            entry.1 = at + 60;
        }
    }
    pub fn success(&mut self, ip: IpAddr) {
        self.failed.remove(&Self::key(ip));
    }
}

pub struct Auth {
    path: PathBuf,
    devices: Vec<Device>,
    code: CodeSlot,
    limiter: RateLimiter,
}
impl Auth {
    pub fn load(data: &Path) -> Result<Self> {
        let dir = data.join("companion");
        fs::create_dir_all(&dir)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&dir, fs::Permissions::from_mode(0o700))?;
        }
        let path = dir.join("tokens.json");
        if path.exists() {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                if fs::metadata(&path)?.permissions().mode() & 0o077 != 0 {
                    bail!("Companion token store is group/world readable");
                }
            }
        }
        let devices = if path.exists() {
            serde_json::from_slice(&fs::read(&path)?).context("Parse companion tokens")?
        } else {
            Vec::new()
        };
        Ok(Self {
            path,
            devices,
            code: CodeSlot::default(),
            limiter: RateLimiter::default(),
        })
    }
    pub fn show_code(&mut self) -> Result<Pairing> {
        self.code.regenerate(now())
    }
    pub fn list(&self) -> Vec<PublicDevice> {
        self.devices.iter().map(PublicDevice::from).collect()
    }
    pub fn revoke(&mut self, id: &str) -> Result<bool> {
        let mut next = self.devices.clone();
        next.retain(|d| d.id != id);
        if next.len() == self.devices.len() {
            return Ok(false);
        }
        self.persist(&next)?;
        self.devices = next;
        Ok(true)
    }
    pub fn authenticate(
        &mut self,
        ip: IpAddr,
        credential: &str,
        name: &str,
    ) -> Result<std::result::Result<(String, Option<String>), &'static str>> {
        let at = now();
        if !self.limiter.allowed(ip, at) {
            return Ok(Err("rate_limited"));
        }
        if credential.len() == 6
            && credential.bytes().all(|b| b.is_ascii_digit())
            && self.code.consume(credential, at)
        {
            let id = random_hex::<16>()?;
            let token = random_hex::<32>()?;
            let safe_name: String = name
                .chars()
                .filter(|c| c.is_ascii_alphanumeric() || " ._-".contains(*c))
                .take(64)
                .collect();
            self.devices.push(Device {
                id: id.clone(),
                name: safe_name,
                created_at: at,
                token_hash: hash_hex(&token),
                last_seen: Some(at),
            });
            self.save()?;
            self.limiter.success(ip);
            return Ok(Ok((id, Some(token))));
        }
        if credential.len() == 64 && credential.bytes().all(|b| b.is_ascii_hexdigit()) {
            let match_index =
                self.devices
                    .iter()
                    .enumerate()
                    .fold(None, |found, (index, device)| {
                        let matches = equal_hash(&device.token_hash, credential);
                        if matches {
                            Some(index)
                        } else {
                            found
                        }
                    });
            if let Some(index) = match_index {
                let device = &mut self.devices[index];
                device.last_seen = Some(at);
                let id = device.id.clone();
                self.save()?;
                self.limiter.success(ip);
                return Ok(Ok((id, None)));
            }
        }
        self.limiter.failure(ip, at);
        Ok(Err("invalid_credential"))
    }
    fn save(&self) -> Result<()> {
        self.persist(&self.devices)
    }
    fn persist(&self, devices: &[Device]) -> Result<()> {
        use std::io::Write;
        let temp = self
            .path
            .with_extension(format!("{}.tmp", random_hex::<8>()?));
        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&temp)?;
        file.write_all(&serde_json::to_vec_pretty(devices)?)?;
        file.sync_all()?;
        fs::rename(temp, &self.path)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn hash_comparison() {
        assert!(equal_hash(&hash_hex("secret"), "secret"));
        assert!(!equal_hash(&hash_hex("secret"), "wrong"));
    }
    #[test]
    fn code_lifecycle() {
        let mut slot = CodeSlot::default();
        let first = slot.regenerate(100).unwrap();
        assert!(!slot.consume(&first.code, 400));
        let second = slot.regenerate(500).unwrap();
        assert!(slot.consume(&second.code, 501));
        assert!(!slot.consume(&second.code, 502));
        let third = slot.regenerate(600).unwrap();
        slot.regenerate(601).unwrap();
        assert!(!slot.consume(&third.code, 602));
    }
    #[test]
    fn limiter_resets_after_lockout() {
        let mut limiter = RateLimiter::default();
        let ip = "127.0.0.1".parse().unwrap();
        for _ in 0..5 {
            assert!(limiter.allowed(ip, 10));
            limiter.failure(ip, 10);
        }
        assert!(!limiter.allowed(ip, 69));
        assert!(limiter.allowed(ip, 70));
        limiter.failure(ip, 70);
        assert!(limiter.allowed(ip, 70));
        for _ in 0..4 {
            limiter.failure(ip, 71);
        }
        assert!(!limiter.allowed("::ffff:127.0.0.1".parse().unwrap(), 71));
    }
}
