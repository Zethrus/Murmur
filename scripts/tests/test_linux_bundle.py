import importlib.util
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

spec = importlib.util.spec_from_file_location("bundle", Path(__file__).parents[1] / "bundle-linux-runtime.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


class PortableBundleTests(unittest.TestCase):
    def test_portable_archive_holds_only_the_appimage(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "release"
            output.mkdir()
            image = output / "Murmur_1.2.3_amd64.AppImage"
            deb = output / "Murmur_1.2.3_amd64.deb"
            image.write_bytes(b"full AppImage runtime")
            deb.write_bytes(b"Debian package")
            image.chmod(0o755)
            # Rebuilding in an old output directory must not keep loose downloads.
            (output / "murmur").write_bytes(b"stale app")
            (output / "murmur-engine").write_bytes(b"stale engine")
            bundle.package_portable(image, output)
            self.assertEqual({p.name for p in output.iterdir()}, {
                image.name, deb.name, "murmur-linux-x86_64.tar.gz"
            })
            with tarfile.open(output / "murmur-linux-x86_64.tar.gz") as archive:
                self.assertEqual(archive.getnames(), ["murmur"])
                member = archive.getmember("murmur")
                self.assertTrue(member.isfile())
                self.assertEqual(member.mode & 0o777, 0o755)
                with archive.extractfile(member) as stream:
                    self.assertEqual(stream.read(), image.read_bytes())


class DedupeTests(unittest.TestCase):
    def test_identical_libraries_become_symlinks_to_one_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            lib = Path(temporary)
            for name in ("libfoo.so", "libfoo.so.0", "libfoo.so.0.8000.0"):
                (lib / name).write_bytes(b"same library")
            (lib / "libbar.so.1").write_bytes(b"different")
            (lib / "libbaz.so.1").write_bytes(b"different")  # same bytes as libbar, different library
            (lib / "notes.txt").write_bytes(b"same library")
            with patch.object(bundle, "_soname", return_value=None):
                replaced = bundle.dedupe_libraries(lib)
            # Shortest versioned name wins without an ELF SONAME.
            self.assertTrue((lib / "libfoo.so.0").is_file() and not (lib / "libfoo.so.0").is_symlink())
            for name in ("libfoo.so", "libfoo.so.0.8000.0"):
                self.assertEqual((lib / name).readlink(), Path("libfoo.so.0"))
                self.assertEqual((lib / name).read_bytes(), b"same library")
            self.assertEqual({p.name for p in replaced}, {"libfoo.so", "libfoo.so.0.8000.0"})
            # Identical bytes under a different library name stay a real file.
            self.assertTrue((lib / "libbaz.so.1").is_file() and not (lib / "libbaz.so.1").is_symlink())
            self.assertFalse((lib / "notes.txt").is_symlink())

    def test_soname_is_kept_as_the_real_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            lib = Path(temporary)
            for name in ("libgio-2.0.so", "libgio-2.0.so.0", "libgio-2.0.so.0.8000.0"):
                (lib / name).write_bytes(b"gio")
            with patch.object(bundle, "_soname", return_value="libgio-2.0.so.0"):
                bundle.dedupe_libraries(lib)
            self.assertFalse((lib / "libgio-2.0.so.0").is_symlink())
            self.assertEqual((lib / "libgio-2.0.so").readlink(), Path("libgio-2.0.so.0"))

    def test_copies_in_different_directories_are_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in ("a", "b"):
                (root / directory).mkdir()
                (root / directory / "libsame.so.1").write_bytes(b"same")
            with patch.object(bundle, "_soname", return_value=None):
                self.assertEqual(bundle.dedupe_libraries(root), [])


class EngineStagingTests(unittest.TestCase):
    def test_onefile_engine_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = Path(temporary) / "murmur-engine"
            engine.mkdir()
            (engine / "murmur-engine").write_bytes(b"onefile")
            (engine / "murmur-engine").chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, "Not a onedir"):
                bundle.check_engine(engine)


class ComponentZipTests(unittest.TestCase):
    def tree(self, root):
        (root / "_internal/pkg.libs").mkdir(parents=True)
        (root / "murmur-engine").write_bytes(b"engine")
        (root / "murmur-engine").chmod(0o755)
        (root / "_internal/pkg.libs/libx.so.1").write_bytes(b"library")
        (root / "_internal/pkg.libs/libx.so.1").chmod(0o644)
        # PyInstaller's convenience link to a file inside the tree.
        (root / "_internal/libx.so.1").symlink_to("pkg.libs/libx.so.1")

    def test_zip_is_deterministic_keeps_modes_and_drops_internal_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.tree(root / "engine")
            first = bundle.write_zip(root / "engine", root / "a.zip")
            second = bundle.write_zip(root / "engine", root / "b.zip")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                infos = {info.filename: info for info in archive.infolist()}
                self.assertEqual(set(infos), {"murmur-engine", "_internal/pkg.libs/libx.so.1"})
                self.assertEqual(infos["murmur-engine"].external_attr >> 16, 0o100755)
                self.assertEqual(infos["_internal/pkg.libs/libx.so.1"].external_attr >> 16, 0o100644)
                self.assertTrue(all(i.compress_type == zipfile.ZIP_LZMA for i in infos.values()))
                self.assertEqual(archive.read("murmur-engine"), b"engine")

    def test_escaping_or_dangling_links_fail(self):
        for target in ("/etc/passwd", "missing.so"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.tree(root / "engine")
                (root / "engine/_internal/bad.so").symlink_to(target)
                with self.assertRaisesRegex(RuntimeError, "Unsupported link"):
                    bundle.write_zip(root / "engine", root / "a.zip")


class DebianPackageTests(unittest.TestCase):
    def test_deb_holds_only_the_app_with_traversable_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            package_dir = Path(temporary) / "deb"
            (package_dir / "usr/bin").mkdir(parents=True)
            (package_dir / "usr/share/applications").mkdir(parents=True)
            (package_dir / "DEBIAN").mkdir()
            (package_dir / "usr/bin/murmur").write_bytes(b"tauri binary")
            for directory in (p for p in package_dir.rglob("*") if p.is_dir()):
                directory.chmod(0o700)
            bundle.finish_debian(package_dir)
            for directory in (p for p in (package_dir / "usr").rglob("*") if p.is_dir()):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o755, directory)
            self.assertFalse((package_dir / "usr/lib/murmur").exists())
            sums = (package_dir / "DEBIAN/md5sums").read_text()
            self.assertIn("usr/bin/murmur", sums)
            self.assertNotIn("DEBIAN", sums)


if __name__ == "__main__":
    unittest.main()
