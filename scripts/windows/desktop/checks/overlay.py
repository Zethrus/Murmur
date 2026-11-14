"""O1-O4, O6, O7: overlay states, drag and restore, B7 focus, scale, subclass log line."""
import ctypes
import re

import fixtures
import uia
import win32
from checks.common import click_overlay, produce_held, wait_not_held
from context import Fail

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000


def _exstyle(hwnd):
    win32.user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    return win32.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)


def _reset_placement(ctx):
    """Saved drags live in config.toml; force the next ensure() to rewrite the file."""
    ctx.murmur.config = {}


def o1_states(ctx):
    long = ctx.fixtures["LONG"]
    ctx.murmur.ensure(long.path, {"overlay": {"idle_view": "pill"}})
    hwnd = ctx.murmur.overlay_hwnd()
    style = _exstyle(hwnd)
    ctx.note(f"overlay ex-style 0x{style:08X} (tool window: {bool(style & WS_EX_TOOLWINDOW)}, "
             f"no-activate: {bool(style & WS_EX_NOACTIVATE)})")
    app = ctx.target("A")
    app.focus()
    ctx.screenshot(hwnd, "overlay-idle.png")
    seen = {ctx.murmur.overlay_state()}
    ctx.press(ctx.hotkey)
    if not ctx.murmur.wait_state("listening", 5):
        ctx.release(ctx.hotkey, "together")
        raise Fail(f"No listening state (overlay: {ctx.murmur.overlay_state()})")
    seen.add("listening")
    ctx.sleep(long.speech_seconds * 0.6)
    ctx.screenshot(hwnd, "overlay-listening.png")
    ctx.sleep(long.speech_seconds * 0.4 + 0.5)
    ctx.release(ctx.hotkey, "key-first")
    for _ in range(600):
        state = ctx.murmur.overlay_state()
        if state not in seen and state in ("transcribing", "done"):
            seen.add(state)
            ctx.screenshot(hwnd, f"overlay-{state}.png")
        if state == "done" or (state == "idle" and "transcribing" in seen):
            break
        ctx.sleep(0.05)
    app.wait_text()
    # Error state: a launch whose QA WAV does not exist cannot open audio.
    ctx.murmur.restart(wav=fixtures.missing_wav(ctx.cache))
    ctx.press(ctx.hotkey)
    error = win32.wait_for(lambda: (s := ctx.murmur.overlay_state()).startswith("other:") and s, 10, 0.1,
                           sleep=ctx.sleep)
    ctx.release(ctx.hotkey, "together")
    if error:
        seen.add("error")
        ctx.screenshot(ctx.murmur.overlay_hwnd(), "overlay-error.png")
        ctx.note(f"error label: {error[len('other:'):]}")
    ctx.murmur.stop()  # the next check relaunches with a real WAV
    missing = [s for s in ("listening", "transcribing", "done", "error") if s not in seen]
    if missing:
        raise Fail(f"Overlay states not observed: {', '.join(missing)} (seen: {sorted(seen)}); "
                   "'transcribing' can be shorter than the 50 ms polling with tiny.en")


