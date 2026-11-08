"""Speech model catalog and pre-0.5 on-disk lookup.

Since 0.5 the app downloads, verifies and stores speech models itself (component manager)
and passes the verified directory as `model_path`. The engine never downloads anything.
"""
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import shutil

REQUIRED = ("model.bin", "config.json", "tokenizer.json")


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
        return all((path / item).is_file() for item in (".complete", *REQUIRED))

    def list(self) -> list[dict[str, Any]]:
        return [dict(asdict(item), installed=self.installed(item.name)) for item in catalog()]

    def install(self, name: str, emit: Any = None) -> dict[str, Any]:
        raise RuntimeError("Speech model installs are managed by the Murmur app")

    def remove(self, name: str) -> dict[str, Any]:
        path = self.path(name)
        if path.exists():
            shutil.rmtree(path)
        return {"name": name, "installed": False}
