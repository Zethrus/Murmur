# Release schedule

Updated November 5, 2026.

## November 5: Linux and Flipper Zero

The public launch includes Linux x86_64 downloads and the matching Flipper Zero
`.fap`. Linux runs on X11. The companion provides USB and Bluetooth controls,
live text, and push-to-talk.

## November 6 target: Android companion

The Android APK is deferred until tomorrow. It is not in today's release and is
not yet available for download.

The desktop already contains the opt-in TLS listener, pairing, and remote audio
handling. The Android app still needs to be built and tested against those pieces.
The plan is a standalone phone remote with four control actions and a microphone
that streams to Murmur over the local network. Speech recognition stays on the
Linux host. A Flipper is optional.

Before shipping: verify pairing and certificate pinning, microphone permissions,
connection loss, background operation, and an APK installation on a real device.
The signed APK and checksum will be attached to a release once those checks pass.
See [the Android implementation plan](../companion/android-plan.md).

## Later this week: Windows

Windows builds are deferred, tentatively until November 6. Build scripts remain
in the repository for development. There is no supported Windows download in
today's release.

Before shipping: produce the installer and portable archive, then check microphone
capture, hotkeys, text injection, tray behavior, and CPU/GPU inference on Windows
hardware. A successful CI build alone does not establish desktop compatibility.

Dates are targets, not promises. This page will be updated if testing changes them.
