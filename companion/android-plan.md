# Android companion plan

Updated November 5, 2026. The APK is deferred to November 6, subject to real-device
testing. Today's release contains Linux and Flipper Zero only.

## What exists

The Linux host already has an opt-in TLS 1.3 WebSocket listener, one-time pairing
codes, revocable device tokens, and remote microphone ingestion. These are the
first two increments of the Android work. The phone app is not implemented yet.

The listener is disabled by default, binds to `127.0.0.1`, and uses port `8787`.
TLS is mandatory; there is no `require_tls` toggle. Settings exposes the certificate
fingerprint and six-digit pairing code. There is no QR code yet. `mdns` defaults
to false and is reserved; discovery is not implemented.

Both companions use protocol **4**. The phone handshake is
`HELLO 4 <app-version> phone CAP mic`. For exact pairing, commands and audio frame
layout, use [protocol.md](protocol.md). The host audio queue is bounded, rejects
invalid stream IDs and sequences, and keeps audio in memory.

## What the phone will do

The phone will be a standalone remote microphone with dictate, enhance, accept,
and dismiss controls. It talks directly to the Linux host over the local network.
Recognition and rewriting stay on the host. A Flipper is optional and does not
pair with the phone; the host coordinates both connections.

The first Android build should use manual host and port entry. It must verify the
host's certificate fingerprint, store its token securely, and stop on a changed
certificate until the user pairs again. Microphone capture uses PCM16 mono at
16 kHz, with the existing 20 ms frame format.

Use a foreground service for background microphone capture and an explicit
notification. Connection loss must stop capture and show an error. Never write
audio or transcript contents to logs. The host rejects nonlocal source addresses,
but this does not protect a listener exposed through a proxy that hides the
original address. Public tunnels and port forwarding are outside this design.

## Remaining work

1. Build the Kotlin/Compose app with pairing, manual connection, and four controls.
2. Add remote microphone capture and host-requested start/stop handling.
3. Test microphone permission denial, sleep/background behavior, lost WiFi,
   certificate changes, token revocation, and Flipper-triggered phone recording.
4. Produce a signed sideload APK, keep the signing key outside the repository,
   and add a required APK/checksum check to the release process when Android ships.
5. Consider QR pairing and mDNS discovery after the manual connection path works.

Pin the Android Gradle Plugin, SDK, and compatible JDK versions when the app is
created. Configure SDK paths locally through `ANDROID_HOME` or Gradle settings.
Do not commit machine paths or signing credentials. The planned minimum Android
SDK is 26. Distribution is through GitHub releases, not an app store.

## Outside this release

There is no Android APK today. Play Store distribution, iOS, phone-to-Flipper BLE,
on-device speech recognition, and public internet access are not part of this plan.
The November 6 date is a target, not a claim that device testing is complete.
