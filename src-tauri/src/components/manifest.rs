//! Signed component manifest (schema 1). Parsing is strict about everything Murmur acts on:
//! identifiers become paths, URLs become requests, sizes become disk checks. Nothing here is
//! trusted until `trust::verify_manifest` has accepted the signature over the exact bytes.
use super::{fetch::allowed_url, ComponentError, Reason};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeSet, HashSet};

pub const SCHEMA_VERSION: u64 = 1;
pub const CHANNEL: &str = "stable";
pub const OS: &str = if cfg!(windows) { "windows" } else { "linux" };
pub const ARCH: &str = "x86_64";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Kind {
    Engine,
    Cuda,
    SpeechModel,
    EnhanceModel,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Archive {
    Zip,
    None,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvenanceType {
    UpstreamSignatureVerified,
    MurmurAttestedOnly,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct License {
    pub name: String,
    pub url: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Provenance {
    #[serde(rename = "type")]
    pub kind: ProvenanceType,
    pub source_url: String,
    pub source_revision: String,
    pub source_sha256: String,
    #[serde(default)]
    pub note: String,
}

/// One file of a multi-file component (`archive: "none"`), stored under `name`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct File {
    pub name: String,
    pub urls: Vec<String>,
    pub size_bytes: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Component {
    pub id: String,
    pub version: String,
    pub os: String,
    pub arch: String,
    pub kind: Kind,
    /// Zip components: download locations of the single archive.
    #[serde(default)]
    pub urls: Vec<String>,
    /// Total download size (the archive, or the sum of `files`).
    pub size_bytes: u64,
    /// Archive digest; absent for `archive: "none"`, whose files carry their own.
    #[serde(default)]
    pub sha256: Option<String>,
    pub installed_size_bytes: u64,
    pub temp_size_bytes: u64,
    pub archive: Archive,
    #[serde(default)]
    pub files: Vec<File>,
    #[serde(default)]
    pub depends: Vec<String>,
    pub license: License,
    pub provenance: Provenance,
    pub display_name: String,
    pub description: String,
    /// Speech model name (`engine.model`) or Enhance catalog ID (`enhance.model`).
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub quality: Option<String>,
    #[serde(default)]
    pub speed: Option<String>,
    #[serde(default)]
    pub memory_mb: Option<u64>,
    #[serde(default)]
    pub languages: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Manifest {
    pub schema_version: u64,
    pub serial: u64,
    pub issued_at: String,
    pub expires_at: String,
    pub channel: String,
    pub components: Vec<Component>,
}

fn invalid(message: impl Into<String>) -> ComponentError {
    ComponentError::new(Reason::ManifestInvalid, message)
}

/// Lowercase ASCII path segment: `[a-z0-9][a-z0-9._-]{0,63}` without `..`.
pub fn safe_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    !value.is_empty()
        && value.len() <= 64
        && bytes[0].is_ascii_alphanumeric()
        && !value.contains("..")
        && bytes.iter().all(|b| {
            b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'.' | b'_' | b'-')
        })
}

/// Version strings become path segments too: `[A-Za-z0-9][A-Za-z0-9._+-]{0,63}`, no `..`.
pub fn safe_version(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && !value.contains("..")
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-' | b'+'))
}

fn safe_file_name(value: &str) -> bool {
    safe_version(value) && value.len() <= 128 && !value.ends_with('.')
}

fn sha256_hex(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn check_urls(urls: &[String], what: &str) -> Result<(), ComponentError> {
    if urls.is_empty() {
        return Err(invalid(format!("{what} has no download URL")));
    }
    for url in urls {
        let parsed =
            reqwest::Url::parse(url).map_err(|_| invalid(format!("{what} has an invalid URL")))?;
        if !allowed_url(&parsed) {
            return Err(invalid(format!(
                "{what} names a host Murmur does not download from"
            )));
        }
    }
    Ok(())
}

impl Component {
    /// `os: "any"` covers platform-neutral files such as speech models.
    pub fn matches_platform(&self) -> bool {
        (self.os == OS || self.os == "any") && self.arch == ARCH
    }
    /// Bytes the download, extraction and staging need at once (before the 100 MiB margin).
    pub fn space_needed(&self) -> u64 {
        self.size_bytes
            .saturating_add(self.installed_size_bytes)
            .saturating_add(self.temp_size_bytes)
    }
    fn validate(&self) -> Result<(), ComponentError> {
        let what = format!("Component {}", self.id);
        if !safe_id(&self.id) || !safe_version(&self.version) {
            return Err(invalid(
                "Component identifier or version is not a safe name",
            ));
        }
        if !matches!(self.os.as_str(), "linux" | "windows" | "any") || self.arch != ARCH {
            return Err(invalid(format!("{what} targets an unknown platform")));
        }
        if self.size_bytes == 0 || self.installed_size_bytes == 0 {
            return Err(invalid(format!("{what} has no size")));
        }
        match self.archive {
            Archive::Zip => {
                check_urls(&self.urls, &what)?;
                if !self.files.is_empty() || !self.sha256.as_deref().is_some_and(sha256_hex) {
                    return Err(invalid(format!(
                        "{what}: a zip needs one SHA-256 and no file list"
                    )));
                }
            }
            Archive::None => {
                if self.files.is_empty() || !self.urls.is_empty() || self.sha256.is_some() {
                    return Err(invalid(format!(
                        "{what}: a file component needs a file list"
                    )));
                }
                let mut names = HashSet::new();
                let mut total: u64 = 0;
                for file in &self.files {
                    if !safe_file_name(&file.name)
                        || !names.insert(file.name.to_ascii_lowercase())
                        || !sha256_hex(&file.sha256)
                        || file.size_bytes == 0
                    {
                        return Err(invalid(format!("{what} has an invalid file entry")));
                    }
                    check_urls(&file.urls, &what)?;
                    total = total.saturating_add(file.size_bytes);
                }
                if total != self.size_bytes {
                    return Err(invalid(format!("{what}: file sizes do not add up")));
                }
            }
        }
        if !sha256_hex(&self.provenance.source_sha256) && !self.provenance.source_sha256.is_empty()
        {
            return Err(invalid(format!("{what} has an invalid provenance digest")));
        }
        match self.kind {
            Kind::SpeechModel => {
                let complete = self.model.is_some()
                    && self.quality.is_some()
                    && self.speed.is_some()
                    && self.memory_mb.is_some()
                    && matches!(self.languages.as_deref(), Some("en" | "multilingual"));
                if !complete || self.archive != Archive::None {
                    return Err(invalid(format!("{what} lacks speech model details")));
                }
            }
            Kind::EnhanceModel if self.model.is_none() || self.archive != Archive::None => {
                return Err(invalid(format!("{what} lacks the Enhance model ID")));
            }
            Kind::Engine | Kind::Cuda if self.archive != Archive::Zip => {
                return Err(invalid(format!("{what} must be a zip archive")));
            }
            _ => {}
        }
        Ok(())
    }
}

impl Manifest {
    /// Parse and structurally validate. Signature checking happens separately and first.
    pub fn parse(bytes: &[u8]) -> Result<Self, ComponentError> {
        #[derive(Deserialize)]
        struct Probe {
            schema_version: u64,
        }
        let probe: Probe = serde_json::from_slice(bytes)
            .map_err(|_| invalid("The component manifest is not valid JSON"))?;
        if probe.schema_version != SCHEMA_VERSION {
            return Err(ComponentError::new(
                Reason::ManifestInvalid,
                format!(
                    "Unsupported component manifest version {}; update Murmur",
                    probe.schema_version
                ),
            ));
        }
        let manifest: Self = serde_json::from_slice(bytes)
            .map_err(|_| invalid("The component manifest does not match schema 1"))?;
        manifest.validate()?;
        Ok(manifest)
    }
    pub fn issued_at(&self) -> Result<u64, ComponentError> {
        parse_rfc3339(&self.issued_at).ok_or_else(|| invalid("Invalid manifest issue time"))
    }
    pub fn expires_at(&self) -> Result<u64, ComponentError> {
        parse_rfc3339(&self.expires_at).ok_or_else(|| invalid("Invalid manifest expiry time"))
    }
    fn validate(&self) -> Result<(), ComponentError> {
        if self.channel != CHANNEL {
            return Err(invalid("The component manifest is for another channel"));
        }
        if self.issued_at()? >= self.expires_at()? {
            return Err(invalid(
                "The component manifest expires before it is issued",
            ));
        }
        let mut keys = BTreeSet::new();
        for component in &self.components {
            component.validate()?;
            if !keys.insert((&component.id, &component.os, &component.arch)) {
                return Err(invalid(format!(
                    "Component {} is listed twice",
                    component.id
                )));
            }
        }
        let any = "any".to_owned();
        for component in &self.components {
            for dependency in &component.depends {
                if !keys.contains(&(dependency, &component.os, &component.arch))
                    && !keys.contains(&(dependency, &any, &component.arch))
                {
                    return Err(invalid(format!(
                        "Component {} depends on unknown {dependency}",
                        component.id
                    )));
                }
            }
        }
        Ok(())
    }
    /// Components for this machine, in manifest order.
    pub fn local(&self) -> impl Iterator<Item = &Component> {
        self.components.iter().filter(|c| c.matches_platform())
    }
    pub fn find(&self, id: &str) -> Option<&Component> {
        self.local().find(|c| c.id == id)
    }
}

/// `YYYY-MM-DDTHH:MM:SS[.fraction]Z` → Unix seconds. Only UTC (`Z`) is accepted.
pub fn parse_rfc3339(value: &str) -> Option<u64> {
    let value = value.strip_suffix('Z')?;
    let (date, time) = value.split_once('T')?;
    let time = time.split_once('.').map_or(time, |(whole, fraction)| {
        if fraction.is_empty() || !fraction.bytes().all(|b| b.is_ascii_digit()) {
            ""
        } else {
            whole
        }
    });
    let number = |text: &str, len: usize| -> Option<i64> {
        (text.len() == len && text.bytes().all(|b| b.is_ascii_digit()))
            .then(|| text.parse().ok())
            .flatten()
    };
    let mut d = date.split('-');
    let (year, month, day) = (
        number(d.next()?, 4)?,
        number(d.next()?, 2)?,
        number(d.next()?, 2)?,
    );
    let mut t = time.split(':');
    let (hour, minute, second) = (
        number(t.next()?, 2)?,
        number(t.next()?, 2)?,
        number(t.next()?, 2)?,
    );
    if d.next().is_some() || t.next().is_some() {
        return None;
    }
    let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    let days_in_month = [
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    if !(1970..=9999).contains(&year)
        || !(1..=12).contains(&month)
        || day < 1
        || day > days_in_month[(month - 1) as usize]
        || hour > 23
        || minute > 59
        || second > 59
    {
        return None;
    }
    // Days from civil (Howard Hinnant), valid for the proleptic Gregorian calendar.
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    u64::try_from(days * 86_400 + hour * 3600 + minute * 60 + second).ok()
}

/// Unix seconds → `YYYY-MM-DDTHH:MM:SSZ` (round-trip check for the parser).
#[cfg(test)]
pub fn format_rfc3339(seconds: u64) -> String {
    let days = (seconds / 86_400) as i64;
    let rest = seconds % 86_400;
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        rest / 3600,
        rest / 60 % 60,
        rest % 60
    )
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use serde_json::{json, Value};

    pub fn component(id: &str, kind: &str) -> Value {
        let mut value = json!({
            "id": id, "version": "1.0.0", "os": OS, "arch": ARCH, "kind": kind,
            "size_bytes": 10, "installed_size_bytes": 20, "temp_size_bytes": 0,
            "license": {"name": "MIT", "url": "https://github.com/Zethrus/Murmur"},
            "provenance": {"type": "murmur_attested_only", "source_url": "https://github.com/Zethrus/Murmur",
                "source_revision": "abc", "source_sha256": "", "note": ""},
            "display_name": id, "description": "test"
        });
        if matches!(kind, "engine" | "cuda") {
            value["archive"] = json!("zip");
            value["urls"] = json!(["https://github.com/Zethrus/Murmur/releases/download/v1/x.zip"]);
            value["sha256"] = json!("0".repeat(64));
        } else {
            value["archive"] = json!("none");
            value["model"] = json!(id);
            value["files"] = json!([{"name": "model.bin", "size_bytes": 10, "sha256": "1".repeat(64),
                "urls": ["https://huggingface.co/a/b/resolve/0123/model.bin"]}]);
            value["quality"] = json!("good");
            value["speed"] = json!("fast");
            value["memory_mb"] = json!(500);
            value["languages"] = json!("en");
        }
        value
    }

    pub fn manifest(serial: u64, components: Vec<Value>) -> Value {
        json!({"schema_version": 1, "serial": serial, "issued_at": "2026-11-01T00:00:00Z",
            "expires_at": "2027-02-01T00:00:00Z", "channel": "stable", "components": components})
    }

    fn parse(value: &Value) -> Result<Manifest, ComponentError> {
        Manifest::parse(&serde_json::to_vec(value).unwrap())
    }

    #[test]
    fn valid_manifest_parses_and_filters_platform() {
        let mut cuda = component("cuda", "cuda");
        cuda["depends"] = json!(["engine"]);
        let mut other = component("engine", "engine");
        other["os"] = json!(if OS == "linux" { "windows" } else { "linux" });
        let m = parse(&manifest(
            3,
            vec![
                component("engine", "engine"),
                cuda,
                other,
                component("speech-tiny.en", "speech-model"),
            ],
        ))
        .unwrap();
        assert_eq!(m.local().count(), 3);
        assert_eq!(m.find("cuda").unwrap().depends, ["engine"]);
        assert_eq!(m.issued_at().unwrap(), 1_793_491_200);
    }

    #[test]
    fn rejects_unknown_schema_and_structural_errors() {
        let mut value = manifest(1, vec![component("engine", "engine")]);
        value["schema_version"] = json!(2);
        assert!(parse(&value).unwrap_err().message.contains("version 2"));
        let cases: Vec<Box<dyn Fn(&mut Value)>> = vec![
            Box::new(|v| v["channel"] = json!("beta")),
            Box::new(|v| v["expires_at"] = json!("2026-10-01T00:00:00Z")),
            Box::new(|v| v["issued_at"] = json!("2026-11-01T00:00:00+01:00")),
            Box::new(|v| v["components"][0]["id"] = json!("../engine")),
            Box::new(|v| v["components"][0]["id"] = json!("Engine")),
            Box::new(|v| v["components"][0]["version"] = json!("1/2")),
            Box::new(|v| v["components"][0]["urls"] = json!(["https://example.com/x.zip"])),
            Box::new(|v| v["components"][0]["urls"] = json!(["http://github.com/x.zip"])),
            Box::new(|v| v["components"][0]["urls"] = json!([])),
            Box::new(|v| v["components"][0]["sha256"] = json!("XYZ")),
            Box::new(|v| v["components"][0]["archive"] = json!("tar")),
            Box::new(|v| v["components"][0]["depends"] = json!(["cuda"])),
            Box::new(|v| v["components"][0]["size_bytes"] = json!(0)),
            Box::new(|v| v["components"][0]["provenance"]["type"] = json!("trust_me")),
            Box::new(|v| {
                let first = v["components"][0].clone();
                v["components"].as_array_mut().unwrap().push(first)
            }),
        ];
        for (index, mutate) in cases.iter().enumerate() {
            let mut value = manifest(1, vec![component("engine", "engine")]);
            mutate(&mut value);
            assert!(parse(&value).is_err(), "case {index} accepted");
        }
        let mut model = component("speech-x", "speech-model");
        model["files"][0]["name"] = json!("../model.bin");
        assert!(parse(&manifest(1, vec![model])).is_err());
        let mut model = component("speech-x", "speech-model");
        model["languages"] = json!("klingon");
        assert!(parse(&manifest(1, vec![model])).is_err());
        let mut model = component("speech-x", "speech-model");
        model["size_bytes"] = json!(11);
        assert!(
            parse(&manifest(1, vec![model])).is_err(),
            "file sizes must add up"
        );
    }

    #[test]
    fn time_round_trip() {
        for (text, seconds) in [
            ("1970-01-01T00:00:00Z", 0),
            ("2000-02-29T12:34:56Z", 951_827_696),
            ("2026-11-06T23:59:59.5Z", 1_794_009_599),
        ] {
            assert_eq!(parse_rfc3339(text), Some(seconds), "{text}");
        }
        assert_eq!(format_rfc3339(951_827_696), "2000-02-29T12:34:56Z");
        for bad in [
            "2026-02-30T00:00:00Z",
            "2026-11-06 00:00:00Z",
            "2026-11-06T24:00:00Z",
            "2026-11-06T00:00:00",
            "2026-11-06T00:00:00.Z",
        ] {
            assert_eq!(parse_rfc3339(bad), None, "{bad}");
        }
    }
}
