# Troubleshooting

## The app launched but no window appeared

Murmur starts in the system tray. Open Settings from its tray menu. On desktops
without tray support, install or enable an AppIndicator extension. Closing
Settings does not quit the app.

## No text arrives

Check that a model is both installed and selected in Engine. A model name in the
configuration is not a download. Select the correct microphone in Audio, then
focus a text field before recording.

Use an X11 session. Release 0.4.2 and newer include `xdotool` and `xprop`.
Source installations still need these utilities from the distribution.
Check for a shortcut conflict and try another combination in General. Escape is
reserved for cancellation. If typing fails, try Clipboard only to isolate the
input path.

## GPU inference falls back to CPU

Release downloads do not bundle NVIDIA CUDA libraries. Device Auto falls back to
CPU when CUDA initialization or inference fails, and stays there until the engine
restarts. Forced CUDA reports the error instead. The source installer installs
CUDA runtime wheels, but still needs a compatible NVIDIA driver.

## The engine does not start

The AppImage and Debian package include the engine. The separate `murmur`
download is also an AppImage and may need `chmod +x murmur`.

The frozen engine extracts libraries into temporary storage. A full disk or a
`noexec` temporary directory can prevent startup. Quit and restart after fixing
the problem. The app retries a failed engine once before requiring a restart.

## AppImage or package errors

If startup reports `GLIBC_2.39 not found`, the distribution is older than the
prebuilt binary's baseline. Current downloads are built on Ubuntu 24.04 and need
glibc 2.39 or newer. Build from source on the older distribution instead of
replacing its system C library. AppImage does not remove this requirement.

Try `--appimage-extract-and-run` if FUSE is unavailable. Release 0.4.2 and newer
bundle WebKitGTK, GTK, audio client libraries, and typing tools in each desktop
download. The host still needs a working X11 desktop, audio services, and hardware
drivers. Report a missing application library as a packaging bug.

## The Flipper does not connect

Install the `.fap` matching your desktop release, open it on the device, and close
qFlipper. Use a USB data cable. In Companion settings, select USB and use Detect
devices. Check access to the serial device through your distribution's serial
port permissions. Do not run Murmur as root.

For Bluetooth, enable Bluetooth on both devices and keep the Flipper app open.
Try USB first to separate pairing issues from a protocol mismatch. A firmware API
mismatch needs a rebuilt `.fap`; see [the companion guide](../companion/README.md).

## Reporting a problem

Logs are under the Murmur data directory described in [configuration](configuration.md).
Quit Murmur and launch with `MURMUR_LOG=debug` for extra diagnostics. Treat logs as
sensitive and remove personal data before posting them. Include the release,
distribution, desktop, and the smallest set of steps that reproduces the failure.
