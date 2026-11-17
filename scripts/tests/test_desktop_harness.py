"""Pure parts of the Windows desktop harness (scripts/windows/desktop), runnable on any OS."""
import ast
from contextlib import closing, ExitStack
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest import mock
import wave

HARNESS = Path(__file__).resolve().parents[1] / "windows" / "desktop"
sys.path.insert(0, str(HARNESS))

import catalog  # noqa: E402
from checks import setup as setup_checks  # noqa: E402
from checks import focus as focus_checks  # noqa: E402
from checks import insertion as insertion_checks  # noqa: E402
from checks import hotkeys as hotkey_checks  # noqa: E402
from checks import microphone as microphone_checks  # noqa: E402
from checks import common as common_checks  # noqa: E402
from checks import overlay as overlay_checks  # noqa: E402
import fixtures  # noqa: E402
import golden  # noqa: E402
import guard  # noqa: E402
import guard_diagnostic  # noqa: E402
import handoff  # noqa: E402
import idle_diagnostic  # noqa: E402
import manual  # noqa: E402
import murmur  # noqa: E402
import report  # noqa: E402
import restore  # noqa: E402
import run  # noqa: E402
import tomlconfig  # noqa: E402
import targets  # noqa: E402
import uia  # noqa: E402
import win32  # noqa: E402
from context import Context, Fail, OutputCount, Skip  # noqa: E402

SHA = "a" * 40
OTHER = "b" * 40


def fake_observer_worker(connection):
    """Spawn-safe test peer for the parent guard's process handshake."""
    connection.send(("ready", {"probes_seen": 1}))
    if connection.recv() == "stop":
        connection.send(("diagnostics", {"probes_seen": 2}))
    connection.close()


def fake_early_input_worker(connection):
    connection.send(("user_input", "real keyboard input during startup"))
    connection.close()


def fake_diagnostic_worker(connection, diagnostic):
    connection.send(("ready", {"probes_seen": 1, "diagnostic": diagnostic}))
    if connection.recv() == "stop":
        connection.send(("diagnostics", {"probes_seen": 2, "diagnostic": diagnostic}))
    connection.close()


def fake_owned_probe_worker(connection, diagnostic, target):
    connection.send(("ready", {"diagnostic": diagnostic, "target": target}))
    connection.recv()
    connection.close()


def digest(data):
    return hashlib.sha256(data).hexdigest()


class IdleDiagnosticTests(unittest.TestCase):
    SESSION = {"session_id": 1, "window_station": "WinSta0", "thread_desktop": "Default",
               "thread_desktop_receives_input": True, "input_desktop": "Default"}

    def test_samples_raw_ticks_and_marks_only_changes_after_first_sample(self):
        elapsed = [0.0]
        def clock():
            return elapsed[0]
        def sleep(seconds):
            elapsed[0] += seconds

        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(idle_diagnostic, "session_identity", return_value=self.SESSION), \
             mock.patch.object(win32, "last_input_tick", side_effect=[1000, 1000, 3000, 3000]), \
             mock.patch.object(win32, "current_tick", side_effect=[1000, 2000, 3000, 4000]), \
             mock.patch.object(win32, "idle_seconds", side_effect=AssertionError("idle gate used")):
            result = idle_diagnostic.diagnose(seconds=3, interval=1, clock=clock, sleep=sleep,
                                              timestamp=lambda: "2026-11-15T00:00:00+00:00")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reset_count"], 1)
        self.assertEqual([sample["elapsed_s"] for sample in result["samples"]], [0, 1, 2, 3])
        self.assertEqual([sample["idle_s"] for sample in result["samples"]], [0, 1, 0, 1])
        self.assertEqual([sample["last_input_changed"] for sample in result["samples"]],
                         [False, False, True, False])
        self.assertEqual(result["session"], self.SESSION)

    def test_tick_wrap_and_nonidle_session_are_observed_without_preflight(self):
        elapsed = [0.0]
        def sleep(seconds):
            elapsed[0] += seconds
        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(idle_diagnostic, "session_identity", return_value=self.SESSION), \
             mock.patch.object(win32, "last_input_tick", return_value=0xFFFFFFF0), \
             mock.patch.object(win32, "current_tick", return_value=25):
            result = idle_diagnostic.diagnose(seconds=1, interval=1, clock=lambda: elapsed[0],
                                              sleep=sleep, timestamp=lambda: "now")
        self.assertEqual(result["samples"][0]["idle_s"], 0.041)

    def test_refuses_wrong_or_lost_session_before_further_tick_reads(self):
        locked = dict(self.SESSION, input_desktop="Winlogon")
        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(idle_diagnostic, "session_identity", return_value=locked), \
             mock.patch.object(win32, "last_input_tick") as last:
            result = idle_diagnostic.diagnose(seconds=1, timestamp=lambda: "now")
        self.assertEqual(result["status"], "session_refused")
        last.assert_not_called()

        elapsed = [0.0]
        def sleep(seconds):
            elapsed[0] += seconds
        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(idle_diagnostic, "session_identity", side_effect=[self.SESSION, locked]), \
             mock.patch.object(win32, "last_input_tick", return_value=100) as last, \
             mock.patch.object(win32, "current_tick", return_value=100):
            result = idle_diagnostic.diagnose(seconds=3, clock=lambda: elapsed[0], sleep=sleep,
                                              timestamp=lambda: "now")
        self.assertEqual(result["status"], "session_lost")
        self.assertEqual(len(result["samples"]), 1)
        last.assert_called_once()

    def test_diagnostic_only_calls_allowlisted_read_only_win32_functions(self):
        tree = ast.parse((HARNESS / "idle_diagnostic.py").read_text(encoding="utf-8"))
        calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == "win32"}
        self.assertEqual(calls, {"require_windows", "desktop_context", "input_desktop",
                                 "last_input_tick", "current_tick"})
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                   for alias in node.names}
        self.assertEqual(imports, {"argparse", "json", "os", "struct", "sys", "time", "win32"})

    def test_refusal_still_writes_structured_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "idle-diagnostic.json"
            with mock.patch.object(idle_diagnostic, "diagnose", return_value={
                    "status": "session_refused", "samples": [], "reset_count": 0}):
                self.assertEqual(idle_diagnostic.main(["--out", str(path)]), 1)
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "session_refused")
            self.assertEqual(record["samples"], [])
            self.assertIn("version", record["python"])


