"""Thin ctypes layer over user32/kernel32 for the desktop harness (Windows only at run time).

Imports cleanly on any OS (the DLLs are loaded only on Windows) so the pure helpers and the
unit tests run on Linux. Every coordinate is in physical pixels: call set_dpi_aware() first.
Injected input goes through SendInput, which marks it LLKHF_INJECTED / LLMHF_INJECTED; the input
guard (guard.py) uses that flag to tell the harness's input from a real person's.
"""
import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import time

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
else:  # pragma: no cover - documentation and tests only
    user32 = kernel32 = dwmapi = None

ULONG_PTR = ctypes.c_size_t
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
WM_CLOSE, WM_SETTINGCHANGE = 0x0010, 0x001A
HWND_BROADCAST, SMTO_ABORTIFHUNG = 0xFFFF, 0x0002
SW_RESTORE = 9
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

VK = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "super": 0x5B,
      "esc": 0x1B, "escape": 0x1B, "enter": 0x0D, "return": 0x0D, "space": 0x20, "tab": 0x09,
      "backspace": 0x08, "delete": 0x2E, "home": 0x24, "end": 0x23, "left": 0x25, "up": 0x26,
      "right": 0x27, "down": 0x28, "pageup": 0x21, "pagedown": 0x22}
VK.update({f"f{n}": 0x6F + n for n in range(1, 25)})
VK.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
VK.update({chr(c): c for c in range(ord("0"), ord("9") + 1)})
EXTENDED = {0x5B, 0x2E, 0x24, 0x23, 0x25, 0x26, 0x27, 0x28, 0x21, 0x22}
MODIFIERS = {"ctrl", "control", "alt", "shift", "win", "super"}
HOTKEY_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
               "win": MOD_WIN, "super": MOD_WIN}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    def tuple(self):
        return (self.left, self.top, self.right, self.bottom)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ULONG_PTR), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def _declare():
    """Argument and return types (64-bit handles must not be truncated to int)."""
    if not IS_WINDOWS:
        return
    handle = ctypes.c_void_p
    for name, args, res in (
        ("SendInput", (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int), wintypes.UINT),
        ("GetForegroundWindow", (), wintypes.HWND),
        ("SetForegroundWindow", (wintypes.HWND,), wintypes.BOOL),
        ("BringWindowToTop", (wintypes.HWND,), wintypes.BOOL),
        ("ShowWindow", (wintypes.HWND, ctypes.c_int), wintypes.BOOL),
        ("IsIconic", (wintypes.HWND,), wintypes.BOOL),
        ("IsWindow", (wintypes.HWND,), wintypes.BOOL),
        ("IsWindowVisible", (wintypes.HWND,), wintypes.BOOL),
        ("GetWindowTextW", (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int), ctypes.c_int),
        ("GetWindowTextLengthW", (wintypes.HWND,), ctypes.c_int),
        ("GetClassNameW", (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int), ctypes.c_int),
        ("GetWindowThreadProcessId", (wintypes.HWND, ctypes.POINTER(wintypes.DWORD)), wintypes.DWORD),
        ("GetWindowRect", (wintypes.HWND, ctypes.POINTER(RECT)), wintypes.BOOL),
        ("GetClientRect", (wintypes.HWND, ctypes.POINTER(RECT)), wintypes.BOOL),
        ("SetWindowPos", (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          wintypes.UINT), wintypes.BOOL),
        ("GetDpiForWindow", (wintypes.HWND,), wintypes.UINT),
        ("SetProcessDpiAwarenessContext", (handle,), wintypes.BOOL),
        ("GetSystemMetrics", (ctypes.c_int,), ctypes.c_int),
        ("GetCursorPos", (ctypes.POINTER(POINT),), wintypes.BOOL),
        ("PostMessageW", (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM), wintypes.BOOL),
        ("SendMessageTimeoutW", (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR, wintypes.UINT,
                                 wintypes.UINT, ctypes.POINTER(ULONG_PTR)), wintypes.LPARAM),
        ("AttachThreadInput", (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL), wintypes.BOOL),
        ("OpenClipboard", (wintypes.HWND,), wintypes.BOOL),
        ("CloseClipboard", (), wintypes.BOOL),
        ("EmptyClipboard", (), wintypes.BOOL),
        ("GetClipboardData", (wintypes.UINT,), handle),
        ("SetClipboardData", (wintypes.UINT, handle), handle),
        ("EnumClipboardFormats", (wintypes.UINT,), wintypes.UINT),
        ("IsClipboardFormatAvailable", (wintypes.UINT,), wintypes.BOOL),
        ("GetLastInputInfo", (ctypes.POINTER(LASTINPUTINFO),), wintypes.BOOL),
        ("OpenInputDesktop", (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), handle),
        ("CloseDesktop", (handle,), wintypes.BOOL),
        ("GetUserObjectInformationW", (handle, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD)), wintypes.BOOL),
        ("MapVirtualKeyW", (wintypes.UINT, wintypes.UINT), wintypes.UINT),
        ("GetAsyncKeyState", (ctypes.c_int,), ctypes.c_short),
        ("RegisterHotKey", (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT), wintypes.BOOL),
        ("UnregisterHotKey", (wintypes.HWND, ctypes.c_int), wintypes.BOOL),
    ):
        function = getattr(user32, name)
        function.argtypes, function.restype = args, res
    for name, args, res in (
        ("GlobalAlloc", (wintypes.UINT, ctypes.c_size_t), handle),
        ("GlobalLock", (handle,), ctypes.c_void_p),
        ("GlobalUnlock", (handle,), wintypes.BOOL),
        ("GlobalFree", (handle,), handle),
        ("GetTickCount", (), wintypes.DWORD),
        ("GetCurrentThreadId", (), wintypes.DWORD),
        ("CreateToolhelp32Snapshot", (wintypes.DWORD, wintypes.DWORD), handle),
        ("Process32FirstW", (handle, ctypes.POINTER(PROCESSENTRY32W)), wintypes.BOOL),
        ("Process32NextW", (handle, ctypes.POINTER(PROCESSENTRY32W)), wintypes.BOOL),
        ("CloseHandle", (handle,), wintypes.BOOL),
        ("GetLongPathNameW", (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD), wintypes.DWORD),
    ):
        function = getattr(kernel32, name)
        function.argtypes, function.restype = args, res
    dwmapi.DwmGetWindowAttribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


