"""Lazy, warm faster-whisper session with sticky automatic CPU fallback."""
from dataclasses import dataclass, field
from typing import Any
import base64
import binascii
import logging
import time
import numpy as np
from .models import Models
from .devices import detect


@dataclass
class Speech:
    models: Models
    model: str = "distil-small.en"
    requested_device: str = "auto"
    requested_compute: str = "auto"
    device: str = "unloaded"
    compute: str = "unloaded"
    cpu_fallback: bool = False
    _session: Any = field(default=None, repr=False)
    _key: tuple[str, str, str] | None = None
    _attempted_device: str = "unloaded"

    def configure(self, params: dict[str, Any]) -> None:
        model = params.get("model", self.model)
        device = params.get("device", self.requested_device)
        compute = params.get("compute_type", self.requested_compute)
        if not isinstance(model, str) or device not in ("auto", "cpu", "cuda"):
            raise ValueError("Invalid engine configuration")
        if compute not in ("auto", "int8", "float16", "int8_float16", "float32", "int8_float32"):
            raise ValueError("Invalid compute type")
        if model:
            self.models.definition(model)
        if (model, device, compute) != (self.model, self.requested_device, self.requested_compute):
            self.unload()
        self.model, self.requested_device, self.requested_compute = model, device, compute

    def unload(self) -> None:
        self._session = None
        self._key = None
        self.device = self.compute = "unloaded"

    def status(self) -> dict[str, Any]:
        return {"model": self.model, "device": self.device, "compute": self.compute,
                "model_available": bool(self.model) and self.models.installed(self.model),
                "loaded": self._session is not None, "cpu_fallback": self.cpu_fallback,
                "hardware": detect().to_dict()}

    def _load(self) -> None:
        if not self.model or not self.models.installed(self.model):
            raise RuntimeError("Choose a speech model to start dictating")
        device = self.requested_device
        if device == "auto":
            if not self.cpu_fallback and not detect().cuda_devices:
                self.cpu_fallback = True
            device = "cpu" if self.cpu_fallback else "cuda"
        self._attempted_device = device
        compute = self.requested_compute
        if compute == "auto" or (self.cpu_fallback and self.requested_device == "auto"):
            compute = "int8_float16" if device == "cuda" else "int8"
        key = (self.model, device, compute)
        if key == self._key and self._session is not None:
            return
        from faster_whisper import WhisperModel
        self._session = WhisperModel(str(self.models.path(self.model)), device=device,
                                     compute_type=compute, local_files_only=True)
        self.device, self.compute, self._key = device, compute, key

    def _infer(self, pcm: np.ndarray, params: dict[str, Any]) -> tuple[str, str]:
        self._load()
        segments, info = self._session.transcribe(
            pcm, language=params.get("language") or None,
            task="translate" if params.get("translate", False) else "transcribe",
            beam_size=params.get("beam_size", 1), vad_filter=params.get("vad", True),
            initial_prompt=params.get("initial_prompt") or None,
        )
        # Iterating executes inference; CUDA failures can occur here, not just at load.
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text, info.language

    def transcribe(self, params: dict[str, Any]) -> dict[str, Any]:
        self.configure(params)
        if params.get("sample_rate") != 16000:
            raise ValueError("Only mono 16000 Hz little-endian float32 PCM is supported")
        encoded = params.get("pcm_b64", "")
        if not isinstance(encoded, str) or len(encoded) > 5_120_000:
            raise ValueError("PCM payload exceeds 60 seconds")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid PCM base64") from exc
        if len(raw) % 4 or not raw or len(raw) > 16000 * 60 * 4:
            raise ValueError("PCM must contain 1–960000 float32 samples")
        pcm = np.frombuffer(raw, dtype="<f4")
        if not np.isfinite(pcm).all():
            raise ValueError("PCM contains non-finite samples")
        if not isinstance(params.get("beam_size", 1), int) or not 1 <= params.get("beam_size", 1) <= 10:
            raise ValueError("Beam size must be 1–10")
        start = time.perf_counter()
        try:
            text, language = self._infer(pcm, params)
        except (RuntimeError, OSError):
            if self.requested_device != "auto" or self.cpu_fallback or self._attempted_device != "cuda":
                raise
            logging.warning("Automatic engine fallback to CPU")
            self.cpu_fallback = True
            self.unload()
            text, language = self._infer(pcm, params)
        return {"text": text, "language": language, "duration_ms": len(pcm) / 16,
                "infer_ms": round((time.perf_counter() - start) * 1000, 2)}
