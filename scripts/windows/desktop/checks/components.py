"""C1, C3: remove the engine while idle; pause, quit, restart and resume a model download.

C3 downloads part of "Small (English)" (463 MiB) from Hugging Face: a network failure is reported
as a network failure, not as an app bug.
"""
import tomlconfig
import uia
import win32
from checks.common import badge
from context import Fail
from murmur import MurmurSession

MODEL = "Small (English)"


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
    overlay = win32.wait_for(lambda: [n for n in ctx.murmur.overlay_names() if "engine not installed" in n.lower()], 15,
                             0.5, sleep=ctx.sleep)
    if not overlay:
        ctx.press(ctx.hotkey)  # the overlay reports the missing engine when a dictation is attempted
        overlay = win32.wait_for(lambda: [n for n in ctx.murmur.overlay_names() if "engine not installed" in n.lower()],
                                 10, 0.25, sleep=ctx.sleep)
        ctx.release(ctx.hotkey, "together")
    ctx.screenshot(ctx.murmur.overlay_hwnd(), "overlay.png")
    if not overlay:
        raise Fail("The overlay does not say the speech engine is missing")


def _progress(window):
    bar = uia.try_find(window, name_=f"{MODEL} progress")
    if bar is None:
        return None
    try:
        return uia.range_value(bar)
    except Exception:  # noqa: BLE001 - indeterminate
        return None


def c3_pause_resume(ctx):
    config = tomlconfig.merge(ctx.murmur.base_config, {})
    session = MurmurSession(ctx.build, ctx.work, ctx.guard, config, name="c3")
    ctx.restorer.push("stop the C3 session", session.stop)
    if ctx.murmur.running():
        ctx.murmur.stop()
    session.install_engine()
    session.launch(wav=ctx.fixtures["A"].path, config={})
    window = session.page("Components")
    uia.invoke(uia.find(window, name_=f"Install {MODEL}", control_type_=uia.BUTTON, timeout=15, sleep=ctx.sleep))
    started = win32.wait_for(lambda: (v := _progress(session.settings())) and v > 0 and v, 120, 0.1, sleep=ctx.sleep)
    if not started:
        errors = [n for n in uia.names(session.settings()) if "fail" in n.lower()]
        raise Fail(f"network: the download did not start ({errors[0] if errors else 'no progress'})")
    uia.invoke(uia.find(session.settings(), name_=f"Pause {MODEL}", control_type_=uia.BUTTON, timeout=5, sleep=ctx.sleep))
    if win32.wait_for(lambda: badge(ctx, session.settings(), MODEL, 2) == "Paused", 15, 0.5, sleep=ctx.sleep) is None:
        raise Fail("Pause did not pause the download")
    uia.invoke(uia.find(session.settings(), name_=f"Resume {MODEL}", control_type_=uia.BUTTON, timeout=5, sleep=ctx.sleep))
    resumed = win32.wait_for(lambda: (v := _progress(session.settings())) and v > started and v, 60, 0.2,
                             sleep=ctx.sleep)
    if not resumed:
        raise Fail("Resume did not continue the download")
    uia.invoke(uia.find(session.settings(), name_=f"Pause {MODEL}", control_type_=uia.BUTTON, timeout=5, sleep=ctx.sleep))
    win32.wait_for(lambda: badge(ctx, session.settings(), MODEL, 2) == "Paused", 15, 0.5, sleep=ctx.sleep)
    session.stop()  # quit mid-download (paused)
    session.launch(wav=ctx.fixtures["A"].path)
    window = session.page("Components")
    state = badge(ctx, window, MODEL)
    ctx.screenshot(session.settings_hwnd(), "after-restart.png")
    if state != "Paused":
        raise Fail(f"After restart the download is {state!r}, not Paused")
    ctx.sleep(5.0)
    if badge(ctx, session.settings(), MODEL) != "Paused":
        raise Fail("The download resumed by itself after restart")
    uia.invoke(uia.find(session.settings(), name_=f"Resume {MODEL}", control_type_=uia.BUTTON, timeout=5, sleep=ctx.sleep))
    if not win32.wait_for(lambda: _progress(session.settings()) is not None, 30, 0.2, sleep=ctx.sleep):
        raise Fail("Resume after restart did not continue the download")
    try:
        uia.invoke(uia.find(session.settings(), name_=f"Cancel {MODEL}", control_type_=uia.BUTTON, timeout=5,
                            sleep=ctx.sleep))
    except Exception:  # noqa: BLE001 - finishing or already done; the profile is deleted anyway
        pass
    session.stop()
    ctx.note(f"progress at pause {started:.3f}, after resume {resumed:.3f}")
