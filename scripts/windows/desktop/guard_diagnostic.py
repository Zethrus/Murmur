#!/usr/bin/env python3
"""Bounded native input-guard probe run with the desktop harness Python environment.

Its imports use only the standard library and local harness modules. Run only on the
owner-controlled, idle, unlocked desktop. The hook records counts for all callbacks,
but event fields only for injected probe candidates; no real key fields are written.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import os
import sys
import threading
import time

import guard
import run
import win32

WS_EX_TOOLWINDOW, WS_EX_TOPMOST = 0x80, 0x08
WS_OVERLAPPEDWINDOW, WS_VISIBLE, ES_READONLY = 0x00CF0000, 0x10000000, 0x0800
PM_REMOVE = 1
SWP_SHOW_NO_ACTIVATE = 0x40 | 0x10 | 0x04 | 0x02 | 0x01
FOCUS_WAIT_SECONDS = 180
IDLE_WAIT_SECONDS = 65
IDLE_SECONDS = 60
TEARDOWN_DEADLINE_SECONDS = 12


class _OwnedProbeWindow:
    """A small, short-lived input target; its thread consumes key messages without translation."""

    def __init__(self):
        self.hwnd = None
        self.status = "pending"
        self.initial_focus = "not_attempted"
        self.restoration = "not_started"
        self.received_events = []
        self._previous = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._clear_focus = threading.Event()
        self._clear_done = threading.Event()
        self._restore_on_close = True
        self._handoff_window = False
        self._destroyed = False

    def open(self, activation_gate=None):
        self._handoff_window = activation_gate is not None
        self._thread = threading.Thread(target=self._run, args=(activation_gate,),
                                        name="guard-probe-window", daemon=True)
        self._thread.start()
        ready = self._ready.wait(3)
        if not ready:
            self.status = "window_thread_timeout"
        if not ready or self.status.startswith(("create_failed", "window_error", "handoff_unsafe",
                                                 "show_failed")):
            self.close()
            return False
        return True

    def _run(self, activation_gate=None):
        user32 = win32.user32
        try:
            self._previous = user32.GetForegroundWindow()
            if activation_gate:
                self._handoff_window = True
                self._restore_on_close = False
            if activation_gate and not activation_gate(None):
                self.status = "handoff_unsafe"
                return
            # EDIT is keyboard-capable. The caption and read-only content tell the owner what to do.
            instructions = ("Murmur guard diagnostic: If this window is not focused, click it; "
                            "then leave the mouse and keyboard untouched for 60 seconds")
            self.hwnd = user32.CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_TOPMOST, "EDIT", instructions,
                                                WS_OVERLAPPEDWINDOW |
                                                (0 if activation_gate else WS_VISIBLE) | ES_READONLY,
                                                80, 80, 600, 140, None, None,
                                                win32.kernel32.GetModuleHandleW(None), None)
            if not self.hwnd:
                self.status = f"create_failed:{ctypes.get_last_error()}"
            else:
                if activation_gate and not activation_gate(self.hwnd):
                    self.status = "handoff_unsafe"
                else:
                    if activation_gate and not user32.SetWindowPos(
                            self.hwnd, None, 0, 0, 0, 0, SWP_SHOW_NO_ACTIVATE):
                        self.status = f"show_failed:{ctypes.get_last_error()}"
                    elif activation_gate and not activation_gate(self.hwnd):
                        self.status = "handoff_unsafe"
                    else:
                        self.initial_focus = ("granted" if user32.SetForegroundWindow(self.hwnd)
                                              else "denied")
                        self.status = "waiting_for_focus"
            self._ready.set()
            if self.hwnd and not self.status.startswith(("handoff_unsafe", "show_failed")):
                while not self._stop.wait(0.01):
                    self._drain_messages(user32)
                    if self._clear_focus.is_set() and not self._clear_done.is_set():
                        if user32.GetForegroundWindow() == self.hwnd:
                            user32.SetFocus(None)  # only this owning GUI thread may clear its focus
                            self.status = ("focus_cleared" if not user32.GetFocus()
                                           else "clear_focus_failed")
                        else:
                            self.status = "focus_lost"
                        self._clear_done.set()
                    if self._clear_focus.is_set():
                        continue
                    if self.status == "focus_lost":
                        continue
                    if user32.GetForegroundWindow() == self.hwnd:
                        if user32.GetFocus() != self.hwnd:
                            user32.SetFocus(self.hwnd)
                        if user32.GetFocus() == self.hwnd:
                            self.status = "ready"
                        elif self.status == "ready":
                            self.status = "focus_lost"
                    elif self.status == "ready":
                        self.status = "focus_lost"
                self._drain_messages(user32)
        except Exception as error:  # noqa: BLE001 - return a report even for native setup errors
            self.status = f"window_error:{type(error).__name__}"
        finally:
            self._ready.set()
            if self.hwnd:
                try:
                    if user32.GetForegroundWindow() != self.hwnd:
                        self.restoration = "skipped_focus_changed"
                    elif not self._restore_on_close:
                        self.restoration = "skipped_teardown_stage"
                    elif self._previous and user32.IsWindow(self._previous):
                        self.restoration = ("restored" if user32.SetForegroundWindow(self._previous)
                                            else "restore_denied")
                    else:
                        self.restoration = "no_previous_window"
                except Exception as error:  # noqa: BLE001 - still destroy our window
                    self.restoration = f"restore_error:{type(error).__name__}"
                finally:
                    self._destroyed = bool(user32.DestroyWindow(self.hwnd))
                    if not self._destroyed:
                        self.restoration += f";destroy_failed:{ctypes.get_last_error()}"

    def _drain_messages(self, user32):
        message = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(message), self.hwnd, 0, 0, PM_REMOVE):
            extra = ctypes.c_size_t(user32.GetMessageExtraInfo()).value
            if (extra & 0xFFFFFFFF == guard.PROBE_TAG and
                    message.message in (guard.WM_KEYDOWN, guard.WM_KEYUP,
                                        guard.WM_SYSKEYDOWN, guard.WM_SYSKEYUP) and
                    message.wParam in (guard.VK_PACKET, guard.PROBE_VK)):
                if len(self.received_events) == 16:
                    self.received_events.pop(0)
                self.received_events.append({"message": int(message.message),
                                             "vk": int(message.wParam),
                                             "extra_info": f"0x{extra:016x}"})
            user32.DispatchMessageW(ctypes.byref(message))

    def clear_focus(self):
        self._clear_focus.set()
        return self._clear_done.wait(2) and self.status == "focus_cleared"

    def close(self, restore=True):
        self._restore_on_close = restore and not self._handoff_window
        self._stop.set()
        if self._thread:
            self._thread.join(3)
            if self._thread.is_alive():
                self.restoration = "window_thread_stop_timeout"
        return bool(self._thread and not self._thread.is_alive() and self._destroyed and
                    not win32.user32.IsWindow(self.hwnd))

    def diagnostics(self):
        return {"status": self.status, "initial_focus": self.initial_focus,
                "restoration": self.restoration,
                "received_probe_events": list(self.received_events)}

    def focused(self):
        return self.status == "ready" and win32.user32.GetForegroundWindow() == self.hwnd


def _wait_for_safe_desktop(window):
    """Allow one owner click, then require 60 seconds without input while focus stays owned."""
    focus_deadline = time.monotonic() + FOCUS_WAIT_SECONDS
    while not window.focused():
        if window.status.startswith(("window_error", "create_failed")):
            return "probe_window_unavailable", []
        if window.status == "focus_lost":
            return "focus_lost", []
        problems = run.preflight_desktop(win32, idle_needed=0)
        if problems:
            return "preflight_refused", problems
        if time.monotonic() >= focus_deadline:
            window.status = "focus_timeout"
            return "probe_window_unavailable", []
        time.sleep(0.25)

    idle_deadline = time.monotonic() + IDLE_WAIT_SECONDS
    while True:
        if not window.focused():
            return "focus_lost", []
        problems = run.preflight_desktop(win32, idle_needed=0)
        if problems:
            return "preflight_refused", problems
        if win32.idle_seconds() >= IDLE_SECONDS:
            break
        if time.monotonic() >= idle_deadline:
            return "idle_timeout", []
        time.sleep(0.25)
    problems = run.preflight_desktop(win32)
    if problems:
        return "preflight_refused", problems
    return ("ready", []) if window.focused() else ("focus_lost", [])


def _safe_handoff_foreground(context, owned_hwnd=None):
    """Only a null foreground or this verified B window may precede B activation."""
    if context.get("input_desktop") != "Default":
        return False
    if (context.get("hwnd") == 0 and context.get("process_id") == 0 and
            context.get("gui_status") == "no_foreground"):
        return True
    return bool(owned_hwnd and
                (win32.owned_foreground(context, owned_hwnd, os.getpid(), "window") or
                 win32.owned_foreground(context, owned_hwnd, os.getpid(), "none")))


def diagnose(seconds):
    win32.require_windows()
    problems = run.preflight_desktop(win32, idle_needed=0)
    if problems:
        return {"status": "preflight_refused", "problems": problems}

    parent_desktop_context = win32.desktop_context()
    window = _OwnedProbeWindow()
    if not window.open():
        return {"status": "probe_window_unavailable", "parent_desktop_context": parent_desktop_context,
                "probe_window": window.diagnostics()}
    print("If the Murmur guard diagnostic window is not focused, click it; then leave input untouched for 60 s.",
          flush=True)
    state, problems = _wait_for_safe_desktop(window)
    if state != "ready":
        window.close()
        return {"status": state, "problems": problems,
                "probe_window": window.diagnostics()}
    observer = guard.Guard(diagnostic=True, owned_probe_target=(window.hwnd, os.getpid()))
    started = time.monotonic()
    status, reason = "ok", ""
    try:
        if not window.focused():
            status = "focus_lost"
        else:
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


def diagnose_teardown():
    """One A/B experiment; a missed hook remains latched through the B probe."""
    win32.require_windows()
    problems = run.preflight_desktop(win32, idle_needed=0)
    if problems:
        return {"status": "preflight_refused", "problems": problems}
    result = {"status": "inconclusive", "reason": "", "stages": [],
              "parent_desktop_context": win32.desktop_context()}
    a, b = _OwnedProbeWindow(), None
    observer = None
    if not a.open():
        return {**result, "status": "probe_window_unavailable", "window_a": a.diagnostics()}
    try:
        print("Focus the owned diagnostic window if needed; then leave input untouched for 60 s.", flush=True)
        state, problems = _wait_for_safe_desktop(a)
        if state != "ready":
            result.update(status=state, problems=problems)
            return result
        if not a.focused():
            result["status"] = "focus_lost"
            return result
        observer = guard.TeardownDiagnosticObserver().start()
        started = time.monotonic()
        deadline = started + TEARDOWN_DEADLINE_SECONDS
        handoff_gate = None
        handoff_input_tick = [None]
        handoff_reason = [""]

        def snapshot(stage):
            hook = observer.diagnostics()["hook_state"]
            return {"stage": stage, "context": win32.foreground_context(),
                    "hook_desktop": win32.desktop_context(observer._thread_id),
                    "hook_thread_id": observer._thread_id,
                    "keyboard_callbacks": hook["keyboard_callbacks"],
                    "heartbeat_received": hook["heartbeat_received"],
                    "elapsed_s": round(time.monotonic() - started, 3)}

        def probe(window, stage, expected_focus, kind, recovery=False):
            if time.monotonic() >= deadline:
                return "deadline"
            if window is b and handoff_gate and not handoff_gate(b.hwnd):
                return handoff_reason[0]
            entry = observer.probe_once(window.hwnd, os.getpid(), kind, expected_focus,
                                        recovery=recovery)
            entry["stage"] = stage
            result["stages"].append(entry)
            if window is b:
                handoff_input_tick[0] = win32.last_input_tick()
            return entry["status"]

        def probe_pair(window, stage, expected_focus):
            for kind in ("unicode_up", "reserved_vk_up"):
                state = probe(window, stage, expected_focus, kind)
                if state != "observed":
                    return state
            return "observed"

        state = probe_pair(a, "a_focused", "window")
        if state == "observed":
            if not a.clear_focus():
                state = "focus_lost"
            else:
                result["stages"].append(snapshot("a_set_focus_null"))
                state = probe_pair(a, "a_focus_null", "none")

        if state in ("observed", "missed"):
            handoff_input_tick[0] = win32.last_input_tick()
            result["stages"].append(snapshot("a_before_teardown"))
            teardown_complete = a.close(restore=False)
            result["stages"].append({**snapshot("a_after_teardown"), "window": a.diagnostics()})
            if not teardown_complete:
                state = ("window_thread_stop_timeout" if a._thread and a._thread.is_alive()
                         else "window_teardown_failed")

        if state in ("observed", "missed") and time.monotonic() >= deadline:
            state = "deadline"
        if state in ("observed", "missed"):
            def handoff_gate(owned_hwnd=None):
                if time.monotonic() >= deadline:
                    handoff_reason[0] = "deadline"
                elif observer.real_input_detected.is_set():
                    handoff_reason[0] = "real_input"
                elif win32.last_input_tick() != handoff_input_tick[0]:
                    handoff_reason[0] = "input_since_last_probe"
                elif not _safe_handoff_foreground(win32.foreground_context(), owned_hwnd):
                    handoff_reason[0] = "owner_or_focus_lost"
                else:
                    return True
                return False

            # This check runs before B is constructed. Its window thread repeats
            # the check before creation, display, and SetForegroundWindow.
            if not handoff_gate():
                state = handoff_reason[0]

        if state in ("observed", "missed"):
            # A missed callback leaves the registration installed. B is the only
            # subsequent target and receives at most one recovery probe.
            b = _OwnedProbeWindow()
            if not b.open(activation_gate=handoff_gate):
                state = (handoff_reason[0] if b.status == "handoff_unsafe"
                         else "probe_window_unavailable")
            else:
                focus_deadline = min(deadline, time.monotonic() + 2)
                while not b.focused() and time.monotonic() < focus_deadline:
                    if observer.real_input_detected.wait(0.025):
                        break
                if observer.real_input_detected.is_set():
                    state = "real_input"
                elif not b.focused():
                    state = "focus_lost"
                elif state == "missed":
                    state = probe(b, "b_recovery", "window", "unicode_up", recovery=True)
                    result["status"] = ({"observed": "miss_recovered", "missed": "miss_persisted"}
                                        .get(state, state))
                else:
                    state = probe_pair(b, "b_after_teardown", "window")
                    if state == "missed" and not observer.real_input_detected.is_set():
                        # The handoff already happened. One additional B probe
                        # tests whether the same registration resumes delivery.
                        recovery = probe(b, "b_recovery", "window", "unicode_up", recovery=True)
                        result["status"] = ({"observed": "miss_recovered", "missed": "miss_persisted"}
                                            .get(recovery, recovery))
        if observer.real_input_detected.is_set() or state == "real_input":
            result.update(status="real_input", reason="real input detected; stopped")
        elif result["status"] == "inconclusive" and state != "observed":
            result["status"] = state
        result["latched_failure"] = observer.tripped.is_set()
        return result
    except Exception as error:  # noqa: BLE001 - retain a bounded report on native failure
        result.update(status="error", reason=type(error).__name__)
        return result
    finally:
        if observer:
            result["registration_before_stop"] = observer.diagnostics()
            observer.stop()
            result["registration_after_stop"] = observer.diagnostics()
        if b:
            b.close()
            result["window_b"] = b.diagnostics()
        a.close(restore=False)
        result["window_a"] = a.diagnostics()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--teardown", action="store_true", help="one bounded owned-window A/B teardown probe")
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 60:
        parser.error("--seconds must be 1-60")
    result = diagnose_teardown() if args.teardown else diagnose(args.seconds)
    result["python"] = {
        "executable": sys.executable,
        "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "prefix": sys.prefix,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Input guard diagnostic: {result['status']}; report: {args.out}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