_declare()

# guard.py installs a check here: every injection first makes sure no real input arrived.
_before_input = None


def set_input_check(callback):
    global _before_input
    _before_input = callback


def require_windows():
    if not IS_WINDOWS:
        raise RuntimeError("The desktop harness runs only on Windows")


def set_dpi_aware():
    """Per-monitor v2: every window and screen coordinate in physical pixels."""
    require_windows()
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))


def long_path(path):
    """Expand 8.3 short names (Edison's %TEMP% can be one)."""
    path = os.path.abspath(path)
    if not IS_WINDOWS:
        return path
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetLongPathNameW(path, buffer, len(buffer))
    return buffer.value if 0 < length < len(buffer) else path


# ---------------------------------------------------------------- keyboard and mouse


def parse_combo(spec):
    """'Ctrl+Alt+Shift+F9' -> (['ctrl', 'alt', 'shift'], 'f9'). Raises on unknown keys."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("Empty key combination")
    for part in parts:
        if part not in VK:
            raise ValueError(f"Unknown key {part!r} in {spec!r}")
    modifiers = [p for p in parts if p in MODIFIERS]
    keys = [p for p in parts if p not in MODIFIERS]
    if len(keys) != 1:
        raise ValueError(f"{spec!r} needs exactly one non-modifier key")
    return modifiers, keys[0]


def _key_input(vk, up):
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    scan = user32.MapVirtualKeyW(vk, 0) if IS_WINDOWS else 0
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return event


def _send(events):
    require_windows()
    if _before_input:
        _before_input()
    array = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise OSError(ctypes.get_last_error(), f"SendInput sent {sent} of {len(events)} events "
                                               "(a higher-integrity window may have focus)")


def key_down(*names):
    _send([_key_input(VK[n.lower()], False) for n in names])


def key_up(*names):
    _send([_key_input(VK[n.lower()], True) for n in names])


def tap(*names, gap=0.03):
    """Press keys in order, release in reverse order (e.g. tap('ctrl', 's'))."""
    key_down(*names)
    time.sleep(gap)
    key_up(*reversed(names))


def type_unicode(text, delay=0.01):
    """KEYEVENTF_UNICODE (VK_PACKET) characters, like enigo's typing path."""
    for char in text:
        events = []
        data = char.encode("utf-16-le")
        for code in (int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)):
            for up in (False, True):
                event = INPUT(type=INPUT_KEYBOARD)
                event.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
                                      time=0, dwExtraInfo=0)
                events.append(event)
        _send(events)
        time.sleep(delay)


def _absolute(x, y):
    vx, vy = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw, vh = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return (round((x - vx) * 65535 / max(vw - 1, 1)), round((y - vy) * 65535 / max(vh - 1, 1)))


