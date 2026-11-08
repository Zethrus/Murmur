# Baseline captures: Murmur 0.4.7 AppImage (Linux)

Reference images for later Linux/Windows comparisons. They show the released
0.4.7 build, not this branch.

| Field | Value |
| --- | --- |
| Artifact | `Murmur_0.4.7_amd64.AppImage` from the v0.4.7 GitHub release, SHA-256 `123bec2cf23278450893b4a8e200dd10c5bfe84bec4fb27dfff50fd8c657f9a6` |
| Source commit | `1f1907d` (master at the 0.4.7 release, version 0.4.7) |
| Captured | 2026-11-06, by script (`import -window <id>`, ImageMagick 6.9.12) |
| Launch | `APPIMAGE_EXTRACT_AND_RUN=1`, throwaway `HOME`/`XDG_*` directories created with `mktemp` and deleted afterwards; no user config, models or history |
| Display | Private Xvfb `:91` (xvfb 2:21.1.12), 1280×800×24, `-dpi 96`, no window manager, no compositor |
| Scale | 100 % (no GDK scaling variables set) |
| Host | Ubuntu 24.04.5 LTS; WebKitGTK is the copy Tauri bundled into the AppImage from its build host (host package today: libwebkit2gtk-4.1-0 2.52.6) |
| Fonts | Default fontconfig resolution on the host: `sans-serif` → Noto Sans; Ubuntu font installed. The UI's own font stack decides the family shown |
| Window | Settings at its default 1000×780 logical size; only the first screen of each page is captured (no scrolling) |
| Config | `general.theme = "light"` or `"dark"`, `overlay.idle_hide = false`, everything else default; no model installed |

## Files

`linux-<theme>-<page>-100.png` for theme ∈ {light, dark} and page ∈ {general,
engine, audio, behavior, privacy, companion} (the six sidebar pages; the Enhance
section is further down the Behavior page, below the captured first screen), plus `linux-<theme>-overlay-idle-100.png`.
PNGs were losslessly recompressed with `optipng -o2 -strip all`.

## Observations (not verdicts)

- Every page shows "Read default microphone format": Xvfb has no audio input
  device. Expected in this headless setup; not a product bug by itself.
- The overlay idle capture is 144×66 and shows a grey rectangle with a
  truncated "mur" label. Without a compositor X11 cannot show transparency, so
  the background colour is an artefact; the clipped label needs checking on a
  real desktop before it is called a bug.
- Companion page (both themes): the status dot and "Companion disabled" sit
  outside the card's content padding, unlike other rows. Recorded for the
  parity review; not investigated in this PR.
- Interaction rows of the parity checklist (hotkeys, injection, overlay focus)
  cannot be judged from still images; see `docs/parity/test-ledger.md`.
