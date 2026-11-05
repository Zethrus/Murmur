Murmur 0.4.1 prepares the Linux and Flipper release for public use.

Changes:

- Linux text injection now owns its helper process instead of trusting a stale PID file.
- Temporary transcript files are private and removed after delivery.
- Debian packages declare the typing and focused-window utilities they need.
- Fork pull requests cannot execute the Flipper workflow on a configured self-hosted runner.
- Releases stay in draft until Linux and Flipper downloads pass validation.
- The README, install guides, platform schedule, and GPL version 3 license are included.

Linux x86_64 and the Flipper Zero companion are included in this release.

Download the `.deb`, AppImage, or portable `.tar.gz` for Linux. The portable archive
contains both required executables. The matching `.fap` goes in `apps/Tools` on
the Flipper's SD card. Check downloads against `SHA256SUMS` from this release.

Use an X11 desktop with tray support, `xdotool`, and `xprop`. Open Settings from
the tray, install a model in Engine, then select Use model. Hold Ctrl+Alt+D to
dictate. Models and NVIDIA CUDA libraries are not bundled; CPU inference is the
default path for these downloads. Packages are unsigned.

The Android APK is deferred, with November 6, 2026 as the target. Windows builds
are planned for later this week, tentatively November 6. Both depend on testing
and are excluded from this release.

See the README for installation and the roadmap for platform status.
