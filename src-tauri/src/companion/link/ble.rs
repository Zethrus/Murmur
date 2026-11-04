//! Bluetooth transport.
//!
//! The Flipper exposes its serial service over BLE as a GATT profile, not as a serial
//! port, so this link is a BLE central built on `btleplug`. The trait is synchronous and
//! polled, so the async work runs on a small runtime of its own and talks through
//! channels: one way for lines out, one queue for lines in.
//!
//! Bonding belongs to the firmware and the OS agent: the Flipper shows a PIN, the host
//! enters it once, and the key is kept. This module only reports that a connection was
//! refused, never tries to pair itself.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use btleplug::api::{
    Central, CharPropFlags, Characteristic, Manager as _, Peripheral as _, ScanFilter, WriteType,
};
use btleplug::platform::Manager;
use futures::StreamExt;
use tokio::sync::Notify;

use super::Link;

/// Flipper's serial service. Matched by string so this module does not depend on `uuid`.
const SERIAL_SERVICE: &str = "8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000";
/// Write characteristic: central → Flipper.
const SERIAL_RX: &str = "19ed82ae-ed21-4c9d-4145-228e62fe0000";
/// Indicate characteristic: Flipper → central.
const SERIAL_TX: &str = "19ed82ae-ed21-4c9d-4145-228e61fe0000";
const FLOW_CONTROL: &str = "19ed82ae-ed21-4c9d-4145-228e63fe0000";
const RPC_STATUS: &str = "19ed82ae-ed21-4c9d-4145-228e64fe0000";

/// A single BLE write is limited by the negotiated MTU; 20 bytes is always safe and the
/// line protocol reassembles on the far side.
const CHUNK: usize = 20;
const SCAN_TIMEOUT: Duration = Duration::from_secs(20);
const OUTBOUND_CAPACITY: usize = 16;

struct OutboundQueue {
    control: Mutex<VecDeque<Vec<u8>>>,
    text: Mutex<Option<Vec<u8>>>,
    ready: Notify,
}

impl OutboundQueue {
    fn push(&self, line: &str) -> std::io::Result<()> {
        let is_text = line.starts_with("T ") || line == "T";
        let mut frame = line.as_bytes().to_vec();
        frame.push(b'\n');
        if is_text {
            *self
                .text
                .lock()
                .map_err(|_| std::io::Error::other("BLE queue poisoned"))? = Some(frame);
        } else {
            let mut control = self
                .control
                .lock()
                .map_err(|_| std::io::Error::other("BLE queue poisoned"))?;
            if control.len() == OUTBOUND_CAPACITY {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::WouldBlock,
                    "BLE outbound queue full",
                ));
            }
            control.push_back(frame);
        }
        self.ready.notify_one();
        Ok(())
    }

    async fn pop(&self) -> Vec<u8> {
        loop {
            let notified = self.ready.notified();
            if let Some(frame) = self.control.lock().expect("BLE queue poisoned").pop_front() {
                return frame;
            }
            if let Some(frame) = self.text.lock().expect("BLE queue poisoned").take() {
                return frame;
            }
            let _ = tokio::time::timeout(Duration::from_millis(25), notified).await;
        }
    }
}

pub struct BleLink {
    outbound: Arc<OutboundQueue>,
    inbound: Arc<Mutex<VecDeque<String>>>,
    connected: Arc<AtomicBool>,
    rssi: Arc<AtomicI32>,
    name: String,
    address: String,
    /// Kept alive for the lifetime of the link; dropping it stops the tasks.
    runtime: Option<tokio::runtime::Runtime>,
}

