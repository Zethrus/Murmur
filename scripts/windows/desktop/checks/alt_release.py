"""H10: opt-in, bounded Alt-release diagnostic in owned Notepad tabs."""
import ctypes
import json
import time
import tomllib

import golden
import guard
import win32
from checks import notepad_delivery as h9
from checks.common import NOTEPAD_OUTPUT_TIMEOUT, SHIFT_HOTKEY
from checks.hotkeys import _key_trace_summary
from context import Fail, OutputCount, TargetCreationError
from murmur import StartupError


ALT_HOTKEY = h9.ALT_HOTKEY
UNBOUND_HOTKEY = "Ctrl+Alt+Shift+F10"
PRESS = ("ctrl", "alt", "shift", "f9")
# Shift is always the first modifier released. Only F9 position and Alt/Ctrl
# order vary; every row sends the same Ctrl+Alt+Shift+F9 press chord.
RELEASES = (
    ("f9-first-alt-before-ctrl", ("f9", "shift", "alt", "ctrl")),
    ("f9-first-alt-after-ctrl", ("f9", "shift", "ctrl", "alt")),
    ("f9-last-alt-before-ctrl", ("shift", "alt", "ctrl", "f9")),
    ("f9-last-alt-after-ctrl", ("shift", "ctrl", "alt", "f9")),
)
TRIALS = tuple(
    (context, name, chord, release, binding)
    for context, control_binding in (("configured-in-Murmur", SHIFT_HOTKEY),
                                     ("not-configured-in-Murmur", UNBOUND_HOTKEY))
    for name, chord, release, binding in (
        (("non-alt-control", SHIFT_HOTKEY, ("f9", "shift", "ctrl"), control_binding),)
        + tuple((name, ALT_HOTKEY, order,
                 ALT_HOTKEY if context == "configured-in-Murmur" else UNBOUND_HOTKEY)
                for name, order in RELEASES))
)


def restorable_clipboard_text():
    try:
        return h9.restorable_clipboard_text()
    except Fail as error:
        raise Fail(str(error).replace("H9 refused", "H10 refused", 1)) from error


def _same_chord(left, right):
    if not left:
        return False
    a_mods, a_key = win32.parse_combo(left)
    b_mods, b_key = win32.parse_combo(right)
    return set(a_mods) == set(b_mods) and a_key == b_key


def _verify_binding(ctx, binding, chord, row):
    """Prove the disposable profile's binding and live app readiness, not OS exclusivity."""
    session = ctx.murmur
    try:
        disk = tomllib.loads(session.config_path.read_text(encoding="utf-8"))
        configured = session.config
        hwnd = session.overlay_hwnd()
        pid = session.process.pid
        ready = session.running() and hwnd and win32.is_window(hwnd) and win32.window_pid(hwnd) == pid
        def shortcuts(config):
            return [config.get("general", {}).get("hotkey", ""),
                    config.get("general", {}).get("hotkey_code", ""),
                    config.get("injection", {}).get("insert_held_hotkey", ""),
                    config.get("enhance", {}).get("rewrite_hotkey", ""),
                    config.get("enhance", {}).get("accept_hotkey", "")]
        bindings = shortcuts(configured)
        disk_bindings = shortcuts(disk)
        disk_binding = disk.get("general", {}).get("hotkey")
        row["binding_evidence"] = {"actual_os_registration": "unverified",
                                   "requested": binding, "profile": disk_binding,
                                   "session": bindings[0], "overlay_hwnd": hwnd,
                                   "overlay_pid": win32.window_pid(hwnd) if hwnd else None,
                                   "process_pid": pid, "ready": bool(ready),
                                   "profile_shortcuts": disk_bindings,
                                   "session_shortcuts": bindings,
                                   "profile_injection": disk.get("injection", {}),
                                   "session_injection": configured.get("injection", {})}
        if not ready or disk_binding != binding or bindings[0] != binding or disk_bindings != bindings:
            raise Fail("Murmur binding/readiness could not be verified")
        active = any(_same_chord(value, chord) for value in bindings if value)
        if active != _same_chord(binding, chord):
            raise Fail("Murmur configuration does not establish the requested chord distinction")
        if any(config.get("injection", {}).get(key) != value
               for config in (disk, configured)
               for key, value in (("method", "type"), ("prefer_paste", False),
                                  ("clipboard_only", True))):
            raise Fail("Murmur clipboard-only configuration could not be verified")
        row["binding_evidence"]["chord_in_murmur_config"] = active
    except guard.UserInput:
        raise
    except (OSError, ValueError, KeyError, AttributeError) as error:
        raise Fail(f"Murmur binding/readiness unavailable: {type(error).__name__}: {error}") from error


