//! USB CDC serial link to the Flipper.
//!
//! The Flipper app claims `usb_cdc_single`, which appears as a CDC ACM device. On unix we
//! configure the tty directly with termios: no new dependency, and no libudev on build
//! hosts. Windows needs a COM-port backend and reports itself unsupported for now.

use super::Link;
use std::{io, path::Path};

/// Paths that may hold the Flipper, best candidate first.
pub fn candidates() -> Vec<String> {
    let mut found = Vec::new();
    if let Ok(entries) = std::fs::read_dir("/dev/serial/by-id") {
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            if name.to_ascii_lowercase().contains("flipper") {
                let path = entry.path().to_string_lossy().to_string();
                if !found.contains(&path) {
                    found.push(path);
                }
            }
        }
    }
    for index in 0..8 {
        let path = format!("/dev/ttyACM{index}");
        if Path::new(&path).exists() && !found.contains(&path) {
            found.push(path);
        }
    }
    found
}

/// Explicit port wins; otherwise the first plausible device.
pub fn resolve(preferred: &str) -> Option<String> {
    let preferred = preferred.trim();
    if !preferred.is_empty() {
        return Some(preferred.to_string());
    }
    candidates().into_iter().next()
}

#[cfg(unix)]
pub struct UsbLink {
    file: std::fs::File,
    buffer: Vec<u8>,
    path: String,
}

#[cfg(unix)]
impl UsbLink {
    pub fn open(path: &str) -> io::Result<Self> {
        use std::os::unix::io::AsRawFd;

        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)?;
        let fd = file.as_raw_fd();
        // SAFETY: `fd` is owned by `file` and stays open for the whole call.
        unsafe {
            let mut termios: libc::termios = std::mem::zeroed();
            if libc::tcgetattr(fd, &mut termios) != 0 {
                return Err(io::Error::last_os_error());
            }
            libc::cfmakeraw(&mut termios);
            // Non-blocking reads: the supervisor polls.
            termios.c_cc[libc::VMIN] = 0;
            termios.c_cc[libc::VTIME] = 0;
            if libc::tcsetattr(fd, libc::TCSANOW, &mut termios) != 0 {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(Self {
            file,
            buffer: Vec::with_capacity(256),
            path: path.to_string(),
        })
    }
}

#[cfg(unix)]
impl Link for UsbLink {
    fn write_line(&mut self, line: &str) -> io::Result<()> {
        use std::io::Write;
        self.file
            .write_all(&crate::companion::protocol::frame(line))?;
        self.file.flush()
    }

    fn poll_line(&mut self) -> io::Result<Option<String>> {
        use std::io::Read;

        loop {
            if let Some(end) = self.buffer.iter().position(|byte| *byte == b'\n') {
                let mut line: Vec<u8> = self.buffer.drain(..=end).collect();
                line.pop();
                let text = String::from_utf8_lossy(&line)
                    .trim_end_matches('\r')
                    .to_string();
                if text.is_empty() {
                    continue;
                }
                return Ok(Some(text));
            }
            let mut chunk = [0u8; 256];
            match self.file.read(&mut chunk) {
                Ok(0) => return Ok(None),
                Ok(n) => {
                    self.buffer.extend_from_slice(&chunk[..n]);
                    // A peer that never terminates a line must not grow this unbounded.
                    // Drop the partial burst and resynchronise on the next newline rather
                    // than tearing down a working link.
                    if self.buffer.len() > 4096 {
                        self.buffer.clear();
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Ok(None),
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            }
        }
    }

    fn describe(&self) -> String {
        format!("USB {}", self.path)
    }
}

#[cfg(not(unix))]
pub struct UsbLink;

#[cfg(not(unix))]
impl UsbLink {
    pub fn open(_path: &str) -> io::Result<Self> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "The USB companion link is not available on this platform yet",
        ))
    }
}

