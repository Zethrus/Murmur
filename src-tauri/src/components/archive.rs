//! Zip extraction that refuses anything an archive should not be able to do: escape the
//! staging directory, create links or device files, collide on case-insensitive or Windows
//! file systems, or expand far beyond the size the signed manifest declared.
use super::{ComponentError, Reason};
use std::{
    collections::HashSet,
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
};

pub const MAX_ENTRIES: usize = 20_000;
pub const MAX_PATH: usize = 240;
pub const MAX_RATIO: u64 = 200;
/// Entries smaller than this cannot be a meaningful bomb; the ratio rule starts here.
pub const RATIO_FLOOR: u64 = 1 << 20;

const RESERVED: &[&str] = &["con", "prn", "aux", "nul"];

fn rejected(message: impl Into<String>) -> ComponentError {
    ComponentError::new(Reason::ArchiveRejected, message)
}

/// Validate one entry name; returns its components. Directories end in `/`.
pub fn check_name(name: &str) -> Result<Vec<String>, ComponentError> {
    let bad = |why: &str| rejected(format!("Archive entry rejected ({why})"));
    if name.is_empty() || name.len() > MAX_PATH {
        return Err(bad("path length"));
    }
    if name.starts_with('/') || name.contains('\\') || name.contains(':') {
        return Err(bad("absolute path, backslash, drive or alternate data stream"));
    }
    if name.chars().any(|c| c.is_control()) {
        return Err(bad("control character"));
    }
    let trimmed = name.strip_suffix('/').unwrap_or(name);
    let mut parts = Vec::new();
    for part in trimmed.split('/') {
        if part.is_empty() || part == "." || part == ".." {
            return Err(bad("empty, current or parent path component"));
        }
        if part.ends_with('.') || part.ends_with(' ') {
            return Err(bad("trailing dot or space"));
        }
        let stem = part.split('.').next().unwrap_or("").trim_end().to_ascii_lowercase();
        let numbered = (stem.starts_with("com") || stem.starts_with("lpt"))
            && stem.len() == 4
            && matches!(stem.as_bytes()[3], b'1'..=b'9');
        if RESERVED.contains(&stem.as_str()) || numbered {
            return Err(bad("reserved Windows device name"));
        }
        parts.push(part.to_owned());
    }
    Ok(parts)
}

const S_IFMT: u32 = 0o170_000;
const S_IFREG: u32 = 0o100_000;
const S_IFDIR: u32 = 0o040_000;

/// Entry count the end-of-central-directory record declares (zip64 aware). The zip crate
/// indexes entries by name, so duplicates would otherwise collapse silently.
fn declared_entries(path: &Path) -> Option<u64> {
    use std::io::{Seek, SeekFrom};
    let mut file = fs::File::open(path).ok()?;
    let length = file.metadata().ok()?.len();
    let tail_start = length.saturating_sub(65_557 + 20);
    file.seek(SeekFrom::Start(tail_start)).ok()?;
    let mut tail = Vec::new();
    file.read_to_end(&mut tail).ok()?;
    let eocd = tail.windows(4).rposition(|w| w == b"PK\x05\x06")?;
    let count = u16::from_le_bytes(tail.get(eocd + 10..eocd + 12)?.try_into().ok()?);
    if count != 0xFFFF {
        return Some(u64::from(count));
    }
    let locator = eocd.checked_sub(20)?;
    if tail.get(locator..locator + 4)? != b"PK\x06\x07" {
        return None;
    }
    let record = u64::from_le_bytes(tail.get(locator + 8..locator + 16)?.try_into().ok()?);
    let mut header = [0u8; 40];
    file.seek(SeekFrom::Start(record)).ok()?;
    file.read_exact(&mut header).ok()?;
    if &header[..4] != b"PK\x06\x06" {
        return None;
    }
    Some(u64::from_le_bytes(header[32..40].try_into().ok()?))
}

struct Entry {
    index: usize,
    path: PathBuf,
    directory: bool,
    executable: bool,
    size: u64,
}

