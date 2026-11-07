#!/usr/bin/env python3
"""Fail if a frozen engine could load cuDNN or NVRTC.

Murmur ships only cuBLAS. CTranslate2 4.6.3 runs Whisper on CUDA through cuBLAS
(Conv1D via im2col + GEMM); a future wheel that links or dlopen()s cuDNN or NVRTC
would fail on user machines without those libraries, so the build must stop.

Every file is scanned for library names (catches NEEDED entries and dlopen
literals). Linux ELF files are also checked with `readelf -d`; Windows PE files
with pefile when it is installed (PR-C Windows builds).
"""
import argparse
from pathlib import Path
import re
import subprocess
import sys

# Library file names only: onnxruntime mentions "cudnn" in option strings,
# which cannot load anything.
FORBIDDEN = re.compile(
    rb"lib(?:cudnn|nvrtc)[\w.+-]*\.so|(?:cudnn|nvrtc)(?:64)?_?[\w.-]*\.dll", re.IGNORECASE)
CHUNK = 1 << 20


def _scan(path):
    found, tail = set(), b""
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            data = tail + chunk
            found.update(match.decode(errors="replace").lower() for match in FORBIDDEN.findall(data))
            tail = data[-256:]
    return found


def _elf_needed(path):
    result = subprocess.run(["readelf", "-d", str(path)], capture_output=True, text=True)
    return set(re.findall(r"\(NEEDED\).*\[([^]]+)\]", result.stdout)) if result.returncode == 0 else set()


def _pe_imports(path):
    try:
        import pefile
    except ImportError:  # String scan above still covers import names.
        return set()
    try:
        image = pefile.PE(str(path), fast_load=True)
        image.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    except pefile.PEFormatError:
        return set()
    return {entry.dll.decode(errors="replace") for entry in getattr(image, "DIRECTORY_ENTRY_IMPORT", ())}


def references(path, platform=sys.platform):
    """Forbidden library names referenced by one file."""
    found = _scan(path)
    with path.open("rb") as stream:
        magic = stream.read(4)
    if platform.startswith("linux") and magic == b"\x7fELF":
        linked = _elf_needed(path)
    elif platform == "win32" and magic[:2] == b"MZ":
        linked = _pe_imports(path)
    else:
        linked = set()
    found.update(name.lower() for name in linked if re.search(r"cudnn|nvrtc", name, re.IGNORECASE))
    return found


def check(directory, platform=sys.platform):
    directory = Path(directory)
    if not directory.is_dir():
        raise RuntimeError(f"Frozen engine directory not found: {directory}")
    files = [path for path in sorted(directory.rglob("*")) if path.is_file() and not path.is_symlink()]
    if not any(path.name.startswith(("libctranslate2", "ctranslate2")) for path in files):
        raise RuntimeError(f"No CTranslate2 library in {directory}; is this a frozen engine?")
    problems = {str(path.relative_to(directory)): sorted(names)
                for path in files if (names := references(path, platform))}
    if problems:
        details = "\n".join(f"  {name}: {', '.join(found)}" for name, found in problems.items())
        raise RuntimeError(f"Frozen engine references cuDNN/NVRTC, which Murmur does not ship:\n{details}")
    return len(files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("engine_dir", type=Path)
    args = parser.parse_args()
    try:
        count = check(args.engine_dir)
    except RuntimeError as error:
        sys.exit(str(error))
    print(f"cuDNN/NVRTC gate passed: {count} files in {args.engine_dir}")