class ImportTests(unittest.TestCase):
    def test_windows_settings_selector_accepts_reused_window_without_matching_murmur(self):
        windows = [(1, 10, "Murmur Settings", "TAURI"),
                   (2, 20, "Settings", "ApplicationFrameWindow"),
                   (3, 30, "Microphone", "Window")]
        with mock.patch.object(win32, "enum_windows", return_value=windows), \
             mock.patch.object(win32, "process_path", side_effect=lambda pid: "SystemSettings.exe" if pid == 30 else "other.exe"):
            self.assertEqual(microphone_checks._windows_settings_windows(), [2, 3])

    def test_target_key_events_preserve_exact_text_when_widget_rejects_ctrl_alt(self):
        keys = [{"char": "", "keysym": "Control_L"}, {"char": "c", "keysym": "c"},
                {"char": "a", "keysym": "a"}, {"char": "f", "keysym": "f"},
                {"char": "é", "keysym": "eacute"}]
        self.assertEqual(targets.delivered_characters(keys), "café")

    def test_terminal_oracle_ignores_only_screen_padding_after_exact_output(self):
        expected = "The quick brown fox jumps over the lazy dog. "
        self.assertTrue(insertion_checks.terminal_line_matches(expected + " " * 20, expected))
        self.assertFalse(insertion_checks.terminal_line_matches(expected[:-2] + " " * 20, expected))
        self.assertFalse(insertion_checks.terminal_line_matches(expected + "".join([" " * 5, expected]), expected))
        self.assertFalse(insertion_checks.terminal_line_matches("x" + expected, expected))

    def test_notepad_readback_never_injects_input_on_uia_failure(self):
        pad = targets.Notepad.__new__(targets.Notepad)
        pad.uia = SimpleNamespace(text=mock.Mock(side_effect=RuntimeError("document rebuilt")))
        pad.win32 = SimpleNamespace(tap=mock.Mock())
        pad.document = mock.Mock(return_value=object())
        pad.focus = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "document rebuilt"):
            pad.text()
        pad.focus.assert_not_called()
        pad.win32.tap.assert_not_called()

    def test_notepad_wait_retries_transient_uia_failure_without_input(self):
        pad = targets.Notepad.__new__(targets.Notepad)
        pad.text = mock.Mock(side_effect=[RuntimeError("document rebuilt"), "hello", "hello"])
        self.assertEqual(pad.wait_text(timeout=1, stable=0, sleep=lambda _: None), "hello")
        self.assertEqual(pad.text.call_count, 3)

    def test_notepad_focus_while_hotkey_held_targets_editor_without_click(self):
        pad = targets.Notepad.__new__(targets.Notepad)
        pad.hwnd = 42
        pad.owned_tab = False
        document = SimpleNamespace(SetFocus=mock.Mock())
        pad.document = mock.Mock(return_value=document)
        pad.uia = SimpleNamespace(center=lambda _: (100, 200), focused=lambda: document,
                                  descendants=lambda _: [], same=lambda first, second: first is second,
                                  describe=lambda _: "Document")
        pad.win32 = SimpleNamespace(activate=mock.Mock(), click=mock.Mock(),
                                    wait_for=lambda predicate, _: predicate(), foreground=lambda: 42)
        pad.focus(click=False)
        pad.win32.activate.assert_called_once_with(42, click_at=None)
        document.SetFocus.assert_called_once_with()
        pad.win32.click.assert_not_called()

    def test_o3_owned_editor_focus_accepts_notepad_child_and_rejects_other_targets(self):
        pad = targets.Notepad.__new__(targets.Notepad)
        pad.hwnd, pad.owner_pid = 42, 33852
        pad.active_document = mock.Mock(return_value=True)
        pad.editor_focused = mock.Mock(return_value=True)
        pad.menu_open = mock.Mock(return_value=False)
        native = {"hwnd": 77, "active_hwnd": 77, "focus_hwnd": 77,
                  "input_desktop": "Default", "gui_status": "ok", "gui_flags": 0,
                  "process_id": 33852}
        roots = {42: 42, 77: 42, 88: 42, 99: 99, 110: 42}
        pids = {42: 33852, 77: 33852, 88: 1234, 99: 1234, 110: 33852}
        pad.win32 = SimpleNamespace(
            GA_ROOT=2, user32=SimpleNamespace(GetAncestor=lambda hwnd, _flag: roots[hwnd]),
            is_window=lambda hwnd: hwnd in roots, window_pid=lambda hwnd: pids[hwnd],
            pids=lambda _name: [33852], foreground_context=lambda: native,
            class_name=lambda hwnd: "RichEditD2DPT" if hwnd == 77 else "Button")
        self.assertTrue(pad.owns_keyboard_focus())  # observed O3 RichEdit foreground
        native["hwnd"] = 42
        self.assertTrue(pad.owns_keyboard_focus())  # ordinary top-level foreground
        native["hwnd"] = 88
        self.assertFalse(pad.owns_keyboard_focus())  # foreign-process child under the same root
        native["hwnd"] = 99
        self.assertFalse(pad.owns_keyboard_focus())  # different root
        native["hwnd"] = 77
        for change in ({"focus_hwnd": 88}, {"focus_hwnd": 99}, {"focus_hwnd": 42},
                       {"active_hwnd": 110, "focus_hwnd": 110},
                       {"gui_flags": 0x4}, {"gui_flags": 0x10},
                       {"input_desktop": "Winlogon"},
                       {"process_id": 1234}):
            with self.subTest(change=change):
                trial = native.copy()
                native.update(change)
                self.assertFalse(pad.owns_keyboard_focus())
                native.clear()
                native.update(trial)
        pad.menu_open.return_value = True
        self.assertFalse(pad.owns_keyboard_focus())
        pad.menu_open.return_value = False
        pad.editor_focused.return_value = False
        self.assertFalse(pad.owns_keyboard_focus())
        pad.editor_focused.return_value = True
        pad.active_document.return_value = False
        self.assertFalse(pad.owns_keyboard_focus())
        pad.active_document.return_value = True
        def switch_tab():
            pad.active_document.return_value = False
            return True
        pad.editor_focused.side_effect = switch_tab
        self.assertFalse(pad.owns_keyboard_focus())
        pad.active_document.return_value = True
        def switch_foreground():
            native["hwnd"] = 99
            return True
        pad.editor_focused.side_effect = switch_foreground
        self.assertFalse(pad.owns_keyboard_focus())
        native["hwnd"] = 77
        pad.editor_focused.side_effect = None
        pids[42] = 1234
        self.assertFalse(pad.owns_keyboard_focus())

    def test_overlay_click_checks_owned_focus_after_click_without_refocusing(self):
        state = {"focused": True}
        ctx = SimpleNamespace(murmur=SimpleNamespace(overlay=lambda: object()),
                              sleep=lambda _: None)
        with mock.patch.object(common_checks, "overlay_button", return_value=object()), \
             mock.patch.object(common_checks.uia, "center", return_value=(10, 20)), \
             mock.patch.object(common_checks.win32, "click", side_effect=lambda *_: state.update(focused=False)) as click, \
             mock.patch.object(common_checks.win32, "foreground", side_effect=AssertionError("raw hwnd check")):
            with self.assertRaisesRegex(Fail, "moved keyboard focus"):
                common_checks.click_overlay(ctx, "Discard", expect_focus=lambda: state["focused"])
        click.assert_called_once_with(10, 20)

    def test_notepad_mode_label_reports_visible_plain_or_formatted_state(self):
        pad = targets.Notepad.__new__(targets.Notepad)
        pad.hwnd = 42
        window = object()
        labels = {"Plain text": [], "Formatted": [object()]}
        pad.uia = SimpleNamespace(from_handle=lambda _: window,
                                  find_all=lambda _window, name_: labels[name_])
        self.assertEqual(pad.editor_mode_label(), "Formatted")
        labels["Plain text"] = [object()]
        self.assertEqual(pad.editor_mode_label(), "Plain text")

    def test_native_focus_snapshot_reports_menu_mode_without_input(self):
        def gui_info(_thread, pointer):
            info = win32.ctypes.cast(pointer, win32.ctypes.POINTER(win32.GUITHREADINFO)).contents
            self.assertEqual(info.cbSize, win32.ctypes.sizeof(win32.GUITHREADINFO))
            info.flags = 0x14  # menu and popup-menu modes
            info.hwndFocus = 77
            info.hwndMenuOwner = 42
            return 1

        fake = SimpleNamespace(GetWindowThreadProcessId=lambda _hwnd, _pid: 9,
                               GetGUIThreadInfo=gui_info)
        with mock.patch.object(win32, "user32", fake), mock.patch.object(win32, "class_name",
                                                                          return_value="NotepadEditor"):
            state = win32.gui_thread_focus(42)
        self.assertEqual(state, {"focus_hwnd": "0x4d", "focus_class": "NotepadEditor",
                                 "menu_owner_hwnd": "0x2a", "menu_mode": True,
                                 "popup_menu_mode": True})

    def test_opt_in_hotkey_probe_samples_releases_and_preserves_exact_oracle(self):
        self.assertNotIn("H8", [check.id for check in catalog.select()])
        self.assertEqual([check.id for check in catalog.select("H8")], ["H8"])
        phases = []
        pad = SimpleNamespace(focus=mock.Mock(), wait_text=mock.Mock(return_value="partial"))
        session = SimpleNamespace(ensure=mock.Mock(), wait_state=mock.Mock(return_value=True),
                                  overlay_state=lambda: "done")
        ctx = SimpleNamespace(murmur=session, fixtures={"A": SimpleNamespace(path="a.wav", speech_seconds=0)},
                              notepad=lambda *_, **__: pad, observe_delivery=mock.Mock(), sleep=lambda _: None,
                              observed_text=lambda **_: "full", mark_delivery_released=mock.Mock(),
                              settle=mock.Mock(),
                              guard=SimpleNamespace(start_key_trace=mock.Mock(),
                                                    stop_key_trace=lambda: {"events": [], "overflow": False}))
        with mock.patch.object(hotkey_checks, "_focus_phase", side_effect=lambda _pad, stage: {"stage": stage}), \
             mock.patch.object(win32, "key_down") as down, \
             mock.patch.object(win32, "key_up") as up, \
             mock.patch.object(win32, "release_all") as release:
            returned_pad, difference, expected, observation = hotkey_checks._notepad_focus_trial(
                ctx, "Ctrl+Alt+Shift+F9", "alt", phases)
        self.assertIs(returned_pad, pad)
        self.assertTrue(difference)
        self.assertEqual(expected, "full")
        self.assertEqual(observation["status"], "output")
        self.assertEqual([phase["stage"] for phase in phases],
                         ["before modifiers", "before F9 down", "F9 held", "listening", "before F9 up",
                          "after F9 up", "after shift up", "after alt up", "after ctrl up", "before delivery wait",
                          "history confirmed", "after delivery wait"])
        self.assertTrue(all(phase["focus_snapshot"] == "deferred" for phase in phases[5:-1]))
        self.assertEqual(down.call_args_list, [mock.call("ctrl", "alt", "shift"), mock.call("f9")])
        self.assertEqual(up.call_args_list, [mock.call("f9"), mock.call("shift"), mock.call("alt"),
                                              mock.call("ctrl")])
        release.assert_called_once_with()

    def test_h8_release_has_no_target_focus_reads_until_history_confirms_output(self):
        events = []
        released = False
        history_confirmed = False
        expected = "The quick brown fox jumps over the lazy dog. "

        def focus_snapshot(_pad, stage):
            events.append(("focus", stage))
            self.assertFalse(released and not history_confirmed,
                             f"Notepad focus read during insertion: {stage}")
            return {"stage": stage}

        def key_up(key):
            nonlocal released
            events.append(("up", key))
            if key == "f9":
                released = True

        def history_after(_cursor):
            nonlocal history_confirmed
            events.append(("history", None))
            if events.count(("history", None)) == 1:
                return []
            history_confirmed = True
            return [(1, expected)]

        def readback(**_):
            events.append(("readback", None))
            self.assertTrue(history_confirmed)
            return "partial"

        pad = SimpleNamespace(focus=mock.Mock(), wait_text=readback)
        session = SimpleNamespace(ensure=mock.Mock(), history_cursor=lambda: 0,
                                  history_after=history_after, wait_state=lambda *_: True,
                                  overlay_state=lambda: "done")
        ctx = Context.__new__(Context)
        ctx.murmur = session
        ctx.fixtures = {"A": SimpleNamespace(path="a.wav", speech_seconds=0)}
        ctx.notepad = lambda *_, **__: pad
        ctx.sleep = lambda _: None
        ctx.mark_delivery_released = lambda: None
        ctx.settle = lambda: events.append(("settled", None))
        ctx.guard = SimpleNamespace(start_key_trace=mock.Mock(),
                                    stop_key_trace=mock.Mock(return_value={"events": [], "overflow": False}))
        phases = []
        with mock.patch.object(hotkey_checks, "_focus_phase", side_effect=focus_snapshot), \
             mock.patch.object(win32, "key_down"), mock.patch.object(win32, "key_up", side_effect=key_up), \
             mock.patch.object(win32, "release_all") as release:
            _, difference, observed, observation = hotkey_checks._notepad_focus_trial(
                ctx, "Ctrl+Alt+Shift+F9", "alt", phases)
        self.assertEqual(observed, expected)
        self.assertTrue(difference)
        self.assertEqual(observation["status"], "output")
        self.assertLess(events.index(("up", "f9")), events.index(("history", None)))
        self.assertLess(events.index(("history", None)), events.index(("readback", None)))
        self.assertLess(events.index(("settled", None)), events.index(("focus", "after delivery wait")))
        self.assertEqual([stage for kind, stage in events if kind == "focus"],
                         ["before modifiers", "before F9 down", "F9 held", "listening",
                          "before F9 up", "after delivery wait"])
        release.assert_called_once_with()
        ctx.guard.stop_key_trace.assert_called_once_with()

    def test_notepad_direct_control_keeps_guard_and_exact_readback(self):
        pad = SimpleNamespace(focus=mock.Mock(), wait_text=mock.Mock(return_value="corrupt"))
        guard_check = mock.Mock()
        guard_state = mock.Mock(side_effect=[{"probes_seen": 3}, {"probes_seen": 5}])
        ctx = SimpleNamespace(notepad=lambda *_, **__: pad,
                              guard=SimpleNamespace(check=guard_check, diagnostics=guard_state,
                                                    start_key_trace=mock.Mock(),
                                                    stop_key_trace=lambda: {"events": [], "overflow": False}),
                              sleep=lambda _: None)
        phases = []
        with mock.patch.object(hotkey_checks, "_focus_phase", side_effect=lambda _pad, stage: {"stage": stage}), \
             mock.patch.object(win32, "keys_down", return_value=[]), \
             mock.patch.object(win32, "type_unicode") as send:
            returned_pad, difference, expected, observation = hotkey_checks._notepad_direct_control(ctx, phases)
        self.assertIs(returned_pad, pad)
        self.assertEqual(expected, hotkey_checks.DIRECT_CONTROL)
        self.assertIn("differs at character", difference)
        send.assert_called_once_with(expected, delay=0.012)
        self.assertEqual(guard_check.call_count, 2)
        self.assertEqual((observation["probes_during_input"], observation["target_chars"]), (2, 7))
        self.assertEqual(observation["key_trace"]["packet_text"], "")
        self.assertEqual([phase["stage"] for phase in phases],
                         ["before direct input", "after direct input", "after direct readback"])

    def test_hotkey_trial_records_no_output_as_held_refusal(self):
        phases = []
        pad = SimpleNamespace(focus=mock.Mock(), wait_text=mock.Mock(return_value=""))
        session = SimpleNamespace(ensure=mock.Mock(), wait_state=mock.Mock(return_value=True),
                                  overlay_state=lambda: "held")
        ctx = SimpleNamespace(murmur=session, fixtures={"A": SimpleNamespace(path="a.wav", speech_seconds=0)},
                              notepad=lambda *_, **__: pad, observe_delivery=mock.Mock(), sleep=lambda _: None,
                              observed_text=mock.Mock(side_effect=OutputCount(0)), settle=mock.Mock(),
                              mark_delivery_released=mock.Mock(), capture_output_failure=mock.Mock(),
                              guard=SimpleNamespace(start_key_trace=mock.Mock(),
                                                    stop_key_trace=lambda: {"events": [], "overflow": False}))
        with mock.patch.object(hotkey_checks, "_focus_phase", side_effect=lambda _pad, stage: {"stage": stage}) as focus, \
             mock.patch.object(win32, "key_down"), mock.patch.object(win32, "key_up"), \
             mock.patch.object(win32, "release_all") as release:
            _, difference, expected, observation = hotkey_checks._notepad_focus_trial(
                ctx, "Ctrl+Alt+Shift+F9", "alt", phases)
        self.assertIsNone(expected)
        self.assertIsNone(difference)
        self.assertEqual(observation["status"], "held_refused")
        self.assertEqual(observation["output_error"], "Expected one output for this utterance, found 0")
        self.assertEqual(observation["key_trace"]["packet_text"], "")
        self.assertEqual(phases[-1]["stage"], "history unavailable")
        self.assertEqual(focus.call_count, 5)
        release.assert_called_once_with()

    def test_h8_missing_history_does_not_read_notepad_while_app_is_transcribing(self):
        pad = SimpleNamespace(focus=mock.Mock(), wait_text=mock.Mock())
        ctx = SimpleNamespace(murmur=SimpleNamespace(ensure=mock.Mock(), wait_state=lambda *_: True,
                                                     overlay_state=lambda: "transcribing"),
                              fixtures={"A": SimpleNamespace(path="a.wav", speech_seconds=0)},
                              notepad=lambda *_, **__: pad, observe_delivery=mock.Mock(), sleep=lambda _: None,
                              mark_delivery_released=mock.Mock(),
                              observed_text=mock.Mock(side_effect=OutputCount(0)),
                              capture_output_failure=mock.Mock(), settle=mock.Mock(return_value="transcribing"),
                              guard=SimpleNamespace(start_key_trace=mock.Mock(),
                                                    stop_key_trace=lambda: {"events": [], "overflow": False}))
        with mock.patch.object(hotkey_checks, "_focus_phase", side_effect=lambda _pad, stage: {"stage": stage}), \
             mock.patch.object(win32, "key_down"), mock.patch.object(win32, "key_up"), \
             mock.patch.object(win32, "release_all"):
            _, _, expected, observation = hotkey_checks._notepad_focus_trial(
                ctx, "Ctrl+Alt+Shift+F9", "alt", [])
        self.assertIsNone(expected)
        self.assertEqual(observation["status"], "no_output")
        self.assertEqual(observation["target_chars"], 0)
        ctx.observed_text.assert_called_once_with(timeout=45)
        ctx.capture_output_failure.assert_called_once_with("alt-output-failure", terminal_state="transcribing")
        pad.wait_text.assert_not_called()

    def test_h8_history_error_cleans_up_without_post_release_focus_read(self):
        phases = []
        pad = SimpleNamespace(focus=mock.Mock(), wait_text=mock.Mock())
        ctx = SimpleNamespace(murmur=SimpleNamespace(ensure=mock.Mock(), wait_state=lambda *_: True),
                              fixtures={"A": SimpleNamespace(path="a.wav", speech_seconds=0)},
                              notepad=lambda *_, **__: pad, observe_delivery=mock.Mock(), sleep=lambda _: None,
                              observed_text=mock.Mock(side_effect=RuntimeError("history unavailable")),
                              mark_delivery_released=mock.Mock(),
                              guard=SimpleNamespace(start_key_trace=mock.Mock(),
                                                    stop_key_trace=mock.Mock(return_value={
                                                        "events": [], "overflow": False})))
        with mock.patch.object(hotkey_checks, "_focus_phase", side_effect=lambda _pad, stage: {"stage": stage}) as focus, \
             mock.patch.object(win32, "key_down"), mock.patch.object(win32, "key_up") as up, \
             mock.patch.object(win32, "release_all") as release:
            with self.assertRaisesRegex(RuntimeError, "history unavailable"):
                hotkey_checks._notepad_focus_trial(ctx, "Ctrl+Alt+Shift+F9", "alt", phases)
        self.assertEqual(focus.call_count, 5)
        self.assertEqual([phase["stage"] for phase in phases[-5:]],
                         ["after F9 up", "after shift up", "after alt up", "after ctrl up",
                          "before delivery wait"])
        self.assertEqual(up.call_args_list, [mock.call("f9"), mock.call("shift"), mock.call("alt"),
                                              mock.call("ctrl")])
        pad.wait_text.assert_not_called()
        release.assert_called_once_with()
        ctx.guard.stop_key_trace.assert_called_once_with()

    def test_drag_sampling_keeps_held_geometry_assertion(self):
        samples = []
        with mock.patch.object(win32, "mouse_move"), mock.patch.object(win32, "mouse_down"), \
             mock.patch.object(win32, "mouse_up") as up, mock.patch.object(win32.time, "sleep"):
            win32.drag((100, 100), (50, 50), steps=4, release=False,
                       sample=lambda stage, step: samples.append((stage, step)))
        self.assertEqual(samples, [("at-start", 0), ("button-down", 0), ("held-before-move", 0),
                                   ("moving", 1), ("moving", 2), ("moving", 4)])
        up.assert_not_called()

    def test_hotkey_probe_records_both_trials_even_when_alt_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f"{name}.txt" for name in ("control", "alt", "no-alt")]
            pads = [SimpleNamespace(hwnd=number, path=path,
                                    text=lambda number=number: "partial" if number == 2 else "full",
                                    save_owned=mock.Mock(side_effect=lambda path=path, number=number:
                                                    path.write_text("partial" if number == 2 else "full")))
                    for number, path in enumerate(paths, 1)]
            screenshot = mock.Mock(return_value=Path(directory) / "screenshot.png")
            ctx = SimpleNamespace(artifact=lambda suffix: Path(directory) / suffix, screenshot=screenshot,
                                  murmur=SimpleNamespace(log=mock.Mock(side_effect=[
                                      "before", "before\nalt\n", "before\nalt\n",
                                      "before\nalt\nno-alt\n"])))
            with mock.patch.object(hotkey_checks, "_notepad_direct_control",
                                   return_value=(pads[0], None, "full", {"probes_during_input": 1})), \
                 mock.patch.object(hotkey_checks, "_notepad_focus_trial",
                                   side_effect=[(pads[1], "wrong text", "full", {"output_error": None}),
                                                (pads[2], None, "full", {"output_error": None})]) as trial:
                with self.assertRaisesRegex(Fail, "alt: wrong text"):
                    hotkey_checks.h8_focus_phases(ctx)
            self.assertEqual(trial.call_count, 2)
            self.assertEqual([pad.save_owned.call_count for pad in pads], [1, 1, 1])
            self.assertEqual(screenshot.call_args_list,
                             [mock.call(1, "control-notepad.png"), mock.call(2, "alt-notepad.png"),
                              mock.call(3, "no-alt-notepad.png")])
            evidence = json.loads((Path(directory) / "focus-phases.json").read_text())
            self.assertEqual(evidence["alt"]["difference"], "wrong text")
            self.assertIsNone(evidence["no-alt"]["difference"])
            self.assertIn("differs at character", evidence["alt"]["saved_file_difference"])
            self.assertIsNone(evidence["no-alt"]["saved_file_difference"])
            self.assertTrue(evidence["alt"]["saved_file_updated"])
            self.assertTrue(evidence["no-alt"]["saved_file_updated"])
            self.assertEqual((Path(directory) / "control-saved.txt").read_text(), "full")
            self.assertEqual((Path(directory) / "alt-app-log.txt").read_text(), "\nalt\n")
            self.assertEqual((Path(directory) / "no-alt-app-log.txt").read_text(), "no-alt\n")

    def test_hotkey_probe_copies_exact_saved_file_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notepad.txt"
            path.write_bytes(b"")
            # Pin the fixture to 2000 so a fast Windows rewrite still changes its mtime.
            os.utime(path, (946684800, 946684800))
            saved_bytes = b"\xef\xbb\xbfcaf\xc3\xa9\r\n"
            pad = SimpleNamespace(hwnd=42, path=path, text=lambda: "café\r\n",
                                  save_owned=mock.Mock(side_effect=lambda: path.write_bytes(saved_bytes)))
            ctx = SimpleNamespace(artifact=lambda suffix: Path(directory) / suffix,
                                  screenshot=mock.Mock(), murmur=SimpleNamespace(log=lambda: ""))
            result = {"saved_file_difference": None, "saved_file_error": None}
            hotkey_checks._capture_notepad_trial(ctx, "control", result, pad, "café\r\n")
            self.assertEqual((Path(directory) / "control-saved.txt").read_bytes(), saved_bytes)
            self.assertIsNone(result["saved_file_difference"])
            self.assertTrue(result["saved_file_updated"])
            self.assertIsNone(result["capture_difference"])
            pad.save_owned.assert_called_once_with()
            ctx.screenshot.assert_called_once_with(42, "control-notepad.png")

    def test_hotkey_probe_records_held_refusal_and_still_runs_non_alt(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f"{name}.txt" for name in ("control", "alt", "no-alt")]
            pads = [SimpleNamespace(hwnd=i, path=path, text=lambda: "full",
                                    save_owned=mock.Mock(side_effect=lambda p=path, i=i:
                                     p.write_text("" if i == 2 else "full")))
                    for i, path in enumerate(paths, 1)]
            ctx = SimpleNamespace(artifact=lambda suffix: Path(directory) / suffix,
                                  screenshot=mock.Mock(), murmur=SimpleNamespace(log=lambda: ""))
            with mock.patch.object(hotkey_checks, "_notepad_direct_control",
                                   return_value=(pads[0], None, "full", {"probes_during_input": 1})), \
                 mock.patch.object(hotkey_checks, "_notepad_focus_trial",
                                   side_effect=[(pads[1], None, None,
                                                 {"status": "held_refused", "overlay": "held", "output_error":
                                                  "Expected one output for this utterance, found 0"}),
                                                (pads[2], None, "full",
                                                 {"status": "output", "output_error": None})]) as trial:
                with self.assertRaisesRegex(Fail, "alt: Expected one output"):
                    hotkey_checks.h8_focus_phases(ctx)
            self.assertEqual(trial.call_count, 2)
            evidence = json.loads((Path(directory) / "focus-phases.json").read_text())
            self.assertEqual(evidence["alt"]["observation"]["status"], "held_refused")
            self.assertIn("no-alt", evidence)

    def test_hotkey_probe_stops_before_target_capture_if_output_never_settles(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = SimpleNamespace(artifact=lambda suffix: Path(directory) / suffix,
                                  murmur=SimpleNamespace(log=lambda: "synthetic log"))
            with mock.patch.object(hotkey_checks, "_notepad_direct_control",
                                   return_value=(SimpleNamespace(hwnd=1), None, "control",
                                                 {"probes_during_input": 1})), \
                 mock.patch.object(hotkey_checks, "_notepad_focus_trial",
                                   return_value=(object(), None, None,
                                                 {"status": "no_output", "output_error": "found 0",
                                                  "overlay": "transcribing"})) as trial, \
                 mock.patch.object(hotkey_checks, "_capture_notepad_trial") as capture:
                with self.assertRaisesRegex(Fail, "alt: found 0"):
                    hotkey_checks.h8_focus_phases(ctx)
            trial.assert_called_once()
            self.assertEqual(capture.call_count, 1)  # direct control only
            evidence = json.loads((Path(directory) / "focus-phases.json").read_text())
            self.assertEqual(evidence["alt"]["observation"]["overlay"], "transcribing")
            self.assertNotIn("no-alt", evidence)

    def test_h8_keeps_three_owned_tabs_through_ensure_and_captures_each_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = root / "unrelated.txt"
            unrelated.write_text("keep me", encoding="utf-8")
            events, calls, pads = [], [], {}
            restorer = restore.Restorer(log=lambda _: None)
            actuals = {"control": hotkey_checks.DIRECT_CONTROL, "alt": "wrong", "no-alt": "full"}

            def notepad(name, *, reuse_hwnd=None, owned_tab=False):
                calls.append((name, reuse_hwnd, owned_tab))
                events.append(f"open {name}")
                path = root / f"{name}.txt"
                path.write_bytes(b"")
                os.utime(path, (946684800, 946684800))

                def wait_text(**_):
                    events.append(f"wait {name}")
                    return actuals[name]

                pad = SimpleNamespace(hwnd=101, path=path,
                                      focus=lambda: events.append(f"focus {name}"),
                                      wait_text=wait_text,
                                      text=lambda: events.append(f"read {name}") or actuals[name])

                def save():
                    events.append(f"save {name}")
                    path.write_bytes(actuals[name].encode("utf-8"))

                pad.save_owned = save
                restorer.push(f"close {name}", lambda: events.append(f"close {name}"))
                pads[name] = pad
                return pad

            session = SimpleNamespace(ensure=lambda *_, **__: events.append("ensure"),
                                      wait_state=lambda *_: True, overlay_state=lambda: "done", log=lambda: "")
            ctx = SimpleNamespace(artifact=lambda suffix: root / suffix, screenshot=mock.Mock(),
                                  murmur=session, notepad=notepad, restorer=restorer,
                                  fixtures={"A": SimpleNamespace(path="a.wav", speech_seconds=0)},
                                  observe_delivery=lambda _: None, observed_text=lambda **_: "full",
                                  mark_delivery_released=lambda: None,
                                  settle=lambda: None, sleep=lambda _: None,
                                  guard=SimpleNamespace(check=lambda: None,
                                                        diagnostics=lambda: {"probes_seen": 1},
                                                        start_key_trace=lambda: None,
                                                        stop_key_trace=lambda: {"events": [], "overflow": False}))
            with mock.patch.object(hotkey_checks, "_focus_phase", side_effect=lambda _pad, stage: {"stage": stage}), \
                 mock.patch.object(win32, "keys_down", return_value=[]), \
                 mock.patch.object(win32, "type_unicode"), mock.patch.object(win32, "key_down"), \
                 mock.patch.object(win32, "key_up"), mock.patch.object(win32, "release_all"):
                with self.assertRaisesRegex(Fail, "alt: differs at character"):
                    hotkey_checks.h8_focus_phases(ctx)

            self.assertEqual(calls, [("control", None, True), ("alt", 101, True), ("no-alt", 101, True)])
            for name in actuals:
                self.assertLess(events.index(f"wait {name}"), events.index(f"read {name}"))
                self.assertLess(events.index(f"read {name}"), events.index(f"save {name}"))
            ensures = [index for index, event in enumerate(events) if event == "ensure"]
            self.assertEqual(len(ensures), 2)
            self.assertLess(events.index("save control"), ensures[0])
            self.assertLess(ensures[0], events.index("open alt"))
            self.assertLess(events.index("focus alt"), events.index("wait alt"))
            self.assertLess(events.index("save alt"), ensures[1])
            self.assertLess(ensures[1], events.index("open no-alt"))
            self.assertLess(events.index("focus no-alt"), events.index("wait no-alt"))
            self.assertFalse(any(event.startswith("close ") for event in events))
            self.assertEqual([(root / f"{name}-saved.txt").read_bytes() for name in actuals],
                             [hotkey_checks.DIRECT_CONTROL.encode("utf-8"), b"wrong", b"full"])
            evidence = json.loads((root / "focus-phases.json").read_text())
            self.assertIsNone(evidence["control"]["capture_difference"])
            self.assertIn("differs at character", evidence["alt"]["capture_difference"])
            self.assertIn("differs at character", evidence["alt"]["saved_file_difference"])
            self.assertEqual(restorer.restore_all(), [])
            self.assertEqual(events[-3:], ["close no-alt", "close alt", "close control"])
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me")

    def test_h8_compares_direct_sender_in_same_tab_after_no_alt_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = []
            pads = []
            for name in ("control", "alt", "no-alt"):
                path = root / f"{name}.txt"
                path.write_text("", encoding="utf-8")
                state = {"text": "wrong" if name == "no-alt" else "good"}

                def save(p=path, s=state, n=name):
                    events.append(f"save {n} {s['text']}")
                    p.write_text(s["text"], encoding="utf-8")

                def clear(s=state):
                    events.append("clear no-alt")
                    s["text"] = ""

                pads.append(SimpleNamespace(hwnd=42, path=path, text=lambda s=state: s["text"],
                                            save_owned=save, clear_owned=clear, state=state))

            def direct(ctx, pad, phases, text):
                self.assertIs(pad, pads[2])
                self.assertEqual(text, "good")
                events.append("direct no-alt")
                pad.state["text"] = text
                return pad, None, text, {"target_chars": len(text)}

            ctx = SimpleNamespace(artifact=lambda suffix: root / suffix, screenshot=mock.Mock(),
                                  murmur=SimpleNamespace(log=lambda: ""))
            with mock.patch.object(hotkey_checks, "_notepad_direct_control",
                                   return_value=(pads[0], None, "good", {"target_chars": 4})), \
                 mock.patch.object(hotkey_checks, "_notepad_focus_trial",
                                   side_effect=[(pads[1], None, "good", {"output_error": None}),
                                                (pads[2], "bad output", "good", {"output_error": None})]), \
                 mock.patch.object(hotkey_checks, "_notepad_direct_input", side_effect=direct):
                with self.assertRaisesRegex(Fail, "no-alt: bad output"):
                    hotkey_checks.h8_focus_phases(ctx)

            self.assertLess(events.index("save no-alt wrong"), events.index("clear no-alt"))
            self.assertLess(events.index("clear no-alt"), events.index("direct no-alt"))
            self.assertEqual((root / "no-alt-saved.txt").read_text(), "wrong")
            self.assertEqual((root / "same-tab-direct-saved.txt").read_text(), "good")
            evidence = json.loads((root / "focus-phases.json").read_text())
            self.assertEqual(evidence["no-alt"]["difference"], "bad output")
            self.assertIn("differs at character", evidence["no-alt"]["saved_file_difference"])
            self.assertIsNone(evidence["same-tab-direct"]["difference"])
            self.assertIsNone(evidence["same-tab-direct"]["saved_file_difference"])

    def test_notepad_h8_reuses_only_named_owned_window(self):
        with tempfile.TemporaryDirectory() as directory:
            pad = targets.Notepad(directory, "H8-alt")
            self.assertIn(Path(directory).name, pad.path.name)
            with tempfile.TemporaryDirectory() as another:
                self.assertNotEqual(pad.path.name, targets.Notepad(another, "H8-alt").path.name)
                self.assertNotEqual(targets.Notepad(directory, "H1-doc").path.name,
                                    targets.Notepad(another, "H1-doc").path.name)
            pad.win32 = SimpleNamespace(find_windows=lambda **_: [42],
                                        window_text=lambda _: f"{pad.path.name} - Notepad",
                                        is_window=lambda _: True, window_pid=lambda _: 7,
                                        pids=lambda _: [7],
                                        wait_for=lambda predicate, _: predicate())
            with mock.patch.object(targets.subprocess, "Popen"):
                self.assertIs(pad.start(reuse_hwnd=42, owned_tab=True, reuse_owner_pid=7), pad)
                self.assertTrue(pad.active_document())
                pad.path.unlink()
                with self.assertRaisesRegex(RuntimeError, "Notepad did not open"):
                    targets.Notepad.start(pad, reuse_hwnd=99, owned_tab=True, reuse_owner_pid=7)
                pad.win32.window_text = lambda _: "notepad-H8-control.txt - Notepad"
                pad.path.unlink()
                with self.assertRaisesRegex(RuntimeError, "Notepad did not open"):
                    targets.Notepad.start(pad, reuse_hwnd=42, owned_tab=True, reuse_owner_pid=7)

                pad.win32.window_text = lambda _: f"{pad.path.name} - Notepad"
                pad.path.unlink()
                with self.assertRaisesRegex(RuntimeError, "Notepad did not open"):
                    targets.Notepad.start(pad, reuse_hwnd=42, owned_tab=True, reuse_owner_pid=8)
                pad.win32.pids = lambda _: [8]
                pad.path.unlink()
                with self.assertRaisesRegex(RuntimeError, "owner is not Notepad.exe"):
                    targets.Notepad.start(pad, reuse_hwnd=42, owned_tab=True, reuse_owner_pid=7)

    def test_h8_context_registers_tab_cleanup_instead_of_window_close(self):
        ctx = Context.__new__(Context)
        ctx.work, ctx.check_id, ctx.attempt = Path("work"), "H8", 1
        ctx._completed_notepads, ctx._notepad_serial = [], 0
        ctx.restorer = restore.Restorer(log=lambda _: None)
        ctx.guard = SimpleNamespace(check=lambda: None)
        pad = SimpleNamespace(path=Path("notepad-H8-alt.txt"), close=mock.Mock(),
                              close_owned=mock.Mock())
        pad.start = mock.Mock(return_value=pad)
        with mock.patch.object(targets, "Notepad", return_value=pad) as create:
            self.assertIs(ctx.notepad("alt", owned_tab=True), pad)
        create.assert_called_once_with(Path("work"), "H8-alt-a1-c1")
        pad.start.assert_called_once_with(reuse_hwnd=None, owned_tab=True,
                                          reuse_owner_pid=None, reuse_windows={})
        self.assertEqual(ctx.restorer.restore_all(), [])
        self.assertEqual(ctx.close_completed_notepads(), [])
        pad.close_owned.assert_called_once_with(max_tabs=65, guard_check=ctx.guard.check)
        pad.close.assert_not_called()

    def test_notepad_accepts_an_older_verified_window_but_rejects_unknown_existing_window(self):
        with tempfile.TemporaryDirectory() as directory:
            pad = targets.Notepad(directory, "I4-doc-a1-c3")
            owners = {42: 7, 99: 8}
            pad.win32 = SimpleNamespace(
                find_windows=lambda **kwargs: [42, 99] if kwargs.get("contains") == "Notepad" else [42],
                window_text=lambda _: f"{pad.path.name} - Notepad",
                window_pid=lambda hwnd: owners[hwnd], pids=lambda _: [7, 8],
                wait_for=lambda predicate, _: predicate())
            with mock.patch.object(targets.subprocess, "Popen"):
                self.assertIs(pad.start(owned_tab=True, reuse_windows=owners), pad)
                self.assertEqual((pad.hwnd, pad.owner_pid), (42, 7))
                pad.path.unlink()
                with self.assertRaisesRegex(RuntimeError, "Notepad did not open"):
                    pad.start(owned_tab=True, reuse_windows={99: 8})
                owners[42] = 9
                pad.path.unlink()
                with self.assertRaisesRegex(RuntimeError, "Notepad did not open"):
                    pad.start(owned_tab=True, reuse_windows={42: 7, 99: 8})

    def test_notepad_start_never_erases_an_existing_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            pad = targets.Notepad(directory, "I1-doc-a1-c1")
            pad.path.write_text("existing text", encoding="utf-8")
            with mock.patch.object(targets.subprocess, "Popen") as spawn:
                with self.assertRaises(FileExistsError):
                    pad.start(owned_tab=True)
            spawn.assert_not_called()
            self.assertEqual(pad.path.read_text(encoding="utf-8"), "existing text")

    def test_context_passes_each_verified_window_for_new_tab_and_fails_on_owner_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = Context(build=None, session=SimpleNamespace(base_config={"privacy": {}}), fixtures={},
                          goldens=None, restorer=restore.Restorer(log=lambda _: None),
                          guard=SimpleNamespace(check=lambda: None), out=directory, work=directory,
                          hotkey="", cache=directory)
            starts, owners = [], {42: 7, 99: 8}

            def start(pad, **options):
                starts.append(options)
                pad.hwnd = (42, 99, 42, 42)[len(starts) - 1]
                pad.owner_pid = owners[pad.hwnd]
                pad.owned_tab = options["owned_tab"]
                pad.win32 = SimpleNamespace(is_window=lambda _: True,
                                            window_pid=lambda hwnd: owners[hwnd],
                                            pids=lambda _: list(owners.values()))
                return pad

            with mock.patch.object(targets.Notepad, "start", autospec=True, side_effect=start):
                for check_id in ("H1", "I4", "O3"):
                    ctx.begin(check_id)
                    ctx.notepad()
                self.assertEqual(starts[2]["reuse_windows"], {42: 7, 99: 8})
                ctx.begin("H8")
                ctx.notepad("alt", reuse_hwnd=42, owned_tab=True)
                self.assertEqual((starts[3]["reuse_owner_pid"], starts[3]["reuse_windows"]), (7, None))
                self.assertEqual(len({pad.path.name for pad in ctx._completed_notepads}), 4)
                owners[99] = 9
                ctx.begin("O4")
                with self.assertRaisesRegex(Fail, "ownership changed"):
                    ctx.notepad()
                self.assertEqual(len(starts), 4)

    def test_f3_explicit_target_destruction_stays_immediate(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            restorer = restore.Restorer(log=lambda _: None)
            ctx = Context(build=None, session=SimpleNamespace(base_config={"privacy": {}},
                          ensure=lambda *_: None), fixtures={"A": SimpleNamespace(path="a.wav")},
                          goldens=None, restorer=restorer, guard=SimpleNamespace(check=lambda: None),
                          out=directory, work=directory, hotkey="", cache=directory)
            ctx.begin("F3")
            mark = restorer.mark()
            apps = []

            def create(*_args, **_kwargs):
                name = "A" if not apps else "A2"
                app = mock.Mock(title=name, hwnd=len(apps) + 1)
                app.start.side_effect = lambda: events.append(f"start {name}") or app
                app.close.side_effect = lambda: events.append(f"close {name}")
                apps.append(app)
                return app

            ctx.dictate = lambda _fixture, during: during(ctx)
            with mock.patch.object(targets, "TargetApp", side_effect=create), \
                 mock.patch.object(focus_checks, "wait_held"), \
                 mock.patch.object(focus_checks, "require_empty"), \
                 mock.patch.object(focus_checks, "click_overlay"), \
                 mock.patch.object(focus_checks, "wait_not_held"):
                focus_checks.f3_window_replaced(ctx)
            self.assertEqual(events[:3], ["start A", "close A", "start A2"])
            self.assertEqual(ctx._completed_notepads, [])
            self.assertEqual(restorer.restore_to(mark), [])
            self.assertEqual(events[-2:], ["close A2", "close A"])

    def test_all_automatic_notepads_retained_with_unique_repeat_identity_and_immediate_state_undo(self):
        with tempfile.TemporaryDirectory() as directory:
            events, pads, starts = [], [], []
            clipboard = {"text": "original"}
            restorer = restore.Restorer(log=lambda _: None)
            restorer.push("run baseline", lambda: events.append("run baseline"))
            baseline = restorer.mark()
            ctx = Context(build=None, session=SimpleNamespace(base_config={"privacy": {}}), fixtures={},
                          goldens=None, restorer=restorer, guard=SimpleNamespace(check=lambda: None),
                          out=directory, work=directory, hotkey="", cache=directory)

            def start(pad, **kwargs):
                starts.append(kwargs)
                pad.hwnd, pad.owner_pid, pad.owned_tab = 42, 7, kwargs["owned_tab"]
                pad.win32 = SimpleNamespace(is_window=lambda _: True, window_pid=lambda _: 7,
                                            pids=lambda _: [7])
                pads.append(pad)
                return pad

            def lookup(check):
                def run_check(context):
                    # These represent clipboard, settings, registry, and mouse undos. Each
                    # must run before the next check despite the retained Notepad tab.
                    self.assertEqual(clipboard["text"], "original")
                    context.clipboard_guard()
                    clipboard["text"] = f"changed by {check.id}"
                    for label in ("settings", "registry", "mouse"):
                        context.restorer.push(label, lambda label=label: events.append(label))
                    if check.id in run.NOTEPAD_CHECKS:
                        context.notepad("control" if check.id == "H8" else "doc")
                    if len(pads) > 1:
                        self.assertEqual(events[-4:], ["mouse", "registry", "settings", "clipboard"])
                return run_check

            with mock.patch.object(targets.Notepad, "start", autospec=True, side_effect=start), \
                 mock.patch.object(targets.Notepad, "close_owned", autospec=True,
                                   side_effect=lambda pad, **_: events.append(f"close {pad.path.name}")), \
                 mock.patch.object(targets.Notepad, "close", autospec=True) as close_window, \
                 mock.patch.object(win32, "clipboard_text", side_effect=lambda: clipboard["text"]), \
                 mock.patch.object(win32, "clipboard_formats", return_value=[]), \
                 mock.patch.object(win32, "set_clipboard_text",
                                   side_effect=lambda value: (clipboard.update(text=value), events.append("clipboard"))), \
                 mock.patch.object(win32, "release_all"):
                selection = "H1,H8,I1,I4,I5,I6,O3,O4,F13,C1,C3"
                results, aborted = run.run_checks(ctx, catalog.select(selection), 2, lookup,
                                                   log=lambda _: None)
                self.assertEqual([result.status for result in results], [report.PASS] * 22)
                self.assertEqual(aborted, "")
                self.assertEqual(clipboard["text"], "original")
                self.assertEqual(restorer.mark(), baseline)
                self.assertEqual(len(pads), 16)
                self.assertEqual(len({pad.path.name for pad in pads}), 16)
                self.assertTrue(all(Path(directory).name in pad.path.name for pad in pads))
                self.assertTrue(all(item["reuse_hwnd"] is None for item in starts))
                self.assertEqual([item["reuse_windows"] for item in starts], [{}] + [{42: 7}] * 15)
                self.assertTrue(all(item["owned_tab"] for item in starts))
                self.assertFalse(any(event.startswith("close ") for event in events))
                self.assertEqual(ctx.close_completed_notepads(), [])
                self.assertEqual([event for event in events if event.startswith("close ")],
                                 [f"close {pad.path.name}" for pad in reversed(pads)])
                close_window.assert_not_called()
            self.assertEqual(restorer.mark(), baseline)

    def test_retained_notepad_rejects_foreign_reuse_and_reports_failed_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = Context(build=None, session=SimpleNamespace(base_config={"privacy": {}}), fixtures={},
                          goldens=None, restorer=restore.Restorer(log=lambda _: None),
                          guard=SimpleNamespace(check=lambda: None), out=directory, work=directory,
                          hotkey="", cache=directory)
            with mock.patch.object(targets.Notepad, "start", side_effect=RuntimeError("creation failed")):
                with mock.patch.object(win32, "release_all"):
                    results, aborted = run.run_checks(ctx, catalog.select("H1,H8"), 1,
                                                       lambda _: lambda context: context.notepad(),
                                                       log=lambda _: None)
            self.assertEqual(results[0].status, report.FAIL)
            self.assertIn("creation failed", results[0].reason)
            self.assertEqual(results[1].status, report.ABORTED)
            self.assertIn("target creation failure", aborted)
            self.assertEqual(len(ctx._completed_notepads), 1)
            # A failed spawn has no verified HWND. It must not activate or close an unknown tab.
            with mock.patch.object(win32, "activate") as activate, mock.patch.object(win32, "tap") as tap:
                self.assertEqual(ctx.close_completed_notepads(), [])
                activate.assert_not_called()
                tap.assert_not_called()
            ctx.begin("H8")
            with mock.patch.object(targets.Notepad, "start") as start:
                with self.assertRaisesRegex(Fail, "not a completed fixture"):
                    ctx.notepad("alt", reuse_hwnd=99, owned_tab=True)
                start.assert_not_called()

    def test_guard_abort_prevents_retained_notepad_cleanup_input(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            ctx = Context(build=None, session=SimpleNamespace(base_config={"privacy": {}}), fixtures={},
                          goldens=None, restorer=restore.Restorer(log=lambda _: None),
                          guard=SimpleNamespace(check=lambda: None), out=directory, work=directory,
                          hotkey="", cache=directory)

            def start(pad, **_):
                pad.hwnd, pad.owner_pid = 42, 7
                return pad

            def check(context):
                context.notepad()
                raise guard.GuardUnavailable("probe 17 missed")

            with mock.patch.object(targets.Notepad, "start", autospec=True, side_effect=start), \
                 mock.patch.object(targets.Notepad, "close_owned", autospec=True,
                                   side_effect=lambda pad, **_: events.append(pad.path.name)), \
                 mock.patch.object(win32, "release_all"):
                results, aborted = run.run_checks(ctx, catalog.select("H1,H8"), 1,
                                                   lambda _: check, log=lambda _: None)
                self.assertEqual([result.status for result in results],
                                 [report.ABORTED, report.ABORTED])
                self.assertIn("input guard unavailable", aborted)
                self.assertEqual(events, [])
                ctx.guard.check = mock.Mock(side_effect=guard.GuardUnavailable("probe 17 missed"))
                failures = ctx.close_completed_notepads()
                self.assertEqual(len(failures), 1)
                self.assertIn("GuardUnavailable", failures[0][1])
                self.assertEqual(events, [])

    def test_guard_trip_during_owned_tab_cleanup_stops_next_input(self):
        for phase in ("activation", "navigation", "save"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                pad = targets.Notepad(directory, "H1-doc")
                pad.hwnd, pad.owner_pid, pad.owned_tab = 42, 7, True
                pad.path.write_text("owned", encoding="utf-8")
                state = {"tripped": False}
                actions = []

                def check():
                    if state["tripped"]:
                        raise guard.GuardUnavailable("probe missed during cleanup")

                def activate(_):
                    if phase == "activation":
                        state["tripped"] = True
                    return True

                def tap(*keys):
                    actions.append(keys)
                    state["tripped"] = True

                pad.win32 = SimpleNamespace(
                    is_window=lambda _: True, window_pid=lambda _: 7, pids=lambda _: [7],
                    foreground=lambda: 42, activate=activate, tap=tap,
                    window_text=lambda _: ("unrelated.txt" if phase == "navigation" else pad.path.name)
                    + " - Notepad", wait_for=lambda predicate, _: predicate())
                with self.assertRaisesRegex(guard.GuardUnavailable, "during cleanup"):
                    pad.close_owned(guard_check=check)
                self.assertEqual(actions, [] if phase == "activation" else
                                 [("ctrl", "tab")] if phase == "navigation" else [("ctrl", "s")])
                self.assertEqual(pad.hwnd, 42)

    def test_notepad_h8_cleanup_closes_only_verified_tab(self):
        with tempfile.TemporaryDirectory() as directory:
            unrelated = Path(directory) / "unrelated.txt"
            unrelated.write_text("keep me", encoding="utf-8")
            pad = targets.Notepad(directory, "H8-alt")
            pad.hwnd, pad.owner_pid, pad.owned_tab = 42, 7, True
            pad.path.write_text("settled text", encoding="utf-8")
            # Restored tabs can outnumber the old eight-tab search limit and can have
            # names from previous H8 runs. Only this run's unique title is closable.
            titles = (["unrelated.txt - Notepad", "notepad-H8-alt-old-run.txt - Notepad"] +
                      [f"other-{index}.txt - Notepad" for index in range(9)] +
                      [f"{pad.path.name} - Notepad"])
            state = {"tab": 0, "open": True}
            actions = []

            def tap(*keys):
                actions.append(keys)
                if keys == ("ctrl", "tab"):
                    state["tab"] = (state["tab"] + 1) % len(titles)
                elif keys == ("ctrl", "w"):
                    state["open"] = False

            pad.win32 = SimpleNamespace(is_window=lambda _: state["open"],
                                        window_text=lambda _: titles[state["tab"]],
                                        window_pid=lambda _: 7,
                                        pids=lambda _: [7],
                                        activate=lambda _: True, foreground=lambda: 42, tap=tap,
                                        wait_for=lambda predicate, _: predicate())
            pad.close_owned()
            self.assertEqual(actions, [("ctrl", "tab")] * (len(titles) - 1) +
                             [("ctrl", "s"), ("ctrl", "w")])
            self.assertEqual(pad.hwnd, 0)
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me")

            pad.hwnd, state["open"], state["tab"] = 42, True, 0
            titles[-1] = "another.txt - Notepad"
            before = len(actions)
            pad.win32.tap = lambda *keys: actions.append(keys)
            with self.assertRaisesRegex(RuntimeError, "Could not locate owned Notepad tab"):
                pad.close_owned()
            self.assertNotIn(("ctrl", "w"), actions[before:])
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me")

            pad.win32.foreground = lambda: 99
            before = len(actions)
            with self.assertRaisesRegex(RuntimeError, "lost foreground"):
                pad.close_owned()
            self.assertEqual(len(actions), before)
            pad.win32.foreground = lambda: 42

            pad.document = mock.Mock()
            with self.assertRaisesRegex(RuntimeError, "active tab is not"):
                pad.focus()
            pad.document.assert_not_called()

            pad.win32.window_pid = lambda _: 8
            pad.win32.activate = mock.Mock()
            with self.assertRaisesRegex(RuntimeError, "window ownership changed"):
                pad.close_owned()
            pad.win32.activate.assert_not_called()
            pad.win32.is_window = lambda _: False
            with self.assertRaisesRegex(RuntimeError, "window disappeared"):
                pad.close_owned()
            pad.win32.activate.assert_not_called()

    def test_dirty_retained_h1_is_saved_before_close_and_save_failure_never_closes(self):
        for outcome in ("saved", "pending", "ownership_changed"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                pad = targets.Notepad(directory, "H1-doc")
                pad.hwnd, pad.owner_pid, pad.owned_tab = 42, 7, True
                pad.path.write_text("", encoding="utf-8")
                state = {"dirty": True, "open": True, "foreground": 42}
                actions = []

                def tap(*keys):
                    actions.append(keys)
                    if keys == ("ctrl", "s") and outcome == "saved":
                        pad.path.write_text("exact completed output", encoding="utf-8")
                        state["dirty"] = False
                    elif keys == ("ctrl", "s") and outcome == "ownership_changed":
                        state["dirty"] = False
                        state["foreground"] = 99
                    elif keys == ("ctrl", "w"):
                        self.assertFalse(state["dirty"], "dirty tab would open a save prompt")
                        self.assertEqual(pad.path.read_text(encoding="utf-8"), "exact completed output")
                        state["open"] = False

                pad.win32 = SimpleNamespace(
                    is_window=lambda _: state["open"], window_pid=lambda _: 7, pids=lambda _: [7],
                    window_text=lambda _: ("*" if state["dirty"] else "") + pad.path.name + " - Notepad",
                    foreground=lambda: state["foreground"], activate=lambda _: True, tap=tap,
                    wait_for=lambda predicate, _: predicate())
                if outcome == "saved":
                    pad.close_owned()
                    self.assertEqual(actions, [("ctrl", "s"), ("ctrl", "w")])
                else:
                    with self.assertRaisesRegex(RuntimeError, "save or ownership verification failed"):
                        pad.close_owned()
                    self.assertEqual(actions, [("ctrl", "s")])
                    self.assertEqual(pad.hwnd, 42)

    def test_notepad_h8_direct_comparison_clears_only_active_owned_tab(self):
        with tempfile.TemporaryDirectory() as directory:
            pad = targets.Notepad(directory, "H8-no-alt")
            pad.hwnd, pad.owner_pid, pad.owned_tab = 42, 7, True
            state = {"title": "other.txt - Notepad", "text": "keep"}
            keys = []

            def tap(*chord):
                keys.append(chord)
                if chord == ("delete",):
                    state["text"] = ""

            pad.win32 = SimpleNamespace(is_window=lambda _: True,
                                        window_text=lambda _: state["title"],
                                        window_pid=lambda _: 7, tap=tap,
                                        wait_for=lambda predicate, _: predicate())
            pad.focus = mock.Mock()
            pad.text = lambda: state["text"]
            with self.assertRaisesRegex(RuntimeError, "not owned"):
                pad.clear_owned()
            pad.focus.assert_not_called()
            self.assertEqual(keys, [])
            self.assertEqual(state["text"], "keep")

            state["title"] = f"{pad.path.name} - Notepad"
            pad.clear_owned()
            self.assertEqual(keys, [("ctrl", "a"), ("delete",)])
            self.assertEqual(state["text"], "")

    def test_overlay_focus_check_refuses_to_blame_click_when_editor_already_lost_focus(self):
        from checks import overlay as checks_overlay
        with tempfile.TemporaryDirectory() as directory:
            pad = SimpleNamespace(hwnd=42, focus_diagnostic=lambda: "UIA focus: MenuItem 'File'\n",
                                  owns_keyboard_focus=lambda: False)
            def require(condition, reason):
                if not condition:
                    raise Fail(reason)
            ctx = SimpleNamespace(murmur=SimpleNamespace(ensure=lambda *_: None),
                                  fixtures={"A": SimpleNamespace(path="fixture.wav")},
                                  clipboard_guard=lambda: None, target=lambda *_: object(),
                                  artifact=lambda suffix: Path(directory) / suffix,
                                  require=require)
            with mock.patch.object(checks_overlay, "_notepad_with_word", return_value=pad), \
                 mock.patch.object(checks_overlay, "produce_held",
                                   side_effect=lambda *_a, after_focus=None: after_focus()), \
                 mock.patch.object(checks_overlay, "click_overlay") as click, \
                 mock.patch.object(win32, "foreground", return_value=42):
                with self.assertRaisesRegex(Fail, "before the overlay click"):
                    checks_overlay.o3_buttons_keep_focus(ctx)
            click.assert_not_called()
            evidence = (Path(directory) / "discard-focus-diagnostic.txt").read_text()
            self.assertIn("after focus_while_held", evidence)
            self.assertIn("held before overlay click", evidence)
            self.assertIn("MenuItem 'File'", evidence)

    def test_overlay_drag_uses_exposed_handle_bounds_and_rejects_outside(self):
        from checks import common
        ctx = SimpleNamespace(murmur=SimpleNamespace(overlay=lambda: object(), overlay_hwnd=lambda: 42),
                              sleep=lambda _: None)
        with mock.patch.object(common.uia, "find", return_value="handle"), \
             mock.patch.object(common.uia, "center", side_effect=[(120, 130), (120, 130), (250, 130)]), \
             mock.patch.object(common.uia, "rect", return_value=(110, 120, 130, 140)), \
             mock.patch.object(win32, "window_rect", return_value=(100, 100, 200, 200)):
            self.assertEqual(common.overlay_drag_point(ctx), (120, 130))
            self.assertEqual(common.overlay_drag_point(ctx, return_bounds=True),
                             ((120, 130), (110, 120, 130, 140)))
            with self.assertRaisesRegex(Fail, "outside overlay"):
                common.overlay_drag_point(ctx)

    def test_microphone_page_waits_for_content_after_window_appears(self):
        with tempfile.TemporaryDirectory() as directory:
            app = SimpleNamespace(ensure=mock.Mock(), page=mock.Mock(), settings=lambda: "murmur-settings",
                                  settings_hwnd=lambda: 1)
            ctx = SimpleNamespace(murmur=app, fixtures={"A": SimpleNamespace(path="a.wav")},
                                  restorer=object(), sleep=lambda _: None, screenshot=mock.Mock(),
                                  artifact=lambda name: Path(directory) / name)
            with mock.patch.object(microphone_checks, "winreg", SimpleNamespace(REG_SZ=1)), \
                 mock.patch.object(win32, "Registry"), \
                 mock.patch.object(microphone_checks.RegistryValue, "set"), \
                 mock.patch.object(microphone_checks, "_windows_settings_windows", side_effect=[[], [42]]), \
                 mock.patch.object(microphone_checks.uia, "find", return_value="button"), \
                 mock.patch.object(microphone_checks.uia, "invoke"), \
                 mock.patch.object(microphone_checks.uia, "from_handle", return_value="windows-settings"), \
                 mock.patch.object(microphone_checks.uia, "names", side_effect=[
                     [microphone_checks.EXPLANATION], [], ["Privacy & security", "Microphone"]]), \
                 mock.patch.object(win32, "wait_for", side_effect=lambda predicate, *_a, **_kw: predicate()), \
                 mock.patch.object(win32, "close_window") as close:
                microphone_checks.m1_desktop_apps_off(ctx)
            self.assertIn("Microphone", (Path(directory) / "windows-settings-names.txt").read_text())
            close.assert_called_once_with(42)

    def test_windows_modules_import_on_any_os(self):
        import capture  # noqa: F401
        import context  # noqa: F401
        import murmur  # noqa: F401
        import targets  # noqa: F401
        import uia  # noqa: F401
        self.assertEqual(win32.parse_combo("Ctrl+Alt+Shift+F9"), (["ctrl", "alt", "shift"], "f9"))
        with self.assertRaises(ValueError):
            win32.parse_combo("Ctrl+Alt")
        with self.assertRaises(ValueError):
            win32.parse_combo("Ctrl+Hyper+X")

    def test_guard_injected_flags(self):
        self.assertTrue(guard.is_injected_key(0x10))
        self.assertFalse(guard.is_injected_key(0x00))
        self.assertTrue(guard.is_injected_mouse(0x01))
        self.assertFalse(guard.is_injected_mouse(0x00))

    def test_uia_selectors_reject_title_bar_and_non_editor_text(self):
        label, switch, minimize, style, document = object(), object(), object(), object(), object()
        elements = [label, switch, minimize, style, document]
        names = {label: "Start at login", switch: "Start at login", minimize: "Minimize",
                 style: "Title", document: "Text Editor"}
        types = {label: uia.TEXT, switch: uia.BUTTON, minimize: uia.BUTTON,
                 style: uia.EDIT, document: uia.DOCUMENT}
        patterns = {(switch, uia.TOGGLE), (style, uia.TEXTPATTERN), (document, uia.TEXTPATTERN)}
        with mock.patch.object(uia, "descendants", return_value=elements), \
             mock.patch.object(uia, "name", side_effect=names.get), \
             mock.patch.object(uia, "control_type", side_effect=types.get), \
             mock.patch.object(uia, "has_pattern", side_effect=lambda e, p: (e, p) in patterns):
            self.assertIs(uia.named_toggle(object(), "Start at login", timeout=0), switch)
            with self.assertRaises(uia.NotFound):
                uia.named_toggle(object(), "Minimize", timeout=0)
            self.assertIs(uia.document_element(object(), timeout=0), document)

    def test_component_verify_note_is_scoped_to_its_row(self):
        names = ["Speech engine", "Speech engine", "Files verified.", "Verify Speech engine",
                 "Remove Speech engine", "Speech models", "Tiny (English)",
                 "Verify Tiny (English)", "Remove Tiny (English)"]
        self.assertTrue(setup_checks._component_verified(names, "Speech engine"))
        self.assertFalse(setup_checks._component_verified(names, "Tiny (English)"))
        names.insert(names.index("Verify Tiny (English)"), "Files verified.")
        self.assertTrue(setup_checks._component_verified(names, "Tiny (English)"))


class DesktopFollowupTests(unittest.TestCase):
    C3_COMPONENT = {"version": "v1", "archive": "zip", "files": []}

    def _c3_case(self, directory, session):
        return SimpleNamespace(build=SimpleNamespace(component=lambda _id: self.C3_COMPONENT),
                               work=directory, guard=object(),
                               murmur=SimpleNamespace(base_config={}, running=lambda: False),
                               fixtures={"A": SimpleNamespace(path="a.wav")},
                               restorer=SimpleNamespace(push=mock.Mock()), sleep=lambda _: None,
                               screenshot=mock.Mock(), artifact=lambda suffix: Path(directory) / suffix,
                               notepad_ownership_snapshot=lambda: {}, note=mock.Mock())

    def test_c3_paused_bytes_match_exact_job_and_partial_file(self):
        from checks import components
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "components"
            (root / "downloads").mkdir(parents=True)
            part = root / "downloads" / "speech-small.en-v1.part"
            part.write_bytes(b"a" * 41)
            jobs = root / "jobs.json"
            record = {"id": components.MODEL_ID, "version": "v1", "phase": "paused", "bytes_done": 41}
            session = SimpleNamespace(data=Path(directory))
            def save():
                jobs.write_text(json.dumps({"jobs": {components.MODEL_ID: record}}))
            save()
            self.assertEqual(components._paused_bytes(session, self.C3_COMPONENT), 41)
            part.write_bytes(b"a" * 42)
            with self.assertRaisesRegex(Fail, "inconsistent"):
                components._paused_bytes(session, self.C3_COMPONENT)
            record["bytes_done"] = 42
            save()
            self.assertEqual(components._paused_bytes(session, self.C3_COMPONENT), 42)
            part.write_bytes(b"")
            with self.assertRaisesRegex(Fail, "inconsistent"):
                components._paused_bytes(session, self.C3_COMPONENT)
            record["version"] = "other"
            save()
            with self.assertRaisesRegex(Fail, "identity differs"):
                components._download_state(session, self.C3_COMPONENT)

    def test_o3_focus_diagnostic_records_root_pid_and_native_editor(self):
        with tempfile.TemporaryDirectory() as directory:
            pad = targets.Notepad(directory, "O3-doc")
            pad.path.write_text("", encoding="utf-8")
            pad.hwnd, pad.owner_pid, pad.owned_tab = 42, 7, True
            pad.uia = SimpleNamespace(focused=lambda: object(), describe=lambda _: "Document 'Text editor'")
            pad.win32 = SimpleNamespace(
                foreground=lambda: 99,
                foreground_context=lambda: {"active_hwnd": 99, "focus_hwnd": 101},
                user32=SimpleNamespace(GetAncestor=lambda _h, _mode: 42), GA_ROOT=2,
                window_pid=lambda _h: 7, class_name=lambda h: "RichEditD2DPT" if h == 101 else "Notepad",
                window_text=lambda _h: "", gui_thread_focus=lambda _h: {"focus_hwnd": "0x65"})
            pad.active_document = lambda: True
            pad.editor_mode_label = lambda: "Plain text"
            pad.menu_open = lambda: False
            detail = pad.focus_diagnostic()
            self.assertIn("expected owner pid: 7; active fixture: True", detail)
            self.assertIn("'hwnd': '0x63', 'root': '0x2a', 'pid': 7", detail)
            self.assertIn("'hwnd': '0x65', 'root': '0x2a'", detail)
            self.assertIn("RichEditD2DPT", detail)

    def test_o2_does_not_drag_without_qa_dom_probe(self):
        from checks import overlay
        with tempfile.TemporaryDirectory() as directory:
            session = SimpleNamespace(ensure=lambda *_a: None, log=lambda **_k: "")
            ctx = SimpleNamespace(murmur=session, fixtures={"A": SimpleNamespace(path="a.wav")},
                                  restorer=SimpleNamespace(push=mock.Mock()),
                                  artifact=lambda suffix: Path(directory) / suffix, sleep=lambda _: None)
            with mock.patch.object(overlay.win32, "wait_for", return_value=None), \
                 mock.patch.object(overlay.win32, "drag") as drag:
                with self.assertRaisesRegex(Fail, "did not arm"):
                    overlay.o2_drag(ctx)
            drag.assert_not_called()
            self.assertIn("did not arm", (Path(directory) / "drag-diagnostic.txt").read_text())

    def test_o2_requires_armed_listener_then_settled_visible_icon(self):
        from checks import overlay
        stage0 = "QA overlay drag probe stage=0 idle_icon=false"
        armed = "QA overlay drag probe stage=4 idle_icon=false"
        settled = "QA overlay drag probe stage=5 idle_icon=true pill_hidden=false"
        sized = "QA overlay drag probe stage=6 idle_icon=true target_kind=3 hit_pill=true"
        self.assertFalse(overlay._qa_drag_ready(stage0))
        self.assertFalse(overlay._qa_drag_ready(stage0 + "\n" + settled))
        self.assertFalse(overlay._qa_drag_ready(stage0 + "\n" + armed))
        self.assertFalse(overlay._qa_drag_ready(settled + "\n" + armed))
        self.assertFalse(overlay._qa_drag_ready(armed + "\n" + settled.replace("false", "true")))
        self.assertFalse(overlay._qa_drag_ready(armed + "\n" + settled.replace("idle_icon=true", "idle_icon=false")))
        self.assertFalse(overlay._qa_drag_ready(stage0 + "\n" + armed + "\n" + settled))
        self.assertFalse(overlay._qa_drag_ready(stage0 + "\n" + armed + "\n" + sized + "\n" + settled))
        self.assertTrue(overlay._qa_drag_ready(stage0 + "\n" + armed + "\n" + settled + "\n" + sized))

    def test_o2_failed_gesture_records_fresh_client_mapping_without_retry(self):
        from checks import overlay
        stages = "\n".join(f"QA overlay drag probe stage={stage} idle_icon=true pill_hidden=false"
                           for stage in (4, 5, 6))
        session = SimpleNamespace(ensure=mock.Mock(), log=lambda **_k: stages,
                                  overlay_hwnd=lambda: 42, overlay_names=lambda: [],
                                  restart=mock.Mock())
        with tempfile.TemporaryDirectory() as directory:
            ctx = SimpleNamespace(murmur=session, fixtures={"A": SimpleNamespace(path="a.wav")},
                                  restorer=SimpleNamespace(push=mock.Mock()),
                                  artifact=lambda suffix: Path(directory) / suffix,
                                  screenshot=lambda *_: None, sleep=lambda _: None)
            user32 = SimpleNamespace(WindowFromPoint=lambda _point: 77,
                                     GetAncestor=lambda _hwnd, _kind: 42,
                                     GetAsyncKeyState=lambda _key: 0)
            with mock.patch.object(overlay, "overlay_drag_point", return_value=((960, 984), (940, 970, 980, 998))), \
                 mock.patch.object(win32, "wait_for", return_value=True), \
                 mock.patch.object(win32, "window_rect", return_value=(934, 964, 986, 1004)), \
                 mock.patch.object(win32, "outer_rect", return_value=(934, 964, 986, 1004)), \
                 mock.patch.object(win32, "client_origin", return_value=(934, 964)), \
                 mock.patch.object(win32, "client_size", return_value=(52, 40)), \
                 mock.patch.object(win32, "dpi_scale", return_value=1), \
                 mock.patch.object(win32, "user32", user32), \
                 mock.patch.object(win32, "class_name", return_value="Chrome_RenderWidgetHostHWND"), \
                 mock.patch.object(win32, "window_pid", return_value=123), \
                 mock.patch.object(win32, "cursor", return_value=(960, 984)), \
                 mock.patch.object(win32, "foreground", return_value=0), \
                 mock.patch.object(win32, "window_text", return_value=""), \
                 mock.patch.object(win32, "drag") as drag, \
                 mock.patch.object(win32, "mouse_up"):
                with self.assertRaisesRegex(Fail, "did not move"):
                    overlay.o2_drag(ctx)
            drag.assert_called_once()
            session.restart.assert_not_called()
            evidence = (Path(directory) / "drag-diagnostic.txt").read_text()
            self.assertIn("gesture-time UIA drag bounds: (940, 970, 980, 998)", evidence)
            self.assertIn("click relative to overlay/hit client (physical px): (26, 20); (26, 20)", evidence)
            self.assertIn("stage=6", evidence)


    def test_h4_failure_records_exact_fixture_boundary_and_app_qa_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.wav"
            with wave.open(str(path), "wb") as writer:
                writer.setparams((1, 2, fixtures.RATE, 0, "NONE", "not compressed"))
                writer.writeframes(b"\0\0" * (16 + int(fixtures.PAD_SECONDS * fixtures.RATE)))
            app = SimpleNamespace(focus=lambda: None, wait_text=lambda: "missing dog")
            session = SimpleNamespace(ensure=lambda *_a: None, log=lambda **_k: "QA capture stopped")
            ctx = SimpleNamespace(murmur=session, fixtures={"A": SimpleNamespace(path=path, speech_seconds=.001)},
                                  target=lambda _name: app, dictate=lambda *_a, **_k: 123.5,
                                  settle=lambda: None, expect_delivery=mock.Mock(side_effect=Fail("missing dog")),
                                  artifact=lambda suffix: Path(directory) / suffix)
            with self.assertRaisesRegex(Fail, "missing dog"):
                hotkey_checks.h4_toggle(ctx)
            record = json.loads((Path(directory) / "toggle-qa-diagnostic.json").read_text())
            self.assertEqual(record["unpadded_speech_samples"], 16)
            self.assertEqual(record["second_toggle_epoch_s"], 123.5)
            self.assertEqual((Path(directory) / "toggle-app-log.txt").read_text(), "QA capture stopped")
            self.assertNotIn(str(path), json.dumps(record))

    def test_c3_restart_resume_requires_advance_or_verified_install(self):
        from checks import components
        session = SimpleNamespace(settings=lambda: object(), installed=mock.Mock(return_value=False))
        with mock.patch.object(components, "_download_state", return_value=("downloading", 42, 42)) as state, \
             mock.patch.object(components, "badge", return_value="Paused"):
            self.assertIsNone(components._resumed_after_restart(session, self.C3_COMPONENT, 42, object()))
            state.return_value = ("downloading", 43, 42)
            self.assertEqual(components._resumed_after_restart(session, self.C3_COMPONENT, 42, object()),
                             ("advanced", 43))
            state.return_value = ("downloading", 0, 0)
            self.assertIsNone(components._resumed_after_restart(session, self.C3_COMPONENT, 42, object()))
            state.return_value = ("paused", 43, 43)
            self.assertIsNone(components._resumed_after_restart(session, self.C3_COMPONENT, 42, object()))
        with mock.patch.object(components, "_download_state", return_value=("paused", 42, 42)) as state, \
             mock.patch.object(components, "badge", return_value="Installed"):
            self.assertIsNone(components._resumed_after_restart(session, self.C3_COMPONENT, 42, object()))
            session.installed.return_value = True
            state.reset_mock()
            self.assertEqual(components._resumed_after_restart(session, self.C3_COMPONENT, 42, object()),
                             ("completed", None))
            state.assert_not_called()

    def test_c3_stalled_after_restart_fails_and_records_precise_progress(self):
        from checks import components
        with tempfile.TemporaryDirectory() as directory:
            session = mock.Mock()
            session.page.return_value = object()
            session.settings.return_value = object()
            session.installed.return_value = False
            ctx = self._c3_case(directory, session)
            states = iter([("downloading", 41, 41), ("paused", 41, 41),
                           ("downloading", 42, 41), ("paused", 42, 42),
                           ("paused", 42, 42), ("paused", 42, 42),
                           ("downloading", 42, 42)])
            with mock.patch.object(components, "MurmurSession", return_value=session), \
                 mock.patch.object(components.tomlconfig, "merge", return_value={}), \
                 mock.patch.object(components.uia, "find", side_effect=lambda _, name_, **__: name_), \
                 mock.patch.object(components.uia, "invoke"), \
                 mock.patch.object(components, "badge", return_value="Paused"), \
                 mock.patch.object(components, "_download_state", side_effect=lambda *_a, **_k: next(states)), \
                 mock.patch.object(components.win32, "wait_for", side_effect=lambda predicate, *_a, **_k: predicate()):
                with self.assertRaisesRegex(Fail, "stalled at 42"):
                    components.c3_pause_resume(ctx)
            self.assertEqual(session.stop.call_count, 1)
            self.assertIn("before_c3_stop", (Path(directory) / "notepad-lifecycle.json").read_text())

    def test_c3_second_pause_must_succeed_before_restart(self):
        from checks import components
        session = mock.Mock()
        session.settings.return_value = object()
        ctx = self._c3_case("work", session)
        with mock.patch.object(components, "MurmurSession", return_value=session), \
             mock.patch.object(components.tomlconfig, "merge", return_value={}), \
             mock.patch.object(components.uia, "find", side_effect=lambda _, name_, **__: name_), \
             mock.patch.object(components.uia, "invoke"), \
             mock.patch.object(components, "badge", side_effect=["Paused", None]), \
             mock.patch.object(components, "_download_state", side_effect=[("downloading", 41, 41),
                                                                      ("paused", 41, 41),
                                                                      ("downloading", 42, 41)]), \
             mock.patch.object(components.win32, "wait_for", side_effect=lambda predicate, *_a, **_k: predicate()):
            with self.assertRaisesRegex(Fail, "second Pause"):
                components.c3_pause_resume(ctx)
        session.stop.assert_not_called()

    def test_c3_after_restart_accepts_advance_or_verified_completion(self):
        from checks import components
        for completion in (False, True):
            with self.subTest(completion=completion), tempfile.TemporaryDirectory() as directory:
                session = mock.Mock()
                session.settings.return_value = object()
                session.installed.return_value = completion
                notes = []
                ctx = self._c3_case(directory, session)
                ctx.note = notes.append
                states = iter([("downloading", 41, 41), ("paused", 41, 41),
                               ("downloading", 42, 41), ("paused", 42, 42),
                               ("paused", 42, 42), ("paused", 42, 42),
                               ("downloading", 43, 42)])
                badges = iter(["Paused", "Paused", "Paused", "Paused", "Installed"])
                with mock.patch.object(components, "MurmurSession", return_value=session), \
                     mock.patch.object(components.tomlconfig, "merge", return_value={}), \
                     mock.patch.object(components.uia, "find", side_effect=lambda _, name_, **__: name_), \
                     mock.patch.object(components.uia, "invoke"), \
                     mock.patch.object(components, "badge", side_effect=lambda *_a, **_k: next(badges)), \
                     mock.patch.object(components, "_download_state", side_effect=lambda *_a, **_k: next(states)), \
                     mock.patch.object(components.win32, "wait_for", side_effect=lambda predicate, *_a, **_k: predicate()):
                    components.c3_pause_resume(ctx)
                self.assertIn("after restart Resume", notes[0])
                self.assertIn("completed" if completion else "advanced", notes[0])
                self.assertEqual(session.stop.call_count, 2)

    def test_refused_notepad_activation_leaves_owned_and_unknown_tabs_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            pad = targets.Notepad(directory, "H1-doc")
            pad.hwnd, pad.owner_pid, pad.owned_tab = 42, 7, True
            pad.path.write_text("owned", encoding="utf-8")
            keys = mock.Mock()
            pad.win32 = SimpleNamespace(is_window=lambda _: True, window_pid=lambda _: 7,
                                        pids=lambda _: [7], activate=lambda _: False,
                                        tap=keys, window_text=lambda _: "unknown.txt - Notepad")
            with self.assertRaisesRegex(RuntimeError, "Could not activate"):
                pad.close_owned()
            keys.assert_not_called()
            self.assertEqual(pad.hwnd, 42)

    def test_notepad_snapshot_records_foreground_and_current_owner_without_input(self):
        ctx = Context.__new__(Context)
        pad = SimpleNamespace(path=Path("owned.txt"), hwnd=42, owner_pid=7, owned_tab=True,
                              win32=SimpleNamespace(is_window=lambda _: True, window_pid=lambda _: 8,
                                                    window_text=lambda _: "unknown.txt - Notepad"))
        ctx._completed_notepads = [pad]
        with mock.patch.object(win32, "foreground_context", return_value={"hwnd": 99, "process_id": 9}), \
             mock.patch.object(win32, "activate") as activate, mock.patch.object(win32, "tap") as tap:
            snapshot = ctx.notepad_ownership_snapshot()
        self.assertEqual(snapshot["foreground"], {"hwnd": 99, "process_id": 9})
        self.assertEqual(snapshot["notepads"][0]["current_pid"], 8)
        activate.assert_not_called()
        tap.assert_not_called()

    def test_tray_uses_open_overflow_without_clicking_and_opens_closed_one_once(self):
        from checks import tray
        ctx = SimpleNamespace(sleep=lambda _: None)
        icon, chevron = object(), object()
        state = {"open": True, "clicks": 0}

        def windows(*, class_):
            return [11] if class_ == "Shell_TrayWnd" else ([22] if state["open"] else [])

        def find(root, **criteria):
            if root == "bar":
                if re.search(criteria.get("name_re", ""), "Murmur Settings - 1 running window"):
                    return "settings taskbar button"
                return chevron if "hidden icons" in criteria.get("name_re", "") else None
            return icon if root == "overflow" else None

        def click(*_):
            state["clicks"] += 1
            state["open"] = True

        with mock.patch.object(tray.win32, "find_windows", side_effect=windows), \
             mock.patch.object(tray.win32, "click", side_effect=click), \
             mock.patch.object(tray.win32, "wait_for", side_effect=lambda predicate, *_a, **_k: predicate()), \
             mock.patch.object(tray.uia, "from_handle", side_effect=lambda hwnd: "bar" if hwnd == 11 else "overflow"), \
             mock.patch.object(tray.uia, "try_find", side_effect=find), \
             mock.patch.object(tray.uia, "center", return_value=(1, 2)):
            self.assertIs(tray._tray_button(ctx), icon)
            self.assertEqual(state["clicks"], 0)
            state["open"] = False
            self.assertIs(tray._tray_button(ctx), icon)
            self.assertEqual(state["clicks"], 1)

    def test_tray_icon_name_matches_app_tooltip_only(self):
        from checks import tray
        self.assertTrue(re.search(tray.TRAY_ICON_NAME, "Murmur — no model"))
        self.assertTrue(re.search(tray.TRAY_ICON_NAME, "Murmur — Ready"))
        self.assertFalse(re.search(tray.TRAY_ICON_NAME, "Murmur Settings - 1 running window"))
        self.assertFalse(re.search(tray.TRAY_ICON_NAME, "Murmur - 1 running window"))

    def test_tray_menu_ignores_existing_and_foreign_menus(self):
        from checks import tray
        with tempfile.TemporaryDirectory() as directory:
            ctx = SimpleNamespace(murmur=SimpleNamespace(process=SimpleNamespace(pid=7)),
                                  sleep=lambda _: None, artifact=lambda suffix: Path(directory) / suffix)
            state = {"clicked": False, "owner": 7}

            def windows(*, class_):
                if class_ == "#32768":
                    return [99, 77] if state["clicked"] else [99]
                return [11] if class_ == "Shell_TrayWnd" else []

            with mock.patch.object(tray, "_tray_button", return_value=object()), \
                 mock.patch.object(tray.win32, "find_windows", side_effect=windows), \
                 mock.patch.object(tray.win32, "window_pid", side_effect=lambda hwnd: state["owner"] if hwnd == 77 else 8), \
                 mock.patch.object(tray.win32, "click", side_effect=lambda *_a, **_k: state.update(clicked=True)), \
                 mock.patch.object(tray.win32, "wait_for", side_effect=lambda predicate, *_a, **_k: predicate()), \
                 mock.patch.object(tray.uia, "center", return_value=(1, 2)), \
             mock.patch.object(tray.uia, "rect", return_value=(0, 0, 2, 4)), \
             mock.patch.object(tray.uia, "handle", return_value=11), \
                 mock.patch.object(tray.uia, "from_handle", side_effect=lambda hwnd: f"menu-{hwnd}"), \
                 mock.patch.object(tray, "_tray_diagnostic") as diagnostic:
                self.assertEqual(tray._menu(ctx), "menu-77")
                state["clicked"], state["owner"] = False, 8
                with self.assertRaisesRegex(Fail, "newly opened tray context menu"):
                    tray._menu(ctx)
                diagnostic.assert_called_once()

    def test_tray_closed_menu_handle_reuse_is_fresh_but_visible_menu_is_not(self):
        from checks import tray
        with tempfile.TemporaryDirectory() as directory:
            ctx = SimpleNamespace(murmur=SimpleNamespace(process=SimpleNamespace(pid=7)),
                                  sleep=lambda _: None, artifact=lambda suffix: Path(directory) / suffix)
            state = {"visible_before": False, "clicked": False}
            def windows(*, class_):
                if class_ == "#32768":
                    return [77] if state["clicked"] or state["visible_before"] else []
                return [11] if class_ == "Shell_TrayWnd" else []
            with mock.patch.object(tray, "_tray_button", return_value=object()), \
                 mock.patch.object(tray, "_overflow_window", return_value=0), \
                 mock.patch.object(tray.win32, "find_windows", side_effect=windows), \
                 mock.patch.object(tray.win32, "window_pid", return_value=7), \
                 mock.patch.object(tray.win32, "click", side_effect=lambda *_a, **_k: state.update(clicked=True)), \
                 mock.patch.object(tray.win32, "wait_for", side_effect=lambda predicate, *_a, **_k: predicate()), \
                 mock.patch.object(tray.uia, "center", return_value=(1, 2)), \
                 mock.patch.object(tray.uia, "rect", return_value=(0, 0, 2, 4)), \
                 mock.patch.object(tray.uia, "handle", return_value=11), \
                 mock.patch.object(tray.uia, "from_handle", side_effect=lambda hwnd: f"menu-{hwnd}"), \
                 mock.patch.object(tray, "_tray_diagnostic") as diagnostic:
                self.assertEqual(tray._menu(ctx, "Sounds mute"), "menu-77")
                state.update(visible_before=True, clicked=False)
                with self.assertRaisesRegex(Fail, "newly opened"):
                    tray._menu(ctx, "Sounds mute")
                self.assertEqual(diagnostic.call_args.kwargs["action"]["operation"], "Sounds mute")

    def test_tray_missing_icon_skips_but_mute_failure_fails(self):
        from checks import tray
        with mock.patch.object(tray.win32, "find_windows", return_value=[]), \
             mock.patch.object(tray.win32, "click") as click:
            with self.assertRaisesRegex(Skip, "no taskbar"):
                tray._tray_button(object())
            click.assert_not_called()
        with mock.patch.object(tray.win32, "find_windows",
                               side_effect=lambda *, class_: [11] if class_ == "Shell_TrayWnd" else [22]), \
             mock.patch.object(tray.uia, "from_handle", side_effect=lambda hwnd: hwnd), \
             mock.patch.object(tray.uia, "try_find", return_value=None), \
             mock.patch.object(tray, "_tray_diagnostic") as diagnostic, \
             mock.patch.object(tray.win32, "click") as click:
            with self.assertRaisesRegex(Skip, "already open overflow"):
                tray._tray_button(SimpleNamespace(sleep=lambda _: None))
            diagnostic.assert_called_once()
            click.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text("muted = false", encoding="utf-8")
            session = SimpleNamespace(ensure=lambda *_a, **_k: None, overlay_hwnd=lambda: 42,
                                      overlay_state=lambda: "unknown", config_path=config)
            ctx = SimpleNamespace(murmur=session, fixtures={"A": SimpleNamespace(path="a.wav")},
                                  note=mock.Mock())
            with mock.patch.object(tray, "_invoke") as invoke, \
                 mock.patch.object(tray.win32, "user32", SimpleNamespace(IsWindowVisible=lambda _: False)):
                with self.assertRaisesRegex(Fail, "Sounds mute.*did not set"):
                    tray.t2_tray_menu(ctx)
            self.assertEqual(invoke.call_count, 4)


class O8ForegroundDiagnosticTests(unittest.TestCase):
    def _selector(self, ctx, candidates, *, root_hwnd=44, roots=None):
        """Mock only UIA's returned tree; run the real O8 candidate selector."""
        root = object()
        ctx.murmur.overlay = lambda: root
        ctx.murmur.overlay_hwnd = lambda: 44
        roots = roots or {}
        self.enterContext(mock.patch.object(uia, "find_all", return_value=list(candidates)))
        self.enterContext(mock.patch.object(uia, "handle", side_effect=lambda e: (
            root_hwnd if e is root else candidates[e][2])))
        self.enterContext(mock.patch.object(uia, "name", return_value="Drag to move"))
        self.enterContext(mock.patch.object(uia, "control_type", side_effect=lambda e: candidates[e][0]))
        self.enterContext(mock.patch.object(uia, "rect", side_effect=lambda e: candidates[e][1]))
        self.enterContext(mock.patch.object(win32, "user32", SimpleNamespace(
            GetAncestor=lambda hwnd, _kind: roots.get(hwnd, 44))))

    def test_o8_selector_skips_tooltip_and_hidden_handle_for_visible_pill(self):
        root = SimpleNamespace(murmur=SimpleNamespace(), guard=SimpleNamespace(check=mock.Mock()),
                               sleep=lambda _seconds: None)
        candidates = {object(): (uia.TOOLTIP, (970, 999, 1059, 1024), 0),
                      object(): (uia.IMAGE, (0, 0, 0, 0), 0),
                      object(): (uia.GROUP, (938, 968, 982, 1000), 0)}
        self._selector(root, candidates)
        details = {}
        self.assertEqual(common_checks.overlay_drag_snapshot(
            root, (934, 964, 986, 1004), details, timeout=0),
            ((960, 984), (938, 968, 982, 1000)))
        self.assertEqual(details["uia_root_hwnd"], 44)
        self.assertEqual([row["control_type"] for row in details["uia_candidates"]],
                         [uia.TOOLTIP, uia.IMAGE, uia.GROUP])
        self.assertEqual(details["uia_selected"]["bounds"], (938, 968, 982, 1000))
        self.assertEqual(details["uia_selected"]["native_root_hwnd"], 0)

    def test_o8_selector_rejects_duplicate_valid_pills_and_unknown_root(self):
        for root_hwnd, candidates, error in (
                (44, {object(): (uia.GROUP, (938, 968, 960, 1000), 0),
                      object(): (uia.GROUP, (960, 968, 982, 1000), 0)}, "ambiguous"),
                (0, {object(): (uia.GROUP, (938, 968, 982, 1000), 0)}, "not the owned overlay")):
            with self.subTest(error=error):
                ctx = SimpleNamespace(murmur=SimpleNamespace(), guard=SimpleNamespace(check=mock.Mock()),
                                      sleep=lambda _seconds: None)
                self._selector(ctx, candidates, root_hwnd=root_hwnd)
                with self.assertRaisesRegex(Fail, error):
                    common_checks.overlay_drag_snapshot(ctx, (934, 964, 986, 1004), {}, timeout=0)

    def test_o8_selector_accepts_native_statusbar_after_tooltip(self):
        ctx = SimpleNamespace(murmur=SimpleNamespace(), guard=SimpleNamespace(check=mock.Mock()),
                              sleep=lambda _seconds: None)
        # Exact control types and rectangles captured by run 43984316462.
        self._selector(ctx, {object(): (50022, (970, 999, 1059, 1024), 0),
                             object(): (50017, (938, 968, 982, 1000), 0)})
        details = {}
        self.assertEqual(common_checks.overlay_drag_snapshot(
            ctx, (934, 964, 986, 1004), details, timeout=0),
            ((960, 984), (938, 968, 982, 1000)))
        self.assertEqual(details["uia_selected"]["control_type"], 50017)

    def test_o8_selector_rejects_mixed_statusbar_group_ambiguity(self):
        ctx = SimpleNamespace(murmur=SimpleNamespace(), guard=SimpleNamespace(check=mock.Mock()),
                              sleep=lambda _seconds: None)
        self._selector(ctx, {object(): (50017, (938, 968, 982, 1000), 0),
                             object(): (uia.GROUP, (938, 968, 982, 1000), 0)})
        with self.assertRaisesRegex(Fail, "ambiguous"):
            common_checks.overlay_drag_snapshot(ctx, (934, 964, 986, 1004), {}, timeout=0)

    def test_o8_selector_records_unreadable_stale_bounds(self):
        ctx = SimpleNamespace(murmur=SimpleNamespace(), guard=SimpleNamespace(check=mock.Mock()),
                              sleep=lambda _seconds: None)
        self._selector(ctx, {object(): (uia.GROUP, (938, 968, 982, 1000), 0)})
        details = {}
        with mock.patch.object(uia, "rect", side_effect=RuntimeError("stale element")):
            with self.assertRaises(uia.NotFound):
                common_checks.overlay_drag_snapshot(ctx, (934, 964, 986, 1004), details, timeout=0)
        self.assertIn("stale element", details["uia_candidates"][0]["bounds_error"])
        self.assertEqual(details["uia_candidates"][0]["rejections"], ["bounds unreadable"])

    def test_o8_selector_rejects_tooltip_outside_and_foreign_native_root_without_input(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx, _session, drag = self._o8_readiness_context(directory)
            self._selector(ctx, {object(): (uia.TOOLTIP, (970, 999, 1059, 1024), 0),
                                 object(): (uia.GROUP, (970, 999, 1059, 1024), 0),
                                 object(): (uia.GROUP, (938, 968, 982, 1000), 55)}, roots={55: 99})
            cases = []
            real = common_checks.overlay_drag_snapshot
            with mock.patch.object(overlay_checks, "overlay_drag_snapshot",
                                   side_effect=lambda c, b, d: real(c, b, d, timeout=0)), \
                 mock.patch.object(win32, "mouse_up") as up:
                with self.assertRaisesRegex(Fail, "did not converge"):
                    overlay_checks._o8_drag(ctx, "cold", 71, None, -1, cases)
            drag.assert_not_called()
            up.assert_not_called()
            self.assertEqual(len(cases[0]["readiness_geometry_history"]), 4)
            for row in cases[0]["readiness_geometry_history"]:
                self.assertEqual(len(row["uia_candidates"]), 3)
                self.assertEqual([item["rejections"] for item in row["uia_candidates"]],
                                 [["not status affordance", "outside native overlay or unusable"],
                                  ["outside native overlay or unusable"], ["foreign native root"]])

    def test_o8_guard_trip_during_candidate_enumeration_stops_before_input(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx, _session, drag = self._o8_readiness_context(directory)
            element = object()
            self._selector(ctx, {element: (uia.GROUP, (938, 968, 982, 1000), 0)})
            def enumerate_candidates(*_args, **_kwargs):
                ctx.guard.check.side_effect = guard.UserInput("input during enumeration")
                return [element]
            real = common_checks.overlay_drag_snapshot
            with mock.patch.object(uia, "find_all", side_effect=enumerate_candidates), \
                 mock.patch.object(overlay_checks, "overlay_drag_snapshot",
                                   side_effect=lambda c, b, d: real(c, b, d, timeout=0)), \
                 mock.patch.object(win32, "mouse_up") as up:
                with self.assertRaises(guard.UserInput):
                    overlay_checks._o8_drag(ctx, "cold", 71, None, -1, [])
            drag.assert_not_called()
            up.assert_not_called()

    def test_o8_real_selector_requires_two_fresh_matching_geometry_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx, _session, drag = self._o8_readiness_context(directory)
            element = object()
            self._selector(ctx, {element: (uia.GROUP, (938, 968, 982, 1000), 0)})
            bounds = [(970, 999, 1059, 1024), (938, 968, 982, 1000), (938, 968, 982, 1000)]
            real = common_checks.overlay_drag_snapshot
            history = []
            with mock.patch.object(uia, "rect", side_effect=lambda _e: bounds.pop(0)), \
                 mock.patch.object(overlay_checks, "overlay_drag_snapshot",
                                   side_effect=lambda c, b, d: real(c, b, d, timeout=0)):
                result = overlay_checks._o8_ready_drag_point(ctx, "cold", 71, 44, None, (0, 0), history)
            self.assertEqual(result, ((960, 984), (938, 968, 982, 1000), (934, 964, 986, 1004)))
            self.assertEqual([row["result"] for row in history],
                             ["UIA candidates rejected", "awaiting matching snapshot", "verified"])
            self.assertEqual(history[0]["uia_candidates"][0]["bounds"], (970, 999, 1059, 1024))
            self.assertEqual(history[2]["uia_selected"]["bounds"], (938, 968, 982, 1000))
            drag.assert_not_called()

    def test_full_travel_fits_after_cold_failure_or_partial_movement(self):
        work = (0, 0, 1920, 1040)
        for start, rect in (((960, 984), (934, 964, 986, 1004)),
                            ((1010, 1004), (984, 984, 1036, 1024))):
            end = overlay_checks._o8_endpoint(start, rect, work, 1)
            self.assertEqual((abs(end[0] - start[0]), abs(end[1] - start[1])), (300, 200))
            self.assertTrue(0 <= end[0] < 1920 and 0 <= end[1] < 1040)
        with self.assertRaisesRegex(Fail, "cannot fit"):
            overlay_checks._o8_endpoint((100, 100), (80, 80, 120, 120), (0, 0, 200, 200), 1)

    def test_guard_trip_during_notepad_discovery_or_activation_prevents_click(self):
        for trip_at in ("discovery", "activation"):
            with self.subTest(trip_at=trip_at):
                tripped = [False]
                def check():
                    if tripped[0]:
                        raise guard.UserInput("observer lost")
                pad = targets.Notepad.__new__(targets.Notepad)
                pad.hwnd, pad.owned_tab = 42, True
                pad.active_document = lambda: True
                document = SimpleNamespace()
                def discover():
                    tripped[0] = trip_at == "discovery"
                    return document
                def activate(*_args, **_kwargs):
                    tripped[0] = True
                pad.document = discover
                pad.uia = SimpleNamespace(center=lambda _: (10, 20))
                pad.win32 = SimpleNamespace(activate=mock.Mock(side_effect=activate), click=mock.Mock())
                with self.assertRaises(guard.UserInput):
                    pad.focus(guard_check=check)
                pad.win32.click.assert_not_called()
                if trip_at == "discovery":
                    pad.win32.activate.assert_not_called()

    def test_guard_trip_during_notepad_creation_prevents_focus(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            ctx, session, pad, drag = self.make_context(directory, events)
            def create(*_args):
                ctx.guard.check.side_effect = guard.UserInput("input during creation")
                return pad
            ctx.notepad = create
            with mock.patch.object(overlay_checks, "_o8_drag", side_effect=drag), \
                 mock.patch.object(win32, "wait_for", return_value=True):
                with self.assertRaises(guard.UserInput):
                    overlay_checks.o8_foreground_drag(ctx)
            pad.focus.assert_not_called()
            session.show_settings.assert_not_called()

    def test_settings_owner_loss_rejected_before_singleton_handoff(self):
        ctx = SimpleNamespace(guard=SimpleNamespace(check=mock.Mock()),
                              murmur=SimpleNamespace(running=lambda: True, process=SimpleNamespace(pid=71),
                                                     show_settings=mock.Mock()))
        with mock.patch.object(overlay_checks, "_o8_settings", return_value=[{"hwnd": 33}]), \
             mock.patch.object(win32, "is_window", return_value=True), \
             mock.patch.object(win32, "window_pid", return_value=999), \
             mock.patch.object(win32, "activate") as activate:
            with self.assertRaisesRegex(Fail, "ownership changed"):
                overlay_checks._o8_show_settings(ctx, 71)
        ctx.murmur.show_settings.assert_not_called()
        activate.assert_not_called()

    def test_native_observation_signatures_preserve_pointer_width(self):
        import ctypes
        from ctypes import wintypes
        api = mock.Mock()
        with mock.patch.object(win32, "IS_WINDOWS", True), \
             mock.patch.object(win32, "user32", api), \
             mock.patch.object(win32, "kernel32", mock.Mock()), \
             mock.patch.object(win32, "dwmapi", mock.Mock()):
            win32._declare()
        self.assertEqual(api.GetWindowLongPtrW.argtypes, (wintypes.HWND, ctypes.c_int))
        self.assertEqual(api.GetWindowLongPtrW.restype, ctypes.c_ssize_t)
        self.assertEqual(api.IsWindowEnabled.argtypes, (wintypes.HWND,))
        self.assertEqual(api.IsWindowEnabled.restype, wintypes.BOOL)
        seen = []
        callback = ctypes.CFUNCTYPE(api.GetWindowLongPtrW.restype, *api.GetWindowLongPtrW.argtypes)(
            lambda hwnd, index: seen.append((hwnd, index)) or 0)
        callback(0x1234567887654321, -20)
        self.assertEqual(seen, [(0x1234567887654321, -20)])

    def test_launch_records_settings_hide_timeout_without_retrying_close(self):
        with tempfile.TemporaryDirectory() as directory:
            session = murmur.MurmurSession(SimpleNamespace(), directory,
                                           SimpleNamespace(sleep=lambda _seconds: None), {})
            session.home.mkdir()
            session.prepare = mock.Mock(return_value=session)
            session.env = mock.Mock(return_value={})
            session._wait_for_window = mock.Mock(side_effect=[44, 33])
            session.settings_hwnd = mock.Mock(return_value=33)
            with mock.patch.object(murmur.subprocess, "Popen", return_value=SimpleNamespace(pid=71)), \
                 mock.patch.object(murmur.win32, "close_window") as close, \
                 mock.patch.object(murmur.win32, "wait_for", return_value=None) as wait:
                session.launch(wav="a.wav")
            close.assert_called_once_with(33)
            wait.assert_called_once()
            self.assertEqual(session.settings_hide_observation,
                             {"hwnd": 33, "wait_succeeded": False, "visible_after_wait": True})
            for options in ({"wait_ready": False}, {"keep_settings": True}, {"args": ("--background",)}):
                session.settings_hide_observation = {"stale": True}
                session._wait_for_window = mock.Mock(return_value=33)
                with mock.patch.object(murmur.subprocess, "Popen", return_value=SimpleNamespace(pid=71)):
                    session.launch(wav="a.wav", **options)
                self.assertIsNone(session.settings_hide_observation)

    def test_o8_one_held_gesture_keeps_o2_movement_oracle_and_renderer_hit(self):
        self.enterContext(mock.patch.object(win32, "work_area_at", return_value=(0, 0, 1920, 1040)))
        rect = [(934, 964, 986, 1004)]
        session = SimpleNamespace(process=SimpleNamespace(pid=71), running=lambda: True,
                                  overlay_hwnd=lambda: 44, log=lambda: "drag started\n")
        ctx = SimpleNamespace(murmur=session, guard=SimpleNamespace(check=mock.Mock()),
                              restorer=SimpleNamespace(push=mock.Mock()), sleep=lambda _: None)
        context = {"hwnd": 52, "process_id": 99, "input_desktop": "Default", "gui_status": "ok"}
        def ancestor(hwnd, _kind):
            return {45: 44, 52: 52}.get(hwnd, hwnd)
        long_ptr = mock.Mock(return_value=0)
        user32 = SimpleNamespace(GetAncestor=ancestor, WindowFromPoint=lambda _point: 45,
                                 GetWindowLongPtrW=long_ptr, IsWindowVisible=lambda _h: 1,
                                 IsWindowEnabled=lambda _h: 1, GetAsyncKeyState=lambda _key: 0x8000)
        def drag(_start, _end, *, steps, release, sample):
            self.assertEqual((steps, release), (25, False))
            sample("at-start", 0)
            rect[0] = (634, 764, 686, 804)
            sample("moving", 25)
        valid = ((960, 984), (938, 968, 982, 1000))
        snapshots = [((1014, 1011), (970, 999, 1059, 1024)), valid, valid, valid, valid]
        def snapshot(_ctx, _native, _details):
            read = 5 - len(snapshots)
            self.assertEqual(injected.call_count, 0 if read < 3 else 1)
            return snapshots.pop(0)
        with mock.patch.object(overlay_checks, "overlay_drag_snapshot", side_effect=snapshot) as sampled, \
             mock.patch.object(win32, "foreground_context", return_value=context), \
             mock.patch.object(win32, "window_pid", side_effect=lambda hwnd: {44: 71, 45: 200}[hwnd]), \
             mock.patch.object(win32, "window_rect", side_effect=lambda _hwnd: rect[0]), \
             mock.patch.object(win32, "outer_rect", side_effect=lambda _hwnd: rect[0]), \
             mock.patch.object(win32, "client_origin", return_value=(934, 964)), \
             mock.patch.object(win32, "client_size", return_value=(52, 40)), \
             mock.patch.object(win32, "dpi_scale", return_value=1), \
             mock.patch.object(win32, "class_name", return_value="renderer"), \
             mock.patch.object(win32, "find_windows", return_value=[]), \
             mock.patch.object(win32, "input_desktop", return_value="Default"), \
             mock.patch.object(win32, "cursor", return_value=(660, 784)), \
             mock.patch.object(win32, "is_window", return_value=True), \
             mock.patch.object(win32, "user32", user32), \
             mock.patch.object(win32, "drag", side_effect=drag) as injected, \
             mock.patch.object(win32, "mouse_up") as up:
            observations = []
            row = overlay_checks._o8_drag(ctx, "owned Notepad foreground", 71, (52, 99),
                                          -1, observations)
            rect[0] = (934, 964, 986, 1004)  # a failed/partial preceding gesture cannot authorize clipping
            with mock.patch.object(win32, "work_area_at", return_value=(900, 900, 1000, 1040)):
                with self.assertRaisesRegex(Fail, "cannot fit"):
                    overlay_checks._o8_drag(ctx, "no room", 71, (52, 99), 1, observations)
        injected.assert_called_once()
        up.assert_called_once()
        self.assertTrue(row["moved_while_held"])
        self.assertEqual(row["hit_before"]["pid"], 200)
        self.assertEqual(row["click_in_hit_client"], (26, 20))
        self.assertEqual(len(row["held_samples"]), 2)
        self.assertEqual(len(row["readiness_geometry_history"]), 3)
        self.assertEqual(sampled.call_count, 5)  # mismatch, two matches, then two matches
        self.assertEqual(snapshots, [])
        self.assertIs(observations[0], row)
        self.assertEqual(len(observations), 2)
        self.assertIn("cannot fit", observations[1]["error"])
        self.assertEqual(len(observations[1]["readiness_geometry_history"]), 2)

    def _o8_readiness_context(self, directory):
        session = SimpleNamespace(process=SimpleNamespace(pid=71), running=lambda: True,
                                  overlay_hwnd=lambda: 44, ensure=mock.Mock(),
                                  log=lambda **_kw: "\n".join(
                                      f"QA overlay drag probe stage={stage} idle_icon=true pill_hidden=false"
                                      for stage in (4, 5, 6)),
                                  settings_hide_observation={"wait_succeeded": True,
                                                             "visible_after_wait": False})
        ctx = SimpleNamespace(murmur=session, fixtures={"A": SimpleNamespace(path="a.wav")},
                              restorer=SimpleNamespace(push=mock.Mock()), guard=SimpleNamespace(check=mock.Mock()),
                              artifact=lambda suffix: Path(directory) / suffix, sleep=lambda _: None,
                              note=mock.Mock())
        context = {"hwnd": 0, "process_id": 0, "input_desktop": "Default",
                   "gui_status": "no_foreground"}
        self.enterContext(mock.patch.object(win32, "window_pid", return_value=71))
        self.enterContext(mock.patch.object(win32, "window_rect", return_value=(934, 964, 986, 1004)))
        self.enterContext(mock.patch.object(win32, "foreground_context", return_value=context))
        self.enterContext(mock.patch.object(win32, "user32", SimpleNamespace(GetAncestor=lambda hwnd, _kind: hwnd)))
        drag = self.enterContext(mock.patch.object(win32, "drag"))
        return ctx, session, drag

    def test_o8_transient_uia_mismatch_waits_for_two_fresh_matching_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx, _session, drag = self._o8_readiness_context(directory)
            invalid = ((1014, 1011), (970, 999, 1059, 1024))
            valid = ((960, 984), (938, 968, 982, 1000))
            rows = [invalid, valid, valid]
            def snapshot(_ctx, _native, _details):
                self.assertEqual(drag.call_count, 0)
                return rows.pop(0)
            history = []
            with mock.patch.object(overlay_checks, "overlay_drag_snapshot", side_effect=snapshot):
                point, bounds, native = overlay_checks._o8_ready_drag_point(
                    ctx, "cold", 71, 44, None, (0, 0), history)
            self.assertEqual((point, bounds, native), (*valid, (934, 964, 986, 1004)))
            self.assertEqual([row["result"] for row in history],
                             ["UIA handle outside native overlay or unusable",
                              "awaiting matching snapshot", "verified"])
            self.assertEqual(rows, [])
            drag.assert_not_called()

    def test_o8_missing_uia_handle_requires_two_later_matching_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx, _session, drag = self._o8_readiness_context(directory)
            valid = ((960, 984), (938, 968, 982, 1000))
            history = []
            with mock.patch.object(overlay_checks, "overlay_drag_snapshot",
                                   side_effect=[uia.NotFound("handle absent"), valid, valid]) as snapshot:
                result = overlay_checks._o8_ready_drag_point(ctx, "cold", 71, 44, None, (0, 0), history)
            self.assertEqual(result, (*valid, (934, 964, 986, 1004)))
            self.assertEqual(snapshot.call_count, 3)
            self.assertEqual([row["result"] for row in history],
                             ["UIA handle not found", "awaiting matching snapshot", "verified"])
            self.assertEqual(history[0]["uia_error"], "handle absent")
            drag.assert_not_called()

    def test_o8_permanent_invalid_geometry_fails_bounded_with_launch_probe_only(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx, session, drag = self._o8_readiness_context(directory)
            invalid = ((1014, 1011), (970, 999, 1059, 1024))
            with mock.patch.object(overlay_checks, "overlay_drag_snapshot", return_value=invalid) as snapshot, \
                 mock.patch.object(win32, "wait_for", side_effect=lambda predicate, *_a, **_kw: predicate()), \
                 mock.patch.object(win32, "mouse_up") as mouse_up:
                with self.assertRaisesRegex(Fail, "did not converge"):
                    overlay_checks.o8_foreground_drag(ctx)
            self.assertEqual(snapshot.call_count, overlay_checks.O8_GEOMETRY_ATTEMPTS)
            drag.assert_not_called()
            mouse_up.assert_not_called()
            self.assertEqual(ctx.restorer.push.call_count, 1)  # placement reset only
            session.ensure.assert_called_once()
            record = json.loads((Path(directory) / "foreground-drag-diagnostic.json").read_text())
            self.assertEqual(record["status"], "fail")
            self.assertEqual(len(record["launch_dom_probes"]), 3)
            self.assertEqual(len(record["cases"]), 1)
            self.assertEqual(len(record["cases"][0]["readiness_geometry_history"]),
                             overlay_checks.O8_GEOMETRY_ATTEMPTS)
            self.assertEqual(record["cases"][0]["readiness_geometry_history"][0]["uia_drag_bounds"],
                             list(invalid[1]))

    def test_o8_guard_trip_during_uia_read_stops_before_input(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx, _session, drag = self._o8_readiness_context(directory)
            def snapshot(_ctx, _native, _details):
                ctx.guard.check.side_effect = guard.UserInput("owner touched input during UIA")
                return (960, 984), (938, 968, 982, 1000)
            cases = []
            with mock.patch.object(overlay_checks, "overlay_drag_snapshot", side_effect=snapshot), \
                 mock.patch.object(win32, "mouse_up") as mouse_up:
                with self.assertRaises(guard.UserInput):
                    overlay_checks._o8_drag(ctx, "cold", 71, None, -1, cases)
            drag.assert_not_called()
            mouse_up.assert_not_called()
            self.assertEqual(len(cases), 1)
            self.assertIn("UserInput", cases[0]["readiness_geometry_history"][0]["error"])
            self.assertEqual(cases[0]["readiness_geometry_history"][0]["uia_drag_bounds"],
                             (938, 968, 982, 1000))

    def test_o8_changed_geometry_owner_or_desktop_during_uia_fails_before_input(self):
        for changed in ("geometry", "owner", "process", "foreground", "root", "desktop"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                ctx, session, drag = self._o8_readiness_context(directory)
                cases = []
                def snapshot(_ctx, _native, _details):
                    if changed == "owner":
                        session.overlay_hwnd = lambda: 55
                    if changed == "process":
                        session.process.pid = 72
                    if changed == "foreground":
                        win32.foreground_context.return_value = {
                            "hwnd": 52, "process_id": 99, "input_desktop": "Default", "gui_status": "ok"}
                    if changed == "root":
                        win32.foreground_context.return_value = {
                            "hwnd": 52, "process_id": 0, "input_desktop": "Default", "gui_status": "ok"}
                    if changed == "desktop":
                        win32.foreground_context.return_value = {
                            "hwnd": 0, "process_id": 0, "input_desktop": "Winlogon", "gui_status": "no_foreground"}
                    return (960, 984), (938, 968, 982, 1000)
                with mock.patch.object(overlay_checks, "overlay_drag_snapshot", side_effect=snapshot), \
                     mock.patch.object(win32, "window_rect", side_effect=[
                         (934, 964, 986, 1004), (935, 964, 987, 1004)] if changed == "geometry"
                         else None, return_value=(934, 964, 986, 1004)):
                    with self.assertRaisesRegex(Fail, "geometry changed|ownership changed|Unsafe desktop"):
                        overlay_checks._o8_drag(ctx, "cold", 71, None, -1, cases)
                drag.assert_not_called()
                self.assertEqual(len(cases), 1)
                self.assertEqual(len(cases[0]["readiness_geometry_history"]), 1)

    def make_context(self, directory, events, *, movements=(True, True, True, True)):
        self.enterContext(mock.patch.object(overlay_checks, "_o8_settings", return_value=[{"hwnd": 33, "pid": 71}]))
        self.enterContext(mock.patch.object(win32, "window_text", return_value="Murmur Settings"))
        pad = SimpleNamespace(hwnd=52, owner_pid=99, owned_tab=True,
                              active_document=lambda: True, owns_keyboard_focus=lambda: True)
        pad.focus = mock.Mock()
        session = SimpleNamespace(process=SimpleNamespace(pid=71), running=lambda: True,
                                  overlay_hwnd=lambda: 44,
                                  ensure=mock.Mock(side_effect=lambda *_a: events.append("ensure")),
                                  log=lambda **_kw: "QA overlay drag probe stage=4\nstage=5\nstage=6",
                                  settings_hide_observation={"wait_succeeded": True, "visible_after_wait": False},
                                  restart=mock.Mock(side_effect=lambda: events.append("restart")),
                                  show_settings=mock.Mock(side_effect=lambda **_kw: events.append("show_settings") or 33))
        ctx = SimpleNamespace(murmur=session, fixtures={"A": SimpleNamespace(path="a.wav")},
                              restorer=SimpleNamespace(push=mock.Mock()), guard=SimpleNamespace(check=mock.Mock()),
                              startup_probe_lease=SimpleNamespace(closed=False),
                              artifact=lambda suffix: Path(directory) / suffix, note=mock.Mock(),
                              sleep=lambda _seconds: None)
        def focus(**kwargs):
            kwargs["guard_check"]()
            events.append("notepad_focus")
            ctx.startup_probe_lease.closed = True
        pad.focus.side_effect = focus
        def notepad(*_args):
            events.append("create_notepad")
            return pad
        ctx.notepad = notepad
        positions = [(634, 764, 686, 804), (934, 964, 986, 1004),
                     (634, 764, 686, 804), (934, 964, 986, 1004)]
        def drag(_ctx, label, pid, expected, direction, observations):
            index = len(observations)
            events.append(("drag", label, pid, expected, direction, ctx.startup_probe_lease.closed))
            row = {"case": label, "moved_while_held": movements[index], "after": positions[index],
                   "foreground_before": {"hwnd": 8, "process_id": 8},
                   "foreground_root": 8, "foreground_after": {"hwnd": 8, "process_id": 8},
                   "foreground_after_root": 8}
            observations.append(row)
            return row
        return ctx, session, pad, drag

    def test_o8_order_one_process_and_first_cold_gesture_precedes_notepad(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            ctx, session, pad, drag = self.make_context(directory, events)
            with mock.patch.object(overlay_checks, "_o8_drag", side_effect=drag), \
                 mock.patch.object(overlay_checks.win32, "wait_for", return_value=True), \
                 mock.patch.object(overlay_checks.win32, "pids", return_value=[99]), \
                 mock.patch.object(overlay_checks.win32, "window_pid", side_effect=lambda hwnd: {52: 99, 33: 71}[hwnd]), \
                 mock.patch.object(overlay_checks.win32, "is_window", return_value=True), \
                 mock.patch.object(overlay_checks.win32, "activate", side_effect=lambda *_a, **_kw: events.append("activate_settings") or True), \
                 mock.patch.object(overlay_checks.win32, "window_rect", return_value=(934, 964, 986, 1004)):
                overlay_checks.o8_foreground_drag(ctx)
            drags = [row for row in events if isinstance(row, tuple)]
            self.assertEqual([row[1] for row in drags], ["cold foreground untouched",
                             "owned Notepad foreground", "owned Murmur Settings foreground",
                             "same owned Notepad foreground"])
            self.assertEqual([row[2] for row in drags], [71] * 4)
            self.assertEqual([row[4] for row in drags], [-1, 1, -1, 1])
            self.assertFalse(drags[0][5])
            self.assertLess(events.index(("drag", "cold foreground untouched", 71, None, -1, False)),
                            events.index("create_notepad"))
            self.assertEqual(pad.focus.call_count, 2)
            self.assertEqual(session.ensure.call_count, 1)
            self.assertEqual(session.restart.call_count, 1)
            record = json.loads((Path(directory) / "foreground-drag-diagnostic.json").read_text())
            self.assertEqual(record["status"], "inconclusive")
            self.assertTrue(record["restart"]["within_2px"])

    def test_o8_latches_early_failure_even_after_later_success(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            ctx, session, _pad, drag = self.make_context(directory, events,
                                                          movements=(False, True, True, True))
            with mock.patch.object(overlay_checks, "_o8_drag", side_effect=drag), \
                 mock.patch.object(overlay_checks.win32, "wait_for", return_value=True), \
                 mock.patch.object(overlay_checks.win32, "pids", return_value=[99]), \
                 mock.patch.object(overlay_checks.win32, "window_pid", side_effect=lambda hwnd: {52: 99, 33: 71}[hwnd]), \
                 mock.patch.object(overlay_checks.win32, "is_window", return_value=True), \
                 mock.patch.object(overlay_checks.win32, "activate", return_value=True), \
                 mock.patch.object(overlay_checks.win32, "window_rect", return_value=(934, 964, 986, 1004)):
                with self.assertRaisesRegex(Fail, "cold foreground untouched: no held movement"):
                    overlay_checks.o8_foreground_drag(ctx)
            record = json.loads((Path(directory) / "foreground-drag-diagnostic.json").read_text())
            self.assertEqual(len(record["cases"]), 4)
            self.assertEqual(record["status"], "fail")
            session.restart.assert_called_once()

    def test_o8_retains_every_movement_failure_and_makes_no_persistence_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            ctx, session, _pad, drag = self.make_context(directory, events,
                                                          movements=(False, False, False, False))
            with mock.patch.object(overlay_checks, "_o8_drag", side_effect=drag), \
                 mock.patch.object(overlay_checks.win32, "wait_for", return_value=True), \
                 mock.patch.object(overlay_checks.win32, "pids", return_value=[99]), \
                 mock.patch.object(overlay_checks.win32, "window_pid", side_effect=lambda hwnd: {52: 99, 33: 71}[hwnd]), \
                 mock.patch.object(overlay_checks.win32, "is_window", return_value=True), \
                 mock.patch.object(overlay_checks.win32, "activate", return_value=True):
                with self.assertRaises(Fail):
                    overlay_checks.o8_foreground_drag(ctx)
            record = json.loads((Path(directory) / "foreground-drag-diagnostic.json").read_text())
            self.assertEqual(len(record["failures"]), 4)
            self.assertEqual(len(record["cases"]), 4)
            self.assertFalse(record["restart"]["verified"])
            session.restart.assert_not_called()

    def test_o8_records_settings_hide_timeout_without_treating_later_drags_as_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            ctx, session, _pad, drag = self.make_context(directory, events)
            session.settings_hide_observation = {"wait_succeeded": False, "visible_after_wait": True}
            with mock.patch.object(overlay_checks, "_o8_drag", side_effect=drag), \
                 mock.patch.object(overlay_checks.win32, "wait_for", return_value=True), \
                 mock.patch.object(overlay_checks.win32, "pids", return_value=[99]), \
                 mock.patch.object(overlay_checks.win32, "window_pid", side_effect=lambda hwnd: {52: 99, 33: 71}[hwnd]), \
                 mock.patch.object(overlay_checks.win32, "is_window", return_value=True), \
                 mock.patch.object(overlay_checks.win32, "activate", return_value=True), \
                 mock.patch.object(overlay_checks.win32, "window_rect", return_value=(934, 964, 986, 1004)):
                with self.assertRaisesRegex(Fail, "Settings hide after cold launch"):
                    overlay_checks.o8_foreground_drag(ctx)
            record = json.loads((Path(directory) / "foreground-drag-diagnostic.json").read_text())
            self.assertEqual(record["status"], "fail")
            self.assertEqual(len(record["cases"]), 4)
            self.assertFalse(record["settings_hide_on_launch"]["wait_succeeded"])

    def test_o8_refuses_notepad_activation_when_window_owner_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            ctx, session, pad, drag = self.make_context(directory, events)
            pad.owner_pid = 100
            with mock.patch.object(overlay_checks, "_o8_drag", side_effect=drag), \
                 mock.patch.object(overlay_checks.win32, "wait_for", return_value=True), \
                 mock.patch.object(overlay_checks.win32, "pids", return_value=[99]):
                with self.assertRaisesRegex(Fail, "Notepad ownership was not verified"):
                    overlay_checks.o8_foreground_drag(ctx)
            self.assertEqual(pad.focus.call_count, 0)
            self.assertNotIn("show_settings", events)
            session.restart.assert_not_called()
            record = json.loads((Path(directory) / "foreground-drag-diagnostic.json").read_text())
            self.assertEqual(len(record["cases"]), 1)

    def test_o8_skips_restart_claim_when_failed_later_drag_changes_position(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            ctx, session, _pad, drag = self.make_context(directory, events,
                                                          movements=(True, False, False, False))
            with mock.patch.object(overlay_checks, "_o8_drag", side_effect=drag), \
                 mock.patch.object(overlay_checks.win32, "wait_for", return_value=True), \
                 mock.patch.object(overlay_checks.win32, "pids", return_value=[99]), \
                 mock.patch.object(overlay_checks.win32, "window_pid", side_effect=lambda hwnd: {52: 99, 33: 71}[hwnd]), \
                 mock.patch.object(overlay_checks.win32, "is_window", return_value=True), \
                 mock.patch.object(overlay_checks.win32, "activate", return_value=True):
                with self.assertRaisesRegex(Fail, "owned Notepad foreground: no held movement"):
                    overlay_checks.o8_foreground_drag(ctx)
            record = json.loads((Path(directory) / "foreground-drag-diagnostic.json").read_text())
            self.assertEqual(len(record["cases"]), 4)
            self.assertFalse(record["restart"]["verified"])
            session.restart.assert_not_called()

    def test_o8_guard_abort_and_notepad_setup_failure_stop_without_settings(self):
        for failure in (guard.UserInput("real input"), RuntimeError("Notepad spawn failed")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                events = []
                ctx, session, _pad, drag = self.make_context(directory, events)
                if isinstance(failure, guard.UserInput):
                    def aborting_drag(*args):
                        if len(args[-1]) == 1:
                            raise failure
                        return drag(*args)
                    trial = aborting_drag
                else:
                    trial = drag
                    def broken_notepad(*_args):
                        events.append("create_notepad")
                        raise failure
                    ctx.notepad = broken_notepad
                with mock.patch.object(overlay_checks, "_o8_drag", side_effect=trial), \
                     mock.patch.object(overlay_checks.win32, "wait_for", return_value=True), \
                     mock.patch.object(overlay_checks.win32, "pids", return_value=[99]), \
                     mock.patch.object(overlay_checks.win32, "window_pid", return_value=99):
                    with self.assertRaises(type(failure)):
                        overlay_checks.o8_foreground_drag(ctx)
                record = json.loads((Path(directory) / "foreground-drag-diagnostic.json").read_text())
                self.assertIn(record["status"], ("aborted", "fail"))
                self.assertNotIn("show_settings", events)
                session.restart.assert_not_called()


class ReportTests(unittest.TestCase):
    def result(self, status, reason="", attempt=1, **extra):
        return report.Result("H1", status, reason, "Hold", "hotkeys", attempt=attempt, **extra)

    def test_fail_and_skip_need_a_reason(self):
        with self.assertRaises(ValueError):
            report.Result("H1", report.FAIL)
        with self.assertRaises(ValueError):
            report.Result("H1", report.SKIPPED)
        with self.assertRaises(ValueError):
            report.Result("H1", "maybe", "x")

    def test_aggregate_marks_flaky_failures(self):
        row = report.aggregate([self.result(report.PASS), self.result(report.FAIL, "late", 2),
                                self.result(report.PASS, attempt=3)])
        self.assertEqual(row.status, report.FAIL)
        self.assertIn("flaky: failed 1 of 3", row.reason)
        self.assertEqual(row.attempt, 3)

    def test_aggregate_all_skipped_stays_skipped(self):
        row = report.aggregate([self.result(report.SKIPPED, "icon hidden")])
        self.assertEqual((row.status, row.reason), (report.SKIPPED, "icon hidden"))

    def test_markdown_has_one_row_per_id_and_escapes_pipes(self):
        attempts = [self.result(report.PASS, artifacts=["C:/out/H1-notepad.png"]),
                    report.Result("I3", report.SKIPPED, "no | buffer", best_effort=True)]
        text = report.markdown({"sha": SHA}, attempts)
        self.assertIn("| H1 | pass |  | `H1-notepad.png` |", text)
        self.assertIn("| I3 (best effort) | skipped | no \\| buffer |  |", text)
        self.assertIn("1 pass · 0 fail · 1 skipped · 0 aborted", text)

    def test_write_and_load_round_trip_and_manual_merge(self):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work)
        attempts = [self.result(report.PASS), report.Result("S2", report.SKIPPED, "manual")]
        report.write(work, {"sha": SHA}, attempts)
        meta, loaded = report.load(work / "desktop-report.json")
        self.assertEqual(meta["sha"], SHA)
        self.assertEqual([r.id for r in loaded], ["H1", "S2"])
        merged = report.merge(loaded, [report.Result("S2", report.PASS, source="manual")])
        self.assertEqual([(r.id, r.status) for r in merged], [("H1", report.PASS), ("S2", report.PASS)])
        data = json.loads((work / "desktop-report.json").read_text())
        self.assertEqual(data["schema"], report.SCHEMA)
        self.assertEqual(len(data["rows"]), 2)


class GoldenTests(unittest.TestCase):
    def test_compare(self):
        self.assertIsNone(golden.compare("The fox. ", "The fox. "))
        self.assertIn("differs at character 4", golden.compare("The fox. ", "The Fox. "))
        self.assertIn("more than once", golden.compare("Hi there. ", "Hi there. Hi there. "))
        self.assertIn("nothing delivered", golden.compare("x", None))
        self.assertIn("lengths 4 vs 3", golden.compare("abcd", "abc"))

    def test_store_records_first_sample_for_semantics_only(self):
        store = golden.GoldenStore()
        key = golden.key("A", "hold", "Ctrl+Alt+Shift+F9")
        self.assertEqual(key, golden.key("A", "hold", "ctrl+alt+shift+f9"))
        with self.assertRaises(ValueError):
            store.record(key, "  ")
        store.record(key, "The quick brown fox. ")
        with self.assertRaises(ValueError):
            store.record(key, "again")
        self.assertEqual(store.get(key), "The quick brown fox. ")
        self.assertNotEqual(key, golden.key("A", "hold", "Ctrl+Alt+Shift+F9", style="code"))

    def test_fixture_recognition_is_separate_from_exact_delivery(self):
        self.assertIsNone(golden.fixture_fidelity("A", "The quick brown fox jumps over the lazy dog "))
        self.assertIsNone(golden.fixture_fidelity("A", "The quick brown fox jumps over the lazy dog. "))
        self.assertIsNotNone(golden.fixture_fidelity("A", "The quick brown fox jumped over a dog. "))
        self.assertIsNotNone(golden.fixture_fidelity("A", "The quick brown fox jumps over the very lazy dog. "))
        self.assertIsNotNone(golden.compare("The quick brown fox jumps over the lazy dog ",
                                           "The quick brown fox jumps over the lazy dog. "))

    def test_per_utterance_oracle_accepts_distinct_inferences_and_catches_delivery_mismatch(self):
        first = "The quick brown fox jumps over the lazy dog "
        second = "The quick brown fox jumps over the lazy dog. "
        outputs = [(1, first)]
        session = SimpleNamespace(history_cursor=lambda: outputs[-1][0] - 1,
                                  history_after=lambda cursor: [row for row in outputs if row[0] > cursor])
        ctx = Context.__new__(Context)
        ctx.murmur = session
        ctx.sleep = lambda _: None
        ctx.observe_delivery("A")
        self.assertEqual(ctx.expect_delivery(first, "TargetApp"), first)
        outputs.append((2, second))
        ctx.observe_delivery("A")
        # This observation starts at row 1, independently of the first sample.
        self.assertEqual(ctx.expect_delivery(second, "TargetApp"), second)
        with self.assertRaisesRegex(Fail, "differs at character"):
            ctx.expect_delivery(first, "TargetApp")
        outputs.append((3, second))
        with self.assertRaisesRegex(Fail, "Expected one output"):
            ctx.observed_text()

    def test_notepad_completion_wait_accepts_history_after_five_seconds_without_early_uia(self):
        now = [0.0]
        expected = fixtures.SENTENCES["A"] + " "
        reads = []

        def history_after(cursor):
            reads.append(now[0])
            return [(1, expected)] if now[0] >= 6.0 and cursor == 0 else []

        ctx = Context.__new__(Context)
        ctx.murmur = SimpleNamespace(history_cursor=lambda: 0, history_after=history_after)
        ctx.sleep = lambda duration: now.__setitem__(0, now[0] + duration)
        pad = SimpleNamespace(focus_diagnostic=mock.Mock(), hwnd=42)
        with mock.patch.object(win32.time, "monotonic", side_effect=lambda: now[0]):
            ctx.observe_delivery("A")
            ctx.mark_delivery_released()
            from checks.common import confirm_notepad_output
            confirm_notepad_output(ctx, pad)
        self.assertGreaterEqual(now[0], 6.0)
        self.assertLess(now[0], 45.0)
        self.assertTrue(any(sample >= 5.0 for sample in reads))
        pad.focus_diagnostic.assert_not_called()

    def test_notepad_completion_timeout_preserves_count_state_timing_and_app_log(self):
        now = [0.0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ctx = Context.__new__(Context)
            ctx.murmur = SimpleNamespace(history_cursor=lambda: 0, history_after=lambda _: [],
                                          overlay_state=lambda: "held", log=lambda **_: "synthetic app log")
            ctx.sleep = lambda duration: now.__setitem__(0, now[0] + duration)
            ctx.settle = lambda: "held"
            ctx.note = mock.Mock()
            ctx.artifact = lambda suffix: root / suffix
            ctx.screenshot = mock.Mock()
            pad = SimpleNamespace(focus_diagnostic=lambda: "synthetic focus", hwnd=42)
            with mock.patch.object(win32.time, "monotonic", side_effect=lambda: now[0]):
                ctx.observe_delivery("A")
                ctx.mark_delivery_released()
                from checks.common import confirm_notepad_output
                with self.assertRaises(OutputCount) as failure:
                    confirm_notepad_output(ctx, pad)
            self.assertEqual(failure.exception.count, 0)
            self.assertGreaterEqual(now[0], 45.0)
            state = json.loads((root / "output-failure-state.json").read_text())
            self.assertEqual(state["history_count_after"], 0)
            self.assertEqual(state["overlay_state"], "held")
            self.assertEqual(state["settle_state"], "held")
            self.assertGreaterEqual(state["history_wait_elapsed_s"], 45.0)
            self.assertGreaterEqual(state["seconds_since_release"], 45.0)
            self.assertEqual((root / "output-failure-app-log.txt").read_text(), "synthetic app log")
            ctx.screenshot.assert_called_once_with(42, "notepad.png")

    def test_notepad_completion_rejects_duplicate_history_and_propagates_guard_loss(self):
        expected = fixtures.SENTENCES["A"] + " "
        ctx = Context.__new__(Context)
        ctx.murmur = SimpleNamespace(history_cursor=lambda: 0,
                                      history_after=lambda _: [(1, expected), (2, expected)])
        ctx.sleep = lambda _: None
        ctx.observe_delivery("A")
        with self.assertRaises(OutputCount) as failure:
            ctx.observed_text(timeout=45)
        self.assertEqual(failure.exception.count, 2)

        ctx.murmur.history_after = lambda _: []
        ctx.sleep = lambda _: (_ for _ in ()).throw(guard.GuardUnavailable("probe lost"))
        with self.assertRaises(guard.GuardUnavailable):
            ctx.observed_text(timeout=45)

    def test_history_oracle_reads_disposable_profile_without_creating_database(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            session = murmur.MurmurSession.__new__(murmur.MurmurSession)
            session.home = Path(folder)
            self.assertEqual(session.history_cursor(), 0)
            self.assertFalse(session.history_path.exists())
            session.data.mkdir(parents=True)
            with closing(sqlite3.connect(session.history_path)) as db, db:
                db.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, text TEXT NOT NULL)")
                db.execute("INSERT INTO entries(text) VALUES (?)", ("The fox. ",))
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")
            self.assertEqual(session.history_after(0), [(1, "The fox. ")])
            self.assertEqual(session.history_cursor(), 1)
            self.assertEqual(session.history_after(1), [])

    def test_history_oracle_closes_connections_after_successful_reads(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            session = murmur.MurmurSession.__new__(murmur.MurmurSession)
            session.home = Path(folder)
            session.data.mkdir(parents=True)
            with closing(sqlite3.connect(session.history_path)) as db, db:
                db.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, text TEXT NOT NULL)")
                db.execute("INSERT INTO entries(text) VALUES ('The fox. ')")

            real_connect = sqlite3.connect
            opened = []

            def tracking_connect(*args, **kwargs):
                db = real_connect(*args, **kwargs)
                opened.append(db)
                return db

            with mock.patch.object(murmur.sqlite3, "connect", side_effect=tracking_connect):
                self.assertEqual(session.history_after(0), [(1, "The fox. ")])
                self.assertEqual(session.history_cursor(), 1)
            self.assertEqual(len(opened), 2)
            for db in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    db.execute("SELECT 1")

    def test_history_oracle_closes_connections_after_query_errors(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            session = murmur.MurmurSession.__new__(murmur.MurmurSession)
            session.home = Path(folder)
            session.data.mkdir(parents=True)
            with closing(sqlite3.connect(session.history_path)) as db, db:
                db.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")

            real_connect = sqlite3.connect
            opened = []

            def tracking_connect(*args, **kwargs):
                db = real_connect(*args, **kwargs)
                opened.append(db)
                return db

            with mock.patch.object(murmur.sqlite3, "connect", side_effect=tracking_connect):
                with self.assertRaises(sqlite3.OperationalError):
                    session.history_after(0)
                with self.assertRaises(sqlite3.OperationalError):
                    session.history_cursor()
            self.assertEqual(len(opened), 2)
            for db in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    db.execute("SELECT 1")

    def test_history_opt_in_is_scoped_to_dictation_checks(self):
        ctx = Context.__new__(Context)
        ctx.murmur = SimpleNamespace(base_config={"privacy": {"history_enabled": False}})
        for check_id, wanted in (("S1", False), ("H2", True), ("I3", True), ("F13", True),
                                 ("M3", True), ("C1", False)):
            ctx.begin(check_id)
            self.assertEqual(ctx.murmur.base_config["privacy"]["history_enabled"], wanted)


class TerminalBufferTests(unittest.TestCase):
    def test_selector_ignores_title_text_pattern_and_chooses_prompt_buffer(self):
        title, buffer = object(), object()
        with mock.patch.object(uia, "descendants", return_value=[title, buffer]), \
             mock.patch.object(uia, "has_pattern", return_value=True), \
             mock.patch.object(uia, "text", side_effect=["Murmur desktop terminal",
                                                        "PS C:\\Users\\qa> The quick brown fox"]):
            self.assertIs(uia.terminal_text_element(object(), timeout=0), buffer)

    def test_selector_does_not_accept_title_when_prompt_is_absent(self):
        with mock.patch.object(uia, "descendants", return_value=[object()]), \
             mock.patch.object(uia, "has_pattern", return_value=True), \
             mock.patch.object(uia, "text", return_value="Murmur desktop terminal"):
            with self.assertRaisesRegex(uia.NotFound, "PowerShell prompt"):
                uia.terminal_text_element(object(), timeout=0)


class ConfigWriterTests(unittest.TestCase):
    def test_partial_config_round_trips_through_toml(self):
        config = tomlconfig.merge(run.base_config("Ctrl+Alt+Shift+F9"), {
            "postproc": {"replacements": {"marker phrase": "café déjà vu", 'say "x"': "y\\z"}},
            "injection": {"exclude_apps": ["notepad"], "focus_change": "refocus"},
            "overlay": {"scale": 1.6, "placement": {"x_fraction": 0.25, "monitor_width": 1920}},
        })
        text = tomlconfig.dumps(config)
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["postproc"]["replacements"]["marker phrase"], "café déjà vu")
        self.assertEqual(parsed["postproc"]["replacements"]['say "x"'], "y\\z")
        self.assertEqual(parsed["injection"]["exclude_apps"], ["notepad"])
        self.assertEqual(parsed["overlay"]["placement"]["x_fraction"], 0.25)
        self.assertEqual(parsed["general"]["hotkey"], "Ctrl+Alt+Shift+F9")
        self.assertLess(text.index("[overlay]"), text.index("[overlay.placement]"))

    def test_keys_and_values_follow_the_app_schema(self):
        for bad in ({"general": {"hot_key": "x"}}, {"nope": {"a": 1}}, {"general": {"hotkey_mode": "press"}},
                    {"injection": {"focus_change": "steal"}}, {"overlay": {"scale": 2.0}},
                    {"general": {"autostart": "yes"}}, {"injection": {"exclude_apps": [1]}},
                    {"audio": {"silence_timeout_ms": 50}}, {"engine": {"beam_size": True}}):
            with self.subTest(bad=bad), self.assertRaises(tomlconfig.ConfigError):
                tomlconfig.dumps(bad)

    def test_schema_keys_exist_in_config_rs(self):
        source = (HARNESS.parents[2] / "src-tauri" / "src" / "config.rs").read_text(encoding="utf-8")
        for section, keys in tomlconfig.SCHEMA.items():
            for key in keys:
                with self.subTest(key=f"{section}.{key}"):
                    self.assertRegex(source, rf"\b{key}: ")

    def test_merge_replaces_replacements_and_deep_merges_tables(self):
        base = {"general": {"hotkey": "A", "theme": "light"}, "postproc": {"replacements": {"a": "b"}}}
        merged = tomlconfig.merge(base, {"general": {"theme": "dark"}, "postproc": {"replacements": {"c": "d"}}})
        self.assertEqual(merged, {"general": {"hotkey": "A", "theme": "dark"}, "postproc": {"replacements": {"c": "d"}}})
        self.assertEqual(base["general"]["theme"], "light")


class CatalogTests(unittest.TestCase):
    def test_47_automated_checks_with_unique_ids(self):
        ids = [c.id for c in catalog.select()]
        self.assertEqual(len(ids), 47)
        self.assertEqual(len(set(ids)), 47)
        self.assertEqual({c.id for c in catalog.select() if c.best_effort}, {"T1", "T2", "I3", "K1"})
        self.assertEqual(len({c.id for c in catalog.CHECKS}), len(catalog.CHECKS))

    def test_every_check_function_exists_in_its_group_module(self):
        for check in catalog.CHECKS:
            source = (HARNESS / "checks" / f"{check.group}.py").read_text(encoding="utf-8")
            with self.subTest(check=check.id):
                self.assertRegex(source, rf"(?m)^def {check.function}\(ctx\):")
                self.assertTrue(check.function.startswith(check.id.lower() + "_"))

    def test_automated_and_manual_ids_cover_the_checklist(self):
        text = (HARNESS.parents[2] / "docs" / "parity" / "windows-checklist.md").read_text(encoding="utf-8")
        listed = set(re.findall(r"(?m)^\| ([A-Z]\d+) ", text))
        covered = {c.id for c in catalog.CHECKS} | {i.split("-")[0] for i, _ in catalog.MANUAL}
        self.assertEqual(listed - covered, set())

    def test_selection(self):
        self.assertNotIn("O8", [check.id for check in catalog.select()])
        self.assertEqual([check.id for check in catalog.select("O8")], ["O8"])
        self.assertIn("O8", run.NOTEPAD_CHECKS)
        self.assertEqual([c.id for c in catalog.select("f2, h1")], ["H1", "F2"])
        self.assertEqual([c.group for c in catalog.select(group="focus")], ["focus"] * 11)
        self.assertEqual([c.id for c in catalog.select("H1,F2", "focus")], ["F2"])
        self.assertEqual(catalog.select()[0].id, "P1")
        self.assertEqual(catalog.select()[-1].id, "C3")
        with self.assertRaisesRegex(catalog.SelectionError, "manual"):
            catalog.select("S2")
        with self.assertRaisesRegex(catalog.SelectionError, "Unknown group"):
            catalog.select(group="network")
        with self.assertRaisesRegex(catalog.SelectionError, "matches no checks"):
            catalog.select("H1", "focus")
        self.assertEqual(catalog.parse_ids(" h1 ;H1, f13 "), ["H1", "F13"])

    def test_o8_refuses_mixed_or_repeated_selection_before_input(self):
        for extra in (("--only", "O8,O2"), ("--only", "O8", "--repeat", "2")):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as error:
                run.main(["--build", "unused", "--out", "unused", *extra])
            self.assertEqual(error.exception.code, 2)

    def test_overlay_error_state_check_is_ordered_before_the_drag(self):
        overlay = [c.id for c in catalog.select(group="overlay")]
        self.assertEqual(overlay[0], "O1")


class RestoreTests(unittest.TestCase):
    def test_undo_runs_newest_first_and_survives_failures(self):
        calls = []
        restorer = restore.Restorer(log=lambda _: None)
        restorer.push("first", lambda: calls.append(1))
        restorer.push("broken", lambda: 1 / 0)
        restorer.push("third", lambda: calls.append(3))
        errors = restorer.restore_all()
        self.assertEqual(calls, [3, 1])
        self.assertEqual([label for label, _ in errors], ["broken"])
        self.assertEqual(restorer.pending, [])

    def test_restore_to_mark_keeps_session_level_entries(self):
        calls = []
        restorer = restore.Restorer()
        restorer.push("session", lambda: calls.append("session"))
        mark = restorer.mark()
        token = restorer.push("check", lambda: calls.append("check"))
        restorer.push("check 2", lambda: calls.append("check 2"))
        restorer.discard(token)
        restorer.restore_to(mark)
        self.assertEqual(calls, ["check 2"])
        self.assertEqual(restorer.pending, ["session"])

    def test_registry_value_restores_missing_and_existing_values(self):
        class Backend:
            def __init__(self):
                self.data = {("Run", "Murmur"): ("C:\\owner\\murmur.exe --background", 1)}

            def read(self, path, name):
                return self.data.get((path, name), restore.MISSING)

            def write(self, path, name, value, kind):
                self.data[(path, name)] = (value, kind)

            def delete(self, path, name):
                del self.data[(path, name)]

        backend, restorer = Backend(), restore.Restorer()
        restore.RegistryValue(backend, "Run", "Murmur").set(restorer, "C:\\test\\murmur.exe", 1)
        restore.RegistryValue(backend, "Consent", "Value").set(restorer, "Deny", 1)
        self.assertEqual(backend.data[("Consent", "Value")], ("Deny", 1))
        restorer.restore_all()
        self.assertEqual(backend.data, {("Run", "Murmur"): ("C:\\owner\\murmur.exe --background", 1)})


class HandoffTests(unittest.TestCase):
    FILES = {
        "Murmur_0.5.0_x64-setup.exe": b"installer",
        "murmur-windows-x86_64.zip": b"portable",
        "murmur-engine-windows-x86_64-0.5.0.zip": b"engine",
        "murmur-cuda-windows-x86_64-0.5.0.zip": b"cuda",
        "components-manifest.json": b"{}",
        "components-manifest.json.sig": b"sig",
        "murmur-release.asc": b"cert",
        "components-windows-inputs.json": b"[]",
    }

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.source = self.work / "murmur-ci-out" / SHA
        self.source.mkdir(parents=True)
        for name, data in self.FILES.items():
            (self.source / name).write_bytes(data)
        self.sums = {name: digest(data) for name, data in self.FILES.items()}
        (self.source / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in sorted(self.sums.items())))

    def log(self, commit=SHA, sums=None):
        lines = ["build\tHand the build\t2026-11-09T10:00:00.0000000Z ##[group]Run $Commit = $env:GITHUB_SHA",
                 'build\tHand the build\t2026-11-09T10:00:00.1Z   Write-Host "MURMUR-HANDOFF-SHA256 $Commit $Hash $($File.Name)"']
        for name, value in sorted((sums or self.sums).items()):
            lines.append(f"build\tHand the build\t2026-11-09T10:00:01.0000000Z MURMUR-HANDOFF-SHA256 {commit} {value} {name}\r")
        return "\n".join(lines)

    def gh(self, runs, logs):
        def fake(*arguments):
            if arguments[0] == "api":
                self.assertIn(f"head_sha={SHA}", arguments[1])
                return json.dumps({"workflow_runs": runs})
            self.assertEqual(arguments[:2], ("run", "view"))
            return logs[int(arguments[2])]
        return fake

    def test_sha_must_be_the_dispatched_commit(self):
        self.assertEqual(handoff.check_sha(SHA.upper(), SHA), SHA)
        with self.assertRaisesRegex(handoff.HandoffError, "not the dispatched commit"):
            handoff.check_sha(OTHER, SHA)
        with self.assertRaisesRegex(handoff.HandoffError, "40-character"):
            handoff.check_sha("abc123", "abc123")
        with self.assertRaises(handoff.HandoffError):
            handoff.check_sha("", "")

    def test_sums_parsing_is_strict(self):
        self.assertEqual(handoff.parse_sums((self.source / "SHA256SUMS").read_text()), self.sums)
        for bad in (f"{'0' * 64} ../evil.exe\n", f"{'0' * 64}  sub/file\n", "zz  file\n", "",
                    f"{'0' * 64}  a\n{'1' * 64}  a\n"):
            with self.subTest(bad=bad), self.assertRaises(handoff.HandoffError):
                handoff.parse_sums(bad)

    def test_log_parsing_ignores_the_script_echo_and_other_commits(self):
        text = self.log() + "\n" + self.log(commit=OTHER, sums={"murmur-windows-x86_64.zip": "f" * 64})
        self.assertEqual(handoff.parse_log(text, SHA), self.sums)
        conflicting = self.log() + f"\nMURMUR-HANDOFF-SHA256 {SHA} {'f' * 64} murmur-windows-x86_64.zip"
        with self.assertRaisesRegex(handoff.HandoffError, "two different hashes"):
            handoff.parse_log(conflicting, SHA)

    def test_compare_and_required_files(self):
        handoff.compare(self.sums, dict(self.sums))
        with self.assertRaisesRegex(handoff.HandoffError, "no MURMUR-HANDOFF"):
            handoff.compare(self.sums, {})
        with self.assertRaisesRegex(handoff.HandoffError, "disagrees"):
            handoff.compare(self.sums, dict(self.sums, **{"murmur-release.asc": "0" * 64}))
        handoff.check_required(self.sums)
        with self.assertRaisesRegex(handoff.HandoffError, "Unexpected"):
            handoff.check_required(dict(self.sums, **{"evil.dll": "0" * 64}))
        missing = dict(self.sums)
        del missing["murmur-release.asc"]
        with self.assertRaisesRegex(handoff.HandoffError, "murmur-release.asc"):
            handoff.check_required(missing)

    def test_verify_copies_and_checks_against_the_matching_log(self):
        runs = [{"id": 2, "head_sha": SHA, "conclusion": "success", "run_started_at": "2026-11-09T10:00:00Z",
                 "html_url": "https://example.invalid/2", "event": "pull_request"},
                {"id": 1, "head_sha": SHA, "conclusion": "success", "run_started_at": "2026-11-08T10:00:00Z",
                 "html_url": "https://example.invalid/1", "event": "workflow_dispatch"}]
        stale = dict(self.sums, **{"murmur-windows-x86_64.zip": "0" * 64})
        dest = self.work / "copy"
        record = handoff.verify(SHA, SHA, self.source, dest, "o/r", self.gh(runs, {2: self.log(sums=stale), 1: self.log()}))
        self.assertEqual(record["build_run"]["id"], 1)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), sorted([*self.FILES, "build.json"]))
        self.assertEqual(json.loads((dest / "build.json").read_text())["sha"], SHA)

    def test_tampered_folder_is_refused(self):
        runs = [{"id": 1, "head_sha": SHA, "conclusion": "success", "event": "workflow_dispatch"}]
        fake = self.gh(runs, {1: self.log()})
        (self.source / "murmur-windows-x86_64.zip").write_bytes(b"tampered")
        with self.assertRaisesRegex(handoff.HandoffError, "murmur-windows-x86_64.zip: SHA-256"):
            handoff.verify(SHA, SHA, self.source, self.work / "copy", "o/r", fake)
        # Rewriting SHA256SUMS to match does not help: the build log disagrees.
        sums = dict(self.sums, **{"murmur-windows-x86_64.zip": digest(b"tampered")})
        (self.source / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in sorted(sums.items())))
        with self.assertRaisesRegex(handoff.HandoffError, "No build log matches"):
            handoff.verify(SHA, SHA, self.source, self.work / "copy", "o/r", fake)

    def test_extra_file_and_wrong_commit_are_refused(self):
        runs = [{"id": 1, "head_sha": SHA, "conclusion": "success"}]
        (self.source / "extra.dll").write_bytes(b"x")
        with self.assertRaisesRegex(handoff.HandoffError, "differs from SHA256SUMS"):
            handoff.verify(SHA, SHA, self.source, self.work / "copy", "o/r", self.gh(runs, {1: self.log()}))
        with self.assertRaisesRegex(handoff.HandoffError, "not the dispatched commit"):
            handoff.verify(SHA, OTHER, self.source, self.work / "copy", "o/r", self.gh(runs, {1: self.log()}))

    def test_no_successful_build_run(self):
        with self.assertRaisesRegex(handoff.HandoffError, "No successful"):
            handoff.matching_run("o/r", SHA, self.sums, self.gh([], {}))


class FixtureTests(unittest.TestCase):
    def test_padding_appends_silence_and_reports_speech_length(self):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work)
        source = work / "speech.wav"
        with wave.open(str(source), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(fixtures.RATE)
            writer.writeframes(b"\x01\x00" * fixtures.RATE * 2)
        speech = fixtures.pad_silence(source, work / "padded.wav", 20.0)
        self.assertAlmostEqual(speech, 2.0)
        self.assertAlmostEqual(fixtures.duration(work / "padded.wav"), 22.0)
        with wave.open(str(work / "padded.wav"), "rb") as reader:
            reader.setpos(fixtures.RATE * 2)
            self.assertEqual(set(reader.readframes(100)), {0})

    def test_padding_outlasts_the_longest_hold_and_stays_below_the_utterance_limit(self):
        self.assertGreater(fixtures.PAD_SECONDS, 15)
        self.assertLess(fixtures.PAD_SECONDS + 10, 90)

    def test_sentences_cover_the_checks(self):
        self.assertGreater(len(fixtures.SENTENCES["LONG"]), 60)
        self.assertIn("d", fixtures.SENTENCES["D"].lower())
        self.assertIn(fixtures.REPLACEMENT[0], fixtures.SENTENCES["MARKER"])
        self.assertNotEqual(fixtures.cache_name("A", "x"), fixtures.cache_name("A", "y"))

    def test_speak_script_escapes_quotes(self):
        script = fixtures.speak_script("It's here", Path("C:/t/o'k.wav"))
        self.assertIn("Speak('It''s here')", script)
        self.assertIn("o''k.wav", script)


class FakeContext:
    def __init__(self, work):
        self.restorer = restore.Restorer(log=lambda _: None)
        self.guard = guard.NoGuard()
        self.out = Path(work)
        self.artifacts, self.notes, self.check_id = [], [], ""

    def begin(self, check_id, attempt=1):
        self.check_id, self.artifacts, self.notes = check_id, [], []

    def artifact(self, suffix):
        path = self.out / f"{self.check_id}-{suffix}"
        self.artifacts.append(str(path))
        return path

    def note(self, text):
        self.notes.append(text)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.ctx = FakeContext(self.work)

    def run_with(self, behaviours, ids, repeat=1):
        undone = []

        def lookup(check):
            def function(ctx):
                ctx.restorer.push(f"undo {check.id}", lambda: undone.append(check.id))
                behaviours[check.id](ctx)
            return function

        with mock.patch.object(win32, "release_all") as release_all:
            results, aborted = run.run_checks(self.ctx, catalog.select(ids), repeat, lookup, log=lambda _: None)
        release_all.assert_called()
        return results, aborted, undone

    def test_statuses_and_per_check_restore(self):
        def fail(_):
            raise Fail("text differs")

        def skip(_):
            raise Skip("VS Code not installed")

        def crash(_):
            raise RuntimeError("boom")

        results, aborted, undone = self.run_with(
            {"H1": lambda c: None, "H2": fail, "I2": skip, "T1": crash, "I1": crash}, "H1,H2,I1,I2,T1")
        statuses = {r.id: (r.status, r.reason) for r in results}
        self.assertEqual(statuses["H1"], (report.PASS, ""))
        self.assertEqual(statuses["H2"], (report.FAIL, "text differs"))
        self.assertEqual(statuses["I2"], (report.SKIPPED, "VS Code not installed"))
        self.assertEqual(statuses["I1"][0], report.FAIL)
        self.assertEqual(statuses["T1"][0], report.SKIPPED)  # best effort: never a pass, never a crash
        self.assertIn("could not run", statuses["T1"][1])
        self.assertEqual(aborted, "")
        self.assertEqual(undone, ["H1", "H2", "I1", "I2", "T1"])
        self.assertTrue((self.work / "T1-harness-error.txt").is_file())

    def test_user_input_aborts_the_remaining_checks(self):
        def touched(_):
            raise guard.UserInput("real keyboard input")

        results, aborted, undone = self.run_with({"H1": lambda c: None, "H2": touched}, "H1,H2,H3", repeat=2)
        self.assertIn("user input", aborted)
        self.assertEqual([(r.id, r.status) for r in results],
                         [("H1", report.PASS), ("H2", report.ABORTED), ("H3", report.ABORTED),
                          ("H1", report.ABORTED), ("H2", report.ABORTED), ("H3", report.ABORTED)])
        self.assertEqual(undone, ["H1", "H2"])

    def test_lost_input_guard_aborts_without_claiming_real_user_activity(self):
        def lost(_):
            raise guard.GuardUnavailable("probe not observed")

        results, aborted, undone = self.run_with({"H1": lost}, "H1,H2")
        self.assertIn("input guard unavailable", aborted)
        self.assertNotIn("user input", aborted)
        self.assertEqual([r.status for r in results], [report.ABORTED, report.ABORTED])
        self.assertEqual(undone, ["H1"])

    def test_guard_loss_during_a_check_cannot_be_reported_as_pass(self):
        self.ctx.guard = SimpleNamespace(check=mock.Mock(side_effect=[None, guard.GuardUnavailable("probe lost")]))
        results, aborted, _ = self.run_with({"H1": lambda _: None}, "H1,H2")
        self.assertIn("input guard unavailable", aborted)
        self.assertEqual([r.status for r in results], [report.ABORTED, report.ABORTED])

    def test_p4_guard_loss_records_exact_handoff_and_preserves_launch_evidence(self):
        stages = []
        self.ctx.guard = SimpleNamespace(check=lambda: None, mark=stages.append)
        self.ctx.fixtures = {"A": SimpleNamespace(path=self.work / "a.wav")}

        def lost_in_ensure(_wav):
            raise guard.GuardUnavailable("probe lost during main session launch")

        def save_startup(artifact):
            artifact("startup-log.txt").write_text("main session starting\n", encoding="utf-8")

        self.ctx.murmur = SimpleNamespace(ensure=lost_in_ensure, save_startup_diagnostics=save_startup)
        with mock.patch.object(win32, "release_all"):
            results, aborted = run.run_checks(self.ctx, catalog.select("P4,S1"), 1,
                                               lambda check: setup_checks.p4_components if check.id == "P4"
                                               else lambda _: None, log=lambda _: None)
        self.assertEqual([r.status for r in results], [report.ABORTED, report.ABORTED])
        self.assertIn("input guard unavailable", aborted)
        self.assertEqual(stages, ["P4:1:begin", "P4:ensure:begin", "P4:1:guard-abort"])
        self.assertEqual((self.work / "P4-startup-log.txt").read_text(), "main session starting\n")
        self.assertIn(str(self.work / "P4-startup-log.txt"), results[0].artifacts)

    def test_startup_failure_preserves_evidence_and_stops_cascading_checks(self):
        session = murmur.MurmurSession(SimpleNamespace(), self.work, SimpleNamespace(), {})
        session.process = SimpleNamespace(pid=42, poll=lambda: 101)
        session._launch_output = self.work / "murmur-launch-1.txt"
        session._launch_output.write_bytes(b"panic from app\r\n")
        session.log_path.parent.mkdir(parents=True)
        session.log_path.write_bytes(b"old log\r\n")
        session._log_offset = session.log_path.stat().st_size
        with session.log_path.open("ab") as stream:
            stream.write(b"new launch error\r\n")

        def startup(_):
            raise murmur.StartupError("Murmur exited before its Settings window appeared (exit code 101)", session)

        with mock.patch.object(murmur.win32, "enum_windows", return_value=[(12, 42, "Murmur Overlay", "WebView")]), \
             mock.patch.object(murmur.win32, "user32", SimpleNamespace(IsWindowVisible=lambda _: False)), \
             mock.patch.object(win32, "IS_WINDOWS", True):
            results, blocked, undone = self.run_with({"P3": startup}, "P3,P4,S1")
        self.assertEqual([r.status for r in results], [report.FAIL, report.ABORTED, report.ABORTED])
        self.assertIn("P3 startup failure", blocked)
        self.assertEqual(undone, ["P3"])
        self.assertEqual((self.work / "P3-startup-output.txt").read_text(), "panic from app\n")
        self.assertEqual((self.work / "P3-startup-log.txt").read_text(), "new launch error\n")
        self.assertIn("Murmur Overlay", (self.work / "P3-startup-windows.txt").read_text())
        self.assertIn(str(self.work / "P3-startup-log.txt"), results[0].artifacts)

    def test_startup_diagnostics_preserve_crlf_and_replace_invalid_utf8(self):
        session = murmur.MurmurSession(SimpleNamespace(), self.work, SimpleNamespace(), {})
        session._launch_output = self.work / "murmur-launch-1.txt"
        session._launch_output.write_bytes(b"panic from app\r\ninvalid: \xff\r\n")
        session.log_path.parent.mkdir(parents=True)
        session.log_path.write_bytes(b"old log\r\n")
        session._log_offset = session.log_path.stat().st_size
        with session.log_path.open("ab") as stream:
            stream.write(b"new launch error\r\ninvalid: \xff\r\n")

        def windows_write_text(path, data, encoding=None):
            # Simulate Windows text-mode newline translation on any host OS.
            return path.write_bytes(data.replace("\n", "\r\n").encode(encoding or "utf-8"))

        with mock.patch.object(Path, "write_text", windows_write_text), \
             mock.patch.object(murmur.win32, "enum_windows", return_value=[]):
            session.save_startup_diagnostics(lambda suffix: self.work / suffix)

        replacement = "\ufffd".encode("utf-8")
        self.assertEqual((self.work / "startup-output.txt").read_bytes(),
                         b"panic from app\r\ninvalid: " + replacement + b"\r\n")
        self.assertEqual((self.work / "startup-log.txt").read_bytes(),
                         b"new launch error\r\ninvalid: " + replacement + b"\r\n")

    def test_startup_window_wait_reports_early_process_exit(self):
        session = murmur.MurmurSession(SimpleNamespace(), self.work, SimpleNamespace(sleep=lambda _: None), {})
        session.process = SimpleNamespace(poll=lambda: 101)
        with mock.patch.object(murmur.win32, "wait_for", side_effect=lambda predicate, *_args, **_kwargs: predicate()):
            with self.assertRaisesRegex(murmur.StartupError, "exit code 101") as raised:
                session._wait_for_window(lambda: 0, "Settings window")
        self.assertIs(raised.exception.session, session)

    def test_restart_marks_boundary_and_stays_fail_closed(self):
        stages = []

        def sleep(_seconds):
            stages.append("guard-sleep")
            raise guard.GuardUnavailable("probe lost")

        session = murmur.MurmurSession.__new__(murmur.MurmurSession)
        session.guard = SimpleNamespace(mark=stages.append, sleep=sleep)
        session.stop = mock.Mock(side_effect=lambda: stages.append("stop"))
        session.launch = mock.Mock()
        with self.assertRaises(guard.GuardUnavailable):
            session.restart()
        self.assertEqual(stages, ["Murmur:restart-stop-begin", "stop",
                                  "Murmur:restart-stop-complete", "guard-sleep"])
        session.launch.assert_not_called()

    def test_launch_env_requests_backtrace_in_the_isolated_profile(self):
        session = murmur.MurmurSession(SimpleNamespace(certificate=self.work / "ci.asc"), self.work,
                                       SimpleNamespace(), {})
        env = session.env()
        self.assertEqual(env["RUST_BACKTRACE"], "1")
        self.assertEqual(env["APPDATA"], str(session.appdata))
        self.assertEqual(env["LOCALAPPDATA"], str(session.localappdata))

    def test_preflight_reports_every_problem(self):
        class FakeWin32:
            def input_desktop(self):
                return None

            def pids(self, name):
                return {"murmur.exe": [42], "Notepad.exe": [7]}.get(name, [])

            def idle_seconds(self):
                return 5.0

        problems = run.preflight(self.work, catalog.select("H1"), FakeWin32())
        text = "\n".join(problems)
        for expected in ("not a verified build", "locked", "another Murmur", "Notepad is running", "idle for 60"):
            self.assertIn(expected, text)
        (self.work / "build.json").write_text("{}")
        self.assertEqual(len(run.preflight(self.work, catalog.select("F1"), FakeWin32())), 3)

    def test_guard_diagnostic_preflight_needs_no_build_and_refuses_busy_desktop(self):
        fake = SimpleNamespace(input_desktop=lambda: "Default", pids=lambda _: [], idle_seconds=lambda: 61.0)
        self.assertEqual(run.preflight_desktop(fake), [])
        fake.idle_seconds = lambda: 5.0
        problems = run.preflight_desktop(fake)
        with mock.patch.object(guard_diagnostic.win32, "require_windows"), \
             mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=problems):
            result = guard_diagnostic.diagnose(1)
        self.assertEqual(result["status"], "preflight_refused")
        self.assertIn("idle for 60", "\n".join(result["problems"]))

    def test_guard_diagnostic_report_includes_bounded_python_identity_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "guard-diagnostic.json"
            with mock.patch.object(guard_diagnostic, "diagnose", return_value={"status": "preflight_refused"}) as diagnose:
                exit_code = guard_diagnostic.main(["--out", str(output), "--seconds", "1"])
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 1)
        diagnose.assert_called_once_with(1)
        self.assertEqual(payload["python"], {
            "executable": sys.executable,
            "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "prefix": sys.prefix,
        })


class ManualTests(unittest.TestCase):
    def test_answers_items_and_results(self):
        self.assertEqual(manual.parse_answer(" P "), report.PASS)
        self.assertEqual(manual.parse_answer("fail"), report.FAIL)
        self.assertIsNone(manual.parse_answer("maybe"))
        self.assertEqual([i for i, _ in manual.items("k2,s2")], ["S2", "K2"])
        with self.assertRaises(catalog.SelectionError):
            manual.items("H1")
        self.assertEqual(set(manual.STEPS), {i for i, _ in catalog.MANUAL})
        failed = manual.manual_result("K2", "Narrator", report.FAIL, "")
        self.assertEqual((failed.reason, failed.source), ("no note given", "manual"))
        passed = manual.manual_result("K3", "Motion", report.PASS, "reduced")
        self.assertEqual((passed.reason, passed.notes), ("", ["reduced"]))


if __name__ == "__main__":
    unittest.main()


class GuardMouseTests(unittest.TestCase):
    """The observer's pure decision behind the low-level mouse hook."""

    def test_injected_input_never_trips(self):
        g = guard._HookObserver()
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, True, (0, 0)))
        self.assertIsNone(g.mouse_event(0x0201, True, (0, 0)))  # WM_LBUTTONDOWN, injected

    def test_real_button_trips(self):
        self.assertEqual(guard._HookObserver().mouse_event(0x0201, False, (5, 5)), "real mouse button or wheel input")

    def test_real_drift_within_tolerance_is_ignored_and_beyond_trips(self):
        g = guard._HookObserver()
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, False, (100, 100)))
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, False, (102, 99)))
        self.assertEqual(g.mouse_event(guard.WM_MOUSEMOVE, False, (110, 100)), "real mouse movement")

    def test_injected_move_reanchors_the_origin(self):
        # Real jitter at the old place, the harness moves the cursor far away, then a pixel of real
        # jitter where the cursor now is: not a person.
        g = guard._HookObserver()
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, False, (100, 100)))
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, True, (900, 700)))
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, False, (901, 700)))
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, False, (902, 701)))
        self.assertEqual(g.mouse_event(guard.WM_MOUSEMOVE, False, (920, 701)), "real mouse movement")

    def test_probe_is_a_reserved_key_with_a_tag(self):
        self.assertEqual(guard.PROBE_VK, 0x88)  # reserved virtual-key code
        self.assertLess(guard.PROBE_TIMEOUT, guard.PROBE_INTERVAL)

    def test_desktop_context_reads_hook_thread_not_calling_thread(self):
        def session_id(_pid, out):
            win32.ctypes.cast(out, win32.ctypes.POINTER(win32.wintypes.DWORD)).contents.value = 9
            return True

        def object_info(handle, index, out, _size, _needed):
            if index == 6:
                win32.ctypes.cast(out, win32.ctypes.POINTER(win32.wintypes.BOOL)).contents.value = 1
            else:
                name = "WinSta0" if handle == 10 else "Default"
                buffer = win32.ctypes.create_unicode_buffer(name)
                win32.ctypes.memmove(out, buffer, win32.ctypes.sizeof(buffer))
            return True

        get_thread_desktop = mock.Mock(return_value=20)
        fake_user32 = SimpleNamespace(GetProcessWindowStation=lambda: 10,
                                      GetThreadDesktop=get_thread_desktop,
                                      GetUserObjectInformationW=object_info,
                                      GetForegroundWindow=lambda: 30)
        fake_kernel32 = SimpleNamespace(GetCurrentThreadId=lambda: 77, ProcessIdToSessionId=session_id)
        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(win32, "user32", fake_user32), \
             mock.patch.object(win32, "kernel32", fake_kernel32):
            context = win32.desktop_context(123)
        get_thread_desktop.assert_called_once_with(123)
        self.assertEqual(context, {"session_id": 9, "window_station": "WinSta0",
                                   "thread_desktop": "Default", "thread_desktop_receives_input": True,
                                   "foreground_window_present": True})

    def test_hook_context_diagnostic_survives_observer_stop(self):
        watcher = guard._HookObserver(diagnostic=True)
        watcher._desktop_context = {"window_station": "WinSta0",
                                    "thread_desktop_receives_input": True}
        with mock.patch.object(win32, "input_desktop", return_value="Default"):
            state = watcher.diagnostics()["hook_state"]["desktop_context"]
        self.assertEqual(state, watcher._desktop_context)

    def test_full_run_mark_snapshots_probe_state_without_clearing_guard_loss(self):
        watcher = guard.Guard(diagnostic=True)
        watcher._started_at = guard.time.monotonic()
        watcher._process = SimpleNamespace(is_alive=lambda: True)
        watcher._diagnostics = {"probes_seen": 21,
                                "hook_state": {"keyboard_callbacks": 21, "heartbeat_received": 21}}
        watcher.mark("P4:ensure:begin")
        watcher._trip("probe lost", unavailable=True)
        watcher.mark("P4:1:guard-abort")
        timeline = watcher.diagnostics()["timeline"]
        self.assertEqual([point["stage"] for point in timeline], ["P4:ensure:begin", "P4:1:guard-abort"])
        self.assertEqual([point["probes_seen"] for point in timeline], [21, 21])
        self.assertEqual([point["tripped"] for point in timeline], [False, True])
        with self.assertRaises(guard.GuardUnavailable):
            watcher.check()

    def test_diagnostic_records_only_injected_probe_candidates_and_exact_match(self):
        watcher = guard._HookObserver(diagnostic=True)
        watcher._expected_probe_tag = guard.PROBE_TAG | (2 << 32)
        real = SimpleNamespace(vkCode=0x41, scanCode=30, flags=0, dwExtraInfo=0)
        unrelated = SimpleNamespace(vkCode=0x41, scanCode=30, flags=guard.LLKHF_INJECTED,
                                    dwExtraInfo=0)
        wrong_tag = SimpleNamespace(vkCode=guard.PROBE_VK, scanCode=0, flags=guard.LLKHF_INJECTED,
                                    dwExtraInfo=guard.PROBE_TAG | (1 << 32))
        exact = SimpleNamespace(vkCode=guard.PROBE_VK, scanCode=0, flags=guard.LLKHF_INJECTED,
                                dwExtraInfo=watcher._expected_probe_tag)
        for event in (real, unrelated, wrong_tag, exact):
            watcher._record_injected_key(0x101, event)
        self.assertEqual(watcher._injected_key_events, 3)
        self.assertEqual([item["matches_expected"] for item in watcher._probe_events], [False, True])
        self.assertNotIn("0x41", str(watcher._probe_events))

    def test_h8_trace_records_injected_events_only_and_stops_cleanly(self):
        watcher = guard._HookObserver(diagnostic=True)
        event = lambda flags: SimpleNamespace(vkCode=guard.VK_PACKET, scanCode=ord("b"),
                                              flags=flags, dwExtraInfo=0)
        watcher._record_injected_key(guard.WM_KEYDOWN, event(guard.LLKHF_INJECTED))
        watcher.start_key_trace()
        watcher._record_injected_key(guard.WM_KEYDOWN, event(0))
        watcher._record_injected_key(guard.WM_KEYDOWN, event(guard.LLKHF_INJECTED))
        watcher._record_injected_key(guard.WM_KEYUP, event(guard.LLKHF_INJECTED | guard.LLKHF_UP))
        trace = watcher.stop_key_trace()
        self.assertEqual(len(trace["events"]), 2)
        summary = hotkey_checks._key_trace_summary(trace, "b")
        self.assertEqual(summary["packet_text"], "b")
        self.assertTrue(summary["packet_pairs_match"])
        self.assertIsNone(summary["packet_difference"])
        watcher._record_injected_key(guard.WM_KEYDOWN, event(guard.LLKHF_INJECTED))
        self.assertEqual(len(trace["events"]), 2)
        watcher.start_key_trace()
        for _ in range(513):
            watcher._record_injected_key(guard.WM_KEYDOWN, event(guard.LLKHF_INJECTED))
        capped = watcher.stop_key_trace()
        self.assertEqual(len(capped["events"]), 512)
        self.assertTrue(capped["overflow"])

    def test_diagnostic_mouse_probe_has_no_displacement_or_button_action(self):
        watcher = guard._HookObserver(diagnostic=True)
        tag = guard.PROBE_TAG | (1 << 32)
        send = mock.Mock(return_value=1)
        with mock.patch.object(win32, "user32", SimpleNamespace(SendInput=send)):
            watcher._send_mouse_probe(tag)
        count, events, size = send.call_args.args
        self.assertEqual((count, size), (1, guard.ctypes.sizeof(win32.INPUT)))
        self.assertEqual(events[0].type, win32.INPUT_MOUSE)
        self.assertEqual((events[0].mi.dx, events[0].mi.dy, events[0].mi.mouseData), (0, 0, 0))
        self.assertEqual(events[0].mi.dwFlags,
                         win32.MOUSEEVENTF_MOVE | win32.MOUSEEVENTF_MOVE_NOCOALESCE)
        self.assertEqual(events[0].mi.dwExtraInfo, tag)

    def test_owned_probe_sends_unicode_pair_only_to_verified_foreground_owner(self):
        watcher = guard._HookObserver(diagnostic=True, owned_probe_target=(55, 123))
        tag = guard.PROBE_TAG | (1 << 32)
        send = mock.Mock(return_value=2)

        def window_owner(_hwnd, out):
            guard.ctypes.cast(out, guard.ctypes.POINTER(guard.wintypes.DWORD)).contents.value = 123
            return 1

        user32 = SimpleNamespace(GetWindowThreadProcessId=window_owner,
                                 GetForegroundWindow=mock.Mock(return_value=55), SendInput=send)
        with mock.patch.object(win32, "user32", user32):
            self.assertTrue(watcher._send_owned_probe(tag))
            count, events, size = send.call_args.args
            self.assertEqual((count, size), (2, guard.ctypes.sizeof(win32.INPUT)))
            self.assertEqual([(event.type, event.ki.wVk, event.ki.wScan, event.ki.dwFlags,
                               event.ki.dwExtraInfo) for event in events], [
                (win32.INPUT_KEYBOARD, 0, guard.OWNED_PROBE_CHAR, win32.KEYEVENTF_UNICODE, tag),
                (win32.INPUT_KEYBOARD, 0, guard.OWNED_PROBE_CHAR,
                 win32.KEYEVENTF_UNICODE | win32.KEYEVENTF_KEYUP, tag)])
            send.reset_mock()
            user32.GetForegroundWindow.return_value = 77
            self.assertFalse(watcher._send_owned_probe(tag))
            send.assert_not_called()
        self.assertEqual(watcher._owned_probe_status, "owner_or_focus_lost")

    def test_owned_probe_does_not_send_after_real_input(self):
        watcher = guard._HookObserver(diagnostic=True, owned_probe_target=(55, 123))
        watcher._trip("real keyboard input")

        def window_owner(_hwnd, out):
            guard.ctypes.cast(out, guard.ctypes.POINTER(guard.wintypes.DWORD)).contents.value = 123
            return 1

        send = mock.Mock()
        user32 = SimpleNamespace(GetWindowThreadProcessId=window_owner,
                                 GetForegroundWindow=lambda: 55, SendInput=send)
        with mock.patch.object(win32, "user32", user32):
            self.assertFalse(watcher._send_owned_probe(guard.PROBE_TAG | (1 << 32)))
        send.assert_not_called()
        self.assertEqual(watcher._owned_probe_status, "real_input")

    def test_partial_owned_probe_attempts_key_release_then_fails_closed(self):
        watcher = guard._HookObserver(diagnostic=True, owned_probe_target=(55, 123))

        def window_owner(_hwnd, out):
            guard.ctypes.cast(out, guard.ctypes.POINTER(guard.wintypes.DWORD)).contents.value = 123
            return 1

        send = mock.Mock(side_effect=[1, 1])
        user32 = SimpleNamespace(GetWindowThreadProcessId=window_owner,
                                 GetForegroundWindow=lambda: 55, SendInput=send)
        with mock.patch.object(win32, "user32", user32), \
             mock.patch.object(guard.ctypes, "get_last_error", return_value=5, create=True):
            self.assertFalse(watcher._send_owned_probe(guard.PROBE_TAG | (1 << 32)))
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.args[1][0].ki.dwFlags,
                         win32.KEYEVENTF_UNICODE | win32.KEYEVENTF_KEYUP)
        self.assertEqual(watcher._owned_probe_release_result, 1)
        self.assertEqual(watcher._owned_probe_status, "send_failed")

    def test_owned_probe_callbacks_separate_valid_event_from_reserved_key_loss(self):
        watcher = guard._HookObserver(diagnostic=True, owned_probe_target=(55, 123))
        tag = guard.PROBE_TAG | (1 << 32)

        def valid_probe(value):
            watcher._owned_probe_tag = value
            for message in (guard.WM_KEYDOWN, guard.WM_KEYUP):
                event = SimpleNamespace(vkCode=guard.VK_PACKET, scanCode=guard.OWNED_PROBE_CHAR,
                                        flags=guard.LLKHF_INJECTED, dwExtraInfo=value)
                watcher._record_injected_key(message, event)
            return True

        with mock.patch.object(guard, "PROBE_TIMEOUT", 0.001), \
             mock.patch.object(watcher, "_post_heartbeat"), \
             mock.patch.object(watcher, "_send_mouse_probe"), \
             mock.patch.object(watcher, "_send_owned_probe", side_effect=valid_probe), \
             mock.patch.object(watcher, "_send_probe", return_value=True):
            watcher._watch()
        with mock.patch.object(win32, "input_desktop", return_value="Default"):
            state = watcher.diagnostics()["hook_state"]
        self.assertEqual(state["owned_probe_status"], "both_hook_callbacks")
        self.assertEqual(state["owned_probe_messages"], [guard.WM_KEYDOWN, guard.WM_KEYUP])
        self.assertEqual(watcher.probes_seen, 0)
        with self.assertRaises(guard.GuardUnavailable):
            watcher.check()

    def test_diagnostic_probe_adds_evidence_but_still_fails_closed(self):
        watcher = guard._HookObserver(diagnostic=True)
        with mock.patch.object(guard, "PROBE_TIMEOUT", 0.001), \
             mock.patch.object(watcher, "_post_heartbeat") as heartbeat, \
             mock.patch.object(watcher, "_send_mouse_probe") as mouse_probe, \
             mock.patch.object(watcher, "_send_probe", return_value=True) as key_probe:
            watcher._watch()
        heartbeat.assert_called_once()
        mouse_probe.assert_called_once_with(key_probe.call_args.args[0])
        with self.assertRaises(guard.GuardUnavailable):
            watcher.check()
        self.assertEqual(watcher.probes_seen, 0)

    def test_missed_probe_records_hook_and_post_send_pump_state(self):
        watcher = guard._HookObserver(diagnostic=True)
        watcher._keyboard_callbacks = 26
        watcher._last_keyboard_callback_at = guard.time.monotonic()
        watcher._thread = SimpleNamespace(is_alive=lambda: True)
        order = []

        def send(_tag):
            order.append("send")
            return True

        def heartbeat():
            order.append("heartbeat")
            watcher._heartbeat_posted += 1
            watcher._heartbeat_received += 1
            watcher._last_heartbeat_at = guard.time.monotonic()

        with mock.patch.object(guard, "PROBE_TIMEOUT", 0.001), \
             mock.patch.object(watcher, "_send_mouse_probe"), \
             mock.patch.object(watcher, "_send_probe", side_effect=send), \
             mock.patch.object(watcher, "_post_heartbeat", side_effect=heartbeat), \
             mock.patch.object(win32, "input_desktop", return_value="Default"):
            watcher._watch()
            state = watcher.diagnostics()["hook_state"]["missed_probe"]
        self.assertEqual(order, ["send", "heartbeat"])
        self.assertEqual(state["sequence"], 1)
        self.assertEqual(state["keyboard_callbacks_before_send"], 26)
        self.assertEqual(state["keyboard_callbacks_at_timeout"], 26)
        self.assertEqual(state["heartbeat_posted_at_timeout"], 1)
        self.assertEqual(state["heartbeat_received_at_timeout"], 1)
        self.assertTrue(state["hook_thread_alive"])
        self.assertIsInstance(state["last_keyboard_callback_age_ms"], int)
        self.assertIsInstance(state["last_heartbeat_age_ms"], int)
        with self.assertRaises(guard.GuardUnavailable):
            watcher.check()

    def test_diagnostic_does_not_send_key_after_real_input_during_mouse_probe(self):
        watcher = guard._HookObserver(diagnostic=True)

        def real_input(_tag):
            watcher._trip("real mouse movement")

        with mock.patch.object(watcher, "_post_heartbeat"), \
             mock.patch.object(watcher, "_send_mouse_probe", side_effect=real_input), \
             mock.patch.object(watcher, "_send_probe") as key_probe:
            watcher._watch()
        key_probe.assert_not_called()
        with self.assertRaises(guard.UserInput):
            watcher.check()

    def test_diagnostic_redacts_real_key_reason(self):
        with mock.patch.object(guard_diagnostic.win32, "require_windows"), \
             mock.patch.object(guard_diagnostic.win32, "desktop_context",
                               return_value={"window_station": "WinSta0"}), \
             mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=[]), \
             mock.patch.object(guard_diagnostic, "_wait_for_safe_desktop", return_value=("ready", [])), \
             mock.patch.object(guard_diagnostic, "_OwnedProbeWindow") as window_factory, \
             mock.patch.object(guard_diagnostic.guard, "Guard") as factory:
            window_factory.return_value.hwnd = 55
            window_factory.return_value.open.return_value = True
            instance = factory.return_value
            instance.start.side_effect = guard.UserInput("real keyboard input (virtual key 0x41)")
            instance.diagnostics.return_value = {"probes_seen": 0}
            result = guard_diagnostic.diagnose(1)
        self.assertEqual(result["status"], "real_input")
        self.assertEqual(result["parent_desktop_context"]["window_station"], "WinSta0")
        self.assertNotIn("0x41", str(result))
        instance.stop.assert_called_once()
        window_factory.return_value.close.assert_called_once()
        factory.assert_called_once_with(diagnostic=True, owned_probe_target=(55, guard_diagnostic.os.getpid()))

    def test_diagnostic_refuses_to_inject_when_own_window_cannot_take_focus(self):
        with mock.patch.object(guard_diagnostic.win32, "require_windows"), \
             mock.patch.object(guard_diagnostic.win32, "desktop_context", return_value={}), \
             mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=[]), \
             mock.patch.object(guard_diagnostic, "_OwnedProbeWindow") as window_factory, \
             mock.patch.object(guard_diagnostic.guard, "Guard") as factory:
            window_factory.return_value.open.return_value = False
            window_factory.return_value.diagnostics.return_value = {"status": "focus_denied"}
            result = guard_diagnostic.diagnose(1)
        self.assertEqual(result["status"], "probe_window_unavailable")
        factory.assert_not_called()

    def test_diagnostic_waits_for_user_focus_and_60_seconds_idle(self):
        window = SimpleNamespace(status="waiting_for_focus",
                                 focused=mock.Mock(side_effect=[False, False, True, True, True, True]))
        with mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=[]) as preflight, \
             mock.patch.object(guard_diagnostic.win32, "idle_seconds", side_effect=[0, 60]), \
             mock.patch.object(guard_diagnostic.time, "monotonic", return_value=0), \
             mock.patch.object(guard_diagnostic.time, "sleep"):
            self.assertEqual(guard_diagnostic._wait_for_safe_desktop(window), ("ready", []))
        self.assertEqual(preflight.call_args, mock.call(guard_diagnostic.win32))
        self.assertEqual(preflight.call_count, 5)

    def test_diagnostic_focus_wait_times_out_without_guard_or_input(self):
        with mock.patch.object(guard_diagnostic.win32, "require_windows"), \
             mock.patch.object(guard_diagnostic.win32, "desktop_context", return_value={}), \
             mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=[]), \
             mock.patch.object(guard_diagnostic._OwnedProbeWindow, "open", return_value=True), \
             mock.patch.object(guard_diagnostic._OwnedProbeWindow, "focused", return_value=False), \
             mock.patch.object(guard_diagnostic._OwnedProbeWindow, "close"), \
             mock.patch.object(guard_diagnostic.time, "monotonic",
                               side_effect=[0, guard_diagnostic.FOCUS_WAIT_SECONDS]), \
             mock.patch.object(guard_diagnostic.guard, "Guard") as factory:
            result = guard_diagnostic.diagnose(1)
        self.assertEqual(result["status"], "probe_window_unavailable")
        self.assertEqual(result["probe_window"]["status"], "focus_timeout")
        factory.assert_not_called()

    def test_diagnostic_focus_loss_during_idle_never_starts_guard(self):
        with mock.patch.object(guard_diagnostic.win32, "require_windows"), \
             mock.patch.object(guard_diagnostic.win32, "desktop_context", return_value={}), \
             mock.patch.object(guard_diagnostic.win32, "idle_seconds", return_value=0), \
             mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=[]), \
             mock.patch.object(guard_diagnostic._OwnedProbeWindow, "open", return_value=True), \
             mock.patch.object(guard_diagnostic._OwnedProbeWindow, "focused", side_effect=[True, True, False]), \
             mock.patch.object(guard_diagnostic._OwnedProbeWindow, "close"), \
             mock.patch.object(guard_diagnostic.time, "monotonic", return_value=0), \
             mock.patch.object(guard_diagnostic.time, "sleep"), \
             mock.patch.object(guard_diagnostic.guard, "Guard") as factory:
            result = guard_diagnostic.diagnose(1)
        self.assertEqual(result["status"], "focus_lost")
        factory.assert_not_called()

    def test_diagnostic_idle_timeout_never_starts_guard(self):
        with mock.patch.object(guard_diagnostic.win32, "require_windows"), \
             mock.patch.object(guard_diagnostic.win32, "desktop_context", return_value={}), \
             mock.patch.object(guard_diagnostic.win32, "idle_seconds", return_value=0), \
             mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=[]), \
             mock.patch.object(guard_diagnostic._OwnedProbeWindow, "open", return_value=True), \
             mock.patch.object(guard_diagnostic._OwnedProbeWindow, "focused", return_value=True), \
             mock.patch.object(guard_diagnostic._OwnedProbeWindow, "close"), \
             mock.patch.object(guard_diagnostic.time, "monotonic",
                               side_effect=[0, 0, guard_diagnostic.IDLE_WAIT_SECONDS]), \
             mock.patch.object(guard_diagnostic.guard, "Guard") as factory:
            result = guard_diagnostic.diagnose(1)
        self.assertEqual(result["status"], "idle_timeout")
        factory.assert_not_called()

    def test_diagnostic_rechecks_focus_immediately_before_guard_start(self):
        with mock.patch.object(guard_diagnostic.win32, "require_windows"), \
             mock.patch.object(guard_diagnostic.win32, "desktop_context", return_value={}), \
             mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=[]), \
             mock.patch.object(guard_diagnostic, "_wait_for_safe_desktop", return_value=("ready", [])), \
             mock.patch.object(guard_diagnostic._OwnedProbeWindow, "open", return_value=True), \
             mock.patch.object(guard_diagnostic._OwnedProbeWindow, "focused", return_value=False), \
             mock.patch.object(guard_diagnostic._OwnedProbeWindow, "close"), \
             mock.patch.object(guard_diagnostic.guard, "Guard") as factory:
            result = guard_diagnostic.diagnose(1)
        self.assertEqual(result["status"], "focus_lost")
        factory.return_value.start.assert_not_called()

    def test_missing_liveness_probe_trips_without_reenabling_input(self):
        watcher = guard._HookObserver()
        with mock.patch.object(guard, "PROBE_INTERVAL", 0.001), \
             mock.patch.object(guard, "PROBE_TIMEOUT", 0.001), \
             mock.patch.object(watcher, "_send_probe", return_value=True) as send:
            watcher._watch()
        with self.assertRaises(guard.GuardUnavailable):
            watcher.check()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0] & 0xFFFFFFFF, guard.PROBE_TAG)
        self.assertEqual(watcher.diagnostics()["probes_seen"], 0)

    def test_first_probe_is_sent_before_the_interval(self):
        watcher = guard._HookObserver()

        def observed(_tag):
            watcher._probe_seen.set()
            return True

        with mock.patch.object(guard, "PROBE_INTERVAL", 10), \
             mock.patch.object(watcher, "_send_probe", side_effect=observed) as send:
            thread = guard.threading.Thread(target=watcher._watch)
            thread.start()
            try:
                self.assertTrue(watcher._first_probe_done.wait(0.2))
                send.assert_called_once()
            finally:
                watcher._stopping.set()
                thread.join(1)


