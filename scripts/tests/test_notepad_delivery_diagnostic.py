"""Offline H9 matrix, safety, and selection regressions."""
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


HARNESS = Path(__file__).resolve().parents[1] / "windows" / "desktop"
sys.path.insert(0, str(HARNESS))

import catalog  # noqa: E402
import guard  # noqa: E402
import guard_diagnostic  # noqa: E402
import run  # noqa: E402
import win32  # noqa: E402
from checks import lookup  # noqa: E402
from checks import notepad_delivery as h9  # noqa: E402
from context import Fail  # noqa: E402


class FakeContext:
    def __init__(self, out):
        self.out = out
        self.fixtures = {"A": SimpleNamespace(path="fixture-a.wav")}
        self.murmur = mock.Mock()
        self.murmur.log.return_value = "synthetic app log"
        self.guard = mock.Mock()
        self._delivery_cursor = 10
        self.startup_probe_lease = SimpleNamespace(closed=True)
        self.created = []

    def artifact(self, suffix):
        return self.out / suffix

    def clipboard_guard(self):
        return "original"

    def notepad(self, name, **kwargs):
        pad = mock.Mock()
        pad.hwnd = 100
        pad.active_document.return_value = True
        pad.owns_keyboard_focus.return_value = True
        pad.text.return_value = ""
        self.created.append((name, kwargs, pad))
        return pad


