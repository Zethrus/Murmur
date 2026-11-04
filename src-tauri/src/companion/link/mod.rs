//! Transport abstraction. One implementation per physical link; the protocol does not
//! care which one is in use.

pub mod ble;
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
    /// False once the transport has died and the supervisor should reopen it.
    fn is_live(&self) -> bool {
        true
    }
    /// Signal strength when the transport measures one (BLE only).
    fn rssi(&self) -> Option<i32> {
        None
    }
}

/// Open the link described by the configuration.
///
/// `auto` is USB first — the wired path needs no radio and no pairing — and Bluetooth when
/// no cable is attached. `ble` never falls back, so a user who chose Bluetooth learns why.
pub fn open(transport: &str, usb_port: &str, ble_address: &str) -> anyhow::Result<Box<dyn Link>> {
    match transport {
        "usb" => Ok(Box::new(usb::UsbLink::open(&resolve_usb(usb_port)?)?)),
        "ble" => Ok(Box::new(ble::BleLink::open(ble_address)?)),
        "auto" => match resolve_usb(usb_port) {
            Ok(path) => match usb::UsbLink::open(&path) {
                Ok(link) => Ok(Box::new(link)),
                Err(error) => {
                    tracing::info!(%error, "Companion USB link unavailable, trying Bluetooth");
                    Ok(Box::new(ble::BleLink::open(ble_address)?))
                }
            },
            Err(_) => {
                tracing::info!("No Flipper on USB, trying Bluetooth");
                Ok(Box::new(ble::BleLink::open(ble_address)?))
            }
        },
        other => anyhow::bail!("Unknown companion transport: {other}"),
    }
}

fn resolve_usb(usb_port: &str) -> anyhow::Result<String> {
    usb::resolve(usb_port).ok_or_else(|| {
        anyhow::anyhow!(
            "No Flipper USB serial device found. Connect it with a data cable, or set the port explicitly."
        )
    })
}

/// How long the supervisor waits between polls. Also the effective input latency.
pub const POLL: Duration = Duration::from_millis(25);
