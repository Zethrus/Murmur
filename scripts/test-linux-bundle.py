#!/usr/bin/env python3
"""Check the shipped AppImage, .deb and portable archive built by bundle-linux-runtime.py."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
from linux_cuda_runtime import CUDA, verify as verify_cuda

HERE = Path(__file__).resolve().parent
ENGINE = Path("usr/lib/murmur/engine/murmur-engine")


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = _load("check_release", "check-release.py")
engine_gate = _load("check_engine_imports", "check-engine-imports.py")
SHARED_LIBRARY = re.compile(r"^lib.+\.so(?:\.\d+)*$")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').digest()


def duplicate_libraries(root):
    """Regular shared-library files with identical content in one directory."""
    seen, duplicates = {}, []
    for path in sorted(root.rglob('*')):
        if path.is_file() and not path.is_symlink() and SHARED_LIBRARY.match(path.name):
            key = (path.parent, path.stat().st_size, digest(path))
            if key in seen:
                duplicates.append((seen[key], path))
            seen.setdefault(key, path)
    return duplicates


def stripped(path):
    sections = run('readelf', '-S', str(path), capture_output=True).stdout
    return '.symtab' not in sections


def check_engine(root, work, env):
    engine = root / ENGINE
    assert engine.is_file() and os.access(engine, os.X_OK), engine
    engine_gate.check(engine.parent)
    requests = [{'id': 1, 'method': 'ping'}, {'id': 2, 'method': 'models.list'}, {'id': 3, 'method': 'shutdown'}]
    result = run(str(engine), '--models-dir', str(work / 'models'),
                 input=''.join(json.dumps(request) + '\n' for request in requests),
                 env=env, capture_output=True, timeout=60)
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(replies) == 3 and all(reply['ok'] for reply in replies), replies
    assert replies[0]['result'] == {'pong': True}
    assert all(not model['installed'] for model in replies[1]['result'])
    assert replies[2]['result'] == {'shutdown': True}


def check_appimage(image, work):
    run(str(image), '--appimage-extract', cwd=work, stdout=subprocess.DEVNULL)
    root = work / 'squashfs-root'
    manifest = verify_cuda(root)
    env = dict(os.environ, LD_LIBRARY_PATH=str(root / 'usr/lib'))
    for name in ('murmur', 'xdotool', 'xprop'):
        executable = root / 'usr/bin' / name
        assert executable.is_file() and os.access(executable, os.X_OK), name
        linked = run('ldd', str(executable), env=env, capture_output=True).stdout
        assert 'not found' not in linked, linked
    assert not (root / 'usr/bin/murmur-engine').exists(), 'Engine belongs in usr/lib/murmur/engine'
    for name in ('murmur', 'murmur-companion-client'):
        assert stripped(root / 'usr/bin' / name), f'{name} is not stripped'
    run(str(root / 'usr/bin/xdotool'), '--version', env=env)
    if env.get('DISPLAY'):
        run(str(root / 'usr/bin/xprop'), '-version', env=env)
    check_engine(root, work, env)
    assert (root / 'usr/lib/libxdo.so.3').is_file()
    assert (root / 'usr/lib/libasound.so.2').is_file()
    assert (root / 'usr/share/doc/murmur/bundled-runtime/xdotool.copyright').is_file()
    assert not duplicate_libraries(root / 'usr/lib'), duplicate_libraries(root / 'usr/lib')
    return root, manifest


def check_deb(deb, root, manifest, work):
    package = work / 'deb'
    run('dpkg-deb', '-x', str(deb), str(package))
    assert not (package / 'opt').exists(), 'The .deb must not carry a bundled /opt runtime'
    assert stripped(package / 'usr/bin/murmur')
    for relative in ('usr/lib/murmur/engine', CUDA):
        files = {p.relative_to(root / relative) for p in (root / relative).rglob('*') if p.is_file()}
        assert files == {p.relative_to(package / relative) for p in (package / relative).rglob('*') if p.is_file()}, relative
        for file in files:
            assert digest(root / relative / file) == digest(package / relative / file), file
    assert verify_cuda(package) == manifest
    # Extraction runs as the owner, so check modes rather than access.
    for directory in (p for base in ('usr/lib/murmur', 'usr/share/doc/murmur') for p in (package / base).rglob('*') if p.is_dir() and not p.is_symlink()):
        assert directory.stat().st_mode & 0o777 == 0o755, f'{directory}: {directory.stat().st_mode & 0o777:o}'
    depends = run('dpkg-deb', '-f', str(deb), 'Depends', capture_output=True).stdout
    for needed in ('libwebkit2gtk-4.1-0', 'xdotool', 'x11-utils', 'libayatana-appindicator3-1'):
        assert needed in depends, depends
    run(sys.executable, str(HERE / 'test-linux-startup.py'), str(package / 'usr/bin/murmur'), timeout=180)


def check(directory):
    directory = directory.resolve()
    image, = directory.glob('*.AppImage')
    deb, = directory.glob('*.deb')
    assert {path.name for path in directory.iterdir()} == {
        image.name, deb.name, 'murmur-linux-x86_64.tar.gz'
    }, 'Linux output must contain only the AppImage, Debian package, and portable archive'
    release.check_budgets(sorted(directory.iterdir()))
    release.check_portable(directory / 'murmur-linux-x86_64.tar.gz', image)
    with tempfile.TemporaryDirectory(prefix='murmur-bundle-test-') as temporary:
        work = Path(temporary)
        root, manifest = check_appimage(image, work)
        check_deb(deb, root, manifest, work)
    print('AppImage, portable archive, engine, cuBLAS runtime and Debian package verified.')


if __name__ == '__main__':
    check(Path(sys.argv[1]))
