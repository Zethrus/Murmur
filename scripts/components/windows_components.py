#!/usr/bin/env python3
"""Build the Windows release components (runs on the Edison runner; testable anywhere).

  prune-engine  remove the unused cuDNN dispatcher shim from a frozen onedir engine
  engine        onedir engine            -> murmur-engine-windows-x86_64-<ver>.zip
  download-cuda pinned win_amd64 wheel   -> <dest>/nvidia_cublas_cu12-...-win_amd64.whl
  cuda          pinned cuBLAS wheel      -> murmur-cuda-windows-x86_64-<ver>.zip
  inputs        built Windows zips       -> components-windows-inputs.json (manifest input)

The CUDA add-on carries cublas64_12.dll, cublasLt64_12.dll and NVIDIA's licence only: no
cuDNN, no NVRTC. CTranslate2 4.6.3's ctranslate2.dll loads cublas64_12.dll at run time and has
no cuDNN import or string; its wheel still bundles cudnn64_9.dll (the 266 KB cuDNN 9 dispatcher,
KERNEL32 imports only), which `prune-engine` removes. Static inspection only: the GPU
validation job on Edison is the run-time evidence.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
from component_zip import write_zip  # noqa: E402

ENGINE_ZIP = "murmur-engine-windows-x86_64-{version}.zip"
CUDA_ZIP = "murmur-cuda-windows-x86_64-{version}.zip"
INPUTS = "components-windows-inputs.json"
ENGINE_LOCK = ROOT / "engine/requirements-windows-release.txt"
CUDA_LOCK = ROOT / "engine/requirements-windows-cuda.txt"
# Wheel member -> name inside the add-on. Nothing else from the wheel ships.
CUDA_FILES = {
    "nvidia/cublas/bin/cublas64_12.dll": "cublas64_12.dll",
    "nvidia/cublas/bin/cublasLt64_12.dll": "cublasLt64_12.dll",
}
LICENCE = re.compile(r"^nvidia_cublas_cu12-[^/]+\.dist-info/(?:licenses/)?License\.txt$", re.IGNORECASE)
# Shipped by the CTranslate2 Windows wheel, never used by ctranslate2.dll (see module docstring).
PRUNED = ("cudnn64_9.dll",)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _gate():
    spec = importlib.util.spec_from_file_location("check_engine_imports", SCRIPTS / "check-engine-imports.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lock_pin(lock, package):
    """(version, sha256) of `package` in a --require-hashes lock."""
    match = re.search(rf"(?m)^{re.escape(package)}==([\w.]+) --hash=sha256:([a-f0-9]{{64}})$", Path(lock).read_text())
    if not match:
        raise RuntimeError(f"{lock}: no pinned {package}")
    return match.groups()


def check_engine_layout(engine):
    engine = Path(engine)
    if not (engine / "murmur-engine.exe").is_file() or not (engine / "_internal").is_dir():
        raise RuntimeError(f"Not a onedir frozen Windows engine: {engine}")


def prune_engine(engine):
    """Delete the cuDNN shim CTranslate2's wheel bundles; returns the removed relative paths."""
    engine = Path(engine)
    check_engine_layout(engine)
    removed = []
    for path in sorted(engine.rglob("*")):
        if path.is_file() and path.name.lower() in PRUNED and path.parent.name.lower() == "ctranslate2":
            removed.append(path.relative_to(engine).as_posix())
            path.unlink()
    return removed


def build_engine(engine, version, output, platform=sys.platform):
    """Prune, gate (cuDNN/NVRTC) and zip a frozen onedir engine."""
    engine, output = Path(engine).resolve(), Path(output).resolve()
    prune_engine(engine)
    _gate().check(engine, platform=platform)
    output.mkdir(parents=True, exist_ok=True)
    return write_zip(engine, output / ENGINE_ZIP.format(version=version))


def download_cuda(lock, destination, python=sys.executable):
    """pip download exactly the pinned win_amd64 wheel, hash-checked, without dependencies."""
    version, _ = lock_pin(lock, "nvidia-cublas-cu12")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run([python, "-m", "pip", "download", "--no-deps", "--only-binary", ":all:",
                    "--platform", "win_amd64", "--python-version", "3.12", "--require-hashes",
                    "-r", str(lock), "-d", str(destination)], check=True)
    wheel = destination / f"nvidia_cublas_cu12-{version}-py3-none-win_amd64.whl"
    if not wheel.is_file():
        raise RuntimeError(f"pip did not produce {wheel.name}")
    return wheel