impl BleLink {
    /// Find the Flipper and connect. `preferred` may hold an address or a name fragment.
    pub fn open(preferred: &str) -> Result<Self> {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .thread_name("murmur-ble")
            .build()
            .context("starting the BLE runtime")?;

        let found = runtime.block_on(discover(preferred))?;
        let peripheral = found.peripheral;
        let name = found.name;
        let address = found.address;
        let tx_uuid = found.tx;
        let rx_uuid = found.rx;

        let outbound = Arc::new(OutboundQueue {
            control: Mutex::new(VecDeque::with_capacity(OUTBOUND_CAPACITY)),
            text: Mutex::new(None),
            ready: Notify::new(),
        });
        let task_outbound = outbound.clone();
        let inbound = Arc::new(Mutex::new(VecDeque::<String>::new()));
        let connected = Arc::new(AtomicBool::new(true));
        let rssi = Arc::new(AtomicI32::new(found.rssi));

        let task_inbound = inbound.clone();
        let task_connected = connected.clone();
        let task_rssi = rssi.clone();

        runtime.spawn(async move {
            let mut notifications = match peripheral.notifications().await {
                Ok(stream) => stream,
                Err(error) => {
                    tracing::warn!(%error, "Companion BLE notifications unavailable");
                    task_connected.store(false, Ordering::SeqCst);
                    return;
                }
            };
            let mut partial: Vec<u8> = Vec::new();
            let mut ticker = tokio::time::interval(Duration::from_secs(10));

            loop {
                tokio::select! {
                    incoming = notifications.next() => {
                        let Some(note) = incoming else { break };
                        if note.uuid != tx_uuid.uuid {
                            continue;
                        }
                        for byte in note.value {
                            if byte == b'\n' {
                                if let Ok(line) = String::from_utf8(partial.clone()) {
                                    let trimmed = line.trim_end_matches('\r').to_string();
                                    if !trimmed.is_empty() {
                                        if let Ok(mut queue) = task_inbound.lock() {
                                            if queue.len() < 64 {
                                                queue.push_back(trimmed);
                                            }
                                        }
                                    }
                                }
                                partial.clear();
                            } else if partial.len() < 4096 {
                                partial.push(byte);
                            }
                        }
                    }
                    bytes = task_outbound.pop() => {
                        for chunk in bytes.chunks(CHUNK) {
                            let write_type = if rx_uuid.properties.contains(CharPropFlags::WRITE_WITHOUT_RESPONSE) {
                                WriteType::WithoutResponse
                            } else {
                                WriteType::WithResponse
                            };
                            if peripheral
                                .write(&rx_uuid, chunk, write_type)
                                .await
                                .is_err()
                            {
                                task_connected.store(false, Ordering::SeqCst);
                                return;
                            }
                        }
                    }
                    _ = ticker.tick() => {
                        if !peripheral.is_connected().await.unwrap_or(false) {
                            task_connected.store(false, Ordering::SeqCst);
                            return;
                        }
                        if let Ok(Some(properties)) = peripheral.properties().await {
                            if let Some(rssi) = properties.rssi {
                                task_rssi.store(rssi as i32, Ordering::SeqCst);
                            }
                        }
                    }
                }
            }
            task_connected.store(false, Ordering::SeqCst);
        });

        Ok(Self {
            outbound,
            inbound,
            connected,
            rssi,
            name,
            address,
            runtime: Some(runtime),
        })
    }

    /// Signal strength of the last refresh, in dBm.
    pub fn rssi(&self) -> i32 {
        self.rssi.load(Ordering::Relaxed)
    }
}

struct Found {
    peripheral: btleplug::platform::Peripheral,
    name: String,
    address: String,
    rssi: i32,
    rx: Characteristic,
    tx: Characteristic,
}

async fn discover(preferred: &str) -> Result<Found> {
    let manager = Manager::new()
        .await
        .context("asking the OS for a BLE adapter")?;
    let adapters = manager.adapters().await.context("listing BLE adapters")?;
    let adapter = adapters.into_iter().next().ok_or_else(|| {
        anyhow!("No Bluetooth adapter found. Plug in a dongle or enable Bluetooth.")
    })?;

    adapter
        .start_scan(ScanFilter::default())
        .await
        .context("starting a BLE scan")?;

    let deadline = tokio::time::Instant::now() + SCAN_TIMEOUT;
    let mut chosen = None;
    while tokio::time::Instant::now() < deadline {
        for peripheral in adapter.peripherals().await.unwrap_or_default() {
            let Ok(Some(properties)) = peripheral.properties().await else {
                continue;
            };
            let name = properties.local_name.clone().unwrap_or_default();
            let address = properties.address.to_string();
            let matches_name = name.to_lowercase().contains("flipper");
            let matches_preferred = !preferred.is_empty()
                && (address.eq_ignore_ascii_case(preferred)
                    || name.to_lowercase().contains(&preferred.to_lowercase()));
            if matches_preferred || (preferred.is_empty() && matches_name) {
                chosen = Some((
                    peripheral,
                    name,
                    address,
                    properties.rssi.unwrap_or(0) as i32,
                ));
                break;
            }
        }
        if chosen.is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(400)).await;
    }

    let (peripheral, name, address, rssi) =
        chosen.ok_or_else(|| {
            anyhow!(
                "No Flipper found over Bluetooth. Start Murmur Companion on the device and keep it nearby."
            )
        })?;

    adapter.stop_scan().await.ok();

    peripheral.connect().await.map_err(|error| {
        anyhow!(
            "Bluetooth connection refused: {error}. Pair with the Flipper and enter the PIN it shows, then reconnect."
        )
    })?;
    peripheral
        .discover_services()
        .await
        .context("reading the device's GATT services")?;

    let characteristics = peripheral.characteristics();
    let (rx, tx) = select_serial_characteristics(&characteristics)?;
    peripheral
        .subscribe(&tx)
        .await
        .context("subscribing to the Flipper's serial indications")?;

    Ok(Found {
        peripheral,
        name: if name.is_empty() {
            "Flipper".into()
        } else {
            name
        },
        address,
        rssi,
        rx,
        tx,
    })
}

