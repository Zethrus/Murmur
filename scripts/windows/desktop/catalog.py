"""Every automated and manual check by checklist id (docs/parity/windows-checklist.md). Pure Python.

Order matters: groups run in GROUPS order and checks in the order listed. O1's error state runs
last in the overlay group (it needs a separate launch), and the components group runs last
because C1 removes the engine.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Check:
    id: str
    group: str
    title: str
    function: str
    best_effort: bool = False


GROUPS = ("setup", "settings", "overlay", "hotkeys", "insertion", "focus", "tray", "microphone", "keyboard",
          "components")

CHECKS = (
    Check("P1", "setup", "Environment recorded", "p1_environment"),
    Check("P2", "setup", "Silent per-user install", "p2_install"),
    Check("P3", "setup", "First-run setup downloads tiny.en (CPU)", "p3_first_run"),
    Check("P4", "setup", "Components page: engine and model Active, Verify", "p4_components"),
    Check("S1", "settings", "Settings captures, light and dark, current scaling", "s1_captures"),
    Check("S3", "settings", "Theme Auto follows Windows light/dark", "s3_theme"),
    Check("S4", "settings", "Minimum window size", "s4_minimum_size"),
    Check("O1", "overlay", "Overlay states", "o1_states"),
    Check("O2", "overlay", "Overlay drag and restore", "o2_drag"),
    Check("O3", "overlay", "B7: overlay buttons keep Notepad focused", "o3_buttons_keep_focus"),
    Check("O4", "overlay", "B7: empty area and drag handle keep focus", "o4_empty_area"),
    Check("O6", "overlay", "Overlay scale minimum and maximum", "o6_scale"),
    Check("O7", "overlay", "B7 evidence: no-activate subclass logged", "o7_subclass_log"),
    Check("H1", "hotkeys", "Hold hotkey into Notepad, key released first", "h1_hold"),
    Check("H2", "hotkeys", "Release order variants", "h2_release_order"),
    Check("H3", "hotkeys", "B8: Shift held after the key", "h3_shift_held"),
    Check("H4", "hotkeys", "Toggle mode", "h4_toggle"),
    Check("H5", "hotkeys", "Escape cancels", "h5_escape"),
    Check("H6", "hotkeys", "Hotkey owned by another app", "h6_conflict"),
    Check("H7", "hotkeys", "Code hotkey", "h7_code"),
    Check("I1", "insertion", "Sentence A into Notepad (type)", "i1_notepad"),
    Check("I2", "insertion", "Sentence B into the editor", "i2_editor"),
    Check("I3", "insertion", "Sentence A into Windows Terminal", "i3_terminal", best_effort=True),
    Check("I4", "insertion", "Long text with Prefer paste; clipboard restored", "i4_paste"),
    Check("I5", "insertion", "Non-ASCII through a replacement rule", "i5_unicode"),
    Check("I6", "insertion", "Clipboard only for an excluded app", "i6_clipboard_only"),
    Check("F1", "focus", "Same window", "f1_same_window"),
    Check("F2", "focus", "Focus changed: held, Insert here", "f2_focus_change"),
    Check("F3", "focus", "Window closed and replaced", "f3_window_replaced"),
    Check("F4", "focus", "Copy, Discard, expiry", "f4_copy_discard_expiry"),
    Check("F5", "focus", "Refocus mode", "f5_refocus"),
    Check("F6", "focus", "Copy mode", "f6_copy_mode"),
    Check("F7", "focus", "Insert mode", "f7_insert_mode"),
    Check("F8", "focus", "Started from Settings", "f8_from_settings"),
    Check("F11", "focus", "Overlay drag delays delivery", "f11_drag_delivery"),
    Check("F12", "focus", "No transcript text in the log", "f12_log_privacy"),
    Check("F13", "focus", "Insert here does not retrigger a 'd' hotkey", "f13_no_retrigger"),
    Check("T1", "tray", "Tray left-click opens Settings", "t1_tray_left", best_effort=True),
    Check("T2", "tray", "Tray menu items", "t2_tray_menu", best_effort=True),
    Check("T3", "tray", "Single instance", "t3_single_instance"),
    Check("A1", "tray", "Start with Windows writes the Run entry (registry half)", "a1_autostart_on"),
    Check("A2", "tray", "Start with Windows off removes the Run entry", "a2_autostart_off"),
    Check("M1", "microphone", "Desktop-apps microphone switch explained", "m1_desktop_apps_off"),
    Check("M3", "microphone", "Restored: no explanation, level meter moves", "m3_restored"),
    Check("K1", "keyboard", "Tab order and activation", "k1_keyboard", best_effort=True),
    Check("C1", "components", "Remove the engine while idle", "c1_remove_engine"),
    Check("C3", "components", "Pause, quit, restart, resume a download", "c3_pause_resume"),
)

# Run by scripts/windows/desktop/manual.py (docs/parity/windows-checklist.md, "manual").
MANUAL = (
    ("S2", "150 % scaling (sign out and in, then capture)"),
    ("O5", "Mixed-DPI overlay drag (second monitor)"),
    ("F9", "UAC secure desktop during insert mode"),
    ("F10", "Elevated Notepad target"),
    ("A1-SIGNIN", "Start with Windows: sign out and in (sign-in half of A1)"),
    ("M2", "Device-wide microphone switch off"),
    ("M4", "Every microphone unplugged"),
    ("C2", "Reinstall while an antivirus scan runs"),
    ("K2", "Narrator on General and Audio"),
    ("K3", "Reduced motion"),
    ("VISUAL", "Contact sheet of S1/O1/O6 captures next to the Linux baselines"),
)

BY_ID = {check.id: check for check in CHECKS}


class SelectionError(ValueError):
    pass


def parse_ids(text):
    """'H1, f2' -> ['H1', 'F2'] (upper-cased, order kept, duplicates dropped)."""
    ids = []
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip().upper()
        if part and part not in ids:
            ids.append(part)
    return ids


def select(only=None, group=None):
    """Checks to run, in catalog order. `only`: ids (string or list); `group`: group names."""
    only_ids = parse_ids(only) if isinstance(only, str) else [i.upper() for i in (only or [])]
    groups = [g.strip().lower() for g in (group or "").split(",") if g.strip()] if isinstance(group, str) \
        else [g.lower() for g in (group or [])]
    unknown = [i for i in only_ids if i not in BY_ID]
    if unknown:
        manual = {item for item, _ in MANUAL}
        hint = [i for i in unknown if i in manual]
        raise SelectionError(f"Unknown check id(s): {', '.join(unknown)}"
                             + (f" ({', '.join(hint)} are manual: run manual.py)" if hint else ""))
    bad_groups = [g for g in groups if g not in GROUPS]
    if bad_groups:
        raise SelectionError(f"Unknown group(s): {', '.join(bad_groups)}; choose from {', '.join(GROUPS)}")
    chosen = [c for c in CHECKS if (not only_ids or c.id in only_ids) and (not groups or c.group in groups)]
    if not chosen:
        raise SelectionError("The selection matches no checks")
    return sorted(chosen, key=lambda c: (GROUPS.index(c.group), CHECKS.index(c)))
