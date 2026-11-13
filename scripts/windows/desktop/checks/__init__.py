"""One module per checklist group; each check is a function named by its id (catalog.py).

Modules import Windows-only helpers, so they are loaded only when a check runs (lookup()).
"""
import importlib

MODULES = {"setup": "checks.setup", "settings": "checks.settings", "overlay": "checks.overlay",
           "hotkeys": "checks.hotkeys", "insertion": "checks.insertion", "focus": "checks.focus",
           "tray": "checks.tray", "microphone": "checks.microphone", "keyboard": "checks.keyboard",
           "components": "checks.components"}


def lookup(check):
    """The function implementing a catalog.Check."""
    module = importlib.import_module(MODULES[check.group])
    return getattr(module, check.function)