def _mouse(flags, x=None, y=None):
    event = INPUT(type=INPUT_MOUSE)
    dx = dy = 0
    if x is not None:
        dx, dy = _absolute(x, y)
        flags |= MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    event.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return event


def mouse_move(x, y):
    _send([_mouse(0, x, y)])


def cursor():
    point = POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def mouse_down(button="left"):
    _send([_mouse(MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN)])


def mouse_up(button="left"):
    _send([_mouse(MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP)])


def click(x, y, button="left", settle=0.05):
    mouse_move(x, y)
    time.sleep(settle)
    mouse_down(button)
    time.sleep(0.04)
    mouse_up(button)


def drag(start, end, steps=20, hold=0.15, step_delay=0.02, release=True):
    """Press at `start`, move in steps to `end`, release (unless release=False)."""
    mouse_move(*start)
    time.sleep(0.05)
    mouse_down()
    time.sleep(hold)
    for i in range(1, steps + 1):
        mouse_move(round(start[0] + (end[0] - start[0]) * i / steps), round(start[1] + (end[1] - start[1]) * i / steps))
        time.sleep(step_delay)
    if release:
        time.sleep(0.05)
        mouse_up()


def keys_down(names=("ctrl", "alt", "shift", "win")):
    """Names of keys currently down (GetAsyncKeyState) — e.g. to release stuck modifiers."""
    return [n for n in names if user32.GetAsyncKeyState(VK[n]) & 0x8000]


def release_all():
    """Release modifiers the harness may have left down (used in `finally`)."""
    if IS_WINDOWS:
        held = keys_down()
        if held:
            try:
                key_up(*held)
            except Exception:  # noqa: BLE001 - best effort during cleanup
                pass
        if user32.GetAsyncKeyState(0x01) & 0x8000:
            try:
                mouse_up()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- windows


def foreground():
    return user32.GetForegroundWindow() or 0


def window_text(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def class_name(hwnd):
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def window_pid(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_thread(hwnd):
    return user32.GetWindowThreadProcessId(hwnd, None)


def is_window(hwnd):
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def enum_windows(visible_only=True):
    """[(hwnd, pid, title, class)] of top-level windows."""
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _):
        if not visible_only or user32.IsWindowVisible(hwnd):
            found.append((hwnd, window_pid(hwnd), window_text(hwnd), class_name(hwnd)))
        return True

    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows(callback_type(each), 0)
    return found


def find_windows(title=None, contains=None, pid=None, class_=None, visible_only=True):
    return [w[0] for w in enum_windows(visible_only)
            if (title is None or w[2] == title) and (contains is None or contains.lower() in w[2].lower())
            and (pid is None or w[1] == pid) and (class_ is None or w[3] == class_)]


def wait_for(predicate, timeout, interval=0.1, sleep=time.sleep):
    """Poll until predicate() is truthy; returns its value or None at the deadline."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        sleep(interval)


def window_rect(hwnd):
    """Visible frame (DWMWA_EXTENDED_FRAME_BOUNDS), else GetWindowRect: (l, t, r, b)."""
    rect = RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.tuple()


def client_size(hwnd):
    rect = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    return rect.right - rect.left, rect.bottom - rect.top


def dpi_scale(hwnd):
    return (user32.GetDpiForWindow(hwnd) or 96) / 96


def set_window_pos(hwnd, x, y, width, height):
    if not user32.SetWindowPos(hwnd, None, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE):
        raise ctypes.WinError(ctypes.get_last_error())


def close_window(hwnd):
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def activate(hwnd, click_at=None, timeout=3.0):
    """Bring `hwnd` to the foreground. SetForegroundWindow is refused unless this process may
    take focus, so attach to the current foreground thread first; if that still fails and a
    point is given, click it (a real activation path)."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if foreground() == hwnd:
        return True
    current = foreground()
    mine = kernel32.GetCurrentThreadId()
    other = window_thread(current) if current else 0
    attached = bool(other and other != mine and user32.AttachThreadInput(mine, other, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(mine, other, False)
    if wait_for(lambda: foreground() == hwnd, 1.0, 0.05):
        return True
    if click_at:
        click(*click_at)
    return bool(wait_for(lambda: foreground() == hwnd, timeout, 0.05))


def broadcast_setting_change(area):
    result = ULONG_PTR()
    user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, area, SMTO_ABORTIFHUNG, 5000, ctypes.byref(result))


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


def monitors():
    """[{device, rect, primary, scale}] for every display (scale from GetDpiForMonitor)."""
    require_windows()
    shcore = ctypes.WinDLL("shcore")
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT),
                                       wintypes.LPARAM)

    def each(monitor, _dc, _rect, _data):
        info = MONITORINFOEXW(cbSize=ctypes.sizeof(MONITORINFOEXW))
        user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info))
        dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
        shcore.GetDpiForMonitor(ctypes.c_void_p(monitor), 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        found.append({"device": info.szDevice, "rect": info.rcMonitor.tuple(), "primary": bool(info.dwFlags & 1),
                      "scale": round(dpi_x.value / 96 * 100)})
        return True

    user32.EnumDisplayMonitors.argtypes = (ctypes.c_void_p, ctypes.c_void_p, callback_type, wintypes.LPARAM)
    user32.EnumDisplayMonitors(None, None, callback_type(each), 0)
    return found


# ---------------------------------------------------------------- processes


def processes():
    """[(pid, exe name)] of every process."""
    require_windows()
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    found = []
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            found.append((entry.th32ProcessID, entry.szExeFile))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def pids(name):
    return [pid for pid, exe in processes() if exe.lower() == name.lower()]


def kill(pid, tree=True):
    arguments = ["taskkill", "/PID", str(pid), "/F"] + (["/T"] if tree else [])
    subprocess.run(arguments, capture_output=True, creationflags=CREATE_NO_WINDOW)


# ---------------------------------------------------------------- clipboard


def _open_clipboard(attempts=40):
    for _ in range(attempts):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise OSError("Clipboard is busy")


def clipboard_text():
    """Unicode text on the clipboard, or None."""
    require_windows()
    _open_clipboard()
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def clipboard_formats():
    _open_clipboard()
    try:
        formats, current = [], 0
        while True:
            current = user32.EnumClipboardFormats(current)
            if not current:
                return formats
            formats.append(current)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text):
    """Replace the clipboard with `text` (None empties it)."""
    require_windows()
    _open_clipboard()
    try:
        user32.EmptyClipboard()
        if text is None:
            return
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------- session state


