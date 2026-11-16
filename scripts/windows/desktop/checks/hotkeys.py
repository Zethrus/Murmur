"""H1-H7 checklist checks and opt-in H8 Notepad focus diagnostic."""
import json
import re
import time

import golden
import guard
import win32
from checks.common import BLOCKED_HOTKEY, CODE_HOTKEY, SHIFT_HOTKEY
from context import Fail, OutputCount


# Three copies take longer than the guard's one-second liveness interval at 12 ms/character.
DIRECT_CONTROL = "The quick brown fox jumps over the lazy dog. " * 3


def _key_trace_summary(trace, expected=None):
    """Compare the hook's injected VK_PACKET stream with the target and history oracles."""
    packets = [(scan, bool(flags & guard.LLKHF_UP)) for _, _, vk, scan, flags in trace["events"]
               if vk == guard.VK_PACKET]
    downs = [unit for unit, up in packets if not up]
    packet_text = b"".join(unit.to_bytes(2, "little") for unit in downs).decode("utf-16-le", "replace")
    return {"columns": ["time_ns", "message", "vk", "scan", "flags"],
            "events": trace["events"], "overflow": trace["overflow"],
            "packet_text": packet_text,
            "packet_pairs_match": (len(packets) % 2 == 0 and all(
                packets[i] == (packets[i + 1][0], False)
                and packets[i + 1] == (packets[i][0], True)
                for i in range(0, len(packets), 2))),
            "packet_difference": golden.compare(expected, packet_text) if expected is not None else None,
            "packet_alt_flag_events": sum(bool(flags & guard.LLKHF_ALTDOWN)
                                          for _, _, vk, _, flags in trace["events"]
                                          if vk == guard.VK_PACKET),
            "probe_events": sum(vk == guard.PROBE_VK for _, _, vk, _, _ in trace["events"])}


def h1_hold(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    pad = ctx.notepad()
    pad.focus()
    ctx.dictate("A", order="key-first")
    ctx.artifact("after-hotkey-focus.txt").write_text(pad.focus_diagnostic(), encoding="utf-8")
    text = pad.wait_text()
    ctx.settle()
    ctx.screenshot(pad.hwnd, "notepad.png")
    ctx.artifact("notepad-focus.txt").write_text(pad.focus_diagnostic(), encoding="utf-8")
    if pad.menu_open():
        win32.tap("esc")
        raise Fail("Notepad's menu was activated by the hotkey release")
    ctx.expect_delivery(text, "Notepad")


def _focus_phase(pad, stage):
    """Observe only: never send input or repair focus during a delivery."""
    focused = pad.uia.focused()
    foreground = win32.foreground()
    try:
        editor_focused = pad.editor_focused()
        editor_focus_error = None
    except Exception as error:  # noqa: BLE001 - stale UIA element must not end the probe
        editor_focused = None
        editor_focus_error = f"{type(error).__name__}: {error}"
    target_chars = None
    if stage == "before delivery wait":
        try:
            target_chars = len(pad.text())
        except Exception:  # noqa: BLE001 - document can rebuild during injection
            pass
    return {"stage": stage, "time": time.time(), "foreground": hex(foreground),
            "foreground_title": win32.window_text(foreground),
            "focus": pad.uia.describe(focused),
            "native_focus": win32.gui_thread_focus(pad.hwnd),
            "editor_mode_label": pad.editor_mode_label() if stage in
            ("before direct input", "before modifiers", "before delivery wait", "after delivery wait") else None,
            "editor_focused": editor_focused, "editor_focus_error": editor_focus_error,
            "target_chars": target_chars,
            "menu_focused": bool(focused) and pad.uia.control_type(focused) in
            (pad.uia.MENU, pad.uia.MENUBAR, pad.uia.MENUITEM),
            "modifiers_down": win32.keys_down(),
            "f9_down": "f9" in win32.keys_down(("f9",))}


def _notepad_focus_trial(ctx, spec, name, phases, previous_hwnd=None):
    mark = getattr(ctx.guard, "mark", lambda _stage: None)
    mark(f"H8:{name}:ensure-begin")
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"general": {"hotkey": spec}})
    mark(f"H8:{name}:ensure-complete")
    pad = ctx.notepad(name, reuse_hwnd=previous_hwnd, owned_tab=True)
    pad.focus()
    sample = lambda stage: phases.append(_focus_phase(pad, stage))
    modifiers, key = win32.parse_combo(spec)
    ctx.observe_delivery("A")
    ctx.guard.start_key_trace()
    observation = None
    try:
        sample("before modifiers")
        win32.key_down(*modifiers)
        sample("before F9 down")
        win32.key_down(key)
        sample("F9 held")
        if not ctx.murmur.wait_state("listening", 5):
            raise Fail(f"{name}: dictation did not start")
        sample("listening")
        ctx.sleep(ctx.fixtures["A"].speech_seconds + 0.8)
        sample("before F9 up")
        win32.key_up(key)
        sample("after F9 up")
        ctx.sleep(0.05)
        for modifier in reversed(modifiers):
            win32.key_up(modifier)
            sample(f"after {modifier} up")
        sample("before delivery wait")
        try:
            expected = ctx.observed_text()
            output_error = None
        except OutputCount as error:
            expected = None
            output_error = error
        actual = pad.wait_text(timeout=8, stable=1.2, sleep=ctx.sleep)
        ctx.settle()
        sample("after delivery wait")
        overlay = ctx.murmur.overlay_state()
        status = ("held_refused" if output_error and output_error.count == 0 and overlay == "held"
                  else "no_output" if output_error and output_error.count == 0
                  else "multiple_outputs" if output_error else "output")
        observation = {"status": status, "output_error": str(output_error) if output_error else None,
                       "overlay": overlay, "target_chars": len(actual)}
        return pad, golden.compare(expected, actual) if expected is not None else None, expected, observation
    finally:
        # The run's fail-closed guard handles user input. This only releases keys owned by
        # the harness if sampling or dictation fails before the normal key-up sequence.
        win32.release_all()
        trace = ctx.guard.stop_key_trace()
        if observation is not None:
            observation["key_trace"] = _key_trace_summary(trace, expected)


