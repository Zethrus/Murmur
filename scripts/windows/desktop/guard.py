"""Real-input guard: stop the run as soon as a person touches the keyboard or mouse.

A separate observer process installs low-level keyboard and mouse hooks (WH_KEYBOARD_LL /
WH_MOUSE_LL) on a dedicated message-pump thread. This keeps the Python callbacks independent of
the harness's UI Automation/COM work and GIL. Events the harness injects carry LLKHF_INJECTED / LLMHF_INJECTED;
anything without that flag is a real person, which trips the guard. Input is never blocked: the
hooks only observe and pass every event on. The harness checks the guard before every injected
event (win32.set_input_check) and while it waits (Guard.sleep, 50 ms steps), so a run stops
within ~100 ms, run.py restores everything and marks the remaining checks aborted.

Windows silently removes a low-level hook whose callback is too slow (LowLevelHooksTimeout), so
the observer injects a harmless probe before reporting ready, then once a second (key-up of an
reserved virtual key, tagged in dwExtraInfo). A missed probe or lost observer trips the parent
guard fail closed.
Mouse moves the harness injects re-anchor the drift origin, so a pixel of real jitter after the
harness moved the cursor is compared with where the cursor really is.
"""
import ctypes
from ctypes import wintypes
import multiprocessing
import os
import threading
import time

import win32

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
LLKHF_INJECTED, LLKHF_LOWER_IL_INJECTED = 0x10, 0x02
LLMHF_INJECTED, LLMHF_LOWER_IL_INJECTED = 0x01, 0x02
WM_QUIT = 0x0012
WM_USER = 0x0400
WM_GUARD_HEARTBEAT = WM_USER + 1
WM_MOUSEMOVE = 0x0200
# A real mouse that only drifts by a pixel or two (desk vibration) is not treated as a person.
MOVE_TOLERANCE = 3
# Liveness probe: key-up of a reserved virtual key, tagged so it is recognised exactly.
PROBE_VK = 0x88
PROBE_TAG = 0x4D55524D  # "MURM"
PROBE_INTERVAL = 1.0
PROBE_TIMEOUT = 0.5


class UserInput(Exception):
    """Real (non-injected) input arrived: the run must stop."""


class GuardUnavailable(UserInput):
    """Input observation is no longer proven live: the run must stop."""


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


