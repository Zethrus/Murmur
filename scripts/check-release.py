#!/usr/bin/env python3
"""Validate release versions, the required Linux/Flipper download set and size budgets."""
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
MiB = 1024 ** 2
# Interim budgets while the engine and cuBLAS still ship inside the Linux packages.
# cuBLAS alone compresses to ~446 MiB, so the 400 MiB first proposed cannot hold;
# these are the measured sizes (AppImage 646.6, .deb 508.9 MiB) plus ~8 % headroom.
# PR-B (component manager) moves both out and lowers these to 125/125/130 MiB.
APPIMAGE_BUDGET = 700 * MiB
DEB_BUDGET = 550 * MiB
TARBALL_BUDGET = 710 * MiB
BUDGETS = {
    ".AppImage": APPIMAGE_BUDGET,
    ".deb": DEB_BUDGET,
    ".tar.gz": TARBALL_BUDGET,
    ".fap": 2 * MiB,
    ".json": 1 * MiB,
}
UNBUDGETED = {"SHA256SUMS"}


def budget(path):
    for suffix, limit in BUDGETS.items():
        if path.name.endswith(suffix):
            return limit
    raise ValueError(f"No size budget for release file: {path.name}")


def check_budgets(paths):
    """Fail closed: every release file needs a budget and must stay within it."""
    for path in paths:
        if path.name in UNBUDGETED:
            continue
        size, limit = path.stat().st_size, budget(path)
        if size > limit:
            raise ValueError(f"{path.name} is {size / MiB:.1f} MiB, over its {limit / MiB:.0f} MiB budget")


def appimage_offset(path):
    """The SquashFS starts where the runtime's ELF section header table ends."""
    with path.open("rb") as stream:
        header = stream.read(64)
    if header[:4] != b"\x7fELF" or header[4] != 2:
        raise ValueError(f"Not a 64-bit ELF AppImage runtime: {path.name}")
    shoff, = struct.unpack_from("<Q", header, 0x28)
    shentsize, shnum = struct.unpack_from("<HH", header, 0x3A)
    return shoff + shentsize * shnum


def unpacked_size(path):
    """Bytes after installation or extraction, without running the artifact."""
    if path.is_dir():
        return sum(p.lstat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())
    if path.name.endswith(".AppImage"):
        listing = subprocess.run(["unsquashfs", "-o", str(appimage_offset(path)), "-lls", str(path)],
                                 capture_output=True, text=True, check=True).stdout
        return sum(int(line.split()[2]) for line in listing.splitlines() if line.startswith("-"))
    if path.name.endswith(".deb"):
        size = subprocess.run(["dpkg-deb", "-f", str(path), "Installed-Size"],
                              capture_output=True, text=True, check=True).stdout
        return int(size) * 1024
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path) as archive:
            return sum(member.size for member in archive.getmembers() if member.isfile())
    return path.stat().st_size


def size_table(paths):
    rows = ["| Artifact | Download (MiB) | Unpacked (MiB) | Budget (MiB) |", "| --- | ---: | ---: | ---: |"]
    for path in paths:
        download = "—" if path.is_dir() else f"{path.stat().st_size / MiB:,.1f}"
        try:
            limit = f"{budget(path) / MiB:,.0f}"
        except ValueError:
            limit = "—"
        rows.append(f"| `{path.name}` | {download} | {unpacked_size(path) / MiB:,.1f} | {limit} |")
    return "\n".join(rows)


def check_version(tag):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Release tags must use vMAJOR.MINOR.PATCH")
    version = tag[1:]
    manifests = {
        "package.json": json.loads((ROOT / "package.json").read_text())["version"],
        "package-lock.json": json.loads((ROOT / "package-lock.json").read_text())["version"],
        "package-lock.json root package": json.loads((ROOT / "package-lock.json").read_text())["packages"][""]["version"],
        "src-tauri/Cargo.lock": next(p["version"] for p in tomllib.loads((ROOT / "src-tauri/Cargo.lock").read_text())["package"] if p["name"] == "murmur"),
        "src-tauri/tauri.conf.json": json.loads((ROOT / "src-tauri/tauri.conf.json").read_text())["version"],
        "src-tauri/Cargo.toml": tomllib.loads((ROOT / "src-tauri/Cargo.toml").read_text())["package"]["version"],
        "engine/pyproject.toml": tomllib.loads((ROOT / "engine/pyproject.toml").read_text())["project"]["version"],
    }
    for name, actual in manifests.items():
        if actual != version:
            raise ValueError(f"{name}: {actual} does not match {tag}")
    manifest = (ROOT / "companion/flipper/application.fam").read_text()
    if f'fap_version="{version}"' not in manifest:
        raise ValueError("Flipper version does not match release")


