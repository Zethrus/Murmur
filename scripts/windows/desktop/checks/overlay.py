"""O1-O4, O6-O8: overlay states, drag and restore, B7 focus, diagnostics."""
import json
import re
import time

import fixtures
import uia
import win32
from checks.common import click_overlay, overlay_drag_snapshot, produce_held, wait_not_held
from context import Fail

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000
O8_GEOMETRY_ATTEMPTS = 4
O8_ACTIVATION_TRACE = "QA overlay activation trace"


def _o8_activation_trace_evidence(launch_log, gesture_delta):
    """Only complete, correlated records can support an absence inference."""
    lines = [line for line in gesture_delta.splitlines() if O8_ACTIVATION_TRACE in line]
    armed = bool(re.search(r"QA overlay activation trace armed\b", launch_log))
    capture_log = launch_log + "\n" + gesture_delta
    capped = (bool(re.search(r"\bcap_reached=true(?:\s|$)", capture_log)) or
              "QA overlay activation trace cap reached" in capture_log)
    errors, seen, active, stacks = [], set(), {}, {}
    entries = activation_entries = 0
    for index, line in enumerate(lines):
        tail = line.split(O8_ACTIVATION_TRACE, 1)[1].strip()
        if tail.startswith("armed ") or tail.startswith("cap reached"):
            continue
        try:
            fields = {}
            for token in tail.split():
                key, value = token.split("=", 1)
                if not key or not value or key in fields:
                    raise ValueError("missing or duplicate field")
                fields[key] = value
            ident, parent, thread = (int(fields[key]) for key in ("id", "parent", "thread"))
            phase, message = fields["phase"], fields["native_message"]
            for key in ("hwnd", "foreground", "focus"):
                if not re.fullmatch(r"0x[0-9a-fA-F]+", fields[key]):
                    raise ValueError(f"invalid {key}")
            if not 1 <= ident <= 128 or parent < 0 or thread <= 0:
                raise ValueError("invalid identity")
            if message not in ("WM_ACTIVATE", "WM_SETFOCUS", "WM_KILLFOCUS"):
                raise ValueError("unknown native message")
            stack = stacks.setdefault(thread, [])
            if phase == "entry":
                if not re.fullmatch(r"0x[0-9a-fA-F]+", fields["peer"]):
                    raise ValueError("invalid peer")
                if fields.get("cap_reached") not in ("true", "false"):
                    raise ValueError("missing entry cap state")
                if ident in seen or parent != (stack[-1] if stack else 0):
                    raise ValueError("duplicate identity or broken parent")
                if message == "WM_ACTIVATE":
                    if fields.get("activation_state") not in ("0", "1", "2"):
                        raise ValueError("missing activation state")
                    activation_entries += 1
                elif "activation_state" in fields:
                    raise ValueError("focus message has activation state")
                seen.add(ident)
                active[ident] = (parent, thread, message, False)
                stack.append(ident)
                entries += 1
            elif phase in ("handback", "exit"):
                current = active.get(ident)
                if not current or current[:3] != (parent, thread, message) or not stack or stack[-1] != ident:
                    raise ValueError("unmatched or out-of-order record")
                if phase == "handback":
                    if message != "WM_ACTIVATE" or current[3] or fields.get("succeeded") not in ("true", "false"):
                        raise ValueError("invalid handback")
                    active[ident] = (parent, thread, message, True)
                else:
                    int(fields["result"])
                    if fields.get("handback_attempted") != str(current[3]).lower():
                        raise ValueError("missing or inconsistent handback result")
                    stack.pop()
                    del active[ident]
            else:
                raise ValueError("unknown phase")
        except (KeyError, ValueError) as error:
            errors.append(f"record {index + 1}: {error}")
    if not entries:
        errors.append("no complete trace evidence")
    if active:
        errors.append("unclosed entry records")
    valid = not errors
    return {"armed": armed, "cap_reached": capped, "activation_entries": activation_entries,
            "valid_capture": valid, "validation_errors": errors,
            "no_activation_entry_interpretable": armed and not capped and valid and activation_entries == 0,
            "log_delta": lines}


