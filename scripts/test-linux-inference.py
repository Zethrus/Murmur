#!/usr/bin/env python3
"""Offline artifact inference and library-origin gate on a trusted Linux GPU host."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import wave

from linux_cuda_runtime import CUDA, REQUIRED, sha256, verify

ENGINE = Path("usr/lib/murmur/engine/murmur-engine")
# Every GPU precision Murmur offers, with Silero VAD on and off.
MATRIX = [(compute, vad) for compute in ("int8_float16", "float16", "float32", "int8") for vad in (False, True)]


def _engine_gate():
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_engine_imports", Path(__file__).with_name("check-engine-imports.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check


def fixture_pcm(path):
    with wave.open(str(path)) as sound:
        if (sound.getnchannels(), sound.getframerate(), sound.getsampwidth()) != (1, 16000, 2):
            raise RuntimeError("Inference fixture must be mono 16 kHz PCM16 WAV")
        raw = sound.readframes(sound.getnframes())
    import array
    samples = array.array("h", raw)
    import struct
    return base64.b64encode(struct.pack(f"<{len(samples)}f", *(sample / 32768 for sample in samples))).decode()


def descendants(pid):
    found = {pid}
    pending = [pid]
    while pending:
        current = pending.pop()
        children = Path(f"/proc/{current}/task/{current}/children")
        if children.exists():
            for child in children.read_text().split():
                number = int(child)
                if number not in found:
                    found.add(number)
                    pending.append(number)
    return found


def loaded_libraries(pid):
    paths = set()
    for child in descendants(pid):
        try:
            for line in Path(f"/proc/{child}/maps").read_text().splitlines():
                fields = line.split(maxsplit=5)
                path = fields[5] if len(fields) == 6 else ""
                if path.startswith("/") and ".so" in Path(path).name:
                    paths.add(path)
        except (FileNotFoundError, ProcessLookupError):
            pass
    return sorted(paths)


def run_engine(engine, models, model, pcm, library_path, device, runs, home, hidden=False, matrix=()):
    env = {key: value for key, value in os.environ.items()
           if key not in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONPATH", "CUDA_HOME", "CUDA_PATH")}
    env.update(HOME=str(home), XDG_CACHE_HOME=str(home / "cache"),
               XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
               HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", LD_LIBRARY_PATH=library_path)
    if hidden:
        env["CUDA_VISIBLE_DEVICES"] = "-1"
    else:
        env.pop("CUDA_VISIBLE_DEVICES", None)
    launched = time.perf_counter()
    process = subprocess.Popen([str(engine), "--models-dir", str(models)], env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, start_new_session=True)
    number = 0

    def request(method, params=None):
        nonlocal number
        number += 1
        process.stdin.write(json.dumps({"id": number, "method": method, "params": params or {}}) + "\n")
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], 180)
        if not ready:
            raise RuntimeError(f"{device} inference timed out")
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(f"{device} engine exited: {process.stderr.read()[-600:]}")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise RuntimeError(f"{device} inference failed: {reply.get('error')}")
        return reply["result"]

    try:
        params = {"model": model, "device": device, "compute_type": "auto",
                  "pcm_b64": pcm, "sample_rate": 16000, "beam_size": 1, "vad": False}
        first = request("transcribe", params)
        startup_ms = round((time.perf_counter() - launched) * 1000, 2)
        if not first["text"].strip():
            raise RuntimeError(f"{device} fixture produced no transcript")
        times = [request("transcribe", params)["infer_ms"] for _ in range(runs)]
        status = request("status", {"model": model, "device": device, "compute_type": "auto"})
        combinations = []
        for compute, vad in matrix:
            reply = request("transcribe", dict(params, compute_type=compute, vad=vad))
            loaded = request("status", {})
            if not reply["text"].strip() or loaded["device"] != device or loaded["compute"] != compute:
                raise RuntimeError(f"{device} {compute} vad={vad} failed: {loaded}")
            combinations.append({"compute": compute, "vad": vad, "infer_ms": reply["infer_ms"],
                                 "transcript_sha256": hashlib.sha256(reply["text"].encode()).hexdigest()})
        libraries = loaded_libraries(process.pid)
        return {"status": status, "startup_and_first_infer_ms": startup_ms, "matrix": combinations,
                "cold_infer_ms": first["infer_ms"],
                "warm_median_ms": statistics.median(times), "warm_runs_ms": times,
                "libraries": libraries, "transcript_sha256": hashlib.sha256(first["text"].encode()).hexdigest()}
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def check_origin(result, cuda):
    loaded = result["libraries"]
    for prefix in ("libcublas.so", "libcublasLt.so"):
        matches = [path for path in loaded if Path(path).name.startswith(prefix)]
        if not matches or any(not Path(path).is_relative_to(cuda) for path in matches):
            raise RuntimeError(f"{prefix} did not load solely from package: {matches}")
    unexpected = [path for path in loaded if Path(path).name.startswith(("libcudnn", "libnvrtc"))]
    if unexpected:
        raise RuntimeError(f"cuDNN/NVRTC loaded although Murmur ships cuBLAS only: {unexpected}")
    drivers = [path for path in loaded if Path(path).name.startswith("libcuda.so")]
    if not drivers or any(Path(path).is_relative_to(cuda) for path in drivers):
        raise RuntimeError(f"Host driver origin invalid: {drivers}")


def probe_runtime_origin(cuda):
    """Resolve the shipped CUDA libraries from a clean child loader environment."""
    names = tuple(sorted(REQUIRED))
    program = """import ctypes, json, os