class GuardProcessTests(unittest.TestCase):
    """The parent must not inject after the observer reports input or loses contact."""

    def setUp(self):
        self.parent, self.child = multiprocessing.Pipe()
        self.guard = guard.Guard()
        self.guard._connection = self.parent
        self.alive = True
        self.guard._process = SimpleNamespace(is_alive=lambda: self.alive)
        self.addCleanup(self.parent.close)
        self.addCleanup(self.child.close)

    def test_probe_diagnostics_do_not_mask_real_input(self):
        self.child.send(("ready", {"probes_seen": 21}))
        self.child.send(("user_input", "real keyboard input"))
        self.child.send(("diagnostics", {"probes_seen": 22}))
        with self.assertRaisesRegex(guard.UserInput, "real keyboard input"):
            self.guard.check()
        self.assertFalse(self.guard.unavailable)
        self.assertEqual(self.guard.diagnostics()["probes_seen"], 22)

    def test_missed_probe_is_fail_closed(self):
        self.child.send(("unavailable", "probe not observed"))
        with self.assertRaisesRegex(guard.GuardUnavailable, "probe not observed"):
            self.guard.check()
        self.assertTrue(self.guard.unavailable)

    def test_silent_observer_death_is_fail_closed(self):
        self.alive = False
        with self.assertRaisesRegex(guard.GuardUnavailable, "observer stopped"):
            self.guard.check()

    def test_pipe_loss_is_fail_closed_even_if_process_appears_alive(self):
        self.child.close()
        with self.assertRaisesRegex(guard.GuardUnavailable, "connection closed"):
            self.guard.check()

    def test_h8_trace_commands_wait_for_observer_acknowledgement(self):
        self.guard._diagnostic = True
        events = [[1, guard.WM_KEYDOWN, guard.VK_PACKET, ord("b"), guard.LLKHF_INJECTED]]

        def observer():
            self.assertEqual(self.child.recv(), "trace_start")
            self.child.send(("key_trace", {"started": True}))
            self.assertEqual(self.child.recv(), "trace_stop")
            self.child.send(("key_trace", {"events": events, "overflow": False}))

        peer = guard.threading.Thread(target=observer)
        peer.start()
        self.guard.start_key_trace()
        self.assertEqual(self.guard.stop_key_trace()["events"], events)
        peer.join(1)
        self.assertFalse(peer.is_alive())

    def test_h8_trace_ack_timeout_aborts_guard(self):
        self.guard._diagnostic = True
        with mock.patch.object(self.guard._trace_ready, "wait", return_value=False), \
             mock.patch.object(guard, "time", SimpleNamespace(monotonic=mock.Mock(side_effect=[0, 3]))):
            with self.assertRaisesRegex(guard.GuardUnavailable, "did not acknowledge"):
                self.guard.start_key_trace()
        self.assertTrue(self.guard.unavailable)

    def test_spawn_handshake_and_clean_stop(self):
        spawned = guard.Guard()
        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(guard, "_guard_worker", fake_observer_worker):
            try:
                spawned.start()
                self.assertIs(win32._before_input.__self__, spawned)
                spawned.check()
            finally:
                spawned.stop()
        self.assertIsNone(win32._before_input)
        self.assertEqual(spawned.diagnostics()["probes_seen"], 2)

    def test_diagnostic_flag_reaches_isolated_worker(self):
        spawned = guard.Guard(diagnostic=True)
        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(guard, "_guard_worker", fake_diagnostic_worker):
            try:
                spawned.start()
                self.assertTrue(spawned.diagnostics()["diagnostic"])
            finally:
                spawned.stop()
        self.assertEqual(spawned.diagnostics()["probes_seen"], 2)

    def test_owned_probe_target_reaches_isolated_worker(self):
        spawned = guard.Guard(diagnostic=True, owned_probe_target=(55, 123))
        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(guard, "_guard_worker", fake_owned_probe_worker):
            try:
                spawned.start()
                self.assertEqual(spawned.diagnostics()["target"], (55, 123))
            finally:
                spawned.stop()

    def test_owned_probe_target_can_be_set_at_guard_start_after_profile_preparation(self):
        spawned = guard.Guard(diagnostic=True)
        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(guard, "_guard_worker", fake_owned_probe_worker):
            try:
                spawned.start(owned_probe_target=(55, 123))
                self.assertEqual(spawned.diagnostics()["target"], (55, 123))
            finally:
                spawned.stop()
        with mock.patch.object(win32, "require_windows"), \
             self.assertRaisesRegex(ValueError, "requires a diagnostic guard"):
            guard.Guard().start(owned_probe_target=(55, 123))

    def test_real_input_before_first_probe_aborts_start(self):
        spawned = guard.Guard()
        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(guard, "_guard_worker", fake_early_input_worker):
            with self.assertRaisesRegex(guard.UserInput, "real keyboard input during startup"):
                spawned.start()
        self.assertIsNone(win32._before_input)

    def test_worker_preserves_real_input_during_initial_probe(self):
        class TouchedObserver:
            tripped = guard.threading.Event()
            reason = "real mouse button or wheel input"
            unavailable = False

            def start(self):
                self.tripped.set()
                raise RuntimeError("first probe interrupted")

            def stop(self):
                pass

            def diagnostics(self):
                return {"probes_seen": 0}

        with mock.patch.object(guard, "_HookObserver", TouchedObserver):
            guard._guard_worker(self.child)
        self.assertEqual(self.parent.recv(), ("user_input", "real mouse button or wheel input"))


