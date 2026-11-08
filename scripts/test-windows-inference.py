#!/usr/bin/env python3
"""Offline artifact inference and DLL-origin gate on the trusted Windows GPU runner (Edison).

Windows port of scripts/test-linux-inference.py; the fixture, matrix and engine conversation are
shared (inference_common.py). murmur.exe from the portable zip installs the Windows engine, the
cuBLAS add-on and the staged speech model into a throwaway %LOCALAPPDATA% through its own
verifier (`--install-component`, development trust root + acknowledgement). The installed engine
then runs the full GPU precision x VAD matrix with MURMUR_CUDA_DIR pointing at the add-on (as the
app passes it) and no CUDA on PATH, Auto on GPU, and CPU with the GPU hidden. Loaded modules
prove cuBLAS came from the add-on, nvcuda.dll from the driver, and that cuDNN/NVRTC never loaded.
Writes GPU-VALIDATION-windows.json next to the artifacts. Not yet run: written without a Windows
machine (verified in source only).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import zipfile

import inference_common
import linux_components as components_cli
from inference_common import MATRIX, fixture_pcm, requester, threaded_reader, transcription_runs

CUDA_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll")
EVIDENCE = "GPU-VALIDATION-windows.json"
CREATE_NO_WINDOW = 0x08000000


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def parse_modules(output):
    """Module paths, one per line, from `(Get-Process -Id N).Modules.FileName`."""
    return sorted({line.strip() for line in output.splitlines() if line.strip().lower().endswith(".dll")})


def loaded_modules(pid):
    command = f"(Get-Process -Id {int(pid)}).Modules | ForEach-Object {{ $_.FileName }}"
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=60, creationflags=CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(f"Could not list engine modules: {result.stderr.strip()[-400:]}")
    return parse_modules(result.stdout)


def _inside(path, directory):
    """Case-insensitive, separator-agnostic containment (Windows paths, checked on any host)."""
    normal = lambda value: str(value).replace("/", "\\").lower().rstrip("\\")  # noqa: E731
    return normal(path).startswith(normal(directory) + "\\")


def check_origin(modules, cuda):
    """cuBLAS only from the add-on, the CUDA driver from the host, no cuDNN/NVRTC at all."""
    for name in CUDA_DLLS:
        matches = [path for path in modules if Path(path.replace("\\", "/")).name.lower() == name.lower()]
        if not matches or any(not _inside(path, cuda) for path in matches):
            raise RuntimeError(f"{name} did not load solely from the add-on: {matches}")
    unexpected = [path for path in modules if re.search(r"(?:^|[\\/])(?:cudnn|nvrtc)[^\\/]*\.dll$", path, re.IGNORECASE)]
    if unexpected:
        raise RuntimeError(f"cuDNN/NVRTC loaded although Murmur ships cuBLAS only: {unexpected}")
    drivers = [path for path in modules if path.lower().replace("/", "\\").endswith("\\nvcuda.dll")]
    if not drivers or any(_inside(path, cuda) for path in drivers):
        raise RuntimeError(f"Host driver origin invalid: {drivers}")


def clean_environment(home, cuda=None, hidden=False, pathsep=os.pathsep):
    """No CUDA from the host: CUDA_PATH* removed and CUDA/NVIDIA toolkit folders dropped from PATH."""
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("CUDA_PATH", "CUDA_HOME", "MURMUR_CUDA_DIR", "PYTHONPATH"))}
    path = [entry for entry in env.get("PATH", "").split(pathsep)
            if entry and not re.search(r"NVIDIA GPU Computing Toolkit|\\CUDA\\|cudnn", entry, re.IGNORECASE)]
    env.update(PATH=pathsep.join(path), USERPROFILE=str(home), APPDATA=str(home / "Roaming"),
               LOCALAPPDATA=str(home / "Local"), HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    if cuda:
        env["MURMUR_CUDA_DIR"] = str(cuda)
    if hidden:
        env["CUDA_VISIBLE_DEVICES"] = "-1"
    else:
        env.pop("CUDA_VISIBLE_DEVICES", None)
    return env


def run_engine(engine, model_path, model, pcm, cuda, device, runs, home, hidden=False, matrix=()):
    home.mkdir(parents=True, exist_ok=True)
    launched = time.perf_counter()
    process = subprocess.Popen([str(engine), "--models-dir", str(home / "no-legacy-models")],
                               env=clean_environment(home, cuda, hidden), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                               creationflags=CREATE_NO_WINDOW)
    request = requester(process.stdin, threaded_reader(process.stdout), device)
    try:
        result = transcription_runs(request, model, model_path, device, pcm, runs, matrix, launched)
        result["modules"] = loaded_modules(process.pid)
        return result
    finally:
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def evidence_assets(artifacts, components):
    """Hashes of exactly the files check-release.py ties the evidence to."""
    names = [*sorted(p.name for p in artifacts.glob("Murmur_*_x64-setup.exe")), "murmur-windows-x86_64.zip"]
    files = {name: artifacts / name for name in names}
    for path in sorted(components.glob("murmur-*-windows-x86_64-*.zip")):
        files[path.name] = path
    files[components_cli.MANIFEST] = components / components_cli.MANIFEST
    return {name: sha256(path) for name, path in sorted(files.items())}


def check(artifacts, components, models, model, fixture, runs):
    with tempfile.TemporaryDirectory(prefix="murmur-inference-") as temporary:
        work = Path(temporary)
        with zipfile.ZipFile(artifacts / "murmur-windows-x86_64.zip") as portable:
            portable.extract("murmur.exe", work / "app")
        app = work / "app/murmur.exe"
        env = components_cli.environment(work / "profile", components, windows=True)
        installed = components_cli.install_release(app, components, env, [models / model], system="windows")
        engine, cuda, model_path = installed["engine"] / "murmur-engine.exe", installed["cuda"], installed[f"speech-{model}"]
        if any(engine.parent.rglob("cudnn64_9.dll")):
            raise RuntimeError("The installed engine still carries CTranslate2's cuDNN shim")
        pcm = fixture_pcm(fixture)
        gpu = run_engine(engine, model_path, model, pcm, cuda, "cuda", runs, work / "gpu", matrix=MATRIX)
        inference_common.require_device(gpu, "cuda", "Forced CUDA")
        check_origin(gpu["modules"], cuda)
        auto = run_engine(engine, model_path, model, pcm, cuda, "auto", 1, work / "auto")
        inference_common.require_device(auto, "cuda", "Auto")
        cpu = run_engine(engine, model_path, model, pcm, None, "cpu", 1, work / "cpu", hidden=True)
        inference_common.require_device(cpu, "cpu", "Explicit CPU")
        cpu_auto = run_engine(engine, model_path, model, pcm, cuda, "auto", 1, work / "cpu-auto", hidden=True)
        inference_common.require_device(cpu_auto, "cpu", "CPU-only Auto", no_gpu_auto=True)
        driver = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                                check=True, capture_output=True, text=True).stdout.strip()
        return {"assets": evidence_assets(artifacts, components), "model": model, "fixture_sha256": sha256(fixture),
                "origin_validated": True, "gpu": gpu, "auto": auto, "cpu": cpu, "cpu_auto": cpu_auto,
                "driver": driver, "windows": sys.getwindowsversion()._asdict() if hasattr(sys, "getwindowsversion") else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--artifacts", type=Path, required=True, help="installer and murmur-windows-x86_64.zip")
    parser.add_argument("--components", type=Path, required=True, help="Windows zips, manifest, signature, certificate")
    parser.add_argument("--models-dir", type=Path, required=True, help="offline staged models (WINDOWS_GPU_TEST_MODELS)")
    parser.add_argument("--model", default="distil-small.en")
    parser.add_argument("--fixture", type=Path, default=Path(__file__).parent / "tests/jfk-3s.wav")
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("runs must be positive")
    if os.name != "nt":
        parser.error("run on the Windows GPU runner")
    report = check(args.artifacts.resolve(), args.components.resolve(), args.models_dir.resolve(), args.model,
                   args.fixture.resolve(), args.runs)
    path = args.artifacts / EVIDENCE
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Windows GPU validation passed; evidence: {path}; median: {report['gpu']['warm_median_ms']} ms")


if __name__ == "__main__":
    main()
