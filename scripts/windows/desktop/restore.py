"""State-restore bookkeeping. Pure Python (the registry backend is injected).

Anything a check changes outside its throwaway profile (clipboard, theme, HKCU Run entry,
microphone consent, processes it started) is registered here *before* the change. run.py calls
restore_all() in `finally`, so a failed check, an exception or real user input still puts the
machine back. Undo actions run newest first; one failing undo does not stop the others.
"""


class Restorer:
    def __init__(self, log=print):
        self._stack = []
        self._log = log

    def push(self, label, undo):
        """Register `undo` (no arguments). Returns a token for discard()."""
        token = object()
        self._stack.append((token, label, undo))
        return token

    def discard(self, token):
        """Drop an undo that is no longer needed (the check restored the state itself)."""
        self._stack = [entry for entry in self._stack if entry[0] is not token]

    def run(self, token):
        """Run one registered undo now and drop it."""
        for entry in list(self._stack):
            if entry[0] is token:
                self._stack.remove(entry)
                entry[2]()
                return
        raise KeyError("Unknown restore token")

    @property
    def pending(self):
        return [label for _, label, _ in self._stack]

    def mark(self):
        """Stack depth, for restore_to() at the end of a single check."""
        return len(self._stack)

    def restore_to(self, mark):
        return self._unwind(lambda: len(self._stack) > mark)

    def restore_all(self):
        """Run every pending undo; returns [(label, error message)] for undos that failed."""
        return self._unwind(lambda: bool(self._stack))

    def _unwind(self, more):
        errors = []
        while more():
            _, label, undo = self._stack.pop()
            try:
                undo()
            except Exception as error:  # noqa: BLE001 - every undo must get its chance
                errors.append((label, f"{type(error).__name__}: {error}"))
                self._log(f"Restore failed: {label}: {error}")
        return errors


MISSING = object()


class RegistryValue:
    """Snapshot of one registry value with set/delete that restores the snapshot on undo.

    `backend` provides read(path, name) -> (value, kind) or MISSING, write(path, name, value, kind)
    and delete(path, name). Paths are HKCU-relative.
    """

    def __init__(self, backend, path, name):
        self.backend, self.path, self.name = backend, path, name
        self.original = backend.read(path, name)

    def undo(self):
        if self.original is MISSING:
            if self.backend.read(self.path, self.name) is not MISSING:
                self.backend.delete(self.path, self.name)
        else:
            value, kind = self.original
            self.backend.write(self.path, self.name, value, kind)

    def set(self, restorer, value, kind):
        restorer.push(f"registry HKCU\\{self.path}\\{self.name}", self.undo)
        self.backend.write(self.path, self.name, value, kind)

    def watch(self, restorer):
        """Register the undo without changing anything (the app under test will change it)."""
        restorer.push(f"registry HKCU\\{self.path}\\{self.name}", self.undo)
