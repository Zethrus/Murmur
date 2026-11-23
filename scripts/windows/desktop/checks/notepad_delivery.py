"""H9: bounded Notepad delivery-path diagnostic on one guarded desktop run."""
import json
import time

import golden
import guard
import win32
from checks.common import NOTEPAD_OUTPUT_TIMEOUT, SHIFT_HOTKEY
from checks.hotkeys import (_drive_registered_hotkey, _focus_phase, _key_trace_summary,
                            _notepad_direct_input)
from context import Fail, OutputCount, TargetCreationError
from murmur import StartupError


ALT_HOTKEY = "Ctrl+Alt+Shift+F9"
DIRECT_A = "The quick brown fox jumps over the lazy dog."
TEXT_CLIPBOARD_FORMATS = {1, 7, win32.CF_UNICODETEXT, 16}  # CF_TEXT, CF_OEMTEXT, CF_LOCALE
MODIFIER_KEYS = {0xA0: win32.VK["shift"], 0xA1: win32.VK["shift"],
                 0xA2: win32.VK["ctrl"], 0xA3: win32.VK["ctrl"],
                 0xA4: win32.VK["alt"], 0xA5: win32.VK["alt"]}
# Fixed order: the two clipboard-only paths precede any app keyboard delivery.
TRIALS = (("control", "direct", None),
          ("clipboard-alt", "clipboard_only", ALT_HOTKEY),
          ("clipboard-no-alt", "clipboard_only", SHIFT_HOTKEY),
          ("type-alt", "type", ALT_HOTKEY),
          ("type-no-alt", "type", SHIFT_HOTKEY),
          ("paste-alt", "paste", ALT_HOTKEY),
          ("paste-no-alt", "paste", SHIFT_HOTKEY))


def _config(path, hotkey):
    # `clipboard_only` takes precedence in inject::choose_path. `clipboard` is the
    # existing paste method; false values prevent an earlier trial leaking through.
    return {"general": {"hotkey": hotkey},
            "injection": {"method": "clipboard" if path == "paste" else "type",
                          "prefer_paste": False, "clipboard_only": path == "clipboard_only"}}


def restorable_clipboard_text():
    """Return the text snapshot, refusing formats a text-only Restorer would discard."""
    before = win32.clipboard_text()
    formats = set(win32.clipboard_formats())
    if (before is None and formats or formats - TEXT_CLIPBOARD_FORMATS):
        raise Fail("H9 refused: clipboard contains formats that cannot be restored as text")
    return before


def _clipboard_trace_problem(trace, hotkey):
    """Hotkey and guard probe are expected; no app keyboard delivery is allowed."""
    if trace["overflow"]:
        return "key trace overflowed; zero delivery keys cannot be proved"
    modifiers, key = win32.parse_combo(hotkey)
    allowed = {win32.VK[name] for name in (*modifiers, key)} | {guard.PROBE_VK}
    events = [(MODIFIER_KEYS.get(vk, vk), flags) for _, _, vk, _, flags in trace["events"]]
    unexpected = [vk for vk, _ in events if vk not in allowed]
    if unexpected:
        return f"clipboard-only emitted delivery keyboard events: {unexpected!r}"
    for name in (*modifiers, key):
        edges = [bool(flags & guard.LLKHF_UP) for vk, flags in events
                 if vk == win32.VK[name]]
        if edges != [False, True]:
            return f"registered hotkey {name} was not a paired down/up: {edges!r}"
    return None


def _paste_trace_problem(trace):
    if trace["overflow"]:
        return "key trace overflowed; paste gesture cannot be proved"
    if any(vk == guard.VK_PACKET for _, _, vk, _, _ in trace["events"]):
        return "paste path emitted VK_PACKET text"
    v_edges = [bool(flags & guard.LLKHF_UP) for _, _, vk, _, flags in trace["events"]
               if vk == win32.VK["v"]]
    if v_edges != [False, True]:
        return f"paste path did not emit one paired Ctrl+V key: {v_edges!r}"
    held = set()
    modifiers = {win32.VK[name] for name in ("ctrl", "alt", "shift", "win")}
    for _, _, vk, _, flags in trace["events"]:
        vk = MODIFIER_KEYS.get(vk, vk)
        if vk in modifiers:
            if flags & guard.LLKHF_UP:
                held.discard(vk)
            else:
                held.add(vk)
        elif vk == win32.VK["v"] and held != {win32.VK["ctrl"]}:
            return "paste path did not hold only Ctrl throughout V down/up"
    if held:
        return "paste path left modifiers held in the trace"
    return None


def _read_settled_clipboard(row):
    try:
        return win32.clipboard_text()
    except guard.UserInput:
        raise
    except Exception as error:  # noqa: BLE001 - read-only evidence cannot authorize retry input
        row["clipboard_error"] = f"{type(error).__name__}: {error}"
        return None


