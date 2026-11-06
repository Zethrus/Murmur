#!/usr/bin/env python3
"""Verify manual/login launches on a private X11 display and D-Bus session."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

BUS_CONFIG = '''<busconfig>
<type>session</type><listen>unix:tmpdir=/tmp</listen><auth>EXTERNAL</auth>
<policy context="default"><allow send_destination="*"/>
<allow receive_sender="*"/><allow own="*"/></policy>
</busconfig>'''


def visible_settings():
    result = subprocess.run(['xdotool', 'search', '--onlyvisible', '--name', '^Murmur Settings$'],
                            text=True, capture_output=True, timeout=5)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr)
    return bool(result.stdout.strip())


def launch_timeout(executable):
    # Extract-and-run unpacks the full CUDA runtime on every invocation,
    # including second-instance launches. Keep the extracted-app check tight.
    return 180 if executable.suffix == '.AppImage' else 25


def wait_for(condition, app, message, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app.poll() is not None:
            raise AssertionError(f'Murmur exited early: {app.returncode}')
        if condition():
            return
        time.sleep(0.1)
    raise AssertionError(message)


def exercise(executable, background):
    timeout = launch_timeout(executable)
    with tempfile.TemporaryDirectory(prefix='murmur-startup-') as directory:
        profile = Path(directory)
        runtime = profile / 'runtime'
        runtime.mkdir(mode=0o700)
        env = {**os.environ, 'HOME': directory, 'APPIMAGE_EXTRACT_AND_RUN': '1',
               'XDG_CONFIG_HOME': str(profile / 'config'),
               'XDG_DATA_HOME': str(profile / 'data'),
               'XDG_CACHE_HOME': str(profile / 'cache'),
               'XDG_RUNTIME_DIR': str(runtime)}
        # Exercise migration of a pre-0.4.3 login entry, not just new installs.
        entry = profile / 'config/autostart/murmur.desktop'
        legacy = profile / '.config/autostart/Murmur.desktop'
        entry.parent.mkdir(parents=True)
        entry.write_text('[Desktop Entry]\nType=Application\nName=Murmur\nExec=/old/murmur\n')
        legacy.parent.mkdir(parents=True)
        legacy.write_text(entry.read_text())
        if background:
            config = profile / 'config/murmur/config.toml'
            config.parent.mkdir(parents=True)
            config.write_text('[general]\nautostart = true\n')
        with (profile / 'stderr.log').open('w+') as log:
            # Launch from outside the runtime so cwd-relative helper paths fail here.
            launch = {'env': env, 'cwd': directory, 'stdout': log, 'stderr': subprocess.STDOUT}
            app = subprocess.Popen([str(executable), *(['--background'] if background else [])],
                                   start_new_session=True, **launch)
            try:
                if background:
                    wait_for(lambda: '--background' in entry.read_text(), app,
                             'Existing autostart entry was not updated', timeout)
                    assert '/old/murmur' not in entry.read_text()
                    assert not legacy.exists(), 'Legacy plugin entry was not removed'
                    time.sleep(2)
                    assert not visible_settings(), 'Login launch unexpectedly opened Settings'
                    subprocess.run([str(executable), '--background'], check=True, timeout=timeout, **launch)
                    time.sleep(1)
                    assert not visible_settings(), 'Second background launch opened Settings'
                    subprocess.run([str(executable)], check=True, timeout=timeout, **launch)
                    wait_for(visible_settings, app, 'Manual relaunch did not open Settings', timeout)
                else:
                    wait_for(visible_settings, app, 'Manual launch did not open Settings', timeout)
                    assert not entry.exists() and not legacy.exists(), 'Disabled autostart entries remain'
                print(('Login, autostart migration, and manual relaunch' if background else 'Manual launch') + ': passed', flush=True)
            except Exception:
                log.flush()
                print((profile / 'stderr.log').read_text(), flush=True)
                for registration in profile.rglob('*.desktop'):
                    print(registration.relative_to(profile), registration.read_text(), flush=True)
                app_log = profile / 'data/murmur/murmur.log'
                if app_log.exists():
                    print(app_log.read_text(), flush=True)
                raise
            finally:
                if app.poll() is None:
                    os.killpg(app.pid, signal.SIGTERM)
                    try:
                        app.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(app.pid, signal.SIGKILL)
                        app.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('executable', type=Path)
    parser.add_argument('--session', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    executable = args.executable.resolve()
    if args.session:
        exercise(executable, False)
        exercise(executable, True)
    else:
        with tempfile.TemporaryDirectory(prefix='murmur-startup-bus-') as directory:
            config = Path(directory) / 'session.conf'
            config.write_text(BUS_CONFIG)
            subprocess.run(['xvfb-run', '-a', 'dbus-run-session', '--config-file=' + str(config),
                            'python3', str(Path(__file__).resolve()), str(executable), '--session'],
                           check=True, timeout=5 * launch_timeout(executable) + 60)


if __name__ == '__main__':
    main()