def _notepad_direct_input(ctx, pad, phases, text):
    """Same guarded Unicode sender and interval as the app, without hotkey or inference."""
    pad.focus()
    ctx.guard.check()
    if win32.keys_down():
        raise Fail("direct control started with a modifier held")
    probes_before = ctx.guard.diagnostics()["probes_seen"]
    phases.append(_focus_phase(pad, "before direct input"))
    ctx.guard.start_key_trace()
    try:
        win32.type_unicode(text, delay=0.012)
    finally:
        trace = ctx.guard.stop_key_trace()
    ctx.guard.check()
    probes_after = ctx.guard.diagnostics()["probes_seen"]
    phases.append(_focus_phase(pad, "after direct input"))
    actual = pad.wait_text(timeout=8, stable=1.2, sleep=ctx.sleep)
    phases.append(_focus_phase(pad, "after direct readback"))
    return pad, golden.compare(text, actual), text, {
        "probes_during_input": probes_after - probes_before, "target_chars": len(actual),
        "key_trace": _key_trace_summary(trace, text)}


def _notepad_direct_control(ctx, phases):
    """First control: a fresh document before either hotkey trial."""
    pad = ctx.notepad("control", owned_tab=True)
    return _notepad_direct_input(ctx, pad, phases, DIRECT_CONTROL)


def _capture_notepad_trial(ctx, name, result, pad, expected):
    """Capture the settled document and exact file bytes without closing its tab."""
    ctx.screenshot(pad.hwnd, f"{name}-notepad.png")
    captured = pad.text()
    result["capture_difference"] = golden.compare(expected, captured) if expected is not None else None
    result["capture_chars"] = len(captured)
    try:
        before_save = pad.path.stat().st_mtime_ns
    except OSError:
        before_save = None
    pad.save_owned()
    try:
        after_save = pad.path.stat().st_mtime_ns
        result["saved_file_updated"] = before_save != after_save
        saved_bytes = pad.path.read_bytes()
        saved_artifact = ctx.artifact(f"{name}-saved.txt")
        saved_artifact.write_bytes(saved_bytes)
        result["saved_file_artifact"] = saved_artifact.name
        saved = saved_bytes.decode("utf-8-sig")
        result["saved_file_difference"] = golden.compare(expected, saved) if expected is not None else None
        result["saved_file_chars"] = len(saved)
    except (OSError, UnicodeError) as error:
        result["saved_file_error"] = f"{type(error).__name__}: {error}"


