#!/usr/bin/env python3
"""Bounded native input-guard probe; requires only Python's standard library.

Run only on the owner-controlled, idle, unlocked desktop. The hook records counts for all
callbacks, but event fields only for injected probe candidates; no real key fields are written.
"""
import argparse
import json
from pathlib import Path
import time

import guard
import run
import win32


def diagnose(seconds):
    win32.require_windows()
    problems = run.preflight_desktop(win32)
    if problems:
        return {"status": "preflight_refused", "problems": problems}

    observer = guard.Guard(diagnostic=True)
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
        observer.stop()
    return {"status": status, "reason": reason, "elapsed_seconds": round(time.monotonic() - started, 2),
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
