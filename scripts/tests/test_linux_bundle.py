import importlib.util
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
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
            (lib / "libbaz.so.1").write_bytes(b"different")  # same bytes, other library name group
            (lib / "notes.txt").write_bytes(b"same library")
            with patch.object(bundle, "_soname", return_value=None):
                replaced = bundle.dedupe_libraries(lib)
            # Shortest versioned name wins without an ELF SONAME.
            self.assertTrue((lib / "libfoo.so.0").is_file() and not (lib / "libfoo.so.0").is_symlink())
            for name in ("libfoo.so", "libfoo.so.0.8000.0"):
                self.assertEqual((lib / name).readlink(), Path("libfoo.so.0"))
                self.assertEqual((lib / name).read_bytes(), b"same library")
            self.assertEqual({p.name for p in replaced}, {"libfoo.so", "libfoo.so.0.8000.0", "libbaz.so.1"})
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
                bundle.stage_engine(engine, Path(temporary) / "out")


class DebianRuntimeTests(unittest.TestCase):
    def test_engine_and_cuda_move_to_usr_lib_and_are_traversable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Mirror AppImage extraction under mktemp: private directories.
            appdir = root / "squashfs-root"
            engine = appdir / bundle.ENGINE
            (engine / "_internal").mkdir(parents=True)
            (engine / "murmur-engine").write_bytes(b"engine")
            (engine / "murmur-engine").chmod(0o755)
            cuda = appdir / bundle.CUDA
            cuda.mkdir(parents=True)
            (cuda / "libcublas.so.12").write_bytes(b"cublas")
            (cuda / "libcublas.so.12").chmod(0o644)
            notices = appdir / bundle.DOC
            notices.mkdir(parents=True)
            (notices / "nvidia-cublas-cu12.License.txt").write_text("notice")
            (notices / "cuda-manifest.json").write_text("{}")
            (notices / "xdotool.copyright").write_text("AppImage only")
            for directory in (appdir, *(p for p in appdir.rglob("*") if p.is_dir())):
                directory.chmod(0o700)
            package_dir = root / "deb"
            (package_dir / "usr/bin").mkdir(parents=True)
            (package_dir / "DEBIAN").mkdir()
            (package_dir / "usr/bin/murmur").write_bytes(b"tauri binary")
            bundle.install_debian_runtime(appdir, package_dir)
            lib = package_dir / "usr/lib/murmur"
            for directory in (lib, *(p for p in lib.rglob("*") if p.is_dir())):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o755, directory)
            self.assertEqual((lib / "engine/murmur-engine").stat().st_mode & 0o777, 0o755)
            self.assertEqual((lib / "cuda/libcublas.so.12").stat().st_mode & 0o777, 0o644)
            self.assertEqual((package_dir / "usr/bin/murmur").read_bytes(), b"tauri binary")
            self.assertFalse((package_dir / "opt").exists())
            shipped = {p.name for p in (package_dir / bundle.DOC).iterdir()}
            self.assertEqual(shipped, {"nvidia-cublas-cu12.License.txt", "cuda-manifest.json"})
            sums = (package_dir / "DEBIAN/md5sums").read_text()
            self.assertIn("usr/lib/murmur/engine/murmur-engine", sums)
            self.assertIn("usr/bin/murmur", sums)
            self.assertNotIn("DEBIAN", sums)


if __name__ == "__main__":
    unittest.main()
