"""MurmurSession: the app under test in a throwaway profile.

APPDATA and LOCALAPPDATA point into the run's work folder (platform::Paths::discover reads them),
so config.toml, murmur.log and installed components never touch the owner's profile. Launches
pass MURMUR_QA_WAV (a WAV replaces the microphone, release builds included), the development
trust root plus its acknowledgement (the CI build's manifest is signed by a throwaway key) and
MURMUR_LOG=debug and RUST_BACKTRACE=1. Murmur is single-instance, so preflight refuses to
run while any other murmur.exe exists. WebView2 keeps its own data folder under the real
%LOCALAPPDATA%.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

import tomlconfig
import uia
import win32

SCRIPTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))
import linux_components  # noqa: E402  (shared --install-component helpers)

SETTINGS_TITLE, OVERLAY_TITLE = "Murmur Settings", "Murmur Overlay"
MODEL, MODEL_ID = "tiny.en", "speech-tiny.en"
STATES = {"Listening": "listening", "Finding your words": "transcribing", "Done": "done", "mur": "idle",
          "Focus changed": "held"}


class StartupError(RuntimeError):
    """A launch cannot reach a required window; retain the session for diagnostics."""

    def __init__(self, message, session):
        super().__init__(message)
        self.session = session


class Build:
    """The verified hand-off copy (handoff.py) of one windows-build.yml run."""

    def __init__(self, root):
        self.root = Path(win32.long_path(str(root)))
        self.installer = self._one("Murmur_*_x64-setup.exe")
        self.portable = self._one("murmur-windows-x86_64.zip")
        self.engine = self._one("murmur-engine-windows-x86_64-*.zip")
        self.cuda = self._one("murmur-cuda-windows-x86_64-*.zip")
        self.manifest = self.root / linux_components.MANIFEST
        self.signature = self.root / linux_components.SIGNATURE
        self.certificate = self.root / linux_components.CERTIFICATE
        self.version = self.installer.name[len("Murmur_"):-len("_x64-setup.exe")]
        record = self.root / "build.json"
        self.record = json.loads(record.read_text()) if record.is_file() else {}

    def _one(self, pattern):
        found = sorted(self.root.glob(pattern))
        if len(found) != 1:
            raise RuntimeError(f"Expected one {pattern} in {self.root}, found {len(found)}")
        return found[0]

    def component(self, component_id):
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        for entry in data["components"]:
            if entry["id"] == component_id:
                return entry
        raise RuntimeError(f"{component_id} is not in the manifest")


def sha256(path):
    import hashlib
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch_model(build, cache, component_id=MODEL_ID):
    """Model folder for offline installs, downloaded once from the manifest's pinned URLs and
    checked against its SHA-256 (the app re-verifies on install)."""
    entry = build.component(component_id)
    folder = Path(cache) / component_id
    folder.mkdir(parents=True, exist_ok=True)
    for file in entry["files"]:
        target = folder / file["name"]
        if target.is_file() and target.stat().st_size == file["size_bytes"] and sha256(target) == file["sha256"]:
            continue
        partial = target.with_suffix(target.suffix + ".part")
        request = urllib.request.Request(file["urls"][0], headers={"User-Agent": "murmur-desktop-tests"})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out, 1 << 20)
        if sha256(partial) != file["sha256"]:
            partial.unlink()
            raise RuntimeError(f"{file['name']}: SHA-256 mismatch (network or mirror problem)")
        partial.replace(target)
    return folder


class MurmurSession:
    def __init__(self, build, work, guard, base_config, name="main"):
        self.build = build
        self.guard = guard
        self.work = Path(work)
        self.home = self.work / f"profile-{name}"
        self.app = self.work / "app" / "murmur.exe"
        self.base_config = base_config
        self.config = {}
        self.wav = None
        self.process = None
        self._log_offset = 0
        self._launch_count = 0
        self._launch_output = None

    # ------------------------------------------------------------ profile
    @property
    def appdata(self):
        return self.home / "Roaming"

    @property
    def localappdata(self):
        return self.home / "Local"

    @property
    def data(self):
        return self.localappdata / "Murmur"

    @property
    def log_path(self):
        return self.data / "murmur.log"

    @property
    def config_path(self):
        return self.appdata / "Murmur" / "config.toml"

    def prepare(self):
        if not self.app.is_file():
            self.app.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(self.build.portable) as portable:
                portable.extract("murmur.exe", self.app.parent)
        for folder in (self.appdata / "Murmur", self.data):
            folder.mkdir(parents=True, exist_ok=True)
        return self

    def env(self, wav=None, extra=None):
        env = dict(os.environ)
        env.update(APPDATA=str(self.appdata), LOCALAPPDATA=str(self.localappdata),
                   MURMUR_DEV_TRUST_ROOT=str(self.build.certificate),
                   MURMUR_DEV_TRUST_ROOT_ACK=linux_components.ACK, MURMUR_LOG="debug", RUST_BACKTRACE="1")
        env.pop("MURMUR_QA_WAV", None)
        if wav is not None:
            env["MURMUR_QA_WAV"] = str(wav)
        env.update(extra or {})
        return env

    def write_config(self, overrides=None):
        self.config = tomlconfig.merge(self.base_config, overrides or {})
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(tomlconfig.dumps(self.config), encoding="utf-8")

    def install(self, source):
        """Offline install through the app's own verifier (--install-component)."""
        self.prepare()
        return linux_components.install(self.app, self.build.root, source, self.env())

    def install_engine(self):
        return self.install(self.build.engine)

    def installed(self, component_id):
        try:
            return linux_components.active(self.env(), component_id).exists()
        except (OSError, ValueError, KeyError):
            return False

    # ------------------------------------------------------------ process
    def running(self):
        return self.process is not None and self.process.poll() is None

    def launch(self, wav=None, config=None, args=(), extra_env=None, wait_ready=True, keep_settings=False):
        """Start Murmur; returns once the overlay window exists. A normal launch opens Settings;
        it is hidden again (its close button hides it) unless keep_settings, so it never covers
        the target windows. settings() reopens it on demand."""
        self.prepare()
        if config is not None:
            self.write_config(config)
        self.wav = wav
        self._log_offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        self._launch_count += 1
        self._launch_output = self.home / f"murmur-launch-{self._launch_count}.txt"
        with self._launch_output.open("wb") as output:
            try:
                self.process = subprocess.Popen([str(self.app), *args], env=self.env(wav, extra_env),
                                                cwd=str(self.work), stdout=output, stderr=subprocess.STDOUT)
            except OSError as error:
                raise StartupError(f"Murmur could not launch: {error}", self) from error
        if wait_ready:
            self._wait_for_window(self.overlay_hwnd, "overlay")
            if "--background" not in args:
                hwnd = self._wait_for_window(self.settings_hwnd, "Settings window")
                if not keep_settings:
                    self.guard.sleep(1.0)
                    win32.close_window(hwnd)
                    win32.wait_for(lambda: not self.settings_hwnd(), 5, sleep=self.guard.sleep)
            self.guard.sleep(1.5)  # engine and hotkeys come up
        return self

    def _wait_for_window(self, find, label):
        def ready_or_exited():
            hwnd = find()
            if hwnd:
                return hwnd
            code = self.process.poll()
            if code is not None:
                raise StartupError(f"Murmur exited before its {label} appeared (exit code {code})", self)
            return 0

        hwnd = win32.wait_for(ready_or_exited, 40, sleep=self.guard.sleep)
        if not hwnd:
            raise StartupError(f"Murmur did not show its {label} (exit code {self.process.poll()})", self)
        return hwnd

    def save_startup_diagnostics(self, artifact):
        """Copy only this test process's launch evidence before its profile is removed."""
        pid = self.process.pid if self.process else None
        code = self.process.poll() if self.process else None
        lines = [f"pid: {pid}", f"exit code: {code}", "top-level windows for this pid:"]
        try:
            windows = [w for w in win32.enum_windows(visible_only=False) if w[1] == pid] if pid else []
            lines.extend(
                f"  hwnd={hwnd} title={title!r} class={class_name!r} "
                f"visible={bool(win32.user32.IsWindowVisible(hwnd))}"
                for hwnd, _, title, class_name in windows
            )
            if not windows:
                lines.append("  none")
        except Exception as error:  # noqa: BLE001 - preserve the original startup failure
            lines.append(f"  enumeration failed: {type(error).__name__}: {error}")
        artifact("startup-windows.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        for suffix, path in (("startup-output.txt", self._launch_output),
                             ("startup-log.txt", self.log_path)):
            evidence = artifact(suffix)
            if not path or not path.is_file():
                evidence.write_text("No file was created for this launch.\n", encoding="utf-8")
                continue
            try:
                data = path.read_bytes()
                if suffix == "startup-log.txt":
                    data = data[self._log_offset:]
                # Keep the log's newline bytes; Windows text writes would turn CRLF into CRCRLF.
                evidence.write_bytes(data.decode("utf-8", errors="replace").encode("utf-8"))
            except OSError as error:
                evidence.write_text(f"Could not read {path.name}: {error}\n", encoding="utf-8")

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                win32.kill(self.process.pid)
                try:
                    self.process.wait(15)
                except subprocess.TimeoutExpired:
                    pass
            self.process = None
        # An engine orphaned by the kill above: only ones started from this session's profile.
        mine = str(self.home.resolve()).lower()
        for pid in win32.pids("murmur-engine.exe"):
            path = win32.process_path(pid)
            if path and path.lower().startswith(mine):
                win32.kill(pid)

    def restart(self, wav=None, config=None, args=()):
        self.stop()
        self.guard.sleep(0.5)
        return self.launch(wav=wav if wav is not None else self.wav, config=config, args=args)

    def ensure(self, wav, config=None):
        """Running with this WAV and config. A different WAV needs a restart (the QA WAV is read
        once when audio opens); a config change is written and picked up by the file watcher."""
        merged = tomlconfig.merge(self.base_config, config or {})
        if not self.running() or self.wav != wav:
            return self.restart(wav=wav, config=config or {})
        if merged != self.config:
            hotkeys_changed = any(merged["general"].get(k) != self.config.get("general", {}).get(k)
                                  for k in ("hotkey", "hotkey_code", "hotkey_mode"))
            if hotkeys_changed:
                return self.restart(wav=wav, config=config or {})
            self.write_config(config or {})
            self.guard.sleep(2.0)
        return self

    # ------------------------------------------------------------ windows
    def _window(self, title):
        if not self.running():
            return 0
        return next(iter(win32.find_windows(title=title, pid=self.process.pid, visible_only=False)), 0)

    def overlay_hwnd(self):
        return self._window(OVERLAY_TITLE)

    def settings_hwnd(self):
        hwnd = self._window(SETTINGS_TITLE)
        return hwnd if hwnd and win32.user32.IsWindowVisible(hwnd) else 0

    def open_settings(self, timeout=20):
        """A second launch hands over to the running instance, which shows Settings (T3 path)."""
        subprocess.run([str(self.app)], env=self.env(self.wav), timeout=30)
        hwnd = win32.wait_for(self.settings_hwnd, timeout, sleep=self.guard.sleep)
        if not hwnd:
            raise RuntimeError("The Settings window did not appear")
        return uia.from_handle(hwnd)

    def settings(self, timeout=20):
        """The Settings window (UIA element), shown first when it is hidden."""
        hwnd = self.settings_hwnd()
        if not hwnd:
            return self.open_settings(timeout)
        return uia.from_handle(hwnd)

    def show_settings(self):
        """Handle of the visible Settings window (opened when hidden)."""
        self.settings()
        return self.settings_hwnd()

    def overlay(self):
        hwnd = self.overlay_hwnd()
        if not hwnd:
            raise RuntimeError("No overlay window")
        return uia.from_handle(hwnd)

    def overlay_names(self):
        try:
            return uia.names(self.overlay())
        except Exception:  # noqa: BLE001 - overlay rebuilt mid-query
            return []

    def overlay_state(self):
        """listening / transcribing / done / idle / held / error:<text> / unknown, from the pill
        label (overlay.ts) as exposed through UI Automation."""
        names = self.overlay_names()
        if "Not inserted: focus changed" in names:
            return "held"
        for text in names:
            if text in STATES:
                return STATES[text]
        return "unknown" if not names else "other:" + " | ".join(names[:6])

    def wait_state(self, wanted, timeout=20):
        wanted = (wanted,) if isinstance(wanted, str) else tuple(wanted)
        return win32.wait_for(lambda: (state := self.overlay_state()) in wanted and state, timeout, 0.05,
                              sleep=self.guard.sleep)

    def page(self, name, settle=0.8):
        """Open a Settings page by its button (General, Components, Audio, Behavior, Privacy, Companion)."""
        window = self.settings()
        uia.invoke(uia.find(window, name_=name, control_type_=uia.BUTTON, timeout=15, sleep=self.guard.sleep))
        self.guard.sleep(settle)
        return window

    # ------------------------------------------------------------ log
    def log(self, since_launch=False):
        if not self.log_path.exists():
            return ""
        with self.log_path.open("rb") as stream:
            if since_launch:
                stream.seek(self._log_offset)
            return stream.read().decode("utf-8", errors="replace")