/// Extract `zip` into the empty directory `destination`. `max_total` bounds the sum of all
/// uncompressed entry sizes (manifest `installed_size_bytes` × 1.05).
pub fn extract(zip: &Path, destination: &Path, max_total: u64, cancel: &AtomicBool) -> Result<u64, ComponentError> {
    let file = fs::File::open(zip).map_err(|_| ComponentError::new(Reason::Io, "Could not open the archive"))?;
    let mut archive = zip::ZipArchive::new(file).map_err(|_| rejected("The archive is not a valid zip file"))?;
    if archive.len() > MAX_ENTRIES {
        return Err(rejected(format!("The archive has more than {MAX_ENTRIES} entries")));
    }
    if declared_entries(zip) != Some(archive.len() as u64) {
        return Err(rejected("Archive entry rejected (duplicate name or inconsistent directory)"));
    }
    let mut entries = Vec::with_capacity(archive.len());
    let mut seen = HashSet::new();
    let mut files = HashSet::new();
    let mut directories = HashSet::new();
    let mut total: u64 = 0;
    for index in 0..archive.len() {
        let entry = archive.by_index(index).map_err(|_| rejected("Unreadable archive entry"))?;
        let parts = check_name(entry.name())?;
        let mode = entry.unix_mode();
        let directory = entry.is_dir();
        let kind = mode.map(|m| m & S_IFMT).filter(|kind| *kind != 0);
        if entry.is_symlink() || kind.is_some_and(|k| k != S_IFREG && k != S_IFDIR) {
            return Err(rejected("Archive entry rejected (link or special file)"));
        }
        if kind == Some(S_IFDIR) && !directory {
            return Err(rejected("Archive entry rejected (directory without trailing slash)"));
        }
        let key = parts.join("/").to_lowercase();
        if !seen.insert(key.clone()) {
            return Err(rejected("Archive entry rejected (duplicate or case-colliding name)"));
        }
        // A file must not also be a parent directory of another entry.
        let mut prefix = String::new();
        for part in &parts[..parts.len() - 1] {
            if !prefix.is_empty() {
                prefix.push('/');
            }
            prefix.push_str(&part.to_lowercase());
            if files.contains(&prefix) {
                return Err(rejected("Archive entry rejected (file used as a directory)"));
            }
            directories.insert(prefix.clone());
        }
        if directory {
            directories.insert(key);
        } else {
            if directories.contains(&key) {
                return Err(rejected("Archive entry rejected (file used as a directory)"));
            }
            files.insert(key);
            let size = entry.size();
            if size >= RATIO_FLOOR && size / entry.compressed_size().max(1) > MAX_RATIO {
                return Err(rejected(format!("Archive entry compresses more than {MAX_RATIO}:1")));
            }
            total = total.saturating_add(size);
            if total > max_total {
                return Err(rejected("The archive expands beyond its declared size"));
            }
        }
        entries.push(Entry {
            index,
            path: parts.iter().collect(),
            directory,
            executable: mode.is_some_and(|m| m & 0o111 != 0),
            size: entry.size(),
        });
    }
    fs::create_dir_all(destination).map_err(|_| ComponentError::new(Reason::Io, "Could not create the staging folder"))?;
    let io = |_| ComponentError::new(Reason::Io, "Could not extract the archive (disk full?)");
    let mut written: u64 = 0;
    let mut buffer = vec![0u8; 1 << 20];
    for entry in &entries {
        if cancel.load(Ordering::Acquire) {
            return Err(ComponentError::new(Reason::Cancelled, "Installation cancelled"));
        }
        let target = destination.join(&entry.path);
        if entry.directory {
            fs::create_dir_all(&target).map_err(io)?;
            continue;
        }
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent).map_err(io)?;
        }
        let mut source = archive.by_index(entry.index).map_err(|_| rejected("Unreadable archive entry"))?;
        let mut out = fs::OpenOptions::new().write(true).create_new(true).open(&target).map_err(io)?;
        let mut copied: u64 = 0;
        loop {
            let n = source.read(&mut buffer).map_err(|_| rejected("Corrupt archive entry"))?;
            if n == 0 {
                break;
            }
            copied += n as u64;
            written += n as u64;
            if copied > entry.size || written > max_total {
                return Err(rejected("Archive entry is larger than declared"));
            }
            out.write_all(&buffer[..n]).map_err(io)?;
        }
        if copied != entry.size {
            return Err(rejected("Archive entry is shorter than declared"));
        }
        out.sync_all().map_err(io)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = if entry.executable { 0o755 } else { 0o644 };
            fs::set_permissions(&target, fs::Permissions::from_mode(mode)).map_err(io)?;
        }
        #[cfg(not(unix))]
        let _ = entry.executable;
    }
    Ok(written)
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use zip::{write::SimpleFileOptions, CompressionMethod, ZipWriter};

    pub enum Item<'a> {
        File(&'a str, &'a [u8], u32),
        Dir(&'a str),
        Link(&'a str, &'a str),
        Mode(&'a str, u32),
    }

    pub fn build(path: &Path, items: &[Item<'_>]) {
        let mut zip = ZipWriter::new(fs::File::create(path).unwrap());
        let options = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
        for item in items {
            match item {
                Item::File(name, data, mode) => {
                    zip.start_file(*name, options.unix_permissions(*mode)).unwrap();
                    zip.write_all(data).unwrap();
                }
                Item::Dir(name) => zip.add_directory(*name, options).unwrap(),
                Item::Link(name, target) => zip.add_symlink(*name, *target, options).unwrap(),
                Item::Mode(name, _) => {
                    zip.start_file(*name, options.unix_permissions(0o644)).unwrap();
                    zip.write_all(b"x").unwrap();
                }
            }
        }
        zip.finish().unwrap();
        // The writer masks file-type bits; patch raw st_mode values into the central directory.
        let modes: Vec<(String, u32)> = items.iter().filter_map(|i| match i {
            Item::Mode(name, mode) => Some((name.to_string(), *mode)),
            _ => None,
        }).collect();
        if !modes.is_empty() {
            let mut bytes = fs::read(path).unwrap();
            let mut at = 0;
            while let Some(offset) = bytes[at..].windows(4).position(|w| w == b"PK\x01\x02") {
                let header = at + offset;
                let name_len = u16::from_le_bytes([bytes[header + 28], bytes[header + 29]]) as usize;
                let name = String::from_utf8_lossy(&bytes[header + 46..header + 46 + name_len]).into_owned();
                if let Some((_, mode)) = modes.iter().find(|(n, _)| *n == name) {
                    bytes[header + 5] = 3; // made by Unix
                    bytes[header + 38..header + 42].copy_from_slice(&(mode << 16).to_le_bytes());
                }
                at = header + 46;
            }
            fs::write(path, bytes).unwrap();
        }
    }

    fn dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("murmur-archive-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }
    fn run(name: &str, items: &[Item<'_>], max: u64) -> Result<u64, ComponentError> {
        let d = dir(name);
        let zip = d.join("a.zip");
        build(&zip, items);
        let result = extract(&zip, &d.join("out"), max, &AtomicBool::new(false));
        if result.is_err() {
            // Nothing escapes the staging directory even on failure.
            assert!(!d.parent().unwrap().join("escaped").exists());
        }
        let _ = fs::remove_dir_all(&d);
        result
    }

    #[test]
    fn extracts_files_dirs_and_executable_bit() {
        let d = dir("ok");
        let zip = d.join("a.zip");
        build(&zip, &[Item::Dir("engine/"), Item::File("engine/murmur-engine", b"#!/bin/sh\n", 0o755),
            Item::File("engine/_internal/lib.txt", b"data", 0o644)]);
        let out = d.join("out");
        assert_eq!(extract(&zip, &out, 1000, &AtomicBool::new(false)).unwrap(), 14);
        assert_eq!(fs::read(out.join("engine/_internal/lib.txt")).unwrap(), b"data");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(out.join("engine/murmur-engine")).unwrap().permissions().mode() & 0o777, 0o755);
            assert_eq!(fs::metadata(out.join("engine/_internal/lib.txt")).unwrap().permissions().mode() & 0o777, 0o644);
        }
        let _ = fs::remove_dir_all(d);
    }

    #[test]
    fn hostile_names_are_refused() {
        for name in ["../escaped", "a/../../escaped", "/etc/passwd", "C:/x", "a\\b", "file.txt:stream",
            "CON", "aux.txt", "dir/nul", "COM1.log", "lpt9", "trailing.", "trailing ", "a//b", "./a", &"x".repeat(241)] {
            assert!(check_name(name).is_err(), "{name:?} accepted");
            assert!(run("name", &[Item::File(name, b"x", 0o644)], 100).is_err(), "{name:?} extracted");
        }
        for ok in ["engine/murmur-engine", "a.b/c-d_e", "COM10", "console.txt", "lpt", "dir/"] {
            assert!(check_name(ok).is_ok(), "{ok:?} refused");
        }
    }

    #[test]
    fn links_devices_and_collisions_are_refused() {
        assert!(run("link", &[Item::Link("lib.so", "/etc/passwd")], 100).is_err());
        assert!(run("device", &[Item::Mode("dev", 0o020_644)], 100).is_err(), "character device");
        assert!(run("fifo", &[Item::Mode("fifo", 0o010_644)], 100).is_err());
        // The writer refuses duplicates, so write a/b and a/c and rename a/c to a/b in place.
        let d = dir("dup");
        let zip = d.join("a.zip");
        build(&zip, &[Item::File("a/b", b"1", 0o644), Item::File("a/c", b"2", 0o644)]);
        let mut bytes = fs::read(&zip).unwrap();
        for at in (0..bytes.len() - 3).rev() {
            if &bytes[at..at + 3] == b"a/c" {
                bytes[at + 2] = b'b';
            }
        }
        fs::write(&zip, bytes).unwrap();
        let error = extract(&zip, &d.join("out"), 100, &AtomicBool::new(false)).unwrap_err();
        assert!(error.message.contains("duplicate"), "{error:?}");
        let _ = fs::remove_dir_all(d);
        assert!(run("case", &[Item::File("Lib.so", b"1", 0o644), Item::File("lib.SO", b"2", 0o644)], 100).is_err());
        assert!(run("parent", &[Item::File("a", b"1", 0o644), Item::File("a/b", b"2", 0o644)], 100).is_err());
    }

    #[test]
    fn extracts_lzma_entries_as_written_by_python() {
        // Release zips are written by Python's zipfile with ZIP_LZMA (scripts/bundle-linux-runtime.py).
        let d = dir("lzma");
        let zip = d.join("a.zip");
        let python = "import sys, zipfile\nwith zipfile.ZipFile(sys.argv[1], 'w', compression=zipfile.ZIP_LZMA) as z:\n    i = zipfile.ZipInfo('bin/tool', (1980, 1, 1, 0, 0, 0)); i.external_attr = 0o100755 << 16; i.create_system = 3\n    z.writestr(i, b'x' * 300000)\n";
        let made = std::process::Command::new("python3").args(["-c", python]).arg(&zip).status();
        if !made.is_ok_and(|s| s.success()) {
            eprintln!("python3 unavailable; skipping");
            return;
        }
        assert_eq!(extract(&zip, &d.join("out"), 300_000, &AtomicBool::new(false)).unwrap(), 300_000);
        assert_eq!(fs::read(d.join("out/bin/tool")).unwrap(), vec![b'x'; 300_000]);
        let _ = fs::remove_dir_all(d);
    }

    #[test]
    fn bombs_and_oversize_are_refused() {
        let zeros = vec![0u8; 4 << 20];
        assert!(run("ratio", &[Item::File("zeros", &zeros, 0o644)], u64::MAX).unwrap_err().message.contains("200:1"));
        assert!(run("total", &[Item::File("a", b"12345", 0o644), Item::File("b", b"67890", 0o644)], 9).is_err());
        assert_eq!(run("exact", &[Item::File("a", b"12345", 0o644)], 5).unwrap(), 5);
        let d = dir("count");
        let zip = d.join("a.zip");
        let names: Vec<String> = (0..=MAX_ENTRIES).map(|i| format!("f{i}")).collect();
        let items: Vec<Item<'_>> = names.iter().map(|n| Item::File(n, b"", 0o644)).collect();
        build(&zip, &items);
        assert!(extract(&zip, &d.join("out"), u64::MAX, &AtomicBool::new(false)).unwrap_err().message.contains("entries"));
        assert!(!d.join("out").exists(), "validation happens before anything is written");
        let _ = fs::remove_dir_all(d);
    }
}