class OwnedProbeWindowTests(unittest.TestCase):
    def test_non_h8_start_uses_original_guard_without_owned_window(self):
        observer = mock.Mock()
        with mock.patch.object(guard_diagnostic, "_OwnedProbeWindow") as window_factory:
            run.start_input_guard(observer, catalog.select("H1"), mock.Mock(), {})
        observer.start.assert_called_once_with()
        window_factory.assert_not_called()

    def test_h1_first_and_h8_only_keep_one_guard_and_window_until_target_focus(self):
        for selection in ("H1,H8", "H8", "O8"):
            with self.subTest(selection=selection):
                self._assert_startup_lease(selection)

    def _assert_startup_lease(self, selection):
        events = []
        observer = mock.Mock()
        observer.start.side_effect = lambda **_kwargs: events.append("guard_start")
        observer.diagnostics.return_value = {"probes_seen": 1, "hook_state": {
            "owned_probe_status": "both_hook_callbacks", "owned_probe_messages": [256, 257]}}
        window = mock.Mock(hwnd=55, restoration="not_started")
        window.open.return_value = True
        window.focused.return_value = True
        window.enter_passive.side_effect = lambda: events.append("passive") or True
        window.close.side_effect = lambda **_kwargs: events.append("window_close") or True
        window.diagnostics.return_value = {"status": "passive", "restoration": "not_started"}
        desktop = SimpleNamespace(desktop_context=lambda: {"thread_desktop": "Default"},
                                  idle_seconds=lambda: 61.0)
        meta = {"guard_startup": {"phase": "banner"}}
        with mock.patch.object(guard_diagnostic, "_OwnedProbeWindow", return_value=window), \
             mock.patch.object(guard_diagnostic, "_wait_for_safe_desktop", return_value=("ready", [])) as wait, \
             mock.patch.object(run.os, "getpid", return_value=123):
            lease = run.start_input_guard(observer, catalog.select(selection), desktop, meta)
        self.assertEqual(events, ["guard_start", "passive"])
        wait.assert_called_once_with(window)
        observer.start.assert_called_once_with(owned_probe_target=(55, 123))
        window.close.assert_not_called()
        observer.stop.assert_not_called()
        self.assertEqual(meta["guard_startup"]["phase"], "ready")
        self.assertEqual(meta["guard_startup"]["first_probe"]["probes_seen"], 1)
        lease.close()
        lease.close()
        self.assertEqual(events, ["guard_start", "passive", "window_close"])
        window.close.assert_called_once_with(restore=False)

    def test_h8_guard_start_refuses_lost_focus_without_starting_observer(self):
        observer = mock.Mock()
        window = mock.Mock(hwnd=55, restoration="restored")
        window.open.return_value = True
        window.focused.return_value = False
        window.diagnostics.return_value = {"status": "focus_lost"}
        meta = {"guard_startup": {"phase": "banner"}}
        with mock.patch.object(guard_diagnostic, "_OwnedProbeWindow", return_value=window), \
             mock.patch.object(guard_diagnostic, "_wait_for_safe_desktop", return_value=("focus_lost", [])):
            with self.assertRaisesRegex(guard.GuardUnavailable, "focus_lost"):
                run.start_input_guard(observer, catalog.select("H8"), mock.Mock(), meta)
        observer.start.assert_not_called()
        window.close.assert_called_once_with(restore=False)
        self.assertEqual(meta["guard_startup"]["phase"], "focus_and_idle_wait")

    def test_h8_guard_start_fails_closed_if_passive_transition_fails(self):
        observer = mock.Mock()
        observer.diagnostics.return_value = {"probes_seen": 1}
        window = mock.Mock(hwnd=55, restoration="window_thread_stop_timeout")
        window.open.return_value = True
        window.focused.return_value = True
        window.enter_passive.return_value = False
        window.diagnostics.return_value = {"restoration": "window_thread_stop_timeout"}
        meta = {"guard_startup": {"phase": "banner"}}
        with mock.patch.object(guard_diagnostic, "_OwnedProbeWindow", return_value=window), \
             mock.patch.object(guard_diagnostic, "_wait_for_safe_desktop", return_value=("ready", [])):
            with self.assertRaisesRegex(guard.GuardUnavailable, "passive transition failed"):
                run.start_input_guard(observer, catalog.select("H8"),
                                      SimpleNamespace(desktop_context=lambda: {}, idle_seconds=lambda: 61), meta)
        observer.start.assert_called_once()
        window.close.assert_called_once_with(restore=False)
        self.assertEqual(meta["guard_startup"]["phase"], "passive_transition")

    def test_startup_lease_retires_once_only_for_verified_keyboard_target(self):
        context = {"input_desktop": "Default", "hwnd": 77, "process_id": 800,
                   "gui_status": "ok", "active_hwnd": 77, "focus_hwnd": 91}
        native = SimpleNamespace(is_window=mock.Mock(return_value=True),
                                 window_pid=mock.Mock(return_value=800),
                                 foreground_context=mock.Mock(return_value=context),
                                 user32=SimpleNamespace(GetAncestor=mock.Mock(return_value=77)),
                                 GA_ROOT=2)
        window = mock.Mock(restoration="skipped_focus_changed")
        window.close.return_value = True
        window.diagnostics.return_value = {"status": "passive"}
        observer = mock.Mock()
        target = SimpleNamespace(hwnd=77, handoff_owner_pid=800, handoff_ready=mock.Mock(return_value=True))
        meta = {}
        lease = run.StartupProbeLease(window, observer, native, meta)
        self.assertTrue(lease.complete_handoff(target))
        self.assertTrue(lease.complete_handoff(target))
        window.close.assert_called_once_with(restore=False)
        self.assertEqual(observer.check.call_count, 3)
        self.assertEqual(native.foreground_context.call_count, 2)
        self.assertEqual(meta["handoff"], "verified_target")

    def test_startup_lease_rejects_wrong_owner_focus_desktop_document_and_editor(self):
        good = {"input_desktop": "Default", "hwnd": 77, "process_id": 800,
                "gui_status": "ok", "active_hwnd": 77, "focus_hwnd": 91}
        cases = (("wrong hwnd", {**good, "hwnd": 42}, 800, True),
                 ("wrong foreground pid", {**good, "process_id": 801}, 800, True),
                 ("wrong window pid", good, 801, True),
                 ("secure desktop", {**good, "input_desktop": "Winlogon"}, 800, True),
                 ("wrong focus", {**good, "focus_hwnd": 0}, 800, True),
                 ("wrong document or editor", good, 800, False))
        for label, context, owner, ready in cases:
            with self.subTest(label=label):
                native = SimpleNamespace(is_window=lambda _: True, window_pid=lambda _: owner,
                                         foreground_context=lambda: context,
                                         user32=SimpleNamespace(GetAncestor=lambda *_: 77), GA_ROOT=2)
                window = mock.Mock()
                target = SimpleNamespace(hwnd=77, handoff_owner_pid=800,
                                         handoff_ready=lambda: ready)
                lease = run.StartupProbeLease(window, mock.Mock(), native, {})
                self.assertFalse(lease.complete_handoff(target))
                window.close.assert_not_called()
                self.assertFalse(lease.closed)

    def test_startup_lease_rechecks_owner_and_guard_before_close(self):
        good = {"input_desktop": "Default", "hwnd": 77, "process_id": 800,
                "gui_status": "ok", "active_hwnd": 77, "focus_hwnd": 77}
        native = SimpleNamespace(is_window=lambda _: True, window_pid=lambda _: 800,
                                 foreground_context=mock.Mock(side_effect=[good, {**good, "hwnd": 42}]),
                                 user32=SimpleNamespace(GetAncestor=mock.Mock()), GA_ROOT=2)
        target = SimpleNamespace(hwnd=77, handoff_owner_pid=800, handoff_ready=lambda: True)
        window = mock.Mock()
        lease = run.StartupProbeLease(window, mock.Mock(), native, {})
        self.assertFalse(lease.complete_handoff(target))
        window.close.assert_not_called()
        self.assertEqual(lease.startup["handoff"], "ownership_changed_before_close")
        tripped = mock.Mock()
        tripped.check.side_effect = guard.GuardUnavailable("missed probe")
        lease = run.StartupProbeLease(window, tripped, native, {})
        with self.assertRaises(guard.GuardUnavailable):
            lease.complete_handoff(target)
        window.close.assert_not_called()

    def test_startup_lease_rejects_foreground_change_inside_final_readiness(self):
        state = {"input_desktop": "Default", "hwnd": 77, "process_id": 800,
                 "gui_status": "ok", "active_hwnd": 77, "focus_hwnd": 77}
        calls = []
        def ready():
            calls.append(True)
            if len(calls) == 2:
                state["hwnd"] = 42
            return True
        native = SimpleNamespace(is_window=lambda _: True, window_pid=lambda _: 800,
                                 foreground_context=lambda: dict(state),
                                 user32=SimpleNamespace(GetAncestor=lambda *_: 77), GA_ROOT=2)
        target = SimpleNamespace(hwnd=77, handoff_owner_pid=800, handoff_ready=ready)
        window = mock.Mock()
        lease = run.StartupProbeLease(window, mock.Mock(), native, {})
        self.assertFalse(lease.complete_handoff(target))
        window.close.assert_not_called()
        self.assertFalse(lease.closed)

    def test_notepad_handoff_rejects_tab_change_during_document_discovery(self):
        pad = targets.Notepad.__new__(targets.Notepad)
        pad.owner_pid = 800
        pad.win32 = SimpleNamespace(pids=lambda _: [800])
        current = [True]
        document = object()
        def discover():
            current[0] = False
            return document
        pad.document = discover
        pad.active_document = lambda: current[0]
        pad.uia = SimpleNamespace(descendants=lambda _: [], focused=lambda: document,
                                  same=lambda a, b: a is b)
        self.assertFalse(pad.handoff_ready())

    def test_notepad_editor_rechecks_focus_after_descendant_enumeration(self):
        pad = targets.Notepad.__new__(targets.Notepad)
        document, external = object(), object()
        focused = [document]
        def descendants(_):
            focused[0] = external
            return []
        pad.uia = SimpleNamespace(descendants=descendants, focused=lambda: focused[0],
                                  same=lambda a, b: a is b)
        self.assertFalse(pad.editor_focused(document))

    def test_context_registers_handoff_only_after_normal_target_focus(self):
        ctx = Context.__new__(Context)
        ctx.work, ctx.check_id, ctx.attempt = Path("work"), "H1", 1
        ctx._completed_notepads, ctx._notepad_serial = [], 0
        ctx.guard = mock.Mock()
        ctx.restorer = restore.Restorer(log=lambda _: None)
        ctx.startup_probe_lease = mock.Mock()
        ctx._targets = []
        app = targets.TargetApp.__new__(targets.TargetApp)
        app.title, app.hwnd = "Desktop Target H1-A", 77
        app.win32 = SimpleNamespace(activate=mock.Mock(), click=mock.Mock(),
                                    wait_for=lambda predicate, _: predicate(), foreground=lambda: 77)
        app.text_point = lambda: (100, 200)
        app.close = mock.Mock()
        app.start = mock.Mock(return_value=app)
        with mock.patch.object(targets, "TargetApp", return_value=app):
            self.assertIs(ctx.target("A"), app)
        ctx.startup_probe_lease.complete_handoff.assert_not_called()
        app.focus()
        ctx.startup_probe_lease.complete_handoff.assert_called_once_with(app)
        pad = targets.Notepad.__new__(targets.Notepad)
        pad.hwnd, pad.owned_tab = 88, False
        pad.path = Path("notepad-H1-doc.txt")
        pad.win32 = SimpleNamespace(activate=mock.Mock(), click=mock.Mock(),
                                    wait_for=lambda predicate, _: predicate(), foreground=lambda: 88)
        document = object()
        pad.document = lambda: document
        pad.uia = SimpleNamespace(center=lambda _: (100, 200))
        pad.editor_focused = lambda _: True
        pad.close = mock.Mock()
        pad.start = mock.Mock(return_value=pad)
        with mock.patch.object(targets, "Notepad", return_value=pad):
            self.assertIs(ctx.notepad(), pad)
        pad.focus()
        ctx.startup_probe_lease.complete_handoff.assert_called_with(pad)

    def test_outer_run_closes_no_target_lease_before_restore_on_repeat_or_abort(self):
        cases = (("H1,H8", 2, ([], ""), False, False, False),
                 ("H8", 1, guard.UserInput("owner touched input"), False, False, False),
                 ("H8", 1, RuntimeError("check setup failed"), False, False, False),
                 ("H8", 1, ([], ""), True, False, False),
                 ("H8", 1, ([], ""), False, True, False),
                 ("H1,H8", 1, ([], ""), False, False, True),
                 ("O8", 1, ([], ""), False, False, False))
        for selection, repeat, outcome, stop_error, close_error, cleanup_error in cases:
            with self.subTest(selection=selection, repeat=repeat, outcome=outcome,
                              stop_error=stop_error, close_error=close_error, cleanup_error=cleanup_error):
                with tempfile.TemporaryDirectory() as temporary:
                    events = []
                    observer = mock.Mock()
                    def stop():
                        events.append("guard_stop")
                        if stop_error:
                            raise RuntimeError("stop failed")
                    observer.stop.side_effect = stop
                    observer.diagnostics.return_value = {}
                    lease = mock.Mock()
                    def close():
                        events.append("lease_close")
                        if close_error:
                            raise guard.GuardUnavailable("destroy failed")
                    lease.close.side_effect = close
                    session = mock.Mock()
                    session.prepare.return_value = session
                    session.stop.side_effect = lambda: events.append("restore_murmur")
                    build = SimpleNamespace(record={}, version="test")
                    result = outcome if isinstance(outcome, tuple) else None
                    failure = outcome if isinstance(outcome, Exception) else None
                    patches = [
                        mock.patch.object(run, "preflight", return_value=[]),
                        mock.patch.object(run, "start_input_guard", return_value=lease),
                        mock.patch.object(run, "run_checks", return_value=result, side_effect=failure),
                        mock.patch.object(run, "remove_tree", return_value=True),
                        mock.patch.object(run.sys, "getwindowsversion", create=True, return_value=(10, 0, 26200)),
                        mock.patch.object(run.tempfile, "mkdtemp", return_value=str(Path(temporary) / "work")),
                        mock.patch.object(win32, "require_windows"),
                        mock.patch.object(win32, "set_dpi_aware"),
                        mock.patch.object(win32, "long_path", side_effect=lambda path: path),
                        mock.patch.object(win32, "clipboard_text", return_value=""),
                        mock.patch.object(win32, "set_clipboard_text"),
                        mock.patch.object(win32, "idle_seconds", return_value=61),
                        mock.patch.object(win32, "desktop_context", return_value={}),
                        mock.patch.object(win32, "release_all", side_effect=lambda: events.append("release")),
                        mock.patch.object(murmur, "Build", return_value=build),
                        mock.patch.object(murmur, "MurmurSession", return_value=session),
                        mock.patch.object(murmur, "fetch_model", return_value="model"),
                        mock.patch.object(fixtures, "build", return_value={}),
                        mock.patch.object(targets, "show_banner"),
                        mock.patch.object(guard, "Guard", return_value=observer),
                        mock.patch.object(report, "write", return_value=SimpleNamespace(read_text=lambda **_: "{}")),
                        mock.patch.object(Context, "close_completed_notepads",
                                          side_effect=lambda: events.append("tab_cleanup") or
                                          ([("close Notepad", "RuntimeError: failed")] if cleanup_error else [])),
                    ]
                    with ExitStack() as stack:
                        start = stack.enter_context(patches[1])
                        for patch in patches[:1] + patches[2:]:
                            stack.enter_context(patch)
                        code = run.main(["--build", temporary, "--out", temporary,
                                         "--only", selection, "--repeat", str(repeat)])
                        written_meta = report.write.call_args.args[1]
                    self.assertEqual(code, 0 if isinstance(outcome, tuple) and
                                     not stop_error and not close_error and not cleanup_error else 1)
                    self.assertEqual(start.call_count, 1)
                    lease.close.assert_called_once_with()
                    self.assertLess(events.index("tab_cleanup"), events.index("guard_stop"))
                    self.assertLess(events.index("guard_stop"), events.index("lease_close"))
                    self.assertLess(events.index("lease_close"), events.index("restore_murmur"))
                    if cleanup_error:
                        self.assertEqual(written_meta["completed_target_cleanup_failures"],
                                         ["close Notepad: RuntimeError: failed"])
                        self.assertIn("cleanup failed", written_meta["aborted"])

    def test_startup_lease_close_failure_is_fail_closed_and_retryable(self):
        window = mock.Mock(restoration="window_thread_stop_timeout")
        window.close.side_effect = [False, True]
        window.diagnostics.return_value = {"restoration": "window_thread_stop_timeout"}
        lease = run.StartupProbeLease(window, mock.Mock(), mock.Mock(), {})
        with self.assertRaisesRegex(guard.GuardUnavailable, "cleanup failed"):
            lease.close()
        self.assertFalse(lease.closed)
        lease.close()
        self.assertTrue(lease.closed)
        self.assertEqual(window.close.call_count, 2)
        self.assertEqual(lease.startup["probe_window"], window.diagnostics.return_value)

    def test_notepad_handoff_requires_live_owner_document_and_editor(self):
        pad = targets.Notepad.__new__(targets.Notepad)
        pad.owner_pid = 800
        pad.win32 = SimpleNamespace(pids=lambda _: [800])
        pad.active_document = mock.Mock(return_value=True)
        pad.editor_focused = mock.Mock(return_value=True)
        self.assertTrue(pad.handoff_ready())
        pad.editor_focused.return_value = False
        self.assertFalse(pad.handoff_ready())
        pad.editor_focused.return_value = True
        pad.active_document.return_value = False
        self.assertFalse(pad.handoff_ready())
        pad.active_document.return_value = True
        pad.win32.pids = lambda _: [801]
        self.assertFalse(pad.handoff_ready())

    def test_target_app_handoff_requires_live_process_registered_hwnd_and_editor(self):
        app = targets.TargetApp.__new__(targets.TargetApp)
        app.hwnd, app.title = 77, "Desktop Target H2-A"
        app.process = SimpleNamespace(pid=800, poll=lambda: None)
        state = {"pid": 800, "hwnd": 77, "title": app.title, "editor_focused": True}
        app.state = lambda: state
        self.assertTrue(app.handoff_ready())
        for key, value in (("pid", 801), ("hwnd", 42), ("title", "other"),
                           ("editor_focused", False)):
            with self.subTest(key=key):
                app.state = lambda key=key, value=value: {**state, key: value}
                self.assertFalse(app.handoff_ready())
        app.state = lambda: state
        app.process.poll = lambda: 0
        self.assertFalse(app.handoff_ready())

    def test_passive_window_removes_topmost_without_focus_or_restoration(self):
        window = guard_diagnostic._OwnedProbeWindow(restore_on_close=False)
        calls = [0]
        def wait(_seconds):
            calls[0] += 1
            if calls[0] == 2:
                window._passive.set()
            return calls[0] == 3
        user32 = SimpleNamespace(GetForegroundWindow=mock.Mock(side_effect=[40, 55, 55, 55]),
                                 CreateWindowExW=mock.Mock(return_value=55),
                                 SetForegroundWindow=mock.Mock(return_value=True),
                                 SetFocus=mock.Mock(), GetFocus=mock.Mock(return_value=55),
                                 SetWindowPos=mock.Mock(return_value=True),
                                 PeekMessageW=mock.Mock(return_value=False),
                                 IsWindow=mock.Mock(return_value=True),
                                 DestroyWindow=mock.Mock(return_value=True))
        kernel32 = SimpleNamespace(GetModuleHandleW=mock.Mock(return_value=1))
        with mock.patch.object(win32, "user32", user32), \
             mock.patch.object(win32, "kernel32", kernel32), \
             mock.patch.object(window._stop, "wait", side_effect=wait):
            window._run()
        self.assertEqual(window._passive_status, "passive")
        self.assertEqual(user32.SetForegroundWindow.call_args_list, [mock.call(55)])
        user32.SetFocus.assert_not_called()
        user32.SetWindowPos.assert_called_once_with(
            55, guard_diagnostic.HWND_NOTOPMOST, 0, 0, 0, 0,
            guard_diagnostic.SWP_KEEP_SIZE_POSITION_NO_ACTIVATE)
        self.assertEqual(window.restoration, "skipped_teardown_stage")

    def test_window_stays_visible_after_focus_denial_until_user_clicks(self):
        window = guard_diagnostic._OwnedProbeWindow()
        user32 = SimpleNamespace(
            GetForegroundWindow=mock.Mock(side_effect=[40, 40, 55, 55]),
            CreateWindowExW=mock.Mock(return_value=55),
            SetForegroundWindow=mock.Mock(side_effect=[False, True]),
            SetFocus=mock.Mock(), GetFocus=mock.Mock(return_value=55),
            PeekMessageW=mock.Mock(return_value=False), IsWindow=mock.Mock(return_value=True),
            DestroyWindow=mock.Mock())
        kernel32 = SimpleNamespace(GetModuleHandleW=mock.Mock(return_value=1))
        with mock.patch.object(win32, "user32", user32), \
             mock.patch.object(win32, "kernel32", kernel32), \
             mock.patch.object(window._stop, "wait", side_effect=[False, False, True]):
            window._run()
        self.assertEqual(window.initial_focus, "denied")
        self.assertEqual(window.status, "ready")
        self.assertIn("If this window is not focused, click it", user32.CreateWindowExW.call_args.args[2])
        self.assertEqual(user32.SetForegroundWindow.call_args_list, [mock.call(55), mock.call(40)])
        user32.DestroyWindow.assert_called_once_with(55)

    def test_window_records_only_tagged_probe_messages(self):
        window = guard_diagnostic._OwnedProbeWindow()
        window.hwnd = 55
        tag = guard.PROBE_TAG | (1 << 32)
        messages = [(guard.WM_KEYDOWN, guard.VK_PACKET, tag),
                    (guard.WM_KEYUP, guard.VK_PACKET, tag),
                    (guard.WM_KEYDOWN, 0x41, 0)]
        current = [None]

        def peek(out, *_args):
            if not messages:
                return False
            kind, vk, extra = messages.pop(0)
            msg = guard.ctypes.cast(out, guard.ctypes.POINTER(guard.wintypes.MSG)).contents
            msg.message, msg.wParam = kind, vk
            current[0] = extra
            return True

        user32 = SimpleNamespace(PeekMessageW=peek, GetMessageExtraInfo=lambda: current[0],
                                 DispatchMessageW=mock.Mock())
        window._drain_messages(user32)
        self.assertEqual([(event["message"], event["vk"]) for event in window.received_events],
                         [(guard.WM_KEYDOWN, guard.VK_PACKET), (guard.WM_KEYUP, guard.VK_PACKET)])
        self.assertEqual(user32.DispatchMessageW.call_count, 3)

    def test_window_restores_previous_focus_only_if_it_still_owns_foreground(self):
        for foreground_on_close, expected_calls in ((55, [mock.call(55), mock.call(40)]),
                                                     (77, [mock.call(55)])):
            with self.subTest(foreground_on_close=foreground_on_close):
                window = guard_diagnostic._OwnedProbeWindow()
                window._stop.set()
                user32 = SimpleNamespace(
                    GetForegroundWindow=mock.Mock(side_effect=[40, foreground_on_close]),
                    CreateWindowExW=mock.Mock(return_value=55),
                    SetForegroundWindow=mock.Mock(return_value=True),
                    SetFocus=mock.Mock(), GetFocus=mock.Mock(return_value=55),
                    PeekMessageW=mock.Mock(return_value=False), IsWindow=mock.Mock(return_value=True),
                    DestroyWindow=mock.Mock())
                kernel32 = SimpleNamespace(GetModuleHandleW=mock.Mock(return_value=1))
                with mock.patch.object(win32, "user32", user32), \
                     mock.patch.object(win32, "kernel32", kernel32):
                    window._run()
                self.assertEqual(user32.SetForegroundWindow.call_args_list, expected_calls)
                user32.DestroyWindow.assert_called_once_with(55)
                self.assertEqual(window.restoration,
                                 "restored" if foreground_on_close == 55 else "skipped_focus_changed")


