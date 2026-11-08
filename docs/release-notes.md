# Murmur 0.5.0 (unreleased)

Much smaller Linux downloads: the app packages no longer contain the speech
engine or the GPU runtime. Murmur downloads what you choose, verifies it against
a signed list, and installs it into your data folder. Measured sizes are in
[the size report](size-report.md).

## Components and first-run setup

- **Setup on first start.** Choose English-only or multilingual, a speech model
  (with a recommendation from your memory, CPU and NVIDIA GPU), and CPU or GPU.
  The summary shows download size, installed size, temporary space and free disk
  space; nothing downloads until you click **Download and install**. **Set up
  later** (or closing the window) is remembered; Settings then offers **Finish
  setup**.
- **Settings → Components** replaces Settings → Engine: install, pause, cancel,
  resume, verify and remove the speech engine, the NVIDIA GPU add-on, speech
  models and the Enhance model, with live progress. Removing something still in
  use is refused with the reason. The inference controls moved there unchanged.
- **Signed and pinned.** Every file is checked against a manifest signed with
  Murmur's release key (OpenPGP, pinned fingerprint, rollback and expiry
  protection). Speech models come from Hugging Face at a pinned commit with a
  SHA-256 per file; downloads resume where they stopped. See
  [components](components.md) and [privacy](privacy.md).
- **Your models carry over.** Models downloaded by 0.4.x are hashed once; the
  ones that match are adopted without downloading again. Others are shown as
  unverified and not used; nothing is deleted.
- **No engine yet?** Murmur still starts, the tray works, and the overlay says
  "Speech engine not installed — open Settings". Installing the engine makes it
  usable without restarting Murmur.

## Always know where the overlay is

- Between dictations the overlay now shrinks to a small, translucent copy of
  the app icon instead of disappearing. Drag the icon anywhere to move the
  overlay; dictation, suggestions and held text still use the full pill.
- General → When idle offers Small app icon (default), Full pill, or Nothing.
  It replaces the Hide when idle switch. Existing installs that turned hiding
  off keep the full pill.

## Smaller Linux packages

- **No cuDNN or NVRTC.** CTranslate2 runs Whisper on NVIDIA GPUs with cuBLAS
  alone; the GPU add-on carries only `libcublas.so.12` and `libcublasLt.so.12`.
  The build fails if a future CTranslate2 starts referencing cuDNN or NVRTC.
- **Leaner engine.** A folder instead of a self-extracting file (no unpacking
  into your temporary directory), without PyAV/FFmpeg or hf_xet.
- **Stripped binaries, symlinked libraries, zstd AppImage.**
- **The `.deb` uses your system's libraries** and installs only `/usr/bin/murmur`
  and its desktop files; `apt install ./Murmur_*.deb` pulls in WebKitGTK, GTK and
  xdotool. The old `/opt/murmur` runtime is gone.
- **The portable archive is just the AppImage** (as `murmur`).

## For packagers and developers

- `MURMUR_ENGINE` selects the engine executable (or its folder);
  `MURMUR_CUDA_DIR` adds a CUDA folder to the engine's DLL search path on
  Windows; `MURMUR_DEV_TRUST_ROOT` (+ acknowledgement in release builds) trusts
  a test signing key. See [configuration](configuration.md#environment-variables).
- Release assets have size budgets enforced by `scripts/check-release.py`:
  AppImage and `.deb` 125 MiB, tar.gz 130 MiB, engine zip 130 MiB, GPU add-on
  520 MiB.
- The engine's `models.install` RPC now fails: installs are the app's job.

## Known limitations

- **Blocked on the release key.** Until the owner creates Murmur's offline
  release key ([signing](signing.md)), builds refuse to install components
  ("Release trust root missing") and tag builds upload nothing.
- Windows builds remain unverified on Windows hardware; Windows engine and GPU
  components are not built yet, and the GPU option stays hidden on Windows.

# Murmur 0.4.7

Everything new since Murmur 0.4.5: dictation that stays in the window you
started in, a working Debian package, reliable overlay dragging, and a Flipper
companion ready for the Apps Catalog.

## Dictation stays in the window you started in

Switching windows mid-sentence no longer sends a note into whatever app has
focus when the transcript is ready, such as a terminal.

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

## Fixes

- **Debian package launches for every user.** Installing the `.deb` no longer
  leaves `/opt/murmur` readable only by root, which made `murmur` fail with
  "Permission denied" for desktop users. The launcher also starts from the
  application directory so its web view helpers are found.
- **Overlay dragging works reliably on GNOME/X11.** The overlay can be moved
  again under GNOME, and repeated drags work instead of only the first one.
  Pausing with the mouse held no longer ends a drag. The drag handle is larger
  and no longer selects text.
- **Overlay position is saved without surprises.** The final position is saved
  after you release the mouse, and the overlay no longer resizes or jumps while
  it is being dragged. Dictated text waits until a drag finishes before typing.
- **Manual config edits are kept.** Edits you make to the configuration file
  are no longer overwritten when the overlay saves its position. An invalid
  configuration file is reported instead of being replaced.

## Flipper Zero companion

- The companion now has an app icon, a description, and a changelog, ready for
  the Flipper Apps Catalog. Its version matches Murmur 0.4.7.

## Downloads

- Downloads contain the AppImage, Debian package, portable archive, Flipper
  companion, GPU validation evidence, and checksums.

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
