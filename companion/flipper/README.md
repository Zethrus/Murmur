# Murmur Companion

Murmur Companion turns a Flipper Zero into a remote for [Murmur](https://github.com/Zethrus/Murmur), a Linux dictation app. It works over USB or Bluetooth and shows live text, rewrite suggestions, battery state, and link status. Murmur works without a Flipper; this app adds a physical remote.

## Requirements

- Murmur desktop app on Linux, from the [Murmur releases](https://github.com/Zethrus/Murmur/releases). Use the same version as this app.
- A USB data cable for the first connection, or Bluetooth on both the computer and the Flipper.

## Getting started

1. Install Murmur on your computer and open Settings.
2. Enable **Companion** in Settings and select **USB**.
3. Close qFlipper, because only one program can use the Flipper's serial connection.
4. Open Murmur Companion on the Flipper (Apps, Tools). The status shows _connected_ once the link is up.

## Controls

- **OK**: start or stop dictation.
- **Hold OK** (Push-to-talk enabled): record until released.
- **Long OK** (Push-to-talk disabled): cancel dictation.
- **Up**: request a rewrite.
- **Down**: copy the current suggestion to the clipboard.
- **Back**: dismiss. Holding Back during recording asks whether to discard; otherwise it exits the app.

Murmur's Settings controls the button map and pushes it to the device, where it is cached on the SD card. Short transcript previews stay in RAM and are not written to the SD card.

## Bluetooth

Enable Bluetooth on Linux and on the Flipper, then open Murmur Companion. Select **Bluetooth** in Murmur's Companion settings and complete firmware-managed pairing if prompted. Leave the BLE device address empty for discovery, or set it when more than one device is nearby.

Automatic transport tries USB first and then Bluetooth. Exiting the app restores normal USB behavior.

## Troubleshooting

- Test USB first, then Bluetooth.
- Confirm Murmur and Murmur Companion have the same version.
- Check Linux serial permissions or Bluetooth service availability. Do not run Murmur as root to work around device permissions.
- If the Flipper reports an API mismatch, update to the latest official firmware.

More documentation, the wire protocol, and build instructions are in the [companion guide](https://github.com/Zethrus/Murmur/blob/master/companion/README.md).