def idle_seconds():
    """Seconds since the last input event in this session (GetLastInputInfo)."""
    require_windows()
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def input_desktop():
    """Name of the desktop receiving input ('Default' when unlocked), or None when it cannot be
    opened (lock screen, UAC secure desktop)."""
    require_windows()
    desktop = user32.OpenInputDesktop(0, False, 0x0001)
    if not desktop:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not user32.GetUserObjectInformationW(desktop, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
            return None
        return buffer.value
    finally:
        user32.CloseDesktop(desktop)


class HotkeyBlocker:
    """Own a global hotkey from this process (H6: a shortcut 'another app' holds). RegisterHotKey
    binds to the calling thread, so a dedicated thread registers it and keeps it until stop()."""

    def __init__(self, spec):
        import threading
        modifiers, key = parse_combo(spec)
        self.mods = MOD_NOREPEAT
        for modifier in modifiers:
            self.mods |= HOTKEY_MODS[modifier]
        self.vk = VK[key]
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.error = None
        self._thread = threading.Thread(target=self._run, name="hotkey-blocker", daemon=True)

    def _run(self):
        if not user32.RegisterHotKey(None, 0x4D55, self.mods, self.vk):
            self.error = ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            return
        self._ready.set()
        self._stop.wait()
        user32.UnregisterHotKey(None, 0x4D55)

    def start(self):
        self._thread.start()
        self._ready.wait(5)
        if self.error:
            raise self.error
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(5)


# ---------------------------------------------------------------- registry (HKCU)


class Registry:
    """restore.RegistryValue backend for HKEY_CURRENT_USER."""

    def __init__(self):
        import winreg
        self.winreg = winreg

    def read(self, path, name):
        from restore import MISSING
        try:
            with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, path) as key:
                return self.winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return MISSING

    def write(self, path, name, value, kind):
        with self.winreg.CreateKeyEx(self.winreg.HKEY_CURRENT_USER, path, 0, self.winreg.KEY_SET_VALUE) as key:
            self.winreg.SetValueEx(key, name, 0, kind, value)

    def delete(self, path, name):
        with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, path, 0, self.winreg.KEY_SET_VALUE) as key:
            self.winreg.DeleteValue(key, name)

    def values(self, path):
        """{name: value} of a key, {} when absent."""
        try:
            with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, path) as key:
                result, index = {}, 0
                while True:
                    try:
                        name, value, _ = self.winreg.EnumValue(key, index)
                    except OSError:
                        return result
                    result[name] = value
                    index += 1
        except FileNotFoundError:
            return {}

    def subkeys(self, path):
        try:
            with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, path) as key:
                names, index = [], 0
                while True:
                    try:
                        names.append(self.winreg.EnumKey(key, index))
                    except OSError:
                        return names
                    index += 1
        except FileNotFoundError:
            return []