def _settled_capture(ctx, pad, name, expected, row):
    """Read first; save only if this exact editor still owns keyboard focus."""
    ctx.screenshot(pad.hwnd, f"{name}-notepad.png")
    ctx.guard.check()
    try:
        active = pad.active_document()
    except guard.UserInput:
        raise
    except Exception as error:  # noqa: BLE001 - an unknown tab cannot be read or saved
        row["uia_error"] = f"active tab unavailable: {type(error).__name__}: {error}"
        row["saved_file_error"] = "save refused: active tab could not be verified"
        return
    if not active:
        row["uia_error"] = "read refused: active tab is no longer the owned document"
        row["saved_file_error"] = "save refused: active tab is no longer the owned document"
        return
    try:
        actual = pad.text()
        row["uia_chars"] = len(actual)
        row["uia_difference"] = golden.compare(expected, actual) if expected is not None else None
        ctx.artifact(f"{name}-uia.txt").write_text(actual, encoding="utf-8")
    except guard.UserInput:
        raise
    except Exception as error:  # noqa: BLE001 - settled read-only evidence may be unavailable
        row["uia_error"] = f"{type(error).__name__}: {error}"
    try:
        detail = pad.focus_diagnostic()
        ctx.artifact(f"{name}-focus-before-save.txt").write_text(detail, encoding="utf-8")
    except guard.UserInput:
        raise
    except Exception as error:  # noqa: BLE001 - evidence never authorizes input
        row["focus_error"] = f"{type(error).__name__}: {error}"
    ctx.guard.check()
    try:
        editor_ready = pad.owns_keyboard_focus()
    except guard.UserInput:
        raise
    except Exception as error:  # noqa: BLE001 - a failed focus read never authorizes Ctrl+S
        row["saved_file_error"] = f"save refused: editor focus unavailable: {type(error).__name__}: {error}"
        return
    if not editor_ready:
        row["saved_file_error"] = "save refused: owned editor no longer has verified keyboard focus"
        return
    try:
        before = pad.path.stat().st_mtime_ns
        ctx.guard.check()
        if not pad.owns_keyboard_focus():
            row["saved_file_error"] = "save refused: owned editor focus changed before Ctrl+S"
            return
        ctx.guard.check()
        win32.tap("ctrl", "s")
        if not win32.wait_for(lambda: pad._saved_owned_document(ctx.guard.check), 5, sleep=ctx.sleep):
            raise RuntimeError("owned Notepad save did not settle")
        if not pad._saved_owned_document(ctx.guard.check):
            raise RuntimeError("owned Notepad changed after save")
        saved = pad.path.read_bytes()
        ctx.artifact(f"{name}-saved.txt").write_bytes(saved)
        row["saved_file_updated"] = pad.path.stat().st_mtime_ns != before
        decoded = saved.decode("utf-8-sig")
        row["saved_file_chars"] = len(decoded)
        row["saved_file_difference"] = golden.compare(expected, decoded) if expected is not None else None
    except guard.UserInput:
        raise
    except Exception as error:  # noqa: BLE001 - save failure remains evidence; next trial gets a new tab
        row["saved_file_error"] = f"{type(error).__name__}: {error}"


def _app_trial(ctx, pad, name, path, hotkey, row):
    phases = row["phases"]
    # After this preflight, phase stamps do no UIA/native focus reads until settled.
    phases.append(_focus_phase(pad, "before modifiers"))
    stamp = lambda stage: phases.append({"stage": stage, "time": time.time(),
                                         "focus_snapshot": "deferred"})
    ctx.guard.start_key_trace()
    trace = None
    expected = None
    try:
        _drive_registered_hotkey(ctx, hotkey, name, stamp, stamp)
        cursor = ctx._delivery_cursor  # same cursor used by observed_text, taken before key-down
        stamp("before history wait")
        try:
            ctx.observed_text(timeout=NOTEPAD_OUTPUT_TIMEOUT)
        except (OutputCount, Fail) as error:
            row["output_error"] = str(error)
        except guard.UserInput:
            raise
        except Exception as error:  # noqa: BLE001 - history read is settled evidence only
            row["output_error"] = f"history read unavailable: {type(error).__name__}: {error}"
        stamp("history wait complete")
        state = ctx.settle(timeout=NOTEPAD_OUTPUT_TIMEOUT)
        row["overlay"] = state
        if state not in ("done", "idle", "held"):
            raise Fail(f"{name}: delivery did not settle (overlay: {state})")
        ctx.sleep(1.2)
        stamp("settled")
        try:
            rows = ctx.murmur.history_after(cursor)
        except guard.UserInput:
            raise
        except Exception as error:  # noqa: BLE001 - retain the initial history error separately
            row["settled_history_error"] = f"history read unavailable: {type(error).__name__}: {error}"
            rows = []
        row["history_count"] = len(rows)
        if len(rows) != 1:
            row.setdefault("settled_history_error", f"settled history has {len(rows)} outputs, expected one")
            expected = None
        else:
            expected = rows[0][1]
            fidelity = golden.fixture_fidelity("A", expected)
            if fidelity:
                row.setdefault("output_error", fidelity)
            ctx.artifact(f"{name}-history.txt").write_text(expected, encoding="utf-8")
        try:
            phases.append(_focus_phase(pad, "after delivery wait"))
        except guard.UserInput:
            raise
        except Exception as error:  # noqa: BLE001 - settled focus evidence may be stale
            row["focus_error"] = f"{type(error).__name__}: {error}"
        row["clipboard"] = _read_settled_clipboard(row)
        row["clipboard_difference"] = (golden.compare(expected, row["clipboard"])
                                       if path == "clipboard_only" and expected is not None else None)
        if path == "clipboard_only" and expected is None:
            row["clipboard_difference"] = "clipboard output has no unique history oracle"
        elif path != "clipboard_only" and row["clipboard"] != row["sentinel"]:
            row["clipboard_difference"] = golden.compare(row["sentinel"], row["clipboard"])
        return expected
    finally:
        win32.release_all()
        trace = ctx.guard.stop_key_trace()
        try:
            log = ctx.artifact(f"{name}-app-log.txt")
            log.write_text(ctx.murmur.log(since_launch=True), encoding="utf-8")
            row["app_log_artifact"] = log.name
        except (OSError, UnicodeError) as error:
            row["app_log_error"] = f"{type(error).__name__}: {error}"
        row["key_trace"] = _key_trace_summary(trace, expected if path == "type" else None)
        if path == "clipboard_only":
            row["keyboard_error"] = _clipboard_trace_problem(trace, hotkey)
        elif trace["overflow"]:
            row["keyboard_error"] = "key trace overflowed"
        elif path == "type" and expected is not None and (
                not row["key_trace"]["packet_pairs_match"] or row["key_trace"]["packet_difference"]):
            row["keyboard_error"] = (row["key_trace"]["packet_difference"] or
                                     "typed VK_PACKET events were not paired")
        elif path == "paste":
            row["keyboard_error"] = _paste_trace_problem(trace)