def check_assets(tag, directory):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Invalid release tag")
    version = tag[1:]
    required = {
        "murmur-linux-x86_64.tar.gz",
        f"Murmur_{version}_amd64.deb", f"Murmur_{version}_amd64.AppImage",
        f"murmur-companion-{tag}.fap", "GPU-VALIDATION.json",
    }
    actual = {p.name for p in directory.iterdir() if p.name != "SHA256SUMS"}
    if actual != required:
        raise ValueError(f"Asset mismatch: missing {required - actual}, unexpected {actual - required}")
    for name in required:
        p = directory / name
        if p.is_symlink() or not p.is_file() or p.stat().st_size == 0:
            raise ValueError(f"Missing, empty or unsafe asset: {name}")
    check_budgets(directory / name for name in sorted(required))
    check_portable(directory / "murmur-linux-x86_64.tar.gz", directory / f"Murmur_{version}_amd64.AppImage")
    evidence = json.loads((directory / "GPU-VALIDATION.json").read_text())
    linux = required - {"GPU-VALIDATION.json", f"murmur-companion-{tag}.fap"}
    if evidence.get("assets") != {name: _sha256(directory / name) for name in linux}:
        raise ValueError("GPU validation is not tied to the final release assets")
    if evidence.get("gpu", {}).get("status", {}).get("device") != "cuda" or evidence["gpu"]["status"].get("cpu_fallback"):
        raise ValueError("Forced CUDA validation did not pass")
    if evidence.get("auto", {}).get("status", {}).get("device") != "cuda" or evidence["auto"]["status"].get("cpu_fallback"):
        raise ValueError("Automatic CUDA validation did not pass")
    if len(evidence["gpu"].get("warm_runs_ms", [])) != 10 or not evidence.get("driver") or evidence.get("origin_validated") is not True:
        raise ValueError("GPU hardware evidence is incomplete")
    libraries = evidence["gpu"].get("libraries", [])
    if not any("/usr/lib/murmur/cuda/libcublas.so" in path for path in libraries) or not any("/libcuda.so" in path and "/murmur/cuda/" not in path for path in libraries):
        raise ValueError("GPU library-origin evidence is incomplete")
    probe = evidence.get("runtime_probe", {})
    if set(probe) != {"libcublas.so.12", "libcublasLt.so.12"} or any(
        not path.endswith(f"/usr/lib/murmur/cuda/{name}") for name, path in probe.items()
    ):
        raise ValueError("CUDA userspace origin evidence is incomplete")
    lines = []
    for name in sorted(required):
        with (directory / name).open("rb") as stream:
            lines.append(f'{hashlib.file_digest(stream, "sha256").hexdigest()}  {name}\n')
    (directory / "SHA256SUMS").write_text("".join(lines))


def check_portable(archive_path, image):
    """The portable archive is the AppImage under a short name, nothing else."""
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        if [m.name for m in members] != ["murmur"]:
            raise ValueError("Portable archive must contain only the AppImage as murmur")
        member, = members
        if not member.isfile() or not member.mode & 0o111 or member.size == 0:
            raise ValueError("Archive member must be a nonempty executable: murmur")
        with archive.extractfile(member) as stream, image.open("rb") as original:
            if hashlib.file_digest(stream, "sha256").digest() != hashlib.file_digest(original, "sha256").digest():
                raise ValueError("Portable desktop must match the AppImage")


def _sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "version":
            check_version(sys.argv[2])
        elif len(sys.argv) == 4 and sys.argv[1] == "assets":
            check_assets(sys.argv[2], Path(sys.argv[3]))
        elif len(sys.argv) == 3 and sys.argv[1] == "budgets":
            check_budgets(sorted(p for p in Path(sys.argv[2]).iterdir() if p.is_file()))
        elif len(sys.argv) >= 3 and sys.argv[1] == "sizes":
            print(size_table([Path(arg) for arg in sys.argv[2:]]))
        else:
            raise ValueError("Usage: check-release.py version TAG | assets TAG DIRECTORY | "
                             "budgets DIRECTORY | sizes PATH... (Python 3.11+)")
    except (ValueError, OSError, tarfile.TarError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