names = json.loads(os.environ['MURMUR_PROBE_LIBRARIES'])
handles = [ctypes.CDLL(name) for name in names]
paths = {}
for line in open('/proc/self/maps'):
    fields = line.split(maxsplit=5)
    if len(fields) == 6:
        name = os.path.basename(fields[5].strip())
        if name in names:
            paths[name] = fields[5].strip()
print(json.dumps(paths))
"""
    env = {key: value for key, value in os.environ.items()
           if key not in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONPATH")}
    # Relocated setup-python interpreters need their own libpython. Do not
    # inherit arbitrary host CUDA paths; origin checks below still require
    # every probed CUDA library to come from the package.
    search = os.pathsep.join((str(cuda), str(Path(sys.base_prefix) / "lib")))
    env.update(LD_LIBRARY_PATH=search, MURMUR_PROBE_LIBRARIES=json.dumps(names))
    result = subprocess.run([sys.executable, "-c", program], env=env,
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"CUDA runtime probe failed: {result.stderr.strip()}")
    paths = json.loads(result.stdout)
    if set(paths) != set(names) or any(not Path(path).is_relative_to(cuda) for path in paths.values()):
        raise RuntimeError(f"CUDA loader origin invalid: {paths}")
    return paths


def check(artifacts, models, model, fixture, runs, baseline_engine=None, baseline_libraries=None):
    image, = artifacts.glob("*.AppImage")
    with tempfile.TemporaryDirectory(prefix="murmur inference ") as temporary:
        work = Path(temporary)
        subprocess.run([str(image), "--appimage-extract"], cwd=work, check=True, stdout=subprocess.DEVNULL)
        appdir = work / "squashfs-root"
        verify(appdir)
        engine = appdir / ENGINE
        _engine_gate()(engine.parent)
        cuda = appdir / CUDA
        search = os.pathsep.join((str(cuda), str(appdir / "usr/lib")))
        pcm = fixture_pcm(fixture)
        # Forced CUDA sees only the shipped cuBLAS directory: proves nothing else is needed.
        gpu = run_engine(engine, models, model, pcm, str(cuda), "cuda", runs, work / "gpu", matrix=MATRIX)
        if gpu["status"]["device"] != "cuda" or gpu["status"]["cpu_fallback"]:
            raise RuntimeError(f"Forced CUDA did not remain on GPU: {gpu['status']}")
        check_origin(gpu, cuda)
        runtime_probe = probe_runtime_origin(cuda)
        auto = run_engine(engine, models, model, pcm, search, "auto", 1, work / "auto")
        if auto["status"]["device"] != "cuda" or auto["status"]["cpu_fallback"]:
            raise RuntimeError(f"Auto did not use GPU: {auto['status']}")
        cpu = run_engine(engine, models, model, pcm, str(appdir / "usr/lib"), "cpu", 1, work / "cpu", True)
        if cpu["status"]["device"] != "cpu" or cpu["status"]["fallback_reason"]:
            raise RuntimeError(f"Explicit CPU failed: {cpu['status']}")
        cpu_auto = run_engine(engine, models, model, pcm, str(appdir / "usr/lib"), "auto", 1, work / "cpu-auto", True)
        if cpu_auto["status"]["device"] != "cpu" or cpu_auto["status"]["fallback_reason"]:
            raise RuntimeError(f"CPU-only Auto failed: {cpu_auto['status']}")
        baseline = None
        if baseline_engine:
            baseline = run_engine(baseline_engine, models, model, pcm, baseline_libraries,
                                  "cuda", runs, work / "baseline")
            limit = baseline["warm_median_ms"] * 1.2 + 100
            if gpu["warm_median_ms"] > limit:
                raise RuntimeError(f"Packaged GPU median {gpu['warm_median_ms']} ms exceeds baseline limit {limit} ms")
        assets = {path.name: sha256(path) for path in artifacts.iterdir() if path.is_file() and path.name != "GPU-VALIDATION.json"}
        return {"assets": assets, "model": model, "fixture_sha256": sha256(fixture),
                "origin_validated": True, "runtime_probe": runtime_probe,
                "gpu": gpu, "auto": auto, "cpu": cpu, "cpu_auto": cpu_auto,
                "baseline": baseline, "driver": subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                    check=True, capture_output=True, text=True).stdout.strip()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--model", default="distil-small.en")
    parser.add_argument("--fixture", type=Path, default=Path(__file__).parent / "tests/jfk-3s.wav")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--baseline-engine", type=Path)
    parser.add_argument("--baseline-libraries")
    args = parser.parse_args()
    if args.runs < 1 or bool(args.baseline_engine) != bool(args.baseline_libraries):
        parser.error("runs must be positive; baseline engine and libraries must be paired")
    report = check(args.artifacts.resolve(), args.models_dir.resolve(), args.model,
                   args.fixture.resolve(), args.runs, args.baseline_engine, args.baseline_libraries)
    path = args.artifacts / "GPU-VALIDATION.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"GPU validation passed; evidence: {path}; median: {report['gpu']['warm_median_ms']} ms")
