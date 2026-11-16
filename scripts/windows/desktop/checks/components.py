"""C1, C3: remove the engine while idle; pause, quit, restart and resume a model download.

C3 downloads part of "Small (English)" (463 MiB) from Hugging Face: a network failure is reported
as a network failure, not as an app bug.
"""
import json
from pathlib import Path

import tomlconfig
import uia
import win32
from checks.common import badge
from context import Fail
from murmur import MurmurSession

MODEL = "Small (English)"
MODEL_ID = "speech-small.en"


def _confirm(ctx):
    """Accept the window.confirm() dialog WebView2 shows for Remove."""
    button = win32.wait_for(lambda: uia.try_find(uia.root(), name_="OK", control_type_=uia.BUTTON), 5,
                            sleep=ctx.sleep)
    if button is None:
        raise Fail("The Remove confirmation dialog did not appear")
    uia.invoke(button)


def c1_remove_engine(ctx):
    ctx.murmur.ensure(ctx.fixtures["A"].path)

    def reinstall():
        ctx.murmur.stop()
        ctx.murmur.install_engine()
        ctx.murmur.config = {}

    ctx.restorer.push("reinstall the engine", reinstall)
    window = ctx.murmur.page("Components")
    uia.invoke(uia.find(window, name_="Remove Speech engine", control_type_=uia.BUTTON, timeout=10, sleep=ctx.sleep))
    _confirm(ctx)
    state = win32.wait_for(lambda: (b := badge(ctx, ctx.murmur.settings(), "Speech engine", timeout=2)) == "Not installed"
                           and b, 60, 0.5, sleep=ctx.sleep)
    names = uia.names(ctx.murmur.settings())
    ctx.screenshot(ctx.murmur.settings_hwnd(), "remove-engine.png")
    in_use = [n for n in names if "in use" in n.lower()]
    if in_use:
        raise Fail(f"Remove reported a file in use: {in_use[0]}")
    if not state:
        raise Fail("The engine is not 'Not installed' after Remove")
    observed = []

    def missing_engine():
        names = ctx.murmur.overlay_names()
        if not observed or names != observed[-1]:
            observed.append(names)
        return [n for n in names if "engine not installed" in n.lower()]

    overlay = win32.wait_for(missing_engine, 15, 0.5, sleep=ctx.sleep)
    if not overlay:
        ctx.press(ctx.hotkey)  # the overlay reports the missing engine when a dictation is attempted
        # Hold mode remains Listening until key-up. Waiting for an engine failure while the
        # key is still held mistakes a normal capture for missing error feedback.
        overlay = win32.wait_for(missing_engine, 2, 0.25, sleep=ctx.sleep)
        ctx.release(ctx.hotkey, "together")
        if not overlay:
            overlay = win32.wait_for(missing_engine, 8, 0.25, sleep=ctx.sleep)
    ctx.artifact("overlay-names.txt").write_text(
        "\n".join(f"snapshot {index}: {names!r}" for index, names in enumerate(observed, 1)) + "\n",
        encoding="utf-8")
    ctx.screenshot(ctx.murmur.overlay_hwnd(), "overlay.png")
    if not overlay:
        ctx.artifact("missing-engine-diagnostic.txt").write_text(
            f"overlay state: {ctx.murmur.overlay_state()}\n"
            f"settings engine notices: {[n for n in names if 'engine' in n.lower()][:20]!r}\n"
            f"log lines: {[line for line in ctx.murmur.log().splitlines() if 'engine' in line.lower() or 'component' in line.lower()][-30:]!r}\n",
            encoding="utf-8")
        raise Fail("The overlay does not say the speech engine is missing")


def _download_state(session, component, allow_absent=False):
    """Read the exact signed component's persisted job and consumed download bytes."""
    root = session.data / "components"
    path = root / "jobs.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))["jobs"][MODEL_ID]
    except (FileNotFoundError, KeyError) as error:
        if allow_absent:
            return None
        raise Fail(f"C3 has no readable {MODEL_ID} job record: {error}") from error
    except (OSError, ValueError, TypeError) as error:
        raise Fail(f"C3 has no readable {MODEL_ID} job record: {error}") from error
    if not isinstance(record, dict):
        raise Fail(f"C3 {MODEL_ID} job record is not an object")
    if record.get("id") != MODEL_ID or record.get("version") != component["version"]:
        raise Fail(f"C3 job identity differs from the signed {MODEL_ID} version")
    base = root / "downloads" / f"{MODEL_ID}-{component['version']}"
    if component["archive"] == "zip":
        paths = [Path(f"{base}.part")]
    elif component["archive"] == "none":
        paths = [base / file["name"] for file in component["files"]]
        paths = [path if path.is_file() else Path(f"{path}.part") for path in paths]
    else:
        raise Fail(f"C3 has unsupported archive type {component['archive']!r}")
    try:
        consumed = sum(path.stat().st_size if path.is_file() else 0 for path in paths)
    except OSError as error:
        raise Fail(f"C3 cannot read consumed download bytes: {error}") from error
    try:
        return record["phase"], consumed, record["bytes_done"]
    except KeyError as error:
        raise Fail(f"C3 job record lacks {error.args[0]}") from error


