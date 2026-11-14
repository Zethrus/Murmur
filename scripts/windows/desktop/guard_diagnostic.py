#!/usr/bin/env python3
"""Bounded native input-guard probe; requires only Python's standard library.

Run only on the owner-controlled, idle, unlocked desktop. The hook records counts for all
callbacks, but event fields only for injected probe candidates; no real key fields are written.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import os
import threading
import time

import guard
import run
import win32

WS_EX_TOOLWINDOW = 0x80
WS_POPUP, WS_VISIBLE = 0x80000000, 0x10000000
PM_REMOVE = 1


class _OwnedProbeWindow:
    """A small, short-lived input target; its thread consumes key messages without translation."""

    def __init__(self):
        self.hwnd = None
        self.status = "pending"
        self.restoration = "not_started"
        self.received_events = []
        self._previous = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def open(self):
        self._thread = threading.Thread(target=self._run, name="guard-probe-window", daemon=True)
        self._thread.start()
        ready = self._ready.wait(3)
        if not ready:
            self.status = "window_thread_timeout"
        if not ready or self.status != "ready":
            self.close()
            return False
        return True

    def _run(self):
        user32 = win32.user32
        try:
            self._previous = user32.GetForegroundWindow()
            # EDIT is a built-in keyboard-capable class. Do not translate the packet to text.
            self.hwnd = user32.CreateWindowExW(WS_EX_TOOLWINDOW, "EDIT", "", WS_POPUP | WS_VISIBLE,
                                                0, 0, 80, 24, None, None,
                                                win32.kernel32.GetModuleHandleW(None), None)
            if not self.hwnd:
                self.status = f"create_failed:{ctypes.get_last_error()}"
            elif not user32.SetForegroundWindow(self.hwnd):
                self.status = "focus_denied"
            else:
                user32.SetFocus(self.hwnd)
                if user32.GetForegroundWindow() != self.hwnd or user32.GetFocus() != self.hwnd:
                    self.status = "focus_unverified"
                else:
                    self.status = "ready"
            self._ready.set()
            if self.status == "ready":
                while not self._stop.wait(0.01):
                    self._drain_messages(user32)
                self._drain_messages(user32)
        except Exception as error:  # noqa: BLE001 - return a report even for native setup errors
            self.status = f"window_error:{type(error).__name__}"
        finally:
            self._ready.set()
            if self.hwnd:
                try:
                    if user32.GetForegroundWindow() != self.hwnd:
                        self.restoration = "skipped_focus_changed"
                    elif self._previous and user32.IsWindow(self._previous):
                        self.restoration = ("restored" if user32.SetForegroundWindow(self._previous)
                                            else "restore_denied")
                    else:
                        self.restoration = "no_previous_window"
                except Exception as error:  # noqa: BLE001 - still destroy our window
                    self.restoration = f"restore_error:{type(error).__name__}"
                finally:
                    if not user32.DestroyWindow(self.hwnd):
                        self.restoration += f";destroy_failed:{ctypes.get_last_error()}"

    def _drain_messages(self, user32):
        message = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(message), self.hwnd, 0, 0, PM_REMOVE):
            extra = ctypes.c_size_t(user32.GetMessageExtraInfo()).value
            if (extra & 0xFFFFFFFF == guard.PROBE_TAG and
                    message.message in (guard.WM_KEYDOWN, guard.WM_KEYUP) and
                    message.wParam in (guard.VK_PACKET, guard.PROBE_VK)):
                if len(self.received_events) == 8:
                    self.received_events.pop(0)
                self.received_events.append({"message": int(message.message),
                                             "vk": int(message.wParam),
                                             "extra_info": f"0x{extra:016x}"})
            user32.DispatchMessageW(ctypes.byref(message))

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3)
            if self._thread.is_alive():
                self.restoration = "window_thread_stop_timeout"

    def diagnostics(self):
        return {"status": self.status, "restoration": self.restoration,
                "received_probe_events": list(self.received_events)}


def diagnose(seconds):
    win32.require_windows()
    problems = run.preflight_desktop(win32)
    if problems:
        return {"status": "preflight_refused", "problems": problems}

    parent_desktop_context = win32.desktop_context()
    window = _OwnedProbeWindow()
    if not window.open():
        return {"status": "probe_window_unavailable", "parent_desktop_context": parent_desktop_context,
                "probe_window": window.diagnostics()}
    problems = run.preflight_desktop(win32)
    if problems:
        window.close()
        return {"status": "preflight_refused", "problems": problems,
                "probe_window": window.diagnostics()}
    observer = guard.Guard(diagnostic=True, owned_probe_target=(window.hwnd, os.getpid()))
    started = time.monotonic()
    status, reason = "ok", ""
    try:
        observer.start()
        observer.sleep(seconds)
    except guard.GuardUnavailable as error:
        status, reason = "guard_unavailable", str(error)
    except guard.UserInput:
        # The normal guard reason includes the virtual key; diagnostic output must not.
        status, reason = "real_input", "real input detected; stopped"
    except Exception as error:  # noqa: BLE001 - preserve diagnostic output on setup failure
        status, reason = "error", type(error).__name__
    finally:
        try:
            observer.stop()
        finally:
            window.close()
    return {"status": status, "reason": reason, "elapsed_seconds": round(time.monotonic() - started, 2),
            "parent_desktop_context": parent_desktop_context,
            "probe_window": window.diagnostics(),
            "input_guard": observer.diagnostics()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 60:
        parser.error("--seconds must be 1-60")
    result = diagnose(args.seconds)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Input guard diagnostic: {result['status']}; report: {args.out}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