def h8_focus_phases(ctx):
    """Opt-in comparison; keep each owned fixture through the next ensure and target focus."""
    results = {}
    artifact = ctx.artifact("focus-phases.json")
    previous_hwnd = None
    try:
        for name, spec in (("control", None), ("alt", "Ctrl+Alt+Shift+F9"),
                           ("no-alt", SHIFT_HOTKEY)):
            log_start = len(ctx.murmur.log()) if name != "control" else 0
            results[name] = {"hotkey": spec, "phases": [], "difference": None,
                             "capture_difference": None, "saved_file_difference": None, "saved_file_updated": None,
                             "saved_file_error": None, "saved_file_artifact": None}
            try:
                if name == "control":
                    pad, difference, expected, observation = _notepad_direct_control(
                        ctx, results[name]["phases"])
                else:
                    pad, difference, expected, observation = _notepad_focus_trial(
                        ctx, spec, name, results[name]["phases"], previous_hwnd)
                results[name]["observation"] = observation
                results[name]["difference"] = difference
            finally:
                if name != "control":
                    log_artifact = ctx.artifact(f"{name}-app-log.txt")
                    log_artifact.write_text(ctx.murmur.log()[log_start:], encoding="utf-8")
                    results[name]["app_log_artifact"] = log_artifact.name
            _capture_notepad_trial(ctx, name, results[name], pad, expected)
            previous_hwnd = pad.hwnd
            if name == "no-alt" and any(results[name][key] for key in
                                        ("difference", "capture_difference", "saved_file_difference")):
                # The failed trial's screenshot and saved bytes are already captured. Reuse
                # exactly that owned editor to separate a Murmur-specific sender path from a
                # Notepad/desktop state that also corrupts the harness's direct sender.
                comparison = results["same-tab-direct"] = {
                    "hotkey": None, "phases": [], "difference": None,
                    "capture_difference": None, "saved_file_difference": None,
                    "saved_file_updated": None, "saved_file_error": None,
                    "saved_file_artifact": None,
                }
                pad.clear_owned()
                _, comparison["difference"], _, comparison["observation"] = _notepad_direct_input(
                    ctx, pad, comparison["phases"], expected)
                _capture_notepad_trial(ctx, "same-tab-direct", comparison, pad, expected)
    finally:
        artifact.write_text(json.dumps(results, indent=2), encoding="utf-8")
    differences = [f"{name}: {reason}" for name, result in results.items()
                   for reason in (result.get("observation", {}).get("output_error"), result["difference"],
                                  result["capture_difference"],
                                  result["saved_file_difference"], result["saved_file_error"])
                   if reason]
    if differences:
        raise Fail("; ".join(differences))


def h2_release_order(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    app = ctx.target("A")
    for order in ("together", "modifiers-held"):
        app.clear()
        app.focus()
        ctx.dictate("A", order=order)
        text = app.wait_text()
        ctx.settle()
        ctx.sleep(1.5)
        if ctx.murmur.overlay_state() == "listening":
            raise Fail(f"Release order {order}: dictation restarted")
        ctx.artifact(f"release-{order}-target.json").write_text(
            json.dumps(app.state(), ensure_ascii=False, indent=2), encoding="utf-8")
        ctx.expect_delivery(text, f"release order {order}")


def h3_shift_held(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"general": {"hotkey": SHIFT_HOTKEY}})
    app = ctx.target("A")
    app.focus()
    released = ctx.dictate("A", hotkey=SHIFT_HOTKEY, order="key-first")
    ctx.expect_delivery(app.wait_text(), "clean release")
    clean_latency = app.first_key_time() - released
    ctx.settle()
    app.clear()
    app.focus()
    shift_up = ctx.dictate("A", hotkey=SHIFT_HOTKEY, order="shift-late")
    text = app.wait_text()
    ctx.settle()
    if not text:
        raise Fail("Nothing delivered with Shift held after the key")
    first = app.first_key()
    # Tk reports the modifier state of each key event; bit 0x1 is Shift. Unicode (VK_PACKET)
    # typing keeps its character either way, so the state bit is what shows B8.
    if first is None or first.get("state") is None:
        raise Fail("TargetApp recorded no key state for the first character")
    if first["state"] & 0x1:
        raise Fail(f"First character {first.get('char')!r} arrived while Shift was still held (B8)")
    ctx.expect_delivery(text, "Shift held 300 ms")
    after_shift = app.first_key_time() - shift_up
    after_key = after_shift + 0.3
    ctx.note(f"first character {after_shift:.2f} s after Shift up; clean release latency {clean_latency:.2f} s")
    # B8: typing waits for Shift, then starts within 0.6 s. When transcription itself takes
    # longer than the 300 ms Shift hold, compare against the clean latency instead.
    if after_shift > 0.6 and after_key > clean_latency + 0.6:
        raise Fail(f"Delivery started {after_shift:.2f} s after Shift was released (limit 0.6 s; clean "
                   f"latency {clean_latency:.2f} s)")


def h4_toggle(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"general": {"hotkey_mode": "toggle"}})
    app = ctx.target("A")
    app.focus()
    ctx.dictate("A", mode="toggle")
    text = app.wait_text()
    ctx.settle()
    ctx.expect_delivery(text, "second toggle dictation")


