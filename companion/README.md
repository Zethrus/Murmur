# Flipper Zero companion

Murmur Companion turns a Flipper Zero into a remote for Linux dictation. It works
over USB or Bluetooth and shows live text, rewrite suggestions, battery state,
and link status. You can use Murmur without a Flipper.

## Install

1. Download `murmur-companion-vX.Y.Z.fap` from the same GitHub release as Murmur.
2. In qFlipper, copy it to `apps/Tools` on the SD card.
3. Close qFlipper, open Murmur Companion on the Flipper, and enable Companion in desktop Settings.
4. Select USB and use a data cable for the first connection. The status should show connected.

The `.fap` uses the Flipper firmware's application API. If the device reports an
API mismatch, update compatible official firmware or build the app against your
firmware's SDK. Custom firmware compatibility is not guaranteed.

## Controls

| Button | Default action |
| --- | --- |
| OK | Start or stop dictation |
| Hold OK, with Push-to-talk enabled | Record until released |
| Up | Request a rewrite |
| Down | Copy the current suggestion to the clipboard |
| Back | Dismiss |
| Long OK, with Push-to-talk disabled | Cancel dictation |

Settings controls the button map and pushes it to the device, where it is cached
on the SD card. Holding Back during recording asks whether to discard; otherwise
it exits the app. Short transcript previews stay in RAM, not on the SD card.

## Bluetooth

Enable Bluetooth on Linux and on the Flipper, then open Murmur Companion. Select
Bluetooth in desktop Companion settings. Complete firmware-managed pairing if
prompted. Leave the BLE device address empty for discovery, or set it explicitly
when more than one device is nearby.

Automatic transport tries USB first and then Bluetooth. Close qFlipper before
using USB because only one program can own the serial connection. Exiting the
Flipper app restores normal USB behavior.

If connection fails, test USB first, confirm both app versions match, and check
Linux serial permissions or Bluetooth service availability. Do not run Murmur as
root to work around device permissions.

## Build

Use an isolated environment for the build tool:

```sh
python3 -m venv .venv-flipper
.venv-flipper/bin/python -m pip install ufbt
PATH="$PWD/.venv-flipper/bin:$PATH" companion/flipper/build.sh
```

Output: `companion/flipper/dist/murmur_companion.fap`. Run `ufbt launch` from
`companion/flipper` to load it onto a connected device. The script synchronizes the
companion version with `src-tauri/Cargo.toml`.

## Release and protocol

Linux and the `.fap` are required assets in every current public release. A
Flipper build failure leaves the release in draft. The companion version matches
the desktop version; the [wire protocol](protocol.md) has a separate version,
currently 4, checked across the specification and both implementations.

The Android APK is deferred to November 6, 2026, subject to testing. Its desktop
network/audio foundation exists, but there is no Android download in today's
release. Windows downloads are also deferred. See [the roadmap](../docs/roadmap.md)
and [Android plan](android-plan.md).
