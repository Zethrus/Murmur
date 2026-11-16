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


def click_overlay(ctx, label, expect_foreground=None, expect_focus=None):
    """Real mouse click on an overlay button (B7: it must not take focus)."""
    button = overlay_button(ctx, label)
    x, y = uia.center(button)
    win32.click(x, y)
    ctx.sleep(0.6)
    if expect_foreground and win32.foreground() != expect_foreground:
        raise Fail(f"Clicking overlay '{label}' moved focus to {win32.window_text(win32.foreground())!r}")
    if expect_focus is not None and not expect_focus():
        raise Fail(f"Clicking overlay '{label}' moved keyboard focus from the expected editor")
    return x, y


def overlay_drag_point(ctx, return_bounds=False):
    """Center of the rendered drag affordance, rather than a window-size guess."""
    element = uia.find(ctx.murmur.overlay(), name_="Drag to move", timeout=5, sleep=ctx.sleep)
    handle = uia.rect(element)
    if handle[2] <= handle[0] or handle[3] <= handle[1]:
        raise Fail(f"Drag handle has no usable bounds: {handle}")
    point = uia.center(element)
    left, top, right, bottom = win32.window_rect(ctx.murmur.overlay_hwnd())
    if not (left <= point[0] < right and top <= point[1] < bottom):
        raise Fail(f"Drag handle {handle} is outside overlay {(left, top, right, bottom)}")
    return (point, handle) if return_bounds else point


def overlay_drag_snapshot(ctx, native_bounds, details, timeout=0.5):
    """Find O8's visible icon pill, recording every same-named UIA candidate."""
    deadline = time.monotonic() + timeout
    hwnd = ctx.murmur.overlay_hwnd()
    while True:
        root = ctx.murmur.overlay()  # from_handle(hwnd), never the desktop UIA root
        root_hwnd = uia.handle(root)
        details["uia_root_hwnd"] = root_hwnd
        if not hwnd or root_hwnd != hwnd:
            raise Fail(f"O8 UIA root is not the owned overlay: {root_hwnd}, expected {hwnd}")
        elements = uia.find_all(root, name_="Drag to move")
        candidates = []
        valid = []
        for element in elements:
            kind = uia.control_type(element)
            native_hwnd = uia.handle(element)
            native_root = (win32.user32.GetAncestor(native_hwnd, win32.GA_ROOT)
                           if native_hwnd else 0)
            candidate = {"name": uia.name(element), "control_type": kind,
                         "control_type_name": uia.TYPE_NAMES.get(kind, str(kind)),
                         "native_hwnd": native_hwnd, "native_root_hwnd": native_root,
                         "uia_root_hwnd": root_hwnd,
                         "native_association": ("overlay UIA subtree" if not native_hwnd else
                                                "overlay window" if native_root == hwnd else "foreign window")}
            reasons = []
            if candidate["name"] != "Drag to move":
                reasons.append("name changed during enumeration")
            try:
                bounds = uia.rect(element)
                candidate["bounds"] = bounds
            except Exception as error:  # noqa: BLE001 - stale UIA element must not become a click
                bounds = None
                candidate["bounds_error"] = f"{type(error).__name__}: {error}"
                reasons.append("bounds unreadable")
            if kind != uia.GROUP:  # icon pill has role=status; hidden handle has role=img
                reasons.append("not status group")
            if native_hwnd and native_root != hwnd:
                reasons.append("foreign native root")
            if bounds is not None:
                left, top, right, bottom = native_bounds
                if not (left <= bounds[0] < bounds[2] <= right and
                        top <= bounds[1] < bounds[3] <= bottom):
                    reasons.append("outside native overlay or unusable")
            candidate["rejections"] = reasons
            candidates.append(candidate)
            if not reasons:
                valid.append(candidate)
        details["uia_candidates"] = candidates
        ctx.guard.check()  # enumeration and property reads can block in COM
        if len(valid) > 1:
            raise Fail(f"O8 drag affordance is ambiguous: {len(valid)} valid UIA candidates")
        if valid:
            selected = valid[0]
            details["uia_selected"] = selected
            bounds = selected["bounds"]
            return ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2), bounds
        if time.monotonic() >= deadline:
            raise uia.NotFound("No unique visible status-group drag affordance in owned overlay")
        ctx.sleep(0.25)


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


def focus_change_dictation(ctx, fixture, start, finish, config=None, hotkey=None, after_focus=None):
    """Start in `start`, click `finish` while the hotkey is held, release. Returns release time."""
    ctx.murmur.ensure(ctx.fixtures[fixture].path, config or {})
    start.focus()
    def focus_and_observe(_):
        try:
            focus_while_held(finish)
        finally:
            if after_focus is not None:
                after_focus()

    return ctx.dictate(fixture, hotkey=hotkey, during=focus_and_observe)


def focus_while_held(target):
    """Focus a target while the hold-mode modifiers are down (no Shift+click where it matters)."""
    if getattr(target, "CLICK_WHILE_HELD", True):
        target.focus()
    else:
        target.focus(click=False)


def produce_held(ctx, fixture, a, b, config=None, after_focus=None):
    """A held transcript with `b` focused (dictation started in `a`)."""
    focus_change_dictation(ctx, fixture, a, b, config, after_focus=after_focus)
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
