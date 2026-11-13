"""Golden texts without hard-coding model output. Pure Python.

The expected text for a fixture is whatever its first delivery into TargetApp produced in this
run under the same settings (hotkey, mode, style and any rule that changes the text). Every later
delivery under the same settings must match it exactly, character for character.
"""


def key(fixture, mode="hold", hotkey="", style="prose", variant=""):
    """Settings that may change the delivered text. Delivery paths (type/paste) are not part of
    the key on purpose: the same text must arrive whichever path delivered it."""
    return (fixture, mode, hotkey.lower(), style, variant)


def compare(expected, actual):
    """None when equal, else a short description of the first difference."""
    if expected == actual:
        return None
    if actual is None:
        return f"nothing delivered (expected {expected!r})"
    limit = min(len(expected), len(actual))
    index = next((i for i in range(limit) if expected[i] != actual[i]), limit)
    if actual.startswith(expected) and len(actual) >= 2 * len(expected) and expected:
        return f"delivered more than once: {actual!r}"
    window = slice(max(0, index - 10), index + 20)
    return (f"differs at character {index}: expected {expected[window]!r}, got {actual[window]!r} "
            f"(lengths {len(expected)} vs {len(actual)})")


class GoldenStore:
    def __init__(self):
        self._texts = {}

    def has(self, golden_key):
        return golden_key in self._texts

    def get(self, golden_key):
        return self._texts.get(golden_key)

    def record(self, golden_key, text):
        """Store the first delivery; empty deliveries are refused (no golden from a failure)."""
        if not text or not text.strip():
            raise ValueError(f"Empty delivery cannot be a golden text for {golden_key}")
        if golden_key in self._texts:
            raise ValueError(f"Golden text for {golden_key} already recorded")
        self._texts[golden_key] = text

    def check(self, golden_key, text):
        """None when `text` matches the recorded golden text, else the difference."""
        if golden_key not in self._texts:
            raise KeyError(f"No golden text for {golden_key}")
        return compare(self._texts[golden_key], text)

    def texts(self):
        """Every golden text (for the log scan: none may appear in murmur.log)."""
        return list(self._texts.values())

    def as_dict(self):
        return {" / ".join(k for k in golden_key if k): text for golden_key, text in self._texts.items()}
