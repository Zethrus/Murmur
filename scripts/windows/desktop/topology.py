"""Read-only native window topology around the overlay's WebView2 hit window.

Answers one question for the activation repair decision: who owns the renderer's
immediate parent and the WebView2 controller host? Nothing here activates, focuses,
subclasses or restyles a window, and no window title or text is read.

Every node is read twice (before and after the whole walk). Any change of identity,
a destroyed handle, a cycle, or a walk that exceeds its bound makes the snapshot
inconclusive rather than partial evidence.
"""
import re

MAX_ANCESTORS = 16
MAX_DESCENDANTS = 128
GA_PARENT, GA_ROOT = 1, 2
GW_OWNER = 4
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_EX_NOACTIVATE = 0x08000000
WRY_HOST_CLASS = "WRY_WEBVIEW"
HOST_LOG = "QA overlay webview host"
HOST_UNAVAILABLE = "QA overlay webview host unavailable"
# Fields that identify one window; style, visibility and bounds may legitimately differ.
IDENTITY = ("hwnd", "parent", "owner", "pid", "tid", "class")


class Native:
    """Default adapter over win32 (imported lazily so tests run on any OS)."""

    def __init__(self, win32=None):
        if win32 is None:
            import win32 as module
            win32 = module
        self.w = win32

    def is_window(self, hwnd):
        return self.w.is_window(hwnd)

    def node(self, hwnd):
        user32 = self.w.user32
        return {"hwnd": hwnd,
                "parent": user32.GetAncestor(hwnd, GA_PARENT) or 0,
                "owner": user32.GetWindow(hwnd, GW_OWNER) or 0,
                "pid": self.w.window_pid(hwnd), "tid": self.w.window_thread(hwnd),
                "class": self.w.class_name(hwnd),
                "style": user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & 0xFFFFFFFF,
                "exstyle": user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF,
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "enabled": bool(user32.IsWindowEnabled(hwnd)),
                "rect": self.w.window_rect(hwnd)}

    def children(self, hwnd, limit):
        return self.w.child_windows(hwnd, limit)


def _read(native, hwnd, errors, where):
    if not hwnd or not native.is_window(hwnd):
        errors.append(f"{where}: window {hwnd:#x} is not a live window" if hwnd else f"{where}: null window")
        return None
    try:
        return native.node(hwnd)
    except Exception as error:  # noqa: BLE001 - an unreadable property is an inconclusive result
        errors.append(f"{where}: {hwnd:#x} unreadable: {type(error).__name__}: {error}")
        return None


def _ancestors(native, hit, root, errors):
    chain, seen, current = [], set(), hit
    while True:
        if current in seen:
            errors.append(f"ancestor cycle at {current:#x}")
            return chain
        if len(chain) >= MAX_ANCESTORS:
            errors.append(f"ancestor chain exceeds {MAX_ANCESTORS}")
            return chain
        seen.add(current)
        node = _read(native, current, errors, "ancestor")
        if node is None:
            return chain
        chain.append(node)
        if current == root:
            return chain
        if not node["parent"]:
            errors.append("ancestor chain ended before the overlay root")
            return chain
        current = node["parent"]


def _descendants(native, root, errors):
    try:
        handles = native.children(root, MAX_DESCENDANTS + 1)
    except Exception as error:  # noqa: BLE001
        errors.append(f"descendant enumeration failed: {type(error).__name__}: {error}")
        return []
    if len(handles) > MAX_DESCENDANTS:
        errors.append(f"descendants exceed {MAX_DESCENDANTS}")
        handles = handles[:MAX_DESCENDANTS]
    if len(set(handles)) != len(handles):
        errors.append("descendant enumeration repeated a window")
    nodes = []
    for hwnd in handles:
        node = _read(native, hwnd, errors, "descendant")
        if node is not None:
            nodes.append(node)
    return nodes