class SettingsCheckTests(unittest.TestCase):
    def test_resize_grip_requires_actual_window_and_native_corner_hit(self):
        hits = []
        def hit_test(_hwnd, _message, _wparam, packed):
            hits.append(packed)
            return win32.HTBOTTOMRIGHT if len(hits) == 2 else 1
        fake = SimpleNamespace(WindowFromPoint=lambda _: 99, GetAncestor=lambda *_: 42,
                               SendMessageW=hit_test)
        with mock.patch.object(win32, "IS_WINDOWS", True), \
             mock.patch.object(win32, "outer_rect", return_value=(0, 0, 1016, 819)), \
             mock.patch.object(win32, "user32", fake):
            self.assertEqual(win32.resize_grip(42), (1010, 813))
            fake.GetAncestor = lambda *_: 77
            with self.assertRaisesRegex(RuntimeError, "No reachable Settings bottom-right resize grip"):
                win32.resize_grip(42)

    def test_theme_cannot_pass_a_boot_error_page_that_changes_colour(self):
        from checks import settings as checks_settings
        ctx = SimpleNamespace(murmur=SimpleNamespace(ensure=lambda *_: None, show_settings=lambda: 42,
                                                     settings=lambda: "window"),
                              fixtures={"A": SimpleNamespace(path="fixture.wav")},
                              restorer=restore.Restorer(log=lambda _: None), sleep=lambda _: None)
        with mock.patch.object(win32, "Registry", return_value=SimpleNamespace(read=lambda *_: restore.MISSING)), \
             mock.patch.object(checks_settings.uia, "find", side_effect=checks_settings.uia.NotFound("General")):
            with self.assertRaises(checks_settings.uia.NotFound):
                checks_settings.s3_theme(ctx)

    def test_minimum_size_uses_user_resize_and_detects_undersized_client(self):
        from checks import settings as checks_settings
        ctx = SimpleNamespace(murmur=SimpleNamespace(ensure=lambda _: None, show_settings=lambda: 42),
                              fixtures={"A": SimpleNamespace(path="fixture.wav")},
                              restorer=restore.Restorer(log=lambda _: None), sleep=lambda _: None, note=lambda _: None,
                              screenshot=lambda *_: None)
        with mock.patch.object(win32, "outer_rect", return_value=(0, 0, 1016, 819)), \
             mock.patch.object(win32, "dpi_scale", return_value=1), \
             mock.patch.object(win32, "client_size", side_effect=[(1000, 780), (700, 500)]), \
             mock.patch.object(win32, "activate"), \
             mock.patch.object(win32, "resize_grip", return_value=(1012, 815)), \
             mock.patch.object(win32, "drag") as drag, \
             mock.patch.object(win32, "set_window_pos") as set_pos:
            with self.assertRaisesRegex(Fail, "below the 760x560 px minimum"):
                checks_settings.s4_minimum_size(ctx)
            drag.assert_called_once_with((1012, 815), (496, 396))
            set_pos.assert_not_called()
            ctx.restorer.restore_all()
            set_pos.assert_called_once()


