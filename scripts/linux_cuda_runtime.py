"""Collect and verify the pinned Linux CUDA userspace beside the desktop engine."""
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import subprocess

PACKAGES = ("nvidia-cublas-cu12", "nvidia-cudnn-cu12", "nvidia-cuda-nvrtc-cu12")
REQUIRED = {"libcublas.so.12", "libcublasLt.so.12", "libcudnn.so.9", "libnvrtc.so.12"}
DOC = Path("usr/share/doc/murmur/bundled-runtime")
CUDA = Path("usr/lib/murmur/cuda")
MAX_ASSET = 2 * 1024 ** 3


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def wheel_lock(path):
    entries = {}
    for name, version, digest in re.findall(
        r"(?m)^([\w-]+)==([\w.]+) --hash=sha256:([a-f0-9]{64})$", path.read_text()
    ):
        entries[name.replace("_", "-").lower()] = (version, digest)
    if not set(PACKAGES) <= entries.keys():
        raise RuntimeError("Linux release lock lacks pinned CUDA wheel hashes")
    return entries


def _runtime_files(distribution, component):
    prefix = f"nvidia/{component}/lib/"
    files = [file for file in distribution.files or () if str(file).startswith(prefix)]
    if not files:
        raise RuntimeError(f"Missing {component} runtime files")
    for file in files:
        source = Path(distribution.locate_file(file))
        if not (source.name.startswith("lib") and ".so" in source.name):
            continue
        if source.is_symlink():
            target = source.resolve(strict=True)
            if not target.is_relative_to(source.parent.resolve()):
                raise RuntimeError(f"Escaping CUDA symlink: {source}")
        elif not source.is_file():
            raise RuntimeError(f"Missing CUDA library: {source}")
        yield source


def stage(appdir, lock_path):
    """Use only the selected interpreter's installed distribution metadata."""
    pinned = wheel_lock(lock_path)
    cuda, docs = appdir / CUDA, appdir / DOC
    cuda.mkdir(parents=True, exist_ok=True)
    docs.mkdir(parents=True, exist_ok=True)
    packages, files, notices = {}, {}, {}
    for package in PACKAGES:
        dist = metadata.distribution(package)
        version, wheel_hash = pinned[package]
        if dist.version != version:
            raise RuntimeError(f"{package}: installed {dist.version}, locked {version}")
        component = package.removeprefix("nvidia-").removesuffix("-cu12").replace("-", "_")
        for source in _runtime_files(dist, component):
            target = cuda / source.name
            if target.exists() or target.is_symlink():
                if target.is_symlink() != source.is_symlink() or (not source.is_symlink() and sha256(target) != sha256(source)):
                    raise RuntimeError(f"Conflicting CUDA library name: {source.name}")
                continue
            if source.is_symlink():
                target.symlink_to(source.readlink())
            else:
                shutil.copy2(source, target)
            files[source.name] = sha256(target)
        licenses = [file for file in dist.files or () if "/licenses/" in str(file).lower() and file.name.lower().startswith("license")]
        if not licenses:
            raise RuntimeError(f"Missing NVIDIA notice: {package}")
        notice = docs / f"{package}.License.txt"
        shutil.copy2(dist.locate_file(licenses[0]), notice)
        notices[notice.name] = sha256(notice)
        packages[package] = {"version": version, "wheel_sha256": wheel_hash,
                             "wheel_source": f"https://pypi.org/project/{package}/{version}/"}
    missing = REQUIRED - files.keys()
    if missing:
        raise RuntimeError(f"Missing required CUDA libraries: {sorted(missing)}")
    if any(name.startswith("libcuda.so") for name in files):
        raise RuntimeError("Host NVIDIA driver must not be bundled")
    manifest = {"lock_sha256": sha256(lock_path), "packages": packages,
                "files": files, "notices": notices}
    (docs / "cuda-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    verify(appdir, lock_path)


def verify(appdir, lock_path=None):
    appdir = Path(appdir).resolve()
    if lock_path is None:
        lock_path = Path(__file__).resolve().parents[1] / "engine/requirements-linux-release.txt"
    cuda, docs = appdir / CUDA, appdir / DOC
    manifest = json.loads((docs / "cuda-manifest.json").read_text())
    if manifest["lock_sha256"] != sha256(lock_path):
        raise RuntimeError("CUDA release lock mismatch")
    pinned = wheel_lock(lock_path)
    if any(manifest["packages"].get(package) != {"version": pinned[package][0],
            "wheel_sha256": pinned[package][1],
            "wheel_source": f"https://pypi.org/project/{package}/{pinned[package][0]}/"}
           for package in PACKAGES):
        raise RuntimeError("CUDA package manifest differs from release lock")
    if set(manifest["packages"]) != set(PACKAGES) or not REQUIRED <= manifest["files"].keys():
        raise RuntimeError("Incomplete CUDA manifest")
    actual = {p.name for p in cuda.iterdir() if p.is_file() or p.is_symlink()}
    if actual != manifest["files"].keys():
        raise RuntimeError("CUDA runtime files differ from manifest")
    for name, expected in manifest["files"].items():
        source = cuda / name
        if source.is_symlink() and not source.resolve(strict=True).is_relative_to(cuda.resolve()):
            raise RuntimeError(f"Escaping CUDA symlink: {name}")
        if sha256(source) != expected:
            raise RuntimeError(f"CUDA library checksum mismatch: {name}")
    if set(manifest["notices"]) != {f"{p}.License.txt" for p in PACKAGES}:
        raise RuntimeError("Incomplete CUDA notices")
    for name, expected in manifest["notices"].items():
        if sha256(docs / name) != expected:
            raise RuntimeError(f"CUDA notice checksum mismatch: {name}")
    if any(name.startswith("libcuda.so") for name in actual):
        raise RuntimeError("Host NVIDIA driver bundled")
    for name in actual:
        if not name.endswith((".so", ".so.9", ".so.12", ".so.12.9")):
            raise RuntimeError(f"Unexpected CUDA runtime file: {name}")
        dynamic = subprocess.run(["readelf", "-d", str(cuda / name)],
                                 capture_output=True, text=True, check=True).stdout
        for needed in re.findall(r"\(NEEDED\).*\[([^]]+)\]", dynamic):
            if needed.startswith(("libcu", "libnv")) and not needed.startswith("libcuda.so") and needed not in actual:
                raise RuntimeError(f"Unbundled CUDA ELF dependency: {name} needs {needed}")
        result = subprocess.run(["ldd", str(cuda / name)], env={"LD_LIBRARY_PATH": str(cuda)},
                                capture_output=True, text=True, check=True)
        if "not found" in result.stdout:
            raise RuntimeError(f"Unresolved CUDA dependency in {name}: {result.stdout}")
        for line in result.stdout.splitlines():
            match = re.match(r"\s*(\S+) => (/.+?) \(", line)
            if match:
                dependency, origin = match.groups()
                if dependency.startswith(("libcu", "libnv")) and not dependency.startswith("libcuda.so") and not Path(origin).is_relative_to(cuda):
                    raise RuntimeError(f"CUDA dependency loaded from host: {dependency} in {name}")
    return manifest


def check_asset_sizes(paths):
    for path in paths:
        size = path.stat().st_size
        if size >= MAX_ASSET:
            raise RuntimeError(f"Release asset at or above 2 GiB: {path.name} ({size} bytes)")
