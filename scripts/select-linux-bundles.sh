#!/usr/bin/env bash
# Source this file, then select the Tauri bundles for the version being built.
select_linux_bundles() {
  local release="$1" version="$2" artifact
  LINUX_APPIMAGE="$release/bundle/appimage/Murmur_${version}_amd64.AppImage"
  LINUX_DEB="$release/bundle/deb/Murmur_${version}_amd64.deb"
  for artifact in "$LINUX_APPIMAGE" "$LINUX_DEB"; do
    if [[ ! -f "$artifact" || ! -s "$artifact" || -L "$artifact" ]]; then
      printf 'Missing current Linux bundle: %s\n' "$artifact" >&2
      return 1
    fi
  done
}
