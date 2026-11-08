"""Process setup shared by ``python -m murmur_engine`` and the frozen engine.

Nothing here contacts a service. It fixes the offline defaults, makes an
optional CUDA directory visible to the Windows DLL loader, and replaces PyAV
with a stub when the frozen engine omits it.
"""
import importlib.util
import logging
import os
from pathlib import Path
import sys
import types
from typing import Any, Callable, MutableMapping

CUDA_ENV = "MURMUR_CUDA_DIR"
# Keeps os.add_dll_directory registrations alive for the process lifetime.
_DLL_DIRECTORIES: list[Any] = []


def configure_environment(environ: MutableMapping[str, str] = os.environ) -> None:
    """Always offline: the app downloads and verifies models; the engine never does."""
    environ["HF_HUB_OFFLINE"] = "1"
    environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    environ["DO_NOT_TRACK"] = "1"
    # The frozen engine omits hf_xet; keep huggingface_hub from looking for it.
    environ["HF_HUB_DISABLE_XET"] = "1"


def cuda_directory(environ: MutableMapping[str, str] = os.environ) -> Path | None:
    """Return the absolute, existing directory named by MURMUR_CUDA_DIR, if any."""
    value = environ.get(CUDA_ENV, "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        logging.warning("Ignoring %s: not an absolute directory", CUDA_ENV)
        return None
    return path


def add_cuda_directory(environ: MutableMapping[str, str] = os.environ, platform: str = os.name,
                       add: Callable[[str], Any] | None = None) -> Path | None:
    """Register MURMUR_CUDA_DIR with the Windows DLL loader.

    Linux reads LD_LIBRARY_PATH only at process start, so the desktop app sets it
    before spawning the engine (src-tauri/src/engine.rs). Nothing to do here.
    """
    if platform != "nt":
        return None
    path = cuda_directory(environ)
    if path is None:
        return None
    register = add or getattr(os, "add_dll_directory")
    try:
        _DLL_DIRECTORIES.append(register(str(path)))
    except OSError as error:
        logging.warning("Could not add %s to the DLL search path: %s", CUDA_ENV, error)
        return None
    return path


def _missing_pyav(name: str) -> Any:
    raise AttributeError(f"PyAV is not bundled with Murmur (requested av.{name}); "
                         "send 16 kHz float32 PCM instead of encoded audio")


def install_av_stub(find_spec: Callable[[str], Any] = importlib.util.find_spec,
                    modules: MutableMapping[str, Any] = sys.modules) -> bool:
    """faster-whisper imports PyAV at module load but only uses it to decode files.

    Murmur always passes decoded PCM, so the frozen engine leaves out PyAV and its
    FFmpeg libraries. Install a stub only when the real package is absent.
    """
    if "av" in modules or find_spec("av") is not None:
        return False
    stub = types.ModuleType("av", "Placeholder: PyAV is not bundled with Murmur.")
    stub.__getattr__ = _missing_pyav  # type: ignore[method-assign]
    modules["av"] = stub
    return True


def prepare() -> None:
    configure_environment()
    add_cuda_directory()
    install_av_stub()
