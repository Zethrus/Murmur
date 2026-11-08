from pathlib import Path
import pytest
from murmur_engine.models import Models


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
    try:
        (models.directory / "tiny.en").symlink_to(tmp_path, target_is_directory=True)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:  # ERROR_PRIVILEGE_NOT_HELD
            pytest.skip("symlink creation needs SeCreateSymbolicLinkPrivilege (non-admin Windows runner)")
        raise
    with pytest.raises(ValueError):
        models.remove("tiny.en")


def test_install_is_refused_without_network(models: Models) -> None:
    with pytest.raises(RuntimeError, match="managed by the Murmur app"):
        models.install("tiny.en")
    assert not models.directory.exists()


def test_installed_requires_marker_and_files(models: Models) -> None:
    path = models.directory / "tiny.en"
    path.mkdir(parents=True)
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (path / name).write_bytes(b"x")
    assert not models.installed("tiny.en")
    (path / ".complete").write_text("1\n")
    assert models.installed("tiny.en")
    assert not models.remove("tiny.en")["installed"]
    assert not path.exists()