def h5_escape(ctx):
    # Toggle mode: in hold mode Escape would join Ctrl+Shift (Ctrl+Shift+Esc opens Task Manager).
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"general": {"hotkey_mode": "toggle"}})
    app = ctx.target("A")
    app.focus()
    ctx.tap_hotkey(ctx.hotkey)
    if not ctx.murmur.wait_state("listening", 5):
        raise Fail(f"Dictation did not start (overlay: {ctx.murmur.overlay_state()})")
    ctx.sleep(1.5)
    win32.tap("esc")
    ctx.sleep(5.0)
    state = ctx.murmur.overlay_state()
    if app.text():
        raise Fail(f"Text delivered after Escape: {app.text()!r}")
    if state not in ("idle", "unknown"):
        raise Fail(f"Overlay did not return to idle after Escape (state {state})")


def h6_conflict(ctx):
    """A shortcut another app owns: Murmur must refuse it and keep the previous one.

    Driven through the config file (what Settings saves and what Murmur reloads), not by pressing
    the combination into Settings' capture field: Windows delivers a RegisterHotKey-owned
    combination only to its owner as WM_HOTKEY, so the capture field never sees it (true for a
    person pressing Win+E too). The harness owns the combination itself, so nothing else opens."""
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    blocker = win32.HotkeyBlocker(BLOCKED_HOTKEY).start()
    ctx.restorer.push("release the blocking hotkey", blocker.stop)
    ctx.restorer.push("restore the config file", lambda: ctx.murmur.write_config({}))
    log_before = len(ctx.murmur.log())
    ctx.murmur.write_config({"general": {"hotkey": BLOCKED_HOTKEY}})
    needle = "is unavailable"
    names_seen = []

    def refused():
        names = ctx.murmur.overlay_names()
        if not names_seen or names != names_seen[-1]:
            names_seen.append(names)
        message = next((name for name in names if "shortcut" in name.lower() and needle in name), None)
        if message:
            return message
        tail = ctx.murmur.log()[log_before:]
        return next((line for line in tail.splitlines()
                     if "shortcut" in line.lower() and needle in line), None)

    evidence = win32.wait_for(refused, 10, 0.2, sleep=ctx.sleep)
    tail = ctx.murmur.log()[log_before:]
    diagnostic = [f"registered blocker: {BLOCKED_HOTKEY}",
                  f"config contains blocker: {BLOCKED_HOTKEY in ctx.murmur.config_path.read_text(encoding='utf-8')}",
                  f"process running: {ctx.murmur.running()}", "overlay accessible-name snapshots:",
                  *[f"  {names!r}" for names in names_seen], "reload/rebind log lines:",
                  *[line for line in tail.splitlines() if any(term in line.lower() for term in
                    ("config", "reload", "shortcut", "rebind", "unavailable"))]]
    ctx.artifact("rebind-diagnostic.txt").write_text("\n".join(diagnostic) + "\n", encoding="utf-8")
    ctx.screenshot(ctx.murmur.overlay_hwnd(), "overlay.png")
    # Put the file back to the running config (reload compares and finds nothing to change).
    ctx.murmur.write_config({})
    ctx.sleep(2.5)  # the error state resets after 1.6 s
    blocker.stop()
    ctx.note("conflict driven through the config file with a combination the harness registered "
             "(RegisterHotKey); pressing it into the capture field cannot work on Windows, see docstring")
    app = ctx.target("A")
    app.focus()
    ctx.dictate("A")
    text = app.wait_text()
    ctx.settle()
    if not evidence:
        raise Fail(f"No 'Shortcut ... {needle}' error after configuring a shortcut another app owns "
                   f"(overlay: {names_seen[-1][:6] if names_seen else []!r}; "
                   f"previous hotkey delivered: {bool(text)})")
    ctx.note(f"error: {evidence.strip()[:200]!r}")
    ctx.expect_delivery(text, "previous hotkey after the conflict")


def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def h7_code(ctx):
    prose = ctx.golden("A")
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"general": {"hotkey_code": CODE_HOTKEY}})
    app = ctx.target("editor", role="editor")
    app.focus()
    ctx.dictate("A", hotkey=CODE_HOTKEY)
    text = app.wait_text()
    ctx.settle()
    ctx.expect_delivery(text, "code hotkey")
    if _words(text) != _words(prose):
        raise Fail(f"Code-style text has different words than prose: {text!r} vs {prose!r}")
    ctx.note(f"prose {prose!r}; code {text!r}")
