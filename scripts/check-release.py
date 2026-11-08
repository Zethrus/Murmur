#!/usr/bin/env python3
"""Validate release versions, the required Linux/Windows/Flipper/component download set and budgets."""
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
# The packages carry only the app; the speech engine and the CUDA add-on are signed
# components downloaded on request (docs/components.md).
APPIMAGE_BUDGET = 125 * MiB
DEB_BUDGET = 125 * MiB
TARBALL_BUDGET = 130 * MiB
ENGINE_ZIP_BUDGET = 130 * MiB
CUDA_ZIP_BUDGET = 520 * MiB
# Windows: the NSIS installer and the portable zip carry only murmur.exe (the engine is a
# component); expected ~15-25 MB, not yet measured (no Windows build has run).
WINDOWS_INSTALLER_BUDGET = 35 * MiB
WINDOWS_PORTABLE_BUDGET = 35 * MiB
WINDOWS_ENGINE_ZIP_BUDGET = 130 * MiB
# Placeholder until measured on Edison: two cuBLAS DLLs from the 553 MB 12.9.2.10 wheel, LZMA.
WINDOWS_CUDA_ZIP_BUDGET = 520 * MiB
# (prefix, suffix, limit); the first match wins.
BUDGETS = (
    ("murmur-engine-windows-", ".zip", WINDOWS_ENGINE_ZIP_BUDGET),
    ("murmur-cuda-windows-", ".zip", WINDOWS_CUDA_ZIP_BUDGET),
    ("murmur-engine-", ".zip", ENGINE_ZIP_BUDGET),
    ("murmur-cuda-", ".zip", CUDA_ZIP_BUDGET),
    ("murmur-windows-", ".zip", WINDOWS_PORTABLE_BUDGET),
    ("Murmur_", "_x64-setup.exe", WINDOWS_INSTALLER_BUDGET),
    ("", ".AppImage", APPIMAGE_BUDGET),
    ("", ".deb", DEB_BUDGET),
    ("", ".tar.gz", TARBALL_BUDGET),
    ("", ".fap", 2 * MiB),
    ("", ".json", 1 * MiB),
    ("", ".sig", 1 * MiB),
    ("", ".asc", 1 * MiB),
)
UNBUDGETED = {"SHA256SUMS"}
MANIFEST = "components-manifest.json"
SIGNATURE = "components-manifest.json.sig"
CERTIFICATE = "murmur-release.asc"
RELEASE_URL = "https://github.com/Zethrus/Murmur/releases/download/{tag}/"
WINDOWS_GPU_EVIDENCE = "GPU-VALIDATION-windows.json"
WINDOWS_PORTABLE = "murmur-windows-x86_64.zip"
WINDOWS_PORTABLE_MEMBERS = {"murmur.exe", "README.txt"}


def windows_assets(version):
    """Windows release files (built on the Edison runner by windows-build.yml)."""
    return {f"Murmur_{version}_x64-setup.exe", WINDOWS_PORTABLE,
            f"murmur-engine-windows-x86_64-{version}.zip", f"murmur-cuda-windows-x86_64-{version}.zip"}


def budget(path):
    for prefix, suffix, limit in BUDGETS:
        if path.name.startswith(prefix) and path.name.endswith(suffix):
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
    if path.name.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(path) as archive:
            return sum(info.file_size for info in archive.infolist())
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


