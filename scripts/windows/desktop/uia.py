"""UI Automation through comtypes: find elements by name with timeouts, invoke, read text.

WebView2 exposes Murmur's Settings and overlay DOM (buttons by their accessible names,
aria-labels included) under the top-level windows "Murmur Settings" and "Murmur Overlay", the
same tree scripts/parity/capture-windows.ps1 used. comtypes is imported lazily so this module
imports on Linux (unit tests); every function needs Windows.
"""
import re
import time

BUTTON, CHECKBOX, EDIT, HYPERLINK, LISTITEM, MENU, MENUBAR, MENUITEM = 50000, 50002, 50004, 50005, 50007, 50009, \
    50010, 50011
PROGRESSBAR, RADIOBUTTON, TEXT, DOCUMENT, WINDOW, PANE, GROUP = 50012, 50013, 50020, 50030, 50032, 50033, 50026
TYPE_NAMES = {BUTTON: "Button", CHECKBOX: "CheckBox", EDIT: "Edit", HYPERLINK: "Hyperlink", LISTITEM: "ListItem",
              MENU: "Menu", MENUBAR: "MenuBar", MENUITEM: "MenuItem", PROGRESSBAR: "ProgressBar",
              RADIOBUTTON: "RadioButton", TEXT: "Text", DOCUMENT: "Document", WINDOW: "Window", PANE: "Pane",
              GROUP: "Group"}
INVOKE, VALUE, RANGEVALUE, SELECTIONITEM, TEXTPATTERN, TOGGLE = 10000, 10002, 10003, 10010, 10014, 10015
TREE_CHILDREN, TREE_DESCENDANTS = 0x2, 0x4
NAME_PROPERTY = 30005

_automation = None
_module = None


class NotFound(Exception):
    pass


def _uia():
    global _automation, _module
    if _automation is None:
        import comtypes.client  # initialises COM (STA) for this thread on import
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as module
        _module = module
        _automation = comtypes.client.CreateObject(module.CUIAutomation, interface=module.IUIAutomation)
    return _automation, _module


def root():
    automation, _ = _uia()
    return automation.GetRootElement()


def from_handle(hwnd):
    automation, _ = _uia()
    return automation.ElementFromHandle(hwnd)


def focused():
    automation, _ = _uia()
    try:
        element = automation.GetFocusedElement()
    except Exception:  # noqa: BLE001 - COM error when focus is on a secure desktop
        return None
    return element if element else None


def name(element):
    try:
        return element.CurrentName or ""
    except Exception:  # noqa: BLE001 - stale element
        return ""


def help_text(element):
    try:
        return element.CurrentHelpText or ""
    except Exception:  # noqa: BLE001
        return ""


def control_type(element):
    try:
        return element.CurrentControlType
    except Exception:  # noqa: BLE001
        return 0


def handle(element):
    try:
        return element.CurrentNativeWindowHandle or 0
    except Exception:  # noqa: BLE001
        return 0


def rect(element):
    """(left, top, right, bottom) in physical screen pixels."""
    box = element.CurrentBoundingRectangle
    return (box.left, box.top, box.right, box.bottom)