def build_cuda(wheel, lock, version, output):
    """cuBLAS-only add-on from the pinned wheel: two DLLs, the licence and a provenance file."""
    wheel, output = Path(wheel), Path(output).resolve()
    pinned_version, pinned_hash = lock_pin(lock, "nvidia-cublas-cu12")
    if sha256(wheel) != pinned_hash:
        raise RuntimeError(f"{wheel.name} does not match the SHA-256 pinned in {Path(lock).name}")
    if f"-{pinned_version}-" not in wheel.name or not wheel.name.endswith("win_amd64.whl"):
        raise RuntimeError(f"{wheel.name} is not the pinned win_amd64 wheel {pinned_version}")
    gate = _gate()
    output.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="murmur-cuda-win-", dir=output.parent))
    try:
        stage = temporary / "cuda"
        stage.mkdir()
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            licences = [name for name in names if LICENCE.match(name)]
            missing = [member for member in CUDA_FILES if member not in names]
            if missing or len(licences) != 1:
                raise RuntimeError(f"{wheel.name} lacks {missing or 'its licence'}")
            for member, name in {**CUDA_FILES, licences[0]: "NVIDIA-CUBLAS-LICENSE.txt"}.items():
                with archive.open(member) as source, (stage / name).open("wb") as target:
                    shutil.copyfileobj(source, target, 1 << 20)
        files = {}
        for name in sorted(CUDA_FILES.values()):
            path = stage / name
            imports = gate.pe_imports_builtin(path.read_bytes())
            if not imports:
                raise RuntimeError(f"{name} is not a PE image with imports")
            # Load-time dependencies (import + delay-import tables) must not include cuDNN/NVRTC.
            linked = sorted(i for i in imports if re.search(r"cudnn|nvrtc", i, re.IGNORECASE))
            if linked:
                raise RuntimeError(f"{name} links {linked}")
            # cublasLt names nvrtc64_120_0.dll for an optional run-time JIT path, exactly as Linux
            # libcublasLt.so.12 names libnvrtc.so.12; the Linux GPU matrix never loaded it. Recorded
            # here; the Edison GPU validation fails if NVRTC or cuDNN is ever loaded.
            files[name] = {"sha256": sha256(path), "size_bytes": path.stat().st_size, "imports": sorted(imports),
                           "lazy_references": sorted(gate.references(path, platform="test"))}
        (stage / "cuda-manifest.json").write_text(json.dumps({
            "source": {"package": "nvidia-cublas-cu12", "version": pinned_version,
                       "wheel": wheel.name, "wheel_sha256": pinned_hash},
            "files": files, "excluded": "cuDNN, NVRTC and every other wheel member",
        }, indent=2, sort_keys=True) + "\n")
        return write_zip(stage, output / CUDA_ZIP.format(version=version))
    finally:
        _remove_staging(temporary)


def _remove_staging(path, attempts=20, delay=1.0):
    """Delete a staging tree, waiting out Windows scanners (Defender, the indexer) that briefly
    hold freshly written DLLs open (WinError 32). The tree sits beside the output directory,
    so if it still cannot go it is left behind with a warning rather than failing the build."""
    for _ in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(delay)
    print(f"warning: could not remove staging directory {path}; a scanner still holds it", file=sys.stderr)


def inputs(components, version, output, engine_lock=ENGINE_LOCK, cuda_lock=CUDA_LOCK):
    """Manifest input for the Linux signing job: what the Windows build produced, by hash."""
    components = Path(components)
    entries = {}
    for kind, pattern in (("engine", ENGINE_ZIP), ("cuda", CUDA_ZIP)):
        path = components / pattern.format(version=version)
        if not path.is_file():
            raise RuntimeError(f"Missing Windows component: {path.name}")
        with zipfile.ZipFile(path) as archive:
            installed = sum(info.file_size for info in archive.infolist())
        entries[kind] = {"zip": path.name, "size_bytes": path.stat().st_size, "sha256": sha256(path),
                         "installed_size_bytes": installed}
    ct2_version, ct2_hash = lock_pin(engine_lock, "ctranslate2")
    cublas_version, cublas_hash = lock_pin(cuda_lock, "nvidia-cublas-cu12")
    document = {"version": version, "os": "windows", "arch": "x86_64", "components": entries,
                "engine_lock_sha256": sha256(engine_lock), "ctranslate2": f"{ct2_version} sha256:{ct2_hash}",
                "cublas_wheel": f"nvidia-cublas-cu12=={cublas_version} sha256:{cublas_hash}"}
    Path(output).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    prune = commands.add_parser("prune-engine")
    prune.add_argument("engine", type=Path)
    engine = commands.add_parser("engine")
    engine.add_argument("--engine", type=Path, required=True)
    engine.add_argument("--version", required=True)
    engine.add_argument("--output", type=Path, required=True)
    download = commands.add_parser("download-cuda")
    download.add_argument("--lock", type=Path, default=CUDA_LOCK)
    download.add_argument("--dest", type=Path, required=True)
    cuda = commands.add_parser("cuda")
    cuda.add_argument("--wheel", type=Path, required=True)
    cuda.add_argument("--lock", type=Path, default=CUDA_LOCK)
    cuda.add_argument("--version", required=True)
    cuda.add_argument("--output", type=Path, required=True)
    manifest_input = commands.add_parser("inputs")
    manifest_input.add_argument("--components", type=Path, required=True)
    manifest_input.add_argument("--version", required=True)
    manifest_input.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prune-engine":
            removed = prune_engine(args.engine)
            print(f"pruned {removed or 'nothing'}")
        elif args.command == "engine":
            print(build_engine(args.engine, args.version, args.output))
        elif args.command == "download-cuda":
            print(download_cuda(args.lock, args.dest))
        elif args.command == "cuda":
            print(build_cuda(args.wheel, args.lock, args.version, args.output))
        else:
            output = args.output or args.components / INPUTS
            inputs(args.components, args.version, output)
            print(output)
    except (RuntimeError, OSError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
