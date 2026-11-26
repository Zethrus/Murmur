"""Offline H10 matrix, guard, oracle, and selection regressions."""
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
import run  # noqa: E402
import tomlconfig  # noqa: E402
import win32  # noqa: E402
from checks import lookup  # noqa: E402
from checks import alt_release as h10  # noqa: E402
from context import Context, Fail
import targets  # noqa: E402


class FakeContext:
    def __init__(self, out):
        self.out = out
        self.fixtures = {"A": SimpleNamespace(path="fixture-a.wav", speech_seconds=0.5)}
        self.murmur = mock.Mock()
        self.murmur.log.return_value = "synthetic app log"
        self.murmur.history_cursor.return_value = 10
        self.guard = mock.Mock()
        self.guard.diagnostics.return_value = {"probes_seen": 3}
        self._delivery_wait_elapsed = 0.1
        self.startup_probe_lease = SimpleNamespace(closed=True)
        self.created = []
        self.sleep = mock.Mock()
        self.settle = mock.Mock(return_value="idle")
        self.observed_text = mock.Mock(return_value="The quick brown fox jumps over the lazy dog.")
        self.mark_delivery_released = mock.Mock()

    def artifact(self, suffix):
        return self.out / suffix

    def clipboard_guard(self):
        return "original"

    def observe_delivery(self, fixture):
        self._delivery_cursor = self.murmur.history_cursor()

    def notepad(self, name, **kwargs):
        pad = mock.Mock()
        pad.hwnd, pad.owner_pid = 100, 20
        pad.active_document.return_value = True
        pad.owns_keyboard_focus.return_value = True
        pad.text.return_value = ""
        self.created.append((name, kwargs, pad))
        return pad


