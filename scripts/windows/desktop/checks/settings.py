"""S1, S3, S4: Settings captures, theme following Windows, minimum size.

S1 ports scripts/parity/capture-windows.ps1: theme through config.toml (the app reloads it), each
page button invoked through UI Automation, the Settings window saved at the current scaling.
"""
import winreg

import capture
import uia
import win32
from context import Fail
from restore import RegistryValue

PAGES = ("General", "Components", "Audio", "Behavior", "Privacy", "Companion")
DEFAULT_SIZE, MINIMUM_SIZE = (1000, 780), (760, 560)  # logical, src-tauri/tauri.conf.json
PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def s1_captures(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    hwnd = ctx.murmur.show_settings()
    scale = win32.dpi_scale(hwnd)
    width, height = win32.client_size(hwnd)
    expected = (round(DEFAULT_SIZE[0] * scale), round(DEFAULT_SIZE[1] * scale))
    if abs(width - expected[0]) > 4 or abs(height - expected[1]) > 4:
        ctx.note(f"Settings client size {width}x{height}, configured {expected[0]}x{expected[1]} at {scale:.0%}")
        raise Fail(f"Settings window is {width}x{height} px, expected the configured "
                   f"{expected[0]}x{expected[1]} px at {scale:.0%} scaling")
    blank = []
    for theme in ("light", "dark"):
        ctx.murmur.ensure(ctx.fixtures["A"].path, {"general": {"theme": theme}})
        for page in PAGES:
            ctx.murmur.page(page)
            win32.activate(hwnd)
            ctx.sleep(0.3)
            path = ctx.artifact(f"windows-{theme}-{page.lower()}-{round(scale * 100)}.png")
            capture.window(hwnd, path)
            if capture.is_blank(path):
                blank.append(path.name)
    if blank:
        raise Fail(f"Blank captures: {', '.join(blank)}")
    ctx.note(f"12 captures at {scale:.0%} scaling")


def _background(ctx, hwnd, suffix):
    path = ctx.screenshot(hwnd, suffix)
    if path is None:
        raise Fail("Could not capture the Settings window")
    width, height = win32.client_size(hwnd)
    # A strip inside the window below the title bar, away from the controls on the right.
    return capture.luminance(path, (10, 60, max(40, width // 6), max(120, height // 3)))


def s3_theme(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"general": {"theme": "auto"}})
    hwnd = ctx.murmur.show_settings()
    registry = win32.Registry()
    value = RegistryValue(registry, PERSONALIZE, "AppsUseLightTheme")
    ctx.restorer.push("broadcast the restored theme", lambda: win32.broadcast_setting_change("ImmersiveColorSet"))
    readings = {}
    for light in (1, 0, 1):
        value.set(ctx.restorer, light, winreg.REG_DWORD)
        win32.broadcast_setting_change("ImmersiveColorSet")
        wanted = "light" if light else "dark"
        reading = None
        for _ in range(10):  # WebView2 may follow the broadcast with a delay: up to 5 s
            ctx.sleep(0.5)
            reading = _background(ctx, hwnd, f"auto-{wanted}-{len(readings)}.png")
            if (reading > 128) == bool(light):
                break
        readings[f"{wanted}-{len(readings)}"] = round(reading, 1)
        if (reading > 128) != bool(light):
            raise Fail(f"Settings did not follow Windows to {wanted} without a restart (background luminance "
                       f"{reading:.0f}); readings {readings}")
    ctx.note(f"background luminance {readings}")


def s4_minimum_size(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    hwnd = ctx.murmur.show_settings()
    left, top, right, bottom = win32.window_rect(hwnd)
    ctx.restorer.push("restore the Settings size", lambda: win32.set_window_pos(hwnd, left, top, right - left,
                                                                                bottom - top))
    scale = win32.dpi_scale(hwnd)
    win32.set_window_pos(hwnd, left, top, round(500 * scale), round(400 * scale))
    ctx.sleep(1.0)
    width, height = win32.client_size(hwnd)
    minimum = (round(MINIMUM_SIZE[0] * scale), round(MINIMUM_SIZE[1] * scale))
    ctx.note(f"asked for 500x400 logical, got client {width}x{height} px (minimum {minimum[0]}x{minimum[1]})")
    if width < minimum[0] - 2 or height < minimum[1] - 2:
        raise Fail(f"Settings shrank to {width}x{height} px, below the {minimum[0]}x{minimum[1]} px minimum")
    window = ctx.murmur.settings()
    for page in PAGES:
        button = uia.find(window, name_=page, control_type_=uia.BUTTON, timeout=10, sleep=ctx.sleep)
        uia.invoke(button)
        ctx.sleep(0.6)
        ctx.screenshot(hwnd, f"min-{page.lower()}.png")
    win32.set_window_pos(hwnd, left, top, round(DEFAULT_SIZE[0] * scale), round(DEFAULT_SIZE[1] * scale))
