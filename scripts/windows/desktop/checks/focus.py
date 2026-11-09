"""F1-F8, F11-F13: the focus contract (docs/focus-contract.md) with TargetApp windows."""
import time

import win32
from checks.common import (D_HOTKEY, HOLD_FOR_SECONDS, click_overlay, focus_change_dictation, produce_held,
                           require_empty, wait_held, wait_not_held)
from context import Fail


def f1_same_window(ctx):
    expected = ctx.golden("A")
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    app = ctx.target("A")
    app.focus()
    ctx.dictate("A")
    ctx.expect(expected, app.wait_text(), "same window")
    ctx.settle()


def f2_focus_change(ctx):
    expected = ctx.golden("A")
    a, b = ctx.target("A"), ctx.target("B")
    produce_held(ctx, "A", a, b)
    ctx.screenshot(ctx.murmur.overlay_hwnd(), "held.png")
    require_empty(a, b)
    b.focus()
    click_overlay(ctx, "Insert here", expect_foreground=b.hwnd)
    ctx.expect(expected, b.wait_text(), "Insert here into the focused window")
    require_empty(a)
    wait_not_held(ctx)


def f3_window_replaced(ctx):
    ctx.golden("A")
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    a = ctx.target("A")
    a.focus()
    replacement = {}

    def replace(c):
        a.close()
        replacement["app"] = c.target("A2")
        replacement["app"].focus()

    ctx.dictate("A", during=replace)
    wait_held(ctx)
    require_empty(replacement["app"])
    click_overlay(ctx, "Discard", expect_foreground=replacement["app"].hwnd)
    wait_not_held(ctx)


def f4_copy_discard_expiry(ctx):
    expected = ctx.golden("A")
    ctx.clipboard_guard()
    a, b = ctx.target("A"), ctx.target("B")
    win32.set_clipboard_text("before")
    produce_held(ctx, "A", a, b)
    click_overlay(ctx, "Copy", expect_foreground=b.hwnd)
    ctx.sleep(0.5)
    clipboard = win32.clipboard_text() or ""
    if clipboard.strip() != expected.strip():
        raise Fail(f"Copy put {clipboard!r} on the clipboard, expected {expected.strip()!r}")
    wait_not_held(ctx)
    produce_held(ctx, "A", a, b)
    click_overlay(ctx, "Discard", expect_foreground=b.hwnd)
    if not wait_not_held(ctx):
        raise Fail("Discard did not clear the held transcript")
    produce_held(ctx, "A", a, b)
    held_at = time.monotonic()
    ctx.sleep(HOLD_FOR_SECONDS - 10)
    if ctx.murmur.overlay_state() != "held":
        raise Fail(f"Held transcript disappeared before {HOLD_FOR_SECONDS} s")
    if not win32.wait_for(lambda: ctx.murmur.overlay_state() != "held", 25, 0.5, sleep=ctx.sleep):
        raise Fail(f"Held transcript did not expire within {HOLD_FOR_SECONDS + 15} s")
    ctx.note(f"expired after {time.monotonic() - held_at:.0f} s")
    require_empty(a, b)


def f5_refocus(ctx):
    expected = ctx.golden("A")
    config = {"injection": {"focus_change": "refocus"}}
    a, b = ctx.target("A"), ctx.target("B")
    focus_change_dictation(ctx, "A", a, b, config)
    ctx.sleep(1.0)
    state = win32.wait_for(lambda: a.text() or (ctx.murmur.overlay_state() == "held" and "held"), 30, 0.2,
                           sleep=ctx.sleep)
    require_empty(b)
    if state == "held":
        ctx.note("Windows refused to refocus A: the transcript was held (accepted outcome)")
        click_overlay(ctx, "Discard")
        return
    ctx.expect(expected, a.wait_text(), "refocused window A")


def f6_copy_mode(ctx):
    expected = ctx.golden("A")
    config = {"injection": {"focus_change": "copy"}}
    ctx.clipboard_guard()
    win32.set_clipboard_text("before")
    a, b = ctx.target("A"), ctx.target("B")
    focus_change_dictation(ctx, "A", a, b, config)
    ctx.sleep(3.0)
    names = ctx.murmur.overlay_names()
    ctx.screenshot(ctx.murmur.overlay_hwnd(), "notice.png")
    ctx.settle()
    require_empty(a, b)
    clipboard = win32.clipboard_text() or ""
    if clipboard.strip() != expected.strip():
        raise Fail(f"Copy mode put {clipboard!r} on the clipboard, expected {expected.strip()!r}")
    ctx.note(f"overlay after delivery: {names[:6]}")


def f7_insert_mode(ctx):
    expected = ctx.golden("A")
    a, b = ctx.target("A"), ctx.target("B")
    focus_change_dictation(ctx, "A", a, b, {"injection": {"focus_change": "insert"}})
    ctx.expect(expected, b.wait_text(), "new focus (insert mode)")
    require_empty(a)
    ctx.settle()


