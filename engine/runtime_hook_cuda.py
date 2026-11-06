"""Make Windows-bundled CUDA libraries available to the frozen engine.

Linux desktop builds stage CUDA beside the engine and set the loader path before spawn.
Windows onefile builds still use this hook for their bundled DLLs.
"""
import os
import sys

_BUNDLE = getattr(sys, "_MEIPASS", "")
_SUBDIRS = ("lib", "bin")
_PACKAGES = ("cudnn", "cublas", "cuda_nvrtc")

if os.name == "nt" and _BUNDLE:
    found = [
        os.path.join(_BUNDLE, "nvidia", name, sub)
        for name in _PACKAGES
        for sub in _SUBDIRS
        if os.path.isdir(os.path.join(_BUNDLE, "nvidia", name, sub))
    ]
    if found:
        for path in found:
            try:
                os.add_dll_directory(path)
            except (AttributeError, OSError):
                pass