def _paused_bytes(session, component):
    phase, consumed, recorded = _download_state(session, component)
    if phase != "paused" or consumed <= 0 or recorded != consumed:
        raise Fail(f"C3 paused job is inconsistent: phase={phase!r}, file={consumed}, record={recorded}")
    return consumed


def _resumed_after_restart(session, component, baseline, ctx):
    """Only actual new bytes or a verified install prove Resume continued."""
    window = session.settings()
    if badge(ctx, window, MODEL, 0) in ("Installed", "Active") and session.installed(MODEL_ID):
        return ("completed", None)
    state = _download_state(session, component, allow_absent=True)
    if state is None:
        return None
    phase, consumed, _ = state
    if phase in ("queued", "downloading") and consumed > baseline:
        return ("advanced", consumed)
    return None


def c3_pause_resume(ctx):
    component = ctx.build.component(MODEL_ID)
    config = tomlconfig.merge(ctx.murmur.base_config, {})
    session = MurmurSession(ctx.build, ctx.work, ctx.guard, config, name="c3")
    ctx.restorer.push("stop the C3 session", session.stop)
    if ctx.murmur.running():
        ctx.murmur.stop()
    session.install_engine()
    session.launch(wav=ctx.fixtures["A"].path, config={})
    window = session.page("Components")
    uia.invoke(uia.find(window, name_=f"Install {MODEL}", control_type_=uia.BUTTON, timeout=15, sleep=ctx.sleep))
    started = win32.wait_for(lambda: (state := _download_state(session, component, allow_absent=True))
                             and state[1] > 0 and state[1],
                             120, 0.1, sleep=ctx.sleep)
    if not started:
        errors = [n for n in uia.names(session.settings()) if "fail" in n.lower()]
        raise Fail(f"network: the download did not start ({errors[0] if errors else 'no progress'})")
    uia.invoke(uia.find(session.settings(), name_=f"Pause {MODEL}", control_type_=uia.BUTTON, timeout=5, sleep=ctx.sleep))
    if win32.wait_for(lambda: badge(ctx, session.settings(), MODEL, 2) == "Paused", 15, 0.5, sleep=ctx.sleep) is None:
        raise Fail("Pause did not pause the download")
    first_pause = _paused_bytes(session, component)
    uia.invoke(uia.find(session.settings(), name_=f"Resume {MODEL}", control_type_=uia.BUTTON, timeout=5, sleep=ctx.sleep))
    resumed = win32.wait_for(lambda: (state := _download_state(session, component, allow_absent=True))
                             and state[1] > first_pause and state[1], 60, 0.2,
                             sleep=ctx.sleep)
    if not resumed:
        raise Fail("Resume did not continue the download")
    uia.invoke(uia.find(session.settings(), name_=f"Pause {MODEL}", control_type_=uia.BUTTON, timeout=5, sleep=ctx.sleep))
    if not win32.wait_for(lambda: badge(ctx, session.settings(), MODEL, 2) == "Paused", 15, 0.5,
                          sleep=ctx.sleep):
        raise Fail("The second Pause did not pause the download")
    baseline = _paused_bytes(session, component)
    lifecycle = ctx.artifact("notepad-lifecycle.json")
    before_stop = ctx.notepad_ownership_snapshot()
    try:
        session.stop()  # quit mid-download (paused)
    finally:
        lifecycle.write_text(json.dumps({"before_c3_stop": before_stop,
                                         "after_c3_stop": ctx.notepad_ownership_snapshot()}, indent=2),
                             encoding="utf-8")
    session.launch(wav=ctx.fixtures["A"].path)
    window = session.page("Components")
    state = badge(ctx, window, MODEL)
    ctx.screenshot(session.settings_hwnd(), "after-restart.png")
    if state != "Paused":
        raise Fail(f"After restart the download is {state!r}, not Paused")
    restart_progress = _paused_bytes(session, component)
    if restart_progress != baseline:
        raise Fail(f"Paused progress did not persist: before restart {baseline!r}, after {restart_progress!r}")
    ctx.sleep(5.0)
    if badge(ctx, session.settings(), MODEL) != "Paused":
        raise Fail("The download resumed by itself after restart")
    persisted = _paused_bytes(session, component)
    if persisted != restart_progress:
        raise Fail(f"Paused progress changed without Resume: {restart_progress!r} -> {persisted!r}")
    uia.invoke(uia.find(session.settings(), name_=f"Resume {MODEL}", control_type_=uia.BUTTON, timeout=5, sleep=ctx.sleep))
    outcome = win32.wait_for(lambda: _resumed_after_restart(session, component, persisted, ctx), 30, 0.2,
                             sleep=ctx.sleep)
    if not outcome:
        raise Fail(f"Resume after restart stalled at {persisted!r}; no progress increase or verified install")
    try:
        uia.invoke(uia.find(session.settings(), name_=f"Cancel {MODEL}", control_type_=uia.BUTTON, timeout=5,
                            sleep=ctx.sleep))
    except Exception:  # noqa: BLE001 - finishing or already done; the profile is deleted anyway
        pass
    session.stop()
    ctx.note(f"consumed bytes start {started!r}, first pause {first_pause!r}, first resume {resumed!r}, "
             f"persisted pause {persisted!r}, after restart Resume {outcome!r}")
