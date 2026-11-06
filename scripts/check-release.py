#!/usr/bin/env python3
"""Validate release versions and the required Linux/Flipper download set."""
import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]


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
        f"murmur-companion-{tag}.fap",
    }
    actual = {p.name for p in directory.iterdir() if p.name != "SHA256SUMS"}
    if actual != required:
        raise ValueError(f"Asset mismatch: missing {required - actual}, unexpected {actual - required}")
    for name in required:
        p = directory / name
        if p.is_symlink() or not p.is_file() or p.stat().st_size == 0:
            raise ValueError(f"Missing, empty or unsafe asset: {name}")
    with tarfile.open(directory / "murmur-linux-x86_64.tar.gz") as archive:
        members = archive.getmembers()
        if {m.name for m in members} != {"murmur", "murmur-engine"} or len(members) != 2:
            raise ValueError("Portable archive must contain only the executable pair")
        for member in members:
            if not member.isfile() or not member.mode & 0o111 or member.size == 0:
                raise ValueError(f"Archive member must be a nonempty executable: {member.name}")
        with archive.extractfile("murmur") as stream, (directory / f"Murmur_{version}_amd64.AppImage").open("rb") as image:
            if hashlib.file_digest(stream, "sha256").digest() != hashlib.file_digest(image, "sha256").digest():
                raise ValueError("Portable desktop must match the AppImage")
    lines = []
    for name in sorted(required):
        with (directory / name).open("rb") as stream:
            lines.append(f'{hashlib.file_digest(stream, "sha256").hexdigest()}  {name}\n')
    (directory / "SHA256SUMS").write_text("".join(lines))


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "version":
            check_version(sys.argv[2])
        elif len(sys.argv) == 4 and sys.argv[1] == "assets":
            check_assets(sys.argv[2], Path(sys.argv[3]))
        else:
            raise ValueError("Usage: check-release.py version TAG | assets TAG DIRECTORY (Python 3.11+)")
    except (ValueError, OSError, tarfile.TarError) as error:
        sys.exit(str(error))