def h9_notepad_delivery(ctx):
    """Seven trials, one each. Diagnostic failures never clear ordinary release failures."""
    # Restorer can preserve text or an empty clipboard, but cannot reconstruct other
    # formats. Refuse before either the app or harness can overwrite non-text data.
    restorable_clipboard_text()
    ctx.clipboard_guard()
    results = {}
    artifact = ctx.artifact("notepad-delivery.json")
    previous_hwnd = None
    stopped = None
    failure_type = Fail
    try:
        for name, path, hotkey in TRIALS:
            row = results[name] = {"path": path, "hotkey": hotkey, "phases": [],
                                   "interpretation": "diagnostic only; H1/I1/I4/I5 remain release oracles"}
            if hotkey is not None:
                ctx.murmur.ensure(ctx.fixtures["A"].path, _config(path, hotkey))
            pad = ctx.notepad(name, reuse_hwnd=previous_hwnd, owned_tab=True)
            pad.focus()
            ctx.guard.check()
            if not pad.active_document() or not pad.owns_keyboard_focus() or pad.text() != "":
                raise Fail(f"{name}: fresh owned Notepad editor is not empty and focused")
            if ctx.startup_probe_lease is not None and not ctx.startup_probe_lease.closed:
                raise Fail("passive startup probe did not hand off to the verified Notepad editor")
            previous_hwnd = pad.hwnd
            sentinel = f"H9 clipboard sentinel {name}"
            win32.set_clipboard_text(sentinel)
            row["sentinel"] = sentinel
            if path == "direct":
                _, row["direct_difference"], expected, observation = _notepad_direct_input(
                    ctx, pad, row["phases"], DIRECT_A)
                row["key_trace"] = observation["key_trace"]
                row["probes_during_input"] = observation["probes_during_input"]
                if (row["key_trace"].get("overflow") or
                        not row["key_trace"].get("packet_pairs_match", True) or
                        row["key_trace"].get("packet_difference")):
                    row["keyboard_error"] = (row["key_trace"].get("packet_difference") or
                                             "direct Unicode key trace incomplete")
                row["clipboard"] = _read_settled_clipboard(row)
                row["clipboard_difference"] = golden.compare(sentinel, row["clipboard"])
            else:
                expected = _app_trial(ctx, pad, name, path, hotkey, row)
                row["history_text"] = expected
            _settled_capture(ctx, pad, name, expected if path != "clipboard_only" else "", row)
    except StartupError:
        raise  # run_checks captures startup diagnostics and stops the run
    except (Fail, RuntimeError) as error:
        # Target creation/focus and unsettled delivery stop further input. The JSON and
        # earlier trial failures remain available and count toward the final result.
        stopped = f"stopped before further input: {type(error).__name__}: {error}"
        if isinstance(error, TargetCreationError):
            failure_type = TargetCreationError
    finally:
        artifact.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    failures = [f"{name}: {value}" for name, row in results.items()
                for field, value in row.items() if field in
                ("output_error", "settled_history_error", "app_log_error", "direct_difference", "clipboard_difference",
                 "clipboard_error", "keyboard_error", "focus_error", "uia_difference", "uia_error",
                 "saved_file_difference", "saved_file_error") and value]
    if stopped:
        failures.append(stopped)
    if failures:
        raise failure_type("; ".join(failures))
