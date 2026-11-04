//! Transport abstraction. One implementation per physical link; the protocol does not
//! care which one is in use.

pub mod usb;

use std::time::Duration;

/// A bidirectional line link. Implementations are polled, never blocking.
pub trait Link: Send {
    /// Write one line, adding the terminator.
    fn write_line(&mut self, line: &str) -> std::io::Result<()>;
    /// Return the next complete line, if one has arrived. Never blocks.
    fn poll_line(&mut self) -> std::io::Result<Option<String>>;
    /// Human-readable name for the status card, e.g. `USB /dev/ttyACM0`.
    fn describe(&self) -> String;
}

/// Open the link described by the configuration. `auto` prefers USB.
pub fn open(transport: &str, usb_port: &str) -> anyhow::Result<Box<dyn Link>> {
    match transport {
        "usb" | "auto" => {
            let path = usb::resolve(usb_port).ok_or_else(|| {
                anyhow::anyhow!(
                    "No Flipper USB serial device found. Connect it with a data cable, or set the port explicitly."
                )
            })?;
            Ok(Box::new(usb::UsbLink::open(&path)?))
        }
        "ble" => anyhow::bail!("Bluetooth companion links arrive in the next phase; use USB"),
        other => anyhow::bail!("Unknown companion transport: {other}"),
    }
}

/// How long the supervisor waits between polls. Also the effective input latency.
pub const POLL: Duration = Duration::from_millis(25);
