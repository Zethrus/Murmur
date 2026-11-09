"""Real-input guard: stop the run as soon as a person touches the keyboard or mouse.

A dedicated thread installs low-level keyboard and mouse hooks (WH_KEYBOARD_LL / WH_MOUSE_LL)
and pumps messages for them. Events the harness injects carry LLKHF_INJECTED / LLMHF_INJECTED;
anything without that flag is a real person, which trips the guard. Input is never blocked: the
hooks only observe and pass every event on. The harness checks the guard before every injected
event (win32.set_input_check) and while it waits (Guard.sleep, 50 ms steps), so a run stops
within ~100 ms, run.py restores everything and marks the remaining checks "aborted: user input".

Windows silently removes a low-level hook whose callback is too slow (LowLevelHooksTimeout), and
the callbacks here need the GIL. A removed hook would make the guard fail open, so a watchdog
thread injects a harmless probe once a second (key-up of an unassigned virtual key, tagged in
dwExtraInfo) and trips the guard if the keyboard hook does not see it within PROBE_TIMEOUT.
Mouse moves the harness injects re-anchor the drift origin, so a pixel of real jitter after the
harness moved the cursor is compared with where the cursor really is.
"""
import ctypes
from ctypes import wintypes
import threading
import time

import win32

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
LLKHF_INJECTED, LLKHF_LOWER_IL_INJECTED = 0x10, 0x02
LLMHF_INJECTED, LLMHF_LOWER_IL_INJECTED = 0x01, 0x02
WM_QUIT = 0x0012
WM_MOUSEMOVE = 0x0200
# A real mouse that only drifts by a pixel or two (desk vibration) is not treated as a person.
MOVE_TOLERANCE = 3
# Liveness probe: key-up of an unassigned virtual key, tagged so it is recognised exactly.
PROBE_VK = 0x88
PROBE_TAG = 0x4D55524D  # "MURM"
PROBE_INTERVAL = 1.0
PROBE_TIMEOUT = 0.5


class UserInput(Exception):
    """Real (non-injected) input arrived: the run must stop."""


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", win32.ULONG_PTR)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", win32.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", win32.ULONG_PTR)]


def is_injected_key(flags):
    return bool(flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED))


def is_injected_mouse(flags):
    return bool(flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED))


class Guard:
    def __init__(self):
        self.tripped = threading.Event()
        self.reason = ""
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self._hooks = []
        self._procs = []
        self._origin = None
        self.error = None
        self.probes_seen = 0
        self._probe_seen = threading.Event()
        self._stopping = threading.Event()
        self._watchdog = None

    # ------------------------------------------------------------ hook thread
    def _trip(self, reason):
        if not self.tripped.is_set():
            self.reason = reason
            self.tripped.set()

    def _run(self):
        user32, kernel32 = win32.user32, win32.kernel32
        hook_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, hook_type, ctypes.c_void_p, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p

        def keyboard(code, wparam, lparam):
            if code >= 0:
                event = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if not is_injected_key(event.flags):
                    self._trip(f"real keyboard input (virtual key 0x{event.vkCode:02X})")
                elif event.vkCode == PROBE_VK and event.dwExtraInfo == PROBE_TAG:
                    self.probes_seen += 1
                    self._probe_seen.set()
            return user32.CallNextHookEx(None, code, wparam, lparam)

        def mouse(code, wparam, lparam):
            if code >= 0:
                event = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                reason = self.mouse_event(wparam, is_injected_mouse(event.flags), (event.pt.x, event.pt.y))
                if reason:
                    self._trip(reason)
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self._procs = [hook_type(keyboard), hook_type(mouse)]
        module = kernel32.GetModuleHandleW(None)
        for kind, proc in ((WH_KEYBOARD_LL, self._procs[0]), (WH_MOUSE_LL, self._procs[1])):
            hook = user32.SetWindowsHookExW(kind, proc, module, 0)
            if not hook:
                self.error = ctypes.WinError(ctypes.get_last_error())
                break
            self._hooks.append(hook)
        self._thread_id = kernel32.GetCurrentThreadId()
        self._ready.set()
        message = wintypes.MSG()
        while self.error is None and user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        for hook in self._hooks:
            if not user32.UnhookWindowsHookEx(hook):
                # Normally means Windows had already removed the hook (callback timeout).
                print(f"input guard: UnhookWindowsHookEx failed: {ctypes.WinError(ctypes.get_last_error())}")
        self._hooks = []

    def mouse_event(self, message, injected, point):
        """Decide one mouse event; returns a trip reason or None. Pure, unit-tested."""
        if injected:
            if message == WM_MOUSEMOVE:
                self._origin = None  # the harness moved the cursor: re-anchor on the next real move
            return None
        if message != WM_MOUSEMOVE:
            return "real mouse button or wheel input"
        if self._origin is None:
            self._origin = point
            return None
        if max(abs(point[0] - self._origin[0]), abs(point[1] - self._origin[1])) > MOVE_TOLERANCE:
            return "real mouse movement"
        return None

    def _send_probe(self):
        event = win32.INPUT(type=win32.INPUT_KEYBOARD)
        event.ki = win32.KEYBDINPUT(wVk=PROBE_VK, wScan=0, dwFlags=win32.KEYEVENTF_KEYUP, time=0,
                                    dwExtraInfo=PROBE_TAG)
        array = (win32.INPUT * 1)(event)
        return win32.user32.SendInput(1, array, ctypes.sizeof(win32.INPUT)) == 1

    def _watch(self):
        while not self._stopping.wait(PROBE_INTERVAL):
            if self.tripped.is_set():
                return
            self._probe_seen.clear()
            if not self._send_probe():
                # SendInput is refused while a secure desktop (UAC, lock screen) has input.
                self._trip("input guard probe could not be sent (desktop locked or secure desktop active)")
                return
            if not self._probe_seen.wait(PROBE_TIMEOUT) and not self._stopping.is_set():
                self._trip("input guard hook stopped receiving input (Windows removed it); stopping for safety")
                return

    # ------------------------------------------------------------ public
    def start(self):
        win32.require_windows()
        self._thread = threading.Thread(target=self._run, name="input-guard", daemon=True)
        self._thread.start()
        if not self._ready.wait(5) or self.error:
            raise RuntimeError(f"Could not install the input guard hooks: {self.error}")
        win32.set_input_check(self.check)
        self._watchdog = threading.Thread(target=self._watch, name="input-guard-watchdog", daemon=True)
        self._watchdog.start()
        return self

    def stop(self):
        win32.set_input_check(None)
        self._stopping.set()
        if self._watchdog:
            self._watchdog.join(3)
        if self._thread and self._thread_id:
            win32.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(3)

    def check(self):
        if self.tripped.is_set():
            raise UserInput(self.reason)

    def sleep(self, seconds):
        """time.sleep that stops within 50 ms of real input."""
        deadline = time.monotonic() + seconds
        while True:
            self.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.tripped.wait(min(0.05, remaining))


class NoGuard:
    """Stand-in for manual.py (a person drives) and unit tests."""
    tripped = threading.Event()
    reason = ""

    def check(self):
        pass

    def sleep(self, seconds):
        time.sleep(seconds)

    def stop(self):
        pass
