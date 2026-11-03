# Murmur icon

The mark is a microphone capsule with a wave slit cut through it: the capsule says
push-to-talk dictation, the slit is the voice running through it. A muted cradle and
stem anchor it so the silhouette still reads as a microphone at 32 px.

- `murmur-icon.svg` — the app tile, `viewBox 0 0 1024 1024`, rounded-square background,
  used to generate every packaged size.
- `murmur-tray.svg` — the same mark as a single-colour glyph on transparency
  (`currentColor`) for panels that want a monochrome tray icon.

## Palette

| Role | Value |
| --- | --- |
| Tile background | `#10131a` (`--grey-950`) |
| Capsule | `#7aa2f7` (`--blue-400`) |
| Cradle and stem | `#a9b1d6`, muted so the capsule stays dominant |
| Inner slit | the tile background showing through, never a drawn colour |

The slit is negative space, so the icon needs no mask or knockout and stays clean
against any launch background.

## Regenerating the packaged sizes

```sh
inkscape docs/branding/murmur-icon.svg --export-type=png \
  --export-filename=/tmp/murmur-icon.png --export-width=1024 --export-height=1024
npx tauri icon /tmp/murmur-icon.png -o src-tauri/icons
```

`npx tauri icon` writes the PNG ladder, `icon.ico`, `icon.icns` and the Windows Store
and mobile assets in one pass. Re-run `./install.sh` afterwards so the hicolor theme
copies used by the desktop entry are refreshed.

## Small sizes

The microphone silhouette survives 16 px and 32 px; the wave slit softens into a
single dark band below 32 px. That is deliberate: at tray sizes the shape carries the
identity, and the slit becomes detail rather than the point.
