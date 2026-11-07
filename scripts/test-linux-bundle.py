#!/usr/bin/env python3
"""Check the shipped AppImage, .deb and portable archive built by bundle-linux-runtime.py,
then install the engine and CUDA add-on components through the app and check those."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import linux_components
from linux_cuda_runtime import REQUIRED as CUDA_LIBRARIES, sha256

HERE = Path(__file__).resolve().parent


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


def check_engine(engine, work):
    """The installed engine component: executable, cuDNN-free, answers RPC, never downloads."""
    assert engine.is_file() and os.access(engine, os.X_OK), engine
    engine_gate.check(engine.parent)
    requests = [{'id': 1, 'method': 'ping'}, {'id': 2, 'method': 'models.install', 'params': {'name': 'tiny.en'}},
                {'id': 3, 'method': 'shutdown'}]
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(work)}
    result = run(str(engine), '--models-dir', str(work / 'models'),
                 input=''.join(json.dumps(request) + '\n' for request in requests),
                 env=env, capture_output=True, timeout=60)
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(replies) == 3, replies
    assert replies[0]['result'] == {'pong': True}
    assert not replies[1]['ok'] and 'managed by the Murmur app' in replies[1]['error']['message'], replies[1]
    assert replies[2]['result'] == {'shutdown': True}


def check_cuda(cuda):
    """The installed CUDA add-on: exactly cuBLAS, hashes per its manifest, NEEDED resolved inside."""
    manifest = json.loads((cuda / 'cuda-manifest.json').read_text())
    shipped = {p.name for p in cuda.iterdir()}
    assert shipped == CUDA_LIBRARIES | {'cuda-manifest.json', 'nvidia-cublas-cu12.License.txt'}, shipped
    assert set(manifest['files']) == CUDA_LIBRARIES
    for name, digest in manifest['files'].items():
        assert sha256(cuda / name) == digest, name
    for name in CUDA_LIBRARIES:
        linked = run('ldd', str(cuda / name), env={'LD_LIBRARY_PATH': str(cuda)}, capture_output=True).stdout
        assert 'not found' not in linked, linked
        for line in linked.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0].startswith(('libcu', 'libnv')) and not parts[0].startswith('libcuda.so'):
                assert Path(parts[2]).is_relative_to(cuda), line
    return manifest


def check_components(app, components, work):
    """Install from the release zips exactly as a user would, then refuse tampered input."""
    env = linux_components.environment(work / 'home', components)
    installed = linux_components.install_release(app, components, env)
    assert set(installed) == {'engine', 'cuda'}, installed
    check_engine(installed['engine'] / 'murmur-engine', work)
    check_cuda(installed['cuda'])
    # A modified archive matches no signed component; a foreign trust root verifies nothing.
    engine_zip, = components.glob('murmur-engine-linux-x86_64-*.zip')
    tampered = work / engine_zip.name
    data = bytearray(engine_zip.read_bytes())
    data[len(data) // 2] ^= 0xFF
    tampered.write_bytes(bytes(data))
    assert linux_components.install(app, components, tampered, env, check=False) is None
    strange = dict(env, MURMUR_DEV_TRUST_ROOT=str(work / 'missing.asc'))
    assert linux_components.install(app, components, engine_zip, strange, check=False) is None
    without_ack = {k: v for k, v in env.items() if k != 'MURMUR_DEV_TRUST_ROOT_ACK'}
    assert linux_components.install(app, components, engine_zip, without_ack, check=False) is None, \
        'a release binary must ignore a development trust root without the acknowledgement'
    return installed


def check_appimage(image, work):
    run(str(image), '--appimage-extract', cwd=work, stdout=subprocess.DEVNULL)
    root = work / 'squashfs-root'
    for absent in ('usr/lib/murmur/engine', 'usr/lib/murmur/cuda', 'usr/bin/murmur-engine'):
        assert not (root / absent).exists(), f'{absent} must be a downloadable component, not packaged'
    env = dict(os.environ, LD_LIBRARY_PATH=str(root / 'usr/lib'))
    for name in ('murmur', 'xdotool', 'xprop'):
        executable = root / 'usr/bin' / name
        assert executable.is_file() and os.access(executable, os.X_OK), name
        linked = run('ldd', str(executable), env=env, capture_output=True).stdout
        assert 'not found' not in linked, linked
    for name in ('murmur', 'murmur-companion-client'):
        assert stripped(root / 'usr/bin' / name), f'{name} is not stripped'
    run(str(root / 'usr/bin/xdotool'), '--version', env=env)
    if env.get('DISPLAY'):
        run(str(root / 'usr/bin/xprop'), '-version', env=env)
    assert (root / 'usr/lib/libxdo.so.3').is_file()
    assert (root / 'usr/lib/libasound.so.2').is_file()
    assert (root / 'usr/share/doc/murmur/bundled-runtime/xdotool.copyright').is_file()
    assert not duplicate_libraries(root / 'usr/lib'), duplicate_libraries(root / 'usr/lib')
    return root


def check_deb(deb, work):
    package = work / 'deb'
    run('dpkg-deb', '-x', str(deb), str(package))
    assert not (package / 'opt').exists(), 'The .deb must not carry a bundled /opt runtime'
    assert not (package / 'usr/lib/murmur').exists(), 'The .deb carries only the app; engine and CUDA are components'
    assert stripped(package / 'usr/bin/murmur')
    # Extraction runs as the owner, so check modes rather than access.
    for directory in (p for p in (package / 'usr').rglob('*') if p.is_dir() and not p.is_symlink()):
        assert directory.stat().st_mode & 0o777 == 0o755, f'{directory}: {directory.stat().st_mode & 0o777:o}'
    depends = run('dpkg-deb', '-f', str(deb), 'Depends', capture_output=True).stdout
    for needed in ('libwebkit2gtk-4.1-0', 'xdotool', 'x11-utils', 'libayatana-appindicator3-1'):
        assert needed in depends, depends
    # Real ALSA first: on Ubuntu 24.04 the virtual libasound2 can resolve to the OSS shim
    # liboss4-salsa-asound2, which lacks ALSA symbols (found by test-clean-install.sh).
    assert 'libasound2t64 | libasound2' in depends, depends
    run(sys.executable, str(HERE / 'test-linux-startup.py'), str(package / 'usr/bin/murmur'), timeout=180)


def check(directory, components):
    directory, components = directory.resolve(), components.resolve()
    image, = directory.glob('*.AppImage')
    deb, = directory.glob('*.deb')
    assert {path.name for path in directory.iterdir()} == {
        image.name, deb.name, 'murmur-linux-x86_64.tar.gz'
    }, 'Linux output must contain only the AppImage, Debian package, and portable archive'
    release.check_budgets(sorted(directory.iterdir()))
    release.check_portable(directory / 'murmur-linux-x86_64.tar.gz', image)
    release.check_budgets(sorted(p for p in components.iterdir() if p.is_file()))
    with tempfile.TemporaryDirectory(prefix='murmur-bundle-test-') as temporary:
        work = Path(temporary)
        root = check_appimage(image, work)
        check_components(root / 'usr/bin/murmur', components, work)
        check_deb(deb, work)
    print('AppImage, portable archive, Debian package, and engine + cuBLAS components verified.')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit('usage: test-linux-bundle.py <linux artifacts dir> <components dir>')
    check(Path(sys.argv[1]), Path(sys.argv[2]))
