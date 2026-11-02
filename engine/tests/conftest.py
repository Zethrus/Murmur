"""No test is allowed to contact a service or load an actual model."""
from pathlib import Path
from unittest.mock import Mock
import socket
import pytest
from murmur_engine.models import Models
from murmur_engine.devices import Hardware


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "1")
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("Network forbidden in tests")))
    monkeypatch.setattr("murmur_engine.stt.detect", lambda: Hardware(0))
    monkeypatch.setattr("faster_whisper.WhisperModel", Mock(side_effect=AssertionError("Real models forbidden in tests")))
    monkeypatch.setattr("huggingface_hub.hf_hub_download", Mock(side_effect=AssertionError("Download must be mocked")))
    monkeypatch.setattr("huggingface_hub.list_repo_files", Mock(side_effect=AssertionError("Repository calls must be mocked")))


@pytest.fixture
def models(tmp_path: Path) -> Models:
    return Models(tmp_path / "models")
