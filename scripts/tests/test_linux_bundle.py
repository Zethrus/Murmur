import importlib.util
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

spec = importlib.util.spec_from_file_location("bundle", Path(__file__).parents[1] / "bundle-linux-runtime.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


class PortableBundleTests(unittest.TestCase):
    def test_packages_only_with_complete_executable_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "release"
            output.mkdir()
            image = output / "Murmur_1.2.3_amd64.AppImage"
            deb = output / "Murmur_1.2.3_amd64.deb"
            engine = root / "engine"
            image.write_bytes(b"full AppImage runtime")
            deb.write_bytes(b"Debian package")
            engine.write_bytes(b"bundled speech engine")
            image.chmod(0o755)
            engine.chmod(0o755)
            # Rebuilding in an old output directory must not keep loose downloads.
            (output / "murmur").write_bytes(b"stale app")
            (output / "murmur-engine").write_bytes(b"stale engine")
            bundle.package_portable(image, engine, output)
            self.assertEqual({p.name for p in output.iterdir()}, {
                image.name, deb.name, "murmur-linux-x86_64.tar.gz"
            })
            with tarfile.open(output / "murmur-linux-x86_64.tar.gz") as archive:
                self.assertEqual(archive.getnames(), ["murmur", "murmur-engine"])
                for name, source in (("murmur", image), ("murmur-engine", engine)):
                    member = archive.getmember(name)
                    self.assertTrue(member.isfile())
                    self.assertEqual(member.mode & 0o777, 0o755)
                    with archive.extractfile(member) as stream:
                        self.assertEqual(stream.read(), source.read_bytes())


class DebianRuntimeTests(unittest.TestCase):
    def test_runtime_is_traversable_and_launcher_enters_app_dir(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Mirror AppImage extraction under mktemp: private directories.
            appdir = root / "squashfs-root"
            webkit = appdir / "usr/lib/x86_64-linux-gnu/webkit2gtk-4.1"
            webkit.mkdir(parents=True)
            (appdir / "apprun-hooks").mkdir()
            engine = appdir / "usr/bin/murmur-engine"
            engine.parent.mkdir()
            engine.write_bytes(b"engine")
            engine.chmod(0o755)
            notice = appdir / "usr/notice.txt"
            notice.write_text("notice")
            notice.chmod(0o644)
            for directory in (appdir, *(p for p in appdir.rglob("*") if p.is_dir())):
                directory.chmod(0o700)
            package_dir = root / "deb"
            (package_dir / "usr/bin").mkdir(parents=True)
            (package_dir / "usr/bin/murmur").write_bytes(b"tauri binary")
            bundle.install_debian_runtime(appdir, package_dir)
            runtime = package_dir / "opt/murmur"
            for directory in (package_dir / "opt", runtime, *(p for p in runtime.rglob("*") if p.is_dir())):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o755, directory)
            self.assertEqual((runtime / "usr/bin/murmur-engine").stat().st_mode & 0o777, 0o755)
            self.assertEqual((runtime / "usr/notice.txt").stat().st_mode & 0o777, 0o644)
            launcher = package_dir / "usr/bin/murmur"
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
            self.assertEqual(launcher.read_text(),
                             '#!/bin/sh\ncd /opt/murmur/usr || exit 1\nexec /opt/murmur/AppRun "$@"\n')
