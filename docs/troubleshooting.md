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

Linux desktop packages ship cuBLAS (`libcublas.so.12`, `libcublasLt.so.12`) in
`usr/lib/murmur/cuda` beside the engine. Since 0.5.0 they no longer ship cuDNN
or NVRTC: the bundled CTranslate2 does not use them. A compatible host
NVIDIA driver is still required. Device Auto falls back to CPU when CUDA
initialization or inference fails, and stays there until the engine restarts or
the device configuration changes. Engine Settings shows a reason for a genuine
GPU failure; an ordinary CPU-only machine has no warning. A missing library
suggests an incomplete package; a driver warning calls for checking driver
compatibility. Forced CUDA reports the error instead of silently using CPU.
## The engine does not start

The AppImage, Debian package, and portable archive include the engine. It lives
in `usr/lib/murmur/engine/` (inside the AppImage, or under `/usr/lib/murmur/engine`
for the `.deb`). "Speech engine is not installed" means that folder is missing or
`MURMUR_ENGINE` points somewhere without `murmur-engine`; reinstall the package
or fix the variable.

Since 0.5.0 the engine is a folder, not a self-extracting file, so it no longer
needs free space or exec permission in the temporary directory. The app retries
a crashed engine once before requiring a restart.

## AppImage or package errors

If startup reports `GLIBC_2.39 not found`, the distribution is older than the
prebuilt binary's baseline. Current downloads are built on Ubuntu 24.04 and need
glibc 2.39 or newer. Build from source on the older distribution instead of
replacing its system C library. AppImage does not remove this requirement.

Try `--appimage-extract-and-run` if FUSE is unavailable. The AppImage bundles
WebKitGTK, GTK, audio client libraries, and typing tools. Since 0.5.0 the `.deb`
uses the distribution's packages instead (`apt` installs them as dependencies). The host still needs a working X11 desktop, audio services, and hardware
drivers. Report a missing application library as a packaging bug.

## The Flipper does not connect

Install the `.fap` matching your desktop release, open it on the device, and close
qFlipper. Use a USB data cable. In Companion settings, select USB and use Detect
devices. Check access to the serial device through your distribution's serial
port permissions. Do not run Murmur as root.

For Bluetooth, enable Bluetooth on both devices and keep the Flipper app open.
Try USB first to separate pairing issues from a protocol mismatch. A firmware API
mismatch needs a rebuilt `.fap`; see [the companion guide](../companion/README.md).

## Windows

Windows support has **not been tested on Windows hardware yet**; these answers
follow the code. Please report what actually happens.

**SmartScreen: "Windows protected your PC".** Murmur's Windows builds are not
code-signed (no Authenticode certificate yet), so SmartScreen warns about every
new download. Check the file first: in PowerShell,
`Get-FileHash .\Murmur_<ver>_x64-setup.exe` must equal the line in `SHA256SUMS`
from the same GitHub release. Only then choose *More info → Run anyway*. Do not
turn SmartScreen off.

**No microphone, or the level meter never moves.** Windows can block desktop
apps from the microphone without any error. Settings → Audio says which switch is
off and **Open Windows privacy settings** opens *Privacy & security →
Microphone*. Turn on *Microphone access*, *Let apps access your microphone* and
*Let desktop apps access your microphone*, then press **Refresh** in Audio.

**Text was held: "the window runs as administrator".** Windows does not let a
normal app type into an app started with *Run as administrator* (UIPI), and it
drops the keystrokes silently. Murmur detects this and holds the text: use
**Copy** and paste it yourself, or run the target app normally. Running Murmur
itself as administrator also works but is not recommended.

**Text was held: "a Windows security prompt or the lock screen had focus".**
A UAC prompt, the lock screen or Ctrl+Alt+Del was showing when the transcript
was ready. Dismiss it, focus your app and choose **Insert here**.

**"Return to the original window" did not switch back.** Windows decides when an
app may bring another window to the front; Murmur asks once
(`SetForegroundWindow`) and holds the text if Windows says no.

**WebView2.** Settings and the overlay need the Microsoft Edge WebView2 Runtime.
Windows 11 has it; the installer adds it on older Windows 10 (with an internet
connection). The portable zip cannot install it: get the *Evergreen Bootstrapper*
from Microsoft.

**Where are the files?** `%APPDATA%\Murmur\config.toml` and everything else under
`%LOCALAPPDATA%\Murmur` (log: `murmur.log`). See [configuration](configuration.md).

## Reporting a problem

Logs are under the Murmur data directory described in [configuration](configuration.md).
Quit Murmur and launch with `MURMUR_LOG=debug` for extra diagnostics. Treat logs as
sensitive and remove personal data before posting them. Include the release,
distribution, desktop, and the smallest set of steps that reproduces the failure.
