#!/usr/bin/env python3
"""Fail if a frozen engine could load cuDNN or NVRTC.

Murmur ships only cuBLAS. CTranslate2 4.6.3 runs Whisper on CUDA through cuBLAS
(Conv1D via im2col + GEMM); a future wheel that links or dlopen()s cuDNN or NVRTC
would fail on user machines without those libraries, so the build must stop.

Every file in the frozen directory is scanned for library names (NEEDED entries
and dlopen literals in native code). Linux ELF files are also checked with
`readelf -d`; Windows PE files with pefile when it is installed.

Python source inside PyInstaller's PYZ archive is zlib-compressed, so a
`ctypes.CDLL("libcudnn.so.9")` in Python would be invisible there. Pass
`--site-packages` with the build interpreter's site-packages: the same packages
are then scanned unfrozen (plain .py and native files), closing that gap.
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


# Packages whose unfrozen sources are scanned when --site-packages is given.
PACKAGES = ("ctranslate2", "faster_whisper", "onnxruntime", "murmur_engine")
# onnxruntime's preload_dlls() lists cuDNN/NVRTC file names but is never called at import,
# and the CPU wheel has no CUDA provider to use them. Allowed only while that holds.
BENIGN = {"onnxruntime/__init__.py"}
CUDA_PROVIDER = re.compile(r"onnxruntime_providers_cuda", re.IGNORECASE)


def _problems(files, root, platform):
    return {str(path.relative_to(root)): sorted(names)
            for path in files if (names := references(path, platform))}


def check(directory, platform=sys.platform, site_packages=None):
    directory = Path(directory)
    if not directory.is_dir():
        raise RuntimeError(f"Frozen engine directory not found: {directory}")
    files = [path for path in sorted(directory.rglob("*")) if path.is_file() and not path.is_symlink()]
    if not any(path.name.startswith(("libctranslate2", "ctranslate2")) for path in files):
        raise RuntimeError(f"No CTranslate2 library in {directory}; is this a frozen engine?")
    problems = _problems(files, directory, platform)
    if problems:
        details = "\n".join(f"  {name}: {', '.join(found)}" for name, found in problems.items())
        raise RuntimeError(f"Frozen engine references cuDNN/NVRTC, which Murmur does not ship:\n{details}")
    if site_packages is not None:
        files += check_sources(site_packages, platform)
    return len(files)


def check_sources(site_packages, platform=sys.platform):
    """Scan the unfrozen packages (plain .py and native files) that the freeze compressed."""
    site_packages = Path(site_packages)
    roots = [site_packages / name for name in PACKAGES if (site_packages / name).is_dir()]
    roots += [path for path in site_packages.glob("*.libs") if path.is_dir()
              and path.name.split(".")[0] in PACKAGES]
    if not any(root.name == "ctranslate2" for root in roots):
        raise RuntimeError(f"No ctranslate2 package in {site_packages}")
    files = [path for root in roots for path in sorted(root.rglob("*"))
             if path.is_file() and not path.is_symlink() and path.suffix != ".pyc"]
    if any(CUDA_PROVIDER.search(path.name) for path in files):
        raise RuntimeError("onnxruntime ships a CUDA provider; the CPU-only wheel is required")
    problems = _problems((path for path in files if str(path.relative_to(site_packages)) not in BENIGN),
                         site_packages, platform)
    if problems:
        details = "\n".join(f"  {name}: {', '.join(found)}" for name, found in problems.items())
        raise RuntimeError(f"Engine packages reference cuDNN/NVRTC, which Murmur does not ship:\n{details}")
    return files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("engine_dir", type=Path)
    parser.add_argument("--site-packages", type=Path,
                        help="Build interpreter's site-packages; scans the engine packages unfrozen")
    args = parser.parse_args()
    try:
        count = check(args.engine_dir, site_packages=args.site_packages)
    except RuntimeError as error:
        sys.exit(str(error))
    print(f"cuDNN/NVRTC gate passed: {count} files in {args.engine_dir}"
          + (f" and {args.site_packages}" if args.site_packages else ""))
