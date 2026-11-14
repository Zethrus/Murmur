#!/usr/bin/env python3
"""Automated Windows desktop checks (docs/parity/windows-checklist.md, items tagged auto).

  python run.py --build <verified build dir> --out <report dir> [--only H1,F2] [--group focus]
                [--repeat N] [--hotkey Ctrl+Alt+Shift+F9] [--preflight-only] [--keep-work]

Runs in the logged-in desktop session (the `desktop` runner, or by hand). It takes the keyboard
and mouse: any real input aborts the run within ~100 ms, everything it changed is restored and
the remaining checks are marked "aborted: user input". A shared Murmur startup failure also
stops later checks, after saving app output, log and window evidence. Writes desktop-report.json,
desktop-report.md and the evidence PNG/TXT files to --out. Exit code 0 only when nothing failed
or aborted; skipped checks carry their reason.
"""
import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import traceback

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import catalog  # noqa: E402
import report  # noqa: E402

DEFAULT_HOTKEY = "Ctrl+Alt+Shift+F9"
IDLE_SECONDS = 60
BANNER_SECONDS = 10
# Checks that drive Notepad: Windows 11 Notepad restores its tabs, so it must not be running.
NOTEPAD_CHECKS = {"O3", "O4", "H1", "I1", "I4", "I5", "I6"}


def base_config(hotkey):
    return {
        "general": {"hotkey": hotkey, "hotkey_mode": "hold", "theme": "light", "autostart": False,
                    "language": "en", "hotkey_code": ""},
        "overlay": {"enabled": True, "idle_view": "pill", "position": "bottom-center", "scale": 1.0,
                    "show_preview": True},
        "sounds": {"enabled": False},
        "engine": {"model": "tiny.en", "device": "cpu"},
        "injection": {"method": "auto", "prefer_paste": False, "focus_change": "hold", "clipboard_only": False},
        "privacy": {"history_enabled": False},
        "setup": {"postponed": True},
    }


def preflight(build_dir, selected, win32, idle_needed=IDLE_SECONDS):
    """Problems that prevent a safe run (empty list = go)."""
    problems = []
    if not (Path(build_dir) / "build.json").is_file():
        problems.append(f"{build_dir} is not a verified build (run handoff.py first)")
    desktop = win32.input_desktop()
    if desktop != "Default":
        problems.append(f"the workstation is locked or on a secure desktop (input desktop: {desktop})")
    running = win32.pids("murmur.exe")
    if running:
        problems.append(f"another Murmur is running (pid {', '.join(map(str, running))}); quit it from the tray "
                        "(single instance would hand the test launch to it)")
    if NOTEPAD_CHECKS & {c.id for c in selected} and win32.pids("Notepad.exe"):
        problems.append("Notepad is running; close it (the Notepad checks open and close their own windows)")
    idle = win32.idle_seconds()
    if idle < idle_needed:
        problems.append(f"the desktop was used {idle:.0f} s ago; it must be idle for {idle_needed} s")
    return problems


def remove_tree(path, attempts=10):
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(1 + attempt)  # Defender may hold fresh DLLs open
    return False


