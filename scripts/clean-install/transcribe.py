"""CPU transcription with the installed engine and model components (no network)."""
import array
import base64
import json
from pathlib import Path
import struct
import subprocess
import sys
import wave

store, fixture = Path(sys.argv[1]), sys.argv[2]


def active(component):
    record = json.loads((store / component / "active.json").read_text())
    return store / component / record["version"]


engine = active("engine") / "murmur-engine"
model = next(p for p in store.glob("speech-*") if (p / "active.json").exists())
with wave.open(fixture) as sound:
    raw = sound.readframes(sound.getnframes())
samples = array.array("h", raw)
pcm = base64.b64encode(struct.pack(f"<{len(samples)}f", *(s / 32768 for s in samples))).decode()
requests = [{"id": 1, "method": "transcribe", "params": {"model": model.name.removeprefix("speech-"),
             "model_path": str(active(model.name)), "device": "cpu", "compute_type": "int8",
             "pcm_b64": pcm, "sample_rate": 16000}}, {"id": 2, "method": "shutdown"}]
result = subprocess.run([str(engine), "--models-dir", "/nonexistent"], input="".join(json.dumps(r) + "\n" for r in requests),
                        capture_output=True, text=True, timeout=300)
reply = json.loads(result.stdout.splitlines()[0])
if not reply.get("ok") or "fellow americans" not in reply["result"]["text"].lower():
    sys.exit(f"transcription failed: {reply} {result.stderr[-500:]}")
print(f"CPU transcription with installed components: {reply['result']['text']!r} ({reply['result']['infer_ms']} ms)")