def o2_drag(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"overlay": {"idle_view": "icon"}})
    ctx.restorer.push("reset the overlay placement", lambda: _reset_placement(ctx))
    hwnd = ctx.murmur.overlay_hwnd()
    log_before = len(ctx.murmur.log())
    before = win32.window_rect(hwnd)
    ctx.screenshot(hwnd, "drag-before.png")
    start = ((before[0] + before[2]) // 2, (before[1] + before[3]) // 2)
    end = (start[0] - 300, start[1] - 200)
    win32.drag(start, end, steps=25)
    ctx.sleep(1.5)
    after = win32.window_rect(ctx.murmur.overlay_hwnd())
    ctx.screenshot(ctx.murmur.overlay_hwnd(), "drag-after.png")
    ctx.artifact("drag-diagnostic.txt").write_text(
        f"start: {start}; end: {end}\nbefore: {before}; after: {after}\n"
        f"cursor: {win32.cursor()}\nforeground: {hex(win32.foreground())} "
        f"{win32.window_text(win32.foreground())!r}\n"
        f"overlay names: {ctx.murmur.overlay_names()!r}\n"
        f"new log: {[line for line in ctx.murmur.log()[log_before:].splitlines() if 'drag' in line.lower()]!r}\n",
        encoding="utf-8")
    if abs(after[0] - before[0]) < 100 and abs(after[1] - before[1]) < 100:
        raise Fail(f"Overlay did not move: {before} -> {after}")
    ctx.murmur.restart()
    restored = win32.window_rect(ctx.murmur.overlay_hwnd())
    ctx.note(f"before {before}, dragged {after}, after restart {restored}")
    if max(abs(a - b) for a, b in zip(after[:2], restored[:2])) > 2:
        raise Fail(f"Position not restored within 2 px: dragged to {after[:2]}, restarted at {restored[:2]}")


def _notepad_with_word(ctx):
    pad = ctx.notepad()
    pad.focus()
    win32.type_unicode("start")
    ctx.sleep(0.5)
    return pad


def _type_lands(ctx, pad, letter, expected):
    before = pad.focus_diagnostic()
    win32.type_unicode(letter)
    ctx.sleep(0.6)
    text = pad.text()
    if text != expected:
        ctx.artifact("focus-diagnostic.txt").write_text(
            f"before typing:\n{before}\nafter typing:\n{pad.focus_diagnostic()}\n",
            encoding="utf-8")
        raise Fail(f"After the overlay click, typed {letter!r} did not land in Notepad (text {text!r}, "
                   f"expected {expected!r})")


def o3_buttons_keep_focus(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    ctx.clipboard_guard()  # Copy replaces the owner's clipboard
    pad = _notepad_with_word(ctx)
    origin = ctx.target("origin")
    expected = "start"
    for button in ("Discard", "Copy"):
        produce_held(ctx, "A", origin, pad)
        ctx.require(win32.foreground() == pad.hwnd, "Notepad lost focus before the overlay click")
        click_overlay(ctx, button, expect_foreground=pad.hwnd)
        expected += "x" if button == "Discard" else "y"
        _type_lands(ctx, pad, expected[-1], expected)
        wait_not_held(ctx)
    ctx.note("held-transcript buttons (Discard, Copy) stand in for Enhance's Accept: both use pressWithoutFocus")
    ctx.screenshot(pad.hwnd, "notepad.png")


def o4_empty_area(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"overlay": {"idle_view": "pill"}})
    ctx.restorer.push("reset the overlay placement", lambda: _reset_placement(ctx))
    pad = _notepad_with_word(ctx)
    left, top, right, bottom = win32.window_rect(ctx.murmur.overlay_hwnd())
    win32.click((left + right) // 2, (top + bottom) // 2)
    ctx.sleep(0.6)
    ctx.require(win32.foreground() == pad.hwnd, "Clicking the overlay's empty area took focus from Notepad")
    _type_lands(ctx, pad, "a", "starta")
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"overlay": {"idle_view": "icon"}})
    pad.focus()
    before = win32.window_rect(ctx.murmur.overlay_hwnd())
    start = ((before[0] + before[2]) // 2, (before[1] + before[3]) // 2)
    win32.drag(start, (start[0] + 150, start[1] - 120), steps=20)
    ctx.sleep(1.0)
    after = win32.window_rect(ctx.murmur.overlay_hwnd())
    ctx.require(win32.foreground() == pad.hwnd, "Dragging the overlay took focus from Notepad")
    ctx.require(abs(after[0] - before[0]) >= 60, f"Drag did not move the overlay: {before} -> {after}")
    _type_lands(ctx, pad, "b", "startab")


def o6_scale(ctx):
    sizes = {}
    for scale in (0.8, 1.6):
        ctx.murmur.ensure(ctx.fixtures["A"].path, {"overlay": {"idle_view": "pill", "scale": scale}})
        ctx.sleep(1.0)
        hwnd = ctx.murmur.overlay_hwnd()
        rect = win32.window_rect(hwnd)
        sizes[scale] = (rect[2] - rect[0], rect[3] - rect[1])
        ctx.screenshot(hwnd, f"scale-{scale}.png")
        label = uia.find(ctx.murmur.overlay(), name_="mur", timeout=5, sleep=ctx.sleep)
        box = uia.rect(label)
        if box[0] < rect[0] or box[1] < rect[1] or box[2] > rect[2] or box[3] > rect[3]:
            raise Fail(f"At scale {scale} the label {box} is clipped by the overlay {rect}")
    ratio = sizes[1.6][1] / max(sizes[0.8][1], 1)
    ctx.note(f"overlay size at 0.8: {sizes[0.8]}, at 1.6: {sizes[1.6]} (height ratio {ratio:.2f})")
    if not 1.6 <= ratio <= 2.4:
        raise Fail(f"Overlay height did not scale with the setting (ratio {ratio:.2f}, expected about 2)")


def o7_subclass_log(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    log = ctx.murmur.log()
    lines = [line for line in log.splitlines() if "Overlay no-activate subclass" in line]
    path = ctx.artifact("subclass-log.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not lines:
        raise Fail("murmur.log has no 'Overlay no-activate subclass' line")
    if not any("top_level=true" in line for line in lines):
        raise Fail(f"Subclass line without top_level=true: {lines[-1]}")
    children = re.findall(r"children=(\d+)", lines[-1])
    ctx.note(f"children={children[0] if children else '?'}")
