"""Speech fixtures: WAVs spoken by Windows' built-in offline SAPI voice (System.Speech).

Generated once on the desktop machine and cached by a hash of the text and settings. Each WAV is
16 kHz mono 16-bit PCM, padded with trailing silence (PAD_SECONDS) so the WAV never runs out
before the harness releases a hold-mode hotkey (MURMUR_QA_WAV ends a dictation when it runs out).
Toggle-mode checks press the hotkey again within a second of `speech_seconds` (silence would end
the dictation after audio.silence_timeout_ms). The WAV helpers are pure Python.
"""
from dataclasses import dataclass
import base64
import hashlib
from pathlib import Path
import subprocess
import wave

RATE = 16000
PAD_SECONDS = 20.0
VERSION = 1
SENTENCES = {
    # docs/parity/windows-checklist.md: sentence A and B.
    "A": "The quick brown fox jumps over the lazy dog.",
    "B": "Version two point one ships on Friday.",
    # Over 60 characters: Prefer paste takes the clipboard path (inject::choose_path).
    "LONG": "Please remember to water the garden plants every morning before the sun gets too hot.",
    # Contains "d" for F13 (hotkey Ctrl+Alt+D while the text is typed).
    "D": "Daniel delivered the old wooden desk today.",
    # I5: a Behavior replacement maps the spoken words "marker phrase" to "café déjà vu".
    "MARKER": "Please type the marker phrase now.",
}
REPLACEMENT = ("marker phrase", "café déjà vu")
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class Fixture:
    name: str
    text: str
    path: Path
    speech_seconds: float


def cache_name(name, text, pad=PAD_SECONDS):
    digest = hashlib.sha256(f"{VERSION}|{RATE}|{pad}|{text}".encode()).hexdigest()[:16]
    return f"{name}-{digest}.wav"


def duration(path):
    with wave.open(str(path), "rb") as reader:
        return reader.getnframes() / reader.getframerate()


def pad_silence(source, dest, seconds):
    """Copy a PCM WAV and append `seconds` of digital silence; returns the speech duration."""
    with wave.open(str(source), "rb") as reader:
        params = reader.getparams()
        frames = reader.readframes(reader.getnframes())
    if params.comptype != "NONE":
        raise ValueError("Only uncompressed PCM WAVs can be padded")
    silence = b"\x00" * (int(seconds * params.framerate) * params.sampwidth * params.nchannels)
    dest = Path(dest)
    temporary = dest.with_suffix(".tmp")
    with wave.open(str(temporary), "wb") as writer:
        writer.setparams(params)
        writer.writeframes(frames + silence)
    temporary.replace(dest)
    return params.nframes / params.framerate


def speak_script(text, path):
    """Windows PowerShell 5.1 script (System.Speech is .NET Framework only, not in pwsh 7)."""
    literal = text.replace("'", "''")
    target = str(path).replace("'", "''")
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "Add-Type -AssemblyName System.Speech\n"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        "$s.Rate = -1\n"
        f"$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo({RATE}, "
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)\n"
        f"$s.SetOutputToWaveFile('{target}', $f)\n"
        f"$s.Speak('{literal}')\n"
        "$s.Dispose()\n"
    )


def synthesize(text, path):
    """Speak `text` into `path` with the default SAPI voice (offline). -EncodedCommand avoids
    execution-policy prompts: the policy applies to script files, not to commands."""
    encoded = base64.b64encode(speak_script(text, path).encode("utf-16-le")).decode()
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, text=True, timeout=120, creationflags=CREATE_NO_WINDOW)
    if result.returncode or not Path(path).is_file():
        raise RuntimeError(f"SAPI synthesis failed: {result.stderr.strip()[-500:]}")


def build(cache, names=None, pad=PAD_SECONDS):
    """{name: Fixture}, generating missing WAVs into `cache`."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    fixtures = {}
    for name, text in SENTENCES.items():
        if names and name not in names:
            continue
        final = cache / cache_name(name, text, pad)
        speech_file = final.with_suffix(".speech.txt")
        if not final.is_file() or not speech_file.is_file():
            raw = cache / f"{name}.raw.wav"
            synthesize(text, raw)
            speech = pad_silence(raw, final, pad)
            raw.unlink()
            speech_file.write_text(f"{speech:.3f}\n")
        fixtures[name] = Fixture(name, text, final, float(speech_file.read_text()))
    return fixtures


def missing_wav(cache):
    """A path that does not exist: MURMUR_QA_WAV pointing here makes dictation fail (O1 error)."""
    return Path(cache) / "does-not-exist" / "missing.wav"
