"""Windows the dictated text goes into.

- TargetApp: a small Tk window shipped with the harness (run as its own process). It records
  every received character with a timestamp, focus-in/out times and the widget text, and writes
  them to a JSON state file after every event. Exact, scriptable assertions.
- Notepad (Windows 11): text read back through UI Automation TextPattern, else by saving.
- Windows Terminal running Windows PowerShell: buffer read back through TextPattern.
- VS Code, only when installed: read back by saving the file.

`python targets.py target-app --title T --state FILE [--role editor]` runs the Tk window;
`python targets.py banner SECONDS` shows the start countdown.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- Tk side (child process)


def run_target_app(title, state_path, role):
    import tkinter as tk
    state_path = Path(state_path)
    command_path = state_path.with_suffix(".cmd")
    root = tk.Tk()
    root.title(title)
    root.geometry("640x260")
    font = ("Consolas", 12) if role == "editor" else ("Segoe UI", 12)
    area = tk.Text(root, wrap="word", font=font, undo=False)
    area.pack(fill="both", expand=True)
    record = {"title": title, "role": role, "pid": os.getpid(), "hwnd": 0, "text": "", "keys": [], "focus": [],
              "updated": time.time()}

    def save():
        record["text"] = area.get("1.0", "end-1c")
        record["updated"] = time.time()
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        for _ in range(20):
            try:
                os.replace(temporary, state_path)
                return
            except PermissionError:  # the harness is reading it right now
                time.sleep(0.01)

    def on_key(event):
        record["keys"].append({"char": event.char, "keysym": event.keysym, "state": event.state,
                               "t": time.time()})
        root.after(1, save)

    def on_focus(kind):
        record["focus"].append({"event": kind, "t": time.time()})
        save()

    def on_modified(_):
        area.edit_modified(False)
        save()

    def poll():
        if command_path.exists():
            try:
                command = command_path.read_text(encoding="utf-8").strip()
                command_path.unlink()
            except OSError:
                command = ""
            if command == "clear":
                area.delete("1.0", "end")
                record["keys"].clear()
                save()
            elif command == "quit":
                root.destroy()
                return
        root.after(100, poll)

    area.bind("<Key>", on_key, add="+")
    root.bind("<FocusIn>", lambda _: on_focus("in"))
    root.bind("<FocusOut>", lambda _: on_focus("out"))
    area.bind("<<Modified>>", on_modified)
    area.focus_set()
    root.update()
    record["hwnd"] = int(root.wm_frame(), 16)
    save()
    root.after(100, poll)
    root.mainloop()


def run_banner(seconds):
    import tkinter as tk
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    width = root.winfo_screenwidth()
    root.geometry(f"{width}x90+0+0")
    root.configure(bg="#8a1c1c")
    label = tk.Label(root, fg="white", bg="#8a1c1c", font=("Segoe UI", 18, "bold"))
    label.pack(expand=True, fill="both")
    end = time.monotonic() + seconds

    def tick():
        left = end - time.monotonic()
        if left <= 0:
            root.destroy()
            return
        label.configure(text=f"Murmur desktop tests start in {left:.0f} s: do not touch the keyboard or mouse "
                             "(any input aborts the run)")
        root.after(200, tick)

    tick()
    root.mainloop()


# ---------------------------------------------------------------- harness side


def pythonw():
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate if candidate.exists() else sys.executable)


def show_banner(seconds):
    """Blocking countdown banner (the harness has not injected anything yet)."""
    subprocess.run([pythonw(), str(HERE / "targets.py"), "banner", str(seconds)], timeout=seconds + 30)


def delivered_keys(keys):
    """Character-producing events of a TargetApp key log (pure; unit-tested)."""
    return [k for k in keys if k.get("char") and k["char"].isprintable()]


class TargetApp:
    """Handle on one TargetApp window. Use clear() between deliveries."""

    # A click with the hold-mode modifiers down is harmless here: the window is empty.
    CLICK_WHILE_HELD = True

    def __init__(self, work, name, role="prose", guard=None):
        import win32
        self.win32 = win32
        self.name = name
        self.title = f"Desktop Target {name}"
        self.role = role
        self.state_path = Path(work) / f"target-{name}.json"
        self.guard = guard
        self.process = None
        self.hwnd = 0

    def start(self, timeout=20):
        for path in (self.state_path, self.state_path.with_suffix(".cmd")):
            path.unlink(missing_ok=True)
        self.process = subprocess.Popen([pythonw(), str(HERE / "targets.py"), "target-app", "--title", self.title,
                                         "--state", str(self.state_path), "--role", self.role])
        state = self.win32.wait_for(lambda: self.state() if self.state().get("hwnd") else None, timeout)
        if not state:
            raise RuntimeError(f"{self.title} did not start")
        self.hwnd = state["hwnd"]
        return self

    def state(self):
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def text(self):
        return self.state().get("text", "")

    def keys(self):
        return self.state().get("keys", [])

    def text_point(self):
        left, top, right, bottom = self.win32.window_rect(self.hwnd)
        return ((left + right) // 2, top + (bottom - top) * 2 // 3)

    def focus(self, click=True):
        """Foreground with the caret in the text area. click=True uses a real mouse click."""
        if click:
            self.win32.activate(self.hwnd)
            self.win32.click(*self.text_point())
            ok = self.win32.wait_for(lambda: self.win32.foreground() == self.hwnd, 3)
        else:
            ok = self.win32.activate(self.hwnd, click_at=self.text_point())
        if not ok:
            raise RuntimeError(f"Could not focus {self.title}")

    def clear(self, timeout=5):
        self.state_path.with_suffix(".cmd").write_text("clear", encoding="utf-8")
        if not self.win32.wait_for(lambda: self.text() == "" and not self.keys(), timeout):
            raise RuntimeError(f"{self.title} did not clear")

    def wait_text(self, timeout=45, stable=1.2, sleep=None):
        """Text once it is non-empty and unchanged for `stable` seconds; '' at the deadline."""
        sleep = sleep or (self.guard.sleep if self.guard else time.sleep)
        deadline = time.monotonic() + timeout
        last, since = None, time.monotonic()
        while time.monotonic() < deadline:
            current = self.text()
            if current != last:
                last, since = current, time.monotonic()
            elif current and time.monotonic() - since >= stable:
                return current
            sleep(0.1)
        return last or ""

    def delivered(self):
        """Key events that produced a character. keys() also holds the hotkey's own Ctrl/Alt/
        Shift presses made while this window had focus; delivery timing must ignore those."""
        return delivered_keys(self.keys())

    def first_key_time(self):
        keys = self.delivered()
        return keys[0]["t"] if keys else None

    def first_key(self):
        keys = self.delivered()
        return keys[0] if keys else None

    def close(self):
        if self.process and self.process.poll() is None:
            self.state_path.with_suffix(".cmd").write_text("quit", encoding="utf-8")
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


class Notepad:
    """Windows 11 Notepad on a file in the work folder. Preflight requires Notepad not to be
    running, so these windows and their tabs are only ever the harness's own."""

    def __init__(self, work, name):
        import uia
        import win32
        self.win32, self.uia = win32, uia
        self.path = Path(win32.long_path(str(Path(work) / f"notepad-{name}.txt")))
        self.hwnd = 0

    def start(self, timeout=20):
        self.path.write_text("", encoding="utf-8")
        before = set(self.win32.find_windows(contains="Notepad"))
        subprocess.Popen(["notepad.exe", str(self.path)])
        self.hwnd = self.win32.wait_for(
            lambda: next((h for h in self.win32.find_windows(contains=self.path.stem) if h not in before), None), timeout)
        if not self.hwnd:
            raise RuntimeError("Notepad did not open")
        return self

    # Shift+click would extend Notepad's selection from the caret, and the next typed character
    # would replace it: while hotkey modifiers are held, activate without clicking.
    CLICK_WHILE_HELD = False

    def document(self):
        return self.uia.text_element(self.uia.from_handle(self.hwnd), timeout=5)

    def focus(self, click=True):
        left, top, right, bottom = self.win32.window_rect(self.hwnd)
        point = ((left + right) // 2, (top + bottom) // 2)
        self.win32.activate(self.hwnd, click_at=None if not click else point)
        if click:
            self.win32.click(*point)
        if not self.win32.wait_for(lambda: self.win32.foreground() == self.hwnd, 3):
            raise RuntimeError("Could not focus Notepad")

    def text(self):
        """Document text via TextPattern; falls back to Ctrl+S and reading the file."""
        try:
            # RichEdit reports a final paragraph mark; transcripts never end in a newline.
            return self.uia.text(self.document()).replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        except Exception:  # noqa: BLE001 - fall back to the saved file
            self.focus()
            self.win32.tap("ctrl", "s")
            time.sleep(1.0)
            return self.path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").rstrip("\n")

    def wait_text(self, timeout=45, stable=1.2, sleep=time.sleep):
        deadline = time.monotonic() + timeout
        last, since = None, time.monotonic()
        while time.monotonic() < deadline:
            current = self.text()
            if current != last:
                last, since = current, time.monotonic()
            elif current and time.monotonic() - since >= stable:
                return current
            sleep(0.2)
        return last or ""

    def menu_open(self):
        """True when keyboard focus sits in a menu (a stray Alt activated the menu bar)."""
        element = self.uia.focused()
        return bool(element) and self.uia.control_type(element) in (self.uia.MENU, self.uia.MENUBAR,
                                                                      self.uia.MENUITEM)

    def close(self):
        if not self.hwnd or not self.win32.is_window(self.hwnd):
            return
        try:
            self.focus()
            self.win32.tap("ctrl", "s")
            time.sleep(0.8)
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
        self.win32.close_window(self.hwnd)
        if not self.win32.wait_for(lambda: not self.win32.is_window(self.hwnd), 5):
            self.win32.kill(self.win32.window_pid(self.hwnd), tree=False)
        self.hwnd = 0


class Terminal:
    """A new Windows Terminal window running Windows PowerShell without a profile."""

    TITLE = "Murmur desktop terminal"

    def __init__(self):
        import uia
        import win32
        self.win32, self.uia = win32, uia
        self.hwnd = 0

    @staticmethod
    def available():
        return shutil.which("wt.exe") is not None

    def start(self, timeout=30):
        subprocess.Popen(["wt.exe", "-w", "new", "--title", self.TITLE, "--suppressApplicationTitle",
                          "powershell.exe", "-NoLogo", "-NoProfile"])
        self.hwnd = self.win32.wait_for(lambda: next(iter(self.win32.find_windows(contains=self.TITLE)), None), timeout)
        if not self.hwnd:
            raise RuntimeError("Windows Terminal did not open")
        time.sleep(2.0)  # PowerShell prints its prompt
        return self

    def focus(self):
        left, top, right, bottom = self.win32.window_rect(self.hwnd)
        point = ((left + right) // 2, (top + bottom) // 2)
        self.win32.activate(self.hwnd, click_at=point)
        self.win32.click(*point)
        if not self.win32.wait_for(lambda: self.win32.foreground() == self.hwnd, 3):
            raise RuntimeError("Could not focus Windows Terminal")

    def buffer(self):
        """Terminal text through TextPattern, or None when this version does not expose it."""
        try:
            return self.uia.text(self.uia.text_element(self.uia.from_handle(self.hwnd), timeout=5))
        except Exception:  # noqa: BLE001 - reported as a skip by I3
            return None

    def close(self):
        if self.hwnd and self.win32.is_window(self.hwnd):
            self.win32.close_window(self.hwnd)
            self.win32.wait_for(lambda: not self.win32.is_window(self.hwnd), 5)
        self.hwnd = 0


class VSCode:
    """VS Code on a scratch file, read back by saving (Monaco's UIA text is partial)."""

    def __init__(self, work, name):
        import win32
        self.win32 = win32
        self.path = Path(win32.long_path(str(Path(work) / f"vscode-{name}.txt")))
        self.hwnd = 0

    @staticmethod
    def executable():
        return shutil.which("code.cmd") or shutil.which("code")

    def start(self, timeout=60):
        self.path.write_text("", encoding="utf-8")
        subprocess.Popen([self.executable(), "--new-window", "--disable-extensions", str(self.path)], shell=False)
        self.hwnd = self.win32.wait_for(
            lambda: next(iter(self.win32.find_windows(contains=f"{self.path.name} - ")), None), timeout)
        if not self.hwnd:
            raise RuntimeError("VS Code did not open the scratch file")
        time.sleep(2.0)
        return self

    def focus(self):
        left, top, right, bottom = self.win32.window_rect(self.hwnd)
        point = ((left + right) // 2, top + (bottom - top) // 3)
        self.win32.activate(self.hwnd, click_at=point)
        self.win32.click(*point)
        if not self.win32.wait_for(lambda: self.win32.foreground() == self.hwnd, 3):
            raise RuntimeError("Could not focus VS Code")

    def text(self):
        self.focus()
        self.win32.tap("ctrl", "s")
        time.sleep(1.0)
        return self.path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")

    def close(self):
        if self.hwnd and self.win32.is_window(self.hwnd):
            self.win32.close_window(self.hwnd)
            self.win32.wait_for(lambda: not self.win32.is_window(self.hwnd), 10)
        self.hwnd = 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    app = sub.add_parser("target-app")
    app.add_argument("--title", required=True)
    app.add_argument("--state", required=True)
    app.add_argument("--role", default="prose", choices=("prose", "editor"))
    banner = sub.add_parser("banner")
    banner.add_argument("seconds", type=float)
    args = parser.parse_args(argv)
    if args.command == "target-app":
        run_target_app(args.title, args.state, args.role)
    else:
        run_banner(args.seconds)


if __name__ == "__main__":
    main()