def check_assets(tag, directory, windows=False, windows_gpu=False):
    """`windows`: the release includes the Windows set (vars.ENABLE_WINDOWS); `windows_gpu`:
    Edison's GPU validation ran and its evidence must be tied to these assets."""
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Invalid release tag")
    if windows_gpu and not windows:
        raise ValueError("Windows GPU evidence needs the Windows assets")
    version = tag[1:]
    required = {
        "murmur-linux-x86_64.tar.gz",
        f"Murmur_{version}_amd64.deb", f"Murmur_{version}_amd64.AppImage",
        f"murmur-companion-{tag}.fap", "GPU-VALIDATION.json",
        f"murmur-engine-linux-x86_64-{version}.zip", f"murmur-cuda-linux-x86_64-{version}.zip",
        MANIFEST, SIGNATURE, CERTIFICATE,
    }
    linux_required = set(required)
    if windows:
        required |= windows_assets(version)
    if windows_gpu:
        required.add(WINDOWS_GPU_EVIDENCE)
    actual = {p.name for p in directory.iterdir() if p.name != "SHA256SUMS"}
    if actual != required:
        raise ValueError(f"Asset mismatch: missing {required - actual}, unexpected {actual - required}")
    for name in required:
        p = directory / name
        if p.is_symlink() or not p.is_file() or p.stat().st_size == 0:
            raise ValueError(f"Missing, empty or unsafe asset: {name}")
    check_budgets(directory / name for name in sorted(required))
    check_portable(directory / "murmur-linux-x86_64.tar.gz", directory / f"Murmur_{version}_amd64.AppImage")
    check_manifest(tag, directory, windows)
    if windows:
        check_windows_portable(directory / WINDOWS_PORTABLE)
    if windows_gpu:
        check_windows_gpu(directory, windows_assets(version) | {MANIFEST})
    evidence = json.loads((directory / "GPU-VALIDATION.json").read_text())
    linux = linux_required - {"GPU-VALIDATION.json", f"murmur-companion-{tag}.fap", SIGNATURE, CERTIFICATE}
    if evidence.get("assets") != {name: _sha256(directory / name) for name in linux}:
        raise ValueError("GPU validation is not tied to the final release assets")
    if evidence.get("gpu", {}).get("status", {}).get("device") != "cuda" or evidence["gpu"]["status"].get("cpu_fallback"):
        raise ValueError("Forced CUDA validation did not pass")
    if evidence.get("auto", {}).get("status", {}).get("device") != "cuda" or evidence["auto"]["status"].get("cpu_fallback"):
        raise ValueError("Automatic CUDA validation did not pass")
    if len(evidence["gpu"].get("warm_runs_ms", [])) != 10 or not evidence.get("driver") or evidence.get("origin_validated") is not True:
        raise ValueError("GPU hardware evidence is incomplete")
    libraries = evidence["gpu"].get("libraries", [])
    if not any(re.search(r"/components/cuda/[^/]+/libcublas\.so", path) for path in libraries) or not any(
            "/libcuda.so" in path and "/components/cuda/" not in path for path in libraries):
        raise ValueError("GPU library-origin evidence is incomplete")
    probe = evidence.get("runtime_probe", {})
    if set(probe) != {"libcublas.so.12", "libcublasLt.so.12"} or any(
        not re.search(rf"/components/cuda/[^/]+/{re.escape(name)}$", path) for name, path in probe.items()
    ):
        raise ValueError("CUDA userspace origin evidence is incomplete")
    lines = []
    for name in sorted(required):
        with (directory / name).open("rb") as stream:
            lines.append(f'{hashlib.file_digest(stream, "sha256").hexdigest()}  {name}\n')
    (directory / "SHA256SUMS").write_text("".join(lines))


