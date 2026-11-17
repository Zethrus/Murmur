"""O9: WebView2 host-input reproducer driver (docs/parity/webview2-input-repro.md).

Runs tools/webview2-input-repro twice in one session, a fixed baseline (`off`) and one
candidate (`on`), with the same gesture sequence. The reproducer is a diagnostic: its result
can justify or reject a Murmur change, and can never clear O2.

Safety: every input is preceded by a guard check and an ownership check of the exact
reproducer process and windows (or the owned Notepad). Typing is sent only through O8's
verified `_o8_type_after_release`. The reproducer is stopped by its own pid only.
"""
import json
from pathlib import Path
import subprocess

import topology
import uia
import win32
from context import Fail

EXE = "webview2-input-repro.exe"
WM_REPRO_HIDE = 0x8000 + 1  # WM_APP + 1, handled only by the reproducer's overlay root
WM_REPRO_RESET = 0x8000 + 2  # WM_APP + 2: back to the first shown position, no activation
MODES = ("off", "on")
RENDERER_CHAIN = ["Chrome_RenderWidgetHostHWND", "Chrome_WidgetWin_1", "Chrome_WidgetWin_0", "WRY_WEBVIEW"]


class Reproducer:
    """One reproducer process: launch, ownership, event log, stop."""

    def __init__(self, ctx, mode):
        self.ctx, self.mode = ctx, mode
        self.exe = Path(ctx.build) / EXE
        self.log_path = ctx.work / f"repro-{mode}.jsonl"
        self.data = ctx.work / f"repro-{mode}-data"
        self.process = None
        self.overlay = self.settings = 0

    def events(self):
        if not self.log_path.exists():
            return []
        rows = []
        for line in self.log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass  # a line still being written
        return rows

    def running(self):
        return self.process is not None and self.process.poll() is None

    def owned(self):
        pid = self.process.pid if self.process else 0
        return (self.running() and all(win32.is_window(h) and win32.window_pid(h) == pid
                                       for h in (self.overlay, self.settings)))

    def start(self, timeout=45):
        self.ctx.guard.check()
        if not self.exe or not self.exe.is_file():
            raise Fail(f"{EXE} is not in the verified build")
        if win32.pids(EXE):
            raise Fail("Another WebView2 reproducer is already running")
        self.data.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen([str(self.exe), "--host-input", self.mode, "--log", str(self.log_path),
                                         "--data", str(self.data)], cwd=str(self.ctx.work))
        self.ctx.restorer.push(f"stop the WebView2 reproducer ({self.mode})", self.stop)

        def ready():
            events = self.events()
            names = [row.get("event") for row in events]
            if "fatal" in names:
                raise Fail(f"Reproducer failed to start: {events[names.index('fatal')]}")
            pages = {(row.get("window"), row.get("message")) for row in events if row.get("event") == "page"}
            return "overlay_shown" in names and ("settings", "ready") in pages and ("overlay", "ready") in pages

        if not win32.wait_for(ready, timeout, 0.2, sleep=self.ctx.sleep):
            raise Fail(f"Reproducer ({self.mode}) did not become ready: {self.events()[-5:]}")
        hosted = {row["window"]: int(row["root"], 16) for row in self.events() if row.get("event") == "hosted"}
        self.overlay, self.settings = hosted.get("overlay", 0), hosted.get("settings", 0)
        if not self.owned():
            raise Fail("Reproducer windows are not owned by the launched process")
        if win32.window_text(self.settings) != "WebView2 Repro Settings":
            raise Fail("Reproducer Settings window identity did not match")
        self.ctx.sleep(1.0)  # let the overlay settle after its first show
        return self

    def stop(self):
        if not self.process:
            return
        if self.process.poll() is None:
            if win32.is_window(self.settings) and win32.window_pid(self.settings) == self.process.pid:
                win32.close_window(self.settings)
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                win32.kill(self.process.pid)
                self.process.wait(10)
        self.process = None


def _foreground_owner():
    context = win32.foreground_context()
    root = win32.user32.GetAncestor(context["hwnd"], win32.GA_ROOT) if context["hwnd"] else 0
    return context, root


def _require_context(ctx, repro, expected, label):
    ctx.guard.check()
    if not repro.owned():
        raise Fail(f"Reproducer ownership changed before {label}")
    context, root = _foreground_owner()
    if (context["input_desktop"] != "Default" or context["gui_status"] != "ok" or
            (root, context["process_id"]) != expected):
        raise Fail(f"Foreground is not the expected owner before {label}: root={root}, {context}")
    return context


def _drag_start(ctx, repro, label):
    rect = win32.window_rect(repro.overlay)
    start = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
    hit = win32.user32.WindowFromPoint(win32.POINT(*start))
    if not hit or win32.user32.GetAncestor(hit, win32.GA_ROOT) != repro.overlay:
        raise Fail(f"Reproducer overlay is not the window under its centre before {label}")
    return rect, start, hit


