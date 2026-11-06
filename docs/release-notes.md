# Murmur 0.4.6

Dictation now stays bound to the window you started in. Switching windows
mid-sentence no longer sends a note into whatever app has focus when the
transcript is ready, such as a terminal.

- Murmur records the focused window when you press the dictation shortcut and
  checks it again before typing. If focus changed, the transcript is held in
  the overlay with Insert here, Copy, and Discard instead of being typed.
- Held text stays in memory only. It is cleared after two minutes, on Discard,
  or when the next dictation starts. Inserted text is formatted for the app it
  lands in, so terminals still receive a single line.
- Behavior → When focus changes offers Hold for review (default), Return to the
  original window, Copy to the clipboard, or Insert into the current window.
- An optional Insert held text shortcut and the Flipper Accept/Dismiss buttons
  act on held text. The Flipper protocol is unchanged.

Windows are compared by identity, so tabs inside one browser or terminal window
count as the same target. Switching windows while a long transcript is already
being typed is not detected.

- Downloads contain the AppImage, Debian package, portable archive, Flipper
  companion, GPU validation evidence, and checksums. Loose app and engine files
  are no longer posted separately.

Models download inside Settings. GPU inference requires a compatible host NVIDIA
driver.

Use the AppImage for a portable download, or the `.deb` for Debian/Ubuntu.
If FUSE is unavailable, add `--appimage-extract-and-run`. Verify downloads against
`SHA256SUMS`. Quit an older running copy from its tray menu before launching this
version. Closing Settings keeps Murmur running; tray → Quit exits the application.

The supported baseline is Linux x86_64, glibc 2.39 or newer, and an X11 desktop
with working audio services and hardware drivers. Packages are unsigned.
The matching Flipper Zero companion is included. Windows and Android downloads
remain deferred.
