"""Exact per-utterance comparison and first-run samples for semantic checks. Pure Python.

Model punctuation can change between separate inferences of the same WAV. The first TargetApp
sample is useful for fixture and privacy checks, never as an exact oracle for a later delivery.
"""
import re


# These signatures check that the fixture was recognized. They are deliberately separate from
# exact delivery comparison; punctuation, spacing and case are all retained there.
SIGNATURES = {
    "A": "the quick brown fox jumps over the lazy dog",
    "B": "version ships on friday",  # the engine may render "two point one" as 2.1
    "LONG": "please remember to water the garden plants every morning before the sun",
    "D": "daniel delivered the old wooden desk today",
    "MARKER": "please type now",  # the Behavior replacement changes the middle words
}


def fixture_fidelity(fixture, text):
    """Return an error when the output lacks the fixture's ordered word signature."""
    words = re.findall(r"[^\W_]+", text.lower(), flags=re.UNICODE)
    signature = SIGNATURES[fixture].split()
    if fixture in ("A", "D"):
        faithful = words == signature
    else:
        remaining = iter(words)
        faithful = all(any(word == wanted for word in remaining) for wanted in signature)
    if not faithful:
        return f"{fixture} fixture recognition lacked its ordered word signature: {text!r}"
    return None


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

    def as_dict(self):
        return {" / ".join(k for k in golden_key if k): text for golden_key, text in self._texts.items()}