def _icon_drag_point(ctx, hwnd, details):
    """Icon-view status affordance through O8's ownership/type/containment/uniqueness selector.

    The first same-named UIA element can be the native ToolTip (run 43984316462), so O2 and
    O4 no longer click whichever "Drag to move" element UIA returns first.
    """
    try:
        return overlay_drag_snapshot(ctx, win32.window_rect(hwnd), details, timeout=5)
    except uia.NotFound as error:
        raise Fail(f"No unique visible status drag affordance in the overlay: {error}") from None


def _exstyle(hwnd):
    return win32.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)


def _reset_placement(ctx):
    """Saved drags live in config.toml; force the next ensure() to rewrite the file."""
    ctx.murmur.config = {}


def _qa_drag_ready(log):
    """Require launch diagnostic coverage, not current-gesture viewport convergence."""
    lines = [line for line in log.splitlines() if "QA overlay drag probe" in line]
    armed = next((index for index, line in enumerate(lines) if re.search(r"\bstage=4(?:\s|$)", line)), None)
    if armed is None:
        return False
    settled = next((index for index, line in enumerate(lines[armed + 1:], armed + 1)
                    if re.search(r"\bstage=5(?:\s|$)", line) and "idle_icon=true" in line
                    and "pill_hidden=false" in line), None)
    return settled is not None and any(
        re.search(r"\bstage=6(?:\s|$)", line) and "idle_icon=true" in line
        for line in lines[settled + 1:]
    )


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
    if not win32.wait_for(lambda: _qa_drag_ready(ctx.murmur.log(since_launch=True)),
                          5, 0.1, sleep=ctx.sleep):
        ctx.artifact("drag-diagnostic.txt").write_text(
            "QA overlay capture listener, settled icon or post-size viewport probe did not arm before the gesture.\n"
            + ctx.murmur.log(since_launch=True), encoding="utf-8")
        raise Fail("QA overlay capture listener, settled icon or post-size viewport probe did not arm")
    hwnd = ctx.murmur.overlay_hwnd()
    log_before = len(ctx.murmur.log())
    before = win32.window_rect(hwnd)
    ctx.screenshot(hwnd, "drag-before.png")
    selection = {}
    start, handle_rect = _icon_drag_point(ctx, hwnd, selection)
    # Sample UIA and native geometry at this gesture, after the settled-icon handshake.
    # The mount's stage-5 DOM probe alone may predate a WebView2 viewport resize.
    native_geometry = {"visible": win32.window_rect(hwnd), "outer": win32.outer_rect(hwnd),
                       "client_origin": win32.client_origin(hwnd), "client_size": win32.client_size(hwnd),
                       "dpi_scale": win32.dpi_scale(hwnd)}
    end = (start[0] - 300, start[1] - 200)
    hit = win32.user32.WindowFromPoint(win32.POINT(*start))
    hit_root = win32.user32.GetAncestor(hit, win32.GA_ROOT)
    hit_geometry = {"rect": win32.window_rect(hit), "client_origin": win32.client_origin(hit),
                    "client_size": win32.client_size(hit),
                    "class": win32.class_name(hit), "pid": win32.window_pid(hit)} if hit else None
    client_click = tuple(start[index] - native_geometry["client_origin"][index] for index in (0, 1))
    hit_client_click = (tuple(start[index] - hit_geometry["client_origin"][index] for index in (0, 1))
                        if hit_geometry else None)
    samples = []

    def sample(stage, step):
        samples.append((round(time.monotonic(), 3), stage, step,
                        win32.cursor(), bool(win32.user32.GetAsyncKeyState(0x01) & 0x8000),
                        win32.window_rect(ctx.murmur.overlay_hwnd())))

    ctx.restorer.push("release the mouse button", win32.mouse_up)
    try:
        win32.drag(start, end, steps=25, release=False, sample=sample)
        held = win32.window_rect(ctx.murmur.overlay_hwnd())
    finally:
        win32.mouse_up()
    ctx.sleep(1.5)
    after = win32.window_rect(ctx.murmur.overlay_hwnd())
    ctx.screenshot(ctx.murmur.overlay_hwnd(), "drag-after.png")
    ctx.artifact("drag-diagnostic.txt").write_text(
        f"start: {start}; end: {end}\nbefore: {before}; held: {held}; after: {after}\n"
        f"gesture-time UIA drag bounds: {handle_rect}; native overlay: {native_geometry!r}; "
        f"UIA candidates: {selection.get('uia_candidates')!r}; "
        f"hit child: {hit_geometry!r}\n"
        f"click relative to overlay/hit client (physical px): {client_click}; {hit_client_click}\n"
        f"hit hwnd: {hex(hit)}; root: {hex(hit_root)}; overlay: {hex(hwnd)}; "
        f"hit owned by overlay: {hit_root == hwnd}\n"
        f"samples (monotonic, stage, step, cursor, physical left down, overlay rect):\n"
        + "".join(f"  {row!r}\n" for row in samples) +
        f"cursor: {win32.cursor()}\nforeground: {hex(win32.foreground())} "
        f"{win32.window_text(win32.foreground())!r}\n"
        f"overlay names: {ctx.murmur.overlay_names()!r}\n"
        f"new drag log: {[line for line in ctx.murmur.log()[log_before:].splitlines() if 'drag' in line.lower()]!r}\n"
        "DOM snapshots below are historical launch evidence, not synchronized with this gesture.\n"
        f"QA DOM hit probes: {[line for line in ctx.murmur.log(since_launch=True).splitlines() if 'QA overlay drag probe' in line]!r}\n"
        f"QA native mouse boundary: {[line for line in ctx.murmur.log()[log_before:].splitlines() if 'QA overlay native mouse' in line]!r}\n",
        encoding="utf-8")
    if abs(held[0] - before[0]) < 100 and abs(held[1] - before[1]) < 100:
        raise Fail(f"Overlay did not move while the button was held: {before} -> {held}")
    ctx.murmur.restart()
    restored = win32.window_rect(ctx.murmur.overlay_hwnd())
    ctx.note(f"before {before}, dragged {after}, after restart {restored}")
    if max(abs(a - b) for a, b in zip(after[:2], restored[:2])) > 2:
        raise Fail(f"Position not restored within 2 px: dragged to {after[:2]}, restarted at {restored[:2]}")


