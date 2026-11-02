"""Explicit installs only. A completion marker distinguishes usable downloads."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
import os
import shutil


@dataclass(frozen=True)
class Model:
    name: str
    repo: str
    size_mb: int
    accuracy: str


def catalog() -> tuple[Model, ...]:
    return (
        Model("tiny.en", "Systran/faster-whisper-tiny.en", 75, "fastest"),
        Model("base.en", "Systran/faster-whisper-base.en", 145, "basic"),
        Model("small.en", "Systran/faster-whisper-small.en", 485, "balanced"),
        Model("distil-small.en", "Systran/faster-distil-whisper-small.en", 335, "balanced"),
        Model("distil-large-v3", "Systran/faster-distil-whisper-large-v3", 1520, "high"),
        Model("large-v3", "Systran/faster-whisper-large-v3", 3100, "highest"),
        Model("large-v3-turbo", "dropbox-dash/faster-whisper-large-v3-turbo", 1620, "high"),
    )


@contextmanager
def explicit_network() -> Iterator[None]:
    # huggingface_hub caches the offline flag at import, so update both forms.
    from huggingface_hub import constants
    previous = os.environ.get("HF_HUB_OFFLINE")
    old_constant = constants.HF_HUB_OFFLINE
    os.environ["HF_HUB_OFFLINE"] = "0"
    constants.HF_HUB_OFFLINE = False
    try:
        yield
    finally:
        constants.HF_HUB_OFFLINE = old_constant
        if previous is None:
            os.environ["HF_HUB_OFFLINE"] = "1"
        else:
            os.environ["HF_HUB_OFFLINE"] = previous


@dataclass
class Models:
    directory: Path

    def definition(self, name: str) -> Model:
        for model in catalog():
            if model.name == name:
                return model
        raise ValueError(f"Unknown model: {name!r}")

    def path(self, name: str) -> Path:
        self.definition(name)
        root = self.directory.resolve()
        path = root / name
        if path.is_symlink() or path.resolve().parent != root:
            raise ValueError("Model path must not be a symlink or escape the model directory")
        return path

    def installed(self, name: str) -> bool:
        path = self.path(name)
        return all((path / item).is_file() for item in (".complete", "model.bin", "config.json", "tokenizer.json"))

    def list(self) -> list[dict[str, Any]]:
        return [dict(asdict(item), installed=self.installed(item.name)) for item in catalog()]

    def install(self, name: str, emit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        model, path = self.definition(name), self.path(name)
        if self.installed(name):
            return {"name": name, "installed": True}
        path.mkdir(parents=True, exist_ok=True)
        emit({"event": "download", "name": name, "phase": "starting", "progress": 0})
        from huggingface_hub import hf_hub_download, list_repo_files
        try:
            with explicit_network():
                files = [item for item in list_repo_files(model.repo) if item in
                         ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt", "vocabulary.json", "preprocessor_config.json")]
                required = {"model.bin", "config.json", "tokenizer.json"}
                if not required.issubset(files):
                    raise RuntimeError("Model repository is missing required inference files")
                for index, filename in enumerate(files):
                    hf_hub_download(repo_id=model.repo, filename=filename, local_dir=str(path), local_files_only=False)
                    emit({"event": "download", "name": name, "phase": "downloading",
                          "file": filename, "progress": (index + 1) / len(files)})
            (path / ".complete").write_text("1\n", encoding="utf-8")
        except Exception:
            emit({"event": "download", "name": name, "phase": "error"})
            raise
        emit({"event": "download", "name": name, "phase": "complete", "progress": 1})
        return {"name": name, "installed": True}

    def remove(self, name: str) -> dict[str, Any]:
        path = self.path(name)
        if path.exists():
            shutil.rmtree(path)
        return {"name": name, "installed": False}