class GoldenDuplicateTests(unittest.TestCase):
    def test_delivered_twice_with_separator(self):
        message = golden.compare("Hello there.", "Hello there. Hello there.")
        self.assertIn("more than once", message)

    def test_prefix_extension_is_not_called_a_duplicate(self):
        message = golden.compare("Hello there.", "Hello there. And")
        self.assertNotIn("more than once", message)
        self.assertIn("differs at character 12", message)


class ChecksImportTests(unittest.TestCase):
    def test_every_check_module_imports_off_windows(self):
        import importlib
        for name in ("common", "components", "focus", "hotkeys", "insertion", "keyboard", "microphone",
                     "overlay", "settings", "setup", "tray"):
            importlib.import_module(f"checks.{name}")


class AbortedAggregationTests(unittest.TestCase):
    def test_an_aborted_repeat_is_not_hidden_by_an_earlier_pass(self):
        attempts = [report.Result("H1", report.PASS, "", "t", "g", attempt=1),
                    report.Result("H1", report.ABORTED, "aborted: user input (x)", "t", "g", attempt=2)]
        row = report.aggregate(attempts)
        self.assertEqual(row.status, report.ABORTED)
        self.assertIn("passed 1 earlier", row.reason)

    def test_a_failure_still_wins_over_an_abort(self):
        attempts = [report.Result("H1", report.FAIL, "boom", "t", "g", attempt=1),
                    report.Result("H1", report.ABORTED, "aborted: user input (x)", "t", "g", attempt=2)]
        self.assertEqual(report.aggregate(attempts).status, report.FAIL)


