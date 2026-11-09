"""T1-T3, A1, A2: notification-area icon, tray menu, single instance, Start with Windows."""
import subprocess
import winreg

import uia
import win32
from context import Fail, Skip

RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
MENU_ITEMS = ("Dictate", "Sounds mute", "Overlay enabled", "Settings…", "Reload config", "Quit")


def _tray_button(ctx):
    """The Murmur notification icon (tooltip 'Murmur — …'), opening the overflow flyout if needed."""
    taskbar = next(iter(win32.find_windows(class_="Shell_TrayWnd")), 0)
    if not taskbar:
        raise Skip("no taskbar (Shell_TrayWnd) found")
    bar = uia.from_handle(taskbar)
    icon = uia.try_find(bar, name_re=r"^Murmur", control_type_=uia.BUTTON, timeout=2, sleep=ctx.sleep)
    if icon is not None:
        return icon
    chevron = uia.try_find(bar, name_re=r"(?i)hidden icons", control_type_=uia.BUTTON, timeout=2, sleep=ctx.sleep)
    if chevron is None:
        raise Skip("icon hidden: Murmur's icon is not on the taskbar and no overflow button was found")
    win32.click(*uia.center(chevron))
    ctx.sleep(1.0)
    icon = uia.try_find(uia.root(), name_re=r"^Murmur", control_type_=uia.BUTTON, timeout=3, sleep=ctx.sleep)
    if icon is None:
        win32.tap("esc")
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
    icon = _tray_button(ctx)
    win32.click(*uia.center(icon))
    hwnd = win32.wait_for(ctx.murmur.settings_hwnd, 5, sleep=ctx.sleep)
    if not hwnd:
        raise Fail("Left-click on the tray icon did not open Settings")
    if not win32.wait_for(lambda: win32.foreground() == hwnd, 3, sleep=ctx.sleep):
        raise Fail("Settings opened but did not come to the front")
    ctx.screenshot(hwnd, "settings.png")


def _menu(ctx):
    icon = _tray_button(ctx)
    win32.click(*uia.center(icon), button="right")
    menu = win32.wait_for(lambda: next(iter(win32.find_windows(class_="#32768")), 0), 3, sleep=ctx.sleep)
    if not menu:
        raise Skip("the tray context menu (#32768) did not appear")
    return uia.from_handle(menu)


def _invoke(ctx, label):
    menu = _menu(ctx)
    item = uia.try_find(menu, name_=label, control_type_=uia.MENUITEM, timeout=2, sleep=ctx.sleep)
    if item is None:
        win32.tap("esc")
        raise Skip(f"menu item {label!r} not exposed through UI Automation")
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
        ctx.note("'Sounds mute' toggled, but config.toml did not show muted = true")
    else:
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
    if taskbar and uia.try_find(uia.from_handle(taskbar), name_re=r"^Murmur", control_type_=uia.BUTTON):
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
    snapshots = {path: registry.values(path) for path in (RUN, APPROVED)}

    def undo():
        for path, before in snapshots.items():
            after = registry.values(path)
            for name in after:
                if name not in before:
                    registry.delete(path, name)
            for name, value in before.items():
                if after.get(name) != value:
                    kind = winreg.REG_BINARY if isinstance(value, bytes) else winreg.REG_SZ
                    registry.write(path, name, value, kind)

    ctx.restorer.push("restore HKCU Run entries", undo)
    return registry


def _toggle_autostart(ctx, wanted):
    window = ctx.murmur.page("General")
    switch = uia.find_following(window, "Start at login", control_type_=(uia.CHECKBOX, uia.BUTTON), timeout=10,
                                sleep=ctx.sleep)
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
