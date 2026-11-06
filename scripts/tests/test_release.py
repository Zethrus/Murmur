import importlib.util
import hashlib
import io
import json
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
        for name in ("murmur", "murmur-engine", "Murmur_1.2.3_amd64.deb", "Murmur_1.2.3_amd64.AppImage", "murmur-companion-v1.2.3.fap"):
            (self.root / name).write_bytes(b"artifact")
        self.archive()
        self.evidence()

    def evidence(self):
        linux = [p for p in self.root.iterdir() if p.name != "murmur-companion-v1.2.3.fap"]
        assets = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in linux}
        (self.root / "GPU-VALIDATION.json").write_text(json.dumps({"assets": assets,
            "gpu": {"status": {"device": "cuda", "cpu_fallback": False}, "warm_runs_ms": [1] * 10,
                    "libraries": ["/tmp/runtime/usr/lib/murmur/cuda/libcublas.so.12", "/usr/lib/libcuda.so.1"]},
            "auto": {"status": {"device": "cuda", "cpu_fallback": False}},
            "driver": "test GPU, test driver", "origin_validated": True,
            "runtime_probe": {name: f"/tmp/runtime/usr/lib/murmur/cuda/{name}" for name in
                ("libcublas.so.12", "libcudnn.so.9", "libnvrtc.so.12")}}))

    def archive(self, mode=0o755, data=b"artifact"):
        with tarfile.open(self.root / "murmur-linux-x86_64.tar.gz", "w:gz") as archive:
            for name in ("murmur", "murmur-engine"):
                member = tarfile.TarInfo(name)
                member.mode, member.size = mode, len(data)
                archive.addfile(member, io.BytesIO(data))

    def test_complete_release_includes_companion_checksum(self):
        release.check_assets("v1.2.3", self.root)
        lines = (self.root / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(lines), 7)
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

    def test_unexpected_platform_blocks_release(self):
        (self.root / "companion.apk").write_bytes(b"unexpected")
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_unbundled_desktop_blocks_release(self):
        (self.root / "murmur").write_bytes(b"raw executable without runtime")
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_stale_gpu_evidence_blocks_release(self):
        (self.root / "GPU-VALIDATION.json").write_text("{}")
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_oversized_asset_blocks_release(self):
        with (self.root / "Murmur_1.2.3_amd64.deb").open("wb") as stream:
            stream.truncate(release.MAX_ASSET)
        with self.assertRaisesRegex(ValueError, "2 GiB"):
            release.check_assets("v1.2.3", self.root)