class H9Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = FakeContext(Path(self.temp.name))

    def test_fixed_matrix_config_isolation_and_exact_count(self):
        expected_names = ["control", "clipboard-alt", "clipboard-no-alt", "type-alt",
                          "type-no-alt", "paste-alt", "paste-no-alt"]
        self.assertEqual([name for name, _, _ in h9.TRIALS], expected_names)
        self.assertEqual(lookup(catalog.BY_ID["H9"]).__name__, "h9_notepad_delivery")
        self.assertNotIn(catalog.BY_ID["H9"], catalog.select())
        self.assertEqual(catalog.select("H9"), [catalog.BY_ID["H9"]])
        clipboard = ["original"]
        with mock.patch.object(win32, "clipboard_text", side_effect=lambda: clipboard[0]), \
             mock.patch.object(win32, "clipboard_formats", return_value=[win32.CF_UNICODETEXT]), \
             mock.patch.object(win32, "set_clipboard_text", side_effect=lambda value: clipboard.__setitem__(0, value)), \
             mock.patch.object(h9, "_notepad_direct_input", return_value=(None, None, h9.DIRECT_A,
                                                                             {"key_trace": {}, "probes_during_input": 2})), \
             mock.patch.object(h9, "_app_trial", return_value=h9.DIRECT_A) as app_trial, \
             mock.patch.object(h9, "_settled_capture") as capture:
            h9.h9_notepad_delivery(self.ctx)
        self.assertEqual([name for name, _, _ in self.ctx.created], expected_names)
        self.assertEqual(app_trial.call_count, 6)
        self.assertEqual(capture.call_count, 7)
        self.assertEqual(self.ctx.murmur.ensure.call_count, 6)
        configs = [call.args[1] for call in self.ctx.murmur.ensure.call_args_list]
        self.assertEqual([config["injection"]["method"] for config in configs],
                         ["type", "type", "type", "type", "clipboard", "clipboard"])
        self.assertEqual([config["injection"]["clipboard_only"] for config in configs],
                         [True, True, False, False, False, False])
        self.assertEqual([config["general"]["hotkey"] for config in configs],
                         [h9.ALT_HOTKEY, h9.SHIFT_HOTKEY] * 3)
        self.assertTrue(all(config["injection"]["prefer_paste"] is False for config in configs))
        self.assertEqual(len({id(config) for config in configs}), 6)
        self.assertEqual(len(json.loads((self.ctx.out / "notepad-delivery.json").read_text())), 7)
        for _, _, pad in self.ctx.created:
            pad.clear_owned.assert_not_called()

    def test_clipboard_only_requires_exact_history_clipboard_and_no_delivery_keys(self):
        expected = h9.DIRECT_A
        row = {"phases": [], "sentinel": "sentinel"}
        self.ctx.murmur.history_cursor.return_value = 10
        self.ctx.murmur.history_after.return_value = [(11, expected)]
        self.ctx.guard.stop_key_trace.return_value = {"events": [
            [1, 0, win32.VK["ctrl"], 0, 0], [2, 0, win32.VK["alt"], 0, 0],
            [3, 0, win32.VK["shift"], 0, 0], [4, 0, win32.VK["f9"], 0, 0],
            [5, 0, win32.VK["f9"], 0, guard.LLKHF_UP],
            [6, 0, win32.VK["shift"], 0, guard.LLKHF_UP],
            [7, 0, win32.VK["alt"], 0, guard.LLKHF_UP],
            [8, 0, win32.VK["ctrl"], 0, guard.LLKHF_UP],
            [9, 0, guard.PROBE_VK, 0, 0]], "overflow": False}
        self.ctx.observed_text = mock.Mock(return_value=expected)
        self.ctx.settle = mock.Mock(return_value="done")
        self.ctx.sleep = mock.Mock()
        self.ctx.artifact = lambda suffix: self.ctx.out / suffix
        with mock.patch.object(h9, "_drive_registered_hotkey"), \
             mock.patch.object(h9, "_focus_phase", return_value={"stage": "settled"}) as focus_phase, \
             mock.patch.object(win32, "clipboard_text", return_value=expected), \
             mock.patch.object(win32, "release_all"):
            actual = h9._app_trial(self.ctx, mock.Mock(), "clipboard-alt", "clipboard_only",
                                   h9.ALT_HOTKEY, row)
        self.assertEqual(actual, expected)
        self.assertEqual(row["history_count"], 1)
        self.assertIsNone(row["clipboard_difference"])
        self.assertIsNone(row["keyboard_error"])
        self.assertEqual(row["key_trace"]["packet_text"], "")
        self.ctx.observed_text.assert_called_once_with(timeout=45)
        self.ctx.settle.assert_called_once_with(timeout=45)
        self.assertEqual([call.args[1] for call in focus_phase.call_args_list],
                         ["before modifiers", "after delivery wait"])
        self.assertEqual(h9._clipboard_trace_problem({"events": [[1, 0, guard.VK_PACKET, 65, 0]],
                                                      "overflow": False}, h9.ALT_HOTKEY),
                         "clipboard-only emitted delivery keyboard events: [231]")
        self.assertIsNotNone(h9._clipboard_trace_problem({"events": [], "overflow": True}, h9.ALT_HOTKEY))
        # An empty owned document is a separate oracle from clipboard and history.
        pad = mock.Mock()
        pad.active_document.return_value = True
        pad.text.return_value = "wrong"
        pad.owns_keyboard_focus.return_value = False
        self.ctx.screenshot = mock.Mock()
        with mock.patch.object(win32, "tap") as tap:
            h9._settled_capture(self.ctx, pad, "clipboard-alt", "", row)
        self.assertIsNotNone(row["uia_difference"])
        self.assertIn("save refused", row["saved_file_error"])
        tap.assert_not_called()

    def test_paste_trace_requires_one_v_pair_and_no_packet(self):
        trace = {"events": [[0, 0, 0xA2, 29, 0], [1, 0, win32.VK["v"], 0, 0],
                            [2, 0, win32.VK["v"], 0, guard.LLKHF_UP],
                            [3, 0, 0xA2, 29, guard.LLKHF_UP]], "overflow": False}
        self.assertIsNone(h9._paste_trace_problem(trace))
        self.assertIsNotNone(h9._paste_trace_problem({"events": [], "overflow": False}))
        self.assertIsNotNone(h9._paste_trace_problem({"events": trace["events"][1:3], "overflow": False}))
        shifted = {"events": [[0, 0, 0xA0, 42, 0]] + trace["events"], "overflow": False}
        self.assertIsNotNone(h9._paste_trace_problem(shifted))
        trace["events"].append([3, 0, guard.VK_PACKET, 65, 0])
        self.assertIn("VK_PACKET", h9._paste_trace_problem(trace))

    def test_clipboard_trace_accepts_native_sided_modifiers_but_not_extra_pairs(self):
        # 45245783015 attempt 3 reports left Ctrl/Alt/Shift rather than generic VKs.
        keys = (0xA2, 0xA4, 0xA0, win32.VK["f9"])
        events = [[0, 256, key, 0, 16] for key in keys]
        events += [[0, 257, key, 0, 144] for key in reversed(keys)]
        self.assertIsNone(h9._clipboard_trace_problem({"events": events, "overflow": False}, h9.ALT_HOTKEY))
        for extra in ([0, 256, guard.VK_PACKET, 65, 16], [0, 256, 0xA3, 0, 16]):
            self.assertIsNotNone(h9._clipboard_trace_problem(
                {"events": events + [extra], "overflow": False}, h9.ALT_HOTKEY))

    def test_non_text_clipboard_is_refused_before_any_input(self):
        with mock.patch.object(win32, "clipboard_text", return_value="text"), \
             mock.patch.object(win32, "clipboard_formats", return_value=[win32.CF_UNICODETEXT, 49333]), \
             mock.patch.object(win32, "set_clipboard_text") as set_text:
            with self.assertRaisesRegex(Fail, "cannot be restored"):
                h9.h9_notepad_delivery(self.ctx)
        set_text.assert_not_called()
        self.assertEqual(self.ctx.created, [])

    def test_preflight_refuses_unrestorable_clipboard_before_run_wide_restore(self):
        (self.ctx.out / "build.json").write_text("{}", encoding="utf-8")
        native = mock.Mock()
        native.input_desktop.return_value = "Default"
        native.pids.return_value = []
        native.idle_seconds.return_value = 120
        with mock.patch.object(win32, "clipboard_text", return_value="text"), \
             mock.patch.object(win32, "clipboard_formats", return_value=[win32.CF_UNICODETEXT, 49333]):
            problems = run.preflight(self.ctx.out, [catalog.BY_ID["H9"]], native)
        self.assertEqual(len(problems), 1)
        self.assertIn("cannot be restored", problems[0])

    def test_windows_ci_selects_new_offline_module(self):
        workflow = (HARNESS.parents[2] / ".github" / "workflows" / "windows-build.yml").read_text()
        self.assertIn("test_desktop_harness test_notepad_delivery_diagnostic test_vendored_wry", workflow)

    def test_earlier_mismatch_remains_failure_after_later_trials(self):
        with mock.patch.object(win32, "clipboard_text", return_value="original"), \
             mock.patch.object(win32, "clipboard_formats", return_value=[win32.CF_UNICODETEXT]), \
             mock.patch.object(win32, "set_clipboard_text"), \
             mock.patch.object(h9, "_notepad_direct_input", return_value=(None, "first mismatch", h9.DIRECT_A,
                                                                             {"key_trace": {}, "probes_during_input": 1})), \
             mock.patch.object(h9, "_app_trial", return_value=h9.DIRECT_A), \
             mock.patch.object(h9, "_settled_capture"):
            with self.assertRaisesRegex(Fail, "control: first mismatch"):
                h9.h9_notepad_delivery(self.ctx)
        self.assertEqual(len(self.ctx.created), 7)
        report = json.loads((self.ctx.out / "notepad-delivery.json").read_text())
        self.assertEqual(report["control"]["direct_difference"], "first mismatch")

    def test_guard_loss_stops_trials_and_does_not_clear_or_refocus(self):
        with mock.patch.object(win32, "clipboard_text", return_value="original"), \
             mock.patch.object(win32, "clipboard_formats", return_value=[win32.CF_UNICODETEXT]), \
             mock.patch.object(win32, "set_clipboard_text"), \
             mock.patch.object(h9, "_notepad_direct_input", return_value=(None, None, h9.DIRECT_A,
                                                                             {"key_trace": {}, "probes_during_input": 1})), \
             mock.patch.object(h9, "_app_trial", side_effect=guard.GuardUnavailable("guard lost")), \
             mock.patch.object(h9, "_settled_capture"), \
             mock.patch.object(win32, "tap") as tap:
            with self.assertRaises(guard.GuardUnavailable):
                h9.h9_notepad_delivery(self.ctx)
        self.assertEqual([name for name, _, _ in self.ctx.created], ["control", "clipboard-alt"])
        tap.assert_not_called()
        self.assertEqual(len(json.loads((self.ctx.out / "notepad-delivery.json").read_text())), 2)

    def test_changed_tab_refuses_read_and_save(self):
        pad = mock.Mock()
        pad.active_document.return_value = False
        row = {}
        self.ctx.screenshot = mock.Mock()
        with mock.patch.object(win32, "tap") as tap:
            h9._settled_capture(self.ctx, pad, "alt", "", row)
        pad.text.assert_not_called()
        tap.assert_not_called()
        self.assertIn("active tab", row["uia_error"])
        self.assertIn("active tab", row["saved_file_error"])

    def test_save_refusal_stays_failed_after_later_trials(self):
        def capture(_ctx, _pad, name, _expected, row):
            if name == "clipboard-alt":
                row["saved_file_error"] = "save refused: focus lost"
        with mock.patch.object(win32, "clipboard_text", return_value="original"), \
             mock.patch.object(win32, "clipboard_formats", return_value=[win32.CF_UNICODETEXT]), \
             mock.patch.object(win32, "set_clipboard_text"), \
             mock.patch.object(h9, "_notepad_direct_input", return_value=(None, None, h9.DIRECT_A,
                                                                             {"key_trace": {}, "probes_during_input": 1})), \
             mock.patch.object(h9, "_app_trial", return_value=h9.DIRECT_A), \
             mock.patch.object(h9, "_settled_capture", side_effect=capture):
            with self.assertRaisesRegex(Fail, "clipboard-alt: save refused"):
                h9.h9_notepad_delivery(self.ctx)
        self.assertEqual(len(self.ctx.created), 7)
        self.assertEqual(json.loads((self.ctx.out / "notepad-delivery.json").read_text())
                         ["clipboard-alt"]["saved_file_error"], "save refused: focus lost")

    def test_alone_repeat_limit_and_startup_lease_selection(self):
        self.assertIn("H9", run.NOTEPAD_CHECKS)
        self.assertIn("H9", run.LEASED_ALONE)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            run.main(["--build", "build", "--out", "out", "--only", "H9", "--repeat", "2"])
        self.assertEqual(error.exception.code, 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            run.main(["--build", "build", "--out", "out", "--only", "H9,H8"])
        self.assertEqual(error.exception.code, 2)
        window = mock.Mock()
        window.open.return_value = True
        window.focused.return_value = True
        window.enter_passive.return_value = True
        window.hwnd = 42
        window.diagnostics.return_value = {"status": "passive"}
        native = mock.Mock()
        native.desktop_context.return_value = {"input_desktop": "Default"}
        native.idle_seconds.return_value = 60
        watcher = mock.Mock()
        watcher.diagnostics.return_value = {"probes_seen": 1, "hook_state": {}}
        meta = {"guard_startup": {}}
        with mock.patch.object(guard_diagnostic, "_OwnedProbeWindow", return_value=window), \
             mock.patch.object(guard_diagnostic, "_wait_for_safe_desktop", return_value=("ready", [])):
            lease = run.start_input_guard(watcher, [catalog.BY_ID["H9"]], native, meta)
        self.assertIsInstance(lease, run.StartupProbeLease)
        watcher.start.assert_called_once_with(owned_probe_target=(42, mock.ANY))
        self.assertEqual(meta["guard_startup"]["phase"], "ready")
        lease.close()


if __name__ == "__main__":
    unittest.main()