def _stamp(ctx, pad, row, stage):
    """Native-only focus observation at an edge; a foreign or unknown owner stops input."""
    ctx.guard.check()
    phase = {"stage": stage, "time_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(),
             "focus_snapshot": "native_only"}
    row["phases"].append(phase)
    try:
        native = win32.foreground_context()
        phase["native_focus"] = native
        focus = native.get("focus_hwnd")
        if (native.get("input_desktop") != "Default" or native.get("gui_status") != "ok" or
                native.get("hwnd") != pad.hwnd or native.get("process_id") != pad.owner_pid or
                native.get("active_hwnd") != pad.hwnd or not focus or
                win32.user32.GetAncestor(focus, win32.GA_ROOT) != pad.hwnd):
            raise Fail(f"{stage}: Notepad foreground/focus ownership changed or is unknown")
    except guard.UserInput:
        raise
    except Fail:
        raise
    except Exception as error:  # noqa: BLE001 - failed native read cannot authorize input
        phase["native_focus_error"] = f"{type(error).__name__}: {error}"
        raise Fail(f"{stage}: native focus unavailable") from error
    ctx.guard.check()


def _release_owned(held):
    """Only release keys this trial sent down; guard-trip cleanup is key-up only."""
    errors = []
    for key in reversed(held):
        try:
            try:
                win32.key_up(key)
            except guard.UserInput:
                # The input guard rejects ordinary SendInput after a trip. Release
                # the tracked synthetic down key without disabling the guard.
                event = win32._key_input(win32.VK[key], True)
                array = (win32.INPUT * 1)(event)
                if win32.user32.SendInput(1, array, ctypes.sizeof(win32.INPUT)) != 1:
                    raise RuntimeError(f"could not release owned {key} after guard trip")
            held.remove(key)
        except Exception as error:  # noqa: BLE001 - try the remaining owned keys
            errors.append(f"{key}: {type(error).__name__}: {error}")
    if errors:
        raise RuntimeError("; ".join(errors))


def _drive(ctx, pad, chord, release, row):
    modifiers, key = win32.parse_combo(chord)
    press = tuple(modifiers) + (key,)
    if set(press) != set(release) or len(press) != len(release):
        raise ValueError("release sequence must contain each pressed key exactly once")
    held = []
    _stamp(ctx, pad, row, "before first key down")
    try:
        for name in press:
            ctx.guard.check()
            win32.key_down(name)
            held.append(name)
            _stamp(ctx, pad, row, f"after {name} down")
        dwell_started = time.monotonic_ns()
        ctx.sleep(ctx.fixtures["A"].speech_seconds + 0.8)
        row["held_dwell_ns"] = time.monotonic_ns() - dwell_started
        for name in release:
            ctx.guard.check()
            win32.key_up(name)
            held.remove(name)
            _stamp(ctx, pad, row, f"after {name} up")
        ctx.mark_delivery_released()
    finally:
        if held:
            try:
                _release_owned(held)
            except Exception as error:  # noqa: BLE001 - preserve the original failure
                row["owned_key_cleanup_error"] = f"{type(error).__name__}: {error}"
                ctx.guard.block_input(f"H10 owned key release failed: {error}")


def _trace_problem(trace, chord, release):
    if trace["overflow"]:
        return "key trace overflowed"
    modifiers, key = win32.parse_combo(chord)
    expected = [(win32.VK[name], False) for name in (*modifiers, key)]
    expected += [(win32.VK[name], True) for name in release]
    actual = [(h9.MODIFIER_KEYS.get(vk, vk), bool(flags & guard.LLKHF_UP))
              for _, _, vk, _, flags in trace["events"] if vk != guard.PROBE_VK]
    if actual != expected:
        return f"chord edges differ: expected {expected!r}, got {actual!r}"
    if any(not flags & guard.LLKHF_INJECTED for _, _, vk, _, flags in trace["events"]
           if vk != guard.PROBE_VK):
        return "chord trace contains a non-injected key event"
    return None


