"""Deterministic component zips shared by the Linux and Windows release builders."""
import os
from pathlib import Path
import shutil
import zipfile

# Fixed timestamp: the same inputs give byte-identical archives.
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def write_zip(source, destination):
    """Deterministic LZMA zip of a directory tree; the component manager refuses links, so
    PyInstaller's convenience symlinks to files elsewhere in the tree are left out (the real
    file ships at its own path). A link that leaves the tree or points at nothing fails."""
    source = Path(source).resolve()
    destination = Path(destination)
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
            # Windows reports every file as executable; the mode only matters on Linux.
            executable = os.name != "nt" and os.access(path, os.X_OK)
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            info.compress_type = zipfile.ZIP_LZMA
            with path.open("rb") as stream, archive.open(info, "w", force_zip64=path.stat().st_size > 2**31) as out:
                shutil.copyfileobj(stream, out, 1 << 20)
    temporary.replace(destination)
    return destination
