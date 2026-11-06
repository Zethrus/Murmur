#!/usr/bin/env python3
"""Finish Tauri's AppImage and derive every desktop download from that runtime.

Run on the same Debian/Ubuntu machine that built the application. glibc and
hardware drivers remain host components; Python is already in the engine.
"""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
from linux_cuda_runtime import stage as stage_cuda, check_asset_sizes

# glibc must stay together with the host loader and NSS configuration.
HOST_LIBRARIES = re.compile(r"^(?:ld-linux.*|lib(?:c|m|pthread|dl|rt|resolv|util|anl)\.so\..*)$")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def dependencies(binary):
    output = run("ldd", str(binary), capture_output=True).stdout
    if "not found" in output:
        raise RuntimeError(f"Unresolved dependency in {binary}:\n{output}")
    for line in output.splitlines():
        match = re.match(r"\s*(\S+) => (/\S+) ", line)
        if match and not HOST_LIBRARIES.fullmatch(match[1]):
            yield match[1], Path(match[2])


def bundle_libraries(appdir, binaries):
    library_dir = appdir / "usr/lib"
    notices = appdir / "usr/share/doc/murmur/bundled-runtime"
    notices.mkdir(parents=True, exist_ok=True)
    packages = set()
    for binary in binaries:
        for name, source in dependencies(binary):
            destination = library_dir / name
            if not destination.exists():
                shutil.copy2(source, destination)
                # Keep the distribution's copyright/license notice for added libraries.
                owner = run("dpkg-query", "-S", str(source.resolve()), capture_output=True).stdout
                packages.add(owner.split(": ", 1)[0].split(":", 1)[0])
    packages.update(("xdotool", "x11-utils", "libasound2-data", "fonts-dejavu-core"))
    for package in sorted(packages):
        copyright_file = Path("/usr/share/doc") / package / "copyright"
        if not copyright_file.is_file():
            raise RuntimeError(f"Missing runtime license notice: {package}")
        shutil.copy2(copyright_file, notices / f"{package}.copyright")
    (notices / "packages.txt").write_text(run(
        "dpkg-query", "-W", "-f=${Package} ${Version}\n", *sorted(packages),
        capture_output=True).stdout)


def package_portable(image, engine, output):
    # Keep loose executables inside the portable archive, not as release assets.
    with tarfile.open(output / "murmur-linux-x86_64.tar.gz", "w:gz", dereference=True) as archive:
        archive.add(image, arcname="murmur")
        archive.add(engine, arcname="murmur-engine")
    # A reused output directory may contain assets staged by older scripts.
    for name in ("murmur", "murmur-engine"):
        (output / name).unlink(missing_ok=True)


