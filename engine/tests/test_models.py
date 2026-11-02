from pathlib import Path
from unittest.mock import Mock
import os
import pytest
from murmur_engine.models import Models, explicit_network


def test_list_is_offline_and_complete(models: Models) -> None:
    items = models.list()
    assert len(items) == 7 and len({item["name"] for item in items}) == 7
    assert any(item["name"] == "large-v3-turbo" for item in items)
    assert not any(item["installed"] for item in items)
    assert not models.directory.exists()


@pytest.mark.parametrize("name", ["../escape", "/tmp/model", "unknown", "tiny.en/../base.en"])
def test_reject_unlisted_paths(models: Models, name: str) -> None:
    with pytest.raises(ValueError):
        models.path(name)


def test_symlink_rejected(models: Models, tmp_path: Path) -> None:
    models.directory.mkdir()
    (models.directory / "tiny.en").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        models.remove("tiny.en")


def test_install_and_remove(models: Models, monkeypatch: pytest.MonkeyPatch) -> None:
    files = ["model.bin", "config.json", "tokenizer.json"]
    monkeypatch.setattr("huggingface_hub.list_repo_files", Mock(return_value=files))
    def download(**kwargs: object) -> str:
        assert os.environ["HF_HUB_OFFLINE"] == "0"
        path = Path(str(kwargs["local_dir"])) / str(kwargs["filename"])
        path.write_bytes(b"mock model fixture, not an actual model")
        return str(path)
    mock = Mock(side_effect=download)
    monkeypatch.setattr("huggingface_hub.hf_hub_download", mock)
    events: list[dict[str, object]] = []
    assert models.install("tiny.en", events.append)["installed"]
    assert mock.call_count == 3
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert models.installed("tiny.en")
    assert events[0]["progress"] == 0 and events[-1]["progress"] == 1
    models.install("tiny.en", events.append)
    assert mock.call_count == 3
    assert not models.remove("tiny.en")["installed"]
    assert not models.installed("tiny.en")


def test_failed_download_remains_uninstalled(models: Models, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("huggingface_hub.list_repo_files", Mock(side_effect=RuntimeError("offline")))
    events: list[dict[str, object]] = []
    with pytest.raises(RuntimeError):
        models.install("base.en", events.append)
    assert not models.installed("base.en")
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert events[-1]["phase"] == "error"


def test_offline_flag_restored_on_error() -> None:
    from huggingface_hub import constants
    previous = constants.HF_HUB_OFFLINE
    with pytest.raises(RuntimeError):
        with explicit_network():
            assert not constants.HF_HUB_OFFLINE
            raise RuntimeError("test")
    assert constants.HF_HUB_OFFLINE == previous
    assert os.environ["HF_HUB_OFFLINE"] == "1"
