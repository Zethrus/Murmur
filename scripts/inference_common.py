"""Inference validation shared by scripts/test-linux-inference.py and
scripts/test-windows-inference.py: the fixture, the GPU precision x VAD matrix and the
JSON-lines conversation with an installed engine. Platform scripts own process launch,
loader paths and library-origin checks."""
import base64
import hashlib
import queue
import statistics
import struct
import threading
import time
import wave
import array
import json

# Every GPU precision Murmur offers, with Silero VAD on and off.
MATRIX = [(compute, vad) for compute in ("int8_float16", "float16", "float32", "int8") for vad in (False, True)]


def fixture_pcm(path):
    """Mono 16 kHz PCM16 WAV -> base64 float32 little-endian, the engine's `pcm_b64`."""
    with wave.open(str(path)) as sound:
        if (sound.getnchannels(), sound.getframerate(), sound.getsampwidth()) != (1, 16000, 2):
            raise RuntimeError("Inference fixture must be mono 16 kHz PCM16 WAV")
        raw = sound.readframes(sound.getnframes())
    samples = array.array("h", raw)
    return base64.b64encode(struct.pack(f"<{len(samples)}f", *(sample / 32768 for sample in samples))).decode()


def threaded_reader(stream):
    """read_line(timeout) for pipes without select() (Windows): a reader thread feeds a queue.
    Returns '' at end of stream, raises TimeoutError when nothing arrives in time."""
    lines = queue.Queue()

    def pump():
        for line in iter(stream.readline, ""):
            lines.put(line)
        lines.put("")

    threading.Thread(target=pump, daemon=True).start()

    def read_line(timeout):
        try:
            return lines.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None

    return read_line


def requester(stdin, read_line, device, stderr=lambda: ""):
    """JSON-lines RPC over the engine's stdin/stdout; raises on timeouts and engine errors."""
    number = 0

    def request(method, params=None):
        nonlocal number
        number += 1
        stdin.write(json.dumps({"id": number, "method": method, "params": params or {}}) + "\n")
        stdin.flush()
        try:
            line = read_line(180)
        except TimeoutError:
            raise RuntimeError(f"{device} inference timed out") from None
        if not line:
            raise RuntimeError(f"{device} engine exited: {stderr()[-600:]}")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise RuntimeError(f"{device} inference failed: {reply.get('error')}")
        return reply["result"]

    return request


def transcription_runs(request, model, model_path, device, pcm, runs, matrix, launched):
    """Cold + warm runs, status and the precision x VAD matrix; same evidence on every OS."""
    params = {"model": model, "model_path": str(model_path), "device": device, "compute_type": "auto",
              "pcm_b64": pcm, "sample_rate": 16000, "beam_size": 1, "vad": False}
    first = request("transcribe", params)
    startup_ms = round((time.perf_counter() - launched) * 1000, 2)
    if not first["text"].strip():
        raise RuntimeError(f"{device} fixture produced no transcript")
    times = [request("transcribe", params)["infer_ms"] for _ in range(runs)]
    status = request("status", {"model": model, "model_path": str(model_path), "device": device, "compute_type": "auto"})
    combinations = []
    for compute, vad in matrix:
        reply = request("transcribe", dict(params, compute_type=compute, vad=vad))
        loaded = request("status", {})
        if not reply["text"].strip() or loaded["device"] != device or loaded["compute"] != compute:
            raise RuntimeError(f"{device} {compute} vad={vad} failed: {loaded}")
        combinations.append({"compute": compute, "vad": vad, "infer_ms": reply["infer_ms"],
                             "transcript_sha256": hashlib.sha256(reply["text"].encode()).hexdigest()})
    return {"status": status, "startup_and_first_infer_ms": startup_ms, "matrix": combinations,
            "cold_infer_ms": first["infer_ms"], "warm_median_ms": statistics.median(times), "warm_runs_ms": times,
            "transcript_sha256": hashlib.sha256(first["text"].encode()).hexdigest()}


def require_device(result, device, label, no_gpu_auto=False):
    """Forced/Auto runs must stay on `device` without a CPU fallback.

    `no_gpu_auto`: Auto with the GPU hidden. The engine then records `cpu_fallback` (it chose
    the CPU because no CUDA device exists), which is required here, but no `fallback_reason`:
    a reason means a CUDA attempt failed, which must never happen without a GPU to try."""
    status = result["status"]
    fallback = bool(status.get("cpu_fallback"))
    if (status["device"] != device or fallback != no_gpu_auto
            or (device == "cpu" and status.get("fallback_reason"))):
        raise RuntimeError(f"{label} did not stay on {device}: {status}")
