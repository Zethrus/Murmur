"""Make bundled CUDA libraries loadable before ctranslate2 imports.

PyInstaller unpacks `nvidia.cudnn` / `nvidia.cublas` binaries into `_MEIPASS/nvidia/*/lib`
(Linux) or `.../bin` (Windows). ctranslate2 dlopen()s them by soname, so the loader path has
to include those directories before `faster_whisper` is imported.
"""
import os
import sys

_BUNDLE = getattr(sys, "_MEIPASS", "")
_SUBDIRS = ("lib", "bin")
_PACKAGES = ("cudnn", "cublas", "cuda_nvrtc")

if _BUNDLE:
    found = [
        os.path.join(_BUNDLE, "nvidia", name, sub)
        for name in _PACKAGES
        for sub in _SUBDIRS
        if os.path.isdir(os.path.join(_BUNDLE, "nvidia", name, sub))
    ]
    if found:
        separator = ";" if os.name == "nt" else ":"
        current = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = separator.join(found + ([current] if current else []))
        if os.name == "nt":
            for path in found:
                try:
                    os.add_dll_directory(path)
                except (AttributeError, OSError):
                    pass