def center(element):
    left, top, right, bottom = rect(element)
    return ((left + right) // 2, (top + bottom) // 2)


def describe(element):
    if not element:
        return "<none>"
    return f"{TYPE_NAMES.get(control_type(element), control_type(element))} {name(element)!r}"


def same(first, second):
    automation, _ = _uia()
    try:
        return bool(automation.CompareElements(first, second))
    except Exception:  # noqa: BLE001
        return False


def _array(found):
    return [found.GetElement(i) for i in range(found.Length)] if found else []


def children(element):
    automation, _ = _uia()
    return _array(element.FindAll(TREE_CHILDREN, automation.CreateTrueCondition()))


def descendants(element):
    """Every descendant in tree (document) order."""
    automation, _ = _uia()
    return _array(element.FindAll(TREE_DESCENDANTS, automation.CreateTrueCondition()))


def matches(element, name_=None, name_re=None, control_type_=None, contains=None):
    if control_type_ is not None and control_type(element) not in (
            control_type_ if isinstance(control_type_, (tuple, list, set)) else (control_type_,)):
        return False
    text = name(element)
    if name_ is not None and text != name_:
        return False
    if contains is not None and contains.lower() not in text.lower():
        return False
    if name_re is not None and not re.search(name_re, text):
        return False
    return True


def find_all(element, **criteria):
    return [e for e in descendants(element) if matches(e, **criteria)]


def find(element, timeout=10.0, sleep=time.sleep, **criteria):
    """First descendant matching the criteria, polling until `timeout`; raises NotFound."""
    deadline = time.monotonic() + timeout
    while True:
        found = find_all(element, **criteria)
        if found:
            return found[0]
        if time.monotonic() >= deadline:
            raise NotFound(f"No element {criteria} after {timeout:.0f} s")
        sleep(0.25)


def try_find(element, timeout=0.0, sleep=time.sleep, **criteria):
    try:
        return find(element, timeout=timeout, sleep=sleep, **criteria)
    except NotFound:
        return None


def find_following(element, anchor, timeout=10.0, sleep=time.sleep, **criteria):
    """The first element matching `criteria` after the element named `anchor` in document order
    (e.g. the hotkey button after the "Dictation shortcut" label)."""
    deadline = time.monotonic() + timeout
    while True:
        items = descendants(element)
        index = next((i for i, e in enumerate(items) if name(e) == anchor), None)
        if index is not None:
            for candidate in items[index + 1:]:
                if matches(candidate, **criteria):
                    return candidate
        if time.monotonic() >= deadline:
            raise NotFound(f"No element {criteria} after {anchor!r}")
        sleep(0.25)


def find_window(title, timeout=30.0, sleep=time.sleep):
    """Top-level window by exact name."""
    automation, _ = _uia()
    condition = automation.CreatePropertyCondition(NAME_PROPERTY, title)
    deadline = time.monotonic() + timeout
    while True:
        window = root().FindFirst(TREE_CHILDREN, condition)
        if window:
            return window
        if time.monotonic() >= deadline:
            raise NotFound(f"Window {title!r} did not appear within {timeout:.0f} s")
        sleep(0.25)


def names(element):
    """Every non-empty descendant name, in document order."""
    return [n for n in (name(e) for e in descendants(element)) if n]


def _pattern(element, pattern_id, interface):
    _, module = _uia()
    raw = element.GetCurrentPattern(pattern_id)
    if not raw:
        raise NotFound(f"{describe(element)} does not support pattern {pattern_id}")
    return raw.QueryInterface(getattr(module, interface))


def invoke(element):
    _pattern(element, INVOKE, "IUIAutomationInvokePattern").Invoke()


def toggle(element):
    _pattern(element, TOGGLE, "IUIAutomationTogglePattern").Toggle()


def toggle_state(element):
    """0 off, 1 on, 2 indeterminate."""
    return _pattern(element, TOGGLE, "IUIAutomationTogglePattern").CurrentToggleState


def select(element):
    _pattern(element, SELECTIONITEM, "IUIAutomationSelectionItemPattern").Select()


def is_selected(element):
    return bool(_pattern(element, SELECTIONITEM, "IUIAutomationSelectionItemPattern").CurrentIsSelected)


def value(element):
    return _pattern(element, VALUE, "IUIAutomationValuePattern").CurrentValue


def range_value(element):
    return _pattern(element, RANGEVALUE, "IUIAutomationRangeValuePattern").CurrentValue


def text(element):
    """Whole document text through TextPattern (Notepad, Windows Terminal)."""
    return _pattern(element, TEXTPATTERN, "IUIAutomationTextPattern").DocumentRange.GetText(-1)


def has_pattern(element, pattern_id):
    try:
        return bool(element.GetCurrentPattern(pattern_id))
    except Exception:  # noqa: BLE001
        return False


def text_element(window, timeout=10.0, sleep=time.sleep):
    """First descendant (or the window itself) that supports TextPattern."""
    deadline = time.monotonic() + timeout
    while True:
        for element in [window] + descendants(window):
            if has_pattern(element, TEXTPATTERN):
                return element
        if time.monotonic() >= deadline:
            raise NotFound("No element with TextPattern")
        sleep(0.25)