def _endpoint(start, rect, work):
    """Full 300x200 travel that keeps the overlay inside the work area (as O8)."""
    left, top, right, bottom = work
    for sx, sy in ((1, 1), (-1, -1), (1, -1), (-1, 1)):
        dx, dy = 300 * sx, 200 * sy
        if (left <= rect[0] + dx and rect[2] + dx <= right and top <= rect[1] + dy and rect[3] + dy <= bottom):
            return (start[0] + dx, start[1] + dy)
    raise Fail("Full 300x200 reproducer trajectory does not fit the work area")


def gesture(ctx, repro, label, kind, expected):
    """One bounded gesture; returns an observation. Input is released in every path."""
    _require_context(ctx, repro, expected, label)
    win32.user32.PostMessageW(repro.overlay, WM_REPRO_RESET, 0, 0)  # same start for every gesture
    ctx.sleep(0.3)
    context = _require_context(ctx, repro, expected, label)
    rect, start, hit = _drag_start(ctx, repro, label)
    work = win32.work_area_at(start)
    mark = len(repro.events())
    row = {"case": label, "kind": kind, "mode": repro.mode, "start": start, "before": rect,
           "foreground_before": context, "held_samples": [], "focus_changes": []}

    def sample(stage, step):
        ctx.guard.check()
        if not repro.owned():
            raise Fail(f"Reproducer ownership changed during {label}")
        now = win32.foreground_context()
        row["held_samples"].append({"stage": stage, "step": step, "rect": win32.window_rect(repro.overlay),
                                    "focus_hwnd": now.get("focus_hwnd"), "foreground": now["hwnd"]})
        if now.get("gui_status") == "ok" and now.get("focus_hwnd") != context.get("focus_hwnd"):
            row["focus_changes"].append({"stage": stage, "step": step, "focus_hwnd": now.get("focus_hwnd")})
        if kind == "hide" and stage == "moving" and step == 12:
            win32.user32.PostMessageW(repro.overlay, WM_REPRO_HIDE, 0, 0)

    pressed = False
    try:
        if kind == "hold":
            win32.mouse_move(*start)
            ctx.sleep(0.05)
            pressed = True
            win32.mouse_down()
            for step in range(4):
                ctx.sleep(0.25)
                sample("holding", step)
        elif kind == "rapid":
            pressed = True
            win32.drag(start, _endpoint(start, rect, work), steps=3, hold=0.01, step_delay=0.005,
                       release=False, sample=sample)
        elif kind == "edge":
            # Leave the work area: the pointer crosses its right edge (another monitor or clamped).
            pressed = True
            win32.drag(start, (work[2] + 120, start[1]), steps=25, release=False, sample=sample)
        else:  # drag, hide
            pressed = True
            win32.drag(start, _endpoint(start, rect, work), steps=24, release=False, sample=sample)
        row["held"] = win32.window_rect(repro.overlay)
    finally:
        if pressed:
            win32.mouse_up()
    ctx.sleep(1.2 if kind != "hide" else 1.6)  # the hidden overlay reappears after 1 s
    row["after"] = win32.window_rect(repro.overlay)
    ctx.sleep(0.5)
    row["settled"] = win32.window_rect(repro.overlay)
    row["button_down_after"] = bool(win32.user32.GetAsyncKeyState(0x01) & 0x8000)
    row["foreground_after"], root = _foreground_owner()
    row["foreground_after_owner"] = (root, row["foreground_after"]["process_id"])
    events = repro.events()[mark:]
    names = [event.get("event") for event in events]
    row["events"] = events[:80]
    row["dom_down"] = sum(1 for e in events if e.get("event") == "page" and e.get("message") == "down"
                          and e.get("window") == "overlay")
    row["dom_up"] = sum(1 for e in events if e.get("event") == "page" and e.get("message") == "up"
                        and e.get("window") == "overlay")
    row["drag_engaged"] = names.count("drag_engaged")
    row["enter_size_move"], row["exit_size_move"] = names.count("enter_size_move"), names.count("exit_size_move")
    row["handback"] = names.count("handback")
    held = row["held"]
    row["moved_while_held"] = abs(held[0] - rect[0]) >= 100 or abs(held[1] - rect[1]) >= 100
    row["release_clean"] = (not row["button_down_after"] and row["enter_size_move"] == row["exit_size_move"]
                            and row["after"] == row["settled"])
    row["foreground_kept"] = row["foreground_after_owner"] == expected
    return row


def typing_probe(ctx, row, expected, read_text, token, element_focused=None):
    from checks.overlay import _o8_type_after_release  # the verified O8 typing gate
    observed = {"foreground_before": row["foreground_before"]}
    probe = _o8_type_after_release(ctx, observed, expected, read_text, token, element_focused)
    row["typing"] = probe
    return probe["landed"]


