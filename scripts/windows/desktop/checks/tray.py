"""T1-T3, A1, A2: notification-area icon, tray menu, single instance, Start with Windows."""
import json
import subprocess

import uia
import win32
from context import Fail, Skip

RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
MENU_ITEMS = ("Dictate", "Sounds mute", "Overlay enabled", "Settings…", "Reload config", "Quit")
OVERFLOW_CLASSES = ("NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland")
TRAY_ICON_NAME = r"^Murmur — \S"  # exact tooltip prefix from src-tauri/src/main.rs and app.rs


def _tray_diagnostic(ctx, taskbar=0, overflow=0, menus=(), action=None):
    """Capture only surfaces relevant to this tray action; discovery is read-only."""
    def describe(hwnd):
        if not hwnd:
            return None
        row = {"hwnd": hwnd}
        try:
            row["pid"] = win32.window_pid(hwnd)
            row["title"] = win32.window_text(hwnd)
            row["names"] = uia.names(uia.from_handle(hwnd))[:80]
        except Exception as error:  # noqa: BLE001 - stale UIA tree is useful evidence too
            row["uia_error"] = f"{type(error).__name__}: {error}"
        return row

    ctx.artifact("tray-diagnostic.json").write_text(json.dumps({
        "taskbar": describe(taskbar), "overflow": describe(overflow),
        "menus": [describe(hwnd) for hwnd in menus], "action": action}, indent=2), encoding="utf-8")


def _overflow_window():
    return next((hwnd for class_name in OVERFLOW_CLASSES
                 for hwnd in win32.find_windows(class_=class_name)), 0)


def _tray_button(ctx, operation="unknown"):
    """The Murmur notification icon (tooltip 'Murmur — …'), opening the overflow flyout if needed."""
    taskbar = next(iter(win32.find_windows(class_="Shell_TrayWnd")), 0)
    if not taskbar:
        raise Skip("no taskbar (Shell_TrayWnd) found")
    bar = uia.from_handle(taskbar)
    icon = uia.try_find(bar, name_re=TRAY_ICON_NAME, control_type_=uia.BUTTON, timeout=2, sleep=ctx.sleep)
    if icon is not None:
        return icon
    overflow = _overflow_window()
    if overflow:
        icon = uia.try_find(uia.from_handle(overflow), name_re=TRAY_ICON_NAME,
                            control_type_=uia.BUTTON, timeout=2, sleep=ctx.sleep)
        if icon is None:
            _tray_diagnostic(ctx, taskbar, overflow, action={"operation": operation, "phase": "icon discovery"})
            raise Skip("Murmur icon is not exposed in the already open overflow flyout")
        return icon
    chevron = uia.try_find(bar, name_re=r"(?i)hidden icons", control_type_=uia.BUTTON, timeout=2, sleep=ctx.sleep)
    if chevron is None:
        _tray_diagnostic(ctx, taskbar, action={"operation": operation, "phase": "icon discovery"})
        raise Skip("icon hidden: Murmur's icon is not on the taskbar and no overflow button was found")
    win32.click(*uia.center(chevron))
    overflow = win32.wait_for(_overflow_window, 3, sleep=ctx.sleep)
    icon = (uia.try_find(uia.from_handle(overflow), name_re=TRAY_ICON_NAME, control_type_=uia.BUTTON,
                         timeout=3, sleep=ctx.sleep) if overflow else None)
    if icon is None:
        _tray_diagnostic(ctx, taskbar, overflow, action={"operation": operation, "phase": "icon discovery"})
        raise Skip("icon hidden: Murmur's icon is not in the overflow flyout either")
    return icon


def _hide_settings(ctx):
    hwnd = ctx.murmur.settings_hwnd()
    if hwnd:
        win32.close_window(hwnd)
        win32.wait_for(lambda: not ctx.murmur.settings_hwnd(), 5, sleep=ctx.sleep)


