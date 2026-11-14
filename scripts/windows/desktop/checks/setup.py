"""P1-P4: environment, installer, first-run setup, Components page."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import time
try:
    import winreg
except ImportError:  # not Windows: the module still imports for the unit tests
    winreg = None

import tomlconfig
import uia
import win32
from checks.common import SIZE, badge, wait_names
from context import Fail, Skip
from murmur import MODEL_ID, MurmurSession

SCRIPTS = Path(__file__).resolve().parents[3]
UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
WEBVIEW2 = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def _hklm(path, name):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _run(arguments, timeout=60):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout,
                                creationflags=win32.CREATE_NO_WINDOW)
        return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"unavailable ({error})"


def p1_environment(ctx):
    current = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    version = sys.getwindowsversion()
    gpu = _run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"]) \
        if Path(r"C:\Windows\System32\nvidia-smi.exe").exists() else ""
    if not gpu or gpu.startswith("unavailable"):
        gpu = _run(["powershell.exe", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_VideoController | ForEach-Object { \"$($_.Name), driver $($_.DriverVersion)\" }"])
    lines = [
        f"Windows: {_hklm(current, 'ProductName')} {_hklm(current, 'DisplayVersion')} "
        f"build {version.build}.{_hklm(current, 'UBR')}",
        f"WebView2: {_hklm(WEBVIEW2, 'pv') or 'not found under HKLM (per-user install?)'}",
        f"GPU: {gpu}",
        *[f"Monitor {m['device']}: {m['rect'][2] - m['rect'][0]}x{m['rect'][3] - m['rect'][1]} at {m['scale']} %"
          f"{' (primary)' if m['primary'] else ''}" for m in win32.monitors()],
        f"Murmur: {_run([str(ctx.murmur.app), '--version'])}",
        f"Commit: {ctx.build.record.get('sha', 'unknown')}",
        f"Build run: {ctx.build.record.get('build_run', {}).get('url', 'unknown')}",
    ]
    path = ctx.artifact("environment.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ctx.note("; ".join(lines[:2]))


def p2_install(ctx):
    registry = win32.Registry()
    existing = [name for name in registry.subkeys(UNINSTALL)
                if str(registry.values(f"{UNINSTALL}\\{name}").get("DisplayName", "")).startswith("Murmur")]
    if existing:
        raise Skip("Murmur is installed for this account; a silent test install would overwrite its Start menu "
                   "and uninstall entries (uninstall it or run P2 on a clean account)")
    target = Path(win32.long_path(str(ctx.work / "install")))
    uninstaller = target / "uninstall.exe"

    def uninstall():
        if uninstaller.exists():
            subprocess.run(f'"{uninstaller}" /S', timeout=120)
            win32.wait_for(lambda: not (target / "murmur.exe").exists(), 60)

    ctx.restorer.push("uninstall the P2 test install", uninstall)
    # NSIS: /D must be last and unquoted, so the command line is built by hand.
    process = subprocess.Popen(f'"{ctx.build.installer}" /S /D={target}')
    deadline = time.monotonic() + 300
    prompted = False
    while process.poll() is None and time.monotonic() < deadline:
        if win32.input_desktop() != "Default":
            prompted = True
        ctx.sleep(0.25)
    if process.poll() is None:
        process.kill()
        raise Fail("Installer did not finish within 5 minutes" + (" (a secure-desktop prompt appeared)" if prompted else ""))
    if prompted:
        raise Fail("A secure-desktop (UAC) prompt appeared during the per-user install")
    if process.returncode != 0:
        raise Fail(f"Installer exited with {process.returncode}")
    app = target / "murmur.exe"
    if not app.is_file():
        raise Fail(f"murmur.exe missing after install in {target}")
    start_menu = Path(win32.long_path(str(Path.home() / r"AppData\Roaming\Microsoft\Windows\Start Menu\Programs")))
    shortcuts = sorted(p.name for p in start_menu.rglob("Murmur*.lnk"))
    ctx.artifact("install-path.txt").write_text(f"{target}\nStart menu: {shortcuts}\n", encoding="utf-8")
    ctx.note(f"Start menu entries: {shortcuts or 'none found'}")


def _embeds_manifest(app):
    spec = importlib.util.spec_from_file_location("check_embedded_manifest", SCRIPTS / "check-embedded-manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return bool(module.embedded_signatures(Path(app).read_bytes()))


def _radio(window, ctx, text):
    element = uia.find(window, contains=text, control_type_=uia.RADIOBUTTON, timeout=15, sleep=ctx.sleep)
    try:
        uia.select(element)
    except Exception:  # noqa: BLE001 - fall back to a click
        win32.click(*uia.center(element))
    ctx.sleep(0.4)


def _button(window, ctx, label, timeout=15):
    uia.invoke(uia.find(window, name_=label, control_type_=uia.BUTTON, timeout=timeout, sleep=ctx.sleep))
    ctx.sleep(0.6)


def p3_first_run(ctx):
    ctx.murmur.prepare()
    if not _embeds_manifest(ctx.murmur.app):
        raise Skip("this build embeds no component manifest (release-tag build): setup has no catalog")
    base = tomlconfig.merge(ctx.murmur.base_config, {"setup": {"postponed": False}})
    base["engine"] = {"device": "cpu"}  # no preselected model: the wizard chooses
    session = MurmurSession(ctx.build, ctx.work, ctx.guard, base, name="p3")
    ctx.restorer.push("stop the P3 session", session.stop)
    if ctx.murmur.running():
        ctx.murmur.stop()
    session.install_engine()  # the manifest's engine URL points at a release that does not exist
    session.launch(wav=ctx.fixtures["A"].path, config={}, keep_settings=True)
    window = session.settings()
    try:
        uia.find(window, name_="Welcome", timeout=30, sleep=ctx.sleep)
    except uia.NotFound:
        ctx.screenshot(session.settings_hwnd(), "missing-welcome.png")
        raise
    _button(window, ctx, "Next")
    _radio(window, ctx, "English only")
    _button(window, ctx, "Next")
    _radio(window, ctx, "Tiny (English)")
    _button(window, ctx, "Next")
    gpu = uia.find_all(window, contains="NVIDIA", control_type_=uia.RADIOBUTTON)
    if gpu:
        raise Fail("The GPU option is offered although Windows GPU support is not validated yet")
    _radio(window, ctx, "CPU")
    _button(window, ctx, "Next")
    names = uia.names(window)
    sizes = [n for n in names if SIZE.search(n)]
    ctx.screenshot(session.settings_hwnd(), "setup-summary.png")
    if not sizes:
        raise Fail("The summary shows no download size before downloading")
    ctx.note(f"tiny.en instead of the checklist's distil-small.en (speed); summary: {sizes[0]!r}")
    _button(window, ctx, "Download and install")
    progress, done = [], None
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        names = uia.names(window)
        errors = [n for n in names if "fail" in n.lower() or "could not" in n.lower()]
        if errors:
            raise Fail(f"network or install error (not necessarily an app bug): {errors[0]}")
        if any(n.startswith("All set.") for n in names):
            done = names
            break
        bar = uia.try_find(window, name_="Tiny (English) progress")
        if bar is not None:
            try:
                progress.append(round(uia.range_value(bar), 3))
            except Exception:  # noqa: BLE001 - indeterminate progress has no value
                pass
        ctx.sleep(0.25)
    ctx.screenshot(session.settings_hwnd(), "setup-done.png")
    if not done:
        raise Fail("Setup did not finish within 15 minutes")
    distinct = sorted(set(progress))
    if len(distinct) < 2:
        ctx.note(f"progress values seen: {distinct} (download too fast to observe advancing)")
        raise Fail("Progress was not observed advancing")
    _button(window, ctx, "Done")
    if not session.installed(MODEL_ID):
        raise Fail("tiny.en is not active after setup")
    session.stop()


def p4_components(ctx):
    ctx.guard.mark("P4:ensure:begin")
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    ctx.guard.mark("P4:ensure:end")
    window = ctx.murmur.page("Components")
    ctx.guard.mark("P4:components:open")
    for name in ("Speech engine", "Tiny (English)"):
        state = badge(ctx, window, name)
        ctx.guard.mark(f"P4:{name}:badge")
        if state != "Active":
            raise Fail(f"{name} is {state!r}, not Active")
        uia.invoke(uia.find(window, name_=f"Verify {name}", control_type_=uia.BUTTON, timeout=10, sleep=ctx.sleep))
        ctx.guard.mark(f"P4:{name}:verify-invoked")
        names = wait_names(ctx, lambda: ctx.murmur.settings(), lambda n: _component_verified(n, name)
                           or any(x.startswith("Verification failed") for x in n), 120)
        if names is None or not _component_verified(names, name):
            seen = names if names is not None else uia.names(ctx.murmur.settings())
            ctx.artifact(f"verify-{name.lower().replace(' ', '-')}-diagnostic.txt").write_text(
                f"component: {name}\nverdict names: {seen!r}\n"
                f"component log: {[line for line in ctx.murmur.log().splitlines() if 'verif' in line.lower()][-20:]!r}\n",
                encoding="utf-8")
            ctx.screenshot(ctx.murmur.settings_hwnd(), f"verify-{name.lower().replace(' ', '-')}.png")
            raise Fail(f"Verify did not report 'Files verified.' for {name}")
    ctx.guard.mark("P4:verification:finished")
    ctx.screenshot(ctx.murmur.settings_hwnd(), "components.png")


def _component_verified(names, display_name):
    """Check the note in this component row, not a note left by an earlier Verify."""
    try:
        action = names.index(f"Verify {display_name}")
        title = max(i for i, name in enumerate(names[:action]) if name == display_name)
    except (ValueError, TypeError):
        return False
    return "Files verified." in names[title:action]
