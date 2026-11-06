# Murmur 0.4.4

Linux desktop downloads now bundle pinned CUDA userspace libraries for compatible
NVIDIA drivers. CPU-only machines continue to work.

- AppImage, Debian, and portable packages include the CUDA runtime alongside the
  CPU engine, typing tools, and desktop client libraries.
- Engine Settings reports automatic GPU fallback. Engine check runs real
  inference even on its silent probe.
- Manual launches open Settings, including when Murmur is already running.
  Starting at login stays in the background, and existing login entries are
  refreshed while preserving the autostart preference.
- Release checks validate AppImage startup and GPU inference against the final
  asset hashes.

Models download inside Settings. The standalone engine download remains CPU-only
unless a compatible CUDA userspace runtime is provided separately. GPU inference
requires a compatible host NVIDIA driver.

Use the AppImage for a portable download, or the `.deb` for Debian/Ubuntu.
If FUSE is unavailable, add `--appimage-extract-and-run`. Verify downloads against
`SHA256SUMS`. Quit an older running copy from its tray menu before launching this
version. Closing Settings keeps Murmur running; tray → Quit exits the application.

The supported baseline is Linux x86_64, glibc 2.39 or newer, and an X11 desktop
with working audio services and hardware drivers. Packages are unsigned.
The matching Flipper Zero companion is included. Windows and Android downloads
remain deferred.