def run_checks(ctx, selected, repeat, lookup, log=print):
    """Run checks until complete, user input, or shared startup failure."""
    from guard import UserInput
    import win32
    from context import Fail, Skip
    from murmur import StartupError
    results = []
    plan = [(attempt, check) for attempt in range(1, repeat + 1) for check in selected]
    for position, (attempt, check) in enumerate(plan):
        ctx.begin(check.id)
        mark = ctx.restorer.mark()
        started = time.monotonic()
        status, reason = report.PASS, ""
        startup_failure = ""
        log(f"[{check.id}] {check.title} (attempt {attempt})")
        try:
            lookup(check)(ctx)
        except UserInput as error:
            reason = f"aborted: user input ({error})"
            results.append(report.Result(check.id, report.ABORTED, reason, check.title, check.group,
                                         check.best_effort, list(ctx.artifacts), list(ctx.notes),
                                         round(time.monotonic() - started, 1), attempt))
            for _, rest in plan[position + 1:]:
                results.append(report.Result(rest.id, report.ABORTED, reason, rest.title, rest.group, rest.best_effort,
                                             attempt=attempt))
            win32.release_all()
            for label, message in ctx.restorer.restore_to(mark):
                log(f"restore failed: {label}: {message}")
            return results, reason
        except Skip as error:
            status, reason = report.SKIPPED, str(error) or "skipped"
        except Fail as error:
            status, reason = report.FAIL, str(error) or "failed"
        except StartupError as error:
            trace = ctx.artifact("harness-error.txt")
            trace.write_text(traceback.format_exc(), encoding="utf-8")
            try:
                error.session.save_startup_diagnostics(ctx.artifact)
            except Exception as diagnostic_error:  # noqa: BLE001 - do not hide the launch failure
                ctx.note(f"startup diagnostics failed: {type(diagnostic_error).__name__}: {diagnostic_error}")
            status, reason = report.FAIL, f"startup failure: {error}"
            startup_failure = f"blocked after {check.id} startup failure: {error}"
        except Exception as error:  # noqa: BLE001 - one broken check must not stop the run
            trace = ctx.artifact("harness-error.txt")
            trace.write_text(traceback.format_exc(), encoding="utf-8")
            message = f"harness error: {type(error).__name__}: {error}"
            # A best-effort check that could not run is a skip, never a pass (and not a failure).
            status, reason = (report.SKIPPED, f"could not run: {message}") if check.best_effort \
                else (report.FAIL, message)
        finally:
            win32.release_all()
            for label, message in ctx.restorer.restore_to(mark):
                ctx.note(f"restore failed: {label}: {message}")
        results.append(report.Result(check.id, status, reason, check.title, check.group, check.best_effort,
                                     list(ctx.artifacts), list(ctx.notes), round(time.monotonic() - started, 1),
                                     attempt))
        log(f"[{check.id}] {status}{': ' + reason if reason else ''}")
        if startup_failure:
            for remaining_attempt, rest in plan[position + 1:]:
                results.append(report.Result(rest.id, report.ABORTED, startup_failure, rest.title, rest.group,
                                             rest.best_effort, attempt=remaining_attempt))
            return results, startup_failure
    return results, ""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--build", required=True, type=Path, help="verified build folder (handoff.py --dest)")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--only", default="", help="comma-separated checklist ids, e.g. H1,F2")
    parser.add_argument("--group", default="", help=f"comma-separated groups: {', '.join(catalog.GROUPS)}")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--hotkey", default=DEFAULT_HOTKEY, help="test dictation hotkey (avoid real app shortcuts)")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--keep-work", action="store_true", help="keep the temporary profile for debugging")
    parser.add_argument("--no-banner", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 10:
        parser.error("--repeat must be 1-10")
    try:
        selected = catalog.select(args.only or None, args.group or None)
    except catalog.SelectionError as error:
        parser.error(str(error))

    import win32
    win32.require_windows()
    win32.set_dpi_aware()
    problems = preflight(args.build, selected, win32)
    if problems:
        print("Preflight refused to start:\n- " + "\n- ".join(problems), file=sys.stderr)
        return 2
    print(f"Preflight OK: {len(selected)} checks x {args.repeat}")
    if args.preflight_only:
        return 0

    import checks
    import fixtures
    import golden
    import targets
    from context import Context
    from guard import Guard, UserInput
    from murmur import Build, MurmurSession, fetch_model
    from restore import Restorer

    build = Build(args.build)
    out = Path(win32.long_path(str(args.out)))
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(win32.long_path(os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
                                              "murmur-desktop-cache")))
    work = Path(win32.long_path(tempfile.mkdtemp(prefix="murmur-desktop-")))
    meta = {"sha": build.record.get("sha", ""), "build_run": build.record.get("build_run", {}).get("url", ""),
            "started": report.now(), "repeat": args.repeat, "murmur": build.version,
            "windows": ".".join(map(str, sys.getwindowsversion()[:3])), "selection": [c.id for c in selected]}
    restorer = Restorer()
    results, aborted = [], ""
    guard = Guard()
    session = None
    try:
        print("Preparing fixtures and the test profile (no input yet)...")
        speech = fixtures.build(cache / "fixtures")
        session = MurmurSession(build, work, guard, base_config(args.hotkey)).prepare()
        restorer.push("stop Murmur", session.stop)
        # Run-wide safety net (checks that touch the clipboard also snapshot it themselves).
        clipboard_before = win32.clipboard_text()
        restorer.push("restore the clipboard text", lambda: win32.set_clipboard_text(clipboard_before))
        session.install_engine()
        session.install(fetch_model(build, cache / "models"))
        if not args.no_banner:
            targets.show_banner(BANNER_SECONDS)
        if win32.idle_seconds() < BANNER_SECONDS:
            raise UserInput("input during the start countdown")
        guard.start()
        ctx = Context(build=build, session=session, fixtures=speech, goldens=golden.GoldenStore(), restorer=restorer,
                      guard=guard, out=out, work=work, hotkey=args.hotkey, cache=cache)
        results, aborted = run_checks(ctx, selected, args.repeat, checks.lookup)
        meta["goldens"] = ctx.goldens.as_dict()
    except UserInput as error:
        aborted = f"aborted: user input ({error})"
        results += [report.Result(c.id, report.ABORTED, aborted, c.title, c.group, c.best_effort) for c in selected
                    if c.id not in {r.id for r in results}]
    except Exception as error:  # noqa: BLE001 - setup failure: every check is reported
        trace = out / "harness-setup-error.txt"
        trace.write_text(traceback.format_exc(), encoding="utf-8")
        aborted = f"setup failed: {type(error).__name__}: {error}"
        results += [report.Result(c.id, report.ABORTED, aborted, c.title, c.group, c.best_effort, [str(trace)])
                    for c in selected if c.id not in {r.id for r in results}]
    finally:
        guard.stop()
        win32.release_all()
        failures = restorer.restore_all()
        if failures:
            meta["restore_failures"] = [f"{label}: {message}" for label, message in failures]
        if not args.keep_work and not remove_tree(work):
            meta["cleanup"] = f"could not delete {work}"
        meta["finished"] = report.now()
        meta["aborted"] = aborted
        path = report.write(out, meta, results)
        print(path.read_text(encoding="utf-8"))
    counts = report.summary(report.rows(results))
    return 0 if not aborted and not counts[report.FAIL] and not counts[report.ABORTED] else 1


if __name__ == "__main__":
    sys.exit(main())