def _o8_window(hwnd):
    """Read-only native state at one point in time; no activation or focus repair."""
    return {"hwnd": hwnd, "pid": win32.window_pid(hwnd), "class": win32.class_name(hwnd),
            "visible": bool(win32.user32.IsWindowVisible(hwnd)),
            "enabled": bool(win32.user32.IsWindowEnabled(hwnd)),
            "style": win32.user32.GetWindowLongPtrW(hwnd, -16),
            "exstyle": win32.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE),
            "rect": win32.window_rect(hwnd), "outer": win32.outer_rect(hwnd),
            "client_origin": win32.client_origin(hwnd), "client_size": win32.client_size(hwnd),
            "dpi_scale": win32.dpi_scale(hwnd)}


def _o8_settings(pid):
    windows = win32.find_windows(title="Murmur Settings", pid=pid, visible_only=False)
    return [{"hwnd": hwnd, "pid": win32.window_pid(hwnd),
             "visible": bool(win32.user32.IsWindowVisible(hwnd))} for hwnd in windows]


def _o8_owned_foreground(hwnd, pid):
    context = win32.foreground_context()
    root = win32.user32.GetAncestor(context["hwnd"], win32.GA_ROOT) if context["hwnd"] else 0
    return (context, root, context["input_desktop"] == "Default" and context["gui_status"] == "ok"
            and root == hwnd and context["process_id"] == pid)


def _o8_verify_gesture_context(ctx, label, pid, hwnd, expected_foreground, original=None):
    """Recheck the guard and exact owners after every potentially blocking UIA read."""
    ctx.guard.check()
    if (not ctx.murmur.running() or ctx.murmur.process.pid != pid or
            ctx.murmur.overlay_hwnd() != hwnd or win32.window_pid(hwnd) != pid):
        raise Fail(f"Murmur overlay ownership changed before {label}")
    context = win32.foreground_context()
    root = win32.user32.GetAncestor(context["hwnd"], win32.GA_ROOT) if context["hwnd"] else 0
    allowed_status = ("ok", "no_foreground") if expected_foreground is None else ("ok",)
    if context["input_desktop"] != "Default" or context["gui_status"] not in allowed_status:
        raise Fail(f"Unsafe desktop or foreground context before {label}: {context}")
    owner = (root, context["process_id"])
    if expected_foreground is not None and owner != expected_foreground:
        raise Fail(f"Foreground ownership changed before {label}: {context}, root={root}")
    if original is not None and owner != original:
        raise Fail(f"Foreground ownership changed during readiness for {label}: {context}, root={root}")
    return context, root


