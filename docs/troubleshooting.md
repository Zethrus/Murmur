# Troubleshooting

## The app launched but no window appeared

Murmur starts in the system tray. Open Settings from its tray menu. On desktops
without tray support, install or enable an AppIndicator extension. Closing
Settings does not quit the app.

## No text arrives

Check that a model is both installed and selected in Engine. A model name in the
configuration is not a download. Select the correct microphone in Audio, then
focus a text field before recording.

Use an X11 session. Install `xdotool` and `xprop` (`x11-utils` on Debian/Ubuntu).
Check for a shortcut conflict and try another combination in General. Escape is
reserved for cancellation. If typing fails, try Clipboard only to isolate the
input path.

## GPU inference falls back to CPU

Release downloads do not bundle NVIDIA CUDA libraries. Device Auto falls back to
CPU when CUDA initialization or inference fails, and stays there until the engine
restarts. Forced CUDA reports the error instead. The source installer installs
CUDA runtime wheels, but still needs a compatible NVIDIA driver.

## The engine does not start

Keep `murmur-engine` beside `murmur` when using the portable files. Prefer the
archive because it preserves executable permissions. Raw downloads may need
`chmod +x murmur murmur-engine`.

The frozen engine extracts libraries into temporary storage. A full disk or a
`noexec` temporary directory can prevent startup. Quit and restart after fixing
the problem. The app retries a failed engine once before requiring a restart.

## AppImage or package errors

Try `--appimage-extract-and-run` if FUSE is unavailable. Missing WebKitGTK, GTK,
audio, or X11 libraries must be installed through your distribution's package
manager. AppImage does not mean every host library is bundled. The `.deb` declares
its native dependencies.

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