#[cfg(not(unix))]
impl Link for UsbLink {
    fn write_line(&mut self, _line: &str) -> io::Result<()> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "unsupported platform",
        ))
    }
    fn poll_line(&mut self) -> io::Result<Option<String>> {
        Ok(None)
    }
    fn describe(&self) -> String {
        "USB unsupported".into()
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn candidates_are_deduplicated_and_ordered() {
        let list = candidates();
        let mut unique = list.clone();
        unique.dedup();
        assert_eq!(list, unique, "candidates must not repeat a device");
    }

    #[test]
    fn an_explicit_port_always_wins() {
        assert_eq!(resolve(" /dev/ttyACM9 ").as_deref(), Some("/dev/ttyACM9"));
    }

    #[test]
    fn a_missing_port_is_not_invented() {
        // Discovery may legitimately return nothing when no Flipper is attached.
        if candidates().is_empty() {
            assert!(resolve("").is_none());
        }
    }

    /// A pseudo-terminal stands in for the device: same termios path, real bytes both ways.
    fn open_pty() -> (std::os::unix::io::RawFd, String) {
        unsafe {
            let master = libc::posix_openpt(libc::O_RDWR | libc::O_NOCTTY);
            assert!(master >= 0, "posix_openpt failed");
            assert_eq!(libc::grantpt(master), 0, "grantpt failed");
            assert_eq!(libc::unlockpt(master), 0, "unlockpt failed");
            let name = libc::ptsname(master);
            assert!(!name.is_null(), "ptsname failed");
            (
                master,
                std::ffi::CStr::from_ptr(name).to_string_lossy().to_string(),
            )
        }
    }

    fn read_master(fd: std::os::unix::io::RawFd, wait: std::time::Duration) -> String {
        use std::io::Read;
        let deadline = std::time::Instant::now() + wait;
        let mut file =
            unsafe { <std::fs::File as std::os::unix::io::FromRawFd>::from_raw_fd(libc::dup(fd)) };
        let mut out = String::new();
        while std::time::Instant::now() < deadline {
            let mut chunk = [0u8; 256];
            match file.read(&mut chunk) {
                Ok(0) => {}
                Ok(n) => out.push_str(&String::from_utf8_lossy(&chunk[..n])),
                Err(_) => {}
            }
            if out.contains('\n') {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        out
    }

    fn write_master(fd: std::os::unix::io::RawFd, bytes: &[u8]) {
        use std::io::Write;
        let mut file =
            unsafe { <std::fs::File as std::os::unix::io::FromRawFd>::from_raw_fd(libc::dup(fd)) };
        file.write_all(bytes).expect("write to pty master");
        file.flush().expect("flush pty master");
    }

    #[test]
    fn a_pty_round_trips_framed_lines_both_ways() {
        let (master, path) = open_pty();
        let mut link = UsbLink::open(&path).expect("open pty as a serial device");

        link.write_line("HELLO 1 0.2.2 host").expect("write hello");
        let seen = read_master(master, std::time::Duration::from_millis(500));
        assert_eq!(
            seen, "HELLO 1 0.2.2 host\n",
            "host frames are newline terminated"
        );

        write_master(master, b"B OK\nS LISTEN\r\n");
        let first = link.poll_line().expect("first line");
        let second = link.poll_line().expect("second line");
        assert_eq!(first.as_deref(), Some("B OK"));
        assert_eq!(
            second.as_deref(),
            Some("S LISTEN"),
            "CRLF from a device is tolerated"
        );
        assert_eq!(link.poll_line().expect("no more data"), None);

        unsafe { libc::close(master) };
    }

    #[test]
    fn an_unterminated_burst_resynchronises_instead_of_failing() {
        let (master, path) = open_pty();
        let mut link = UsbLink::open(&path).expect("open pty");
        // One oversized, unterminated burst, then a valid line: the burst is discarded at
        // 4 KiB and the next terminated line arrives intact.
        let mut burst = vec![b'X'; 5000];
        burst.push(b'\n');
        write_master(master, &burst);
        write_master(master, b"B UP\n");

        let deadline = std::time::Instant::now() + std::time::Duration::from_millis(1500);
        let mut recovered = false;
        while std::time::Instant::now() < deadline {
            match link.poll_line() {
                Ok(Some(line)) if line == "B UP" => {
                    recovered = true;
                    break;
                }
                Ok(_) => std::thread::sleep(std::time::Duration::from_millis(10)),
                Err(error) => panic!("a garbage burst must not break the link: {error}"),
            }
        }
        assert!(recovered, "the link resumed after the oversized burst");
        unsafe { libc::close(master) };
    }
}
