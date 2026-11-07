#!/usr/bin/env python3
"""Build the Linux release: slim app packages plus downloadable component zips.

  components  onedir engine -> murmur-engine-linux-x86_64-<ver>.zip
              pinned cuBLAS -> murmur-cuda-linux-x86_64-<ver>.zip
  package     Tauri AppImage/.deb -> finished AppImage, .deb, tar.gz (no engine, no CUDA)

Run on the same Debian/Ubuntu machine that built the application. glibc and
hardware drivers remain host components; Python is inside the onedir engine.
The app installs the engine and CUDA add-on into ~/.local/share/murmur/components
after verifying them against the signed manifest (docs/components.md).

The AppImage carries the app, WebKitGTK client libraries and typing tools; the .deb
uses the system packages named in tauri.conf.json instead.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from linux_cuda_runtime import CUDA, DOC, stage as stage_cuda

ENGINE_ZIP = "murmur-engine-linux-x86_64-{version}.zip"
CUDA_ZIP = "murmur-cuda-linux-x86_64-{version}.zip"
# Fixed timestamp: the same inputs give byte-identical archives.
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
CHECK_RELEASE = Path(__file__).with_name("check-release.py")
SHARED_LIBRARY = re.compile(r"^lib.+\.so(?:\.\d+)*$")

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


def _soname(path):
    result = subprocess.run(["readelf", "-d", str(path)], capture_output=True, text=True)
    match = re.search(r"\(SONAME\).*\[([^]]+)\]", result.stdout) if result.returncode == 0 else None
    return match[1] if match else None


def _digest(path, algorithm="sha256"):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).digest()


def dedupe_libraries(root):
    """Replace identical copies of a shared library with symlinks to one file.

    Tauri's AppImage tooling copies libfoo.so, libfoo.so.0 and libfoo.so.0.1.2
    as separate files. Keep the SONAME (what the loader asks for) as the real
    file; fall back to the shortest versioned name. Returns replaced paths.
    """
    groups = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and SHARED_LIBRARY.match(path.name):
            # Key on the library stem too: identical bytes under unrelated names stay separate files.
            stem = path.name.split(".so", 1)[0]
            groups.setdefault((path.parent, stem, path.stat().st_size, _digest(path)), []).append(path)
    replaced = []
    for copies in groups.values():
        if len(copies) < 2:
            continue
        names = {path.name: path for path in copies}
        keep = names.get(_soname(copies[0])) or min(
            copies, key=lambda path: (not re.search(r"\.so\.\d+$", path.name), len(path.name), path.name))
        for path in copies:
            if path != keep:
                path.unlink()
                path.symlink_to(keep.name)
                replaced.append(path)
    return replaced


def check_engine(engine):
    """Refuse anything that is not a complete onedir freeze; run the cuDNN/NVRTC gate."""
    executable = engine / "murmur-engine"
    if not executable.is_file() or not os.access(executable, os.X_OK) or not (engine / "_internal").is_dir():
        raise RuntimeError(f"Not a onedir frozen engine: {engine}")
    subprocess.run([sys.executable, str(Path(__file__).with_name("check-engine-imports.py")), str(engine)],
                   check=True)


def write_zip(source, destination):
    """Deterministic LZMA zip of a directory tree; the component manager refuses links, so
    PyInstaller's convenience symlinks to files elsewhere in the tree are left out (the real
    file ships at its own path). A link that leaves the tree or points at nothing fails."""
    source = source.resolve()
    files = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            target = path.resolve()
            if not target.is_relative_to(source) or not target.is_file():
                raise RuntimeError(f"Unsupported link in component: {relative}")
            continue
        if path.is_file():
            files.append((relative, path))
    temporary = destination.with_suffix(".zip.tmp")
    # LZMA: the CUDA add-on is 335 MiB this way against 554 MiB with deflate.
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_LZMA) as archive:
        for relative, path in files:
            info = zipfile.ZipInfo(relative, ZIP_TIME)
            info.create_system = 3
            executable = os.access(path, os.X_OK)
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            info.compress_type = zipfile.ZIP_LZMA
            with path.open("rb") as stream, archive.open(info, "w", force_zip64=path.stat().st_size > 2**31) as out:
                shutil.copyfileobj(stream, out, 1 << 20)
    temporary.replace(destination)
    return destination


def build_components(engine, version, output, lock):
    """Engine and CUDA add-on zips for the component manifest (make-manifest.py)."""
    engine, output = engine.resolve(), output.resolve()
    check_engine(engine)
    output.mkdir(parents=True, exist_ok=True)
    engine_zip = write_zip(engine, output / ENGINE_ZIP.format(version=version))
    with tempfile.TemporaryDirectory(prefix="murmur-cuda-", dir=output.parent) as temporary:
        root = Path(temporary)
        stage_cuda(root, lock)
        flat = root / "cuda"
        shutil.copytree(root / CUDA, flat, symlinks=False)
        for notice in [*(root / DOC).glob("nvidia-*"), root / DOC / "cuda-manifest.json"]:
            shutil.copy2(notice, flat / notice.name)
        cuda_zip = write_zip(flat, output / CUDA_ZIP.format(version=version))
    return engine_zip, cuda_zip


def package_portable(image, output):
    """The portable archive is the AppImage named murmur."""
    with tarfile.open(output / "murmur-linux-x86_64.tar.gz", "w:gz", dereference=True) as archive:
        archive.add(image, arcname="murmur")
    # A reused output directory may contain assets staged by older scripts.
    for name in ("murmur", "murmur-engine"):
        (output / name).unlink(missing_ok=True)


def open_directories(root):
    """dpkg installs directory modes as packaged; make every directory traversable."""
    for directory, _, _ in os.walk(root):
        os.chmod(directory, 0o755)


def finish_debian(package_dir):
    """The .deb holds only the app; engine and CUDA are components. Refresh md5sums."""
    open_directories(package_dir / "usr")
    (package_dir / "DEBIAN/md5sums").write_text("".join(
        f"{_digest(path, 'md5').hex()}  {path.relative_to(package_dir)}\n"
        for path in sorted(package_dir.rglob("*"))
        if path.is_file() and not path.is_symlink() and "DEBIAN" not in path.relative_to(package_dir).parts))


def squash(appdir, destination):
    """zstd SquashFS with 1 MiB blocks; the type-2 runtime Tauri ships mounts and extracts it.

    Measured on this AppDir: zstd-19 with 128K blocks 703.7 MiB, with 1M blocks
    646.6 MiB; xz with 1M blocks 614.2 MiB. zstd keeps decompression fast for the
    750 MB cuBLASLt that GPU start-up reads through FUSE.
    """
    processors = os.environ.get("MURMUR_SQUASHFS_PROCESSORS", "2")
    run("mksquashfs", str(appdir), str(destination), "-noappend", "-comp", "zstd",
        "-Xcompression-level", "19", "-b", "1M", "-root-owned", "-processors", processors,
        stdout=subprocess.DEVNULL)


def package(image, deb, output):
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
        dedupe_libraries(appdir / "usr/lib")
        # Reuse the unsigned Type 2 runtime shipped by Tauri; replace its SquashFS.
        offset = int(run(str(image), "--appimage-offset", capture_output=True).stdout.strip())
        filesystem = work / "runtime.squashfs"
        squash(appdir, filesystem)
        packaged = output / image.name
        with image.open("rb") as source, packaged.open("wb") as destination:
            runtime = source.read(offset)
            if len(runtime) != offset or runtime[:4] != b"\x7fELF":
                raise RuntimeError("Invalid AppImage runtime")
            destination.write(runtime)
            with filesystem.open("rb") as stream:
                shutil.copyfileobj(stream, destination)
        # The filesystem is now embedded in the AppImage; release its disk space.
        filesystem.unlink()
        packaged.chmod(0o755)
        package_portable(packaged, output)
        # Debian: Tauri's package (app, desktop entry, system Depends), xz-compressed.
        # WebKitGTK, xdotool and x11-utils come from the system.
        package_dir = work / "deb"
        run("dpkg-deb", "-R", str(deb), str(package_dir), stdout=subprocess.DEVNULL)
        finish_debian(package_dir)
        control = package_dir / "DEBIAN/control"
        size = sum(p.lstat().st_size for p in package_dir.rglob("*")
                   if p.is_file() and "DEBIAN" not in p.relative_to(package_dir).parts) // 1024
        control.write_text(re.sub(r"^Installed-Size:.*$", f"Installed-Size: {size}", control.read_text(), flags=re.M))
        run("dpkg-deb", "--root-owner-group", "-Zxz", "-b", str(package_dir), str(output / deb.name))
        run(sys.executable, str(CHECK_RELEASE), "budgets", str(output))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    components = commands.add_parser("components", help="engine and CUDA add-on zips")
    components.add_argument("--engine", type=Path, required=True, help="onedir frozen engine (engine/dist/murmur-engine)")
    components.add_argument("--version", required=True)
    components.add_argument("--output", type=Path, required=True)
    components.add_argument("--lock", type=Path, default=Path(__file__).resolve().parents[1] / "engine/requirements-linux-release.txt")
    packages = commands.add_parser("package", help="AppImage, .deb and tar.gz without engine or CUDA")
    packages.add_argument("--appimage", type=Path, required=True)
    packages.add_argument("--deb", type=Path, required=True)
    packages.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "components":
        for path in build_components(args.engine, args.version, args.output, args.lock):
            print(path)
    else:
        package(args.appimage, args.deb, args.output)
