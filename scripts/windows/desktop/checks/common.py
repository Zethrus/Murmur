"""Steps shared by several checks."""
import re
import time

import uia
import win32
from context import Fail, Skip

CODE_HOTKEY = "Ctrl+Alt+Shift+F8"
SHIFT_HOTKEY = "Ctrl+Shift+F9"          # H3 (B8): a hotkey that includes Shift
BLOCKED_HOTKEY = "Ctrl+Alt+Shift+F10"   # H6: registered by the harness, so Murmur cannot have it
D_HOTKEY = "Ctrl+Alt+D"                 # F13: Murmur's default, contains "d"
HOLD_FOR_SECONDS = 120                  # src-tauri/src/held.rs HOLD_FOR
NOTEPAD_OUTPUT_TIMEOUT = 45             # Notepad's existing completion/readback allowance
SIZE = re.compile(r"\b\d+(?:\.\d+)? ?(?:KB|MB|GB)\b")
# Component badges (src/settings/widgets/component.ts, STATE).
BADGE = r"^(Not installed|Queued|Installing|Installed|Active|Paused|Failed|Unverified|Damaged)$"


def badge(ctx, window, display_name, timeout=15):
    """State badge of a component row (the first badge after its title), None when not found."""
    try:
        return uia.name(uia.find_following(window, display_name, name_re=BADGE, timeout=timeout, sleep=ctx.sleep))
    except uia.NotFound:
        return None


def overlay_button(ctx, label, timeout=10):
    overlay = ctx.murmur.overlay()
    return uia.find(overlay, name_=label, control_type_=uia.BUTTON, timeout=timeout, sleep=ctx.sleep)


def click_overlay(ctx, label, expect_foreground=None):
    """Real mouse click on an overlay button (B7: it must not take focus)."""
    button = overlay_button(ctx, label)
    x, y = uia.center(button)
    win32.click(x, y)
    ctx.sleep(0.6)
    if expect_foreground and win32.foreground() != expect_foreground:
        raise Fail(f"Clicking overlay '{label}' moved focus to {win32.window_text(win32.foreground())!r}")
    return x, y


def overlay_drag_point(ctx):
    """Center of the rendered drag affordance, rather than a window-size guess."""
    element = uia.find(ctx.murmur.overlay(), name_="Drag to move", timeout=5, sleep=ctx.sleep)
    handle = uia.rect(element)
    if handle[2] <= handle[0] or handle[3] <= handle[1]:
        raise Fail(f"Drag handle has no usable bounds: {handle}")
    point = uia.center(element)
    left, top, right, bottom = win32.window_rect(ctx.murmur.overlay_hwnd())
    if not (left <= point[0] < right and top <= point[1] < bottom):
        raise Fail(f"Drag handle {handle} is outside overlay {(left, top, right, bottom)}")
    return point


def wait_held(ctx, timeout=30):
    if not ctx.murmur.wait_state("held", timeout):
        raise Fail(f"Transcript was not held (overlay: {ctx.murmur.overlay_state()})")


def wait_not_held(ctx, timeout=10):
    return win32.wait_for(lambda: ctx.murmur.overlay_state() != "held", timeout, 0.1, sleep=ctx.sleep)


def held_text(ctx):
    """The transcript shown in the held panel (the name after the 'Started in…' line)."""
    names = ctx.murmur.overlay_names()
    candidates = [n for n in names if n not in ("Not inserted: focus changed", "Insert here", "Copy", "Discard",
                                                 "Focus changed") and not n.startswith("Started ")]
    return candidates


def focus_change_dictation(ctx, fixture, start, finish, config=None, hotkey=None):
    """Start in `start`, click `finish` while the hotkey is held, release. Returns release time."""
    ctx.murmur.ensure(ctx.fixtures[fixture].path, config or {})
    start.focus()
    return ctx.dictate(fixture, hotkey=hotkey, during=lambda c: focus_while_held(finish))


def focus_while_held(target):
    """Focus a target while the hold-mode modifiers are down (no Shift+click where it matters)."""
    if getattr(target, "CLICK_WHILE_HELD", True):
        target.focus()
    else:
        target.focus(click=False)


def produce_held(ctx, fixture, a, b, config=None):
    """A held transcript with `b` focused (dictation started in `a`)."""
    focus_change_dictation(ctx, fixture, a, b, config)
    wait_held(ctx)


def require_empty(*targets_):
    for target in targets_:
        text = target.text()
        if text:
            raise Fail(f"{getattr(target, 'title', 'target')} received text it should not have: {text!r}")


def confirm_notepad_output(ctx, pad):
    """Wait for this utterance's history before querying Notepad during delivery."""
    try:
        ctx.observed_text(timeout=NOTEPAD_OUTPUT_TIMEOUT)
    except Fail:
        # The history gate never reads Notepad. Let the app finish before any
        # diagnostic UIA access, and record its resulting state with the log.
        state = ctx.settle()
        try:
            ctx.capture_output_failure(terminal_state=state)
        except Exception as error:  # noqa: BLE001 - preserve the output failure
            ctx.note(f"output diagnostics failed: {type(error).__name__}: {error}")
        # No output (including a held refusal) remains a failure.
        if state not in ("done", "idle", "held"):
            ctx.note(f"Notepad diagnostics skipped while overlay state was {state!r}")
            raise
        ctx.artifact("after-hotkey-focus.txt").write_text(pad.focus_diagnostic(), encoding="utf-8")
        ctx.screenshot(pad.hwnd, "notepad.png")
        ctx.artifact("notepad-focus.txt").write_text(pad.focus_diagnostic(), encoding="utf-8")
        raise


def find_settings_button(ctx, label, timeout=15):
    window = ctx.murmur.settings()
    return uia.find(window, name_=label, control_type_=uia.BUTTON, timeout=timeout, sleep=ctx.sleep)


def wait_names(ctx, element_getter, predicate, timeout):
    """Poll the descendant names of an element until predicate(names) holds."""
    deadline = time.monotonic() + timeout
    names = []
    while time.monotonic() < deadline:
        try:
            names = uia.names(element_getter())
        except Exception:  # noqa: BLE001 - window rebuilt while reading
            names = []
        if predicate(names):
            return names
        ctx.sleep(0.25)
    return None


def skip_unless(condition, reason):
    if not condition:
        raise Skip(reason)
