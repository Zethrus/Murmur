"""Partial config.toml writer for the throwaway test profile. Pure Python.

Murmur fills every missing key with its default (src-tauri/src/config.rs, `#[serde(default)]`),
so the harness writes only what a check needs. Keys are checked against the real schema below
(copied from config.rs; a typo would otherwise be ignored silently by the app and a check would
test the wrong thing). Value checks mirror Config::validate for the keys the harness uses.
"""
import math
import re

# section -> key -> type (subset of config.rs that the harness may write)
SCHEMA = {
    "general": {"hotkey": str, "hotkey_mode": str, "language": str, "translate": bool, "autostart": bool,
                "theme": str, "hotkey_code": str},
    "overlay": {"enabled": bool, "position": str, "scale": float, "show_preview": bool, "idle_view": str},
    "overlay.placement": {"monitor_name": str, "monitor_x": int, "monitor_y": int, "monitor_width": int,
                          "monitor_height": int, "x_fraction": float, "y_fraction": float},
    "sounds": {"enabled": bool, "volume": float, "muted": bool},
    "engine": {"model": str, "device": str, "compute_type": str, "beam_size": int, "vad_filter": bool},
    "audio": {"device": str, "gain": float, "silence_threshold": float, "silence_timeout_ms": int,
              "max_utterance_s": int, "min_utterance_ms": int},
    "injection": {"method": str, "clipboard_only": bool, "trailing_space": bool, "capitalize": bool,
                  "remove_fillers": bool, "exclude_apps": list, "prefer_paste": bool, "focus_change": str,
                  "insert_held_hotkey": str},
    "postproc": {"replacements": dict, "style": str, "spoken_punctuation": bool, "allow_newlines": bool,
                 "remove_duplicate_words": bool, "code_apps": list},
    "privacy": {"history_enabled": bool, "history_retention_days": int},
    "setup": {"postponed": bool},
}
CHOICES = {
    ("general", "hotkey_mode"): ("hold", "toggle"),
    ("general", "theme"): ("auto", "light", "dark"),
    ("overlay", "position"): ("bottom-center", "top-center", "custom"),
    ("overlay", "idle_view"): ("icon", "pill", "hidden"),
    ("engine", "device"): ("auto", "cuda", "cpu"),
    ("injection", "method"): ("auto", "type", "clipboard"),
    ("injection", "focus_change"): ("hold", "refocus", "copy", "insert"),
    ("postproc", "style"): ("prose", "code", "auto"),
}
BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(ValueError):
    pass


def merge(base, patch):
    """Deep merge like the app's config patching: tables merge, `replacements` is replaced."""
    result = {key: (dict(value) if isinstance(value, dict) else value) for key, value in base.items()}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict) and key != "replacements":
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def _flatten(config, prefix=""):
    """section path -> {key: value}; nested tables except `replacements` become sub-sections."""
    sections = {}
    for key, value in config.items():
        path = f"{prefix}.{key}" if prefix else key
        if not isinstance(value, dict):
            raise ConfigError(f"Top-level value {path!r} must be a table")
        own = sections[path] = {}
        for name, item in value.items():
            if isinstance(item, dict) and name != "replacements":
                sections.update(_flatten({name: item}, path))
            else:
                own[name] = item
    return sections


def validate(config):
    for section, values in _flatten(config).items():
        if section not in SCHEMA:
            raise ConfigError(f"Unknown config section [{section}]")
        for name, value in values.items():
            expected = SCHEMA[section].get(name)
            if expected is None:
                raise ConfigError(f"Unknown config key {section}.{name}")
            if expected is float and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise ConfigError(f"{section}.{name} must be {expected.__name__}, got {value!r}")
            choices = CHOICES.get((section, name))
            if choices and value not in choices:
                raise ConfigError(f"{section}.{name} must be one of {choices}, got {value!r}")
            if isinstance(value, list) and not all(isinstance(v, str) for v in value):
                raise ConfigError(f"{section}.{name} must be a list of strings")
            if isinstance(value, dict) and not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                raise ConfigError(f"{section}.{name} must map strings to strings")
    scale = config.get("overlay", {}).get("scale")
    if scale is not None and not 0.8 <= scale <= 1.6:
        raise ConfigError("overlay.scale must be 0.8-1.6")
    timeout = config.get("audio", {}).get("silence_timeout_ms")
    if timeout is not None and not 100 <= timeout <= 60000:
        raise ConfigError("audio.silence_timeout_ms must be 100-60000")


def quote(text):
    """TOML basic string (UTF-8 kept as is; control characters escaped)."""
    out = ['"']
    for char in text:
        if char in '"\\':
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _key(name):
    return name if BARE_KEY.match(name) else quote(name)


def _value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigError("Non-finite float")
        return repr(value)
    if isinstance(value, str):
        return quote(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }"
    raise ConfigError(f"Unsupported value {value!r}")


def dumps(config):
    """Validated TOML text; sections and keys in the given order."""
    validate(config)
    blocks = []
    for section, values in _flatten(config).items():
        lines = [f"[{section}]"] + [f"{_key(name)} = {_value(value)}" for name, value in values.items()]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