class _HookObserver:
    def __init__(self, diagnostic=False):
        self.tripped = threading.Event()
        self.reason = ""
        self.unavailable = False
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self._hooks = []
        self._procs = []
        self._origin = None
        self.error = None
        self.probes_seen = 0
        self.last_probe_latency_ms = None
        self.max_probe_latency_ms = 0
        self.last_probe_send_ms = None
        self.unhook_errors = []
        self._probe_sequence = 0
        self._expected_probe_tag = None
        self._probe_started = 0
        self._probe_seen = threading.Event()
        self._first_probe_done = threading.Event()
        self._stopping = threading.Event()
        self._watchdog = None
        self._diagnostic = diagnostic
        self._probe_events = []
        self._keyboard_callbacks = 0
        self._injected_key_events = 0
        self._mouse_callbacks = 0
        self._pump_started = False
        self._pump_result = None
        self._pump_error = None
        self._pump_exited_before_stop = False
        self._installed_hooks = 0
        self._last_send_result = None
        self._last_send_error = None
        self._heartbeat_posted = 0
        self._heartbeat_received = 0
        self._heartbeat_post_error = None
        self._mouse_probe_tag = None
        self._mouse_probe_seen = 0
        self._mouse_probe_send_result = None
        self._mouse_probe_send_error = None
        self._desktop_context = None

    def _record_injected_key(self, message, event):
        """Keep only our probe candidates; never retain a person's keystrokes."""
        if not self._diagnostic or not is_injected_key(event.flags):
            return
        self._injected_key_events += 1
        if event.vkCode != PROBE_VK and event.dwExtraInfo & 0xFFFFFFFF != PROBE_TAG:
            return
        if len(self._probe_events) == 8:
            self._probe_events.pop(0)
        self._probe_events.append({"message": int(message), "vk": int(event.vkCode),
                                   "scan": int(event.scanCode), "flags": int(event.flags),
                                   "extra_info": f"0x{event.dwExtraInfo:016x}",
                                   "expected_tag": f"0x{self._expected_probe_tag:016x}"
                                   if self._expected_probe_tag is not None else None,
                                   "matches_expected": event.vkCode == PROBE_VK
                                   and event.dwExtraInfo == self._expected_probe_tag})

    # ------------------------------------------------------------ hook thread
    def _trip(self, reason, unavailable=False):
        if not self.tripped.is_set():
            self.reason = reason
            self.unavailable = unavailable
            self.tripped.set()
            self._first_probe_done.set()

    def _run(self):
        user32, kernel32 = win32.user32, win32.kernel32
        hook_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, hook_type, ctypes.c_void_p, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        user32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                        wintypes.UINT, wintypes.UINT, wintypes.UINT)
        user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT,
                                              wintypes.WPARAM, wintypes.LPARAM)
        user32.PostThreadMessageW.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p

        def keyboard(code, wparam, lparam):
            if code >= 0:
                self._keyboard_callbacks += 1
                event = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if not is_injected_key(event.flags):
                    self._trip(f"real keyboard input (virtual key 0x{event.vkCode:02X})")
                else:
                    self._record_injected_key(wparam, event)
                    if event.vkCode == PROBE_VK and event.dwExtraInfo == self._expected_probe_tag:
                        self.probes_seen += 1
                        self.last_probe_latency_ms = round((time.monotonic() - self._probe_started) * 1000)
                        self.max_probe_latency_ms = max(self.max_probe_latency_ms, self.last_probe_latency_ms)
                        self._probe_seen.set()
            return user32.CallNextHookEx(None, code, wparam, lparam)

        def mouse(code, wparam, lparam):
            if code >= 0:
                self._mouse_callbacks += 1
                event = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                injected = is_injected_mouse(event.flags)
                if (self._diagnostic and injected and wparam == WM_MOUSEMOVE
                        and event.dwExtraInfo == self._mouse_probe_tag):
                    self._mouse_probe_seen += 1
                reason = self.mouse_event(wparam, injected, (event.pt.x, event.pt.y))
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
            self._installed_hooks += 1
        self._thread_id = kernel32.GetCurrentThreadId()
        if self._diagnostic:
            self._desktop_context = win32.desktop_context(self._thread_id)
        message = wintypes.MSG()
        # PostThreadMessage requires a queue; create it before the ready signal.
        user32.PeekMessageW(ctypes.byref(message), None, WM_USER, WM_USER, 0)
        self._ready.set()
        self._pump_started = self.error is None
        while self.error is None:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result <= 0:
                self._pump_result = result
                self._pump_exited_before_stop = not self._stopping.is_set()
                if result < 0:
                    self._pump_error = ctypes.get_last_error()
                break
            if message.message == WM_GUARD_HEARTBEAT:
                self._heartbeat_received += 1
                continue
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        if not self._stopping.is_set() and self.error is None:
            self._trip("input guard hook message pump stopped", unavailable=True)
        for hook in self._hooks:
            if not user32.UnhookWindowsHookEx(hook):
                # Normally means Windows had already removed the hook (callback timeout).
                error = str(ctypes.WinError(ctypes.get_last_error()))
                self.unhook_errors.append(error)
                print(f"input guard: UnhookWindowsHookEx failed: {error}")
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

    def _send_probe(self, tag):
        event = win32.INPUT(type=win32.INPUT_KEYBOARD)
        event.ki = win32.KEYBDINPUT(wVk=PROBE_VK, wScan=0, dwFlags=win32.KEYEVENTF_KEYUP, time=0,
                                    dwExtraInfo=tag)
        array = (win32.INPUT * 1)(event)
        sent = win32.user32.SendInput(1, array, ctypes.sizeof(win32.INPUT))
        if self._diagnostic:
            self._last_send_result = sent
            self._last_send_error = ctypes.get_last_error() if sent != 1 else None
        return sent == 1

    def _post_heartbeat(self):
        posted = win32.user32.PostThreadMessageW(self._thread_id, WM_GUARD_HEARTBEAT, 0, 0)
        if posted:
            self._heartbeat_posted += 1
        else:
            self._heartbeat_post_error = ctypes.get_last_error()

    def _send_mouse_probe(self, tag):
        # Diagnostic only: a zero-displacement move changes no button or cursor position.
        event = win32.INPUT(type=win32.INPUT_MOUSE)
        event.mi = win32.MOUSEINPUT(dx=0, dy=0, mouseData=0,
                                    dwFlags=win32.MOUSEEVENTF_MOVE | win32.MOUSEEVENTF_MOVE_NOCOALESCE,
                                    time=0, dwExtraInfo=tag)
        array = (win32.INPUT * 1)(event)
        sent = win32.user32.SendInput(1, array, ctypes.sizeof(win32.INPUT))
        self._mouse_probe_send_result = sent
        self._mouse_probe_send_error = ctypes.get_last_error() if sent != 1 else None

    def _watch(self):
        while not self._stopping.is_set():
            if self.tripped.is_set():
                return
            self._probe_seen.clear()
            self._probe_sequence += 1
            self._expected_probe_tag = PROBE_TAG | (self._probe_sequence << 32)
            if self._diagnostic:
                self._post_heartbeat()
                if self._probe_sequence == 1:
                    self._mouse_probe_tag = self._expected_probe_tag
                    self._send_mouse_probe(self._mouse_probe_tag)
                if self.tripped.is_set():
                    return
            self._probe_started = time.monotonic()
            if not self._send_probe(self._expected_probe_tag):
                self._trip("input guard probe could not be sent (desktop unavailable or injection blocked)",
                           unavailable=True)
                return
            self.last_probe_send_ms = round((time.monotonic() - self._probe_started) * 1000)
            if not self._probe_seen.wait(PROBE_TIMEOUT) and not self._stopping.is_set():
                self._trip(f"input guard probe not observed within {PROBE_TIMEOUT:g} s "
                           f"(SendInput {self.last_probe_send_ms} ms, {self.probes_seen} earlier probes seen); "
                           "hook may be delayed or removed; stopping for safety", unavailable=True)
                return
            self._first_probe_done.set()
            if self._stopping.wait(PROBE_INTERVAL):
                return

    # ------------------------------------------------------------ public
    def start(self):
        win32.require_windows()
        self._thread = threading.Thread(target=self._run, name="input-guard", daemon=True)
        self._thread.start()
        if not self._ready.wait(5) or self.error:
            raise RuntimeError(f"Could not install the input guard hooks: {self.error}")
        self._watchdog = threading.Thread(target=self._watch, name="input-guard-watchdog", daemon=True)
        self._watchdog.start()
        if not self._first_probe_done.wait(5) or self.tripped.is_set():
            raise RuntimeError(f"Could not verify the input guard hooks: {self.reason or 'first probe not completed'}")
        return self

    def stop(self):
        self._stopping.set()
        if self._watchdog:
            self._watchdog.join(3)
        if self._thread and self._thread_id:
            win32.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(3)

    def check(self):
        if self.tripped.is_set():
            raise (GuardUnavailable if self.unavailable else UserInput)(self.reason)

    def diagnostics(self):
        result = {"probes_seen": self.probes_seen, "last_probe_latency_ms": self.last_probe_latency_ms,
                  "max_probe_latency_ms": self.max_probe_latency_ms, "last_probe_send_ms": self.last_probe_send_ms,
                  "unhook_errors": self.unhook_errors}
        if self._diagnostic:
            result["hook_state"] = {"process_id": os.getpid(), "thread_id": self._thread_id,
                                    "installed_hooks": self._installed_hooks, "pump_started": self._pump_started,
                                    "pump_result": self._pump_result, "pump_error": self._pump_error,
                                    "pump_exited_before_stop": self._pump_exited_before_stop,
                                    "heartbeat_posted": self._heartbeat_posted,
                                    "heartbeat_received": self._heartbeat_received,
                                    "heartbeat_post_error": self._heartbeat_post_error,
                                    "last_send_result": self._last_send_result,
                                    "last_send_error": self._last_send_error,
                                    "keyboard_callbacks": self._keyboard_callbacks,
                                    "injected_key_events": self._injected_key_events,
                                    "mouse_callbacks": self._mouse_callbacks,
                                    "zero_move_probe_send_result": self._mouse_probe_send_result,
                                    "zero_move_probe_send_error": self._mouse_probe_send_error,
                                    "zero_move_probe_seen": self._mouse_probe_seen,
                                    "input_desktop": win32.input_desktop(),
                                    "desktop_context": self._desktop_context,
                                    "pointer_bits": ctypes.sizeof(ctypes.c_void_p) * 8,
                                    "input_size": ctypes.sizeof(win32.INPUT),
                                    "input_union_offset": win32.INPUT.u.offset,
                                    "key_input_size": ctypes.sizeof(win32.KEYBDINPUT),
                                    "key_input_extra_offset": win32.KEYBDINPUT.dwExtraInfo.offset,
                                    "key_hook_size": ctypes.sizeof(KBDLLHOOKSTRUCT),
                                    "key_hook_extra_offset": KBDLLHOOKSTRUCT.dwExtraInfo.offset}
            result["expected_probe"] = {"vk": PROBE_VK, "sequence": self._probe_sequence,
                                        "tag": f"0x{self._expected_probe_tag:016x}"
                                        if self._expected_probe_tag is not None else None}
            result["probe_events"] = list(self._probe_events)
        return result

    def sleep(self, seconds):
        """time.sleep that stops within 50 ms of real input."""
        deadline = time.monotonic() + seconds
        while True:
            self.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.tripped.wait(min(0.05, remaining))


