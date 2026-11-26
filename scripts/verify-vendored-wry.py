#!/usr/bin/env python3
"""Verify the entire Wry vendor tree against its pinned archive plus recorded patch."""
import argparse
import hashlib
import io
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.57.2"
ARCHIVE_ROOT = f"wry-{VERSION}"
SHA256 = "a490119196d02a51d0871df7b9119a1f3e46d75b7e90053a801454d5c6b3491c"
URL = f"https://static.crates.io/crates/wry/{ARCHIVE_ROOT}.crate"
OMITTED = {".cargo-ok", ".cargo_vcs_info.json"}
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024


def read_archive(data: bytes, expected_sha: str, archive_root: str) -> dict[str, bytes]:
    if hashlib.sha256(data).hexdigest() != expected_sha:
        raise ValueError("Wry archive SHA-256 mismatch")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or path.parts[0] != archive_root:
                raise ValueError(f"Unexpected archive path: {member.name}")
            if member.isdir():
                continue
            if not member.isfile() or len(path.parts) < 2:
                raise ValueError(f"Unexpected archive entry: {member.name}")
            name = str(PurePosixPath(*path.parts[1:]))
            if name in files:
                raise ValueError(f"Duplicate archive path: {name}")
            files[name] = archive.extractfile(member).read()
    return files


def tree_files(root: Path) -> dict[str, bytes]:
    files = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Redirected vendor path: {path.relative_to(root)}")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def verify(data: bytes, vendor: Path, patch: Path, *, expected_sha: str = SHA256,
           archive_root: str = ARCHIVE_ROOT) -> int:
    upstream = read_archive(data, expected_sha, archive_root)
    # Reconstruct only in a disposable directory; never patch the checkout being verified.
    with tempfile.TemporaryDirectory(prefix="murmur-wry-verify-") as temp:
        root = Path(temp)
        for name, content in upstream.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        result = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "apply", "--whitespace=error", "-p2", str(patch.resolve())],
            cwd=root, capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise ValueError(f"Recorded Wry patch does not apply: {result.stderr.strip()}")
        expected = {name: content for name, content in tree_files(root).items() if name not in OMITTED}
    actual = tree_files(vendor)
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    changed = sorted(name for name in expected.keys() & actual.keys() if expected[name] != actual[name])
    if missing or extra or changed:
        raise ValueError(f"Vendored Wry differs: missing={missing}, extra={extra}, changed={changed}")
    return len(expected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Use a previously downloaded .crate (offline)")
    args = parser.parse_args()
    if args.archive:
        data = args.archive.read_bytes()
    else:
        with urllib.request.urlopen(URL, timeout=30) as response:
            data = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("Wry archive exceeds the download limit")
    count = verify(data, ROOT / "src-tauri/vendor/wry", ROOT / "src-tauri/vendor/wry-host-input.patch")
    print(f"Verified Wry {VERSION}: {count} files match the pinned archive plus recorded patch")


if __name__ == "__main__":
    main()
