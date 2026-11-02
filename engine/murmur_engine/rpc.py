"""Synchronous newline-delimited JSON RPC; one response for every request."""
from dataclasses import dataclass
from typing import Any, TextIO
import json
from .models import Models
from .stt import Speech


@dataclass
class Server:
    speech: Speech
    models: Models
    output: TextIO
    stopping: bool = False

    def emit(self, message: dict[str, Any]) -> None:
        self.output.write(json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n")
        self.output.flush()

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping":
            return {"pong": True}
        if method == "status":
            self.speech.configure(params)
            return self.speech.status()
        if method in ("transcribe", "partial"):
            return self.speech.transcribe(params)
        if method == "models.list":
            return self.models.list()
        if method == "models.install":
            return self.models.install(params["name"], self.emit)
        if method == "models.remove":
            if params["name"] == self.speech.model:
                self.speech.unload()
            return self.models.remove(params["name"])
        if method == "shutdown":
            self.stopping = True
            self.speech.unload()
            return {"shutdown": True}
        raise ValueError(f"Unknown method: {method}")

    def handle(self, line: str) -> None:
        request_id: int | str | None = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Request must be an object")
            request_id = request.get("id")
            if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
                request_id = None
                raise ValueError("Request requires an integer or string id")
            method, params = request.get("method"), request.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("Expected method string and params object")
            result = self.dispatch(method, params)
            self.emit({"id": request_id, "ok": True, "result": result})
        except Exception as exc:
            # Never log input, PCM or transcript. The error belongs to this caller only.
            self.emit({"id": request_id, "ok": False,
                       "error": {"message": str(exc), "type": type(exc).__name__}})

    def run(self, source: TextIO) -> None:
        while not self.stopping:
            line = source.readline(5_300_000)
            if not line:
                break
            if len(line) >= 5_300_000 and not line.endswith("\n"):
                self.emit({"id": None, "ok": False, "error": {"message": "Request too large"}})
                break
            if line.strip():
                self.handle(line)
