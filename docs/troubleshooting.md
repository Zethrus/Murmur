# Troubleshooting

## The app launched but no window appeared

From version 0.4.3, a manual launch opens Settings. Starting at login, or passing
`--background`, keeps it in the tray. Launch Murmur again or open Settings from
the tray menu to show the window. Closing Settings does not quit the app.

An older running copy can receive the launch request instead of the downloaded
version. Quit that copy through its tray menu, then launch the new download.
Versions 0.4.2 and earlier start in the tray even when launched manually.

## No text arrives

Check that a model is both installed and selected in Engine. A model name in the
configuration is not a download. Select the correct microphone in Audio, then
focus a text field before recording.

Use an X11 session. Release 0.4.2 and newer include `xdotool` and `xprop`.
Source installations still need these utilities from the distribution.
Check for a shortcut conflict and try another combination in General. Escape is
reserved for cancellation. If typing fails, try Clipboard only to isolate the
input path.

## Text was held instead of typed

Murmur binds each dictation to the window focused when you press the shortcut.
If a different window has focus when the transcript is ready, the overlay shows
**Not inserted: focus changed** with Insert here, Copy, and Discard. Focus the
window the text belongs in, then choose Insert here, press the optional insert
shortcut, or press Accept on the Flipper. Held text stays in memory only. It is
cleared after two minutes, on Discard, or when the next dictation starts.

Change this in Behavior → When focus changes. Windows are compared by identity,
so switching between two windows of one app counts as a change. Tabs inside one
browser or terminal window cannot be told apart. Focus is checked just before
typing starts; switching windows while a long transcript is being typed is not
detected. With **Return to the original window**, a window manager or Windows
may refuse to refocus; the text is then held.

## GPU inference falls back to CPU

Desktop builds from this checkout bundle NVIDIA CUDA userspace libraries;
published 0.4.2 downloads do not. A compatible host
NVIDIA driver is still required. Device Auto falls back to CPU when CUDA
initialization or inference fails, and stays there until the engine restarts or
the device configuration changes. Engine Settings shows a reason for a genuine
GPU failure; an ordinary CPU-only machine has no warning. A missing library
suggests an incomplete package; a driver warning calls for checking driver
compatibility. Forced CUDA reports the error instead of silently using CPU.
The separately downloaded `murmur-engine` needs an external CUDA userspace
runtime for GPU use.

## The engine does not start

The AppImage, Debian package, and portable archive include the engine.

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
