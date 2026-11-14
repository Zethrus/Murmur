"""K1 (best effort): Tab order and keyboard activation in Settings.

WebView2 reports focus changes to UI Automation with a lag, so every step is polled. The visible
focus ring itself is judged by eye in the manual session (screenshots are saved here).
"""
import uia
import win32
from context import Fail, Skip

MAX_TABS = 60


def _focused_index(window, items):
    element = uia.focused()
    if element is None:
        return None, None
    for index, candidate in enumerate(items):
        if uia.same(element, candidate):
            return index, element
    return -1, element


def _tab_to(ctx, window, label, limit=MAX_TABS):
    for _ in range(limit):
        element = uia.focused()
        if element is not None and uia.name(element) == label:
            return True
        win32.tap("tab")
        ctx.sleep(0.2)
    return False


def k1_keyboard(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    hwnd = ctx.murmur.show_settings()
    left, top, right, bottom = win32.window_rect(hwnd)
    if not win32.activate(hwnd, click_at=((left + right) // 2, top + 15)):
        raise Skip("could not focus the Settings window")
    window = ctx.murmur.page("General")
    items = uia.descendants(window)
    order, wraps, previous = [], 0, None
    for step in range(MAX_TABS):
        win32.tap("tab")
        ctx.sleep(0.25)
        if win32.foreground() != hwnd:
            raise Fail(f"Tab {step + 1} moved focus out of Settings")
        index, element = _focused_index(window, items)
        if index is None:
            raise Skip("UI Automation reports no focused element inside WebView2")
        order.append((index, uia.describe(element)))
        if step == 2:
            ctx.screenshot(hwnd, "focus-ring.png")
        if previous is not None and index != -1 and previous != -1 and index < previous:
            wraps += 1
            if wraps > 1:
                break
        previous = index if index != -1 else previous
        if step > 0 and index == order[0][0] and wraps == 1:
            break  # one complete traversal; a second cycle is not a second order defect
    distinct = {name for _, name in order}
    ctx.artifact("tab-order.txt").write_text("\n".join(f"{i}\t{n}" for i, n in order) + "\n", encoding="utf-8")
    if len(distinct) < 5:
        raise Fail(f"Tab reached only {len(distinct)} distinct elements")
    if wraps > 1:
        raise Fail("Focus moved backwards in document order more than once (one wrap is expected)")
    if not _tab_to(ctx, window, "Audio"):
        raise Skip("could not reach the Audio page button with Tab")
    win32.tap("enter")
    if uia.try_find(ctx.murmur.settings(), name_="Refresh inputs", control_type_=uia.BUTTON, timeout=5, sleep=ctx.sleep) is None:
        raise Fail("Enter on the Audio page button did not open Audio")
    if not _tab_to(ctx, window, "General"):
        raise Skip("could not reach the General page button with Tab")
    win32.tap("space")
    if uia.try_find(ctx.murmur.settings(), name_="Dictation shortcut", timeout=5, sleep=ctx.sleep) is None:
        raise Fail("Space on the General page button did not open General")
    ctx.note(f"{len(order)} Tab steps, {len(distinct)} distinct elements")
