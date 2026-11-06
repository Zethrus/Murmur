#!/usr/bin/env python3
"""Check actual shipped executables, helper libraries and Debian runtime parity."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from linux_cuda_runtime import verify as verify_cuda, check_asset_sizes


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').digest()


def check(directory):
    directory = directory.resolve()
    image, = directory.glob('*.AppImage')
    deb, = directory.glob('*.deb')
    check_asset_sizes(directory.iterdir())
    assert {path.name for path in directory.iterdir()} == {
        image.name, deb.name, 'murmur-linux-x86_64.tar.gz'
    }, 'Linux output must contain only the AppImage, Debian package, and portable archive'
    with tempfile.TemporaryDirectory(prefix='murmur-bundle-test-') as temporary:
        work = Path(temporary)
        run(str(image), '--appimage-extract', cwd=work, stdout=subprocess.DEVNULL)
        root = work / 'squashfs-root'
        manifest = verify_cuda(root)
        env = dict(os.environ, LD_LIBRARY_PATH=str(root / 'usr/lib'))
        for name in ('murmur', 'murmur-engine', 'xdotool', 'xprop'):
            executable = root / 'usr/bin' / name
            assert executable.is_file() and os.access(executable, os.X_OK), name
            linked = run('ldd', str(executable), env=env, capture_output=True).stdout
            assert 'not found' not in linked, linked
        run(str(root / 'usr/bin/xdotool'), '--version', env=env)
        if env.get('DISPLAY'):
            run(str(root / 'usr/bin/xprop'), '-version', env=env)
        with tarfile.open(directory / 'murmur-linux-x86_64.tar.gz') as archive:
            members = archive.getmembers()
            assert len(members) == 2 and {member.name for member in members} == {'murmur', 'murmur-engine'}
            for member in members:
                assert member.isfile() and member.mode & 0o111 and member.size > 0, member.name
                expected = image if member.name == 'murmur' else root / 'usr/bin/murmur-engine'
                with archive.extractfile(member) as stream:
                    assert hashlib.file_digest(stream, 'sha256').digest() == digest(expected), member.name
        requests = [{'id': 1, 'method': 'ping'}, {'id': 2, 'method': 'models.list'}, {'id': 3, 'method': 'shutdown'}]
        engine = run(str(root / 'usr/bin/murmur-engine'), '--models-dir', str(work / 'models'),
                     input=''.join(json.dumps(request) + '\n' for request in requests),
                     env=env, capture_output=True, timeout=60)
        replies = [json.loads(line) for line in engine.stdout.splitlines()]
        assert len(replies) == 3 and all(reply['ok'] for reply in replies), replies
        assert replies[0]['result'] == {'pong': True}
        assert all(not model['installed'] for model in replies[1]['result'])
        assert replies[2]['result'] == {'shutdown': True}
        assert (root / 'usr/lib/libxdo.so.3').is_file()
        assert (root / 'usr/lib/libasound.so.2').is_file()
        assert (root / 'usr/share/doc/murmur/bundled-runtime/xdotool.copyright').is_file()
        package = work / 'deb'
        run('dpkg-deb', '-x', str(deb), str(package))
        for file in root.rglob('*'):
            if file.is_file():
                assert digest(file) == digest(package / 'opt/murmur' / file.relative_to(root)), file
        assert verify_cuda(package / 'opt/murmur') == manifest
        # Extraction runs as the owner, so check modes rather than access.
        runtime = package / 'opt/murmur'
        for directory in (package / 'opt', runtime, *(p for p in runtime.rglob('*') if p.is_dir() and not p.is_symlink())):
            assert directory.stat().st_mode & 0o777 == 0o755, f'{directory}: {directory.stat().st_mode & 0o777:o}'
        launcher = (package / 'usr/bin/murmur').read_text()
        assert 'cd /opt/murmur/usr' in launcher and '/opt/murmur/AppRun' in launcher, launcher
        run(sys.executable, str(Path(__file__).with_name('test-linux-startup.py')),
            str(package / 'opt/murmur/AppRun'), timeout=120)
    print('AppImage, tools, engine, and Debian runtime verified.')


if __name__ == '__main__':
    check(Path(sys.argv[1]))
