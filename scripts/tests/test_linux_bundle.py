import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest

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
