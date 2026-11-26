"""Metadata-only clipboard probing: no payload APIs, input, or restoration."""
import ast
import ctypes
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HARNESS = Path(__file__).resolve().parents[1] / "windows" / "desktop"
sys.path.insert(0, str(HARNESS))
import clipboard_diagnostic as probe

SESSION = {"session_id": 1, "window_station": "WinSta0", "thread_desktop": "Default",
           "thread_desktop_receives_input": True, "input_desktop": "Default"}


class FakeMetadata:
    def __init__(self, formats=(13, 16, 1, 7), enum_error=0):
        self.formats = formats
        self.enum_error = enum_error
        self.opened = False
        self.close_count = 0

    def sequence(self):
        return 77

    def open(self):
        self.opened = True
        self.index = 0
        return True, 0

    def close(self):
        self.opened = False
        self.close_count += 1
        return True, 0

    def next_format(self, previous):
        if self.index == len(self.formats):
            return 0, self.enum_error
        value = self.formats[self.index]
        self.index += 1
        return value, 0

    def name(self, value):
        return "CanIncludeInClipboardHistory", 0, False

    def unicode_available(self):
        return 13 in self.formats


class ClipboardMetadataTests(unittest.TestCase):
    def test_three_snapshots_close_before_sleep_and_keep_unknown_metadata(self):
        native = FakeMetadata((13, 0xC123))
        sleeps = []
        def sleep(seconds):
            self.assertFalse(native.opened)
            sleeps.append(seconds)
        result = probe.diagnose(native, identity=lambda: dict(SESSION), sleep=sleep)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["samples"]), 3)
        self.assertEqual(sleeps, [2, 2])
        self.assertEqual(native.close_count, 3)
        self.assertFalse(result["payload_accessed"])
        self.assertFalse(result["clipboard_modified"])
        row = result["samples"][0]
        self.assertEqual(row["unsupported_format_ids"], [0xC123])
        self.assertEqual(row["formats"][1]["name"], "CanIncludeInClipboardHistory")

    def test_empty_and_standard_text_are_distinct_complete_metadata(self):
        for formats in ((), (13, 16, 1, 7)):
            result = probe.snapshot(FakeMetadata(formats))
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["enumeration_complete"])
            self.assertEqual(result["unsupported_format_ids"], [])
            self.assertEqual(result["unicode_available"], bool(formats))

    def test_session_zero_wrong_station_and_secure_desktop_never_open_clipboard(self):
        for change in ({"session_id": 0}, {"window_station": "Service"},
                       {"thread_desktop": "Other"}, {"input_desktop": "Winlogon"},
                       {"thread_desktop_receives_input": False}):
            native = mock.Mock(spec=FakeMetadata)
            result = probe.diagnose(native, identity=lambda: SESSION | change)
            self.assertEqual(result["status"], "session_refused")
            native.open.assert_not_called()

    def test_session_loss_after_snapshot_stops_before_another_acquisition(self):
        native = FakeMetadata()
        identity = mock.Mock(side_effect=[SESSION, SESSION | {"session_id": 2}])
        sleep = mock.Mock()
        result = probe.diagnose(native, identity=identity, sleep=sleep)
        self.assertEqual(result["status"], "session_lost")
        self.assertEqual(native.close_count, 1)
        sleep.assert_not_called()

    def test_enumeration_error_is_not_a_complete_list(self):
        native = FakeMetadata((13,), enum_error=5)
        result = probe.diagnose(native, identity=lambda: SESSION, sleep=mock.Mock())
        self.assertEqual(result["status"], "read_error")
        self.assertEqual(len(result["samples"]), 1)
        row = result["samples"][0]
        self.assertFalse(row["enumeration_complete"])
        self.assertEqual(row["errors"], [{"operation": "EnumClipboardFormats", "code": 5}])
        self.assertFalse(native.opened)

    def test_busy_clipboard_has_no_close_or_data_operations(self):
        native = FakeMetadata()
        native.open = lambda: (False, 5)
        result = probe.snapshot(native)
        self.assertEqual(result["status"], "read_error")
        self.assertEqual(native.close_count, 0)
        self.assertEqual(result["formats"], [])

    def test_duplicate_limit_and_unknown_name_fail_boundedly(self):
        for formats in ((13, 13), tuple(range(1, 258))):
            native = FakeMetadata(formats)
            result = probe.snapshot(native)
            self.assertEqual(result["status"], "read_error")
            self.assertLessEqual(len(result["formats"]), probe.MAX_FORMATS)
            self.assertFalse(native.opened)
        native = FakeMetadata((0xC123,))
        native.name = lambda value: (None, 87, False)
        result = probe.snapshot(native)
        self.assertEqual(result["errors"][0]["operation"], "GetClipboardFormatNameW")

    def test_sequence_change_is_reported_not_hidden(self):
        native = FakeMetadata()
        native.sequence = mock.Mock(side_effect=[77, 78, 78])
        result = probe.snapshot(native)
        self.assertTrue(result["sequence_changed"])
        self.assertEqual(result["sequence_open"], 78)

    def test_exception_still_closes_and_does_not_expose_exception_text(self):
        native = FakeMetadata()
        native.next_format = mock.Mock(side_effect=OSError("sensitive exception text"))
        result = probe.diagnose(native, identity=lambda: SESSION)
        self.assertEqual(result["status"], "read_error")
        self.assertFalse(native.opened)
        self.assertNotIn("sensitive", json.dumps(result))

    def test_native_api_surface_cannot_read_payloads_or_write_input(self):
        source = ast.parse(Path(probe.__file__).read_text())
        calls = {node.func.attr for node in ast.walk(source) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Attribute)
                 and node.func.value.attr == "api"}
        self.assertEqual(calls, {"GetClipboardSequenceNumber", "OpenClipboard", "CloseClipboard",
                                 "EnumClipboardFormats", "GetClipboardFormatNameW", "IsClipboardFormatAvailable"})
        allowed = list(calls)
        api = mock.Mock(spec=allowed)
        api.OpenClipboard.return_value = True
        api.CloseClipboard.return_value = True
        api.GetClipboardSequenceNumber.return_value = 3
        api.IsClipboardFormatAvailable.return_value = True
        api.EnumClipboardFormats.side_effect = [13, 0]
        with mock.patch.object(probe.win32, "require_windows"), mock.patch.object(probe.win32, "user32", api), \
             mock.patch.object(ctypes, "set_last_error", create=True), \
             mock.patch.object(ctypes, "get_last_error", return_value=0, create=True):
            result = probe.snapshot(probe.NativeMetadata())
        self.assertEqual(result["status"], "ok")
        api.CloseClipboard.assert_called_once()
        self.assertEqual(api.GetClipboardSequenceNumber.argtypes, ())

    def test_cli_persists_metadata_even_on_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "clipboard.json"
            with mock.patch.object(probe, "diagnose", return_value={"status": "session_refused"}), \
                 mock.patch("builtins.print"):
                self.assertEqual(probe.main(["--out", str(out)]), 1)
            self.assertEqual(json.loads(out.read_text()), {"status": "session_refused"})


if __name__ == "__main__":
    unittest.main()
