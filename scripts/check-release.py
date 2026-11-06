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
MAX_ASSET = 2 * 1024 ** 3


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
        "murmur", "murmur-engine", "murmur-linux-x86_64.tar.gz",
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
        if p.stat().st_size >= MAX_ASSET:
            raise ValueError(f"Release asset at or above 2 GiB: {name}")
    with (directory / "murmur").open("rb") as desktop, (directory / f"Murmur_{version}_amd64.AppImage").open("rb") as image:
        if hashlib.file_digest(desktop, "sha256").digest() != hashlib.file_digest(image, "sha256").digest():
            raise ValueError("Standalone desktop must include the full AppImage runtime")
    with tarfile.open(directory / "murmur-linux-x86_64.tar.gz") as archive:
        members = archive.getmembers()
        if {m.name for m in members} != {"murmur", "murmur-engine"} or len(members) != 2:
            raise ValueError("Portable archive must contain only the executable pair")
        for member in members:
            if not member.isfile() or not member.mode & 0o111:
                raise ValueError(f"Archive member is not executable: {member.name}")
            with archive.extractfile(member) as stream, (directory / member.name).open("rb") as raw:
                if hashlib.file_digest(stream, "sha256").digest() != hashlib.file_digest(raw, "sha256").digest():
                    raise ValueError(f"Portable and standalone assets differ: {member.name}")
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
    if set(probe) != {"libcublas.so.12", "libcudnn.so.9", "libnvrtc.so.12"} or any(
        not path.endswith(f"/usr/lib/murmur/cuda/{name}") for name, path in probe.items()
    ):
        raise ValueError("CUDA userspace origin evidence is incomplete")
    lines = []
    for name in sorted(required):
        with (directory / name).open("rb") as stream:
            lines.append(f'{hashlib.file_digest(stream, "sha256").hexdigest()}  {name}\n')
    (directory / "SHA256SUMS").write_text("".join(lines))


def _sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


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
