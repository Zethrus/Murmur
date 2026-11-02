from io import StringIO
from unittest.mock import Mock
import json
import pytest
from murmur_engine.rpc import Server
from murmur_engine.models import Models
from murmur_engine.stt import Speech


def server(models: Models) -> tuple[Server, StringIO]:
    output = StringIO()
    return Server(Speech(models), models, output), output


def test_ping_and_shutdown(models: Models) -> None:
    rpc, out = server(models)
    rpc.run(StringIO('{"id":1,"method":"ping"}\n{"id":2,"method":"shutdown"}\n{"id":3,"method":"ping"}\n'))
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(lines) == 2
    assert lines[0] == {"id": 1, "ok": True, "result": {"pong": True}}
    assert rpc.stopping


@pytest.mark.parametrize("line", ["no json", "[]", "{}", '{"id":true,"method":"ping"}',
                                      '{"id":1,"method":3}', '{"id":1,"method":"ping","params":[]}'])
def test_bad_request_recovers(models: Models, line: str) -> None:
    rpc, out = server(models)
    rpc.run(StringIO(line + '\n{"id":2,"method":"ping"}\n'))
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert not lines[0]["ok"]
    assert lines[1]["result"] == {"pong": True}


def test_unknown_method_preserves_id(models: Models) -> None:
    rpc, out = server(models)
    rpc.handle('{"id":"abc","method":"missing"}')
    message = json.loads(out.getvalue())
    assert message["id"] == "abc" and not message["ok"]


def test_status_does_not_load(models: Models) -> None:
    rpc, out = server(models)
    rpc.handle('{"id":1,"method":"status"}')
    status = json.loads(out.getvalue())["result"]
    assert status["device"] == "unloaded" and not status["model_available"]


@pytest.mark.parametrize("method", ["transcribe", "partial"])
def test_transcription_dispatch(models: Models, method: str) -> None:
    rpc, out = server(models)
    rpc.speech.transcribe = Mock(return_value={"text": "local"})
    rpc.handle(json.dumps({"id": 1, "method": method, "params": {"sample_rate": 16000}}))
    assert json.loads(out.getvalue())["result"] == {"text": "local"}
    rpc.speech.transcribe.assert_called_once_with({"sample_rate": 16000})


def test_download_events_before_response(models: Models) -> None:
    rpc, out = server(models)
    def install(name: str, emit: object) -> dict[str, bool]:
        emit({"event": "download", "name": name, "progress": 0.5})
        return {"installed": True}
    rpc.models.install = Mock(side_effect=install)
    rpc.handle('{"id":4,"method":"models.install","params":{"name":"tiny.en"}}')
    messages = [json.loads(line) for line in out.getvalue().splitlines()]
    assert messages[0]["event"] == "download"
    assert messages[1]["id"] == 4 and messages[1]["ok"]


def test_remove_unloads_selected_model(models: Models) -> None:
    rpc, out = server(models)
    rpc.speech._session = object()
    rpc.handle('{"id":1,"method":"models.remove","params":{"name":"distil-small.en"}}')
    assert rpc.speech._session is None
    assert json.loads(out.getvalue())["ok"]