def _o8_ready_drag_point(ctx, label, pid, hwnd, expected_foreground, original, history):
    """Accept two consecutive matching UIA/native snapshots, with bounded retries."""
    previous = None
    for attempt in range(O8_GEOMETRY_ATTEMPTS):
        _o8_verify_gesture_context(ctx, label, pid, hwnd, expected_foreground, original)
        before = win32.window_rect(hwnd)
        row = {"attempt": attempt + 1, "native_before": before}
        try:
            point = bounds = None
            try:
                point, bounds = overlay_drag_snapshot(ctx, before, row)
                row.update(uia_drag_bounds=bounds, point=point)
            except uia.NotFound as error:
                row["uia_error"] = str(error)
            # UIA discovery may block while the user, process or foreground changes.
            _o8_verify_gesture_context(ctx, label, pid, hwnd, expected_foreground, original)
            after = win32.window_rect(hwnd)
            row["native_after"] = after
            if before != after:
                raise Fail(f"Native overlay geometry changed during readiness for {label}: {before} -> {after}")
            if bounds is not None:
                left, top, right, bottom = after
                valid = (bounds[2] > bounds[0] and bounds[3] > bounds[1] and
                         left <= bounds[0] < bounds[2] <= right and
                         top <= bounds[1] < bounds[3] <= bottom and
                         left <= point[0] < right and top <= point[1] < bottom)
                if valid:
                    current = (point, bounds, after)
                    if current == previous:
                        row["result"] = "verified"
                        return current
                    previous = current
                    row["result"] = "awaiting matching snapshot"
                else:
                    previous = None
                    row["result"] = "UIA handle outside native overlay or unusable"
            else:
                previous = None
                row["result"] = ("UIA candidates rejected" if row.get("uia_candidates")
                                 else "UIA handle not found")
        except BaseException as error:
            row["error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            history.append(row)
        if attempt + 1 < O8_GEOMETRY_ATTEMPTS:
            ctx.sleep(0.1)
    raise Fail(f"O8 drag handle did not converge with native overlay for {label} "
               f"after {O8_GEOMETRY_ATTEMPTS} guarded snapshots")


def _o8_endpoint(start, before, work, preferred):
    """Choose a full trajectory from the actual position; never shorten or clip it."""
    left, top, right, bottom = work
    def fits(rect):
        return left <= rect[0] < rect[2] <= right and top <= rect[1] < rect[3] <= bottom
    if not fits(before) or not (left <= start[0] < right and top <= start[1] < bottom):
        raise Fail("O8 overlay or pointer is outside the verified work area")
    for sx, sy in ((preferred, preferred), (-preferred, -preferred),
                   (preferred, -preferred), (-preferred, preferred)):
        dx, dy = sx * 300, sy * 200
        end = (start[0] + dx, start[1] + dy)
        moved = (before[0] + dx, before[1] + dy, before[2] + dx, before[3] + dy)
        if fits(moved) and left <= end[0] < right and top <= end[1] < bottom:
            return end
    raise Fail("O8 full 300x200 trajectory cannot fit the verified work area")


def _o8_focus_notepad(ctx, pad):
    def verify():
        ctx.guard.check()
        if not (pad.owned_tab and pad.hwnd and pad.owner_pid in win32.pids("Notepad.exe") and
                win32.window_pid(pad.hwnd) == pad.owner_pid and pad.active_document()):
            raise Fail("Notepad ownership was not verified before activation")
    verify()
    pad.focus(guard_check=verify)
    verify()


def _o8_show_settings(ctx, pid):
    # Singleton handoff itself can activate Settings; verify its existing hidden
    # window before requesting it, not only after activation has already occurred.
    windows = _o8_settings(pid)
    if len(windows) != 1:
        raise Fail("Could not uniquely verify existing Murmur Settings before handoff")
    hwnd = windows[0]["hwnd"]
    def verify():
        ctx.guard.check()
        if (not ctx.murmur.running() or ctx.murmur.process.pid != pid or
                not win32.is_window(hwnd) or win32.window_pid(hwnd) != pid or
                win32.window_text(hwnd) != "Murmur Settings"):
            raise Fail("Murmur Settings ownership changed before activation")
    verify()
    shown = ctx.murmur.show_settings(guard_check=verify)
    verify()
    if shown != hwnd:
        raise Fail("Settings handoff returned a different window")
    if not win32.activate(hwnd, guard_check=verify):
        raise Fail("Could not activate verified Murmur Settings")
    verify()
    return hwnd


def _o8_drag(ctx, label, pid, expected_foreground, direction, observations):
    """One O2 held gesture, with the same 300x200 travel and movement oracle."""
    geometry_history = []
    observation = {"case": label, "readiness_geometry_history": geometry_history}
    observations.append(observation)
    hwnd = ctx.murmur.overlay_hwnd()
    try:
        context, foreground_root = _o8_verify_gesture_context(ctx, label, pid, hwnd, expected_foreground)
        original_foreground = (foreground_root, context["process_id"])
        start, bounds, ready_rect = _o8_ready_drag_point(
            ctx, label, pid, hwnd, expected_foreground, original_foreground, geometry_history)
        context, foreground_root = _o8_verify_gesture_context(
            ctx, label, pid, hwnd, expected_foreground, original_foreground)
        allowed_status = ("ok", "no_foreground") if expected_foreground is None else ("ok",)
        hit = win32.user32.WindowFromPoint(win32.POINT(*start))
        hit_root = win32.user32.GetAncestor(hit, win32.GA_ROOT) if hit else 0
        # WebView2 can host its hit child in a different renderer process. The
        # top-level root, rather than the child's PID, establishes overlay ownership.
        if not hit or hit_root != hwnd:
            raise Fail(f"Drag point is not owned by the overlay: hit={hit}, root={hit_root}, overlay={hwnd}")
        before = win32.window_rect(hwnd)
        if before != ready_rect:
            raise Fail(f"Native overlay geometry changed after readiness for {label}: {ready_rect} -> {before}")
        work_area = win32.work_area_at(start)
        end = _o8_endpoint(start, before, work_area, direction)
        log_before = len(ctx.murmur.log())
        observation.update(process_pid=pid, start=start, end=end, verified_work_area=work_area,
                           preferred_direction=direction, uia_drag_bounds=bounds, before=before,
                           foreground_before=context, foreground_root=foreground_root,
                           settings_before=_o8_settings(pid), overlay_before=_o8_window(hwnd),
                           hit_before=_o8_window(hit),
                           click_in_overlay_client=tuple(start[i] - win32.client_origin(hwnd)[i]
                                                         for i in (0, 1)),
                           click_in_hit_client=tuple(start[i] - win32.client_origin(hit)[i]
                                                     for i in (0, 1)),
                           held_samples=[])
    except BaseException as error:
        observation["error"] = f"{type(error).__name__}: {error}"
        raise

    def sample(stage, step):
        ctx.guard.check()
        if not ctx.murmur.running() or ctx.murmur.process.pid != pid or \
                ctx.murmur.overlay_hwnd() != hwnd or win32.window_pid(hwnd) != pid or \
                win32.input_desktop() != "Default":
            raise Fail(f"Overlay ownership or desktop changed during {label}")
        focus = win32.foreground_context()
        focus_root = (win32.user32.GetAncestor(focus["hwnd"], win32.GA_ROOT)
                      if focus["hwnd"] else 0)
        observation["held_samples"].append({"monotonic_s": round(time.monotonic(), 3),
                                             "stage": stage, "step": step, "cursor": win32.cursor(),
                                             "left_down": bool(win32.user32.GetAsyncKeyState(0x01) & 0x8000),
                                             "overlay_rect": win32.window_rect(hwnd),
                                             "foreground": focus, "foreground_root": focus_root})
        if (focus["input_desktop"] != "Default" or focus["gui_status"] not in allowed_status or
                (focus_root, focus["process_id"]) != original_foreground):
            raise Fail(f"Foreground or desktop changed during {label}: {focus}, root={focus_root}")

    drag_attempted = False
    try:
        try:
            _o8_verify_gesture_context(ctx, label, pid, hwnd, expected_foreground, original_foreground)
            if win32.window_rect(hwnd) != before:
                raise Fail(f"Native overlay geometry changed before input for {label}")
            current_hit = win32.user32.WindowFromPoint(win32.POINT(*start))
            current_root = win32.user32.GetAncestor(current_hit, win32.GA_ROOT) if current_hit else 0
            if not current_hit or current_root != hwnd:
                raise Fail(f"Drag point ownership changed before input for {label}")
            drag_attempted = True
            win32.drag(start, observation["end"], steps=25, release=False, sample=sample)
            observation["held"] = win32.window_rect(hwnd)
        finally:
            if drag_attempted:
                win32.mouse_up()
        ctx.sleep(1.5)
        observation["after"] = win32.window_rect(hwnd)
        observation["foreground_after"] = win32.foreground_context()
        after_hwnd = observation["foreground_after"]["hwnd"]
        observation["foreground_after_root"] = (win32.user32.GetAncestor(after_hwnd, win32.GA_ROOT)
                                                if after_hwnd else 0)
        observation["settings_after"] = _o8_settings(pid)
        observation["overlay_after"] = _o8_window(hwnd)
        observation["hit_after"] = _o8_window(hit) if win32.is_window(hit) else None
        observation["moved_while_held"] = not (abs(observation["held"][0] - before[0]) < 100 and
                                                abs(observation["held"][1] - before[1]) < 100)
        if expected_foreground is not None:
            _, root, owned = _o8_owned_foreground(*expected_foreground)
            if not owned:
                raise Fail(f"Foreground ownership changed during {label}: root={root}")
        return observation
    except BaseException as error:
        observation["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        try:
            delta = ctx.murmur.log()[log_before:]
            observation["log_delta"] = delta.splitlines()
            observation["drag_log_delta"] = [line for line in delta.splitlines() if "drag" in line.lower()]
            observation["activation_trace"] = _o8_activation_trace_evidence(
                ctx.murmur.log(since_launch=True), delta)
        except Exception as error:  # noqa: BLE001 - preserve the gesture's original outcome
            observation["log_read_error"] = f"{type(error).__name__}: {error}"


def o8_foreground_drag(ctx):
    """Opt-in, four-observation foreground comparison; a pass cannot clear O2."""
    artifact = ctx.artifact("foreground-drag-diagnostic.json")
    record = {"status": "running", "interpretation": "Diagnostic only; O2 remains the release oracle",
              "cases": [], "failures": [], "restart": None}
    try:
        ctx.murmur.ensure(ctx.fixtures["A"].path, {"overlay": {"idle_view": "icon"}})
        ctx.restorer.push("reset the overlay placement", lambda: _reset_placement(ctx))
        pid = ctx.murmur.process.pid
        record["initial_pid"] = pid
        record["settings_hide_on_launch"] = ctx.murmur.settings_hide_observation
        armed = win32.wait_for(lambda: _qa_drag_ready(ctx.murmur.log(since_launch=True)),
                               5, 0.1, sleep=ctx.sleep)
        record["launch_dom_probes"] = [line for line in ctx.murmur.log(since_launch=True).splitlines()
                                       if "QA overlay drag probe" in line]
        if not armed:
            raise Fail("QA overlay capture listener, settled icon or post-size viewport probe did not arm")
        if not record["settings_hide_on_launch"] or not record["settings_hide_on_launch"]["wait_succeeded"] \
                or record["settings_hide_on_launch"]["visible_after_wait"]:
            record["failures"].append("Settings hide after cold launch did not complete")
        # The first gesture precedes any Notepad creation, activation or handoff.
        cold = _o8_drag(ctx, "cold foreground untouched", pid, None, -1, record["cases"])
        if not cold["moved_while_held"]:
            record["failures"].append("cold foreground untouched: no held movement")
        if (cold["foreground_root"], cold["foreground_before"]["process_id"]) != \
                (cold["foreground_after_root"], cold["foreground_after"]["process_id"]):
            record["failures"].append("cold gesture changed foreground ownership")
            raise Fail("Cold gesture changed foreground ownership")
        last_success = cold["after"] if cold["moved_while_held"] else None
        position_clean = True

        ctx.guard.check()
        pad = ctx.notepad("foreground")
        _o8_focus_notepad(ctx, pad)
        if ctx.startup_probe_lease is not None and not ctx.startup_probe_lease.closed:
            raise Fail("Passive startup probe did not hand off to the first verified Notepad target")
        for label, expected, direction in (
                ("owned Notepad foreground", (pad.hwnd, pad.owner_pid), 1),
                ("owned Murmur Settings foreground", (None, pid), -1),
                ("same owned Notepad foreground", (pad.hwnd, pad.owner_pid), 1)):
            ctx.guard.check()
            if label == "owned Murmur Settings foreground":
                settings = _o8_show_settings(ctx, pid)
                expected = (settings, pid)
            elif label == "same owned Notepad foreground":
                _o8_focus_notepad(ctx, pad)
            if label != "owned Murmur Settings foreground" and not pad.owns_keyboard_focus():
                raise Fail(f"Owned Notepad editor lost keyboard focus before {label}")
            observed = _o8_drag(ctx, label, pid, expected, direction, record["cases"])
            if label != "owned Murmur Settings foreground" and not pad.owns_keyboard_focus():
                raise Fail(f"Owned Notepad editor lost keyboard focus during {label}")
            if not observed["moved_while_held"]:
                record["failures"].append(f"{label}: no held movement")
                if last_success is not None and observed["after"] != last_success:
                    position_clean = False
            else:
                last_success, position_clean = observed["after"], True
        if last_success is not None and position_clean:
            ctx.guard.check()
            ctx.murmur.restart()
            restored = win32.window_rect(ctx.murmur.overlay_hwnd())
            difference = max(abs(a - b) for a, b in zip(last_success[:2], restored[:2]))
            record["restart"] = {"last_successful_position": last_success, "restored": restored,
                                 "max_origin_difference_px": difference, "within_2px": difference <= 2}
            if difference > 2:
                record["failures"].append(f"Last successful position was not restored within 2 px: {difference}")
        else:
            record["restart"] = {"verified": False, "reason": "no meaningful last successful position"}
        record["status"] = "fail" if record["failures"] else "inconclusive"
        ctx.note("Four foreground observations are diagnostic; even four passes do not clear O2")
        if record["failures"]:
            raise Fail("; ".join(record["failures"]))
    except BaseException as error:
        if record["status"] == "running":
            from guard import UserInput
            record["status"] = "aborted" if isinstance(error, UserInput) else "fail"
            record["stop_reason"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        artifact.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def _notepad_with_word(ctx):
    pad = ctx.notepad()
    pad.focus()
    win32.type_unicode("start")
    ctx.sleep(0.5)
    return pad


def _type_lands(ctx, pad, letter, expected, before_click=None):
    before = pad.focus_diagnostic()
    win32.type_unicode(letter)
    ctx.sleep(0.6)
    text = pad.text()
    if text != expected:
        ctx.artifact("focus-diagnostic.txt").write_text(
            f"before overlay click:\n{before_click or 'not sampled'}\n"
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
        diagnostic = ctx.artifact(f"{button.lower()}-focus-diagnostic.txt")
        observations = []

        def observe(stage):
            try:
                detail = pad.focus_diagnostic()
            except Exception as error:  # noqa: BLE001 - preserve the check's original failure
                detail = f"focus diagnostic unavailable: {type(error).__name__}: {error}"
            observations.append(f"{stage}: foreground={win32.foreground():#x}; expected={pad.hwnd:#x}\n{detail}")
            diagnostic.write_text("\n".join(observations), encoding="utf-8")

        produce_held(ctx, "A", origin, pad, after_focus=lambda: observe("after focus_while_held"))
        observe("held before overlay click")
        before_click = observations[-1]
        ctx.require(pad.owns_keyboard_focus(), "Owned Notepad editor lost keyboard focus before the overlay click")
        click_overlay(ctx, button, expect_focus=pad.owns_keyboard_focus)
        expected += "x" if button == "Discard" else "y"
        _type_lands(ctx, pad, expected[-1], expected, before_click)
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
    start, _bounds = _icon_drag_point(ctx, ctx.murmur.overlay_hwnd(), {})
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
