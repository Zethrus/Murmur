#!/usr/bin/env python3
"""Manual session (about 25 minutes, once per release) for the checklist items tagged manual.

  python manual.py --report <dir with desktop-report.json> [--only S2,K2] [--app <murmur.exe>]

Walks the tester through each item: shows the steps and the expected result, opens what it can
(Windows Settings pages, Notepad, the capture folder), optionally saves a full-screen screenshot,
and records pass / fail / skip with a note. Results are merged into desktop-report.json and
desktop-report.md in the same folder (source "manual"). Run it in the logged-in session; it does
not inject any input.
"""
import argparse
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import catalog  # noqa: E402
import report  # noqa: E402

STEPS = {
    "S2": ("Set display scaling to 150 % (Settings > Display), sign out and in, then run "
           "scripts\\parity\\capture-windows.ps1 -App <murmur.exe>.",
           "No clipped text, overlapping controls or horizontal scrollbars in the 150 % captures.",
           "ms-settings:display"),
    "O5": ("With a second monitor at a different scaling (100 % and 150 %), drag the overlay across the "
           "boundary and release it on each monitor.", "Size and position follow the monitor; no jump back.",
           "ms-settings:display"),
    "F9": ("Insert mode: start dictating in Notepad; before releasing, run regedit to raise a UAC prompt and "
           "leave it up; release.", "Held with 'a Windows security prompt or the lock screen had focus'; nothing "
           "typed anywhere. Film it with a phone: the secure desktop cannot be captured.", None),
    "F10": ("Run Notepad as administrator; dictate into it (Murmur not elevated), hold mode and insert mode.",
            "Held with 'the window runs as administrator…' or 'Murmur could not confirm…'; Copy works; nothing "
            "claims delivered.", None),
    "A1-SIGNIN": ("Enable Start with Windows, sign out and sign in.",
           "Murmur starts in the tray without opening Settings (--background).", None),
    "M2": ("Turn off 'Microphone access' for the device (needs an administrator), open Murmur > Audio, Refresh.",
           "The explanation names the device switch. Turn it back on afterwards.", "ms-settings:privacy-microphone"),
    "M4": ("Unplug every microphone; Murmur > Audio > Refresh.", "'No microphone found' with the Settings button.",
           None),
    "C2": ("Start a Microsoft Defender custom scan of %LOCALAPPDATA%\\Murmur, then Components > Reinstall the "
           "engine while it runs.", "Install succeeds (rename retries up to 5 s) or fails clearly and keeps the "
           "previous state.", "windowsdefender:"),
    "K2": ("Start Narrator (Ctrl+Win+Enter); move through Settings > General and Audio.",
           "Page buttons, controls and their labels are announced. Write down what was read.", None),
    "K3": ("Turn off animation effects (Settings > Accessibility > Visual effects); dictate once.",
           "Overlay animations are reduced. Turn the setting back on afterwards.", "ms-settings:easeofaccess-visualeffects"),
    "VISUAL": ("Open the report folder: compare the S1, O1 and O6 PNGs with "
               "docs/parity/captures/baseline-0.4.7/linux-* (docs/parity/README.md 'Must match').",
               "Looks right: same layout and hierarchy, no clipping, transparent overlay.", "folder"),
}
ANSWERS = {"p": report.PASS, "pass": report.PASS, "f": report.FAIL, "fail": report.FAIL,
           "s": report.SKIPPED, "skip": report.SKIPPED}


def parse_answer(text):
    """'p', 'fail', ... -> status, or None when the answer is not understood."""
    return ANSWERS.get((text or "").strip().lower())


def items(only=None):
    wanted = catalog.parse_ids(only) if only else [item for item, _ in catalog.MANUAL]
    known = {item for item, _ in catalog.MANUAL}
    unknown = [i for i in wanted if i not in known]
    if unknown:
        raise catalog.SelectionError(f"Not a manual item: {', '.join(unknown)}")
    return [(item, title) for item, title in catalog.MANUAL if item in wanted]


def manual_result(item, title, status, note, artifacts=()):
    if status in (report.FAIL, report.SKIPPED) and not note:
        note = "no note given"
    return report.Result(item, status, note if status != report.PASS else "", title, "manual",
                         artifacts=list(artifacts), notes=[note] if status == report.PASS and note else [],
                         source="manual")


def _open(target, folder):
    if target == "folder":
        subprocess.Popen(["explorer.exe", str(folder)])
    elif target:
        subprocess.Popen(["cmd", "/c", "start", "", target])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--report", required=True, type=Path, help="folder with desktop-report.json")
    parser.add_argument("--only", default="")
    args = parser.parse_args(argv)
    path = args.report / "desktop-report.json"
    meta, attempts = report.load(path) if path.exists() else ({"sha": "manual"}, [])
    collected = []
    for item, title in items(args.only or None):
        steps, expected, target = STEPS[item]
        print(f"\n== {item}: {title}\nSteps: {steps}\nExpected: {expected}")
        if target and input("Open what this step needs? [y/N] ").strip().lower() == "y":
            _open(target, args.report)
        artifacts = []
        if input("Save a full-screen screenshot now? [y/N] ").strip().lower() == "y":
            time.sleep(2)
            from PIL import ImageGrab
            shot = args.report / f"{item}-manual-{len(artifacts) + 1}.png"
            ImageGrab.grab(all_screens=True).save(shot)
            artifacts.append(str(shot))
            print(f"Saved {shot.name}")
        status = None
        while status is None:
            status = parse_answer(input("Result [p]ass / [f]ail / [s]kip: "))
        note = input("Note (observed behaviour; required for fail/skip): ").strip()
        collected.append(manual_result(item, title, status, note, artifacts))
    meta["manual_finished"] = report.now()
    report.write(args.report, meta, report.merge(attempts, collected))
    print(f"\nMerged {len(collected)} manual results into {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
