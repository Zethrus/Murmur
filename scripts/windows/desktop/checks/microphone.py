"""M1, M3: Murmur's reading of the "Let desktop apps access your microphone" switch.

M1 changes only the per-user consent value Murmur reads (HKCU ConsentStore\\microphone\\NonPackaged);
it tests the explanation, not Windows blocking capture. The value is restored after the check.
"""
from pathlib import Path
import time
try:
    import winreg
except ImportError:  # not Windows: the module still imports for the unit tests
    winreg = None

import uia
import win32
from checks.common import wait_names
from context import Fail
from restore import RegistryValue

CONSENT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged"
EXPLANATION = "Let desktop apps access your microphone"


def _windows_settings_windows():
    """Include an existing Settings window: ms-settings: reuses it on Windows 11."""
    return [hwnd for hwnd, pid, title, class_name in win32.enum_windows()
            if (class_name == "ApplicationFrameWindow" and "Settings" in title)
            or Path(win32.process_path(pid) or "").name.lower() == "systemsettings.exe"]


def m1_desktop_apps_off(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    RegistryValue(win32.Registry(), CONSENT, "Value").set(ctx.restorer, "Deny", winreg.REG_SZ)
    ctx.murmur.page("Audio")
    uia.invoke(uia.find(ctx.murmur.settings(), name_="Refresh inputs", control_type_=uia.BUTTON, timeout=10, sleep=ctx.sleep))
    names = wait_names(ctx, ctx.murmur.settings, lambda n: any(EXPLANATION in x for x in n), 10)
    ctx.screenshot(ctx.murmur.settings_hwnd(), "desktop-apps-off.png")
    if not names:
        raise Fail("No explanation naming the desktop-apps microphone switch")
    before = set(_windows_settings_windows())
    uia.invoke(uia.find(ctx.murmur.settings(), name_="Open Windows privacy settings", control_type_=uia.BUTTON,
                        timeout=5, sleep=ctx.sleep))
    opened = win32.wait_for(lambda: next((h for h in _windows_settings_windows()
                                          if h not in before or win32.foreground() == h), 0), 10,
                            sleep=ctx.sleep)
    if not opened:
        windows = [(hex(h), title, class_name) for h, _, title, class_name in win32.enum_windows()
                   if "Settings" in title or "Microphone" in title]
        names = uia.names(ctx.murmur.settings())
        ctx.artifact("settings-open-diagnostic.txt").write_text(
            f"existing Settings windows: {[hex(h) for h in before]!r}\n"
            f"foreground: {hex(win32.foreground())} {win32.window_text(win32.foreground())!r}\n"
            f"candidate windows: {windows!r}\n"
            f"Murmur UI feedback: {[n for n in names if 'Could not open Settings' in n]!r}\n",
            encoding="utf-8")
        raise Fail("'Open Windows privacy settings' did not open or foreground Windows Settings")
    ctx.screenshot(opened, "windows-settings.png")
    page_names = uia.names(uia.from_handle(opened))
    ctx.artifact("windows-settings-names.txt").write_text(repr(page_names[:80]) + "\n", encoding="utf-8")
    if not any("Microphone" in name for name in page_names):
        raise Fail("Windows Settings opened without the Microphone page")
    if opened not in before:
        win32.close_window(opened)


def m3_restored(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    ctx.murmur.page("Audio")
    uia.invoke(uia.find(ctx.murmur.settings(), name_="Refresh inputs", control_type_=uia.BUTTON, timeout=10, sleep=ctx.sleep))
    ctx.sleep(2.0)
    if any(EXPLANATION in n or "turned off" in n for n in uia.names(ctx.murmur.settings())):
        raise Fail("The privacy explanation is still shown with the switches restored")
    meter = uia.find(ctx.murmur.settings(), name_="Microphone level", timeout=10, sleep=ctx.sleep)
    app = ctx.target("A")
    app.focus()
    levels = []
    ctx.observe_delivery("A")
    ctx.press(ctx.hotkey)
    if not ctx.murmur.wait_state("listening", 5):
        ctx.release(ctx.hotkey, "together")
        raise Fail("Dictation did not start")
    deadline = time.monotonic() + ctx.fixtures["A"].speech_seconds + 0.8
    while time.monotonic() < deadline:
        try:
            levels.append(uia.range_value(meter))
        except Exception:  # noqa: BLE001 - meter rebuilt
            pass
        ctx.sleep(0.1)
    ctx.release(ctx.hotkey, "key-first")
    text = app.wait_text()
    ctx.settle()
    ctx.note(f"level meter maximum {max(levels, default=0)} over {len(levels)} samples")
    if not levels or max(levels) <= 0:
        raise Fail("The level meter did not move during the dictation")
    ctx.expect_delivery(text, "dictation after restoring the switch")