def t1_tray_left(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    _hide_settings(ctx)
    icon = _tray_button(ctx, "left click")
    win32.click(*uia.center(icon))
    hwnd = win32.wait_for(ctx.murmur.settings_hwnd, 5, sleep=ctx.sleep)
    if not hwnd:
        raise Fail("Left-click on the tray icon did not open Settings")
    if not win32.wait_for(lambda: win32.foreground() == hwnd, 3, sleep=ctx.sleep):
        raise Fail("Settings opened but did not come to the front")
    ctx.screenshot(hwnd, "settings.png")


def _menu(ctx, operation="unknown"):
    icon = _tray_button(ctx, operation)
    try:
        rect = uia.rect(icon)
    except Exception as error:  # noqa: BLE001 - overflow can rebuild between discovery and click
        _tray_diagnostic(ctx, next(iter(win32.find_windows(class_="Shell_TrayWnd")), 0),
                         _overflow_window(), action={"operation": operation, "phase": "icon bounds",
                                                     "error": f"{type(error).__name__}: {error}"})
        raise Fail("Murmur tray icon bounds became unavailable before right-click") from error
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        _tray_diagnostic(ctx, next(iter(win32.find_windows(class_="Shell_TrayWnd")), 0),
                         _overflow_window(), action={"operation": operation, "phase": "icon bounds",
                                                     "icon_rect": rect})
        raise Fail(f"Murmur tray icon has no clickable bounds: {rect}")
    point = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
    before_overflow = _overflow_window()
    action = {"operation": operation, "icon_point": point, "icon_rect": rect,
              "icon_uia_hwnd": uia.handle(icon), "before_overflow_hwnd": before_overflow,
              "expected_app_pid": ctx.murmur.process.pid}
    if win32.IS_WINDOWS:
        hit = win32.user32.WindowFromPoint(win32.POINT(*point))
        root = win32.user32.GetAncestor(hit, win32.GA_ROOT) if hit else 0
        action.update(hit_hwnd=hit, hit_root_hwnd=root,
                      hit_root_pid=win32.window_pid(root) if root else 0)
    # find_windows lists visible top-level menus. A closed menu may reuse an HWND;
    # it is fresh when it was absent from this visible set before the right click.
    before = set(win32.find_windows(class_="#32768"))
    action["before_visible_menus"] = sorted(before)
    action["before_visible_menu_pids"] = {str(hwnd): win32.window_pid(hwnd) for hwnd in before}
    win32.click(*point, button="right")
    owner = ctx.murmur.process.pid
    menu = win32.wait_for(lambda: next((hwnd for hwnd in win32.find_windows(class_="#32768")
                                        if hwnd not in before and win32.window_pid(hwnd) == owner), 0),
                          3, sleep=ctx.sleep)
    action["after_overflow_hwnd"] = _overflow_window()
    action["after_visible_menus"] = win32.find_windows(class_="#32768")
    action["opened_menu_hwnd"] = menu or 0
    action["opened_menu_pid"] = win32.window_pid(menu) if menu else 0
    if not menu:
        _tray_diagnostic(ctx, next(iter(win32.find_windows(class_="Shell_TrayWnd")), 0),
                         _overflow_window(), action["after_visible_menus"], action=action)
        raise Fail("Murmur's newly opened tray context menu (#32768) did not appear")
    return uia.from_handle(menu)


def _invoke(ctx, label):
    menu = _menu(ctx, label)
    item = uia.try_find(menu, name_=label, control_type_=uia.MENUITEM, timeout=2, sleep=ctx.sleep)
    if item is None:
        _tray_diagnostic(ctx, next(iter(win32.find_windows(class_="Shell_TrayWnd")), 0),
                         _overflow_window(), win32.find_windows(class_="#32768"),
                         action={"operation": label, "phase": "menu item discovery"})
        raise Fail(f"Murmur tray menu item {label!r} not exposed through UI Automation")
    uia.invoke(item)
    ctx.sleep(1.0)


def t2_tray_menu(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"general": {"hotkey_mode": "toggle"}})
    observed = []
    overlay = ctx.murmur.overlay_hwnd()
    _invoke(ctx, "Overlay enabled")
    hidden = not win32.user32.IsWindowVisible(overlay) or ctx.murmur.overlay_state() == "unknown"
    _invoke(ctx, "Overlay enabled")
    if not hidden:
        raise Fail("'Overlay enabled' did not hide the overlay")
    observed.append("Overlay enabled")
    _invoke(ctx, "Sounds mute")
    muted = "muted = true" in ctx.murmur.config_path.read_text(encoding="utf-8")
    _invoke(ctx, "Sounds mute")
    if not muted:
        raise Fail("'Sounds mute' did not set muted = true in config.toml")
    observed.append("Sounds mute")
    _invoke(ctx, "Dictate")
    if not ctx.murmur.wait_state("listening", 5):
        raise Fail("'Dictate' (toggle mode) did not start a dictation")
    _invoke(ctx, "Dictate")
    ctx.settle()
    observed.append("Dictate")
    _hide_settings(ctx)
    _invoke(ctx, "Settings…")
    if not win32.wait_for(ctx.murmur.settings_hwnd, 5, sleep=ctx.sleep):
        raise Fail("'Settings…' did not open Settings")
    observed.append("Settings…")
    _invoke(ctx, "Reload config")
    ctx.note("'Reload config' invoked; no observable effect is asserted")
    process = ctx.murmur.process
    _invoke(ctx, "Quit")
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        raise Fail("'Quit' did not end murmur.exe") from None
    ctx.murmur.process = None
    ctx.sleep(1.0)
    taskbar = next(iter(win32.find_windows(class_="Shell_TrayWnd")), 0)
    if taskbar and uia.try_find(uia.from_handle(taskbar), name_re=TRAY_ICON_NAME, control_type_=uia.BUTTON):
        raise Fail("The tray icon stayed after Quit")
    observed.append("Quit")
    ctx.note(f"observed: {', '.join(observed)}")


