from types import SimpleNamespace
from unittest.mock import Mock
import base64
import numpy as np
import pytest
from murmur_engine.devices import Hardware
from murmur_engine.models import Models
from murmur_engine.stt import Speech


def pcm(**kwargs: object) -> dict[str, object]:
    return dict({"pcm_b64": base64.b64encode(np.zeros(1600, dtype="<f4")).decode(),
                 "sample_rate": 16000}, **kwargs)


def fake_session() -> Mock:
    session = Mock()
    session.transcribe.side_effect = lambda *args, **kwargs: (
        iter([SimpleNamespace(text=" hello "), SimpleNamespace(text="world")]), SimpleNamespace(language="en"))
    return session


def available(models: Models, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "installed", lambda name: True)


def test_missing_model_never_downloads(models: Models) -> None:
    speech = Speech(models)
    with pytest.raises(RuntimeError, match="Choose a speech model"):
        speech.transcribe(pcm())
    assert not speech.cpu_fallback


def test_lazy_warm_cpu_and_options(models: Models, monkeypatch: pytest.MonkeyPatch) -> None:
    available(models, monkeypatch)
    session = fake_session()
    constructor = Mock(return_value=session)
    monkeypatch.setattr("faster_whisper.WhisperModel", constructor)
    speech = Speech(models)
    assert speech._session is None
    result = speech.transcribe(pcm(language="", translate=True, beam_size=2, vad=False, initial_prompt="Murmur"))
    assert result["text"] == "hello world" and result["duration_ms"] == 100
    options = session.transcribe.call_args.kwargs
    assert options == {"language": None, "task": "translate", "beam_size": 2, "vad_filter": False, "initial_prompt": "Murmur"}
    speech.transcribe(pcm())
    assert constructor.call_count == 1
    assert constructor.call_args.kwargs["local_files_only"] is True
    assert speech.status()["device"] == "cpu" and speech.compute == "int8"


def test_cuda_load_failure_sticky_fallback(models: Models, monkeypatch: pytest.MonkeyPatch) -> None:
    available(models, monkeypatch)
    monkeypatch.setattr("murmur_engine.stt.detect", lambda: Hardware(1))
    constructor = Mock(side_effect=[RuntimeError("CUDA missing"), fake_session()])
    monkeypatch.setattr("faster_whisper.WhisperModel", constructor)
    speech = Speech(models)
    speech.transcribe(pcm())
    speech.transcribe(pcm())
    assert [call.kwargs["device"] for call in constructor.call_args_list] == ["cuda", "cpu"]
    assert speech.cpu_fallback and speech.compute == "int8"


def test_cuda_generator_failure_falls_back(models: Models, monkeypatch: pytest.MonkeyPatch) -> None:
    available(models, monkeypatch)
    monkeypatch.setattr("murmur_engine.stt.detect", lambda: Hardware(1))
    def broken():
        raise RuntimeError("cuDNN failure")
        yield
    gpu = Mock()
    gpu.transcribe.return_value = (broken(), SimpleNamespace(language="en"))
    monkeypatch.setattr("faster_whisper.WhisperModel", Mock(side_effect=[gpu, fake_session()]))
    speech = Speech(models)
    assert speech.transcribe(pcm())["text"] == "hello world"
    assert speech.device == "cpu"


def test_explicit_cuda_error_is_reported(models: Models, monkeypatch: pytest.MonkeyPatch) -> None:
    available(models, monkeypatch)
    monkeypatch.setattr("faster_whisper.WhisperModel", Mock(side_effect=RuntimeError("CUDA")))
    speech = Speech(models)
    with pytest.raises(RuntimeError):
        speech.transcribe(pcm(device="cuda"))
    assert not speech.cpu_fallback


def test_cpu_error_does_not_retry(models: Models, monkeypatch: pytest.MonkeyPatch) -> None:
    available(models, monkeypatch)
    constructor = Mock(side_effect=RuntimeError("bad model"))
    monkeypatch.setattr("faster_whisper.WhisperModel", constructor)
    with pytest.raises(RuntimeError, match="bad model"):
        Speech(models).transcribe(pcm())
    assert constructor.call_count == 1


def test_config_change_unloads(models: Models, monkeypatch: pytest.MonkeyPatch) -> None:
    available(models, monkeypatch)
    monkeypatch.setattr("faster_whisper.WhisperModel", Mock(return_value=fake_session()))
    speech = Speech(models)
    speech.transcribe(pcm())
    speech.configure({"model": "tiny.en"})
    assert speech._session is None


@pytest.mark.parametrize("patch", [
    {"sample_rate": 44100}, {"pcm_b64": "%%%"}, {"pcm_b64": "YQ=="}, {"pcm_b64": ""},
    {"pcm_b64": base64.b64encode(np.array([float("nan")], dtype="<f4")).decode()},
    {"beam_size": 0}, {"device": "other"}, {"compute_type": "other"},
])
def test_invalid_pcm_or_options(models: Models, patch: dict[str, object]) -> None:
    params = pcm()
    params.update(patch)
    with pytest.raises(ValueError):
        Speech(models).transcribe(params)
