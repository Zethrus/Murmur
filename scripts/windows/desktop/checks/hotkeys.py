"""H1-H7 checklist checks and opt-in H8 Notepad focus diagnostic."""
import json
import re
import time

import golden
import win32
from checks.common import BLOCKED_HOTKEY, CODE_HOTKEY, SHIFT_HOTKEY
from context import Fail


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
            "editor_focused": editor_focused, "editor_focus_error": editor_focus_error,
            "target_chars": target_chars,
            "menu_focused": bool(focused) and pad.uia.control_type(focused) in
            (pad.uia.MENU, pad.uia.MENUBAR, pad.uia.MENUITEM),
            "modifiers_down": win32.keys_down(),
            "f9_down": "f9" in win32.keys_down(("f9",))}


def _notepad_focus_trial(ctx, spec, name, phases):
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"general": {"hotkey": spec}})
    pad = ctx.notepad(name)
    pad.focus()
    sample = lambda stage: phases.append(_focus_phase(pad, stage))
    modifiers, key = win32.parse_combo(spec)
    ctx.observe_delivery("A")
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
        expected = ctx.observed_text()
        actual = pad.wait_text(timeout=8, stable=1.2, sleep=ctx.sleep)
        ctx.settle()
        sample("after delivery wait")
        difference = golden.compare(expected, actual)
        return pad, difference, expected
    finally:
        # The run's fail-closed guard handles user input. This only releases keys owned by
        # the harness if sampling or dictation fails before the normal key-up sequence.
        win32.release_all()


def h8_focus_phases(ctx):
    """Opt-in native probe for Notepad's Alt-menu failure; keeps the exact history oracle."""
    results = {}
    artifact = ctx.artifact("focus-phases.json")
    try:
        for name, spec in (("alt", "Ctrl+Alt+Shift+F9"), ("no-alt", SHIFT_HOTKEY)):
            results[name] = {"hotkey": spec, "phases": [], "difference": None,
                             "saved_file_difference": None, "saved_file_updated": None,
                             "saved_file_error": None}
            pad, difference, expected = _notepad_focus_trial(ctx, spec, name, results[name]["phases"])
            results[name]["difference"] = difference
            # Preserve the input-free UIA verdict, then capture independent visual evidence
            # before close() changes focus or sends Ctrl+S. The saved file is a second readback
            # of this disposable fixture document, not a replacement for the exact UIA oracle.
            ctx.screenshot(pad.hwnd, f"{name}-notepad.png")
            # Windows 11 Notepad may reuse the same top-level window for another tab.
            # Close this harness-owned document before starting the comparison trial.
            try:
                before_save = pad.path.stat().st_mtime_ns
            except OSError:
                before_save = None
            pad.close()
            try:
                after_save = pad.path.stat().st_mtime_ns
                results[name]["saved_file_updated"] = before_save != after_save
                saved = pad.path.read_text(encoding="utf-8-sig")
                results[name]["saved_file_difference"] = golden.compare(expected, saved)
            except (OSError, UnicodeError) as error:
                results[name]["saved_file_error"] = f"{type(error).__name__}: {error}"
    finally:
        artifact.write_text(json.dumps(results, indent=2), encoding="utf-8")
    differences = [f"{name}: {result['difference']}" for name, result in results.items() if result["difference"]]
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
