# Murmur 0.5.1 (beta)

The second beta of Murmur 0.5, with fixes found while testing the 0.5.0 beta.
Everything new in 0.5 (the first Windows build, much smaller Linux downloads and
downloadable components) is described under Murmur 0.5.0 below.

**This is a beta.** It is published as a GitHub pre-release for about a week of
testing on Linux and Windows. If no blocking problems turn up, these same files
become the official 0.5.1 release; a fix would ship as 0.5.2 instead. Please
report problems at <https://github.com/Zethrus/Murmur/issues> with your
platform, desktop (for example Windows 11, or GNOME on X11), how you installed
Murmur, and what you expected.

## Fixes since the 0.5.0 beta

- **"Speech engine not installed" no longer stays after setup.** When Murmur
  started without the speech engine, Settings kept showing "Speech engine not
  installed — open Settings" after first-run setup had installed it, until the
  first dictation or a restart. The message now clears as soon as the engine
  answers ([#30](https://github.com/Zethrus/Murmur/pull/30)).
- **Clearer download errors.** A failed component download now names the
  server's answer: a missing file says "The file is not on the download server
  (HTTP 404)" instead of "The download server rejected the request", and
  refusals, rate limits and server errors each have their own message.

## Known limitations

- **Check for updates fails during the beta.** Settings → Components → Check
  for updates reads the newest *official* release, which is still 0.4.7 and has
  no component list, so it reports an error. First-run setup and installing
  components are not affected: they use the signed list built into this
  version. The error goes away once 0.5.1 becomes the official release.
- **Windows uses the CPU in this beta.** The NVIDIA GPU option is hidden on
  Windows; Windows GPU inference passed in release testing but is not offered
  yet.
- **Windows is new.** All 48 automated desktop checks passed in a logged-in
  Windows session on the test machine (overlay, hotkeys, typing, tray, focus
  guards; [test ledger](parity/test-ledger.md)). Manual checks such as screen
  readers and mixed-DPI monitors are tracked in
  [the Windows checklist](parity/windows-checklist.md). Expect rough edges.
- **No Windows publisher signature.** The Windows installer and executable are
  intentionally unsigned with Authenticode, so Windows may show an unknown
  publisher or SmartScreen warning. The required OpenPGP signature protects the
  component manifest; it does not provide an Authenticode publisher identity.

## Downloads

- **Linux x86_64:** `Murmur_0.5.1_amd64.AppImage` (portable), `Murmur_0.5.1_amd64.deb`
  (Debian/Ubuntu: `sudo apt install ./Murmur_0.5.1_amd64.deb`), or
  `murmur-linux-x86_64.tar.gz` (the AppImage as `murmur`). If FUSE is
  unavailable, add `--appimage-extract-and-run`.
- **Windows 10 1809+ x64:** `Murmur_0.5.1_x64-setup.exe` (per-user installer, no
  administrator rights) or `murmur-windows-x86_64.zip` (portable). The files are
  not Authenticode-signed: first check that
  `Get-FileHash .\Murmur_0.5.1_x64-setup.exe` matches the line in `SHA256SUMS`,
  then choose *More info → Run anyway* at the SmartScreen prompt
  ([troubleshooting](troubleshooting.md#windows)).
- **Flipper Zero:** the matching companion `.fap`.
- **Do not download** the `murmur-engine-*`, `murmur-cuda-*` and
  `components-manifest*` files yourself: Murmur fetches and verifies them during
  setup. `GPU-VALIDATION*.json` is test evidence.

Verify downloads against `SHA256SUMS`. Quit an older running copy from its tray
menu before launching this version. Closing Settings keeps Murmur running; tray
→ Quit exits the application.

Linux needs x86_64, glibc 2.39 or newer, and an X11 desktop with working audio
services and hardware drivers; GPU inference needs a compatible NVIDIA driver.
Linux packages are unsigned. Android remains deferred.

# Murmur 0.5.0 (beta)

The first Windows build, and much smaller Linux downloads: the app packages no
longer contain the speech engine or the GPU runtime. Murmur downloads what you
choose, verifies it against a signed list, and installs it into your data
folder. Measured sizes are in [the size report](size-report.md).

Published as a beta pre-release and replaced by 0.5.1 before promotion.

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
- Settings no longer redraws the page while you are typing in one of its text
  fields, for example after the overlay is moved and its position is saved.

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
- **CI build changes.** Self-hosted Linux and Windows builds now keep separate,
  runner-scoped compiler and download caches. An opt-in controller can send
  queued ordinary PR and branch builds to hosted runners; GPU, desktop and
  release-tag jobs are excluded. The fallback has not been activated or
  validated live, and no build speedup has been measured. See
  [CI build performance](ci-build-performance.md).
- **Vendored Wry on Windows.** Windows builds use a pinned copy of Wry 0.57.2
  with one patch that lets the overlay's WebView2 receive a drag press while
  another Murmur window is in front (upstream:
  [tauri-apps/wry#1850](https://github.com/tauri-apps/wry/pull/1850)).
  Provenance, verification and removal steps are in `src-tauri/vendor/README.md`.

## Windows

- **Small per-user installer and portable zip** with the app only; the engine
  downloads on first run like on Linux. The installer needs no administrator
  rights and installs WebView2 if missing (that step needs an internet
  connection). Requires Windows 10 1809+ x64.
- **Windows engine and GPU components** in the signed manifest: the engine
  frozen from hash-locked win_amd64 wheels, and a cuBLAS-only GPU add-on
  (`cublas64_12.dll`, `cublasLt64_12.dll`). Release GPU inference is validated
  on an RTX 3060 (`GPU-VALIDATION-windows.json`); the GPU option stays hidden on
  Windows for this beta.
- **The overlay can be dragged with Settings in front.** Dragging the overlay
  icon no longer depends on which Murmur window is active, and it does not take
  keyboard focus from the app you are typing in.
- **Delivery guards.** Murmur is designed to hold text with Copy/Discard when
  the target is elevated, at a UAC prompt or at the lock screen; the overlay
  explains why.
- **Window identity** is the window handle plus its process and the process's
  start time, so a reused handle never counts as the original window.
- **Modifiers are released before typing**, and clicking the overlay's buttons
  no longer risks taking keyboard focus from the target app.
- **Microphone privacy:** when Windows blocks desktop apps from the microphone,
  Settings → Audio explains which switch is off and opens the right Windows
  Settings page.
- See [the focus contract](focus-contract.md) and
  [Windows troubleshooting](troubleshooting.md#windows).

## Behaviour change on all platforms

- With **When focus changes: Insert anyway**, a transcript whose target window
  cannot be identified is now held instead of failing with an error.

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
