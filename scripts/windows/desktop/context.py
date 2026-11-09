"""What every check receives: the session, fixtures, goldens, restore stack and input helpers.

Checks signal their outcome by returning (pass) or raising Fail / Skip; real user input raises
guard.UserInput, which aborts the run. Helpers here drive a dictation the way a person would:
focus the target, hold the hotkey for the length of the spoken sentence, release in a given
order, then wait for the text to settle.
"""
from pathlib import Path
import time

import golden
import win32


class Fail(Exception):
    pass


class Skip(Exception):
    pass


HOLD_MARGIN = 0.8  # seconds held after the spoken sentence ends


class Context:
    def __init__(self, *, build, session, fixtures, goldens, restorer, guard, out, work, hotkey, cache):
        self.build = build
        self.murmur = session
        self.fixtures = fixtures
        self.goldens = goldens
        self.restorer = restorer
        self.guard = guard
        self.out = Path(out)
        self.work = Path(work)
        self.hotkey = hotkey
        self.cache = Path(cache)
        self.artifacts = []
        self.notes = []
        self.check_id = ""
        self._targets = []

    # ------------------------------------------------------------ bookkeeping
    def begin(self, check_id):
        self.check_id = check_id
        self.artifacts, self.notes = [], []

    def artifact(self, suffix):
        """Path for an evidence file of the current check (registered in the report)."""
        path = self.out / f"{self.check_id}-{suffix}"
        self.artifacts.append(str(path))
        return path

    def note(self, text):
        self.notes.append(text)

    def sleep(self, seconds):
        self.guard.sleep(seconds)

    def require(self, condition, reason):
        if not condition:
            raise Fail(reason)

    def screenshot(self, hwnd, suffix):
        import capture
        try:
            return capture.window(hwnd, self.artifact(suffix))
        except Exception as error:  # noqa: BLE001 - evidence must not turn a pass into a crash
            self.note(f"screenshot {suffix} failed: {error}")
            return None

    # ------------------------------------------------------------ targets
    def target(self, name, role="prose"):
        """A fresh TargetApp, closed by the restore stack."""
        import targets
        app = targets.TargetApp(self.work, f"{self.check_id}-{name}", role, guard=self.guard).start()
        self.restorer.push(f"close {app.title}", app.close)
        self._targets.append(app)
        return app

    def notepad(self, name="doc"):
        import targets
        pad = targets.Notepad(self.work, f"{self.check_id}-{name}").start()
        self.restorer.push(f"close Notepad {pad.path.name}", pad.close)
        return pad

    def clipboard_guard(self):
        """Restore the clipboard text after the check (other formats cannot be restored)."""
        before = win32.clipboard_text()
        formats = win32.clipboard_formats()
        if formats and before is None:
            self.note("the clipboard held non-text data, which is not restored")
        self.restorer.push("clipboard", lambda: win32.set_clipboard_text(before))
        return before

    # ------------------------------------------------------------ hotkeys
    def press(self, spec):
        modifiers, key = win32.parse_combo(spec)
        win32.key_down(*modifiers)
        time.sleep(0.03)
        win32.key_down(key)

    def release(self, spec, order="key-first"):
        """key-first: key, then modifiers (H1); together: one SendInput batch (H2);
        modifiers-held: key, 1 s, modifiers (H2); shift-late: key and others, Shift 300 ms later
        (H3). Returns the time the last key went up."""
        modifiers, key = win32.parse_combo(spec)
        if order == "together":
            win32.key_up(key, *reversed(modifiers))
        elif order == "key-first":
            win32.key_up(key)
            time.sleep(0.05)
            win32.key_up(*reversed(modifiers))
        elif order == "modifiers-held":
            win32.key_up(key)
            self.sleep(1.0)
            win32.key_up(*reversed(modifiers))
        elif order == "shift-late":
            win32.key_up(key)
            others = [m for m in modifiers if m != "shift"]
            if others:
                win32.key_up(*reversed(others))
            self.sleep(0.3)
            if "shift" in modifiers:
                win32.key_up("shift")
        else:
            raise ValueError(order)
        return time.time()

    def tap_hotkey(self, spec):
        self.press(spec)
        time.sleep(0.08)
        self.release(spec, "together")

    def dictate(self, fixture, hotkey=None, mode="hold", order="key-first", during=None):
        """One dictation of `fixture` (the session must run with its WAV). `during(ctx)` runs while
        the hotkey is still held (hold) or before the second press (toggle), e.g. to change focus.
        Returns the time the dictation was released/stopped."""
        hotkey = hotkey or self.hotkey
        speech = self.fixtures[fixture].speech_seconds
        if mode == "hold":
            self.press(hotkey)
            if not self.murmur.wait_state("listening", 5):
                self.release(hotkey, "together")
                raise Fail(f"Dictation did not start (overlay: {self.murmur.overlay_state()})")
            self.sleep(speech + HOLD_MARGIN)
            if during:
                during(self)
            return self.release(hotkey, order)
        self.tap_hotkey(hotkey)
        if not self.murmur.wait_state("listening", 5):
            raise Fail(f"Toggle dictation did not start (overlay: {self.murmur.overlay_state()})")
        self.sleep(speech + 0.3)
        if during:
            during(self)
        self.tap_hotkey(hotkey)
        return time.time()

    def settle(self, timeout=45):
        """Wait until the overlay leaves listening/transcribing."""
        return self.murmur.wait_state(("done", "idle", "held", "unknown"), timeout) or self.murmur.overlay_state()

    # ------------------------------------------------------------ goldens
    def golden(self, fixture, mode="hold", hotkey=None, style="prose", variant="", config=None):
        """Expected text for these settings; made by a first delivery into a fresh TargetApp."""
        hotkey = hotkey or self.hotkey
        key = golden.key(fixture, mode, hotkey, style, variant)
        if self.goldens.has(key):
            return self.goldens.get(key)
        settings = dict(config or {})
        general = dict(settings.get("general", {}))
        if mode != "hold":
            general["hotkey_mode"] = mode
        if style == "code":
            if hotkey == self.hotkey:
                raise ValueError("The code hotkey must differ from the dictation hotkey")
            general["hotkey_code"] = hotkey
        elif hotkey != self.hotkey:
            general["hotkey"] = hotkey
        settings["general"] = general
        self.murmur.ensure(self.fixtures[fixture].path, settings)
        app = self.target(f"golden-{fixture}-{style}{variant}", role="editor" if style == "code" else "prose")
        app.focus()
        self.dictate(fixture, hotkey=hotkey, mode=mode)
        text = app.wait_text()
        self.settle()
        app.close()
        if not text:
            raise Fail(f"No text reached TargetApp while recording the golden text for {fixture}")
        self.goldens.record(key, text)
        self.note(f"golden {fixture}/{mode}/{style}{('/' + variant) if variant else ''}: {text!r}")
        return text

    def expect(self, expected, actual, where):
        difference = golden.compare(expected, actual)
        if difference:
            raise Fail(f"{where}: {difference}")
