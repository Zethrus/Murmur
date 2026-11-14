"""F1-F8, F11-F13: the focus contract (docs/focus-contract.md) with TargetApp windows."""
import time

import win32
from checks.common import (D_HOTKEY, HOLD_FOR_SECONDS, click_overlay, focus_change_dictation, produce_held,
                           require_empty, wait_held, wait_not_held)
from context import Fail


def f1_same_window(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    app = ctx.target("A")
    app.focus()
    ctx.dictate("A")
    ctx.expect_delivery(app.wait_text(), "same window")
    ctx.settle()


def f2_focus_change(ctx):
    a, b = ctx.target("A"), ctx.target("B")
    produce_held(ctx, "A", a, b)
    ctx.screenshot(ctx.murmur.overlay_hwnd(), "held.png")
    require_empty(a, b)
    b.focus()
    click_overlay(ctx, "Insert here", expect_foreground=b.hwnd)
    ctx.expect_delivery(b.wait_text(), "Insert here into the focused window")
    require_empty(a)
    wait_not_held(ctx)


def f3_window_replaced(ctx):
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
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    ctx.clipboard_guard()
    a, b = ctx.target("A"), ctx.target("B")
    win32.set_clipboard_text("before")
    produce_held(ctx, "A", a, b)
    click_overlay(ctx, "Copy", expect_foreground=b.hwnd)
    ctx.sleep(0.5)
    clipboard = win32.clipboard_text() or ""
    ctx.expect_delivery(clipboard, "Copy clipboard")
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
    ctx.expect_delivery(a.wait_text(), "refocused window A")


def f6_copy_mode(ctx):
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
    ctx.expect_delivery(clipboard, "Copy mode clipboard")
    ctx.note(f"overlay after delivery: {names[:6]}")


def f7_insert_mode(ctx):
    a, b = ctx.target("A"), ctx.target("B")
    focus_change_dictation(ctx, "A", a, b, {"injection": {"focus_change": "insert"}})
    ctx.expect_delivery(b.wait_text(), "new focus (insert mode)")
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
        grabbed["engaged"] = win32.wait_for(
            lambda: abs(win32.window_rect(ctx.murmur.overlay_hwnd())[0] - left) >= 20,
            2, 0.1, sleep=ctx.sleep)

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
    if not grabbed["engaged"]:
        raise Fail("Overlay did not move while the mouse button was held; drag-release safety was not exercised")
    if early:
        raise Fail(f"Text was delivered while the overlay was being dragged: {early!r}")
    if moved[:2] == (left, top):
        raise Fail("The overlay did not move: the drag handle position guess missed")
    first = app.first_key_time()
    if first is None or first < released:
        raise Fail("Delivery did not wait for the mouse release")
    ctx.expect_delivery(text, "after the drag")


def f12_log_privacy(ctx):
    texts = [t.strip() for t in dict.fromkeys([row[1] for row in ctx.murmur.history_after(0)])
             if len(t.strip()) >= 8]
    if not texts:
        expected = ctx.golden("A")
        texts = [expected.strip()]
    log = ctx.murmur.log()
    path = ctx.artifact("log-excerpt.txt")
    path.write_text("\n".join(log.splitlines()[-200:]) + "\n", encoding="utf-8")
    leaked = [t for t in texts if t in log or t.lower() in log.lower()]
    if leaked:
        raise Fail(f"Transcript text appears in murmur.log: {leaked[0]!r}")
    ctx.note(f"{len(texts)} observed outputs searched in {len(log.splitlines())} log lines")


def _hold_modifiers_through_delivery(ctx, app):
    """Direct delivery with Ctrl+Alt still physically held while the transcript is typed: the
    path where a typed 'd' could fire WM_HOTKEY again (hotkey.rs ignore_dictation_press)."""
    modifiers, key = win32.parse_combo(D_HOTKEY)
    app.focus()
    ctx.observe_delivery("D")
    log_before = len(ctx.murmur.log())
    started = time.monotonic()
    timeline = []

    def record(stage):
        state = ctx.murmur.overlay_state()
        item = (round(time.monotonic() - started, 3), stage, state, len(app.text()), len(app.delivered()),
                app.first_key_time())
        if not timeline or item[1:3] != timeline[-1][1:3] or item[3:5] != timeline[-1][3:5]:
            timeline.append(item)
        return state

    ctx.press(D_HOTKEY)
    record("hotkey down")
    if not ctx.murmur.wait_state("listening", 5):
        ctx.release(D_HOTKEY, "together")
        raise Fail(f"Dictation did not start (overlay: {ctx.murmur.overlay_state()})")
    record("listening")
    ctx.restorer.push("release the hotkey modifiers", lambda: win32.key_up(*reversed(modifiers)))
    ctx.sleep(ctx.fixtures["D"].speech_seconds + 0.8)
    win32.key_up(key)
    record("D up; Ctrl+Alt held")
    restarted = False
    deadline = time.monotonic() + 20
    # Keep Ctrl+Alt down past Murmur's 600 ms modifier wait, until typing has finished.
    while time.monotonic() < deadline:
        state = record("Ctrl+Alt held")
        restarted = restarted or (state == "listening" and bool(app.text()))
        typed = app.delivered()
        if typed and time.monotonic() - typed[-1]["t"] > 1.5:
            break
        ctx.sleep(0.05)
    typed_while_held = bool(app.text())
    win32.key_up(*reversed(modifiers))
    record("modifiers up")
    text = app.wait_text()
    restarted = restarted or win32.wait_for(lambda: ctx.murmur.overlay_state() == "listening", 2.0, 0.05,
                                            sleep=ctx.sleep)
    ctx.settle()
    record("settled")
    log_tail = ctx.murmur.log()[log_before:]
    lines = ["time_s\tstage\toverlay\ttarget_chars\tdelivered_keys\tfirst_key_epoch",
             *["\t".join(map(str, item)) for item in timeline], "app milestones:",
             *[line for line in log_tail.splitlines() if any(phrase in line for phrase in
               ("utterance captured", "transcript received", "transcript delivered", "utterance discarded"))],
             f"new history outputs: {len(ctx.murmur.history_after(ctx._delivery_cursor))}"]
    ctx.artifact("delivery-timing.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if restarted:
        raise Fail("Typing a 'd' with Ctrl+Alt held restarted dictation")
    if not typed_while_held:
        ctx.note("typing had not started while Ctrl+Alt were held (20 s); the retrigger path was not reached")
        raise Fail("Delivery did not start while the modifiers were held, so the retrigger path was not tested")
    ctx.note(f"typed with Ctrl+Alt held: {text!r}")
    observed = ctx.observed_text()
    if "d" not in observed.lower():
        raise Fail("The D fixture output did not contain a retrigger key")
    if text != observed:
        # Characters typed with Ctrl+Alt down may be interpreted as AltGr combinations by the target.
        ctx.note(f"target differed from this utterance's output {observed!r} "
                 "(modifiers were down while typing)")


def f13_no_retrigger(ctx):
    ctx.murmur.ensure(ctx.fixtures["D"].path, {"general": {"hotkey": D_HOTKEY}})
    ctx.clipboard_guard()  # the Prefer paste pass goes through the clipboard
    _hold_modifiers_through_delivery(ctx, ctx.target("direct"))
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
        ctx.expect_delivery(text, f"Insert here (prefer paste {prefer_paste})")
        require_empty(a)
        wait_not_held(ctx)
