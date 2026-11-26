#!/usr/bin/env python3
"""Three bounded clipboard metadata snapshots; never retrieve or change payloads."""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import time

import win32
from idle_diagnostic import interactive_session, session_identity, utc_now

STANDARD_NAMES = {1: "CF_TEXT", 2: "CF_BITMAP", 3: "CF_METAFILEPICT", 4: "CF_SYLK",
                  5: "CF_DIF", 6: "CF_TIFF", 7: "CF_OEMTEXT", 8: "CF_DIB",
                  9: "CF_PALETTE", 10: "CF_PENDATA", 11: "CF_RIFF", 12: "CF_WAVE",
                  13: "CF_UNICODETEXT", 14: "CF_ENHMETAFILE", 15: "CF_HDROP",
                  16: "CF_LOCALE", 17: "CF_DIBV5"}
TEXT_FORMATS = {1, 7, 13, 16}
MAX_FORMATS = 256


class NativeMetadata:
    """The only native operations this diagnostic permits (no GetClipboardData)."""
    def __init__(self):
        win32.require_windows()
        self.api = win32.user32
        self.api.GetClipboardFormatNameW.argtypes = (wintypes.UINT, wintypes.LPWSTR, ctypes.c_int)
        self.api.GetClipboardFormatNameW.restype = ctypes.c_int
        self.api.GetClipboardSequenceNumber.argtypes = ()
        self.api.GetClipboardSequenceNumber.restype = wintypes.DWORD

    def sequence(self):
        return int(self.api.GetClipboardSequenceNumber())

    def open(self):
        ctypes.set_last_error(0)
        ok = bool(self.api.OpenClipboard(None))
        return ok, ctypes.get_last_error() if not ok else 0

    def close(self):
        ctypes.set_last_error(0)
        ok = bool(self.api.CloseClipboard())
        return ok, ctypes.get_last_error() if not ok else 0

    def next_format(self, previous):
        ctypes.set_last_error(0)
        value = int(self.api.EnumClipboardFormats(previous))
        return value, ctypes.get_last_error() if not value else 0

    def name(self, format_id):
        buffer = ctypes.create_unicode_buffer(256)
        ctypes.set_last_error(0)
        length = self.api.GetClipboardFormatNameW(format_id, buffer, len(buffer))
        return buffer.value if length else None, ctypes.get_last_error() if not length else 0, length == 255

    def unicode_available(self):
        return bool(self.api.IsClipboardFormatAvailable(win32.CF_UNICODETEXT))


def snapshot(native):
    result = {"utc": utc_now(), "sequence_before": native.sequence(), "formats": [],
              "enumeration_complete": False, "errors": []}
    opened, error = native.open()
    if not opened:
        result["errors"].append({"operation": "OpenClipboard", "code": error})
    else:
        try:
            result["sequence_open"] = native.sequence()
            result["unicode_available"] = native.unicode_available()
            previous, seen = 0, set()
            for _ in range(MAX_FORMATS + 1):
                value, error = native.next_format(previous)
                if not value:
                    result["enumeration_complete"] = error == 0
                    if error:
                        result["errors"].append({"operation": "EnumClipboardFormats", "code": error})
                    break
                if value in seen or len(seen) == MAX_FORMATS:
                    result["errors"].append({"operation": "EnumClipboardFormats", "reason": "duplicate_or_limit"})
                    break
                seen.add(value)
                item = {"id": value, "name": STANDARD_NAMES.get(value), "text_allowlisted": value in TEXT_FORMATS}
                if 0xC000 <= value <= 0xFFFF:
                    item["name"], item["name_error"], item["name_may_be_truncated"] = native.name(value)
                    if item["name"] is None or item["name_may_be_truncated"]:
                        result["errors"].append({"operation": "GetClipboardFormatNameW", "format_id": value,
                                                 "code": item["name_error"]})
                result["formats"].append(item)
                previous = value
            result["unsupported_format_ids"] = sorted(seen - TEXT_FORMATS)
        finally:
            closed, error = native.close()
            if not closed:
                result["errors"].append({"operation": "CloseClipboard", "code": error})
    result["sequence_after"] = native.sequence()
    result["sequence_changed"] = result["sequence_before"] != result["sequence_after"]
    result["status"] = "ok" if result["enumeration_complete"] and not result["errors"] else "read_error"
    return result


def diagnose(native=None, *, identity=session_identity, sleep=time.sleep):
    result = {"status": "running", "started_utc": utc_now(), "samples": [],
              "payload_accessed": False, "clipboard_modified": False}
    first = None
    try:
        for index in range(3):
            current = identity()
            result["last_session"] = current
            if first is None:
                first = current
                result["session"] = current
            if not interactive_session(current) or current != first:
                result["status"] = "session_refused" if not result["samples"] else "session_lost"
                break
            if native is None:
                native = NativeMetadata()
            sample = snapshot(native)
            result["samples"].append(sample)
            after = identity()
            result["last_session"] = after
            if after != first or not interactive_session(after):
                result["status"] = "session_lost"
                break
            if sample["status"] != "ok":
                result["status"] = "read_error"
                break
            if index < 2:
                sleep(2)  # The clipboard is closed before waiting.
        else:
            result["status"] = "ok"
    except (OSError, KeyError, TypeError) as error:
        result["status"] = "read_error"
        result["error_type"] = type(error).__name__
    result["finished_utc"] = utc_now()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = diagnose()
    encoded = json.dumps(result, indent=2) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(encoded, encoding="utf-8")
    print(encoded)  # Metadata survives artifact upload failures; never contains payloads.
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