def t3_single_instance(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    _hide_settings(ctx)
    ctx.murmur.open_settings()
    hwnd = ctx.murmur.settings_hwnd()
    running = win32.pids("murmur.exe")
    if running != [ctx.murmur.process.pid]:
        raise Fail(f"Expected one murmur.exe ({ctx.murmur.process.pid}), found {running}")
    if not win32.wait_for(lambda: win32.foreground() == hwnd, 3, sleep=ctx.sleep):
        raise Fail("The second launch did not bring Settings to the front")


def _watch_run_keys(ctx):
    """Restore the owner's Run / StartupApproved values whatever the app writes (same value name)."""
    registry = win32.Registry()
    snapshots = {path: registry.typed_values(path) for path in (RUN, APPROVED)}

    def undo():
        for path, before in snapshots.items():
            after = registry.typed_values(path)
            for name in after:
                if name not in before:
                    registry.delete(path, name)
            for name, (value, kind) in before.items():
                if after.get(name) != (value, kind):
                    registry.write(path, name, value, kind)  # same type (REG_EXPAND_SZ stays expandable)

    ctx.restorer.push("restore HKCU Run entries", undo)
    return registry


def _toggle_autostart(ctx, wanted):
    window = ctx.murmur.page("General")
    switch = uia.named_toggle(window, "Start at login", timeout=10, sleep=ctx.sleep)
    if uia.toggle_state(switch) != int(wanted):
        uia.toggle(switch)
    ctx.sleep(1.5)


def _entry(registry, app):
    return next(((n, v) for n, v in registry.values(RUN).items() if str(app).lower() in str(v).lower()), None)


def a1_autostart_on(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    registry = _watch_run_keys(ctx)
    _toggle_autostart(ctx, True)
    entry = _entry(registry, ctx.murmur.app)
    path = ctx.artifact("run-key.txt")
    path.write_text(f"{entry}\n", encoding="utf-8")
    if entry is None:
        raise Fail("No HKCU Run entry points at the tested murmur.exe")
    if "--background" not in entry[1]:
        raise Fail(f"Run entry lacks --background: {entry[1]!r}")
    ctx.note("sign-out/sign-in half is manual (manual.py A1)")


def a2_autostart_off(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    registry = _watch_run_keys(ctx)
    _toggle_autostart(ctx, True)
    _toggle_autostart(ctx, False)
    entry = _entry(registry, ctx.murmur.app)
    ctx.artifact("run-key.txt").write_text(f"{entry}\n", encoding="utf-8")
    if entry is not None:
        raise Fail(f"Run entry still present after disabling: {entry}")