def f8_from_settings(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    hwnd = ctx.murmur.show_settings()
    app = ctx.target("A")
    left, top, right, bottom = win32.window_rect(hwnd)
    if not win32.activate(hwnd, click_at=((left + right) // 2, top + 15)):
        raise Fail("Could not focus the Settings window")
    ctx.dictate("A", during=lambda c: app.focus())
    wait_held(ctx)
    require_empty(app)
    click_overlay(ctx, "Discard", expect_foreground=app.hwnd)


def f11_drag_delivery(ctx):
    expected = ctx.golden("A")
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"overlay": {"idle_view": "pill"}})
    ctx.restorer.push("reset the overlay placement", lambda: setattr(ctx.murmur, "config", {}))
    app = ctx.target("A")
    app.focus()
    grabbed = {}

    def grab(_ctx):
        # Grab the pill by its drag handle (left end) while the hotkey is still held, so the
        # transcript cannot be delivered before the drag has started.
        left, top, right, bottom = win32.window_rect(ctx.murmur.overlay_hwnd())
        handle = (left + max(8, round(14 * win32.dpi_scale(ctx.murmur.overlay_hwnd()))), (top + bottom) // 2)
        win32.drag(handle, (handle[0] + 80, handle[1] - 60), steps=10, release=False)
        grabbed["origin"] = (left, top)

    ctx.restorer.push("release the mouse button", win32.mouse_up)
    ctx.dictate("A", during=grab)
    left, top = grabbed["origin"]
    ctx.sleep(4.0)
    early = app.text()
    released = time.time()
    win32.mouse_up()
    text = app.wait_text()
    ctx.settle()
    moved = win32.window_rect(ctx.murmur.overlay_hwnd())
    if early:
        raise Fail(f"Text was delivered while the overlay was being dragged: {early!r}")
    if moved[:2] == (left, top):
        raise Fail("The overlay did not move: the drag handle position guess missed")
    first = app.first_key_time()
    if first is None or first < released:
        raise Fail("Delivery did not wait for the mouse release")
    ctx.expect(expected, text, "after the drag")


def f12_log_privacy(ctx):
    texts = [t.strip() for t in ctx.goldens.texts() if len(t.strip()) >= 8]
    if not texts:
        expected = ctx.golden("A")
        texts = [expected.strip()]
    log = ctx.murmur.log()
    path = ctx.artifact("log-excerpt.txt")
    path.write_text("\n".join(log.splitlines()[-200:]) + "\n", encoding="utf-8")
    leaked = [t for t in texts if t in log or t.lower() in log.lower()]
    if leaked:
        raise Fail(f"Transcript text appears in murmur.log: {leaked[0]!r}")
    ctx.note(f"{len(texts)} golden texts searched in {len(log.splitlines())} log lines")


def _hold_modifiers_through_delivery(ctx, app, expected):
    """Direct delivery with Ctrl+Alt still physically held while the transcript is typed: the
    path where a typed 'd' could fire WM_HOTKEY again (hotkey.rs ignore_dictation_press)."""
    modifiers, key = win32.parse_combo(D_HOTKEY)
    app.focus()
    ctx.press(D_HOTKEY)
    if not ctx.murmur.wait_state("listening", 5):
        ctx.release(D_HOTKEY, "together")
        raise Fail(f"Dictation did not start (overlay: {ctx.murmur.overlay_state()})")
    ctx.restorer.push("release the hotkey modifiers", lambda: win32.key_up(*reversed(modifiers)))
    ctx.sleep(ctx.fixtures["D"].speech_seconds + 0.8)
    win32.key_up(key)
    restarted = False
    deadline = time.monotonic() + 20
    # Keep Ctrl+Alt down past Murmur's 600 ms modifier wait, until typing has finished.
    while time.monotonic() < deadline:
        state = ctx.murmur.overlay_state()
        restarted = restarted or (state == "listening" and bool(app.text()))
        if app.text() and time.monotonic() - (app.keys()[-1]["t"] if app.keys() else 0) > 1.5:
            break
        ctx.sleep(0.05)
    typed_while_held = bool(app.text())
    win32.key_up(*reversed(modifiers))
    text = app.wait_text()
    restarted = restarted or win32.wait_for(lambda: ctx.murmur.overlay_state() == "listening", 2.0, 0.05,
                                            sleep=ctx.sleep)
    ctx.settle()
    if restarted:
        raise Fail("Typing a 'd' with Ctrl+Alt held restarted dictation")
    if not typed_while_held:
        ctx.note("typing had not started while Ctrl+Alt were held (20 s); the retrigger path was not reached")
        raise Fail("Delivery did not start while the modifiers were held, so the retrigger path was not tested")
    ctx.note(f"typed with Ctrl+Alt held: {text!r}")
    if text != expected:
        # Characters typed with Ctrl+Alt down may be interpreted as AltGr combinations by the target.
        ctx.note(f"text differs from the clean golden {expected!r} (modifiers were down while typing)")


def f13_no_retrigger(ctx):
    expected = ctx.golden("D", hotkey=D_HOTKEY)
    if "d" not in expected.lower():
        raise Fail(f"The fixture transcript has no 'd': {expected!r}")
    ctx.murmur.ensure(ctx.fixtures["D"].path, {"general": {"hotkey": D_HOTKEY}})
    _hold_modifiers_through_delivery(ctx, ctx.target("direct"), expected)
    for prefer_paste in (False, True):
        config = {"general": {"hotkey": D_HOTKEY}, "injection": {"prefer_paste": prefer_paste}}
        a, b = ctx.target(f"A{int(prefer_paste)}"), ctx.target(f"B{int(prefer_paste)}")
        ctx.murmur.ensure(ctx.fixtures["D"].path, config)
        a.focus()
        ctx.dictate("D", hotkey=D_HOTKEY, during=lambda c: b.focus())
        wait_held(ctx)
        b.focus()
        click_overlay(ctx, "Insert here", expect_foreground=b.hwnd)
        text = b.wait_text()
        restarted = win32.wait_for(lambda: ctx.murmur.overlay_state() == "listening", 2.0, 0.05, sleep=ctx.sleep)
        ctx.settle()
        if restarted:
            raise Fail(f"Typing the transcript restarted dictation (prefer paste {prefer_paste})")
        ctx.expect(expected, text, f"Insert here (prefer paste {prefer_paste})")
        require_empty(a)
        wait_not_held(ctx)
