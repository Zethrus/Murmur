Murmur 0.4.3 opens Settings when you launch the app.

- Manual launches show Settings immediately, including when Murmur is already running.
- Starting at login stays in the background. Login entries now pass `--background`.
- Existing enabled login entries are refreshed when the app starts. Duplicate legacy entries are removed.
- Linux login entries respect XDG_CONFIG_HOME and use the bundled launcher for Debian installs.
- Release checks now launch the AppImage and verify window visibility for manual and background startup.
- Downloads contain the AppImage, Debian package, portable archive, Flipper companion, and checksums. Loose app and engine files are no longer posted separately.

If an older copy is running, quit it from the tray before launching this version.
Closing Settings keeps Murmur running; Quit in the tray exits the application.

Linux AppImage, Debian, and portable downloads include the CPU runtime and typing
tools. Models download inside Settings. No separate Python, WebKitGTK, xdotool,
or xprop installation is needed. CUDA is optional and is not bundled.

Use the AppImage for a portable download, or the `.deb` for Debian/Ubuntu.
If FUSE is unavailable, add `--appimage-extract-and-run` when launching the AppImage.
Verify downloads against `SHA256SUMS`.

The supported baseline is Linux x86_64, glibc 2.39 or newer, and an X11 desktop
with working audio services and hardware drivers. Packages are unsigned.
The matching Flipper Zero companion is included. Android is planned for November 6.
Windows is deferred until November 6 or 7, depending on testing.
