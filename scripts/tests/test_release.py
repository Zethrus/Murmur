import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("release", Path(__file__).parents[1] / "check-release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("Murmur_1.2.3_amd64.deb", "Murmur_1.2.3_amd64.AppImage", "murmur-companion-v1.2.3.fap"):
            (self.root / name).write_bytes(b"artifact")
        self.archive()

    def archive(self, mode=0o755, data=b"artifact", engine_data=b"engine"):
        with tarfile.open(self.root / "murmur-linux-x86_64.tar.gz", "w:gz") as archive:
            for name in ("murmur", "murmur-engine"):
                contents = data if name == "murmur" else engine_data
                member = tarfile.TarInfo(name)
                member.mode, member.size = mode, len(contents)
                archive.addfile(member, io.BytesIO(contents))

    def test_complete_release_includes_companion_checksum(self):
        release.check_assets("v1.2.3", self.root)
        lines = (self.root / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(any(line.endswith("murmur-companion-v1.2.3.fap") for line in lines))

    def test_missing_companion_blocks_release(self):
        (self.root / "murmur-companion-v1.2.3.fap").unlink()
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_empty_asset_blocks_release(self):
        (self.root / "Murmur_1.2.3_amd64.deb").write_bytes(b"")
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_non_executable_archive_blocks_release(self):
        self.archive(mode=0o644)
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_different_portable_binary_blocks_release(self):
        self.archive(data=b"stale")
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_empty_portable_engine_blocks_release(self):
        self.archive(engine_data=b"")
        with self.assertRaisesRegex(ValueError, "nonempty executable"):
            release.check_assets("v1.2.3", self.root)

    def test_unexpected_platform_blocks_release(self):
        (self.root / "companion.apk").write_bytes(b"unexpected")
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_standalone_binaries_block_release(self):
        for name in ("murmur", "murmur-engine"):
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(b"unwanted standalone download")
                try:
                    with self.assertRaisesRegex(ValueError, "unexpected"):
                        release.check_assets("v1.2.3", self.root)
                finally:
                    path.unlink()