fn select_serial_characteristics(
    characteristics: &std::collections::BTreeSet<Characteristic>,
) -> Result<(Characteristic, Characteristic)> {
    let in_serial = |c: &Characteristic| {
        c.service_uuid
            .to_string()
            .eq_ignore_ascii_case(SERIAL_SERVICE)
    };
    let data_characteristic = |c: &Characteristic| {
        in_serial(c)
            && ![FLOW_CONTROL, RPC_STATUS]
                .iter()
                .any(|uuid| c.uuid.to_string().eq_ignore_ascii_case(uuid))
    };
    let writable = |c: &Characteristic| {
        c.properties.contains(CharPropFlags::WRITE_WITHOUT_RESPONSE)
            || c.properties.contains(CharPropFlags::WRITE)
    };
    let notifying = |c: &Characteristic| {
        c.properties
            .intersects(CharPropFlags::INDICATE | CharPropFlags::NOTIFY)
    };

    // A fallback is confined to the serial service and excludes qFlipper's control
    // characteristics; a different service must never be used for protocol data.
    let rx = characteristics
        .iter()
        .find(|c| {
            data_characteristic(c)
                && c.uuid.to_string().eq_ignore_ascii_case(SERIAL_RX)
                && writable(c)
        })
        .or_else(|| {
            characteristics
                .iter()
                .find(|c| data_characteristic(c) && writable(c))
        })
        .cloned();
    let tx = characteristics
        .iter()
        .find(|c| {
            data_characteristic(c)
                && c.uuid.to_string().eq_ignore_ascii_case(SERIAL_TX)
                && notifying(c)
        })
        .or_else(|| {
            characteristics
                .iter()
                .find(|c| data_characteristic(c) && notifying(c))
        })
        .cloned();
    let rx = rx.ok_or_else(|| anyhow!("The Flipper has no writable serial characteristic"))?;
    let tx = tx.ok_or_else(|| anyhow!("The Flipper has no indicating serial characteristic"))?;
    Ok((rx, tx))
}

impl Link for BleLink {
    fn write_line(&mut self, line: &str) -> std::io::Result<()> {
        if !self.connected.load(Ordering::SeqCst) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::NotConnected,
                "Bluetooth link is down",
            ));
        }
        self.outbound.push(line)
    }

    fn poll_line(&mut self) -> std::io::Result<Option<String>> {
        let Ok(mut queue) = self.inbound.lock() else {
            return Ok(None);
        };
        Ok(queue.pop_front())
    }

    fn describe(&self) -> String {
        format!("BLE {} ({})", self.name, self.address)
    }

    fn is_live(&self) -> bool {
        self.connected.load(Ordering::SeqCst)
    }

    fn rssi(&self) -> Option<i32> {
        Some(BleLink::rssi(self))
    }
}

impl Drop for BleLink {
    fn drop(&mut self) {
        self.connected.store(false, Ordering::SeqCst);
        if let Some(runtime) = self.runtime.take() {
            runtime.shutdown_background();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    fn characteristic(uuid: &str, service: &str, properties: CharPropFlags) -> Characteristic {
        Characteristic {
            uuid: uuid.parse().unwrap(),
            service_uuid: service.parse().unwrap(),
            properties,
            descriptors: BTreeSet::new(),
        }
    }

    #[test]
    fn flipper_serial_data_ignores_flow_control_and_rpc_status() {
        let characteristics = [
            characteristic(
                FLOW_CONTROL,
                SERIAL_SERVICE,
                CharPropFlags::WRITE | CharPropFlags::NOTIFY,
            ),
            characteristic(RPC_STATUS, SERIAL_SERVICE, CharPropFlags::INDICATE),
            characteristic(
                SERIAL_RX,
                SERIAL_SERVICE,
                CharPropFlags::WRITE_WITHOUT_RESPONSE,
            ),
            characteristic(SERIAL_TX, SERIAL_SERVICE, CharPropFlags::INDICATE),
            characteristic(
                SERIAL_RX,
                "0000180a-0000-1000-8000-00805f9b34fb",
                CharPropFlags::WRITE,
            ),
        ]
        .into_iter()
        .collect();

        let (rx, tx) = select_serial_characteristics(&characteristics).unwrap();
        assert_eq!(rx.uuid.to_string(), SERIAL_RX);
        assert_eq!(tx.uuid.to_string(), SERIAL_TX);
        assert_eq!(rx.service_uuid.to_string(), SERIAL_SERVICE);
        assert_eq!(tx.service_uuid.to_string(), SERIAL_SERVICE);
    }
}