class DeliveredKeysTests(unittest.TestCase):
    def test_hotkey_modifier_presses_are_not_delivery(self):
        import targets
        keys = [{"char": "", "keysym": "Control_L", "state": 0, "t": 1.0},
                {"char": "", "keysym": "Shift_L", "state": 4, "t": 1.1},
                {"char": "\x04", "keysym": "d", "state": 12, "t": 1.2},
                {"char": "T", "keysym": "T", "state": 0, "t": 5.0},
                {"char": "h", "keysym": "h", "state": 0, "t": 5.1}]
        delivered = targets.delivered_keys(keys)
        self.assertEqual([k["char"] for k in delivered], ["T", "h"])
        self.assertEqual(delivered[0]["t"], 5.0)


class NormalNotepadReadbackTests(unittest.TestCase):
    CHECKS = (("H1", hotkey_checks.h1_hold, "A"),
              ("I1", insertion_checks.i1_notepad, "A"),
              ("I4", insertion_checks.i4_paste, "LONG"),
              ("I5", insertion_checks.i5_unicode, "MARKER"))

    def exercise(self, check_id, check, fixture, *, output=True, target_text=None,
                 menu=False, settled_state=None):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            expected = ("Please type the café déjà vu now. " if fixture == "MARKER" else
                        fixtures.SENTENCES[fixture] + " ")
            target_text = expected if target_text is None else target_text

            def observed_text(**_):
                events.append("history")
                if not output:
                    raise OutputCount(0)
                return expected

            def focus_diagnostic():
                events.append("uia-focus")
                return "UIA focus: RichEditD2DPT\n"

            def wait_text():
                events.append("uia-text")
                return target_text

            pad = SimpleNamespace(hwnd=42, focus=lambda: events.append("focus"),
                                  focus_diagnostic=focus_diagnostic, wait_text=wait_text,
                                  menu_open=lambda: events.append("uia-menu") or menu)
            ctx = Context.__new__(Context)
            ctx.fixtures = {fixture: SimpleNamespace(path=f"{fixture}.wav")}
            ctx.murmur = SimpleNamespace(ensure=lambda *_: events.append("ensure"))
            ctx.notepad = lambda: pad
            ctx.dictate = lambda *_a, **_kw: events.append("dictate")
            ctx.observed_text = observed_text
            ctx.capture_output_failure = lambda *_, **__: events.append("output-diagnostics")
            ctx.settle = lambda: events.append("settle") or (settled_state or ("done" if output else "held"))
            ctx.screenshot = lambda *_: events.append("screenshot")
            ctx.artifact = lambda suffix: Path(directory) / f"{check_id}-{suffix}"
            ctx.note = lambda _: events.append("note")
            ctx.clipboard_guard = lambda: events.append("clipboard-guard")
            ctx.sleep = lambda _: events.append("sleep")
            with mock.patch.object(win32, "set_clipboard_text"), \
                 mock.patch.object(win32, "clipboard_text", return_value="murmur-sentinel-deadbeef"), \
                 mock.patch.object(insertion_checks.secrets, "token_hex", return_value="deadbeef"):
                if not output:
                    with self.assertRaisesRegex(OutputCount, "found 0"):
                        check(ctx)
                elif menu:
                    with self.assertRaisesRegex(Fail, "menu was activated"):
                        check(ctx)
                elif target_text != expected:
                    with self.assertRaisesRegex(Fail, "differs at character"):
                        check(ctx)
                else:
                    check(ctx)
            evidence = {suffix: (Path(directory) / f"{check_id}-{suffix}").exists()
                        for suffix in ("after-hotkey-focus.txt", "notepad-focus.txt")}
            return events, evidence

    def test_notepad_uia_readback_waits_for_history_then_keeps_exact_comparison(self):
        for check_id, check, fixture in self.CHECKS:
            with self.subTest(check=check_id):
                events, evidence = self.exercise(check_id, check, fixture)
                self.assertLess(events.index("dictate"), events.index("history"))
                self.assertLess(events.index("history"), events.index("uia-focus"))
                self.assertLess(events.index("history"), events.index("uia-text"))
                self.assertEqual(events.count("history"), 2)  # confirmation and exact oracle
                self.assertTrue(all(evidence.values()))
                events, _ = self.exercise(check_id, check, fixture, target_text="wrong text")
                self.assertLess(events.index("history"), events.index("uia-text"))

    def test_missing_notepad_output_fails_and_captures_only_after_settle(self):
        for check_id, check, fixture in self.CHECKS:
            with self.subTest(check=check_id):
                events, evidence = self.exercise(check_id, check, fixture, output=False)
                self.assertEqual(events.count("history"), 1)
                self.assertNotIn("uia-text", events)
                self.assertLess(events.index("history"), events.index("settle"))
                self.assertLess(events.index("settle"), events.index("uia-focus"))
                self.assertIn("screenshot", events)
                self.assertTrue(all(evidence.values()))

    def test_missing_output_does_not_query_notepad_while_still_transcribing(self):
        events, evidence = self.exercise("I1", insertion_checks.i1_notepad, "A",
                                         output=False, settled_state="transcribing")
        self.assertNotIn("uia-focus", events)
        self.assertNotIn("uia-text", events)
        self.assertNotIn("screenshot", events)
        self.assertIn("note", events)
        self.assertFalse(any(evidence.values()))

    def test_h1_menu_still_fails_after_history_without_sending_escape(self):
        with mock.patch.object(win32, "tap") as tap:
            events, _ = self.exercise("H1", hotkey_checks.h1_hold, "A", menu=True)
        self.assertLess(events.index("history"), events.index("uia-menu"))
        tap.assert_not_called()


