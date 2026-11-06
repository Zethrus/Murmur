Murmur 0.4.2 bundles the Linux application runtime.

- AppImage, Debian, and portable downloads include the typing tools and client libraries.
- Python and the CPU speech engine are included. No separate Python, WebKitGTK, xdotool, or xprop installation is needed.
- The standalone `murmur` download is now the full AppImage too.
- Debian installs the extracted runtime, so it does not need FUSE.
- Models download from Settings when you choose Install. CUDA remains optional and is not bundled.

Choose the AppImage for a portable download, or the `.deb` for Debian/Ubuntu.
Make the AppImage executable and launch it. If FUSE is unavailable, add
`--appimage-extract-and-run`. Verify downloads with `SHA256SUMS`.

A Linux x86_64 X11 desktop, glibc 2.39 or newer, working audio services and hardware
drivers are still required. The build baseline is Ubuntu 24.04. Packages are unsigned.

The matching Flipper Zero `.fap` is included. Android is planned for November 6.
Windows is deferred until November 6 or 7, depending on testing. Neither is part
of this Linux release.
