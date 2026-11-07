#!/usr/bin/env python3
"""User-file installation only: no builds, elevation, model downloads or app launch."""
import argparse
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
ICONS = {32: "32x32.png", 64: "64x64.png", 128: "128x128.png", 256: "128x128@2x.png"}


def safe(path):
    """Do not write/delete through symlinks or accept relative XDG roots."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Expected an absolute path without '..': {path}")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Refusing symlink path: {part}")
    return path


def locations():
    home = safe(Path(os.environ["HOME"]))
    if home == Path("/"):
        raise ValueError("HOME must not be /")
    config = safe(Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config"))
    data = safe(Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share"))
    prefix = home / ".local"
    return home, config, data, prefix


def engine_path():
    _, _, data, _ = locations()
    engine = safe(data / "murmur/engine")
    safe(engine / ".venv")
    safe(engine / "murmur_engine")
    return engine


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest() if hasattr(hashlib, "file_digest") else hash_stream(stream)


def hash_stream(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(block)
    return result.digest()


def put(destination, *, source=None, text=None, mode=0o644):
    safe(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".murmur-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            if source is not None:
                with source.open("rb") as stream:
                    shutil.copyfileobj(stream, output)
            else:
                if text is None:
                    raise ValueError("put requires source or text")
                output.write(text.encode("utf-8"))
        temporary.chmod(mode)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    if source is not None and digest(source) != digest(destination):
        raise RuntimeError(f"Copy verification failed: {destination}")
    if text is not None and destination.read_text() != text:
        raise RuntimeError(f"Text verification failed: {destination}")
    if destination.stat().st_mode & 0o777 != mode:
        raise RuntimeError(f"Permission verification failed: {destination}")
    print(f"Verified {destination}")


def desktop_exec(path):
    # Exec quoting is parsed after desktop-entry string escaping; '%' is a field code.
    value = str(path).replace("%", "%%")
    for char in ("\\", '"', "`", "$"):
        value = value.replace(char, "\\" + char)
    value = value.replace("\\", "\\\\")
    if any(char in value for char in "\n\r\t"):
        raise ValueError("Control characters are not supported in installation paths")
    return f'"{value}"'


def refresh(prefix, data):
    commands = [
        ["update-desktop-database", str(data / "applications")],
        ["gtk-update-icon-cache", "-f", "-t", str(prefix / "share/icons/hicolor")],
    ]
    for command in commands:
        if shutil.which(command[0]) and Path(command[-1]).is_dir():
            if subprocess.run(command, check=False).returncode:
                print(f"Warning: optional cache refresh failed: {command[0]}", file=sys.stderr)


def install(binary, sidecar):
    home, config, data, prefix = locations()
    app = prefix / "share/murmur"
    launcher = prefix / "bin/murmur"
    entry = (
        "[Desktop Entry]\nType=Application\nName=Murmur\n"
        "Comment=Private local dictation\n"
        f"Exec={desktop_exec(launcher)}\nIcon=murmur\nTerminal=false\n"
        "Categories=Utility;Accessibility;\nStartupNotify=false\n"
    )
    sources = [binary, sidecar, *(ROOT / "src-tauri/icons" / name for name in ICONS.values())]
    for source in sources:
        if not source.is_file() or not source.stat().st_size:
            raise ValueError(f"Missing or empty build input: {source}")
    if not all(os.access(path, os.X_OK) for path in (binary, sidecar)):
        raise ValueError("App and sidecar must be executable")
    # The app finds its engine at <exe dir>/../lib/murmur/engine (src-tauri/src/engine.rs).
    engine = app / "lib/murmur/engine/murmur-engine"
    # Validate every destination before writing anything.
    targets = [app / "bin/murmur", engine, app / "bin/murmur-engine", launcher,
               data / "applications/murmur.desktop", config / "autostart/murmur.desktop",
               config / "murmur/config.toml"]
    for size in ICONS:
        targets += [app / f"icons/{size}x{size}/murmur.png",
                    prefix / f"share/icons/hicolor/{size}x{size}/apps/murmur.png"]
    for target in targets:
        safe(target)
    put(app / "bin/murmur", source=binary, mode=0o755)
    put(engine, source=sidecar, mode=0o755)
    # Launcher location used by 0.4.x source installs; the app no longer looks there.
    (app / "bin/murmur-engine").unlink(missing_ok=True)
    put(launcher, text=f'#!/bin/sh\nexec {shlex.quote(str(app / "bin/murmur"))} "$@"\n', mode=0o755)
    for size, name in ICONS.items():
        source = ROOT / "src-tauri/icons" / name
        put(app / f"icons/{size}x{size}/murmur.png", source=source)
        put(prefix / f"share/icons/hicolor/{size}x{size}/apps/murmur.png", source=source)
    put(data / "applications/murmur.desktop", text=entry)
    autostart_entry = entry.replace(
        f"Exec={desktop_exec(launcher)}\n", f"Exec={desktop_exec(launcher)} --background\n")
    put(config / "autostart/murmur.desktop", text=autostart_entry + "X-GNOME-Autostart-enabled=true\n")
    config_file = config / "murmur/config.toml"
    if not config_file.exists():
        put(config_file, text="[general]\nautostart = true\n", mode=0o600)
    else:
        print("Existing config preserved; its autostart setting takes precedence when Murmur starts.")
    refresh(prefix, data)


def uninstall(purge):
    home, config, data, prefix = locations()
    app = prefix / "share/murmur"
    files = [prefix / "bin/murmur"]
    for base in {data, prefix / "share"}:
        files.append(base / "applications/murmur.desktop")
    # The plugin may use the product name instead of the lowercase executable name.
    for base in {config, home / ".config"}:
        files += [base / "autostart" / name for name in ("murmur.desktop", "Murmur.desktop")]
    files += list((prefix / "share/icons/hicolor").glob("*/apps/murmur.png"))
    directories = [app / "bin", app / "lib", app / "icons"]
    if purge:
        directories = list({app, data / "murmur", config / "murmur", home / ".config/murmur"})
    for path in files + directories:
        safe(path)
    for path in files:
        path.unlink(missing_ok=True)
        if path.exists():
            raise RuntimeError(f"Removal failed: {path}")
    for path in directories:
        if path.exists():
            shutil.rmtree(path)
        if path.exists():
            raise RuntimeError(f"Removal failed: {path}")
    if app.is_dir() and not any(app.iterdir()):
        app.rmdir()
    refresh(prefix, data)
    print("Removed installation and config/models/history/logs." if purge else
          "Removed installation; config, models, history and logs were kept.")
    print("Repository sources, build outputs and engine/.venv were not removed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install_args = commands.add_parser("install")
    install_args.add_argument("--binary", type=Path, required=True)
    install_args.add_argument("--sidecar", type=Path, required=True)
    commands.add_parser("engine-path")
    remove_args = commands.add_parser("uninstall")
    remove_args.add_argument("--purge", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() == 0:
        parser.error("Use a regular Linux user, not root")
    try:
        if args.command == "engine-path":
            print(engine_path())
        elif args.command == "install":
            install(args.binary, args.sidecar)
        else:
            uninstall(args.purge)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Murmur: {error}\n")


if __name__ == "__main__":
    main()