def check_manifest(tag, directory, windows=False):
    """Zip components must be the release's own assets: same URL, size and SHA-256."""
    try:
        manifest = json.loads((directory / MANIFEST).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unreadable component manifest: {error}") from error
    if manifest.get("schema_version") != 1 or manifest.get("channel") != "stable":
        raise ValueError("Component manifest must be schema 1 on the stable channel")
    base = RELEASE_URL.format(tag=tag)
    zips = [c for c in manifest.get("components", []) if c.get("archive") == "zip"]
    for system in ("linux", "windows") if windows else ("linux",):
        if sorted(c.get("id") for c in zips if c.get("os") == system) != ["cuda", "engine"]:
            raise ValueError(f"Component manifest must list the {system.capitalize()} engine and CUDA add-on")
    if not windows and any(c.get("os") == "windows" for c in zips):
        raise ValueError("Windows components listed but the Windows assets are not part of this release")
    for component in zips:
        urls = component.get("urls", [])
        if len(urls) != 1 or not urls[0].startswith(base):
            raise ValueError(f"{component['id']} must download from this release ({base})")
        asset = directory / urls[0][len(base):]
        if "/" in urls[0][len(base):] or not asset.is_file():
            raise ValueError(f"{component['id']} points at a file that is not a release asset")
        if asset.stat().st_size != component.get("size_bytes") or _sha256(asset) != component.get("sha256"):
            raise ValueError(f"{component['id']} size or SHA-256 differs from {asset.name}")
    for component in manifest.get("components", []):
        for file in component.get("files", []):
            if not all(url.startswith("https://huggingface.co/") for url in file.get("urls", [])):
                raise ValueError(f"{component['id']} model files must come from huggingface.co")


def check_windows_portable(path):
    """The portable zip is murmur.exe and a README: the engine is downloaded on first run."""
    import zipfile
    with zipfile.ZipFile(path) as archive:
        names = {info.filename for info in archive.infolist()}
        if names != WINDOWS_PORTABLE_MEMBERS:
            raise ValueError(f"Windows portable zip must contain only {sorted(WINDOWS_PORTABLE_MEMBERS)}, not {sorted(names)}")
        with archive.open("murmur.exe") as stream:
            if stream.read(2) != b"MZ":
                raise ValueError("Windows portable murmur.exe is not a Windows executable")


def check_windows_gpu(directory, assets):
    """Edison evidence: forced CUDA and Auto stayed on the GPU with the add-on's cuBLAS only."""
    evidence = json.loads((directory / WINDOWS_GPU_EVIDENCE).read_text())
    if evidence.get("assets") != {name: _sha256(directory / name) for name in sorted(assets)}:
        raise ValueError("Windows GPU validation is not tied to the final release assets")
    for run in ("gpu", "auto"):
        status = evidence.get(run, {}).get("status", {})
        if status.get("device") != "cuda" or status.get("cpu_fallback"):
            raise ValueError(f"Windows {run} CUDA validation did not pass")
    if len(evidence["gpu"].get("warm_runs_ms", [])) != 10 or not evidence.get("driver") or evidence.get("origin_validated") is not True:
        raise ValueError("Windows GPU hardware evidence is incomplete")
    modules = [path.replace("/", "\\").lower() for path in evidence["gpu"].get("modules", [])]
    for name in ("cublas64_12.dll", "cublaslt64_12.dll"):
        if not any(re.search(rf"\\components\\cuda\\[^\\]+\\{re.escape(name)}$", path) for path in modules):
            raise ValueError(f"Windows {name} did not load from the CUDA add-on")
    if not any(path.endswith("\\nvcuda.dll") and "\\components\\" not in path for path in modules):
        raise ValueError("Windows host driver (nvcuda.dll) origin evidence is missing")
    if any(re.search(r"\\(?:cudnn|nvrtc)[^\\]*\.dll$", path) for path in modules):
        raise ValueError("cuDNN/NVRTC loaded on Windows although Murmur ships cuBLAS only")


def check_signature(directory, fingerprint):
    """gpg-verify the manifest with the shipped certificate; the primary must be `fingerprint`."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="murmur-verify-") as home:
        env = {"GNUPGHOME": home, "PATH": "/usr/bin:/bin"}
        subprocess.run(["gpg", "--batch", "--quiet", "--import", str(directory / CERTIFICATE)], env=env,
                       check=True, capture_output=True)
        status = subprocess.run(["gpg", "--batch", "--status-fd", "1", "--verify", str(directory / SIGNATURE),
                                 str(directory / MANIFEST)], env=env, capture_output=True, text=True)
    valid = [line.split() for line in status.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG")]
    if status.returncode or not valid or valid[0][-1].upper() != fingerprint.upper():
        raise ValueError("Component manifest is not signed by the release key")


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
        elif len(sys.argv) >= 4 and sys.argv[1] == "assets" and set(sys.argv[4:]) <= {"--windows", "--windows-gpu"}:
            check_assets(sys.argv[2], Path(sys.argv[3]), "--windows" in sys.argv[4:], "--windows-gpu" in sys.argv[4:])
        elif len(sys.argv) == 4 and sys.argv[1] == "signature":
            check_signature(Path(sys.argv[2]), sys.argv[3])
        elif len(sys.argv) == 3 and sys.argv[1] == "budgets":
            check_budgets(sorted(p for p in Path(sys.argv[2]).iterdir() if p.is_file()))
        elif len(sys.argv) >= 3 and sys.argv[1] == "sizes":
            print(size_table([Path(arg) for arg in sys.argv[2:]]))
        else:
            raise ValueError("Usage: check-release.py version TAG | assets TAG DIRECTORY [--windows] [--windows-gpu] | "
                             "signature DIRECTORY FINGERPRINT | budgets DIRECTORY | sizes PATH... (Python 3.11+)")
    except (ValueError, OSError, tarfile.TarError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
