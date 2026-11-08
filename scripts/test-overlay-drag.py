#!/usr/bin/env python3
"""Exercise a built Murmur overlay with real pointer input in isolated GNOME/X11.

Requires Xvfb, GNOME Shell, xdotool, xprop and /usr/bin/python3 with GTK 3.
Pass a self-contained executable (not a dev build expecting a Vite server).
This validates native movement/focus/persistence, not speech or Windows behavior.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

BUS_CONFIG = '''<busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen>
<auth>EXTERNAL</auth><policy context="default"><allow send_destination="*"/>
<allow receive_sender="*"/><allow own="*"/></policy></busconfig>'''
TARGET = '''import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
w = Gtk.Window(title="Murmur drag test target")
w.set_default_size(350, 100)
w.move(100, 100)
w.add(Gtk.Entry(text="Synthetic focus target"))
w.connect("destroy", Gtk.main_quit)
w.show_all()
Gtk.main()
'''


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=10).strip()


def wait_for(check, message, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError(message)


def window_named(name):
    result = subprocess.run(['xdotool', 'search', '--onlyvisible', '--name', '^' + name + '$'],
                            capture_output=True, text=True, timeout=5)
    # Mutter's decoration windows share the client's title. Select the actual
    # managed client, which owns WM_STATE, for focus comparisons.
    if result.returncode == 0:
        for window in result.stdout.splitlines():
            if 'window state:' in run('xprop', '-id', window, 'WM_STATE'):
                return window
    return None


def active_window():
    result = subprocess.run(['xdotool', 'getactivewindow'], capture_output=True, text=True, timeout=5)
    return result.stdout.strip() if result.returncode == 0 else None


def geometry(window):
    return {key: int(value) for key, value in
            (line.split('=', 1) for line in run('xdotool', 'getwindowgeometry', '--shell', window).splitlines())}


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def exercise(executable, evidence):
    processes = []

    def spawn(args, name):
        with (evidence / (name + '.log')).open('w') as log:
            process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        processes.append(process)
        return process

    config = Path(os.environ['XDG_CONFIG_HOME']) / 'murmur/config.toml'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[general]\nautostart=false\n[overlay]\nidle_view="pill"\n'
                      '[sounds]\nenabled=false\n[companion]\nsource="phone"\n')

    def prefs():
        return tomllib.loads(config.read_text())['overlay']

    def update_overlay(key, value):
        text = config.read_text()
        start = text.index('[overlay]') + len('[overlay]')
        end = text.find('\n[', start)
        if end < 0:
            end = len(text)
        section = text[start:end]
        line = key + ' = ' + json.dumps(value)
        if re.search(r'^' + key + r'\s*=', section, re.M):
            section = re.sub(r'^' + key + r'\s*=.*$', line, section, flags=re.M)
        else:
            section += '\n' + line + '\n'
        temporary = config.with_suffix('.tmp')
        temporary.write_text(text[:start] + section + text[end:])
        temporary.replace(config)

    evidence_rows = []
    (evidence / 'environment.json').write_text(json.dumps({
        'executable': str(executable),
        'sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
        'gnome': run('gnome-shell', '--version'),
        'display': os.environ['DISPLAY'],
        'backend': 'X11 / Xvfb 1280x800, one monitor, 100% scale',
        'os': Path('/etc/os-release').read_text(),
    }, indent=2) + '\n')
    try:
        wm = spawn(['gnome-shell', '--x11', '--sm-disable', '--mode=user'], 'gnome')
        wait_for(lambda: 'window id #' in run('xprop', '-root', '_NET_SUPPORTING_WM_CHECK'),
                 'GNOME did not start')
        assert wm.poll() is None
        time.sleep(1)
        run('xdotool', 'key', 'Escape')
        spawn(['/usr/bin/python3', '-c', TARGET], 'target')
        target = wait_for(lambda: window_named('Murmur drag test target'), 'Target window missing')
        app = spawn([str(executable), '--background'], 'murmur')
        window = wait_for(lambda: window_named('Murmur Overlay'), 'Overlay not visible', 45)
        time.sleep(3)
        run('xdotool', 'mousemove', '600', '400')
        run('xdotool', 'key', 'Escape')
        time.sleep(.7)
        run('xdotool', 'mousemove', '600', '400', 'click', '1')
        time.sleep(.7)
        run('xdotool', 'windowactivate', target)
        wait_for(lambda: active_window() == target, 'Target did not get focus')
        print('Initial native geometry:', geometry(window), flush=True)
        print(run('xprop', '-id', window, 'WM_NORMAL_HINTS'), flush=True)
        # A compact idle view proves that the frontend loaded and reported its size.
        wait_for(lambda: geometry(window)['WIDTH'] < 250, 'Frontend did not size the idle overlay', 45)
        run('xdotool', 'windowactivate', target)
        wait_for(lambda: active_window() == target, 'Target did not get focus')
        properties = run('xprop', '-id', window, '_NET_WM_WINDOW_TYPE', '_NET_WM_ALLOWED_ACTIONS',
                         'WM_HINTS', '_NET_WM_STATE')
        (evidence / 'window-properties.txt').write_text(properties)
        assert '_NET_WM_ACTION_MOVE' in properties
        assert '_NET_WM_WINDOW_TYPE_NOTIFICATION' not in properties
        assert 'Client accepts input or input focus: False' in properties
        assert '_NET_WM_STATE_ABOVE' in properties and '_NET_WM_STATE_SKIP_TASKBAR' in properties

        work_area = [int(value) for value in re.findall(r'\d+',
                     run('xprop', '-root', '_NET_WORKAREA').split('=', 1)[1])][:4]

        def begin_drag():
            before = geometry(window)
            # Center of the 24px grip in the compact idle pill, including canvas/pill padding.
            run('xdotool', 'mousemove', str(before['X'] + 40), str(before['Y'] + before['HEIGHT'] // 2))
            run('xdotool', 'mousedown', '1')
            time.sleep(.15)
            return before

        for index in range(20):
            before = begin_drag()
            x, y = 140 + index % 5 * 180, 180 + index % 3 * 130
            if index >= 16:
                # Use the compositor's work area; panel height depends on fonts/theme.
                left, top, width, height = work_area
                x, y = [(left, top), (left + width - before['WIDTH'], top),
                        (left, top + height - before['HEIGHT']),
                        (left + width - before['WIDTH'], top + height - before['HEIGHT'])][index - 16]
            run('xdotool', 'mousemove', str(x + 40), str(y + before['HEIGHT'] // 2))
            if index == 0:
                time.sleep(1.2)
                assert prefs().get('position', 'bottom-center') == 'bottom-center', 'Saved while button held'
                assert geometry(window)['X'] != before['X'], 'Handle did not begin native movement'
            run('xdotool', 'mouseup', '1')
            wait_for(lambda: prefs().get('position') == 'custom', 'Custom placement not persisted')
            time.sleep(.4)
            after = geometry(window)
            assert abs(after['X'] - x) <= 2 and abs(after['Y'] - y) <= 2, (index, before, after, x, y)
            assert active_window() == target, 'Dragging stole target focus'
            evidence_rows.append({'drag': index + 1, 'x': after['X'], 'y': after['Y'], 'focus_retained': True})
            print(f'Drag {index + 1}: ({after["X"]}, {after["Y"]}), focus retained', flush=True)

        # Resize during a held drag must wait for release and preserve the new anchor.
        before = begin_drag()
        run('xdotool', 'mousemove', str(before['X'] - 60 + 40), str(before['Y'] - 50 + before['HEIGHT'] // 2))
        time.sleep(.2)
        held = geometry(window)
        update_overlay('scale', 1.2)
        time.sleep(1)
        during = geometry(window)
        assert all(during[k] == held[k] for k in ['X', 'Y', 'WIDTH', 'HEIGHT']), 'Resize fought the held drag'
        run('xdotool', 'mouseup', '1')
        wait_for(lambda: geometry(window)['WIDTH'] > before['WIDTH'], 'Deferred resize not applied')
        time.sleep(.5)
        final = geometry(window)
        assert active_window() == target
        evidence_rows.append({'resize_during_drag': 'passed', 'before': held, 'after': final})

        update_overlay('enabled', False)
        wait_for(lambda: not window_named('Murmur Overlay'), 'Disable did not hide overlay')
        update_overlay('enabled', True)
        wait_for(lambda: window_named('Murmur Overlay'), 'Enable did not reveal overlay')
        time.sleep(.5)
        shown = geometry(window)
        assert all(abs(shown[k] - final[k]) <= 2 for k in ['X', 'Y']), 'Hide/show lost placement'

        stop(app)
        app = spawn([str(executable), '--background'], 'murmur-restart')
        window = wait_for(lambda: window_named('Murmur Overlay'), 'Restart did not reveal overlay', 45)
        wait_for(lambda: geometry(window)['WIDTH'] < 250, 'Restart frontend did not load', 45)
        time.sleep(.5)
        restarted = geometry(window)
        assert all(abs(restarted[k] - final[k]) <= 2 for k in ['X', 'Y']), 'Restart lost placement'
        evidence_rows.append({'hide_show_and_restart': 'passed', 'position': restarted})

        # Explicit preset during a native grab wins after release, without
        # fighting the compositor or persisting the cancelled movement.
        before = begin_drag()
        run('xdotool', 'mousemove', str(before['X'] - 100 + 40), str(before['Y'] - 100 + before['HEIGHT'] // 2))
        time.sleep(.2)
        held = geometry(window)
        update_overlay('position', 'bottom-center')
        time.sleep(1)
        assert geometry(window)['X'] == held['X'], 'Preset fought the held drag'
        run('xdotool', 'mouseup', '1')
        wait_for(lambda: geometry(window)['X'] != held['X'], 'Deferred preset not applied')
        time.sleep(.5)
        reset = geometry(window)
        assert abs(reset['X'] - (1280 - reset['WIDTH']) / 2) <= 2, 'Preset did not restore placement'
        assert prefs()['position'] == 'bottom-center', 'Cancelled drag overwrote preset'
        evidence_rows.append({'preset': 'passed', 'position': reset})
        (evidence / 'results.json').write_text(json.dumps(evidence_rows, indent=2) + '\n')
        print('PASS: 20 native drags, held pause, focus, resize deferral, persistence, hide/show, restart and preset', flush=True)
    except Exception:
        if shutil.which("import"):
            subprocess.run(["import", "-window", "root", str(evidence / "failure.png")], check=False, timeout=10)
        raise
    finally:
        subprocess.run(['xdotool', 'mouseup', '1'], check=False, capture_output=True)
        for process in reversed(processes):
            stop(process)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('executable', type=Path)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--session', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    executable = args.executable.resolve()
    evidence = args.evidence.resolve() if args.evidence else Path(tempfile.mkdtemp(prefix='murmur-overlay-test-'))
    evidence.mkdir(parents=True, exist_ok=True)
    if args.session:
        exercise(executable, evidence)
        return
    print('Evidence:', evidence, flush=True)
    with tempfile.TemporaryDirectory(prefix='murmur-overlay-profile-') as directory:
        profile = Path(directory)
        env = dict(os.environ)
        for key in ['XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR']:
            path = profile / key.lower()
            path.mkdir(mode=0o700)
            env[key] = str(path)
        env.update(GDK_BACKEND='x11', LIBGL_ALWAYS_SOFTWARE='1', NO_AT_BRIDGE='1',
                   GSETTINGS_BACKEND='memory', GNOME_SHELL_SESSION_MODE='user')
        bus = profile / 'bus.conf'
        bus.write_text(BUS_CONFIG)
        try:
            subprocess.run(['xvfb-run', '-a', '-s', '-screen 0 1280x800x24 -ac +extension GLX +render -noreset',
                            'dbus-run-session', '--config-file=' + str(bus), '--', sys.executable,
                            str(Path(__file__).resolve()), str(executable), '--session', '--evidence', str(evidence)],
                           env=env, check=True, timeout=180)
        finally:
            log = profile / 'xdg_data_home/murmur/murmur.log'
            if log.exists():
                (evidence / 'application.log').write_bytes(log.read_bytes())


if __name__ == '__main__':
    main()