def parse_host_log(log):
    """Last app-side controller-host record (QA builds only); None when absent or malformed."""
    found = None
    for line in log.splitlines():
        if HOST_UNAVAILABLE in line:
            found = {"valid": False, "unavailable": True}
            continue
        if HOST_LOG + " " not in line:
            continue
        fields = dict(token.split("=", 1) for token in line.split(HOST_LOG, 1)[1].split() if "=" in token)
        record = {"valid": False, "fields": fields}
        try:
            for key in ("overlay", "controller_parent", "controller_parent_parent"):
                if not re.fullmatch(r"0x[0-9a-fA-F]+", fields[key]):
                    raise ValueError(key)
                record[key] = int(fields[key], 16)
            for key in ("controller_parent_pid", "controller_parent_tid", "process", "thread"):
                record[key] = int(fields[key])
            for key in ("parent_hresult", "version_hresult"):
                if not re.fullmatch(r"0x[0-9a-fA-F]+", fields[key]):
                    raise ValueError(key)
                record[key] = int(fields[key], 16)
            record["browser_version"] = fields["browser_version"]
            # A failed COM read is a recorded result; it never becomes a host identity.
            # An unreadable host (IsWindow failed) logs pid/tid 0: never treat it as "foreign".
            record["valid"] = (record["parent_hresult"] == 0 and record["controller_parent"] != 0 and
                               record["controller_parent_pid"] != 0 and record["controller_parent_tid"] != 0)
        except (KeyError, ValueError):
            pass
        found = record
    return found


def classify(snapshot, process_pid, overlay_tid, host=None):
    """Decision input for plan section 3, Batch C. Inconclusive is a result, not a retry."""
    if snapshot["errors"]:
        return {"result": "inconclusive", "reason": "; ".join(snapshot["errors"])}
    chain = snapshot["ancestors"]
    if len(chain) < 2:
        return {"result": "inconclusive", "reason": "hit window is the overlay root or has no parent"}
    parent = chain[1]
    owned = parent["pid"] == process_pid and parent["tid"] == overlay_tid
    hosts = [node for node in snapshot["descendants"] if node["class"] == WRY_HOST_CLASS
             and node["pid"] == process_pid and node["tid"] == overlay_tid]
    out = {"renderer_parent": {key: parent[key] for key in ("hwnd", "pid", "tid", "class", "exstyle")},
           "renderer_parent_owned": owned,
           "renderer_parent_noactivate": bool(parent["exstyle"] & WS_EX_NOACTIVATE),
           "owned_wry_hosts": [node["hwnd"] for node in hosts],
           "chain_classes": [node["class"] for node in chain]}
    if host is not None:
        if host.get("unavailable"):
            out["controller_host"] = "app could not queue the host read"
        elif not host.get("valid"):
            out["controller_host"] = "malformed or unreadable app record"
        elif host["overlay"] != chain[-1]["hwnd"] or host["process"] != process_pid:
            out["controller_host"] = "app record is for a different overlay or process"
        else:
            matched = [node for node in chain if node["hwnd"] == host["controller_parent"]]
            out["controller_host"] = {"hwnd": host["controller_parent"],
                                      "in_hit_chain": bool(matched),
                                      "is_renderer_parent": host["controller_parent"] == parent["hwnd"],
                                      "owned": (host["controller_parent_pid"] == process_pid and
                                                host["controller_parent_tid"] == overlay_tid),
                                      "browser_version": host["browser_version"]}
    out["result"] = "owned-immediate-parent" if owned else "foreign-immediate-parent"
    return out


def renderer_parents(snapshot):
    """Immediate parent of every renderer window found among the overlay's descendants."""
    by_hwnd = {node["hwnd"]: node for node in [snapshot["overlay"] or {}] + snapshot["descendants"]
               if node}
    rows = []
    for node in snapshot["descendants"]:
        if node["class"] != "Chrome_RenderWidgetHostHWND":
            continue
        parent = by_hwnd.get(node["parent"])
        rows.append({"renderer": node["hwnd"], "renderer_pid": node["pid"], "parent": node["parent"],
                     "parent_pid": parent and parent["pid"], "parent_tid": parent and parent["tid"],
                     "parent_class": parent and parent["class"],
                     "parent_noactivate": bool(parent and parent["exstyle"] & WS_EX_NOACTIVATE)})
    return rows


def snapshot(overlay, hit, native=None):
    """Bounded, twice-read topology. `hit` may be 0 to enumerate descendants only."""
    native = native or Native()
    errors = []
    first_root = _read(native, overlay, errors, "overlay")
    ancestors = _ancestors(native, hit, overlay, errors) if hit else []
    descendants = _descendants(native, overlay, errors) if first_root else []
    # Second read: identities must not change while the walk was in progress.
    for node in ([first_root] if first_root else []) + ancestors + descendants:
        again = _read(native, node["hwnd"], errors, "reread")
        if again is not None and any(again[key] != node[key] for key in IDENTITY):
            errors.append(f"window {node['hwnd']:#x} identity changed during enumeration")
    return {"overlay": first_root, "hit": hit, "ancestors": ancestors, "descendants": descendants,
            "limits": {"ancestors": MAX_ANCESTORS, "descendants": MAX_DESCENDANTS},
            "complete": not errors, "errors": errors}
