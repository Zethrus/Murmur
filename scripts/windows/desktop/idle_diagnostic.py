#!/usr/bin/env python3
"""Read-only, bounded last-input sampling in the owner's interactive Windows session.

No keyboard/mouse hooks, injected input, window operations, app launch, or idle gate.
GetLastInputInfo includes injected input and does not identify its source.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import struct
import sys
import time

import win32

DURATION_SECONDS = 90
INTERVAL_SECONDS = 1


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def session_identity():
    context = win32.desktop_context()
    return {"session_id": context["session_id"],
            "window_station": context["window_station"],
            "thread_desktop": context["thread_desktop"],
            "thread_desktop_receives_input": context["thread_desktop_receives_input"],
            "input_desktop": win32.input_desktop()}


def interactive_session(identity):
    return (isinstance(identity["session_id"], int) and identity["session_id"] > 0 and
            identity["window_station"] == "WinSta0" and
            identity["thread_desktop"] == "Default" and
            identity["thread_desktop_receives_input"] is True and
            identity["input_desktop"] == "Default")


def diagnose(seconds=DURATION_SECONDS, interval=INTERVAL_SECONDS, *, clock=time.monotonic,
             sleep=time.sleep, timestamp=utc_now):
    """Sample until the deadline or session loss; never require an idle desktop."""
    win32.require_windows()
    result = {"status": "running", "started_utc": timestamp(), "duration_requested_s": seconds,
              "sample_interval_s": interval, "samples": [], "reset_count": 0}
    started = clock()
    deadline = started + seconds
    first_session = None
    previous_last_tick = None
    try:
        while True:
            identity = session_identity()
            if first_session is None:
                first_session = identity
                result["session"] = identity
            if not interactive_session(identity) or identity != first_session:
                result["status"] = "session_refused" if not result["samples"] else "session_lost"
                break
            sampled_at = clock()
            if sampled_at > deadline and result["samples"]:
                result["status"] = "ok"
                break
            last_tick = win32.last_input_tick()
            current_tick = win32.current_tick()
            changed = previous_last_tick is not None and last_tick != previous_last_tick
            sample = {"utc": timestamp(), "elapsed_s": round(sampled_at - started, 3),
                      "last_input_tick_ms": last_tick, "current_tick_ms": current_tick,
                      "idle_s": round(((current_tick - last_tick) & 0xFFFFFFFF) / 1000, 3),
                      "last_input_changed": changed}
            result["samples"].append(sample)
            if changed:
                result["reset_count"] += 1
            previous_last_tick = last_tick
            remaining = deadline - clock()
            if remaining <= 0:
                result["status"] = "ok"
                break
            sleep(min(interval, remaining))
    except (OSError, KeyError, TypeError) as error:
        result["status"] = "read_error"
        result["error_type"] = type(error).__name__
    result["finished_utc"] = timestamp()
    result["elapsed_s"] = round(clock() - started, 3)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = diagnose()
    result["python"] = {"executable_name": os.path.basename(sys.executable),
                        "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                        "implementation": sys.implementation.name,
                        "bitness": struct.calcsize("P") * 8,
                        "venv": sys.prefix != sys.base_prefix}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Idle diagnostic: {result['status']}; samples: {len(result['samples'])}; "
          f"last-input changes: {result['reset_count']}; report: {args.out}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