def _trial(ctx, pad, name, chord, release, registered, row):
    if registered:
        ctx.observe_delivery("A")
        cursor = ctx._delivery_cursor
    else:
        cursor = ctx.murmur.history_cursor()
    row["history_cursor_before"] = cursor
    row["probes_before"] = ctx.guard.diagnostics().get("probes_seen")
    ctx.guard.start_key_trace()
    trace = None
    try:
        _drive(ctx, pad, chord, release, row)
        if registered:
            try:
                ctx.observed_text(timeout=NOTEPAD_OUTPUT_TIMEOUT)
            except (OutputCount, Fail) as error:
                row["output_error"] = str(error)
            row["history_wait_elapsed_s"] = ctx._delivery_wait_elapsed
            state = ctx.settle(timeout=NOTEPAD_OUTPUT_TIMEOUT)
        else:
            # The chord is unbound only in Murmur's verified profile. Do not
            # wait for listening or for an output from another global shortcut.
            ctx.sleep(1.2)
            state = ctx.murmur.overlay_state()
        row["overlay"] = state
        if state not in ("done", "idle", "held"):
            raise Fail(f"{name}: Murmur did not settle (overlay: {state})")
        if not registered and state != "idle":
            row["overlay_error"] = f"unbound Murmur chord changed overlay to {state}"
        if registered:
            ctx.sleep(1.2)
        _stamp(ctx, pad, row, "settled")
        rows = ctx.murmur.history_after(cursor)
        row["history_count"] = len(rows)
        if registered:
            if len(rows) != 1:
                row["history_error"] = f"expected one history output, found {len(rows)}"
                expected = None
            else:
                expected = rows[0][1]
                row["history_error"] = golden.fixture_fidelity("A", expected)
                ctx.artifact(f"{name}-history.txt").write_text(expected, encoding="utf-8")
            row["clipboard"] = h9._read_settled_clipboard(row)
            row["clipboard_difference"] = (golden.compare(expected, row["clipboard"])
                                           if expected is not None else "no unique history oracle for clipboard")
        else:
            if rows:
                row["history_error"] = f"unbound Murmur chord produced {len(rows)} history outputs"
            row["clipboard"] = h9._read_settled_clipboard(row)
            row["clipboard_difference"] = golden.compare(row["sentinel"], row["clipboard"])
    finally:
        try:
            trace = ctx.guard.stop_key_trace()
            row["key_trace"] = _key_trace_summary(trace)
            row["keyboard_error"] = _trace_problem(trace, chord, release)
        except guard.UserInput:
            row["trace_error"] = "guard tripped before key trace could be read"
        except Exception as error:  # noqa: BLE001 - preserve prior trial evidence
            row["trace_error"] = f"{type(error).__name__}: {error}"
        try:
            row["probes_after"] = ctx.guard.diagnostics().get("probes_seen")
            log_path = ctx.artifact(f"{name}-app-log.txt")
            log_path.write_text(ctx.murmur.log(since_launch=True), encoding="utf-8")
            row["app_log_artifact"] = log_path.name
        except (OSError, UnicodeError) as error:
            row["app_log_error"] = f"{type(error).__name__}: {error}"


FAILURE_FIELDS = ("binding_error", "output_error", "history_error", "overlay_error", "clipboard_error",
                  "clipboard_difference", "keyboard_error", "trace_error", "owned_key_cleanup_error",
                  "uia_difference", "uia_error", "focus_error", "saved_file_difference", "saved_file_error")


def h10_alt_release(ctx):
    """One ten-trial matrix; prior failures remain latched while safe tabs continue."""
    restorable_clipboard_text()
    ctx.clipboard_guard()
    results = {}
    artifact = ctx.artifact("alt-release.json")
    previous_hwnd = None
    stopped = None
    failure_type = Fail
    try:
        for context, variant, chord, release, binding in TRIALS:
            name = f"{context}-{variant}"
            registered = _same_chord(chord, binding)
            row = results[name] = {"registration_context": context, "chord": chord,
                                   "binding": binding, "release_order": list(release), "phases": [],
                                   "interpretation": "diagnostic only; other global shortcuts may exist"}
            ctx.guard.check()
            ctx.murmur.ensure(ctx.fixtures["A"].path, h9._config("clipboard_only", binding))
            _verify_binding(ctx, binding, chord, row)
            pad = ctx.notepad(name, reuse_hwnd=previous_hwnd, owned_tab=True)
            pad.require_editor_cleanup = True
            pad.focus()
            ctx.guard.check()
            if not pad.active_document() or not pad.owns_keyboard_focus() or pad.text() != "":
                raise Fail(f"{name}: fresh owned Notepad editor is not empty and focused")
            if ctx.startup_probe_lease is not None and not ctx.startup_probe_lease.closed:
                raise Fail("passive startup probe did not hand off to verified Notepad")
            previous_hwnd = pad.hwnd
            sentinel = f"H10 clipboard sentinel {name}"
            win32.set_clipboard_text(sentinel)
            row["sentinel"] = sentinel
            if win32.clipboard_text() != sentinel:
                raise Fail(f"{name}: clipboard sentinel could not be verified")
            _trial(ctx, pad, name, chord, release, registered, row)
            if not pad.active_document():
                raise Fail(f"{name}: active tab ownership changed after chord")
            h9._settled_capture(ctx, pad, name, "", row)
            if not pad.active_document():
                raise Fail(f"{name}: active tab ownership changed after capture")
            _stamp(ctx, pad, row, "after capture")
            if row.get("uia_error") or row.get("saved_file_error", "").startswith(
                    ("save refused: editor focus unavailable", "save refused: active tab")):
                raise Fail(f"{name}: settled editor ownership could not be verified")
            if not pad.owns_keyboard_focus() and not pad.menu_open():
                raise Fail(f"{name}: settled focus left the owned editor without a verified menu")
    except guard.UserInput:
        raise
    except StartupError:
        raise
    except (Fail, RuntimeError) as error:
        stopped = f"stopped before further input: {type(error).__name__}: {error}"
        if isinstance(error, TargetCreationError):
            failure_type = TargetCreationError
    finally:
        artifact.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    failures = [f"{name}: {row[field]}" for name, row in results.items()
                for field in FAILURE_FIELDS if row.get(field)]
    if stopped:
        failures.append(stopped)
    if failures:
        raise failure_type("; ".join(failures))