class F13Tests(unittest.TestCase):
    def test_held_modifier_phase_requires_full_transcript_reason_zero_input_and_no_history(self):
        names = ["Not inserted: focus changed", "Daniel delivered the old wooden desk today.",
                 "Delivery paused: release Ctrl, Alt, Shift or Windows, then choose Insert here to retry."]
        session = SimpleNamespace(overlay_state=lambda: "held", overlay_names=lambda: names,
                                  history_after=lambda _: [])
        app = SimpleNamespace(delivered=lambda: [], text=lambda: "")
        ctx = SimpleNamespace(murmur=session, _delivery_cursor=7, note=mock.Mock())
        focus_checks._assert_f13_held(ctx, app)
        ctx.note.assert_called_once()
        session.overlay_names = lambda: [names[0], "Daniel delivered", names[2]]
        with self.assertRaisesRegex(Fail, "full D transcript"):
            focus_checks._assert_f13_held(ctx, app)
        session.overlay_names = lambda: names
        session.history_after = lambda _: [(8, names[1])]
        with self.assertRaisesRegex(Fail, "successful delivery"):
            focus_checks._assert_f13_held(ctx, app)
        session.history_after = lambda _: []
        app.delivered = lambda: [{"char": "D"}]
        with self.assertRaisesRegex(Fail, "Target received input"):
            focus_checks._assert_f13_held(ctx, app)

    def test_f13_recovers_then_runs_exact_insert_here_type_and_clipboard_paths(self):
        apps = {name: SimpleNamespace(hwnd=index + 1, focus=mock.Mock(), wait_text=lambda: "D text")
                for index, name in enumerate(("direct", "A0", "B0", "A1", "B1"))}
        configs = []
        session = SimpleNamespace(ensure=lambda _, config: configs.append(config),
                                  overlay_state=lambda: "held")
        ctx = SimpleNamespace(murmur=session, fixtures={"D": SimpleNamespace(path="d.wav")},
                              clipboard_guard=mock.Mock(), target=lambda name: apps[name],
                              dictate=mock.Mock(), expect_delivery=mock.Mock(), settle=mock.Mock(),
                              sleep=lambda _: None)
        with mock.patch.object(focus_checks, "_assert_modifier_hold_refuses_delivery") as held, \
             mock.patch.object(focus_checks, "click_overlay") as click, \
             mock.patch.object(focus_checks, "wait_held") as wait_held, \
             mock.patch.object(focus_checks, "wait_not_held", return_value=True), \
             mock.patch.object(focus_checks, "require_empty"), \
             mock.patch.object(win32, "wait_for", return_value=False), \
             mock.patch.object(win32, "set_clipboard_text") as set_clipboard, \
             mock.patch.object(win32, "clipboard_text", return_value="F13 clipboard sentinel"):
            focus_checks.f13_no_retrigger(ctx)
        held.assert_called_once_with(ctx, apps["direct"])
        self.assertEqual(click.call_count, 3)
        self.assertEqual([call.kwargs["expect_foreground"] for call in click.call_args_list],
                         [apps["direct"].hwnd, apps["B0"].hwnd, apps["B1"].hwnd])
        self.assertEqual(ctx.expect_delivery.call_count, 3)
        self.assertEqual(ctx.dictate.call_count, 2)
        self.assertEqual([config["injection"]["method"] for config in configs[1:]],
                         ["type", "clipboard"])
        set_clipboard.assert_called_once_with("F13 clipboard sentinel")


class TeardownDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.observer = guard.TeardownDiagnosticObserver()
        self.observer._thread_id = 91
        self.observer._installed_hooks = 2
        self.observer._thread = SimpleNamespace(is_alive=lambda: True)
        self.context = {"hwnd": 55, "thread_id": 12, "process_id": 123,
                        "active_hwnd": 55, "focus_hwnd": 55, "gui_status": "ok",
                        "input_desktop": "Default"}

    def _patched(self, send):
        return (mock.patch.object(win32, "user32", SimpleNamespace(SendInput=send)),
                mock.patch.object(win32, "foreground_context", return_value=self.context),
                mock.patch.object(win32, "desktop_context", return_value={"thread_desktop": "Default"}),
                mock.patch.object(win32, "last_input_tick", return_value=100))

    def _run_a_miss(self, *, foreground=None, ticks=None, close_ok=True, thread_alive=False):
        operations = []
        a = mock.Mock(hwnd=55, restoration="skipped_teardown_stage")
        b = mock.Mock(hwnd=66, restoration="skipped_teardown_stage")
        for window in (a, b):
            window.open.return_value = True
            window.focused.return_value = True
            window.diagnostics.return_value = {"status": "ready"}
        a._thread = SimpleNamespace(is_alive=lambda: thread_alive)

        def close_a(*, restore):
            if a.close.call_count == 1:
                operations.append("close_a")
            self.assertFalse(restore)
            return close_ok

        a.close.side_effect = close_a
        observed = guard.TeardownDiagnosticObserver()
        observed._thread_id = 91
        observed.start = mock.Mock(return_value=observed)
        observed.stop = mock.Mock()
        observed.diagnostics = mock.Mock(return_value={"hook_state": {
            "keyboard_callbacks": 1, "heartbeat_received": 1}})

        def probe(hwnd, *_args, recovery=False):
            operations.append("probe_b" if recovery else "probe_a")
            if recovery:
                return {"status": "observed"}
            self.assertEqual(hwnd, 55)
            observed.tripped.set()
            return {"status": "missed"}

        observed.probe_once = mock.Mock(side_effect=probe)

        # Mock increments call_count before invoking side_effect.
        def make_window():
            operations.append("construct_a" if factory.call_count == 1 else "construct_b")
            return a if factory.call_count == 1 else b

        def open_b(*, activation_gate):
            operations.append("open_b")
            return True

        b.open.side_effect = open_b
        null_context = {"hwnd": 0, "process_id": 0, "gui_status": "no_foreground",
                        "input_desktop": "Default"}
        with mock.patch.object(guard_diagnostic.win32, "require_windows"), \
             mock.patch.object(guard_diagnostic.win32, "desktop_context", return_value={}), \
             mock.patch.object(guard_diagnostic.win32, "foreground_context",
                               return_value=foreground or null_context), \
             mock.patch.object(guard_diagnostic.win32, "last_input_tick",
                               side_effect=ticks or (lambda: 100)), \
             mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=[]), \
             mock.patch.object(guard_diagnostic, "_wait_for_safe_desktop", return_value=("ready", [])), \
             mock.patch.object(guard_diagnostic, "_OwnedProbeWindow", side_effect=make_window) as factory, \
             mock.patch.object(guard_diagnostic.guard, "TeardownDiagnosticObserver",
                               return_value=observed):
            result = guard_diagnostic.diagnose_teardown()
        return result, a, b, observed, operations

    def test_a_miss_closes_and_snapshots_a_before_constructing_b(self):
        result, a, b, observed, operations = self._run_a_miss()
        self.assertEqual(result["status"], "miss_recovered")
        self.assertEqual(operations[:5],
                         ["construct_a", "probe_a", "close_a", "construct_b", "open_b"])
        self.assertEqual(operations[5], "probe_b")
        self.assertEqual([row["stage"] for row in result["stages"]],
                         ["a_focused", "a_before_teardown", "a_after_teardown", "b_recovery"])
        a.close.assert_any_call(restore=False)
        self.assertEqual(observed.probe_once.call_count, 2)
        self.assertTrue(observed.probe_once.call_args_list[-1].kwargs["recovery"])
        b.open.assert_called_once()

    def test_external_foreground_after_a_destroyed_prevents_b_and_input(self):
        external = {"hwnd": 77, "process_id": 999, "active_hwnd": 77,
                    "focus_hwnd": 77, "gui_status": "ok", "input_desktop": "Default"}
        result, a, b, observed, operations = self._run_a_miss(foreground=external)
        self.assertEqual(result["status"], "owner_or_focus_lost")
        self.assertEqual(operations, ["construct_a", "probe_a", "close_a"])
        observed.probe_once.assert_called_once()
        b.open.assert_not_called()

    def test_handoff_permits_only_null_or_verified_b_foreground(self):
        null = {"hwnd": 0, "process_id": 0, "gui_status": "no_foreground",
                "input_desktop": "Default"}
        owned = {"hwnd": 66, "process_id": os.getpid(), "active_hwnd": 66,
                 "focus_hwnd": 0, "gui_status": "ok", "input_desktop": "Default"}
        self.assertTrue(guard_diagnostic._safe_handoff_foreground(null))
        self.assertTrue(guard_diagnostic._safe_handoff_foreground(owned, 66))
        self.assertFalse(guard_diagnostic._safe_handoff_foreground(owned))
        self.assertFalse(guard_diagnostic._safe_handoff_foreground({**owned, "hwnd": 77,
                                                                    "active_hwnd": 77}, 66))
        self.assertFalse(guard_diagnostic._safe_handoff_foreground({**null,
                                                                    "input_desktop": "Winlogon"}))

    def test_input_since_a_probe_prevents_b_activation(self):
        result, _a, b, observed, operations = self._run_a_miss(ticks=iter([100, 101]))
        self.assertEqual(result["status"], "input_since_last_probe")
        self.assertEqual(operations, ["construct_a", "probe_a", "close_a"])
        observed.probe_once.assert_called_once()
        b.open.assert_not_called()

    def test_failed_a_destruction_or_thread_join_prevents_b_and_recovery(self):
        for alive, expected in ((False, "window_teardown_failed"),
                                (True, "window_thread_stop_timeout")):
            with self.subTest(alive=alive):
                result, _a, b, observed, operations = self._run_a_miss(
                    close_ok=False, thread_alive=alive)
                self.assertEqual(result["status"], expected)
                self.assertEqual(operations, ["construct_a", "probe_a", "close_a"])
                observed.probe_once.assert_called_once()
                b.open.assert_not_called()

    def test_b_window_checks_gate_before_create_show_and_activation(self):
        for verdicts, shown in (([False], False), ([True, False], False),
                                ([True, True, False], True)):
            with self.subTest(verdicts=verdicts):
                window = guard_diagnostic._OwnedProbeWindow()
                user32 = SimpleNamespace(
                    GetForegroundWindow=mock.Mock(return_value=0),
                    CreateWindowExW=mock.Mock(return_value=66),
                    SetWindowPos=mock.Mock(return_value=True), SetForegroundWindow=mock.Mock(),
                    DestroyWindow=mock.Mock(return_value=True))
                kernel32 = SimpleNamespace(GetModuleHandleW=mock.Mock(return_value=1))
                with mock.patch.object(win32, "user32", user32), \
                     mock.patch.object(win32, "kernel32", kernel32):
                    window._run(activation_gate=lambda _hwnd: verdicts.pop(0))
                self.assertEqual(window.status, "handoff_unsafe")
                self.assertEqual(user32.CreateWindowExW.call_count, int(window.hwnd is not None))
                self.assertEqual(user32.SetWindowPos.called, shown)
                if shown:
                    self.assertEqual(user32.SetWindowPos.call_args.args[-1],
                                     guard_diagnostic.SWP_SHOW_NO_ACTIVATE)
                user32.SetForegroundWindow.assert_not_called()

    def test_a_close_requires_destroy_success_thread_exit_and_absent_hwnd(self):
        for destroyed, alive, exists, expected in ((True, False, False, True),
                                                   (False, False, False, False),
                                                   (True, True, False, False),
                                                   (True, False, True, False)):
            with self.subTest(destroyed=destroyed, alive=alive, exists=exists):
                window = guard_diagnostic._OwnedProbeWindow()
                window.hwnd = 55
                window._destroyed = destroyed
                window._thread = mock.Mock()
                window._thread.is_alive.return_value = alive
                with mock.patch.object(win32, "user32", SimpleNamespace(
                        IsWindow=mock.Mock(return_value=exists))):
                    self.assertEqual(window.close(restore=False), expected)

    def test_handoff_close_cannot_reenable_previous_foreground_restoration(self):
        window = guard_diagnostic._OwnedProbeWindow()
        window._handoff_window = True
        window._restore_on_close = False
        window.close()
        self.assertFalse(window._restore_on_close)

    def test_keyup_only_probe_and_registration_continuity_across_miss_and_recovery(self):
        calls = []

        def send(count, events, size):
            calls.append((count, events[0].ki.dwFlags, events[0].ki.wVk, size))
            if len(calls) == 2:
                tag = events[0].ki.dwExtraInfo
                self.observer._record_injected_key(guard.WM_SYSKEYUP, SimpleNamespace(
                    vkCode=guard.VK_PACKET, scanCode=guard.OWNED_PROBE_CHAR,
                    flags=guard.LLKHF_INJECTED | guard.LLKHF_UP, dwExtraInfo=tag))
            return 1

        user, context, desktop, last_input = self._patched(send)
        with user, context, desktop, last_input, mock.patch.object(guard, "PROBE_TIMEOUT", 0.005), \
             mock.patch.object(self.observer, "_post_heartbeat"):
            missed = self.observer.probe_once(55, 123, "unicode_up", "window")
            recovered = self.observer.probe_once(55, 123, "unicode_up", "window", recovery=True)
        self.assertEqual((missed["status"], recovered["status"]), ("missed", "observed"))
        self.assertEqual(missed["registration"], recovered["registration"])
        self.assertEqual(recovered["hook_messages"], [guard.WM_SYSKEYUP])
        self.assertTrue(self.observer.tripped.is_set())
        self.assertTrue(self.observer.unavailable)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(row == (1, win32.KEYEVENTF_UNICODE | win32.KEYEVENTF_KEYUP,
                                    0, guard.ctypes.sizeof(win32.INPUT)) for row in calls))
        with self.assertRaises(RuntimeError):
            self.observer.probe_once(55, 123, "unicode_up", "window", recovery=True)

    def test_owner_and_null_focus_are_required_before_send(self):
        send = mock.Mock(return_value=1)
        user, context, desktop, last_input = self._patched(send)
        with user, context, desktop, last_input:
            self.assertEqual(self.observer.probe_once(55, 123, "unicode_up", "none")["status"],
                             "owner_or_focus_lost")
            self.context["focus_hwnd"] = 0
            self.context["process_id"] = 999
            self.assertEqual(self.observer.probe_once(55, 123, "unicode_up", "none")["status"],
                             "owner_or_focus_lost")
        send.assert_not_called()

    def test_foreground_context_reads_gui_focus_and_owner(self):
        def owner(_hwnd, out):
            guard.ctypes.cast(out, guard.ctypes.POINTER(guard.wintypes.DWORD)).contents.value = 123
            return 12

        def gui(_thread, out):
            info = guard.ctypes.cast(out, guard.ctypes.POINTER(win32.GUITHREADINFO)).contents
            info.hwndActive, info.hwndFocus = 55, 0
            return 1

        native = SimpleNamespace(GetForegroundWindow=lambda: 55, GetWindowThreadProcessId=owner,
                                 GetGUIThreadInfo=gui)
        with mock.patch.object(win32, "user32", native), \
             mock.patch.object(win32, "input_desktop", return_value="Default"):
            context = win32.foreground_context()
        self.assertTrue(win32.owned_foreground(context, 55, 123, "none"))
        self.assertFalse(win32.owned_foreground(context, 55, 123, "window"))
        self.assertEqual((context["thread_id"], context["input_desktop"]), (12, "Default"))

    def test_real_input_after_miss_blocks_recovery_without_clearing_latch(self):
        self.observer._trip("teardown diagnostic probe missed", unavailable=True)
        self.observer._trip("real keyboard input")
        send = mock.Mock()
        user, context, desktop, last_input = self._patched(send)
        with user, context, desktop, last_input:
            result = self.observer.probe_once(55, 123, "unicode_up", "window", recovery=True)
        self.assertEqual(result["status"], "real_input")
        self.assertEqual(self.observer.reason, "teardown diagnostic probe missed")
        send.assert_not_called()

    def test_new_session_input_tick_after_miss_blocks_recovery(self):
        send = mock.Mock(return_value=1)
        user, context, desktop, last_input = self._patched(send)
        with user, context, desktop, last_input as last_input_mock, \
             mock.patch.object(self.observer, "_post_heartbeat"), \
             mock.patch.object(guard, "PROBE_TIMEOUT", 0.005):
            self.assertEqual(self.observer.probe_once(55, 123, "unicode_up", "window")["status"],
                             "missed")
            last_input_mock.return_value = 101
            self.assertEqual(self.observer.probe_once(55, 123, "unicode_up", "window",
                                                      recovery=True)["status"], "input_since_miss")
        send.assert_called_once()

    def test_teardown_cli_is_opt_in_and_reports_inconclusive_as_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            with mock.patch.object(guard_diagnostic, "diagnose_teardown",
                                   return_value={"status": "inconclusive"}) as diagnose:
                code = guard_diagnostic.main(["--teardown", "--out", str(path)])
            self.assertEqual(code, 1)
            diagnose.assert_called_once_with()
            self.assertEqual(json.loads(path.read_text())["status"], "inconclusive")

    def test_teardown_deadline_is_fixed_and_does_not_change_normal_probe_timeout(self):
        self.assertEqual(guard_diagnostic.TEARDOWN_DEADLINE_SECONDS, 12)
        self.assertEqual(guard.PROBE_TIMEOUT, 0.5)

    def test_one_a_miss_allows_only_one_b_recovery_and_preserves_latch(self):
        a = mock.Mock(hwnd=55, restoration="skipped_teardown_stage")
        b = mock.Mock(hwnd=66, restoration="skipped_teardown_stage")
        for window in (a, b):
            window.open.return_value = True
            window.focused.return_value = True
            window._thread = SimpleNamespace(is_alive=lambda: False)
            window.diagnostics.return_value = {"status": "ready"}
        a.close.return_value = True
        observed = guard.TeardownDiagnosticObserver()

        def probe(_hwnd, _pid, _kind, _focus, recovery=False):
            if recovery:
                return {"status": "observed"}
            observed.tripped.set()
            return {"status": "missed"}

        observed.probe_once = mock.Mock(side_effect=probe)
        observed.start = mock.Mock(return_value=observed)
        observed.stop = mock.Mock()
        observed.diagnostics = mock.Mock(return_value={"hook_state": {
            "keyboard_callbacks": 1, "heartbeat_received": 1}})
        with mock.patch.object(guard_diagnostic.win32, "require_windows"), \
             mock.patch.object(guard_diagnostic.win32, "desktop_context", return_value={}), \
             mock.patch.object(guard_diagnostic.win32, "foreground_context",
                               return_value={"hwnd": 0, "process_id": 0,
                                             "gui_status": "no_foreground", "input_desktop": "Default"}), \
             mock.patch.object(guard_diagnostic.win32, "last_input_tick", return_value=100), \
             mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=[]), \
             mock.patch.object(guard_diagnostic, "_wait_for_safe_desktop", return_value=("ready", [])), \
             mock.patch.object(guard_diagnostic, "_OwnedProbeWindow", side_effect=[a, b]), \
             mock.patch.object(guard_diagnostic.guard, "TeardownDiagnosticObserver", return_value=observed):
            result = guard_diagnostic.diagnose_teardown()
        self.assertEqual(result["status"], "miss_recovered")
        self.assertTrue(result["latched_failure"])
        self.assertEqual(observed.probe_once.call_count, 2)
        self.assertEqual(observed.probe_once.call_args_list[0].args[0], 55)
        self.assertEqual(observed.probe_once.call_args_list[1].args[0], 66)
        self.assertTrue(observed.probe_once.call_args_list[1].kwargs["recovery"])
        a.clear_focus.assert_not_called()
        observed.start.assert_called_once()
        observed.stop.assert_called_once()

    def test_expired_teardown_deadline_sends_nothing(self):
        a = mock.Mock(hwnd=55)
        a.open.return_value = True
        a.focused.return_value = True
        a.diagnostics.return_value = {}
        observed = guard.TeardownDiagnosticObserver()
        observed.start = mock.Mock(return_value=observed)
        observed.stop = mock.Mock()
        observed.diagnostics = mock.Mock(return_value={})
        observed.probe_once = mock.Mock()
        with mock.patch.object(guard_diagnostic.win32, "require_windows"), \
             mock.patch.object(guard_diagnostic.win32, "desktop_context", return_value={}), \
             mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=[]), \
             mock.patch.object(guard_diagnostic, "_wait_for_safe_desktop", return_value=("ready", [])), \
             mock.patch.object(guard_diagnostic, "_OwnedProbeWindow", return_value=a), \
             mock.patch.object(guard_diagnostic.guard, "TeardownDiagnosticObserver", return_value=observed), \
             mock.patch.object(guard_diagnostic, "TEARDOWN_DEADLINE_SECONDS", 0):
            result = guard_diagnostic.diagnose_teardown()
        self.assertEqual(result["status"], "deadline")
        observed.probe_once.assert_not_called()

    def test_nonreproduction_runs_focus_null_then_teardown_then_b_and_is_inconclusive(self):
        a = mock.Mock(hwnd=55, restoration="skipped_teardown_stage")
        b = mock.Mock(hwnd=66, restoration="skipped_teardown_stage")
        for window in (a, b):
            window.open.return_value = True
            window.focused.return_value = True
            window._thread = SimpleNamespace(is_alive=lambda: False)
            window.diagnostics.return_value = {"status": "ready"}
        a.close.return_value = True
        a.clear_focus.return_value = True
        observed = guard.TeardownDiagnosticObserver()
        observed._thread_id = 91
        observed.start = mock.Mock(return_value=observed)
        observed.stop = mock.Mock()
        observed.diagnostics = mock.Mock(return_value={"hook_state": {
            "keyboard_callbacks": 6, "heartbeat_received": 6}})
        observed.probe_once = mock.Mock(return_value={"status": "observed"})
        with mock.patch.object(guard_diagnostic.win32, "require_windows"), \
             mock.patch.object(guard_diagnostic.win32, "desktop_context", return_value={}), \
             mock.patch.object(guard_diagnostic.win32, "foreground_context",
                               return_value={"hwnd": 0, "process_id": 0,
                                             "gui_status": "no_foreground", "input_desktop": "Default"}), \
             mock.patch.object(guard_diagnostic.win32, "last_input_tick", return_value=100), \
             mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=[]), \
             mock.patch.object(guard_diagnostic, "_wait_for_safe_desktop", return_value=("ready", [])), \
             mock.patch.object(guard_diagnostic, "_OwnedProbeWindow", side_effect=[a, b]), \
             mock.patch.object(guard_diagnostic.guard, "TeardownDiagnosticObserver", return_value=observed):
            result = guard_diagnostic.diagnose_teardown()
        self.assertEqual(result["status"], "inconclusive")
        self.assertFalse(result["latched_failure"])
        self.assertEqual(observed.probe_once.call_count, 6)
        self.assertEqual([call.args[0] for call in observed.probe_once.call_args_list],
                         [55, 55, 55, 55, 66, 66])
        a.clear_focus.assert_called_once()
        a.close.assert_any_call(restore=False)
        self.assertIn("a_after_teardown", [row["stage"] for row in result["stages"]])