def focus_field(ctx, repro):
    """Click the reproducer's text field only when it is unobstructed in its owned window."""
    window = uia.from_handle(repro.settings)
    field = uia.find(window, name_="Repro field", control_type_=uia.EDIT, timeout=10, sleep=ctx.sleep)
    bounds = uia.rect(field)
    left, top, right, bottom = win32.window_rect(repro.settings)
    if not (left <= bounds[0] < bounds[2] <= right and top <= bounds[1] < bounds[3] <= bottom):
        raise Fail(f"Reproducer field {bounds} is not inside its window")
    point = ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)
    ctx.guard.check()
    if not repro.owned() or not win32.activate(repro.settings, guard_check=ctx.guard.check):
        raise Fail("Could not activate the owned reproducer Settings window")
    hit = win32.user32.WindowFromPoint(win32.POINT(*point))
    if not hit or win32.user32.GetAncestor(hit, win32.GA_ROOT) != repro.settings:
        raise Fail("Reproducer field is covered")
    ctx.guard.check()
    win32.click(*point)
    ctx.sleep(0.4)
    if not uia.same(uia.focused(), field):
        raise Fail("Reproducer field did not take keyboard focus")
    return field


def topology_matches(snap):
    chain = [node["class"] for node in snap.get("ancestors", [])][:-1]
    return snap.get("complete") and chain == RENDERER_CHAIN


SETTINGS_CASES = (("settings drag", "drag"), ("settings hold without moving", "hold"),
                  ("settings rapid click-drag-release", "rapid"), ("settings leave work area", "edge"),
                  ("settings repeat 1", "drag"), ("settings repeat 2", "drag"), ("settings repeat 3", "drag"),
                  ("settings hide during gesture", "hide"))


def run_mode(ctx, mode, pad, record):
    repro = Reproducer(ctx, mode).start()
    result = {"mode": mode, "host_input": [e for e in repro.events() if e.get("event") == "host_input"],
              "cases": []}
    record["modes"].append(result)
    try:
        _rect, _start, hit = _drag_start(ctx, repro, "topology")
        snap = topology.snapshot(repro.overlay, hit)
        result["topology"] = {"complete": snap["complete"], "errors": snap["errors"],
                              "chain": [node["class"] for node in snap["ancestors"]],
                              "classification": topology.classify(snap, repro.process.pid,
                                                                  win32.window_thread(repro.overlay))}
        result["topology_matches_murmur"] = bool(topology_matches(snap))
        ctx.guard.check()
        pid = repro.process.pid
        sequence = [("notepad drag", "drag", "notepad")] + [(l, k, "settings") for l, k in SETTINGS_CASES] + \
                   [("notepad drag after sequence", "drag", "notepad")]
        for index, (label, kind, target) in enumerate(sequence):
            if target == "notepad":
                pad.focus(guard_check=ctx.guard.check)
                if not pad.owns_keyboard_focus():
                    raise Fail(f"Owned Notepad editor did not own keyboard focus before {label}")
                expected, read_text, element = (pad.hwnd, pad.owner_pid), pad.text, None
            else:
                field = focus_field(ctx, repro)
                expected = (repro.settings, pid)
                read_text = lambda field=field: uia.value(field)  # noqa: E731
                element = lambda field=field: uia.same(uia.focused(), field)  # noqa: E731
            row = gesture(ctx, repro, label, kind, expected)
            result["cases"].append(row)
            typing_probe(ctx, row, expected, read_text, f"o9{mode[1]}{index}", element)
    finally:
        repro.stop()
    return result


def summarize(result):
    cases = {row["case"]: row for row in result["cases"]}
    drags = [row for row in result["cases"] if row["kind"] == "drag"]
    problems = []
    for row in result["cases"]:
        if not row["release_clean"]:
            problems.append(f"{row['case']}: release not clean")
        if row["focus_changes"] or not row["foreground_kept"]:
            problems.append(f"{row['case']}: focus or foreground changed")
        if not row.get("typing", {}).get("landed"):
            problems.append(f"{row['case']}: typing after release did not land exactly")
    for row in drags:
        if not row["moved_while_held"]:
            problems.append(f"{row['case']}: no held movement")
    for kind in ("hold", "rapid"):
        for row in result["cases"]:
            if row["kind"] == kind and row["after"] != row["settled"]:
                problems.append(f"{row['case']}: overlay kept moving after release")
    settings = cases.get("settings drag", {})
    notepad = cases.get("notepad drag", {})
    return {"problems": problems,
            "settings_drag_moved": settings.get("moved_while_held"),
            "notepad_drag_moved": notepad.get("moved_while_held"),
            "reproduces_murmur": (result.get("topology_matches_murmur") and notepad.get("moved_while_held") is True
                                  and settings.get("moved_while_held") is False)}