def _guard_worker(connection, diagnostic=False):
    """Own the hooks and their Python callbacks in a process without UIA/COM work."""
    observer = _HookObserver(diagnostic=diagnostic) if diagnostic else _HookObserver()
    try:
        observer.start()
        connection.send(("ready", observer.diagnostics()))
        last_count = 0
        while not observer.tripped.is_set():
            if connection.poll(0.025) and connection.recv() == "stop":
                return
            if observer.probes_seen != last_count:
                last_count = observer.probes_seen
                connection.send(("diagnostics", observer.diagnostics()))
        connection.send(("unavailable" if observer.unavailable else "user_input", observer.reason))
    except Exception as error:  # noqa: BLE001 - report observer failure to the harness
        try:
            if observer.tripped.is_set():
                connection.send(("unavailable" if observer.unavailable else "user_input", observer.reason))
            else:
                connection.send(("error", f"{type(error).__name__}: {error}"))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        observer.stop()
        try:
            connection.send(("diagnostics", observer.diagnostics()))
        except (BrokenPipeError, EOFError, OSError):
            pass
        connection.close()


class Guard:
    """Parent-side fail-closed view of the isolated input observer."""

    def __init__(self, diagnostic=False):
        self.tripped = threading.Event()
        self.reason = ""
        self.unavailable = False
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._process = None
        self._connection = None
        self._listener = None
        self._receive_lock = threading.Lock()
        self._diagnostic = diagnostic
        self._diagnostics = {"probes_seen": 0, "last_probe_latency_ms": None,
                             "max_probe_latency_ms": 0, "last_probe_send_ms": None,
                             "unhook_errors": []}
        self._started_at = None
        self._timeline = []

    def _trip(self, reason, unavailable=False):
        if not self.tripped.is_set():
            self.reason = reason
            self.unavailable = unavailable
            self.tripped.set()

    def _receive(self):
        """Drain pending events under one lock; check() may race the listener."""
        with self._receive_lock:
            try:
                while self._connection.poll():
                    kind, value = self._connection.recv()
                    if kind == "ready":
                        self._diagnostics = value
                        self._ready.set()
                    elif kind == "diagnostics":
                        self._diagnostics = value
                    elif kind == "user_input":
                        self._trip(value)
                        self._ready.set()
                    else:
                        self._trip(value, unavailable=True)
                        self._ready.set()
            except (EOFError, OSError):
                if not self._stopping.is_set():
                    self._trip("input guard observer connection closed", unavailable=True)
                self._ready.set()

    def _listen(self):
        while not self._stopping.is_set() and not self.tripped.is_set():
            self._receive()
            if self._process and not self._process.is_alive():
                self._receive()
                if not self.tripped.is_set() and not self._stopping.is_set():
                    self._trip("input guard observer stopped", unavailable=True)
                self._ready.set()
                return
            self.tripped.wait(0.025)

    def start(self):
        win32.require_windows()
        self._started_at = time.monotonic()
        context = multiprocessing.get_context("spawn")
        self._connection, worker_connection = context.Pipe()
        worker_args = (worker_connection, True) if self._diagnostic else (worker_connection,)
        self._process = context.Process(target=_guard_worker, args=worker_args,
                                        name="input-guard-observer", daemon=True)
        try:
            self._process.start()
        finally:
            worker_connection.close()
        self._listener = threading.Thread(target=self._listen, name="input-guard-listener", daemon=True)
        self._listener.start()
        if not self._ready.wait(5):
            self.stop()
            raise RuntimeError("Could not install the input guard hooks: observer not ready")
        if self.tripped.is_set():
            try:
                self.check()
            finally:
                self.stop()
        win32.set_input_check(self.check)
        return self

    def stop(self):
        win32.set_input_check(None)
        self._stopping.set()
        if self._connection and self._process and self._process.is_alive():
            try:
                self._connection.send("stop")
            except (BrokenPipeError, EOFError, OSError):
                pass
            self._process.join(3)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(3)
        if self._listener:
            self._listener.join(3)
        if self._connection:
            self._receive()
            self._connection.close()
            self._connection = None

    def check(self):
        if self._connection:
            self._receive()
        if self._process and not self._process.is_alive() and not self._stopping.is_set():
            self._trip("input guard observer stopped", unavailable=True)
        if self.tripped.is_set():
            raise (GuardUnavailable if self.unavailable else UserInput)(self.reason)

    def diagnostics(self):
        result = dict(self._diagnostics)
        if self._diagnostic:
            result["timeline"] = list(self._timeline)
        return result

    def mark(self, stage):
        """Correlate a suite action with the observer's last reported probe and hook state."""
        if not self._diagnostic:
            return
        if self._connection:
            self._receive()
        hook = self._diagnostics.get("hook_state", {})
        self._timeline.append({
            "stage": stage,
            "elapsed_s": round(time.monotonic() - self._started_at, 3) if self._started_at else None,
            "probes_seen": self._diagnostics.get("probes_seen", 0),
            "keyboard_callbacks": hook.get("keyboard_callbacks"),
            "heartbeat_received": hook.get("heartbeat_received"),
            "observer_alive": self._process.is_alive() if self._process else False,
            "tripped": self.tripped.is_set(),
        })

    def sleep(self, seconds):
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

    def mark(self, stage):
        pass

    def sleep(self, seconds):
        time.sleep(seconds)

    def stop(self):
        pass
