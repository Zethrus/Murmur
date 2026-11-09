"""H1-H7: hold, release orders, B8 Shift, toggle, Escape, conflicts, code hotkey."""
import re

import uia
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
    if text[0] != expected[0]:
        raise Fail(f"First character {text[0]!r} differs from the clean release ({expected[0]!r}): shifted")
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
    expected = ctx.golden("A")
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    blocker = win32.HotkeyBlocker(BLOCKED_HOTKEY).start()
    ctx.restorer.push("release the blocking hotkey", blocker.stop)
    window = ctx.murmur.page("General")
    button = uia.find_following(window, "Dictation shortcut", control_type_=uia.BUTTON, timeout=10, sleep=ctx.sleep)
    win32.activate(ctx.murmur.settings_hwnd(), click_at=uia.center(button))
    win32.click(*uia.center(button))
    ctx.sleep(0.5)
    ctx.press(BLOCKED_HOTKEY)
    ctx.sleep(0.2)
    ctx.release(BLOCKED_HOTKEY, "together")
    names = win32.wait_for(lambda: [n for n in uia.names(window) if "unavailable" in n.lower()], 5, 0.25,
                           sleep=ctx.sleep)
    ctx.screenshot(ctx.murmur.settings_hwnd(), "conflict.png")
    capturing = any(n == "Press a combination…" for n in uia.names(window))
    if capturing:
        win32.tap("esc")
    ctx.note("the conflicting shortcut is held by the harness (RegisterHotKey) instead of Win+E, so Explorer "
             "does not open")
    blocker.stop()
    app = ctx.target("A")
    app.focus()
    ctx.dictate("A")
    ctx.expect(expected, app.wait_text(), "previous hotkey after the conflict")
    if not names:
        raise Fail("No inline 'unavailable' error" + (": the capture never received the key (the owner of a "
                                                       "registered hotkey swallows it)" if capturing else ""))
    ctx.note(f"error: {names[0]!r}")


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