def package(image, deb, output, lock):
    image, deb, output = image.resolve(), deb.resolve(), output.resolve()
    if image.parent == output or deb.parent == output:
        raise ValueError("Output directory must differ from the original bundles")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="murmur-runtime-", dir=output.parent) as temporary:
        work = Path(temporary)
        image.chmod(0o755)
        run(str(image), "--appimage-extract", cwd=work, stdout=subprocess.DEVNULL)
        appdir = work / "squashfs-root"
        binaries = [appdir / "usr/bin/murmur"]
        for name in ("xdotool", "xprop"):
            source = shutil.which(name)
            if not source:
                raise RuntimeError(f"Build machine needs {name}")
            target = appdir / "usr/bin" / name
            shutil.copy2(source, target)
            binaries.append(Path(source))
        bundle_libraries(appdir, binaries)
        stage_cuda(appdir, lock)
        shutil.copytree('/usr/share/alsa', appdir / 'usr/share/alsa', symlinks=False)
        fonts = appdir / 'usr/share/fonts'
        fonts.mkdir(exist_ok=True)
        shutil.copy2('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', fonts)
        (fonts / 'fonts.conf').write_text('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n<fontconfig><dir prefix="relative">.</dir><dir>/usr/share/fonts</dir>\n<dir>/usr/local/share/fonts</dir><dir prefix="xdg">fonts</dir>\n<cachedir prefix="xdg">fontconfig</cachedir></fontconfig>\n')
        # AppRun's normal environment is set after GTK's hook. Set these first so
        # the hook and every child process can also find the shipped libraries.
        original = appdir / "AppRun"
        original.rename(appdir / "AppRun.tauri")
        original.write_text('''#!/bin/sh
set -eu
APPDIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export APPDIR
export PATH="$APPDIR/usr/bin:$PATH"
export LD_LIBRARY_PATH="$APPDIR/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
if [ ! -f /usr/share/alsa/alsa.conf ]; then
    export ALSA_CONFIG_DIR="$APPDIR/usr/share/alsa"
    export ALSA_CONFIG_PATH="$ALSA_CONFIG_DIR/alsa.conf"
fi
if [ ! -f /etc/fonts/fonts.conf ]; then
    export FONTCONFIG_FILE="$APPDIR/usr/share/fonts/fonts.conf"
fi
exec "$APPDIR/AppRun.tauri" "$@"
''')
        original.chmod(0o755)
        # Reuse the unsigned Type 2 runtime shipped by Tauri; replace its SquashFS.
        offset = int(run(str(image), "--appimage-offset", capture_output=True).stdout.strip())
        squash = work / "runtime.squashfs"
        run("mksquashfs", str(appdir), str(squash), "-noappend", "-comp", "gzip",
            "-root-owned", "-processors", "2", stdout=subprocess.DEVNULL)
        packaged = output / image.name
        with image.open("rb") as source, packaged.open("wb") as destination:
            runtime = source.read(offset)
            if len(runtime) != offset or runtime[:4] != b"\x7fELF":
                raise RuntimeError("Invalid AppImage runtime")
            destination.write(runtime)
            with squash.open("rb") as filesystem:
                shutil.copyfileobj(filesystem, destination)
        # The filesystem is now embedded in the AppImage; release its disk space.
        squash.unlink()
        packaged.chmod(0o755)
        package_portable(packaged, appdir / "usr/bin/murmur-engine", output)
        # Install the extracted runtime so Debian users need neither FUSE nor
        # extraction on every launch. Preserve Tauri's desktop integration.
        package_dir = work / "deb"
        run("dpkg-deb", "-R", str(deb), str(package_dir), stdout=subprocess.DEVNULL)
        for name in ("murmur", "murmur-engine"):
            (package_dir / "usr/bin" / name).unlink(missing_ok=True)
        runtime_dir = package_dir / "opt/murmur"
        runtime_dir.parent.mkdir(parents=True, exist_ok=True)
        # Both paths share the temporary filesystem. Move the completed runtime
        # instead of duplicating several GiB of CUDA libraries for Debian.
        appdir.rename(runtime_dir)
        launcher = package_dir / "usr/bin/murmur"
        launcher.write_text('#!/bin/sh\nexec /opt/murmur/AppRun "$@"\n')
        launcher.chmod(0o755)
        control = package_dir / "DEBIAN/control"
        metadata = control.read_text()
        metadata = re.sub(r"^Depends:.*$", "Depends: libc6 (>= 2.39), bash, coreutils", metadata, flags=re.M)
        size = sum(p.stat().st_size for p in package_dir.rglob("*") if p.is_file()) // 1024
        metadata = re.sub(r"^Installed-Size:.*$", f"Installed-Size: {size}", metadata, flags=re.M)
        control.write_text(metadata)
        # Original checksums describe files replaced above.
        (package_dir / "DEBIAN/md5sums").unlink(missing_ok=True)
        run("dpkg-deb", "--root-owner-group", "-Zgzip", "-b", str(package_dir), str(output / deb.name))
        check_asset_sizes(output.iterdir())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--appimage", type=Path, required=True)
    parser.add_argument("--deb", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=Path(__file__).resolve().parents[1] / "engine/requirements-linux-release.txt")
    args = parser.parse_args()
    package(args.appimage, args.deb, args.output, args.lock)