class H10Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = FakeContext(Path(self.temp.name))

    def test_fixed_matrix_release_permutations_and_registration_labels(self):
        self.assertEqual(len(h10.TRIALS), 10)
        self.assertEqual(len({(context, name) for context, name, *_ in h10.TRIALS}), 10)
        for context, name, chord, release, binding in h10.TRIALS:
            if name == "non-alt-control":
                self.assertEqual(chord, "Ctrl+Shift+F9")
                self.assertEqual(release, ("f9", "shift", "ctrl"))
            else:
                self.assertEqual(chord, h10.ALT_HOTKEY)
                self.assertIn((name, release), h10.RELEASES)
                self.assertEqual(release.index("alt") < release.index("ctrl"),
                                 "alt-before-ctrl" in name)
                self.assertEqual(release[0] == "f9", "f9-first" in name)
                self.assertEqual(release.index("shift"), 1 if release[0] == "f9" else 0)
            self.assertEqual(h10._same_chord(binding, chord), context == "configured-in-Murmur")
        self.assertTrue(all(label == "not-configured-in-Murmur" for label, *_ in h10.TRIALS[5:]))

    def test_catalog_lookup_and_alone_repeat_gate(self):
        check = catalog.BY_ID["H10"]
        self.assertNotIn(check, catalog.select())
        self.assertEqual(catalog.select("H10"), [check])
        self.assertEqual(lookup(check).__name__, "h10_alt_release")
        self.assertIn("H10", run.NOTEPAD_CHECKS)
        self.assertIn("H10", run.LEASED_ALONE)
        for selection, repeat in (("H10,H9", "1"), ("H10", "2")):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                run.main(["--build", "build", "--out", "out", "--only", selection,
                          "--repeat", repeat])
            self.assertEqual(error.exception.code, 2)

    def test_config_distinction_requires_disk_session_and_live_overlay(self):
        config = {"general": {"hotkey": h10.UNBOUND_HOTKEY, "hotkey_code": ""},
                  "injection": {"method": "type", "prefer_paste": False, "clipboard_only": True}}
        path = self.ctx.out / "config.toml"
        path.write_text(tomlconfig.dumps(config), encoding="utf-8")
        self.ctx.murmur.config = config
        self.ctx.murmur.config_path = path
        self.ctx.murmur.process.pid = 42
        self.ctx.murmur.running.return_value = True
        self.ctx.murmur.overlay_hwnd.return_value = 77
        with mock.patch.object(win32, "is_window", return_value=True), \
             mock.patch.object(win32, "window_pid", return_value=42):
            row = {}
            h10._verify_binding(self.ctx, h10.UNBOUND_HOTKEY, h10.ALT_HOTKEY, row)
            self.assertFalse(row["binding_evidence"]["chord_in_murmur_config"])
            config["general"]["hotkey"] = h10.ALT_HOTKEY
            with self.assertRaisesRegex(Fail, "binding/readiness"):
                h10._verify_binding(self.ctx, h10.UNBOUND_HOTKEY, h10.ALT_HOTKEY, {})

    def test_native_stamps_stop_foreign_foreground_before_next_key(self):
        pad = SimpleNamespace(hwnd=100, owner_pid=20)
        row = {"phases": []}
        native = {"input_desktop": "Default", "gui_status": "ok", "hwnd": 200,
                  "process_id": 21, "active_hwnd": 200, "focus_hwnd": 201}
        with mock.patch.object(win32, "foreground_context", return_value=native), \
             mock.patch.object(win32, "key_down") as down:
            with self.assertRaisesRegex(Fail, "ownership"):
                h10._drive(self.ctx, pad, h10.ALT_HOTKEY, h10.RELEASES[0][1], row)
        down.assert_not_called()
        self.assertEqual(row["phases"][0]["focus_snapshot"], "native_only")

    def test_guard_trip_sends_no_further_press_and_releases_owned_down_key(self):
        pad = SimpleNamespace(hwnd=100, owner_pid=20)
        row = {"phases": []}
        checks = iter((None, None, guard.GuardUnavailable("lost")))
        def check():
            result = next(checks)
            if result:
                raise result
        self.ctx.guard.check.side_effect = check
        with mock.patch.object(h10, "_stamp"), \
             mock.patch.object(win32, "key_down") as down, \
             mock.patch.object(win32, "key_up") as up:
            with self.assertRaises(guard.GuardUnavailable):
                h10._drive(self.ctx, pad, h10.ALT_HOTKEY, h10.RELEASES[0][1], row)
        self.assertEqual([call.args for call in down.call_args_list], [("ctrl",), ("alt",)])
        self.assertEqual([call.args for call in up.call_args_list], [("alt",), ("ctrl",)])

    def test_guard_trip_cleanup_bypasses_check_only_for_owned_key_up(self):
        held = ["ctrl"]
        fake_user32 = mock.Mock()
        fake_user32.SendInput.return_value = 1
        with mock.patch.object(win32, "key_up", side_effect=guard.GuardUnavailable("lost")), \
             mock.patch.object(win32, "user32", fake_user32), \
             mock.patch.object(win32, "_key_input", return_value=win32.INPUT()):
            h10._release_owned(held)
        self.assertEqual(held, [])
        fake_user32.SendInput.assert_called_once()

    def test_failed_owned_release_blocks_final_tab_cleanup(self):
        self.ctx.guard = guard.Guard()
        pad = SimpleNamespace(hwnd=100, owner_pid=20, path=self.ctx.out / "owned.txt",
                              close_owned=mock.Mock())
        self.ctx._completed_notepads = [pad]
        row = {"phases": []}
        with mock.patch.object(h10, "_stamp", side_effect=[None, Fail("observation lost")]), \
             mock.patch.object(win32, "key_down"), \
             mock.patch.object(win32, "key_up", side_effect=OSError("release failed")):
            with self.assertRaisesRegex(Fail, "observation lost"):
                h10._drive(self.ctx, pad, h10.ALT_HOTKEY, h10.RELEASES[0][1], row)
        self.assertIn("release failed", row["owned_key_cleanup_error"])
        failures = Context.close_completed_notepads(self.ctx)
        self.assertTrue(failures)
        pad.close_owned.assert_not_called()
        with self.assertRaises(guard.GuardUnavailable):
            self.ctx.guard.check()

    def test_final_cleanup_requires_editor_before_save_and_again_before_close(self):
        for focus, expected_actions in (([False], []), ([True, False], [("ctrl", "s")]),
                                        ([True, True], [("ctrl", "s"), ("ctrl", "w")])):
            with self.subTest(focus=focus):
                pad = targets.Notepad(self.ctx.out, "H10-cleanup")
                pad.hwnd, pad.owner_pid, pad.owned_tab = 42, 7, True
                pad.require_editor_cleanup = True
                pad.path.write_text("", encoding="utf-8")
                pad.owns_keyboard_focus = mock.Mock(side_effect=focus)
                actions = []
                state = {"open": True}
                def tap(*keys):
                    actions.append(keys)
                    if keys == ("ctrl", "w"):
                        state["open"] = False
                pad.win32 = SimpleNamespace(is_window=lambda _: state["open"],
                    window_pid=lambda _: 7, pids=lambda _: [7], activate=lambda _: True,
                    foreground=lambda: 42, window_text=lambda _: pad.path.name + " - Notepad",
                    tap=tap, wait_for=lambda predicate, _: predicate())
                if False in focus:
                    with self.assertRaisesRegex(RuntimeError, "Cleanup refused"):
                        pad.close_owned()
                else:
                    pad.close_owned()
                self.assertEqual(actions, expected_actions)

    def test_paired_trace_requires_exact_order_and_no_delivery_keys(self):
        chord, release = h10.ALT_HOTKEY, h10.RELEASES[0][1]
        events = [[i, 256, win32.VK[key], 0,
                   guard.LLKHF_INJECTED | (guard.LLKHF_UP if up else 0)]
                  for i, (key, up) in enumerate([(key, False) for key in h10.PRESS]
                                                + [(key, True) for key in release])]
        trace = {"events": events + [[9, 257, guard.PROBE_VK, 0, guard.LLKHF_UP]],
                 "overflow": False}
        self.assertIsNone(h10._trace_problem(trace, chord, release))
        self.assertIsNotNone(h10._trace_problem({"events": events[:-1], "overflow": False}, chord, release))
        self.assertIsNotNone(h10._trace_problem({"events": events + [[9, 256, guard.VK_PACKET, 65, 16]],
                                                "overflow": False}, chord, release))
        self.assertIsNotNone(h10._trace_problem({"events": events, "overflow": True}, chord, release))

    def test_unbound_trial_has_fixed_dwell_and_no_listening_or_output_wait(self):
        pad = mock.Mock()
        row = {"phases": [], "sentinel": "sentinel"}
        self.ctx.murmur.overlay_state.return_value = "idle"
        self.ctx.murmur.history_after.return_value = []
        self.ctx.guard.stop_key_trace.return_value = {"events": [], "overflow": False}
        with mock.patch.object(h10, "_drive"), mock.patch.object(h10, "_stamp"), \
             mock.patch.object(win32, "clipboard_text", return_value="sentinel"):
            h10._trial(self.ctx, pad, "unbound", h10.ALT_HOTKEY, h10.RELEASES[0][1], False, row)
        self.ctx.observed_text.assert_not_called()
        self.ctx.settle.assert_not_called()
        self.ctx.murmur.wait_state.assert_not_called()
        self.ctx.sleep.assert_called_once_with(1.2)
        self.assertEqual(row["history_count"], 0)
        self.assertIsNone(row["clipboard_difference"])

    def test_registered_trial_uses_unique_history_as_exact_clipboard_oracle(self):
        pad = mock.Mock()
        expected = "The quick brown fox jumps over the lazy dog."
        row = {"phases": [], "sentinel": "sentinel"}
        self.ctx.murmur.history_after.return_value = [(11, expected)]
        self.ctx.guard.stop_key_trace.return_value = {"events": [], "overflow": False}
        with mock.patch.object(h10, "_drive"), mock.patch.object(h10, "_stamp"), \
             mock.patch.object(win32, "clipboard_text", return_value="wrong"):
            h10._trial(self.ctx, pad, "registered", h10.ALT_HOTKEY, h10.RELEASES[0][1], True, row)
        self.ctx.observed_text.assert_called_once_with(timeout=45)
        self.ctx.settle.assert_called_once_with(timeout=45)
        self.assertEqual(row["history_count"], 1)
        self.assertIsNotNone(row["clipboard_difference"])
        self.assertEqual((self.ctx.out / "registered-history.txt").read_text(), expected)

    def test_nontext_clipboard_refused_before_input(self):
        with mock.patch.object(win32, "clipboard_text", return_value="text"), \
             mock.patch.object(win32, "clipboard_formats", return_value=[win32.CF_UNICODETEXT, 49333]), \
             mock.patch.object(win32, "set_clipboard_text") as set_text:
            with self.assertRaisesRegex(Fail, "cannot be restored"):
                h10.h10_alt_release(self.ctx)
        set_text.assert_not_called()
        self.assertEqual(self.ctx.created, [])

    def test_preflight_refuses_nontext_clipboard_for_h10(self):
        (self.ctx.out / "build.json").write_text("{}", encoding="utf-8")
        native = mock.Mock()
        native.input_desktop.return_value = "Default"
        native.pids.return_value = []
        native.idle_seconds.return_value = 120
        with mock.patch.object(win32, "clipboard_text", return_value="text"), \
             mock.patch.object(win32, "clipboard_formats", return_value=[win32.CF_UNICODETEXT, 49333]):
            problems = run.preflight(self.ctx.out, [catalog.BY_ID["H10"]], native)
        self.assertEqual(len(problems), 1)
        self.assertIn("H10 refused", problems[0])

    def test_prior_failure_latches_and_all_tabs_are_retained(self):
        clipboard = ["original"]
        def trial(_ctx, _pad, name, _chord, _release, _registered, row):
            if name.endswith("non-alt-control") and name.startswith("configured"):
                row["clipboard_difference"] = "first mismatch"
        with mock.patch.object(h10, "restorable_clipboard_text", return_value="original"), \
             mock.patch.object(win32, "set_clipboard_text", side_effect=lambda value: clipboard.__setitem__(0, value)), \
             mock.patch.object(win32, "clipboard_text", side_effect=lambda: clipboard[0]), \
             mock.patch.object(h10, "_verify_binding"), \
             mock.patch.object(h10, "_trial", side_effect=trial), \
             mock.patch.object(h10, "_stamp"), \
             mock.patch.object(h10.h9, "_settled_capture"):
            with self.assertRaisesRegex(Fail, "first mismatch"):
                h10.h10_alt_release(self.ctx)
        self.assertEqual(len(self.ctx.created), 10)
        self.assertTrue(all(kwargs["owned_tab"] for _, kwargs, _ in self.ctx.created))
        self.assertTrue(all(kwargs["reuse_hwnd"] == 100 for _, kwargs, _ in self.ctx.created[1:]))
        self.assertEqual(len(json.loads((self.ctx.out / "alt-release.json").read_text())), 10)
        for _, _, pad in self.ctx.created:
            pad.clear_owned.assert_not_called()

    def test_lost_editor_never_saves_and_next_owned_tab_runs(self):
        clipboard = ["original"]
        original_notepad = self.ctx.notepad
        def notepad(name, **kwargs):
            pad = original_notepad(name, **kwargs)
            pad.owns_keyboard_focus.side_effect = [True, False, False]
            pad.menu_open.return_value = True
            pad.path = self.ctx.out / f"{name}.txt"
            pad.path.write_text("", encoding="utf-8")
            pad.focus_diagnostic.return_value = "menu focus"
            return pad
        self.ctx.notepad = notepad
        self.ctx.screenshot = mock.Mock()
        with mock.patch.object(h10, "restorable_clipboard_text", return_value="original"), \
             mock.patch.object(win32, "set_clipboard_text", side_effect=lambda value: clipboard.__setitem__(0, value)), \
             mock.patch.object(win32, "clipboard_text", side_effect=lambda: clipboard[0]), \
             mock.patch.object(win32, "tap") as tap, \
             mock.patch.object(h10, "_verify_binding"), \
             mock.patch.object(h10, "_stamp"), \
             mock.patch.object(h10, "_trial"):
            with self.assertRaisesRegex(Fail, "save refused"):
                h10.h10_alt_release(self.ctx)
        tap.assert_not_called()
        self.assertEqual(len(self.ctx.created), 10)

    def test_unknown_settled_editor_ownership_aborts_matrix(self):
        clipboard = ["original"]
        def capture(_ctx, _pad, _name, _expected, row):
            row["uia_error"] = "active tab unavailable"
        with mock.patch.object(h10, "restorable_clipboard_text", return_value="original"), \
             mock.patch.object(win32, "set_clipboard_text", side_effect=lambda value: clipboard.__setitem__(0, value)), \
             mock.patch.object(win32, "clipboard_text", side_effect=lambda: clipboard[0]), \
             mock.patch.object(h10, "_verify_binding"), \
             mock.patch.object(h10, "_trial"), \
             mock.patch.object(h10, "_stamp"), \
             mock.patch.object(h10.h9, "_settled_capture", side_effect=capture):
            with self.assertRaisesRegex(Fail, "ownership could not be verified"):
                h10.h10_alt_release(self.ctx)
        self.assertEqual(len(self.ctx.created), 1)
        self.assertEqual(len(json.loads((self.ctx.out / "alt-release.json").read_text())), 1)


if __name__ == "__main__":
    unittest.main()
