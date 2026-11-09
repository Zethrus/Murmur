"""H1-H7: hold, release orders, B8 Shift, toggle, Escape, conflicts, code hotkey."""
import re

import win32
from checks.common import BLOCKED_HOTKEY, CODE_HOTKEY, SHIFT_HOTKEY
from context import Fail


def h1_hold(ctx):
    expected = ctx.golden("A")
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    pad = ctx.notepad()
    pad.focus()
    ctx.dictate("A", order="key-first")
    text = pad.wait_text()
    ctx.settle()
    ctx.screenshot(pad.hwnd, "notepad.png")
    if pad.menu_open():
        win32.tap("esc")
        raise Fail("Notepad's menu was activated by the hotkey release")
    ctx.expect(expected, text, "Notepad")


def h2_release_order(ctx):
    expected = ctx.golden("A")
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
        ctx.expect(expected, text, f"release order {order}")


def h3_shift_held(ctx):
    expected = ctx.golden("A", hotkey=SHIFT_HOTKEY)
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"general": {"hotkey": SHIFT_HOTKEY}})
    app = ctx.target("A")
    app.focus()
    released = ctx.dictate("A", hotkey=SHIFT_HOTKEY, order="key-first")
    ctx.expect(expected, app.wait_text(), "clean release")
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
    ctx.expect(expected, text, "Shift held 300 ms")
    after_shift = app.first_key_time() - shift_up
    after_key = after_shift + 0.3
    ctx.note(f"first character {after_shift:.2f} s after Shift up; clean release latency {clean_latency:.2f} s")
    # B8: typing waits for Shift, then starts within 0.6 s. When transcription itself takes
    # longer than the 300 ms Shift hold, compare against the clean latency instead.
    if after_shift > 0.6 and after_key > clean_latency + 0.6:
        raise Fail(f"Delivery started {after_shift:.2f} s after Shift was released (limit 0.6 s; clean "
                   f"latency {clean_latency:.2f} s)")


def h4_toggle(ctx):
    expected = ctx.golden("A", mode="toggle")
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"general": {"hotkey_mode": "toggle"}})
    app = ctx.target("A")
    app.focus()
    ctx.dictate("A", mode="toggle")
    text = app.wait_text()
    ctx.settle()
    ctx.expect(expected, text, "second toggle dictation")


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
    expected = ctx.golden("A")
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    blocker = win32.HotkeyBlocker(BLOCKED_HOTKEY).start()
    ctx.restorer.push("release the blocking hotkey", blocker.stop)
    ctx.restorer.push("restore the config file", lambda: ctx.murmur.write_config({}))
    log_before = len(ctx.murmur.log())
    ctx.murmur.write_config({"general": {"hotkey": BLOCKED_HOTKEY}})
    needle = "is unavailable"

    def refused():
        state = ctx.murmur.overlay_state()
        if state.startswith("error:") and needle in state:
            return state
        tail = ctx.murmur.log()[log_before:]
        return next((line for line in tail.splitlines() if needle in line), None)

    evidence = win32.wait_for(refused, 10, 0.2, sleep=ctx.sleep)
    ctx.screenshot(ctx.murmur.overlay_hwnd(), "overlay.png")
    # Put the file back to the running config (reload compares and finds nothing to change).
    ctx.murmur.write_config({})
    ctx.sleep(2.5)  # the error state resets after 1.6 s
    blocker.stop()
    ctx.note("conflict driven through the config file with a combination the harness registered "
             "(RegisterHotKey); pressing it into the capture field cannot work on Windows, see docstring")
    if not evidence:
        raise Fail(f"No 'Shortcut ... {needle}' error after configuring a shortcut another app owns "
                   f"(overlay: {ctx.murmur.overlay_state()})")
    ctx.note(f"error: {evidence.strip()[:200]!r}")
    app = ctx.target("A")
    app.focus()
    ctx.dictate("A")
    ctx.expect(expected, app.wait_text(), "previous hotkey after the conflict")


def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def h7_code(ctx):
    prose = ctx.golden("A")
    expected = ctx.golden("A", hotkey=CODE_HOTKEY, style="code")
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"general": {"hotkey_code": CODE_HOTKEY}})
    app = ctx.target("editor", role="editor")
    app.focus()
    ctx.dictate("A", hotkey=CODE_HOTKEY)
    text = app.wait_text()
    ctx.settle()
    ctx.expect(expected, text, "code hotkey")
    if _words(text) != _words(prose):
        raise Fail(f"Code-style text has different words than prose: {text!r} vs {prose!r}")
    ctx.note(f"prose {prose!r}; code {text!r}")
